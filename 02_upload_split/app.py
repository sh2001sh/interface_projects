from runtime_config import apply_runtime_environment
# 接口2: 文档上传与智能分割
# POST /api/data/upload_split

from flask import Flask, request, jsonify
from splitter import DocumentSplitter
import io
import json
import os
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

# 添加shared模块路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.mysql_client import MySQLClient
from database.models import Block
from protocol_conversion import build_protocol_doc_index
from utils.file_store import FileStore
from protocol_extractor import enrich_protocol_metadata
from document_processing import process_document_with_pages
from job_runtime import (
    build_status_response,
    build_stream_response,
    build_submit_response,
    complete_job,
    fail_job,
    start_job,
    update_job,
)


apply_runtime_environment()

app = Flask(__name__)
splitter = DocumentSplitter()

# 初始化共享模块
mysql_client = MySQLClient()
file_store = FileStore()
try:
    mysql_client.init_tables()
except Exception as exc:
    print(f"数据库表初始化失败: {exc}")

UPLOAD_DIR = "/tmp/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

SUPPORTED_EXTS = [
    ".pdf",
    ".docx",
    ".xlsx",
    ".xls",
    ".txt",
    ".md",
    ".py",
    ".java",
    ".js",
    ".cpp",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
]
TEXT_BASED_EXTS = {".txt", ".md", ".py", ".java", ".js", ".cpp", ".json", ".xml", ".yaml", ".yml"}
DEFAULT_MAX_FILE_SIZE_MB = 50
AUTO_ASYNC_TRIGGER_MB = 20
DEFAULT_PAGE_BATCH_SIZE = 100
MIN_READABLE_CHAR_COUNT = 20
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
OLE_SIGNATURE = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"


def split_large_content(
    content: str,
    page_num: int,
    block_type: str,
    file_ext: str,
    base_metadata: dict | None = None,
) -> list:
    """
    对大块内容进行分割，保持页码和类型信息
    返回: [{"page_num": int, "content": str, "type": str}, ...]
    """
    if file_ext == "pdf":
        splitter_type = "pdf"
    elif file_ext in {"docx", "xlsx", "xls"}:
        splitter_type = "docx"
    else:
        splitter_type = "txt"
    splitter_instance = splitter.get_splitter(doc_type=splitter_type)

    if hasattr(splitter_instance, "split_text"):
        chunks = splitter_instance.split_text(content)
    else:
        docs = splitter_instance.create_documents([content])
        chunks = [d.page_content for d in docs]

    result = []
    for idx, chunk in enumerate(chunks):
        if chunk.strip():
            metadata = dict(base_metadata or {})
            metadata.update({"chunk_index": idx, "total_chunks": len(chunks)})
            result.append({
                "page_num": page_num,
                "content": chunk.strip(),
                "type": block_type,
                "metadata": metadata
            })

    return result


def process_file(
    file_path: str,
    project_id: str,
    file_name: str,
    enable_llm_postprocess: bool = False,
    page_batch_size: int = DEFAULT_PAGE_BATCH_SIZE,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> tuple:
    """
    处理文件，返回(blocks, total_pages)
    blocks: 用于响应的块列表
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext in {".pdf", ".docx", ".xlsx", ".xls", ".md"}:
        raw_blocks = process_document_with_pages(
            file_path,
            enable_llm_postprocess=enable_llm_postprocess,
            page_batch_size=page_batch_size,
            progress_callback=progress_callback,
            progress_start=5.0,
            progress_end=70.0,
        )
    else:
        # 其他文件类型使用原有逻辑
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        documents = splitter.split_to_documents(content, file_path=file_path)
        raw_blocks = []
        for idx, doc in enumerate(documents):
            metadata = doc.metadata if isinstance(doc.metadata, dict) else {}
            block = {
                "page_num": idx + 1,
                "content": doc.page_content,
                "type": "text",
                "metadata": dict(metadata),
            }
            raw_blocks.append(enrich_protocol_metadata(block, enable_llm_postprocess))

    # 对大块内容进行分割
    final_blocks = []
    total_raw_blocks = len(raw_blocks)
    for index, raw_block in enumerate(raw_blocks, start=1):
        content = raw_block["content"]
        # 如果内容较长，进行分割
        if raw_block.get("type") == "table":
            final_blocks.append(raw_block)
        elif len(content) > 1000:
            split_blocks = split_large_content(
                content,
                raw_block["page_num"],
                raw_block["type"],
                ext.lstrip("."),
                base_metadata=raw_block.get("metadata", {}),
            )
            final_blocks.extend(split_blocks)
        else:
            final_blocks.append(raw_block)
        if index == total_raw_blocks or index % 50 == 0:
            split_progress = 70.0 + (index / max(total_raw_blocks, 1)) * 20.0
            _emit_progress(
                progress_callback,
                stage="splitting_blocks",
                message=f"已完成内容块切分 {index} / {total_raw_blocks}",
                progress=split_progress,
                processed_blocks=index,
                total_blocks=total_raw_blocks,
            )

    # 计算总页数
    if ext == ".pdf":
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
    else:
        # 对于非PDF文件，使用最大页码值
        total_pages = max([b["page_num"] for b in final_blocks]) if final_blocks else 0

    _emit_progress(
        progress_callback,
        stage="processing_completed",
        message="文件解析与分块完成",
        progress=92.0,
        processed_pages=total_pages,
        total_pages=total_pages,
        processed_blocks=len(final_blocks),
        total_blocks=len(final_blocks),
    )
    return final_blocks, total_pages


def _file_size_bytes(uploaded_file) -> int:
    current_pos = uploaded_file.stream.tell()
    uploaded_file.stream.seek(0, os.SEEK_END)
    size = uploaded_file.stream.tell()
    uploaded_file.stream.seek(current_pos)
    return size


def _read_uploaded_bytes(uploaded_file) -> bytes:
    uploaded_file.stream.seek(0)
    file_bytes = uploaded_file.read()
    uploaded_file.stream.seek(0)
    return file_bytes


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _persist_uploaded_file(uploaded_file, prefix: str = "upload") -> tuple[str, str]:
    file_name = os.path.basename((uploaded_file.filename or "").strip())
    file_path = os.path.join(UPLOAD_DIR, f"{prefix}_{uuid.uuid4().hex}_{file_name}")
    uploaded_file.save(file_path)
    return file_path, file_name


def _parse_positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _emit_progress(
    callback: Optional[Callable[[dict], None]],
    *,
    stage: str,
    message: str,
    progress: Optional[float] = None,
    processed_pages: Optional[int] = None,
    total_pages: Optional[int] = None,
    processed_blocks: Optional[int] = None,
    total_blocks: Optional[int] = None,
) -> None:
    if callback is None:
        return
    payload = {
        "stage": stage,
        "message": message,
    }
    if progress is not None:
        payload["progress"] = round(float(progress), 4)
    if processed_pages is not None:
        payload["processed_pages"] = int(processed_pages)
    if total_pages is not None:
        payload["total_pages"] = int(total_pages)
    if processed_blocks is not None:
        payload["processed_blocks"] = int(processed_blocks)
    if total_blocks is not None:
        payload["total_blocks"] = int(total_blocks)
    callback(payload)


def _looks_like_text_content(file_bytes: bytes) -> bool:
    if not file_bytes or b"\x00" in file_bytes:
        return False

    sample = file_bytes[:4096]
    printable = sum(
        1
        for byte in sample
        if byte in {9, 10, 13} or 32 <= byte <= 126 or byte >= 128
    )
    if printable / max(len(sample), 1) < 0.85:
        return False

    try:
        return bool(sample.decode("utf-8", errors="ignore").strip())
    except Exception:
        return False


def _detect_file_authenticity(ext: str, file_bytes: bytes) -> tuple[bool, str, str]:
    if ext == ".pdf":
        if file_bytes.startswith(b"%PDF-"):
            return True, "pdf", "文件头匹配 PDF 签名"
        return False, "unknown", "文件扩展名为 .pdf，但文件头未匹配 PDF 签名"

    if ext in {".docx", ".xlsx"}:
        if not any(file_bytes.startswith(signature) for signature in ZIP_SIGNATURES):
            return False, "unknown", f"文件扩展名为 {ext}，但文件头未匹配 Office ZIP 签名"
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
                members = set(archive.namelist())
        except zipfile.BadZipFile:
            return False, "unknown", "文件扩展名指向 Office 文档，但压缩结构已损坏"

        if ext == ".docx" and "word/document.xml" in members:
            return True, "docx", "文件扩展名与 DOCX 包结构一致"
        if ext == ".xlsx" and "xl/workbook.xml" in members:
            return True, "xlsx", "文件扩展名与 XLSX 包结构一致"

        detected_type = "xlsx" if "xl/workbook.xml" in members else "docx" if "word/document.xml" in members else "unknown"
        return False, detected_type, f"文件扩展名为 {ext}，但包内结构与实际类型不一致"

    if ext == ".xls":
        if file_bytes.startswith(OLE_SIGNATURE):
            return True, "xls", "文件头匹配 XLS 复合文档签名"
        if any(file_bytes.startswith(signature) for signature in ZIP_SIGNATURES):
            return False, "xlsx", "文件扩展名为 .xls，但内容更像 XLSX 压缩包"
        return False, "unknown", "文件扩展名为 .xls，但文件头未匹配 XLS 签名"

    if ext in TEXT_BASED_EXTS:
        if _looks_like_text_content(file_bytes):
            return True, "text", "文本类文件可正常解码且未检测到二进制特征"
        return False, "binary", f"文件扩展名为 {ext}，但内容更像二进制文件"

    return False, "unknown", f"暂未支持识别扩展名 {ext} 的真实性"


def _save_validation_temp_file(file_name: str, file_bytes: bytes) -> str:
    file_path = os.path.join(UPLOAD_DIR, f"validate_{uuid.uuid4().hex}_{file_name}")
    with open(file_path, "wb") as temp_file:
        temp_file.write(file_bytes)
    return file_path


def _analyze_file_content(
    file_path: str,
    file_name: str,
    *,
    progress_callback: Optional[Callable[[dict], None]] = None,
    page_batch_size: int = DEFAULT_PAGE_BATCH_SIZE,
) -> tuple[dict, list[str]]:
    issues = []
    raw_blocks, total_pages = process_file(
        file_path,
        project_id="validation_preview",
        file_name=file_name,
        enable_llm_postprocess=False,
        page_batch_size=page_batch_size,
        progress_callback=progress_callback,
    )
    non_empty_blocks = [block for block in raw_blocks if str(block.get("content", "")).strip()]
    readable_chars = sum(len(str(block.get("content", "")).strip()) for block in non_empty_blocks)
    protocol_field_count = sum(len(block.get("metadata", {}).get("protocol_fields", [])) for block in raw_blocks)

    if not raw_blocks:
        issues.append("未提取到任何内容块")
    if readable_chars < MIN_READABLE_CHAR_COUNT:
        issues.append("提取出的有效文本过少，内容可读性不足")
    if total_pages <= 0:
        issues.append("未识别到有效页数或 Sheet 数")

    return {
        "total_units": total_pages,
        "total_blocks": len(raw_blocks),
        "readable_blocks": len(non_empty_blocks),
        "readable_chars": readable_chars,
        "protocol_field_count": protocol_field_count,
    }, issues


def _detect_file_authenticity_from_path(ext: str, file_path: str) -> tuple[bool, str, str]:
    if ext == ".pdf":
        with open(file_path, "rb") as fp:
            header = fp.read(8)
        if header.startswith(b"%PDF-"):
            return True, "pdf", "文件头匹配 PDF 签名"
        return False, "unknown", "文件扩展名为 .pdf，但文件头未匹配 PDF 签名"

    if ext in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(file_path) as archive:
                members = set(archive.namelist())
        except zipfile.BadZipFile:
            return False, "unknown", "文件扩展名指向 Office 文档，但压缩结构已损坏"
        except Exception as exc:
            return False, "unknown", f"Office 文档读取失败: {exc}"

        if ext == ".docx" and "word/document.xml" in members:
            return True, "docx", "文件扩展名与 DOCX 包结构一致"
        if ext == ".xlsx" and "xl/workbook.xml" in members:
            return True, "xlsx", "文件扩展名与 XLSX 包结构一致"

        detected_type = "xlsx" if "xl/workbook.xml" in members else "docx" if "word/document.xml" in members else "unknown"
        return False, detected_type, f"文件扩展名为 {ext}，但包内结构与实际类型不一致"

    if ext == ".xls":
        with open(file_path, "rb") as fp:
            header = fp.read(8)
        if header.startswith(OLE_SIGNATURE):
            return True, "xls", "文件头匹配 XLS 复合文档签名"
        if any(header.startswith(signature) for signature in ZIP_SIGNATURES):
            return False, "xlsx", "文件扩展名为 .xls，但内容更像 XLSX 压缩包"
        return False, "unknown", "文件扩展名为 .xls，但文件头未匹配 XLS 签名"

    if ext in TEXT_BASED_EXTS:
        with open(file_path, "rb") as fp:
            sample = fp.read(4096)
        if _looks_like_text_content(sample):
            return True, "text", "文本类文件可正常解码且未检测到二进制特征"
        return False, "binary", f"文件扩展名为 {ext}，但内容更像二进制文件"

    return False, "unknown", f"暂未支持识别扩展名 {ext} 的真实性"


def _validate_file_at_path(
    file_path: str,
    file_name: str,
    max_size_mb: int,
    *,
    progress_callback: Optional[Callable[[dict], None]] = None,
    page_batch_size: int = DEFAULT_PAGE_BATCH_SIZE,
) -> dict:
    ext = os.path.splitext(file_name)[1].lower()
    size_bytes = os.path.getsize(file_path)
    max_size_bytes = max_size_mb * 1024 * 1024
    checks = {}
    issues = []
    metrics = {
        "total_units": 0,
        "total_blocks": 0,
        "readable_blocks": 0,
        "readable_chars": 0,
        "protocol_field_count": 0,
    }
    detected_type = "unknown"
    temp_file_path = ""

    extension_passed = bool(file_name) and ext in SUPPORTED_EXTS
    checks["extension"] = {
        "passed": extension_passed,
        "actual": ext or "",
        "supported": SUPPORTED_EXTS,
        "message": "扩展名受支持" if extension_passed else f"不支持的文件类型: {ext or '未知'}",
    }
    if not extension_passed:
        issues.append(checks["extension"]["message"])

    size_passed = 0 < size_bytes <= max_size_bytes
    checks["file_size"] = {
        "passed": size_passed,
        "actual_bytes": size_bytes,
        "max_bytes": max_size_bytes,
        "message": "文件大小符合限制" if size_passed else f"文件大小超出限制，当前 {size_bytes} bytes，上限 {max_size_bytes} bytes",
    }
    if not size_passed:
        issues.append(checks["file_size"]["message"])
    _emit_progress(
        progress_callback,
        stage="precheck",
        message="已完成文件基础检查",
        progress=3.0,
    )

    authenticity_passed = False
    authenticity_message = "扩展名非法，跳过真实性校验"
    if extension_passed:
        authenticity_passed, detected_type, authenticity_message = _detect_file_authenticity_from_path(ext, file_path)
        if not authenticity_passed:
            issues.append(authenticity_message)
    checks["authenticity"] = {
        "passed": authenticity_passed,
        "detected_type": detected_type,
        "message": authenticity_message,
    }
    _emit_progress(
        progress_callback,
        stage="authenticity",
        message=authenticity_message,
        progress=5.0,
    )

    readability_passed = False
    completeness_passed = False
    readability_message = "前置检查未通过，跳过内容可读性分析"
    completeness_message = "前置检查未通过，跳过完整性分析"
    parse_error = None

    if extension_passed and size_passed and authenticity_passed:
        try:
            temp_file_path = file_path
            metrics, content_issues = _analyze_file_content(
                file_path,
                file_name,
                progress_callback=progress_callback,
                page_batch_size=page_batch_size,
            )
            readability_passed = metrics["readable_chars"] >= MIN_READABLE_CHAR_COUNT and metrics["readable_blocks"] > 0
            completeness_passed = metrics["total_units"] > 0 and metrics["total_blocks"] > 0
            readability_message = (
                "文件内容可正常读取"
                if readability_passed
                else "可提取内容不足，无法确认文档具备稳定可读性"
            )
            completeness_message = (
                "文件已提取出有效内容块"
                if completeness_passed
                else "未提取出完整内容块或页/Sheet 信息"
            )
            issues.extend(content_issues)
        except Exception as exc:
            parse_error = str(exc)
            issues.append(f"内容解析失败: {parse_error}")
            readability_message = "内容解析失败，无法完成可读性校验"
            completeness_message = "内容解析失败，无法完成完整性校验"

    checks["readability"] = {
        "passed": readability_passed,
        "message": readability_message,
        "metrics": {
            "readable_blocks": metrics["readable_blocks"],
            "readable_chars": metrics["readable_chars"],
        },
    }
    checks["completeness"] = {
        "passed": completeness_passed,
        "message": completeness_message,
        "metrics": {
            "total_units": metrics["total_units"],
            "total_blocks": metrics["total_blocks"],
            "protocol_field_count": metrics["protocol_field_count"],
        },
    }

    result = {
        "file_name": file_name,
        "extension": ext,
        "size_bytes": size_bytes,
        "max_size_mb": max_size_mb,
        "detected_type": detected_type,
        "valid": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "issues": sorted(set(issue for issue in issues if issue)),
    }
    if parse_error:
        result["parse_error"] = parse_error
    _emit_progress(
        progress_callback,
        stage="validation_completed",
        message="文件校验完成",
        progress=100.0,
    )
    return result


def _validate_uploaded_file(uploaded_file, max_size_mb: int, page_batch_size: int = DEFAULT_PAGE_BATCH_SIZE) -> dict:
    file_path, file_name = _persist_uploaded_file(uploaded_file, prefix="validate")
    try:
        return _validate_file_at_path(
            file_path,
            file_name,
            max_size_mb=max_size_mb,
            page_batch_size=page_batch_size,
        )
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


def _collect_uploaded_files() -> list:
    collected = []
    seen = set()
    for uploaded_file in request.files.getlist("files") + [request.files.get("file")]:
        if not uploaded_file or not getattr(uploaded_file, "filename", ""):
            continue
        identity = id(uploaded_file)
        if identity in seen:
            continue
        seen.add(identity)
        collected.append(uploaded_file)
    return collected


def _request_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith('['):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [part.strip() for part in raw.split(',')]
        else:
            items = [part.strip() for part in raw.split(',')]
    else:
        items = [value]
    normalized = []
    for item in items:
        item_str = str(item or '').strip()
        if item_str:
            normalized.append(item_str)
    return normalized


def _collect_input_path_entries() -> list[dict[str, Any]]:
    payload = _request_payload()
    candidates = []
    if payload:
        candidates.extend(_normalize_string_list(payload.get('file_paths')))
        single_path = str(payload.get('file_path') or '').strip()
        if single_path:
            candidates.append(single_path)
    else:
        candidates.extend(_normalize_string_list(request.form.getlist('file_paths')))
        single_path = str(request.form.get('file_path') or '').strip()
        if single_path:
            candidates.append(single_path)

    entries = []
    seen = set()
    for raw_path in candidates:
        file_path = os.path.abspath(os.path.expanduser(str(raw_path).strip()))
        if not file_path or file_path in seen:
            continue
        seen.add(file_path)
        entries.append({
            'file_path': file_path,
            'file_name': os.path.basename(file_path),
            'cleanup_after': False,
        })
    return entries


def _normalize_return_mode(raw_value: Any) -> str:
    value = str(raw_value or 'content').strip().lower()
    if value not in {'content', 'path', 'both'}:
        raise ValueError('return_mode仅支持 content、path、both')
    return value


def _resolve_upload_split_input() -> dict[str, Any]:
    payload = _request_payload()
    project_id = str((payload.get('project_id') if payload else request.form.get('project_id')) or '').strip()
    dataset_id = str((payload.get('dataset_id') if payload else request.form.get('dataset_id')) or '').strip() or None
    document_id = str((payload.get('document_id') if payload else request.form.get('document_id')) or '').strip() or None
    enable_llm_value = payload.get('enable_llm_postprocess') if payload else request.form.get('enable_llm_postprocess', 'false')
    enable_llm_postprocess = _as_bool(enable_llm_value, default=False)
    async_value = payload.get('async') if payload else request.form.get('async')
    page_batch_raw = payload.get('page_batch_size') if payload else request.form.get('page_batch_size')
    page_batch_size = _parse_positive_int(page_batch_raw, DEFAULT_PAGE_BATCH_SIZE)
    return_mode_raw = payload.get('return_mode') if payload else request.form.get('return_mode', 'content')
    return_mode = _normalize_return_mode(return_mode_raw)

    uploaded_file = request.files.get('file')
    if uploaded_file and getattr(uploaded_file, 'filename', ''):
        return {
            'project_id': project_id,
            'dataset_id': dataset_id,
            'document_id': document_id,
            'enable_llm_postprocess': enable_llm_postprocess,
            'async_requested': _as_bool(async_value, default=False),
            'page_batch_size': page_batch_size,
            'return_mode': return_mode,
            'input_entry': None,
            'uploaded_file': uploaded_file,
        }

    path_entries = _collect_input_path_entries()
    return {
        'project_id': project_id,
        'dataset_id': dataset_id,
        'document_id': document_id,
        'enable_llm_postprocess': enable_llm_postprocess,
        'async_requested': _as_bool(async_value, default=False),
        'page_batch_size': page_batch_size,
        'return_mode': return_mode,
        'input_entry': path_entries[0] if path_entries else None,
        'uploaded_file': None,
    }


def _build_doc_index_response(project_id: str, doc_index_result: dict[str, Any]) -> dict[str, Any]:
    return {
        'doc_set_id': doc_index_result['doc_set_id'],
        'index_ref': doc_index_result['index_ref'],
        'status': doc_index_result['status'],
        'document_count': doc_index_result['document_count'],
        'storage_path': f"data/pageindex_registry/{project_id}/{doc_index_result['doc_set_id']}.json",
    }


def _build_upload_split_response_data(
    *,
    raw_blocks: list[dict[str, Any]],
    total_pages: int,
    project_id: str,
    file_name: str,
    source_file_path: Optional[str] = None,
    return_mode: str = 'content',
    dataset_id: Optional[str] = None,
    document_id: Optional[str] = None,
) -> dict[str, Any]:
    db_blocks = []
    for block_data in raw_blocks:
        db_blocks.append(
            Block(
                block_id=0,
                project_id=project_id,
                file_name=file_name,
                page_num=block_data['page_num'],
                content=block_data['content'],
                block_type=block_data['type'],
                metadata=block_data.get('metadata', {}),
            )
        )

    db_block_ids = []
    try:
        db_block_ids = mysql_client.insert_blocks(db_blocks)
    except Exception as db_error:
        print(f'数据库保存失败: {str(db_error)}')

    if db_block_ids:
        for block, db_id in zip(db_blocks, db_block_ids):
            block.block_id = db_id
    else:
        for idx, block in enumerate(db_blocks, start=1):
            block.block_id = idx

    response_blocks = []
    blocks_for_store = []
    for block in db_blocks:
        protocol_fields = block.metadata.get('protocol_fields', []) if isinstance(block.metadata, dict) else []
        response_blocks.append({
            'block_id': block.block_id,
            'page_num': block.page_num,
            'content': block.content,
            'type': block.block_type,
            'block_type': block.block_type,
            'cleaned_content': block.cleaned_content,
            'file_name': block.file_name,
            'project_id': block.project_id,
            'metadata': block.metadata,
            'protocol_fields': protocol_fields,
        })
        blocks_for_store.append({
            'block_id': block.block_id,
            'page_num': block.page_num,
            'content': block.content,
            'type': block.block_type,
            'block_type': block.block_type,
            'cleaned_content': block.cleaned_content,
            'file_name': block.file_name,
            'project_id': block.project_id,
            'metadata': block.metadata,
        })

    blocks_file_path = ''
    if return_mode in {'path', 'both'}:
        task_id = f"tsp_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        try:
            blocks_file_path = file_store.save_blocks(project_id, blocks_for_store, file_name=f'{task_id}_blocks.json')
        except Exception as store_error:
            print(f'文件存储失败: {str(store_error)}')

    doc_index = None
    try:
        doc_index_result = build_protocol_doc_index(
            project_id=project_id,
            blocks=db_blocks,
            file_names=[file_name],
            document_paths=[source_file_path] if source_file_path else None,
            source_block_ids=[block.block_id for block in db_blocks],
            file_store=file_store,
        )
        doc_index = _build_doc_index_response(project_id, doc_index_result)
    except Exception as index_error:
        print(f'文档索引创建失败: {str(index_error)}')

    table_count = sum(1 for block in response_blocks if block['type'] == 'table')
    text_count = len(response_blocks) - table_count
    protocol_field_count = sum(len(block.get('protocol_fields', [])) for block in response_blocks)

    return {
        'project_id': project_id,
        'dataset_id': dataset_id,
        'document_id': document_id,
        'payload_type': 'blocks',
        'source_file_path': source_file_path or None,
        'file_name': file_name,
        'total_pages': total_pages,
        'total_blocks': len(response_blocks),
        'table_count': table_count,
        'text_count': text_count,
        'protocol_field_count': protocol_field_count,
        'blocks_file_path': blocks_file_path or None if return_mode in {'path', 'both'} else None,
        'doc_index': doc_index,
        'blocks': response_blocks if return_mode in {'content', 'both'} else None,
    }


def _should_async(requested: bool, file_sizes: list[int]) -> bool:
    if requested:
        return True
    threshold = AUTO_ASYNC_TRIGGER_MB * 1024 * 1024
    return any(size >= threshold for size in file_sizes)


def _run_validate_job(job_id: str, saved_files: list[dict], max_size_mb: int, page_batch_size: int) -> None:
    try:
        total = len(saved_files)
        results = []
        update_job(job_id, status='running', stage='validating', message='开始校验上传文件', progress=1.0)
        for index, item in enumerate(saved_files, start=1):
            update_job(
                job_id,
                status='running',
                stage='validating',
                message=f"正在校验第 {index}/{total} 个文件: {item['file_name']}",
                progress=((index - 1) / max(total, 1)) * 90.0 + 5.0,
            )
            file_base_progress = ((index - 1) / max(total, 1)) * 90.0 + 5.0
            file_weight = 90.0 / max(total, 1)

            def file_progress(payload: dict) -> None:
                local_progress = float(payload.get('progress', 0.0))
                overall_progress = file_base_progress + (local_progress / 100.0) * file_weight
                update_job(
                    job_id,
                    status='running',
                    stage=str(payload.get('stage') or 'validating'),
                    message=str(payload.get('message') or f"正在校验第 {index}/{total} 个文件"),
                    progress=overall_progress,
                    extra={
                        'processed_pages': payload.get('processed_pages'),
                        'total_pages': payload.get('total_pages'),
                        'processed_blocks': payload.get('processed_blocks'),
                        'total_blocks': payload.get('total_blocks'),
                    },
                )

            result = _validate_file_at_path(
                item['file_path'],
                item['file_name'],
                max_size_mb=max_size_mb,
                progress_callback=file_progress,
                page_batch_size=page_batch_size,
            )
            result['file_path'] = item['file_path']
            results.append(result)
        passed_files = sum(1 for result in results if result['valid'])
        complete_job(
            job_id,
            {
                'code': 200,
                'message': 'success',
                'data': {
                    'summary': {
                        'total_files': len(results),
                        'passed_files': passed_files,
                        'failed_files': len(results) - passed_files,
                    },
                    'results': results,
                },
            },
        )
    except Exception as exc:
        fail_job(job_id, f'文件校验失败: {exc}')
    finally:
        for item in saved_files:
            if not item.get('cleanup_after'):
                continue
            file_path = item.get('file_path')
            if file_path and os.path.exists(file_path):
                os.remove(file_path)


def _run_upload_split_job(
    job_id: str,
    *,
    file_path: str,
    file_name: str,
    project_id: str,
    dataset_id: Optional[str],
    document_id: Optional[str],
    enable_llm_postprocess: bool,
    page_batch_size: int,
    return_mode: str,
    cleanup_after: bool = False,
) -> None:
    try:
        update_job(job_id, status='running', stage='processing', message=f'开始解析文件: {file_name}', progress=5.0)

        def split_progress(payload: dict) -> None:
            update_job(
                job_id,
                status='running',
                stage=str(payload.get('stage') or 'processing'),
                message=str(payload.get('message') or f'正在解析文件: {file_name}'),
                progress=float(payload.get('progress', 5.0)),
                extra={
                    'processed_pages': payload.get('processed_pages'),
                    'total_pages': payload.get('total_pages'),
                    'processed_blocks': payload.get('processed_blocks'),
                    'total_blocks': payload.get('total_blocks'),
                },
            )

        raw_blocks, total_pages = process_file(
            file_path,
            project_id,
            file_name,
            enable_llm_postprocess=enable_llm_postprocess,
            page_batch_size=page_batch_size,
            progress_callback=split_progress,
        )
        update_job(job_id, status='running', stage='persisting', message='解析完成，正在保存块信息与文档索引', progress=75.0)
        response_data = _build_upload_split_response_data(
            raw_blocks=raw_blocks,
            total_pages=total_pages,
            project_id=project_id,
            file_name=file_name,
            source_file_path=file_path,
            return_mode=return_mode,
            dataset_id=dataset_id,
            document_id=document_id,
        )
        complete_job(job_id, {'code': 200, 'message': 'success', 'data': response_data})
    except Exception as exc:
        fail_job(job_id, f'上传拆分失败: {exc}')
    finally:
        if cleanup_after and file_path and os.path.exists(file_path):
            os.remove(file_path)


@app.route("/api/data/upload_split", methods=["POST"])
def upload_split():
    """文档上传与智能分割接口。支持上传文件或本地文件路径输入。"""
    try:
        request_payload = _resolve_upload_split_input()
    except ValueError as e:
        return jsonify({'code': 400, 'message': str(e)}), 400
    project_id = request_payload['project_id']
    dataset_id = request_payload['dataset_id']
    document_id = request_payload['document_id']
    enable_llm_postprocess = request_payload['enable_llm_postprocess']
    page_batch_size = request_payload['page_batch_size']
    return_mode = request_payload['return_mode']
    uploaded_file = request_payload['uploaded_file']
    input_entry = request_payload['input_entry']

    if not project_id:
        return jsonify({'code': 400, 'message': '缺少必要参数: project_id'}), 400
    if uploaded_file is None and input_entry is None:
        return jsonify({'code': 400, 'message': '缺少必要参数: file 或 file_path'}), 400

    cleanup_after = False
    if uploaded_file is not None:
        file_name = uploaded_file.filename
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in SUPPORTED_EXTS:
            return jsonify({'code': 400, 'message': f"不支持的文件类型: {ext}，支持的类型: {', '.join(SUPPORTED_EXTS)}"}), 400
        size_bytes = _file_size_bytes(uploaded_file)
        async_requested = _should_async(request_payload['async_requested'], [size_bytes])
        if async_requested:
            file_path, normalized_file_name = _persist_uploaded_file(uploaded_file, prefix='upload_split_job')
            cleanup_after = True
        else:
            file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{file_name}")
            uploaded_file.save(file_path)
            normalized_file_name = file_name
            cleanup_after = True
    else:
        file_path = input_entry['file_path']
        normalized_file_name = input_entry['file_name']
        ext = os.path.splitext(normalized_file_name)[1].lower()
        if ext not in SUPPORTED_EXTS:
            return jsonify({'code': 400, 'message': f"不支持的文件类型: {ext}，支持的类型: {', '.join(SUPPORTED_EXTS)}"}), 400
        try:
            size_bytes = os.path.getsize(file_path)
        except OSError:
            return jsonify({'code': 400, 'message': f'文件不存在或不可读: {file_path}'}), 400
        async_requested = _should_async(request_payload['async_requested'], [size_bytes])

    if async_requested:
        job = start_job(
            'upload_split',
            lambda job_id: _run_upload_split_job(
                job_id,
                file_path=file_path,
                file_name=normalized_file_name,
                project_id=project_id,
                dataset_id=dataset_id,
                document_id=document_id,
                enable_llm_postprocess=enable_llm_postprocess,
                page_batch_size=page_batch_size,
                return_mode=return_mode,
                cleanup_after=cleanup_after,
            ),
            metadata={
                'project_id': project_id,
                'dataset_id': dataset_id,
                'document_id': document_id,
                'file_name': normalized_file_name,
                'file_path': file_path,
                'size_bytes': size_bytes,
                'async_mode': True,
                'auto_triggered': not request_payload['async_requested'],
                'page_batch_size': page_batch_size,
                'return_mode': return_mode,
            },
        )
        return build_submit_response(job)

    try:
        raw_blocks, total_pages = process_file(
            file_path,
            project_id,
            normalized_file_name,
            enable_llm_postprocess=enable_llm_postprocess,
            page_batch_size=page_batch_size,
        )
        response_data = _build_upload_split_response_data(
            raw_blocks=raw_blocks,
            total_pages=total_pages,
            project_id=project_id,
            file_name=normalized_file_name,
            source_file_path=file_path,
            return_mode=return_mode,
            dataset_id=dataset_id,
            document_id=document_id,
        )
        return jsonify({'code': 200, 'message': 'success', 'data': response_data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'code': 500, 'message': f'处理失败: {str(e)}'}), 500
    finally:
        if cleanup_after and os.path.exists(file_path):
            os.remove(file_path)


@app.route("/api/data/upload_split/status", methods=["GET"])
def upload_split_status():
    job_id = str(request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    return build_status_response(job_id)


@app.route("/api/data/upload_split/stream", methods=["GET"])
def upload_split_stream():
    job_id = str(request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    return build_stream_response(job_id)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})

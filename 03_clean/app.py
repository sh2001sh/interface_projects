# 接口2: 数据自动化清洗
# POST /api/data/clean

import json
import re
import sys
import os
import time
import uuid
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from flask import Flask, request, jsonify

# 添加项目根目录到路径以导入shared模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import MySQLClient
from database.models import Block, CleaningIssue
from utils.file_store import FileStore
from runtime_config import apply_runtime_environment, get_service_runner_config
from streaming_utils import is_stream_requested, stream_flask_handler


apply_runtime_environment()

app = Flask(__name__)

# 初始化数据库客户端
db_client = MySQLClient()
file_store = FileStore()


# ==================== 清洗规则实现 ====================

class DataCleaner:
    """数据清洗器 - 实现多种清洗规则"""

    # OCR乱码检测正则模式
    GARBLED_PATTERNS = [
        # 连续特殊符号（排除常见标点组合）
        r'[#$%^&*]{3,}',
        # 随机符号组合（如 @#$%, *&^%）
        r'[@#$%^&*]{4,}',
        # 乱码字符序列（非正常文本）
        r'[^\w\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef.,;:!?\'"()\[\]{}\-–—…·]{5,}',
        # 重复无意义符号
        r'([#$%&*^@!~])\1{3,}',
        # OCR常见乱码模式
        r'[|\\\/]{4,}',
        # 混合乱码符号
        r'[#@$%^&*()]{6,}',
    ]

    # 编译正则表达式
    GARBLED_REGEX = re.compile('|'.join(GARBLED_PATTERNS))

    @classmethod
    def detect_garbled_text(cls, content: str) -> Tuple[bool, str, str]:
        """
        检测并移除OCR乱码字符

        Returns:
            Tuple[has_issue, cleaned_content, description]
        """
        if not content:
            return False, content, ""

        matches = cls.GARBLED_REGEX.findall(content)
        if not matches:
            return False, content, ""

        # 记录发现的乱码
        found_garbled = list(set(matches))[:5]  # 最多记录5种

        # 移除乱码字符
        cleaned = cls.GARBLED_REGEX.sub('', content)

        # 清理多余的空白
        cleaned = re.sub(r' {2,}', ' ', cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

        garbled_str = ', '.join(repr(g) for g in found_garbled[:3])
        description = f"检测到OCR乱码字符: {garbled_str}"
        return True, cleaned.strip(), description

    @classmethod
    def fix_encoding(cls, content: str) -> Tuple[bool, str, str]:
        """
        修复编码问题

        常见的编码问题：
        - 全角/半角标点混用
        - 异常Unicode字符
        - 常见乱码替换
        """
        if not content:
            return False, content, ""

        original = content
        fixed = False
        issues = []

        # 常见编码错误映射
        encoding_fixes = {
            '锟斤拷': '',  # 经典乱码
            '烫烫烫': '',  # 未初始化内存
            '屯屯屯': '',  # 未初始化内存
            '\ufffd': '',  # 替换字符
            '\u0000': '',  # 空字符
        }

        for wrong, correct in encoding_fixes.items():
            if wrong in content:
                content = content.replace(wrong, correct)
                fixed = True
                issues.append(f"修复编码错误: {repr(wrong)}")

        # 检查并修复异常控制字符（保留换行、制表符）
        control_pattern = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
        if control_pattern.search(content):
            content = control_pattern.sub('', content)
            fixed = True
            issues.append("移除异常控制字符")

        if fixed:
            return True, content, '; '.join(issues)
        return False, original, ""

    @classmethod
    def normalize_whitespace(cls, content: str) -> Tuple[bool, str, str]:
        """
        规范化空白字符

        处理：
        - 多个连续空格压缩为单个
        - 多个连续换行压缩为最多两个
        - 行首行尾空白去除
        - 制表符转空格
        """
        if not content:
            return False, content, ""

        original = content

        # 制表符转空格
        content = content.replace('\t', '    ')

        # 压缩连续空格（不在行首）
        content = re.sub(r'(?<!\n) {2,}', ' ', content)

        # 压缩连续换行为最多两个
        content = re.sub(r'\n{3,}', '\n\n', content)

        # 去除每行首尾空白
        lines = [line.strip() for line in content.split('\n')]
        content = '\n'.join(lines)

        # 去除整体首尾空白
        content = content.strip()

        if content != original:
            return True, content, "规范化空白字符"
        return False, content, ""

    @classmethod
    def fix_broken_table(cls, content: str) -> Tuple[bool, str, str]:
        """
        修复破损的表格格式

        处理：
        - 表格分隔线断裂
        - 单元格对齐问题
        - 表格行合并错误
        """
        if not content:
            return False, content, ""

        original = content
        issues = []

        # 检测是否包含表格特征
        table_indicators = [
            r'\|.*\|',  # 管道符分隔
            r'[-+]{3,}',  # 分隔线
            r'^\s*\+[-+]+\+',  # 表格边框
        ]

        has_table = any(re.search(p, content, re.MULTILINE) for p in table_indicators)
        if not has_table:
            return False, content, ""

        # 修复管道符表格
        lines = content.split('\n')
        fixed_lines = []

        for line in lines:
            fixed_line = line

            # 修复断开的表格行（以|开头但不以|结尾）
            if line.strip().startswith('|') and not line.strip().endswith('|'):
                fixed_line = line.rstrip() + ' |'
                issues.append("修复表格行结尾")

            # 修复多余的管道符
            if re.search(r'\|{3,}', line):
                fixed_line = re.sub(r'\|{3,}', '||', line)
                issues.append("修复多余管道符")

            # 修复表格分隔线
            if re.match(r'^\s*\|[\s\-:]+\|\s*$', line):
                # 确保分隔线完整
                if '|' in line:
                    parts = line.split('|')
                    if len(parts) > 2:
                        # 标准化分隔线格式
                        fixed_line = '|' + '|'.join(p.strip() or '---' for p in parts[1:-1]) + '|'
                        issues.append("修复表格分隔线")

            fixed_lines.append(fixed_line)

        content = '\n'.join(fixed_lines)

        if content != original:
            unique_issues = list(set(issues))[:3]
            return True, content, '; '.join(unique_issues)
        return False, original, ""

    @classmethod
    def detect_duplicate(cls, content: str, content_hash_dict: Dict[str, int] = None) -> Tuple[bool, str, str]:
        """
        检测重复内容

        检测：
        - 行级重复
        - 段落级重复
        - 与其他块的内容重复
        """
        if not content:
            return False, content, ""

        issues = []
        original = content

        # 检测连续重复行
        lines = content.split('\n')
        seen_lines = {}
        dedup_lines = []

        for i, line in enumerate(lines):
            line_stripped = line.strip()
            if not line_stripped:  # 保留空行
                dedup_lines.append(line)
                continue

            if line_stripped in seen_lines:
                # 检查是否是连续重复
                if i > 0 and lines[i-1].strip() == line_stripped:
                    issues.append(f"检测到连续重复行")
                    continue  # 跳过连续重复行
            else:
                seen_lines[line_stripped] = i

            dedup_lines.append(line)

        content = '\n'.join(dedup_lines)

        # 检测段落级重复
        paragraphs = re.split(r'\n\s*\n', content)
        seen_paras = {}
        dedup_paras = []

        for para in paragraphs:
            para_stripped = para.strip()
            if not para_stripped:
                dedup_paras.append(para)
                continue

            # 简单哈希用于检测相似段落
            para_hash = hash(para_stripped[:100])  # 使用前100字符做哈希

            if para_hash in seen_paras:
                issues.append("检测到重复段落")
                continue

            seen_paras[para_hash] = True
            dedup_paras.append(para)

        if issues:
            content = '\n\n'.join(dedup_paras)
            return True, content, '; '.join(list(set(issues))[:3])
        return False, original, ""


def clean_block(block: Block) -> Tuple[Block, List[CleaningIssue]]:
    """
    对单个Block执行所有清洗规则

    Args:
        block: 待清洗的文档块

    Returns:
        Tuple[清洗后的Block, 清洗问题列表]
    """
    issues = []
    content = block.content or ""

    if not content.strip():
        return block, issues

    block_type = str(block.block_type or "").strip().lower()
    if block_type == "table":
        # Structured tables are the parser output. Preserve them byte-for-byte:
        # symbols such as "******" can be meaningful protocol coding, not noise.
        block.cleaned_content = content
        return block, issues
    else:
        cleaning_rules = [
            ("GARBLED_TEXT", DataCleaner.detect_garbled_text),
            ("ENCODING_FIX", DataCleaner.fix_encoding),
            ("WHITESPACE", DataCleaner.normalize_whitespace),
            ("BROKEN_TABLE", DataCleaner.fix_broken_table),
            ("DUPLICATE", DataCleaner.detect_duplicate),
        ]

    current_content = content

    for rule_name, rule_func in cleaning_rules:
        has_issue, cleaned_content, description = rule_func(current_content)

        if has_issue:
            issue = CleaningIssue(
                block_id=block.block_id,
                issue_type=rule_name,
                description=description,
                original=current_content[:200] + "..." if len(current_content) > 200 else current_content,
                cleaned=cleaned_content[:200] + "..." if len(cleaned_content) > 200 else cleaned_content
            )
            issues.append(issue)
            current_content = cleaned_content

    # 更新block的cleaned_content
    if issues:
        block.cleaned_content = current_content
    else:
        # 无问题则cleaned_content等于原始内容
        block.cleaned_content = content

    return block, issues


def _request_payload() -> Dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _normalize_block_payload(item: Dict[str, Any], default_project_id: str = "") -> Block:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    block_type = item.get("block_type") or item.get("type") or "text"
    return Block(
        block_id=int(item.get("block_id") or 0),
        project_id=str(item.get("project_id") or default_project_id or "file_path_project").strip(),
        file_name=str(item.get("file_name") or metadata.get("file_name") or "").strip(),
        page_num=int(item.get("page_num") or 0),
        content=str(item.get("content") or ""),
        block_type=str(block_type),
        cleaned_content=item.get("cleaned_content"),
        metadata=metadata,
    )


def _serialize_block(block: Block) -> Dict[str, Any]:
    payload = block.to_dict()
    payload["type"] = payload.get("block_type")
    return payload


def _iter_upstream_cleaning_groups(blocks: List[Block]) -> List[Dict[str, Any]]:
    seen = set()
    groups: List[Dict[str, Any]] = []
    for block in blocks:
        metadata = block.metadata if isinstance(block.metadata, dict) else {}
        upstream = metadata.get("upstream_cleaning") if isinstance(metadata.get("upstream_cleaning"), dict) else {}
        try:
            removed_count = int(upstream.get("pdf_removed_count") or 0)
        except (TypeError, ValueError):
            removed_count = 0
        try:
            input_count = int(upstream.get("pdf_input_element_count") or 0)
        except (TypeError, ValueError):
            input_count = 0
        if removed_count <= 0:
            continue
        key = (
            str(upstream.get("stage") or ""),
            input_count,
            str(upstream.get("pdf_kept_element_count") or ""),
            str(upstream.get("pdf_output_block_count") or ""),
            removed_count,
        )
        if key in seen:
            continue
        seen.add(key)
        groups.append({
            "block_id": int(block.block_id or 0),
            "stage": str(upstream.get("stage") or "").strip(),
            "removed_count": removed_count,
            "input_count": max(input_count, 0),
            "kept_count": int(upstream.get("pdf_kept_element_count") or 0) if str(upstream.get("pdf_kept_element_count") or "").strip() else 0,
            "output_block_count": int(upstream.get("pdf_output_block_count") or 0) if str(upstream.get("pdf_output_block_count") or "").strip() else 0,
        })
    return groups


def _get_upstream_cleaning_totals(blocks: List[Block]) -> Tuple[int, int]:
    groups = _iter_upstream_cleaning_groups(blocks)
    removed_total = sum(group["removed_count"] for group in groups)
    input_total = sum(group["input_count"] for group in groups)
    return removed_total, input_total


def _describe_upstream_cleaning_stage(stage: str) -> str:
    normalized = str(stage or "").strip().lower()
    if normalized in {"pdf_layout_postprocess", "pdfplumber_native_fallback"}:
        return "上游PDF预清洗已移除页眉页脚等页面噪声"
    if normalized:
        return f"上游预清洗阶段 {normalized} 已移除异常元素"
    return "上游预清洗已移除异常元素"


def _build_upstream_cleaning_issues(blocks: List[Block]) -> Tuple[List[CleaningIssue], List[int], int, int]:
    issues: List[CleaningIssue] = []
    modified_block_ids: List[int] = []
    removed_total = 0
    input_total = 0

    for group in _iter_upstream_cleaning_groups(blocks):
        removed_count = int(group["removed_count"] or 0)
        if removed_count <= 0:
            continue
        block_id = int(group["block_id"] or 0)
        description = f"{_describe_upstream_cleaning_stage(group['stage'])} {removed_count} 项"
        if group["input_count"] > 0:
            description += f"，输入元素 {group['input_count']}，保留 {group['kept_count']}"
        issues.append(CleaningIssue(
            block_id=block_id,
            issue_type="UPSTREAM_CLEANING",
            description=description,
            original=f"upstream_removed_count={removed_count}",
            cleaned=f"upstream_kept_count={group['kept_count']}",
        ))
        if block_id and block_id not in modified_block_ids:
            modified_block_ids.append(block_id)
        removed_total += removed_count
        input_total += int(group["input_count"] or 0)

    return issues, modified_block_ids, removed_total, input_total


def _load_blocks_from_file(blocks_file_path: str, project_id_hint: str = "") -> Tuple[str, List[Block]]:
    resolved_path = os.path.abspath(os.path.expanduser(str(blocks_file_path or "").strip()))
    if not resolved_path or not os.path.exists(resolved_path):
        raise FileNotFoundError(f"blocks_file_path不存在: {blocks_file_path}")
    with open(resolved_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("blocks文件内容必须为JSON对象")
    blocks_payload = payload.get("blocks")
    if not isinstance(blocks_payload, list) or not blocks_payload:
        raise ValueError("blocks文件缺少blocks数组")
    project_id = str(project_id_hint or payload.get("project_id") or "").strip()
    blocks = [_normalize_block_payload(item, default_project_id=project_id) for item in blocks_payload if isinstance(item, dict)]
    if not blocks:
        raise ValueError("blocks文件中没有可用块")
    project_id = project_id or blocks[0].project_id or f"proj_{int(time.time())}"
    for block in blocks:
        if not block.project_id:
            block.project_id = project_id
    return project_id, blocks


def _save_cleaned_blocks_output(project_id: str, source_path: str, blocks: List[Block]) -> str:
    serialized_blocks = [_serialize_block(block) for block in blocks]
    try:
        if project_id:
            return file_store.save_cleaned_blocks(project_id, serialized_blocks)
    except Exception:
        pass
    target_dir = os.path.dirname(os.path.abspath(source_path)) or os.getcwd()
    target_path = os.path.join(target_dir, f"cleaned_{int(time.time())}_{uuid.uuid4().hex[:8]}.json")
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump({
            "project_id": project_id or None,
            "total_blocks": len(serialized_blocks),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "blocks": serialized_blocks,
        }, f, ensure_ascii=False, indent=2)
    return target_path


def _normalize_return_mode(raw_value: Any) -> str:
    value = str(raw_value or "content").strip().lower()
    if value not in {"content", "path", "both"}:
        raise ValueError("return_mode仅支持 content、path、both")
    return value


def _load_blocks_from_payload(payload: Any, project_id_hint: str = "") -> Tuple[str, List[Block]]:
    if isinstance(payload, list):
        block_items = payload
        payload_project_id = project_id_hint
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), dict):
            return _load_blocks_from_payload(payload.get("data"), project_id_hint=project_id_hint)
        block_items = payload.get("blocks") or payload.get("cleaned_blocks") or payload.get("items")
        payload_project_id = str(payload.get("project_id") or project_id_hint or "").strip()
    else:
        raise ValueError("载荷内容不是有效的blocks JSON")

    if not isinstance(block_items, list) or not block_items:
        raise ValueError("载荷内容缺少blocks数组")

    project_id = payload_project_id
    blocks = [_normalize_block_payload(item, default_project_id=project_id) for item in block_items if isinstance(item, dict)]
    if not blocks:
        raise ValueError("载荷内容中没有可用块")
    project_id = project_id or blocks[0].project_id or f"proj_{int(time.time())}"
    for block in blocks:
        if not block.project_id:
            block.project_id = project_id
    return project_id, blocks


def _load_blocks_from_pipeline_payload(content_id: str, project_id_hint: str = "") -> Tuple[str, List[Block], Dict[str, Any]]:
    record = db_client.get_pipeline_payload(content_id)
    if not record:
        raise FileNotFoundError(f"未找到content_id对应的数据库内容: {content_id}")

    payload = record.get("payload")
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict):
            for key in ("cleaned_blocks_file_path", "blocks_file_path"):
                file_path = str(data.get(key) or "").strip()
                if file_path and os.path.exists(file_path):
                    project_id, blocks = _load_blocks_from_file(file_path, project_id_hint=project_id_hint or str(record.get("project_id") or ""))
                    return project_id, blocks, record

    try:
        project_id, blocks = _load_blocks_from_payload(payload, project_id_hint=project_id_hint or str(record.get("project_id") or ""))
        return project_id, blocks, record
    except ValueError:
        file_path = str(record.get("file_path") or "").strip()
        if file_path and os.path.exists(file_path):
            project_id, blocks = _load_blocks_from_file(file_path, project_id_hint=project_id_hint or str(record.get("project_id") or ""))
            return project_id, blocks, record
        raise


def _load_blocks_from_dataset_payload(dataset_id: str, project_id_hint: str = "") -> Tuple[str, List[Block], Dict[str, Any]]:
    record = db_client.get_latest_pipeline_payload_by_dataset(dataset_id, payload_types=["blocks", "upload_split_blocks", "upload_split"])
    if not record:
        raise FileNotFoundError(f"未找到dataset_id对应的原始块内容: {dataset_id}")
    return _load_blocks_from_pipeline_payload(str(record.get("content_id") or ""), project_id_hint=project_id_hint)


@app.route("/api/data/clean", methods=["POST"])
def clean_data():
    """数据自动化清洗接口。优先支持路径和内容直传，也支持按外部content_id / dataset_id取数。"""
    data = _request_payload()
    if not data:
        return jsonify({
            "code": 400,
            "message": "请求体不能为空",
            "data": None
        }), 400

    blocks_file_path = str(data.get("blocks_file_path") or data.get("input_file_path") or "").strip()
    project_id_hint = str(data.get("project_id") or "").strip()
    return_mode = _normalize_return_mode(data.get("return_mode"))
    content_id = str(data.get("content_id") or data.get("blocks_content_id") or data.get("document_id") or "").strip()
    blocks_payload = data.get("blocks") if data.get("blocks") not in (None, "") else data.get("blocks_content")
    dataset_id = str(data.get("dataset_id") or "").strip()

    try:
        if blocks_file_path:
            project_id, blocks = _load_blocks_from_file(blocks_file_path, project_id_hint=project_id_hint)
        elif blocks_payload not in (None, ""):
            project_id, blocks = _load_blocks_from_payload(blocks_payload, project_id_hint=project_id_hint)
        elif content_id:
            project_id, blocks, _record = _load_blocks_from_pipeline_payload(content_id, project_id_hint=project_id_hint)
        else:
            block_ids = data.get("block_ids", [])
            if dataset_id and not block_ids:
                split_result = db_client.get_all_document_split_blocks(dataset_id)
                blocks = split_result.get("blocks") or []
                if blocks:
                    project_id = str(blocks[0].project_id or project_id_hint)
                else:
                    project_id, blocks, _record = _load_blocks_from_dataset_payload(dataset_id, project_id_hint=project_id_hint)
            else:
                if not dataset_id:
                    return jsonify({
                        "code": 400,
                        "message": "缺少blocks_file_path、blocks_content、content_id或dataset_id参数",
                        "data": None
                    }), 400
                if not block_ids:
                    return jsonify({
                        "code": 400,
                        "message": "兼容模式下block_ids不能为空",
                        "data": None
                    }), 400
                split_result = db_client.get_document_split_blocks(dataset_id, block_ids)
                blocks = split_result.get("blocks") or []
                missing_block_ids = split_result.get("missing_block_ids") or []
                if blocks and missing_block_ids:
                    return jsonify({
                        "code": 400,
                        "message": "部分block_ids未找到",
                        "data": {
                            "dataset_id": dataset_id,
                            "document_id": split_result.get("document_id"),
                            "requested_count": len(block_ids),
                            "found_count": len(blocks),
                            "missing_block_ids": missing_block_ids[:100],
                            "missing_count": len(missing_block_ids),
                        }
                    }), 400
                if not blocks:
                    blocks = db_client.get_blocks_by_ids(block_ids)
                if not blocks:
                    return jsonify({
                        "code": 404,
                        "message": "未找到指定的Block数据",
                        "data": {
                            "cleaning_rate": "0%",
                            "total_count": len(block_ids),
                            "total_input_count": len(block_ids),
                            "modified_count": 0,
                            "upstream_removed_count": 0,
                            "removed_count": 0,
                            "retention_rate": "100.0%" if block_ids else "0%",
                            "removal_rate": "0%",
                            "modified_block_ids": [],
                            "issues": [],
                            "cleaned_blocks_file_path": None,
                            "cleaned_blocks": [] if return_mode in {"content", "both"} else None,
                        }
                    }), 404
                project_id = project_id_hint or blocks[0].project_id
            blocks_file_path = ""

        all_issues = []
        local_modified_block_ids = []
        cleaned_blocks = []

        for block in blocks:
            cleaned_block, issues = clean_block(block)
            cleaned_blocks.append(cleaned_block)
            if issues:
                local_modified_block_ids.append(cleaned_block.block_id)
                all_issues.extend(issues)

        total_count = len(cleaned_blocks)
        upstream_issues, upstream_modified_block_ids, upstream_removed_count, upstream_input_count = _build_upstream_cleaning_issues(cleaned_blocks)
        all_issues.extend(upstream_issues)
        modified_block_ids = list(dict.fromkeys(local_modified_block_ids + upstream_modified_block_ids))
        modified_count = len(local_modified_block_ids) + upstream_removed_count
        removed_count = modified_count
        total_input_count = upstream_input_count or (total_count + upstream_removed_count)
        cleaning_rate = f"{(removed_count / total_input_count * 100):.1f}%" if total_input_count > 0 else "0%"
        retained_count = max(0, total_input_count - removed_count)
        retention_rate = f"{(retained_count / total_input_count * 100):.1f}%" if total_input_count > 0 else "0%"
        removal_rate = cleaning_rate
        cleaned_blocks_file_path: Optional[str] = None
        if return_mode in {"path", "both"}:
            cleaned_blocks_file_path = _save_cleaned_blocks_output(project_id, blocks_file_path or os.getcwd(), cleaned_blocks)

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "project_id": project_id or None,
                "dataset_id": dataset_id or None,
                "source_content_id": content_id or None,
                "payload_type": "cleaned_blocks",
                "cleaning_rate": cleaning_rate,
                "total_count": total_count,
                "total_input_count": total_input_count,
                "modified_count": modified_count,
                "upstream_removed_count": upstream_removed_count,
                "removed_count": removed_count,
                "retention_rate": retention_rate,
                "removal_rate": removal_rate,
                "modified_block_ids": modified_block_ids,
                "issues": [issue.to_dict() for issue in all_issues],
                "cleaned_blocks_file_path": cleaned_blocks_file_path,
                "cleaned_blocks": [_serialize_block(block) for block in cleaned_blocks] if return_mode in {"content", "both"} else None,
            }
        })

    except FileNotFoundError as e:
        return jsonify({
            "code": 400,
            "message": str(e),
            "data": None
        }), 400
    except ValueError as e:
        return jsonify({
            "code": 400,
            "message": str(e),
            "data": None
        }), 400
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": f"清洗过程发生错误: {str(e)}",
            "data": None
        }), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})

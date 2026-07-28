from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from protocol_extractor import enrich_protocol_metadata


PROJECT_ROOT = Path(__file__).resolve().parent
WORKER_PATH = PROJECT_ROOT / "docling_worker.py"
VENDORED_DOCLING_PATH = PROJECT_ROOT / "vendor" / "docling"
DOCLING_SUPPORTED_EXTS = {".pdf", ".docx", ".xlsx", ".md"}
DEFAULT_DOCLING_WORKER_TIMEOUT_SECONDS = 1800.0


def _emit_progress(progress_callback: Optional[Callable[[dict], None]], payload: dict) -> None:
    if progress_callback:
        progress_callback(payload)


def _process_xls_with_sheets(file_path: str) -> list[dict]:
    try:
        import xlrd
    except ImportError as exc:
        raise ValueError("未安装 xlrd，无法解析 .xls 文件") from exc

    def normalize_cell(value) -> str:
        if value is None:
            return ""
        return " ".join(str(value).split()).strip()

    def normalize_table_rows(table: list[list[str]]) -> list[list[str]]:
        rows = []
        max_cols = 0
        for row in table or []:
            cleaned = [normalize_cell(cell) for cell in (row or [])]
            if any(cleaned):
                rows.append(cleaned)
                max_cols = max(max_cols, len(cleaned))
        if max_cols == 0:
            return []
        normalized = []
        for row in rows:
            normalized.append(row + [""] * (max_cols - len(row)) if len(row) < max_cols else row[:max_cols])
        return normalized

    def format_table_to_text(table: list[list[str]]) -> str:
        return "\n".join(" | ".join(row) for row in table)

    workbook = xlrd.open_workbook(file_path)
    blocks = []
    for index in range(workbook.nsheets):
        sheet = workbook.sheet_by_index(index)
        rows = []
        for row_idx in range(sheet.nrows):
            cleaned = [normalize_cell(sheet.cell_value(row_idx, col_idx)) for col_idx in range(sheet.ncols)]
            if any(cleaned):
                rows.append(cleaned)

        normalized_rows = normalize_table_rows(rows)
        if not normalized_rows:
            continue

        col_count = max(len(row) for row in normalized_rows)
        if col_count <= 1:
            content = "\n".join(row[0] for row in normalized_rows if row and row[0]).strip()
            block_type = "text"
        else:
            content = format_table_to_text(normalized_rows)
            block_type = "table"

        blocks.append(
            {
                "page_num": index + 1,
                "content": content,
                "type": block_type,
                "metadata": {
                    "sheet_name": sheet.name,
                    "sheet_index": index + 1,
                    "row_count": len(normalized_rows),
                    "col_count": col_count,
                    "parser": "xlrd",
                },
            }
        )
    return blocks


def _build_worker_env() -> dict[str, str]:
    env = os.environ.copy()
    python_path_parts = [str(VENDORED_DOCLING_PATH)]
    if env.get("PYTHONPATH"):
        python_path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
    return env


def _docling_worker_timeout_seconds() -> float:
    raw_value = str(os.getenv("DOCLING_WORKER_TIMEOUT_SECONDS") or "").strip()
    if not raw_value:
        return DEFAULT_DOCLING_WORKER_TIMEOUT_SECONDS
    try:
        parsed = float(raw_value)
    except ValueError:
        return DEFAULT_DOCLING_WORKER_TIMEOUT_SECONDS
    return parsed if parsed > 0 else DEFAULT_DOCLING_WORKER_TIMEOUT_SECONDS


def _run_docling_worker(file_path: str, page_batch_size: int) -> dict:
    if not WORKER_PATH.exists():
        raise RuntimeError(f"Docling worker 不存在: {WORKER_PATH}")

    command = [sys.executable, str(WORKER_PATH), file_path, str(max(1, int(page_batch_size or 100)))]
    timeout_seconds = _docling_worker_timeout_seconds()

    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=_build_worker_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Docling worker 超时，已超过 {int(timeout_seconds)} 秒: {os.path.basename(file_path)}"
        ) from exc

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if not stdout:
        message = stderr or f"Docling worker 未返回结果，退出码 {result.returncode}"
        raise RuntimeError(message)

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Docling worker 输出无法解析为 JSON: {stdout[:500]}") from exc

    if not payload.get("ok"):
        error_message = str(payload.get("error") or stderr or "Docling worker 执行失败")
        raise RuntimeError(error_message)

    return payload["data"]


def _emit_worker_events(
    events: list[dict],
    *,
    file_ext: str,
    progress_callback: Optional[Callable[[dict], None]],
    progress_start: float,
    progress_end: float,
) -> None:
    if not events:
        return

    total_events = len(events)
    for index, event in enumerate(events, start=1):
        payload = dict(event)
        if "progress" not in payload:
            if file_ext == ".pdf" and payload.get("stage") == "processing_pdf_pages":
                processed_pages = int(payload.get("processed_pages") or 0)
                total_pages = int(payload.get("total_pages") or 0)
                ratio = processed_pages / max(total_pages, 1)
                payload["progress"] = progress_start + ratio * (progress_end - progress_start)
            else:
                payload["progress"] = progress_start + (index / max(total_events, 1)) * (progress_end - progress_start)
        _emit_progress(progress_callback, payload)


def process_document_with_pages(
    file_path: str,
    enable_llm_postprocess: bool = False,
    page_batch_size: int = 100,
    progress_callback: Optional[Callable[[dict], None]] = None,
    progress_start: float = 0.0,
    progress_end: float = 100.0,
) -> list[dict]:
    """Parse structured documents through an isolated Docling worker process."""
    file_ext = os.path.splitext(file_path)[1].lower()

    if file_ext == ".xls":
        _emit_progress(
            progress_callback,
            {
                "stage": "processing_xls_sheets",
                "message": "正在解析 XLS Sheet 内容",
                "progress": progress_start,
            },
        )
        blocks = _process_xls_with_sheets(file_path)
        _emit_progress(
            progress_callback,
            {
                "stage": "processing_document",
                "message": "已完成 .xls 文档结构化解析",
                "progress": progress_end,
                "processed_blocks": len(blocks),
                "total_blocks": len(blocks),
            },
        )
        return [enrich_protocol_metadata(block, enable_llm_postprocess) for block in blocks]

    if file_ext not in DOCLING_SUPPORTED_EXTS:
        raise ValueError(f"当前统一结构化解析链路不支持该文件类型: {file_ext}")

    worker_data = _run_docling_worker(file_path, page_batch_size)
    _emit_worker_events(
        list(worker_data.get("events") or []),
        file_ext=file_ext,
        progress_callback=progress_callback,
        progress_start=progress_start,
        progress_end=progress_end,
    )
    blocks = list(worker_data.get("blocks") or [])
    return [enrich_protocol_metadata(block, enable_llm_postprocess) for block in blocks]

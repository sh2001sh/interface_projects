from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from database.models import Block


TEXT_CHUNK_MAX_CHARS = 1200
TEXT_CHUNK_OVERLAP = 120


def normalize_text_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def split_text_chunks(
    text: str,
    max_chars: int = TEXT_CHUNK_MAX_CHARS,
    overlap: int = TEXT_CHUNK_OVERLAP,
) -> List[str]:
    normalized = str(text or "").replace("\r\n", "\n").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    if not paragraphs:
        paragraphs = [normalized]

    chunks: List[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if current and len(candidate) > max_chars:
            chunks.append(current)
            tail = current[-overlap:] if overlap > 0 else ""
            current = f"{tail}\n\n{paragraph}".strip() if tail else paragraph
            if len(current) <= max_chars:
                continue
        current = candidate
        while len(current) > max_chars:
            chunk = current[:max_chars].strip()
            chunks.append(chunk)
            current = current[max(max_chars - overlap, 1):].strip()
    if current:
        chunks.append(current)
    return [item for item in chunks if item.strip()]


def format_table_rows(rows: List[List[Any]]) -> str:
    lines: List[str] = []
    for row in rows:
        formatted = [str(cell or "").strip() for cell in row]
        if any(formatted):
            lines.append(" | ".join(formatted))
    return "\n".join(lines).strip()


def derive_file_names_from_document_paths(document_paths: Iterable[str]) -> List[str]:
    return [Path(path).name.strip() for path in document_paths if Path(path).name.strip()]


def normalize_document_paths(raw_paths: Any) -> List[str]:
    if isinstance(raw_paths, str):
        values = [raw_paths]
    elif isinstance(raw_paths, list):
        values = raw_paths
    else:
        values = []

    normalized: List[str] = []
    seen = set()
    for item in values:
        raw_path = str(item or "").strip()
        if not raw_path:
            continue
        resolved = str(Path(raw_path).expanduser())
        if resolved in seen:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return normalized


def build_direct_index_project_id(project_id: str, file_names: List[str], document_paths: List[str]) -> str:
    resolved_project_id = str(project_id or "").strip()
    if resolved_project_id:
        return resolved_project_id
    seed_items = file_names or [Path(path).name for path in document_paths]
    seed = "|".join(str(item or "").strip() for item in seed_items if str(item or "").strip()) or "rag_pageindex"
    return f"rag_{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:12]}"


def append_block(
    target: List[Block],
    *,
    project_id: str,
    file_name: str,
    page_num: int,
    content: str,
    block_type: str,
    source_document_path: str,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    normalized_content = str(content or "").strip()
    if not normalized_content:
        return
    metadata = {
        "source_document_path": source_document_path,
        "generated_from": "direct_document_index",
    }
    if isinstance(extra_metadata, dict):
        metadata.update(extra_metadata)
    page_range: List[int] = []
    merged_pages = metadata.get("merged_pages")
    if isinstance(merged_pages, list):
        for value in merged_pages:
            try:
                normalized_page = int(value)
            except (TypeError, ValueError):
                continue
            if normalized_page > 0 and normalized_page not in page_range:
                page_range.append(normalized_page)
    if not page_range:
        try:
            end_page = int(metadata.get("end_page") or 0)
        except (TypeError, ValueError):
            end_page = 0
        normalized_page_num = max(1, int(page_num or 1))
        if end_page > 0:
            start_page, stop_page = sorted((normalized_page_num, end_page))
            page_range = list(range(start_page, stop_page + 1))
        else:
            page_range = [normalized_page_num]
    target.append(
        Block(
            block_id=len(target) + 1,
            project_id=project_id,
            file_name=file_name,
            page_num=max(1, int(page_num or 1)),
            content=normalized_content,
            block_type=block_type,
            cleaned_content=normalized_content,
            page_range=page_range,
            metadata=metadata,
        )
    )


def validate_document_paths(document_paths: List[str]) -> List[str]:
    resolved_paths = normalize_document_paths(document_paths)
    if not resolved_paths:
        raise ValueError("document_paths不能为空")
    for raw_path in resolved_paths:
        if not os.path.exists(raw_path):
            raise FileNotFoundError(f"document_path不存在: {raw_path}")
        if os.path.getsize(raw_path) <= 0:
            raise ValueError(f"document_path文件为空: {raw_path}")
    return resolved_paths

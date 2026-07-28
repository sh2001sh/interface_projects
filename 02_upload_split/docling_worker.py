from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parent
INTERFACE_PROJECTS_ROOT = PROJECT_ROOT.parent
SEMANTIC_CHUNK_ROOT = INTERFACE_PROJECTS_ROOT / "04_semantic_chunk"
VENDORED_DOCLING_PATH = PROJECT_ROOT / "vendor" / "docling"


DOCLING_SUPPORTED_EXTS = {".pdf", ".docx", ".xlsx", ".md"}
HEADER_REPEAT_MIN_PAGES = 3
HEADER_ZONE_RATIO = 0.18
FOOTER_ZONE_RATIO = 0.82
TABLE_TOP_RATIO = 0.24
TABLE_BOTTOM_RATIO = 0.72
TABLE_TITLE_GAP_RATIO = 0.12
TABLE_TITLE_LOOKAHEAD_GAP_RATIO = 0.08
EXCEL_TABLE_MAX_ROWS = 50
EXCEL_TABLE_MAX_CHARS = 4000
WIDE_TEXT_LAYOUT_SPLIT_PATTERN = re.compile(r"^(?P<left>.+?\S)\s{6,}(?P<right>\S.+)$")
WIDE_TEXT_ANCHOR_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.()/-]{2,}$")
WIDE_TEXT_HEADER_PATTERN = re.compile(
    r"^(?:MIL-STD-[A-Z0-9.\-]+|J13\.2 MESSAGE SUMMARY(?: \(CONTINUED\))?|DATA ELEMENT SUMMARY(?: \(CONTINUED\))?)$",
    flags=re.IGNORECASE,
)
WIDE_TEXT_FOOTER_PATTERN = re.compile(r"^(?:\d+-\d+|PAGE\s*\d+(?:\s*OF\s*\d+)?)$", flags=re.IGNORECASE)
TABLE_SECTION_TITLE_PATTERN = re.compile(
    r"^(?P<section>[A-Z]\d+(?:\.\d+)?[A-Z]\d*(?:\s*\(CONTINUED\))?)\b",
    flags=re.IGNORECASE,
)
DEFAULT_HUGE_PDF_PAGE_THRESHOLD = 1000
DEFAULT_HUGE_PDF_DOCLING_ENHANCE_MAX_PAGES = 0
DEFAULT_HUGE_PDF_DOCLING_ENHANCE_BATCH_SIZE = 8


def _normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _huge_pdf_page_threshold() -> int:
    raw_value = str(os.getenv("DOCLING_HUGE_PDF_PAGE_THRESHOLD") or "").strip()
    if not raw_value:
        return DEFAULT_HUGE_PDF_PAGE_THRESHOLD
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_HUGE_PDF_PAGE_THRESHOLD
    return parsed if parsed > 0 else DEFAULT_HUGE_PDF_PAGE_THRESHOLD


def _huge_pdf_docling_enhance_max_pages() -> int:
    raw_value = str(os.getenv("DOCLING_HUGE_PDF_ENHANCE_MAX_PAGES") or "").strip()
    if not raw_value:
        return DEFAULT_HUGE_PDF_DOCLING_ENHANCE_MAX_PAGES
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_HUGE_PDF_DOCLING_ENHANCE_MAX_PAGES
    return max(0, parsed)


def _huge_pdf_docling_enhance_batch_size() -> int:
    raw_value = str(os.getenv("DOCLING_HUGE_PDF_ENHANCE_BATCH_SIZE") or "").strip()
    if not raw_value:
        return DEFAULT_HUGE_PDF_DOCLING_ENHANCE_BATCH_SIZE
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_HUGE_PDF_DOCLING_ENHANCE_BATCH_SIZE
    return parsed if parsed > 0 else DEFAULT_HUGE_PDF_DOCLING_ENHANCE_BATCH_SIZE


def _huge_pdf_docling_candidate_score(
    *,
    rows_by_table: list[list[list[str]]],
    text: str,
) -> int:
    valid_table_count = sum(1 for rows in rows_by_table if len(rows) >= 2)
    score = 0
    if valid_table_count:
        score += 10 + min(10, valid_table_count * 3)

    normalized_text = re.sub(r"\s+", " ", text or "").strip()
    if not normalized_text:
        return score
    if re.search(r"\b(?:TABLE|FIELD\s+CODING|WORD\s+NUMBER)\b", normalized_text, flags=re.IGNORECASE):
        score += 3
    if TABLE_SECTION_TITLE_PATTERN.search(normalized_text):
        score += 2
    return score


def _collapse_uniform_repeated_row(row: list[str]) -> list[str]:
    normalized = [_normalize_cell(cell) for cell in row if _normalize_cell(cell)]
    if not normalized:
        return []
    unique = []
    for cell in normalized:
        if cell not in unique:
            unique.append(cell)
    if len(unique) == 1:
        return [unique[0]]
    return [_normalize_cell(cell) for cell in row]


def _normalize_table_rows(table: list[list[Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    max_cols = 0
    for row in table or []:
        cleaned = [_normalize_cell(cell) for cell in (row or [])]
        if any(cleaned):
            rows.append(cleaned)
            max_cols = max(max_cols, len(cleaned))

    if max_cols == 0:
        return []

    collapsed_rows = [_collapse_uniform_repeated_row(row) for row in rows]
    effective_cols = max((len(row) for row in collapsed_rows), default=0)
    normalized = []
    for row in collapsed_rows:
        if len(row) < effective_cols:
            normalized.append(row + [""] * (effective_cols - len(row)))
        else:
            normalized.append(row[:effective_cols])
    return normalized


def _format_table_to_text(table: list[list[str]]) -> str:
    if not table:
        return ""
    return "\n".join(" | ".join(row) for row in table)


def _split_table_rows_for_blocks(
    rows: list[list[str]],
    *,
    max_rows: int = EXCEL_TABLE_MAX_ROWS,
    max_chars: int = EXCEL_TABLE_MAX_CHARS,
) -> list[tuple[list[list[str]], int, int]]:
    if not rows:
        return []
    header = rows[0]
    data_rows = rows[1:] if len(rows) > 1 else []
    chunks: list[tuple[list[list[str]], int, int]] = []
    current: list[list[str]] = []
    current_start = 2 if data_rows else 1

    def flush(end_row: int) -> None:
        nonlocal current, current_start
        if data_rows:
            chunk_rows = [header, *current]
        else:
            chunk_rows = current or [header]
        chunks.append((chunk_rows, current_start, end_row))
        current = []
        current_start = end_row + 1

    if not data_rows:
        chunks.append(([header], 1, 1))
        return chunks

    for offset, row in enumerate(data_rows, start=2):
        candidate = [header, *current, row]
        candidate_too_large = len(current) >= max(1, max_rows) or len(_format_table_to_text(candidate)) > max_chars
        if current and candidate_too_large:
            flush(offset - 1)
        current.append(row)
    if current:
        flush(len(rows))
    return chunks


def _collapse_adjacent_duplicate_cells(row: list[str]) -> list[str]:
    collapsed: list[str] = []
    for cell in row:
        normalized = _normalize_cell(cell)
        if collapsed and normalized and normalized == collapsed[-1]:
            continue
        collapsed.append(normalized)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return collapsed


def _row_text(row: list[str]) -> str:
    return _normalize_line_text(" ".join(cell for cell in row if _normalize_cell(cell)))


def _split_side_by_side_rows(rows: list[list[str]]) -> list[tuple[list[list[str]], str | None]]:
    normalized_rows = _normalize_table_rows(rows)
    if not normalized_rows:
        return []
    col_count = max(len(row) for row in normalized_rows)
    if col_count < 4 or col_count % 2 != 0:
        return [(normalized_rows, None)]

    split_index = col_count // 2
    dual_rows = 0
    for row in normalized_rows[: min(len(normalized_rows), 16)]:
        left = row[:split_index]
        right = row[split_index:]
        if any(_normalize_cell(cell) for cell in left) and any(_normalize_cell(cell) for cell in right):
            dual_rows += 1
    if dual_rows < 3:
        return [(normalized_rows, None)]

    def build_part(part_rows: list[list[str]], role: str) -> tuple[list[list[str]], str]:
        cleaned_rows: list[list[str]] = []
        for row in part_rows:
            collapsed = _collapse_adjacent_duplicate_cells(row)
            if any(collapsed):
                cleaned_rows.append(collapsed)
        return (_normalize_table_rows(cleaned_rows), role)

    left_rows = [row[:split_index] for row in normalized_rows]
    right_rows = [row[split_index:] for row in normalized_rows]
    return [build_part(left_rows, "left"), build_part(right_rows, "right")]


def _split_rows_on_section_titles(rows: list[list[str]]) -> list[list[list[str]]]:
    normalized_rows = _normalize_table_rows(rows)
    if not normalized_rows:
        return []
    split_points: list[int] = []
    for index in range(1, len(normalized_rows)):
        first_cell = _normalize_cell(normalized_rows[index][0]) if normalized_rows[index] else ""
        text = _row_text(normalized_rows[index])
        if not text:
            continue
        if TABLE_SECTION_TITLE_PATTERN.match(first_cell) or TABLE_SECTION_TITLE_PATTERN.match(text):
            split_points.append(index)
    if not split_points:
        return [normalized_rows]

    starts = [0] + split_points
    groups: list[list[list[str]]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(normalized_rows)
        chunk = normalized_rows[start:end]
        if chunk:
            groups.append(chunk)
    return groups or [normalized_rows]


def _infer_non_pdf_block_type(rows: list[list[str]], fallback_type: str) -> str:
    if fallback_type == "text":
        return "text"
    normalized_rows = _normalize_table_rows(rows)
    if not normalized_rows:
        return "text"
    col_count = max(len(row) for row in normalized_rows)
    if col_count <= 1:
        return "text"
    first_row_text = _row_text(normalized_rows[0])
    if TABLE_SECTION_TITLE_PATTERN.match(first_row_text):
        return "table"
    if len(normalized_rows) > 1:
        second_row_text = _row_text(normalized_rows[1])
        if "DATA ELEMENT" in second_row_text or "# BITS" in second_row_text:
            return "table"
    if len(normalized_rows) <= 3 and col_count <= 2:
        compact_lines = [_row_text(row) for row in normalized_rows if _row_text(row)]
        if compact_lines and all(len(line) <= 120 for line in compact_lines):
            return "text"
    return "table"


def _table_rows_to_blocks(
    rows: list[list[str]],
    *,
    page_num: int | None,
    block_type: str,
    metadata: dict[str, Any],
    order: int | None = None,
) -> list[dict]:
    normalized_rows = _normalize_table_rows(rows)
    if not normalized_rows:
        return []
    blocks = []
    outer_parts = _split_side_by_side_rows(normalized_rows)
    outer_total = len(outer_parts)
    emitted_order = 0
    for outer_index, (outer_rows, column_role) in enumerate(outer_parts, start=1):
        inner_parts = _split_rows_on_section_titles(outer_rows)
        for inner_index, part_rows in enumerate(inner_parts, start=1):
            normalized_part_rows = _normalize_table_rows(part_rows)
            if not normalized_part_rows:
                continue
            emitted_order += 1
            resolved_type = _infer_non_pdf_block_type(normalized_part_rows, block_type)
            col_count = max(len(row) for row in normalized_part_rows)
            part_metadata = {
                **metadata,
                "row_count": len(normalized_part_rows),
                "col_count": col_count,
            }
            if column_role is not None:
                part_metadata["column_role"] = column_role
                part_metadata["split_from_side_by_side_sheet"] = True
                part_metadata["sheet_column_index"] = outer_index
                part_metadata["sheet_column_total"] = outer_total
            if len(inner_parts) > 1:
                part_metadata["split_from_sectioned_sheet"] = True
                part_metadata["sheet_section_index"] = inner_index
                part_metadata["sheet_section_total"] = len(inner_parts)

            if resolved_type == "text":
                content_rows = [[cell for cell in row if cell] for row in normalized_part_rows]
                content = "\n".join(" ".join(row).strip() for row in content_rows if row).strip()
                if not content:
                    continue
                blocks.append(
                    {
                        "page_num": page_num,
                        "content": content,
                        "type": "text",
                        "metadata": part_metadata,
                        "order": (order or 0) + (emitted_order / 1000),
                    }
                )
                continue

            row_chunks = _split_table_rows_for_blocks(normalized_part_rows)
            total_chunks = len(row_chunks)
            for chunk_index, (chunk_rows, row_start, row_end) in enumerate(row_chunks, start=1):
                chunk_metadata = {
                    **part_metadata,
                    "row_count": len(chunk_rows),
                    "source_row_count": len(normalized_part_rows),
                    "col_count": col_count,
                    "table_chunk_index": chunk_index,
                    "total_table_chunks": total_chunks,
                    "source_row_start": row_start,
                    "source_row_end": row_end,
                }
                blocks.append(
                    {
                        "page_num": page_num,
                        "content": _format_table_to_text(chunk_rows),
                        "type": "table",
                        "metadata": chunk_metadata,
                        "order": (order or 0) + (emitted_order / 1000) + (chunk_index / 100000),
                        "_rows": chunk_rows,
                    }
                )
    return blocks


def _row_signature(row: list[str]) -> str:
    return "|".join(_normalize_cell(cell).lower() for cell in row if _normalize_cell(cell))


def _row_blank_ratio(row: list[str]) -> float:
    if not row:
        return 1.0
    blank = sum(1 for cell in row if not _normalize_cell(cell))
    return blank / max(1, len(row))


def _cell_shape(cell: str) -> str:
    text = _normalize_cell(cell)
    if not text:
        return "e"
    has_digit = bool(re.search(r"\d", text))
    has_alpha = bool(re.search(r"[A-Za-z\u4e00-\u9fff]", text))
    if has_alpha and has_digit:
        return "m"
    if has_digit:
        return "n"
    if has_alpha:
        return "a"
    return "o"


def _row_shape_similarity(left: list[str], right: list[str]) -> float:
    size = max(len(left), len(right), 1)
    score = 0
    for idx in range(size):
        left_shape = _cell_shape(left[idx] if idx < len(left) else "")
        right_shape = _cell_shape(right[idx] if idx < len(right) else "")
        if left_shape == right_shape:
            score += 1
    return score / size


def _looks_like_header_row(row: list[str]) -> bool:
    non_empty = [_normalize_cell(cell) for cell in row if _normalize_cell(cell)]
    if len(non_empty) < 2:
        return False

    joined = " ".join(non_empty).lower()
    hints = ("field", "字段", "name", "名称", "bit", "位", "desc", "描述", "length", "len", "type", "unit")
    if any(keyword in joined for keyword in hints):
        return True

    alpha_cells = sum(1 for cell in non_empty if re.search(r"[A-Za-z\u4e00-\u9fff]", cell))
    digit_cells = sum(1 for cell in non_empty if re.search(r"\d", cell))
    return alpha_cells >= max(1, len(non_empty) - 1) and digit_cells <= len(non_empty) // 2


def _normalize_line_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_table_title(text: str) -> str:
    normalized = _normalize_line_text(text)
    normalized = re.sub(r"\(sheet\s+\d+\s+of\s+\d+\)", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    return normalized


def _is_page_marker(text: str) -> bool:
    normalized = _normalize_line_text(text)
    if not normalized:
        return False
    patterns = (
        r"^[ivxlcdm]+$",
        r"^[A-Z]-\s*\d+$",
        r"^\d+\s*-\s*[A-Za-z0-9ivxlcdm]+$",
        r"^[A-Za-z0-9]+\s*-\s*[A-Za-z0-9]+$",
        r"^\d+$",
    )
    return any(re.fullmatch(pattern, normalized, re.IGNORECASE) for pattern in patterns)


def _extract_sheet_info(title: str) -> tuple[Optional[int], Optional[int], str]:
    match = re.search(r"\(sheet\s+(\d+)\s+of\s+(\d+)\)", title, re.IGNORECASE)
    if not match:
        return None, None, re.sub(r"\s+", " ", title).strip().lower()
    index = int(match.group(1))
    total = int(match.group(2))
    base = re.sub(r"\(sheet\s+\d+\s+of\s+\d+\)", "", title, flags=re.IGNORECASE)
    return index, total, re.sub(r"\s+", " ", base).strip().lower()


def _is_default_dataframe_header(column: Any, index: int) -> bool:
    if isinstance(column, int):
        return column == index
    text = _normalize_cell(column)
    return text == str(index) or text.lower().startswith("unnamed:")


def _get_docling_components() -> dict[str, Any]:
    try:
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        if str(VENDORED_DOCLING_PATH) not in sys.path and VENDORED_DOCLING_PATH.exists():
            sys.path.insert(0, str(VENDORED_DOCLING_PATH))
        try:
            from docling.datamodel.accelerator_options import AcceleratorOptions
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError:
            raise RuntimeError(
                "当前文档处理主链路依赖 Docling 版面模型，但运行环境未安装 `docling`；"
                "该接口不会回退到旧 PDF/Office 解析器。"
            ) from exc

    return {
        "AcceleratorOptions": AcceleratorOptions,
        "DocumentConverter": DocumentConverter,
        "InputFormat": InputFormat,
        "PdfFormatOption": PdfFormatOption,
        "PdfPipelineOptions": PdfPipelineOptions,
    }


def _get_semantic_pdf_components() -> dict[str, Any]:
    if str(SEMANTIC_CHUNK_ROOT) not in sys.path:
        sys.path.insert(0, str(SEMANTIC_CHUNK_ROOT))
    from document_parsing_pdf_layout import (
        filter_layout_lines,
        find_layout_anchor_index,
        infer_layout_split_index,
        normalize_cell as layout_normalize_cell,
        normalize_line_text as layout_normalize_line_text,
        pick_layout_anchor,
        split_layout_lines_by_index,
    )
    from document_parsing_pdf import (
        ParsedElement,
        _count_table_header_rows,
        _derive_header_row_count,
        _extract_embedded_title_row,
        _extract_table_identifier,
        _is_table_title_text_item,
        _item_bbox_ratios,
        _item_page_num,
        _item_text_value,
        _normalize_title_compare_key,
        _resolve_table_title,
        _table_item_to_rows,
        _title_candidate_score,
    )
    from document_parsing_pdf_postprocess import merge_cross_page_tables, reorder_reading_order, strip_page_furniture
    from document_parsing_pdf_table_cleanup import promote_text_tables, suppress_duplicate_table_text

    return {
        "ParsedElement": ParsedElement,
        "filter_layout_lines": filter_layout_lines,
        "find_layout_anchor_index": find_layout_anchor_index,
        "infer_layout_split_index": infer_layout_split_index,
        "_count_table_header_rows": _count_table_header_rows,
        "_derive_header_row_count": _derive_header_row_count,
        "_extract_embedded_title_row": _extract_embedded_title_row,
        "_extract_table_identifier": _extract_table_identifier,
        "_is_table_title_text_item": _is_table_title_text_item,
        "_item_bbox_ratios": _item_bbox_ratios,
        "_item_page_num": _item_page_num,
        "_item_text_value": _item_text_value,
        "_normalize_title_compare_key": _normalize_title_compare_key,
        "_resolve_table_title": _resolve_table_title,
        "_table_item_to_rows": _table_item_to_rows,
        "_title_candidate_score": _title_candidate_score,
        "layout_normalize_cell": layout_normalize_cell,
        "layout_normalize_line_text": layout_normalize_line_text,
        "merge_cross_page_tables": merge_cross_page_tables,
        "pick_layout_anchor": pick_layout_anchor,
        "promote_text_tables": promote_text_tables,
        "reorder_reading_order": reorder_reading_order,
        "split_layout_lines_by_index": split_layout_lines_by_index,
        "strip_page_furniture": strip_page_furniture,
        "suppress_duplicate_table_text": suppress_duplicate_table_text,
    }


def _build_docling_converter():
    components = _get_docling_components()
    AcceleratorOptions = components["AcceleratorOptions"]
    DocumentConverter = components["DocumentConverter"]
    InputFormat = components["InputFormat"]
    PdfFormatOption = components["PdfFormatOption"]
    PdfPipelineOptions = components["PdfPipelineOptions"]

    accelerator_device = os.getenv("DOCLING_DEVICE", "cpu").strip() or "cpu"
    artifacts_path = os.getenv("DOCLING_ARTIFACTS_PATH") or None
    if not artifacts_path and (SEMANTIC_CHUNK_ROOT / "runtime" / "docling_artifacts").exists():
        artifacts_path = str(SEMANTIC_CHUNK_ROOT / "runtime" / "docling_artifacts")
    pdf_options = PdfPipelineOptions(
        accelerator_options=AcceleratorOptions(device=accelerator_device),
        artifacts_path=artifacts_path,
        do_ocr=False,
        do_table_structure=True,
        enable_remote_services=False,
    )

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
        }
    )
    return converter, accelerator_device


def _raise_docling_error(file_path: str, exc: Exception) -> None:
    message = str(exc)
    if "LocalEntryNotFoundError" in type(exc).__name__ or "snapshot folder" in message or "Network is unreachable" in message:
        raise RuntimeError(
            "Docling PDF 版面模型资源不可用，当前环境既没有本地模型缓存，也无法在线拉取；"
            "请预先下载模型并通过 `DOCLING_ARTIFACTS_PATH` 指向缓存目录，或为服务提供可访问的模型网络。"
        ) from exc
    if "not valid" in message.lower():
        raise RuntimeError(f"Docling 无法解析 PDF 文件: {file_path}") from exc
    raise RuntimeError(f"Docling 文档解析失败: {message}") from exc


def _is_docling_invalid_pdf_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "not valid" in message or "无法解析 pdf 文件" in message


def _collect_page_sizes(doc) -> dict[int, tuple[float, float]]:
    page_sizes = {}
    for page_no, page in (getattr(doc, "pages", {}) or {}).items():
        size = getattr(page, "size", None)
        if size is None:
            continue
        page_sizes[int(page_no)] = (float(getattr(size, "width", 0.0)), float(getattr(size, "height", 0.0)))
    return page_sizes


def _extract_item_location(item) -> tuple[Optional[int], Optional[tuple[float, float, float, float]]]:
    prov = list(getattr(item, "prov", []) or [])
    if not prov:
        return None, None
    first = prov[0]
    bbox = getattr(first, "bbox", None)
    bbox_tuple = None
    if bbox is not None:
        bbox_tuple = (float(bbox.l), float(bbox.t), float(bbox.r), float(bbox.b))
    return int(getattr(first, "page_no", 0)) or None, bbox_tuple


def _rows_from_table_item(table_item, doc) -> list[list[str]]:
    dataframe = table_item.export_to_dataframe(doc=doc)
    raw_rows: list[list[Any]] = []

    columns = list(dataframe.columns)
    if columns and not all(_is_default_dataframe_header(column, idx) for idx, column in enumerate(columns)):
        raw_rows.append([_normalize_cell(column) for column in columns])

    for _, row in dataframe.iterrows():
        raw_rows.append([_normalize_cell(value) for value in row.tolist()])
    return _normalize_table_rows(raw_rows)


def _is_top_or_bottom_fragment(fragment: dict) -> Optional[str]:
    bbox = fragment.get("bbox")
    page_no = fragment.get("page_num")
    page_height = fragment.get("page_height")
    if bbox is None or page_no is None or not page_height:
        return None
    _, top, _, bottom = bbox
    if top <= page_height * HEADER_ZONE_RATIO:
        return "top"
    if bottom >= page_height * FOOTER_ZONE_RATIO:
        return "bottom"
    return None


def _filter_pdf_text_fragments(text_fragments: list[dict]) -> list[dict]:
    occurrences: dict[tuple[str, str], set[int]] = defaultdict(set)
    for fragment in text_fragments:
        label = fragment.get("label")
        normalized = _normalize_line_text(fragment.get("text", ""))
        zone = _is_top_or_bottom_fragment(fragment)
        if label in {"page_header", "page_footer"}:
            continue
        if not zone or not normalized or len(normalized) > 120:
            continue
        occurrences[(zone, normalized)].add(int(fragment["page_num"]))

    repeated = {
        key
        for key, pages in occurrences.items()
        if len(pages) >= HEADER_REPEAT_MIN_PAGES
    }

    filtered = []
    for fragment in text_fragments:
        label = fragment.get("label")
        text = fragment.get("text", "")
        normalized = _normalize_line_text(text)
        zone = _is_top_or_bottom_fragment(fragment)

        if label in {"page_header", "page_footer"}:
            continue
        if label == "caption" and normalized.lower().startswith("table "):
            filtered.append(fragment)
            continue
        if normalized.lower() == "mil-std-6016d":
            continue
        if normalized.lower().startswith("appendix ") and zone == "top":
            continue
        if normalized.lower().startswith("section ") and zone == "top":
            continue
        if zone and _is_page_marker(normalized):
            continue
        if zone and (zone, normalized) in repeated:
            continue
        filtered.append(fragment)
    return filtered


def _find_table_title(table_item: dict, page_texts: dict[int, list[dict]]) -> str:
    page_num = table_item.get("page_num")
    bbox = table_item.get("bbox")
    page_height = table_item.get("page_height") or 0.0
    if page_num is None or bbox is None:
        return ""

    _, table_top, _, _ = bbox
    candidates = []
    for fragment in page_texts.get(int(page_num), []):
        fragment_bbox = fragment.get("bbox")
        if fragment_bbox is None:
            continue
        _, fragment_top, _, fragment_bottom = fragment_bbox

        before_table = fragment["order"] < table_item["order"] and fragment_bottom <= table_top + 1
        near_before = before_table and (table_top - fragment_bottom <= page_height * TABLE_TITLE_GAP_RATIO)

        after_table = fragment["order"] > table_item["order"] and fragment_top >= table_top - 1
        near_after = after_table and (fragment_top - table_top <= page_height * TABLE_TITLE_LOOKAHEAD_GAP_RATIO)

        if near_before or near_after:
            candidates.append(fragment)

    if not candidates:
        return ""

    caption_candidates = [item for item in candidates if item.get("label") == "caption"]
    if caption_candidates:
        preferred = min(caption_candidates, key=lambda item: abs(item["order"] - table_item["order"]))
        return _normalize_table_title(preferred.get("text", ""))

    nearest = max(candidates, key=lambda item: item["bbox"][3])
    return _normalize_table_title(nearest.get("text", ""))


def _build_table_metadata(table_item: dict, page_texts: dict[int, list[dict]]) -> dict:
    rows = table_item["rows"]
    page_num = table_item.get("page_num")
    page_height = table_item.get("page_height") or 0.0
    bbox = table_item.get("bbox")
    title = _find_table_title(table_item, page_texts)
    sheet_index, sheet_total, title_base = _extract_sheet_info(title)

    near_top = False
    near_bottom = False
    if bbox is not None and page_height:
        _, top, _, bottom = bbox
        near_top = top <= page_height * TABLE_TOP_RATIO
        near_bottom = bottom >= page_height * TABLE_BOTTOM_RATIO

    metadata = {
        "row_count": len(rows),
        "col_count": max(len(row) for row in rows) if rows else 0,
        "source_pages": [page_num] if page_num is not None else [],
        "near_top": near_top,
        "near_bottom": near_bottom,
        "table_title": title,
        "table_title_base": _normalize_table_title(title_base or title),
        "sheet_index": sheet_index,
        "sheet_total": sheet_total,
        "parser": "docling",
    }
    if bbox is not None:
        metadata["table_bbox"] = [float(value) for value in bbox]
    return metadata


def _should_merge_cross_page_table(previous: dict, current: dict) -> tuple[bool, bool]:
    previous_meta = previous.get("metadata", {})
    current_meta = current.get("metadata", {})
    previous_pages = previous_meta.get("source_pages", [previous.get("page_num")])
    if not previous_pages:
        return False, False
    if current.get("page_num") != max(previous_pages) + 1:
        return False, False

    previous_rows = previous.get("_rows", [])
    current_rows = current.get("_rows", [])
    if not previous_rows or not current_rows:
        return False, False

    previous_cols = int(previous_meta.get("col_count", 0))
    current_cols = int(current_meta.get("col_count", 0))
    if previous_cols <= 0 or current_cols <= 0 or abs(previous_cols - current_cols) > 1:
        return False, False

    previous_title = _normalize_line_text(previous_meta.get("table_title_base", ""))
    current_title = _normalize_line_text(current_meta.get("table_title_base", ""))
    title_match = bool(previous_title) and previous_title == current_title

    previous_sheet = previous_meta.get("sheet_index")
    current_sheet = current_meta.get("sheet_index")
    sheet_continuation = bool(
        title_match
        and previous_sheet is not None
        and current_sheet is not None
        and current_sheet == previous_sheet + 1
    )

    if not (previous_meta.get("near_bottom") or current_meta.get("near_top") or title_match):
        return False, False

    repeated_header = _row_signature(current_rows[0]) == _row_signature(previous_rows[0]) and bool(_row_signature(current_rows[0]))
    if repeated_header:
        return True, True

    current_starts_with_header = _looks_like_header_row(current_rows[0])
    compare_row = current_rows[1] if current_starts_with_header and len(current_rows) > 1 else current_rows[0]
    similarity = _row_shape_similarity(previous_rows[-1], compare_row)
    sparse_tail = _row_blank_ratio(previous_rows[-1]) >= 0.4

    if sheet_continuation and current_starts_with_header:
        return True, True
    if title_match and current_starts_with_header and len(current_rows) > 1:
        return True, True
    if title_match and similarity >= 0.35:
        return True, current_starts_with_header
    if previous_cols == current_cols and (similarity >= 0.75 or (sparse_tail and similarity >= 0.6)):
        return True, False
    return False, False


def _merge_cross_page_tables(table_items: list[dict], cleanup: bool = True) -> tuple[list[dict], set[int]]:
    if not table_items:
        return [], set()

    merged: list[dict] = []
    consumed_orders: set[int] = set()
    for current in sorted(table_items, key=lambda item: item["order"]):
        if merged:
            can_merge, drop_header = _should_merge_cross_page_table(merged[-1], current)
            if can_merge:
                append_rows = current.get("_rows", [])
                if drop_header and len(append_rows) > 1:
                    append_rows = append_rows[1:]
                elif drop_header and len(append_rows) <= 1:
                    append_rows = []

                if append_rows:
                    merged[-1]["_rows"].extend(append_rows)

                source_pages = merged[-1]["metadata"].setdefault("source_pages", [merged[-1].get("page_num")])
                for page_num in current["metadata"].get("source_pages", [current.get("page_num")]):
                    if page_num not in source_pages:
                        source_pages.append(page_num)

                merged[-1]["content"] = _format_table_to_text(merged[-1]["_rows"])
                merged[-1]["metadata"]["row_count"] = len(merged[-1]["_rows"])
                merged[-1]["metadata"]["col_count"] = max(
                    int(merged[-1]["metadata"].get("col_count", 0)),
                    int(current["metadata"].get("col_count", 0)),
                )
                merged[-1]["metadata"]["cross_page_merged"] = len(source_pages) > 1
                merged[-1]["metadata"]["near_bottom"] = bool(current["metadata"].get("near_bottom"))
                consumed_orders.add(current["order"])
                continue
        merged.append(current)

    if cleanup:
        for item in merged:
            item["metadata"].pop("near_bottom", None)
            item["metadata"].pop("near_top", None)
            item.pop("_rows", None)
    return merged, consumed_orders


def _merge_pdf_blocks_across_batches(blocks: list[dict]) -> list[dict]:
    merged_blocks: list[dict] = []
    for block in blocks:
        if block.get("type") == "table" and merged_blocks and merged_blocks[-1].get("type") == "table":
            candidate = merged_blocks[-1]
            can_merge, drop_header = _should_merge_cross_page_table(candidate, block)
            if can_merge:
                append_rows = block.get("_rows", [])
                if drop_header and len(append_rows) > 1:
                    append_rows = append_rows[1:]
                elif drop_header and len(append_rows) <= 1:
                    append_rows = []

                if append_rows:
                    candidate.setdefault("_rows", []).extend(append_rows)
                source_pages = candidate["metadata"].setdefault("source_pages", [candidate.get("page_num")])
                for page_num in block.get("metadata", {}).get("source_pages", [block.get("page_num")]):
                    if page_num not in source_pages:
                        source_pages.append(page_num)
                candidate["content"] = _format_table_to_text(candidate.get("_rows", []))
                candidate["metadata"]["row_count"] = len(candidate.get("_rows", []))
                candidate["metadata"]["col_count"] = max(
                    int(candidate["metadata"].get("col_count", 0)),
                    int(block.get("metadata", {}).get("col_count", 0)),
                )
                candidate["metadata"]["cross_page_merged"] = len(source_pages) > 1
                candidate["metadata"]["near_bottom"] = bool(block.get("metadata", {}).get("near_bottom"))
                continue
        merged_blocks.append(block)

    for block in merged_blocks:
        if block.get("type") == "table":
            block.get("metadata", {}).pop("near_bottom", None)
            block.get("metadata", {}).pop("near_top", None)
            block.pop("_rows", None)
    return merged_blocks


def _semantic_pdf_elements_from_doc(doc) -> list[Any]:
    from docling_core.types.doc import ContentLayer, TableItem

    components = _get_semantic_pdf_components()
    ParsedElement = components["ParsedElement"]
    _count_table_header_rows = components["_count_table_header_rows"]
    _derive_header_row_count = components["_derive_header_row_count"]
    _extract_embedded_title_row = components["_extract_embedded_title_row"]
    _extract_table_identifier = components["_extract_table_identifier"]
    _is_table_title_text_item = components["_is_table_title_text_item"]
    _item_bbox_ratios = components["_item_bbox_ratios"]
    _item_page_num = components["_item_page_num"]
    _item_text_value = components["_item_text_value"]
    _normalize_title_compare_key = components["_normalize_title_compare_key"]
    _resolve_table_title = components["_resolve_table_title"]
    _table_item_to_rows = components["_table_item_to_rows"]
    _title_candidate_score = components["_title_candidate_score"]

    items = [
        item
        for item, _level in doc.iterate_items(
            with_groups=False,
            traverse_pictures=False,
            included_content_layers={ContentLayer.BODY},
        )
        if isinstance(item, TableItem) or hasattr(item, "text")
    ]
    item_layouts: list[dict[str, Optional[float]]] = []
    for item in items:
        top_ratio, bottom_ratio, left_ratio, right_ratio, center_ratio, width_ratio, height_ratio = _item_bbox_ratios(doc, item)
        item_layouts.append(
            {
                "page_num": float(_item_page_num(item)),
                "top_ratio": top_ratio,
                "bottom_ratio": bottom_ratio,
                "left_ratio": left_ratio,
                "right_ratio": right_ratio,
                "center_ratio": center_ratio,
                "width_ratio": width_ratio,
                "height_ratio": height_ratio,
            }
        )

    elements = []
    for index, item in enumerate(items):
        layout = item_layouts[index]
        page_num = max(1, int(layout["page_num"] or 1))
        if isinstance(item, TableItem):
            rows = _table_item_to_rows(item, doc)
            row_title, rows = _extract_embedded_title_row(rows)
            title, title_candidates, title_score = _resolve_table_title(items, item_layouts, index, item, doc, TableItem)
            if row_title:
                title_candidates = [row_title, *title_candidates]
                row_title_score = _title_candidate_score(
                    row_title,
                    source="row_header",
                    item_top_ratio=layout["top_ratio"],
                    item_center_ratio=layout["center_ratio"],
                    table_top_ratio=layout["top_ratio"],
                    table_center_ratio=layout["center_ratio"],
                )
                if title_score < row_title_score:
                    title = row_title
                    title_score = row_title_score
            model_header_rows = _count_table_header_rows(item)
            metadata = {
                "title": title,
                "table_title": title,
                "title_candidates": title_candidates,
                "title_score": title_score,
                "title_key": _normalize_title_compare_key(title),
                "table_id": _extract_table_identifier(title),
                "col_count": max((len(row) for row in rows), default=0),
                "row_count": len(rows),
                "header_row_count": _derive_header_row_count(rows, model_header_rows),
                "parser": "docling_pdf_layout",
            }
            elements.append(
                ParsedElement(
                    kind="table",
                    page_num=page_num,
                    text=title,
                    rows=rows,
                    top_ratio=layout["top_ratio"],
                    bottom_ratio=layout["bottom_ratio"],
                    left_ratio=layout["left_ratio"],
                    right_ratio=layout["right_ratio"],
                    center_ratio=layout["center_ratio"],
                    width_ratio=layout["width_ratio"],
                    height_ratio=layout["height_ratio"],
                    source_index=index,
                    label="table",
                    metadata=metadata,
                )
            )
            continue

        text = _item_text_value(item)
        if not text or _is_table_title_text_item(items, item_layouts, index, item, TableItem):
            continue
        label = str(getattr(getattr(item, "label", None), "value", getattr(item, "label", "text")))
        elements.append(
            ParsedElement(
                kind="text",
                page_num=page_num,
                text=text,
                top_ratio=layout["top_ratio"],
                bottom_ratio=layout["bottom_ratio"],
                left_ratio=layout["left_ratio"],
                right_ratio=layout["right_ratio"],
                center_ratio=layout["center_ratio"],
                width_ratio=layout["width_ratio"],
                height_ratio=layout["height_ratio"],
                source_index=index,
                label=label,
                metadata={"label": label, "parser": "docling_pdf_layout"},
            )
        )
    return elements


def _semantic_pdf_elements_to_blocks(elements: list[Any]) -> list[dict]:
    blocks: list[dict] = []
    for index, element in enumerate(elements, start=1):
        metadata = dict(element.metadata)
        metadata.setdefault("parser", "docling_pdf_layout")
        layout_metadata = {
            "column_role": element.column_role,
            "top_ratio": element.top_ratio,
            "bottom_ratio": element.bottom_ratio,
            "left_ratio": element.left_ratio,
            "right_ratio": element.right_ratio,
            "center_ratio": element.center_ratio,
            "width_ratio": element.width_ratio,
            "height_ratio": element.height_ratio,
        }
        layout_metadata = {key: value for key, value in layout_metadata.items() if value is not None}
        if layout_metadata:
            metadata.setdefault("layout", layout_metadata)
            if element.column_role is not None:
                metadata.setdefault("column_role", element.column_role)
        if element.kind == "table":
            rows = [list(row) for row in element.rows]
            content = _format_table_to_text(rows)
            if element.text:
                content = f"{element.text}\n{content}".strip()
            metadata["row_count"] = len(rows)
            metadata["col_count"] = max((len(row) for row in rows), default=0)
            metadata.setdefault("source_pages", metadata.get("merged_pages") or [element.page_num])
            blocks.append(
                {
                    "page_num": element.page_num,
                    "content": content,
                    "type": "table",
                    "metadata": metadata,
                    "order": int(element.source_index if element.source_index is not None else index),
                    "_rows": rows,
                }
            )
            continue
        blocks.append(
            {
                "page_num": element.page_num,
                "content": element.text,
                "type": "text",
                "metadata": metadata,
                "order": int(element.source_index if element.source_index is not None else index),
            }
        )
    return blocks


def _normalize_block_text(value: Any) -> str:
    return _normalize_line_text(str(value or ""))


def _looks_like_residual_table_fragment(content: str) -> bool:
    normalized = _normalize_block_text(content)
    if not normalized:
        return True
    if re.fullmatch(r"\d{4,6}\s+[A-Z]", normalized):
        return True
    if re.fullmatch(r"[A-Z]{2,12}\.", normalized):
        return True
    if "." in normalized and len(normalized) >= 4:
        return False
    tokens = normalized.split()
    compact = re.sub(r"[^A-Z0-9]+", "", normalized.upper())
    if len(tokens) == 2 and any(re.search(r"\d", token) for token in tokens) and any(re.search(r"[A-Z]", token) for token in tokens):
        return False
    if len(tokens) <= 4 and len(compact) <= 18:
        if all(re.fullmatch(r"[A-Z0-9.:/()\-]+", token.upper()) for token in tokens):
            return True
    if compact == "SPARE":
        return True
    if re.fullmatch(r"\d{4,6}\s+[A-Z]", normalized):
        return True
    if re.fullmatch(r"[A-Z]+\.?", normalized) and len(normalized) <= 8:
        return True
    return False


def _looks_like_table_annotation_fragment(content: str) -> bool:
    normalized = _normalize_block_text(content)
    if not normalized:
        return True
    if normalized.startswith("(") and normalized.endswith(")") and len(normalized) <= 12:
        if not re.search(r"[A-Za-z]", normalized):
            return True
    if re.fullmatch(r"\(?#[^\w]{0,2}[A-Z0-9 ]{1,12}\)?", normalized):
        return True
    if re.fullmatch(r"\(#\s*[A-Z0-9 ]{1,12}\)", normalized):
        return True
    if re.fullmatch(r"\([A-Z0-9]{1,4}\)", normalized):
        return True
    return False


def _drop_residual_table_fragments(blocks: list[dict]) -> list[dict]:
    if not blocks:
        return blocks

    def block_type(block: dict) -> str:
        return str(block.get("type") or block.get("block_type") or "")

    def table_covers_page(block: dict, page_num: int) -> bool:
        if block_type(block) != "table":
            return False
        metadata = block.get("metadata") or {}
        merged_pages = metadata.get("merged_pages") or [block.get("page_num")]
        for value in merged_pages:
            try:
                if int(value) == page_num:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def same_page_table_neighbor(index: int) -> bool:
        page_num = int(blocks[index].get("page_num") or 0)
        previous_block = blocks[index - 1] if index > 0 else None
        next_block = blocks[index + 1] if index + 1 < len(blocks) else None
        for candidate in (previous_block, next_block):
            if candidate is None:
                continue
            if table_covers_page(candidate, page_num):
                return True
        return False

    filtered: list[dict] = []
    for index, block in enumerate(blocks):
        if block_type(block) != "text":
            filtered.append(block)
            continue

        content = block.get("content") or ""
        normalized = _normalize_block_text(content)
        if not normalized:
            continue
        if len(normalized) <= 2 and not re.search(r"[A-Za-z0-9]", normalized):
            continue
        if re.fullmatch(r"[\W_]+", normalized):
            continue
        if _looks_like_table_annotation_fragment(normalized):
            continue
        if same_page_table_neighbor(index) and _looks_like_residual_table_fragment(normalized):
            continue
        filtered.append(block)
    return filtered


def _merge_short_text_fragments_into_cross_page_tables(blocks: list[dict]) -> list[dict]:
    if not blocks:
        return blocks

    def block_type(block: dict) -> str:
        return str(block.get("type") or block.get("block_type") or "")

    def table_covers_page(block: dict, page_num: int) -> bool:
        if block_type(block) != "table":
            return False
        metadata = block.get("metadata") or {}
        merged_pages = metadata.get("merged_pages") or [block.get("page_num")]
        for value in merged_pages:
            try:
                if int(value) == page_num:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def normalized_table_title(block: dict) -> str:
        metadata = block.get("metadata") or {}
        title = metadata.get("title") or ""
        if not title:
            title_candidates = metadata.get("title_candidates") or []
            if title_candidates:
                title = title_candidates[0]
        if not title:
            title = str(block.get("content") or "").splitlines()[0] if block.get("content") else ""
        return _normalize_block_text(title)

    def title_candidates(block: dict) -> set[str]:
        metadata = block.get("metadata") or {}
        values = {normalized_table_title(block)}
        for candidate in metadata.get("title_candidates") or []:
            normalized_candidate = _normalize_block_text(candidate)
            if normalized_candidate:
                values.add(normalized_candidate)
        return {value for value in values if value}

    def is_title_fragment(text_block: dict, table_block: dict) -> bool:
        if block_type(table_block) != "table":
            return False
        page_num = int(text_block.get("page_num") or 0)
        table_page = int(table_block.get("page_num") or 0)
        if page_num != table_page:
            return False
        normalized_text = _normalize_block_text(text_block.get("content") or "")
        if not normalized_text:
            return False
        if len(normalized_text) > 96:
            return False
        return normalized_text in title_candidates(table_block)

    def is_residual_fragment(text: str) -> bool:
        normalized_text = _normalize_block_text(text)
        if not normalized_text:
            return False
        if normalized_text == "COMPLIANCE (RRN R/C)":
            return True
        if len(normalized_text) <= 24 and _looks_like_table_annotation_fragment(normalized_text):
            return True
        if _looks_like_residual_table_fragment(normalized_text):
            return True
        if re.fullmatch(r"[:\-\s./#()]+", normalized_text):
            return True
        if re.fullmatch(r"\d{5}\s+[A-Z]", normalized_text):
            return True
        alnum_tokens = re.findall(r"[A-Za-z0-9]+", normalized_text)
        if not alnum_tokens:
            return False
        if len(alnum_tokens) <= 2 and normalized_text.count(":") >= 2:
            punctuation_count = sum(1 for char in normalized_text if not char.isalnum())
            if punctuation_count >= max(3, len(normalized_text) // 3):
                return True
        return False

    def append_fragment(table_block: dict, page_num: int, normalized_text: str, *, position: str) -> None:
        table_content = str(table_block.get("content") or "").strip()
        lines = [_normalize_block_text(line) for line in table_content.splitlines()]
        if position == "prefix":
            if not lines or lines[0] != normalized_text:
                table_block["content"] = f"{normalized_text}\n{table_content}".strip()
        else:
            if normalized_text not in lines[-3:]:
                table_block["content"] = f"{table_content}\n{normalized_text}".strip()
        metadata = table_block.setdefault("metadata", {})
        absorbed = metadata.setdefault("absorbed_text_fragments", [])
        absorbed.append({"page_num": page_num, "content": normalized_text, "position": position})

    merged: list[dict] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block_type(block) != "text":
            merged.append(block)
            index += 1
            continue

        normalized = _normalize_block_text(block.get("content") or "")
        if not normalized:
            index += 1
            continue

        page_num = int(block.get("page_num") or 0)
        next_block = blocks[index + 1] if index + 1 < len(blocks) else None
        if next_block and is_title_fragment(block, next_block):
            append_fragment(next_block, page_num, normalized, position="prefix")
            index += 1
            continue

        previous_table = merged[-1] if merged and block_type(merged[-1]) == "table" else None
        if previous_table and table_covers_page(previous_table, page_num) and is_residual_fragment(normalized):
            append_fragment(previous_table, page_num, normalized, position="suffix")
            index += 1
            continue

        merged.append(block)
        index += 1

    return merged


def _infer_layout_split_index(lines: list[str]) -> Optional[int]:
    if not lines:
        return None
    max_len = max(len(line) for line in lines)
    if max_len < 40:
        return None

    padded = [line.ljust(max_len) for line in lines]
    start = max(1, int(max_len * 0.35))
    end = min(max_len - 1, int(max_len * 0.65))
    best_index: Optional[int] = None
    best_score = -1.0
    for idx in range(start, end + 1):
        whitespace_score = 0.0
        edge_score = 0.0
        for line in padded:
            current = line[idx]
            left = line[idx - 1] if idx - 1 >= 0 else " "
            right = line[idx + 1] if idx + 1 < max_len else " "
            if current.isspace():
                whitespace_score += 1.0
                if not left.isspace():
                    edge_score += 0.5
                if not right.isspace():
                    edge_score += 0.5
        score = whitespace_score + edge_score
        if score > best_score:
            best_score = score
            best_index = idx
    return best_index


def _split_layout_lines_by_index(lines: list[str], split_index: int) -> tuple[list[str], list[str]]:
    left_lines: list[str] = []
    right_lines: list[str] = []
    for raw_line in lines:
        if not raw_line.strip():
            continue
        line = raw_line.rstrip("\n")
        left = _normalize_line_text(line[:split_index])
        right = _normalize_line_text(line[split_index:])
        if left:
            left_lines.append(left)
        if right:
            right_lines.append(right)
    return left_lines, right_lines


def _pick_layout_anchor(text: str) -> str:
    for raw_line in str(text or "").splitlines():
        normalized = _normalize_line_text(raw_line)
        if not normalized:
            continue
        for token in normalized.split():
            if WIDE_TEXT_ANCHOR_PATTERN.fullmatch(token):
                return token
    return ""


def _find_layout_anchor_index(lines: list[str], anchor: str) -> int:
    if not anchor:
        return 0
    anchor_normalized = _normalize_line_text(anchor)
    for index, line in enumerate(lines):
        if anchor_normalized and anchor_normalized in _normalize_line_text(line):
            return index
    return 0


def _split_docling_wide_text_elements_with_pdfplumber(file_path: str, elements: list[Any]) -> list[Any]:
    import pdfplumber

    components = _get_semantic_pdf_components()
    filter_layout_lines = components["filter_layout_lines"]
    find_layout_anchor_index = components["find_layout_anchor_index"]
    infer_layout_split_index = components["infer_layout_split_index"]
    layout_normalize_line_text = components["layout_normalize_line_text"]
    pick_layout_anchor = components["pick_layout_anchor"]
    split_layout_lines_by_index = components["split_layout_lines_by_index"]

    pages: dict[int, list[Any]] = defaultdict(list)
    for element in elements:
        pages[int(element.page_num)].append(element)

    page_layout_lines: dict[int, list[str]] = {}
    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            raw_text = page.extract_text(layout=True) or ""
            page_layout_lines[page_num] = [line.rstrip() for line in raw_text.splitlines() if line.strip()]

    adjusted: list[Any] = []
    ParsedElement = type(elements[0]) if elements else None
    for page_num in sorted(pages):
        layout_lines = page_layout_lines.get(page_num) or []
        filtered_layout_lines = filter_layout_lines(layout_lines)
        for element in pages[page_num]:
            if element.kind != "text":
                adjusted.append(element)
                continue
            label = str(element.label or element.metadata.get("label") or "")
            width = float(element.width_ratio or 0.0)
            center = float(element.center_ratio or 0.5)
            if label != "code" or width < 0.68 or not (0.42 <= center <= 0.58):
                adjusted.append(element)
                continue

            raw_text = str(element.text or "")
            anchor = pick_layout_anchor(raw_text)
            selected_lines = filtered_layout_lines[find_layout_anchor_index(filtered_layout_lines, anchor):] if filtered_layout_lines else []

            split_index = infer_layout_split_index(selected_lines)
            if split_index is None:
                left_lines = []
                right_lines = []
                for line in selected_lines:
                    match = WIDE_TEXT_LAYOUT_SPLIT_PATTERN.match(line)
                    if not match:
                        continue
                    left = layout_normalize_line_text(match.group("left"))
                    right = layout_normalize_line_text(match.group("right"))
                    if not left or not right:
                        continue
                    left_lines.append(left)
                    right_lines.append(right)
            else:
                left_lines, right_lines = split_layout_lines_by_index(selected_lines, split_index)

            if len(left_lines) < 4 or len(right_lines) < 4 or ParsedElement is None:
                adjusted.append(element)
                continue

            left_ratio = float(element.left_ratio if element.left_ratio is not None else 0.0)
            right_ratio = float(element.right_ratio if element.right_ratio is not None else 1.0)
            mid_ratio = (left_ratio + right_ratio) / 2.0
            metadata = dict(element.metadata)
            metadata["split_from_pdf_layout_text"] = True

            adjusted.append(
                ParsedElement(
                    kind="text",
                    page_num=element.page_num,
                    text="\n".join(left_lines),
                    metadata={**metadata, "split_part": "left"},
                    top_ratio=element.top_ratio,
                    bottom_ratio=element.bottom_ratio,
                    left_ratio=left_ratio,
                    right_ratio=mid_ratio,
                    center_ratio=(left_ratio + mid_ratio) / 2.0,
                    width_ratio=max(0.0, mid_ratio - left_ratio),
                    height_ratio=element.height_ratio,
                    source_index=element.source_index,
                    label=element.label,
                    column_role="left",
                )
            )
            adjusted.append(
                ParsedElement(
                    kind="text",
                    page_num=element.page_num,
                    text="\n".join(right_lines),
                    metadata={**metadata, "split_part": "right"},
                    top_ratio=element.top_ratio,
                    bottom_ratio=element.bottom_ratio,
                    left_ratio=mid_ratio,
                    right_ratio=right_ratio,
                    center_ratio=(mid_ratio + right_ratio) / 2.0,
                    width_ratio=max(0.0, right_ratio - mid_ratio),
                    height_ratio=element.height_ratio,
                    source_index=(element.source_index or 0) + 1,
                    label=element.label,
                    column_role="right",
                )
            )
    return adjusted


def _extract_optimized_pdf_blocks(doc, file_path: str) -> list[dict]:
    components = _get_semantic_pdf_components()
    elements = _semantic_pdf_elements_from_doc(doc)
    elements = _split_docling_wide_text_elements_with_pdfplumber(file_path, elements)
    ordered = components["reorder_reading_order"](elements)
    promoted = components["promote_text_tables"](ordered)
    deduped = components["suppress_duplicate_table_text"](promoted)
    cleaned = components["strip_page_furniture"](deduped)
    merged = components["merge_cross_page_tables"](cleaned)
    blocks = _semantic_pdf_elements_to_blocks(merged)
    blocks = _merge_short_text_fragments_into_cross_page_tables(blocks)
    blocks = _drop_residual_table_fragments(blocks)
    removed_count = max(0, len(deduped) - len(cleaned))
    if removed_count and blocks:
        stats = {
            "stage": "pdf_layout_postprocess",
            "pdf_removed_count": removed_count,
            "pdf_input_element_count": len(deduped),
            "pdf_kept_element_count": len(cleaned),
            "pdf_output_block_count": len(blocks),
        }
        for block in blocks:
            metadata = block.setdefault("metadata", {})
            metadata.setdefault("upstream_cleaning", stats)
    return blocks


def _extract_native_pdfplumber_elements(
    pdf,
    *,
    start_page: int = 1,
    end_page: int | None = None,
    source_index_offset: int = 0,
    collect_candidate_scores: bool = False,
) -> tuple[list[Any], int, dict[int, int]]:
    components = _get_semantic_pdf_components()
    ParsedElement = components["ParsedElement"]
    total_pages = len(pdf.pages)
    resolved_start = max(1, int(start_page or 1))
    resolved_end = min(total_pages, int(end_page or total_pages))
    if resolved_start > resolved_end:
        return [], source_index_offset, {}

    elements = []
    source_index = max(0, int(source_index_offset or 0))
    candidate_scores: dict[int, int] = {}
    for page_num in range(resolved_start, resolved_end + 1):
        page = pdf.pages[page_num - 1]
        page_width = float(page.width or 0.0)
        page_height = float(page.height or 0.0)
        raw_tables = page.extract_tables() or []
        normalized_rows_by_table: list[list[list[str]]] = []
        for raw_table in raw_tables:
            rows = _normalize_table_rows(raw_table)
            if len(rows) < 2:
                continue
            normalized_rows_by_table.append(rows)
            source_index += 1
            title = _native_table_title_from_page(page, rows)
            elements.append(
                ParsedElement(
                    kind="table",
                    page_num=page_num,
                    text=title,
                    rows=rows,
                    metadata={
                        "title": title,
                        "row_count": len(rows),
                        "col_count": max((len(row) for row in rows), default=0),
                        "header_row_count": 1,
                        "parser": "pdfplumber_native_fallback",
                    },
                    top_ratio=0.08,
                    bottom_ratio=0.08,
                    left_ratio=0.0,
                    right_ratio=1.0,
                    center_ratio=0.5,
                    width_ratio=1.0,
                    height_ratio=0.84,
                    source_index=source_index,
                    label="table",
                )
            )

        text = _normalize_line_text(page.extract_text() or "")
        if collect_candidate_scores:
            score = _huge_pdf_docling_candidate_score(rows_by_table=normalized_rows_by_table, text=text)
            if score > 0:
                candidate_scores[page_num] = score
        if not text:
            continue
        for table in raw_tables:
            for row in table or []:
                row_text = _normalize_line_text(" ".join(_normalize_cell(cell) for cell in (row or []) if _normalize_cell(cell)))
                if row_text and row_text in text:
                    text = text.replace(row_text, " ")
        text = _normalize_line_text(text)
        if not text:
            continue
        source_index += 1
        elements.append(
            ParsedElement(
                kind="text",
                page_num=page_num,
                text=text,
                top_ratio=0.0 if page_height or page_width else None,
                bottom_ratio=0.0 if page_height or page_width else None,
                left_ratio=0.0 if page_height or page_width else None,
                right_ratio=1.0 if page_height or page_width else None,
                center_ratio=0.5 if page_height or page_width else None,
                width_ratio=1.0 if page_height or page_width else None,
                height_ratio=1.0 if page_height or page_width else None,
                source_index=source_index,
                label="text",
                metadata={"parser": "pdfplumber_native_fallback"},
            )
        )

    return elements, source_index, candidate_scores


def _finalize_native_pdfplumber_blocks(elements: list[Any], *, stage: str) -> list[dict]:
    components = _get_semantic_pdf_components()
    if not elements:
        return []

    ordered = components["reorder_reading_order"](elements)
    promoted = components["promote_text_tables"](ordered)
    deduped = components["suppress_duplicate_table_text"](promoted)
    cleaned = components["strip_page_furniture"](deduped)
    merged = components["merge_cross_page_tables"](cleaned)
    blocks = _semantic_pdf_elements_to_blocks(merged)
    blocks = _merge_short_text_fragments_into_cross_page_tables(blocks)
    blocks = _drop_residual_table_fragments(blocks)
    removed_count = max(0, len(deduped) - len(cleaned))
    if removed_count and blocks:
        stats = {
            "stage": stage,
            "pdf_removed_count": removed_count,
            "pdf_input_element_count": len(deduped),
            "pdf_kept_element_count": len(cleaned),
            "pdf_output_block_count": len(blocks),
        }
        for block in blocks:
            metadata = block.setdefault("metadata", {})
            metadata.setdefault("upstream_cleaning", stats)
    return blocks


def _native_pdf_blocks_with_pdfplumber(file_path: str) -> list[dict]:
    import pdfplumber

    with pdfplumber.open(file_path) as pdf:
        elements, _, _ = _extract_native_pdfplumber_elements(pdf)
    return _finalize_native_pdfplumber_blocks(elements, stage="pdfplumber_native_fallback")


def _select_top_scored_pages(candidate_scores: dict[int, int], max_pages: int) -> list[int]:
    if max_pages <= 0 or not candidate_scores:
        return []
    ranked = sorted(candidate_scores.items(), key=lambda item: (-item[1], item[0]))
    return sorted(page for page, _score in ranked[:max_pages])


def _group_contiguous_pages(pages: list[int], max_group_size: int) -> list[tuple[int, int]]:
    if not pages:
        return []
    resolved_max_group_size = max(1, int(max_group_size or 1))
    ordered_pages = sorted(set(int(page) for page in pages if int(page) > 0))
    groups: list[tuple[int, int]] = []
    group_start = ordered_pages[0]
    group_end = ordered_pages[0]
    group_size = 1
    for page in ordered_pages[1:]:
        if page == group_end + 1 and group_size < resolved_max_group_size:
            group_end = page
            group_size += 1
            continue
        groups.append((group_start, group_end))
        group_start = page
        group_end = page
        group_size = 1
    groups.append((group_start, group_end))
    return groups


def _mark_hybrid_docling_blocks(blocks: list[dict], *, mode: str) -> list[dict]:
    marked: list[dict] = []
    for block in blocks:
        copied = dict(block)
        metadata = dict(copied.get("metadata") or {})
        metadata["hybrid_pdf_parser"] = mode
        copied["metadata"] = metadata
        marked.append(copied)
    return marked


def _build_page_coverage(blocks: list[dict]) -> dict[int, list[dict]]:
    coverage: dict[int, list[dict]] = defaultdict(list)
    for block in blocks:
        metadata = block.get("metadata") or {}
        source_pages = metadata.get("merged_pages") or metadata.get("source_pages") or [block.get("page_num")]
        for page in source_pages:
            try:
                page_num = int(page)
            except (TypeError, ValueError):
                continue
            if page_num > 0:
                coverage[page_num].append(block)
    return coverage


def _page_has_cross_page_table_coverage(coverage: dict[int, list[dict]], page_num: int) -> bool:
    for block in coverage.get(page_num, []):
        if block.get("type") != "table":
            continue
        metadata = block.get("metadata") or {}
        source_pages = metadata.get("merged_pages") or metadata.get("source_pages") or [block.get("page_num")]
        unique_pages: set[int] = set()
        for page in source_pages:
            try:
                unique_pages.add(int(page))
            except (TypeError, ValueError):
                continue
        if len(unique_pages) > 1:
            return True
    return False


def _replace_page_text_blocks(base_blocks: list[dict], doc_text_blocks: list[dict]) -> list[dict]:
    if not doc_text_blocks:
        return list(base_blocks)
    result: list[dict] = []
    inserted = False
    for block in base_blocks:
        if block.get("type") == "text":
            if not inserted:
                result.extend(doc_text_blocks)
                inserted = True
            continue
        result.append(block)
    if not inserted:
        result.extend(doc_text_blocks)
    return result


def _hybrid_enhance_huge_pdf_blocks_with_docling(
    base_blocks: list[dict],
    *,
    file_path: str,
    converter,
    candidate_pages: list[int],
) -> tuple[list[dict], list[dict]]:
    enhance_max_pages = _huge_pdf_docling_enhance_max_pages()
    if enhance_max_pages <= 0:
        return base_blocks, []

    selected_pages = sorted(set(int(page) for page in candidate_pages[:enhance_max_pages] if int(page) > 0))
    if not selected_pages:
        return base_blocks, []

    page_groups = _group_contiguous_pages(selected_pages, _huge_pdf_docling_enhance_batch_size())
    coverage = _build_page_coverage(base_blocks)
    base_blocks_by_page: dict[int, list[dict]] = defaultdict(list)
    ordered_pages: list[int] = []
    seen_pages: set[int] = set()
    for block in base_blocks:
        page_num = int(block.get("page_num") or 0)
        if page_num <= 0:
            continue
        if page_num not in seen_pages:
            ordered_pages.append(page_num)
            seen_pages.add(page_num)
        base_blocks_by_page[page_num].append(block)

    enhanced_page_blocks: dict[int, list[dict]] = {}
    events = [
        {
            "stage": "processing_pdf_pages",
            "message": f"超大 PDF 已选中 {len(selected_pages)} 个关键页，开始局部 Docling 增强",
            "processed_pages": 0,
            "total_pages": len(selected_pages),
        }
    ]
    processed_pages = 0
    for range_start, range_end in page_groups:
        doc = _convert_docling_document(
            converter,
            file_path,
            page_range=(range_start, range_end),
        )
        doc_blocks = _mark_hybrid_docling_blocks(
            _extract_optimized_pdf_blocks(doc, file_path),
            mode="docling_selective_enhancement",
        )
        doc_blocks_by_page: dict[int, list[dict]] = defaultdict(list)
        for block in doc_blocks:
            page_num = int(block.get("page_num") or 0)
            if page_num > 0:
                doc_blocks_by_page[page_num].append(block)

        for page_num in range(range_start, range_end + 1):
            page_doc_blocks = doc_blocks_by_page.get(page_num) or []
            if not page_doc_blocks:
                continue
            if _page_has_cross_page_table_coverage(coverage, page_num):
                doc_text_blocks = [block for block in page_doc_blocks if block.get("type") == "text"]
                enhanced_page_blocks[page_num] = _replace_page_text_blocks(
                    base_blocks_by_page.get(page_num, []),
                    doc_text_blocks,
                )
            else:
                enhanced_page_blocks[page_num] = page_doc_blocks

        processed_pages += range_end - range_start + 1
        events.append(
            {
                "stage": "processing_pdf_pages",
                "message": f"已完成关键页 Docling 增强 {range_start}-{range_end}",
                "processed_pages": min(processed_pages, len(selected_pages)),
                "total_pages": len(selected_pages),
            }
        )

    merged_blocks: list[dict] = []
    for page_num in ordered_pages:
        merged_blocks.extend(enhanced_page_blocks.get(page_num) or base_blocks_by_page.get(page_num, []))
    return merged_blocks, events


def _native_pdf_blocks_with_pdfplumber_batches(
    file_path: str,
    *,
    total_pages: int,
    page_batch_size: int,
    converter,
) -> tuple[list[dict], list[dict]]:
    import pdfplumber

    resolved_batch_size = max(1, int(page_batch_size or 100))
    progress_events = [
        {
            "stage": "processing_pdf_pages",
            "message": f"PDF 共 {total_pages} 页，已切换为 pdfplumber 超大文档解析模式",
            "processed_pages": 0,
            "total_pages": total_pages,
        }
    ]
    all_elements: list[Any] = []
    source_index = 0
    candidate_scores: dict[int, int] = {}

    with pdfplumber.open(file_path) as pdf:
        for batch_start in range(0, total_pages, resolved_batch_size):
            batch_end = min(total_pages, batch_start + resolved_batch_size)
            batch_elements, source_index, batch_candidate_scores = _extract_native_pdfplumber_elements(
                pdf,
                start_page=batch_start + 1,
                end_page=batch_end,
                source_index_offset=source_index,
                collect_candidate_scores=True,
            )
            all_elements.extend(batch_elements)
            candidate_scores.update(batch_candidate_scores)
            progress_events.append(
                {
                    "stage": "processing_pdf_pages",
                    "message": f"已完成 pdfplumber 超大 PDF 解析 {batch_start + 1}-{batch_end} / {total_pages} 页",
                    "processed_pages": batch_end,
                    "total_pages": total_pages,
                }
            )

    blocks = _finalize_native_pdfplumber_blocks(all_elements, stage="pdfplumber_huge_pdf")
    selected_pages = _select_top_scored_pages(candidate_scores, _huge_pdf_docling_enhance_max_pages())
    if selected_pages:
        blocks, enhancement_events = _hybrid_enhance_huge_pdf_blocks_with_docling(
            blocks,
            file_path=file_path,
            converter=converter,
            candidate_pages=selected_pages,
        )
        progress_events.extend(enhancement_events)
    return blocks, progress_events


def _native_table_title_from_page(page: object, rows: list[list[str]]) -> str:
    text = str(getattr(page, "extract_text", lambda: "")() or "")
    lines = [_normalize_line_text(line) for line in text.splitlines() if _normalize_line_text(line)]
    for line in lines[:8]:
        if re.search(r"\b(?:TABLE|FIELD\s+CODING|WORD\s+NUMBER)\b", line, flags=re.IGNORECASE):
            return line
    if rows and any(rows[0]):
        return _normalize_line_text(" ".join(cell for cell in rows[0] if cell))[:180]
    return ""


def _collapse_text_fragments(items: list[dict]) -> list[dict]:
    blocks = []
    current: list[dict] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        page_num = current[0].get("page_num")
        text = "\n".join(fragment["text"] for fragment in current if fragment.get("text")).strip()
        if text:
            blocks.append(
                {
                    "page_num": page_num,
                    "content": text,
                    "type": "text",
                    "metadata": {
                        "parser": "docling",
                        "docling_labels": sorted({fragment.get("label") for fragment in current if fragment.get("label")}),
                    },
                    "order": current[0]["order"],
                }
            )
        current = []

    for item in items:
        if item["kind"] == "table":
            flush()
            continue
        if current and current[0].get("page_num") != item.get("page_num"):
            flush()
        current.append(item)
    flush()
    return blocks


def _finalize_non_pdf_page_numbers(blocks: list[dict]) -> list[dict]:
    if any(block.get("page_num") for block in blocks):
        next_page = 1
        for block in blocks:
            if block.get("page_num"):
                next_page = max(next_page, int(block["page_num"]) + 1)
                continue
            block["page_num"] = next_page
            next_page += 1
        return blocks

    for index, block in enumerate(blocks, start=1):
        block["page_num"] = index
    return blocks


def _extract_docling_blocks(doc, file_ext: str) -> list[dict]:
    page_sizes = _collect_page_sizes(doc)
    items: list[dict] = []
    for order, (raw_item, _) in enumerate(doc.iterate_items(), start=1):
        label = getattr(raw_item, "label", None)
        label_value = getattr(label, "value", str(label or "")).lower()
        page_num, bbox = _extract_item_location(raw_item)
        page_height = page_sizes.get(int(page_num), (0.0, 0.0))[1] if page_num is not None else 0.0

        if label_value in {"table", "document_index"}:
            rows = _rows_from_table_item(raw_item, doc)
            if not rows:
                continue
            items.append(
                {
                    "kind": "table",
                    "page_num": page_num,
                    "order": order,
                    "bbox": bbox,
                    "page_height": page_height,
                    "rows": rows,
                    "content": _format_table_to_text(rows),
                    "label": label_value,
                }
            )
            continue

        text = _normalize_line_text(str(getattr(raw_item, "text", "")))
        if not text:
            continue
        items.append(
            {
                "kind": "text",
                "page_num": page_num,
                "order": order,
                "bbox": bbox,
                "page_height": page_height,
                "text": text,
                "label": label_value,
            }
        )

    text_fragments = [item for item in items if item["kind"] == "text"]
    if file_ext == ".pdf":
        text_fragments = _filter_pdf_text_fragments(text_fragments)
    text_orders = {item["order"] for item in text_fragments}

    page_texts: dict[int, list[dict]] = defaultdict(list)
    for fragment in text_fragments:
        if fragment.get("page_num") is not None:
            page_texts[int(fragment["page_num"])].append(fragment)

    table_items = []
    for item in items:
        if item["kind"] != "table":
            continue
        if file_ext == ".pdf":
            block = {
                "page_num": item.get("page_num"),
                "content": item["content"],
                "type": "table",
                "metadata": _build_table_metadata(item, page_texts),
                "order": item["order"],
                "_rows": item["rows"],
            }
            table_items.append(block)
            continue
        table_items.extend(
            _table_rows_to_blocks(
                item["rows"],
                page_num=item.get("page_num"),
                block_type="table",
                metadata={"parser": "docling", "source_format": file_ext.lstrip(".") or "document"},
                order=item["order"],
            )
        )

    merged_tables = table_items
    consumed_orders: set[int] = set()
    if file_ext == ".pdf":
        merged_tables, consumed_orders = _merge_cross_page_tables(table_items, cleanup=False)
    merged_table_by_order = {item["order"]: item for item in merged_tables}

    filtered_items = []
    for item in sorted(items, key=lambda current: current["order"]):
        if item["kind"] == "text":
            if item["order"] in text_orders:
                filtered_items.append(item)
            continue
        if item["order"] in consumed_orders:
            continue
        if item["order"] in merged_table_by_order:
            filtered_items.append({"kind": "table", "order": item["order"]})

    blocks = []
    blocks.extend(_collapse_text_fragments(filtered_items))
    blocks.extend(merged_tables)
    blocks.sort(key=lambda item: item["order"])

    for block in blocks:
        block.pop("order", None)

    if file_ext != ".pdf":
        blocks = _finalize_non_pdf_page_numbers(blocks)

    return blocks


def _convert_docling_document(
    converter,
    file_path: str,
    *,
    page_range: Optional[tuple[int, int]] = None,
):
    kwargs = {}
    if page_range is not None:
        kwargs["page_range"] = page_range
    result = converter.convert(file_path, **kwargs)
    errors = list(getattr(result, "errors", []) or [])
    status = getattr(result, "status", None)
    if errors:
        first_error = errors[0]
        if isinstance(first_error, Exception):
            _raise_docling_error(file_path, first_error)
        raise RuntimeError(f"Docling 文档解析失败: {first_error}")
    if status is not None and str(status).upper().endswith("FAILURE"):
        raise RuntimeError(f"Docling 文档解析失败: {status}")
    return result.document


def _process_pdf_with_docling(
    file_path: str,
    *,
    converter,
    page_batch_size: int,
) -> tuple[list[dict], list[dict]]:
    import pdfplumber

    all_blocks = []
    progress_events = []
    with pdfplumber.open(file_path) as pdf:
        total_pages = len(pdf.pages)

    resolved_batch_size = max(1, int(page_batch_size or 100))
    if total_pages >= _huge_pdf_page_threshold():
        return _native_pdf_blocks_with_pdfplumber_batches(
            file_path,
            total_pages=total_pages,
            page_batch_size=resolved_batch_size,
            converter=converter,
        )

    for batch_start in range(0, total_pages, resolved_batch_size):
        batch_end = min(total_pages, batch_start + resolved_batch_size)
        try:
            doc = _convert_docling_document(
                converter,
                file_path,
                page_range=(batch_start + 1, batch_end),
            )
        except Exception as exc:
            if _is_docling_invalid_pdf_error(exc):
                fallback_blocks = _native_pdf_blocks_with_pdfplumber(file_path)
                if fallback_blocks:
                    return fallback_blocks, [
                        {
                            "stage": "processing_pdf_pages",
                            "message": "Docling 无法解析该 PDF，已使用 pdfplumber 原生文本层 fallback 完成解析",
                            "processed_pages": total_pages,
                            "total_pages": total_pages,
                        }
                    ]
            if isinstance(exc, RuntimeError):
                raise
            _raise_docling_error(file_path, exc)
        all_blocks.extend(_extract_optimized_pdf_blocks(doc, file_path))
        progress_events.append(
            {
                "stage": "processing_pdf_pages",
                "message": f"已完成 Docling PDF 版面解析 {batch_start + 1}-{batch_end} / {total_pages} 页",
                "processed_pages": batch_end,
                "total_pages": total_pages,
            }
        )

    all_blocks = _merge_pdf_blocks_across_batches(all_blocks)
    return all_blocks, progress_events


def _process_xls_with_sheets(file_path: str) -> list[dict]:
    try:
        import xlrd
    except ImportError as exc:
        raise ValueError("未安装 xlrd，无法解析 .xls 文件") from exc

    workbook = xlrd.open_workbook(file_path)
    blocks = []
    for index in range(workbook.nsheets):
        sheet = workbook.sheet_by_index(index)
        rows = []
        for row_idx in range(sheet.nrows):
            cleaned = [_normalize_cell(sheet.cell_value(row_idx, col_idx)) for col_idx in range(sheet.ncols)]
            if any(cleaned):
                rows.append(cleaned)

        normalized_rows = _normalize_table_rows(rows)
        if not normalized_rows:
            continue

        col_count = max(len(row) for row in normalized_rows)
        block_type = "text" if col_count <= 1 else "table"
        blocks.extend(
            _table_rows_to_blocks(
                normalized_rows,
                page_num=index + 1,
                block_type=block_type,
                metadata={
                    "sheet_name": sheet.name,
                    "sheet_index": index + 1,
                    "parser": "xlrd",
                },
                order=index + 1,
            )
        )
    return blocks


def process_document_with_pages(
    file_path: str,
    page_batch_size: int = 100,
) -> dict[str, Any]:
    """Parse structured documents with a single Docling-based main chain."""
    file_ext = os.path.splitext(file_path)[1].lower()

    if file_ext == ".xls":
        blocks = _process_xls_with_sheets(file_path)
        return {
            "blocks": blocks,
            "events": [
                {
                    "stage": "processing_xls_sheets",
                    "message": "正在解析 XLS Sheet 内容",
                },
                {
                    "stage": "processing_document",
                    "message": "已完成 .xls 文档结构化解析",
                    "processed_blocks": len(blocks),
                    "total_blocks": len(blocks),
                },
            ],
        }

    if file_ext not in DOCLING_SUPPORTED_EXTS:
        raise ValueError(f"当前统一结构化解析链路不支持该文件类型: {file_ext}")

    converter, accelerator_device = _build_docling_converter()
    if file_ext == ".pdf":
        blocks, events = _process_pdf_with_docling(
            file_path,
            converter=converter,
            page_batch_size=page_batch_size,
        )
        return {
            "blocks": blocks,
            "events": [
                {
                    "stage": "initializing_docling",
                    "message": f"已启用 PDF 结构化解析链路，默认使用 Docling，超大 PDF 自动切换为 pdfplumber，device={accelerator_device}",
                },
                *events,
            ],
        }

    try:
        doc = _convert_docling_document(converter, file_path)
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Docling 文档解析失败: {exc}") from exc

    blocks = _extract_docling_blocks(doc, file_ext)
    return {
        "blocks": blocks,
        "events": [
            {
                "stage": "processing_document",
                "message": f"已启用 Docling 结构化解析，正在处理 {file_ext} 文档",
            },
            {
                "stage": "processing_document",
                "message": f"已完成 {file_ext} 文档结构化解析",
                "processed_blocks": len(blocks),
                "total_blocks": len(blocks),
            },
        ],
    }


def _main() -> int:
    if len(sys.argv) != 3:
        print(json.dumps({"ok": False, "error": "usage: docling_worker.py <file_path> <page_batch_size>"}, ensure_ascii=False))
        return 2

    file_path = sys.argv[1]
    page_batch_size = int(sys.argv[2])
    try:
        payload = process_document_with_pages(file_path, page_batch_size=page_batch_size)
        print(json.dumps({"ok": True, "data": payload}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())

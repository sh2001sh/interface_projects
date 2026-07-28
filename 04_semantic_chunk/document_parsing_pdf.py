from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from database.models import Block
from document_parsing_common import append_block, format_table_rows, normalize_text_line, split_text_chunks
from document_parsing_pdf_postprocess import (
    merge_cross_page_tables,
    reorder_reading_order,
    strip_page_furniture,
)
from document_parsing_pdf_native import supplement_native_layout_tables
from document_parsing_pdf_runtime import convert_pdf_document
from document_parsing_pdf_table_cleanup import promote_text_tables, suppress_duplicate_table_text
from document_parsing_pdf_types import ParsedElement


TABLE_TITLE_PATTERN = re.compile(r"^(?:TABLE|表)\b", flags=re.IGNORECASE)
TABLE_ID_PATTERN = re.compile(r"\b(?:TABLE|表)\s+([A-Z0-9.\-]+)", flags=re.IGNORECASE)
TABLE_SHEET_PATTERN = re.compile(r"\bSHEET\s+(\d+)(?:\s+OF\s+(\d+))?\b", flags=re.IGNORECASE)
STRUCTURED_TABLE_TITLE_PATTERN = re.compile(
    r"\b(?:FIELD\s+CODING\s+FOR|WORD\s+MAP|WORD\s+DESCRIPTION|WORD\s+NUMBER)\b",
    flags=re.IGNORECASE,
)
STRUCTURED_SECTION_TITLE_PATTERN = re.compile(
    r"\b(?:DATA\s+ELEMENT\s+SUMMARY|WORD\s+MAP|WORD\s+DESCRIPTION|WORD\s+NUMBER|FIELD\s+CODING\s+FOR)\b",
    flags=re.IGNORECASE,
)
RULE_ONLY_PATTERN = re.compile(r"^[\s\-_=.\u2500-\u257f]+$")


def _item_page_num(item: Any) -> int:
    provenance = list(getattr(item, "prov", None) or [])
    if not provenance:
        return 1
    return max(1, int(getattr(provenance[0], "page_no", 1) or 1))


def _item_bbox_ratios(
    doc: Any,
    item: Any,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    provenance = list(getattr(item, "prov", None) or [])
    if not provenance:
        return None, None, None, None, None, None, None
    page_num = _item_page_num(item)
    page = getattr(doc, "pages", {}).get(page_num)
    if page is None or getattr(page, "size", None) is None:
        return None, None, None, None, None, None, None
    page_width = float(page.size.width or 0)
    page_height = float(page.size.height or 0)
    if page_width <= 0 or page_height <= 0:
        return None, None, None, None, None, None, None
    bbox = provenance[0].bbox.to_top_left_origin(page_height)
    left, top, right, bottom = bbox.as_tuple()
    left_ratio = max(0.0, left / page_width)
    right_ratio = min(1.0, max(0.0, right / page_width))
    top_ratio = max(0.0, top / page_height)
    bottom_ratio = max(0.0, (page_height - bottom) / page_height)
    center_ratio = (left_ratio + right_ratio) / 2.0
    width_ratio = max(0.0, right_ratio - left_ratio)
    height_ratio = max(0.0, (bottom - top) / page_height)
    return top_ratio, bottom_ratio, left_ratio, right_ratio, center_ratio, width_ratio, height_ratio


def _looks_like_table_title(text: str) -> bool:
    normalized = normalize_text_line(text)
    if not normalized or len(normalized) > 240:
        return False
    if RULE_ONLY_PATTERN.fullmatch(normalized):
        return False
    return bool(TABLE_TITLE_PATTERN.match(normalized))


def _is_noise_title(text: str) -> bool:
    normalized = normalize_text_line(text)
    if not normalized:
        return True
    if RULE_ONLY_PATTERN.fullmatch(normalized):
        return True
    compact = re.sub(r"[^A-Z0-9\u4e00-\u9fff]+", "", normalized.upper())
    return len(compact) < 4


def _item_text_value(item: Any) -> str:
    return str(getattr(item, "text", "") or "").strip()


def _extract_table_identifier(value: str) -> str:
    match = TABLE_ID_PATTERN.search(str(value or ""))
    return str(match.group(1) or "").strip().upper() if match else ""


def _normalize_title_compare_key(value: str) -> str:
    normalized = normalize_text_line(value).upper()
    identifier = _extract_table_identifier(normalized)
    if identifier:
        return identifier
    normalized = TABLE_SHEET_PATTERN.sub("", normalized)
    normalized = re.sub(r"\b(?:CONTINUED|续表|续页)\b", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" .:-")


def _looks_like_sheet_title(text: str) -> bool:
    normalized = normalize_text_line(text)
    if not normalized or _is_noise_title(normalized):
        return False
    return bool(TABLE_SHEET_PATTERN.search(normalized))


def _looks_like_structured_table_title(text: str) -> bool:
    normalized = normalize_text_line(text)
    if not normalized or _is_noise_title(normalized) or len(normalized) > 240:
        return False
    if _looks_like_table_title(normalized) or _looks_like_sheet_title(normalized):
        return True
    return bool(STRUCTURED_TABLE_TITLE_PATTERN.search(normalized))


def _title_candidate_score(
    text: str,
    *,
    source: str,
    item_top_ratio: Optional[float],
    item_center_ratio: Optional[float],
    table_top_ratio: Optional[float],
    table_center_ratio: Optional[float],
) -> int:
    normalized = normalize_text_line(text)
    if not normalized or _is_noise_title(normalized):
        return -1000
    score = len(normalized)
    if _looks_like_table_title(normalized):
        score += 140
    if STRUCTURED_SECTION_TITLE_PATTERN.search(normalized):
        score += 120
    if TABLE_SHEET_PATTERN.search(normalized):
        score += 20
    if _extract_table_identifier(normalized):
        score += 35
    if source == "caption":
        score += 30
    elif source == "neighbor":
        score += 18
    elif source == "page_top":
        score += 12
    elif source == "row_header":
        score += 24
    if item_top_ratio is not None and table_top_ratio is not None:
        delta_top = table_top_ratio - item_top_ratio
        if 0.0 <= delta_top <= 0.10:
            score += 18
        elif abs(delta_top) <= 0.03:
            score += 8
        elif delta_top < -0.06:
            score -= 10
    if item_center_ratio is not None and table_center_ratio is not None:
        if abs(item_center_ratio - table_center_ratio) <= 0.12:
            score += 10
    if normalized.upper().startswith("APPENDIX ") or normalized.upper().startswith("MIL-STD-"):
        score -= 60
    if len(normalized) > 180 and not STRUCTURED_SECTION_TITLE_PATTERN.search(normalized):
        score -= 120
    return score


def _is_table_item(item: Any, table_item_type: type) -> bool:
    return isinstance(item, table_item_type)


def _resolve_table_title(
    items: List[Any],
    item_layouts: List[Dict[str, Optional[float]]],
    index: int,
    table_item: Any,
    doc: Any,
    table_item_type: type,
) -> Tuple[str, List[str], int]:
    table_layout = item_layouts[index]
    page_num = int(table_layout["page_num"] or 1)
    table_top_ratio = table_layout["top_ratio"]
    table_center_ratio = table_layout["center_ratio"]
    candidates: Dict[str, int] = {}

    def consider(text: str, *, source: str, item_index: Optional[int] = None) -> None:
        normalized = normalize_text_line(text)
        if not normalized:
            return
        item_top = None
        item_center = None
        if item_index is not None:
            item_top = item_layouts[item_index]["top_ratio"]
            item_center = item_layouts[item_index]["center_ratio"]
        score = _title_candidate_score(
            normalized,
            source=source,
            item_top_ratio=item_top,
            item_center_ratio=item_center,
            table_top_ratio=table_top_ratio,
            table_center_ratio=table_center_ratio,
        )
        current_score = candidates.get(normalized)
        if current_score is None or score > current_score:
            candidates[normalized] = score

    direct_caption = str(table_item.caption_text(doc) or "").strip()
    consider(direct_caption, source="caption")

    for offset in (-4, -3, -2, -1, 1, 2, 3, 4):
        other_index = index + offset
        if other_index < 0 or other_index >= len(items):
            continue
        if _is_table_item(items[other_index], table_item_type):
            continue
        if int(item_layouts[other_index]["page_num"] or 0) != page_num:
            continue
        text = _item_text_value(items[other_index])
        if not text:
            continue
        other_top = item_layouts[other_index]["top_ratio"]
        if table_top_ratio is not None and other_top is not None and other_top - table_top_ratio > 0.05:
            continue
        consider(text, source="neighbor", item_index=other_index)

    for other_index, other_item in enumerate(items):
        if other_index == index or _is_table_item(other_item, table_item_type):
            continue
        other_layout = item_layouts[other_index]
        if int(other_layout["page_num"] or 0) != page_num:
            continue
        text = _item_text_value(other_item)
        if not text:
            continue
        other_top = other_layout["top_ratio"]
        if other_top is None or other_top > min((table_top_ratio or 0.22) + 0.03, 0.22):
            continue
        consider(text, source="page_top", item_index=other_index)

    if not candidates:
        return "", [], 0
    ranked = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    structured_ranked = [item for item in ranked if STRUCTURED_SECTION_TITLE_PATTERN.search(item[0])]
    if structured_ranked:
        best_title, best_score = structured_ranked[0]
        return best_title, [title for title, _score in ranked], best_score
    best_title, best_score = ranked[0]
    if best_score < 0:
        return "", [title for title, _score in ranked], best_score
    return best_title, [title for title, _score in ranked], best_score


def _is_table_title_text_item(
    items: List[Any],
    item_layouts: List[Dict[str, Optional[float]]],
    index: int,
    item: Any,
    table_item_type: type,
) -> bool:
    text = _item_text_value(item)
    if not normalize_text_line(text):
        return False
    page_num = int(item_layouts[index]["page_num"] or 1)
    top_ratio = item_layouts[index]["top_ratio"]
    center_ratio = item_layouts[index]["center_ratio"]
    normalized_text = normalize_text_line(text)
    for offset in (-4, -3, -2, -1, 1, 2, 3, 4):
        other_index = index + offset
        if other_index < 0 or other_index >= len(items):
            continue
        if not _is_table_item(items[other_index], table_item_type):
            continue
        if int(item_layouts[other_index]["page_num"] or 0) != page_num:
            continue
        other_top_ratio = item_layouts[other_index]["top_ratio"]
        other_center_ratio = item_layouts[other_index]["center_ratio"]
        if top_ratio is not None and other_top_ratio is not None:
            if top_ratio - other_top_ratio > 0.03:
                continue
            if abs(top_ratio - other_top_ratio) > 0.14:
                continue
        if center_ratio is not None and other_center_ratio is not None and abs(center_ratio - other_center_ratio) > 0.20:
            continue
        if _looks_like_table_title(normalized_text):
            return True
        if offset < 0 and top_ratio is not None and other_top_ratio is not None and top_ratio <= other_top_ratio + 0.02:
            return True
    return False


def _table_item_to_rows(table_item: Any, doc: Any) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in getattr(getattr(table_item, "data", None), "grid", []) or []:
        current = []
        for cell in row:
            text = str(cell._get_text(doc=doc) or "").replace("\r\n", "\n")
            current.append(text.strip())
        rows.append(current)
    return rows


def _is_probable_header_row(row: List[str]) -> bool:
    cells = [normalize_text_line(cell) for cell in row if normalize_text_line(cell)]
    if len(cells) < 2:
        return False
    short_ratio = sum(1 for cell in cells if len(cell) <= 40) / len(cells)
    uppercase_ratio = sum(1 for cell in cells if cell == cell.upper()) / len(cells)
    return short_ratio >= 0.6 and uppercase_ratio >= 0.6


def _extract_embedded_title_row(rows: List[List[str]]) -> Tuple[str, List[List[str]]]:
    if len(rows) < 2:
        return "", rows
    first_cells = [normalize_text_line(cell) for cell in rows[0] if normalize_text_line(cell)]
    if not first_cells:
        return "", rows
    candidate = normalize_text_line(" ".join(first_cells))
    if len(candidate) < 8 or len(candidate) > 240:
        return "", rows
    if not _looks_like_structured_table_title(candidate):
        return "", rows
    if not _is_probable_header_row(rows[1]):
        return "", rows
    return candidate, rows[1:]


def _count_table_header_rows(table_item: Any) -> int:
    header_rows = 0
    for row in getattr(getattr(table_item, "data", None), "grid", []) or []:
        if not row:
            break
        if any(bool(getattr(cell, "column_header", False)) for cell in row):
            header_rows += 1
            continue
        break
    return header_rows


def _derive_header_row_count(rows: List[List[str]], model_header_rows: int) -> int:
    if not rows:
        return 0
    if model_header_rows > 0:
        return min(model_header_rows, len(rows))
    return 1 if _is_probable_header_row(rows[0]) else 0


def _extract_pdf_elements(file_path: str) -> List[ParsedElement]:
    from docling_core.types.doc import ContentLayer, TableItem

    doc = convert_pdf_document(file_path)
    items = [
        item
        for item, _level in doc.iterate_items(
            with_groups=False,
            traverse_pictures=False,
            included_content_layers={ContentLayer.BODY},
        )
        if isinstance(item, TableItem) or hasattr(item, "text")
    ]

    item_layouts: List[Dict[str, Optional[float]]] = []
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

    elements: List[ParsedElement] = []
    for index, item in enumerate(items):
        layout = item_layouts[index]
        page_num = max(1, int(layout["page_num"] or 1))
        if isinstance(item, TableItem):
            rows = _table_item_to_rows(item, doc)
            row_title, rows = _extract_embedded_title_row(rows)
            title, title_candidates, title_score = _resolve_table_title(items, item_layouts, index, item, doc, TableItem)
            if row_title:
                title_candidates = [row_title, *title_candidates]
                if title_score < _title_candidate_score(
                    row_title,
                    source="row_header",
                    item_top_ratio=layout["top_ratio"],
                    item_center_ratio=layout["center_ratio"],
                    table_top_ratio=layout["top_ratio"],
                    table_center_ratio=layout["center_ratio"],
                ):
                    title = row_title
                    title_score = _title_candidate_score(
                        row_title,
                        source="row_header",
                        item_top_ratio=layout["top_ratio"],
                        item_center_ratio=layout["center_ratio"],
                        table_top_ratio=layout["top_ratio"],
                        table_center_ratio=layout["center_ratio"],
                    )
            model_header_rows = _count_table_header_rows(item)
            metadata = {
                "title": title,
                "title_candidates": title_candidates,
                "title_score": title_score,
                "title_key": _normalize_title_compare_key(title),
                "table_id": _extract_table_identifier(title),
                "col_count": max((len(row) for row in rows), default=0),
                "row_count": len(rows),
                "header_row_count": _derive_header_row_count(rows, model_header_rows),
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
                metadata={"label": label},
            )
        )
    supplemented = supplement_native_layout_tables(file_path, elements)
    ordered = reorder_reading_order(supplemented)
    promoted = promote_text_tables(ordered)
    deduped = suppress_duplicate_table_text(promoted)
    return merge_cross_page_tables(strip_page_furniture(deduped))


_merge_cross_page_tables = merge_cross_page_tables
_supplement_native_layout_tables = supplement_native_layout_tables
_promote_text_tables = promote_text_tables
_reorder_reading_order = reorder_reading_order
_suppress_duplicate_table_text = suppress_duplicate_table_text
_strip_page_furniture = strip_page_furniture


def append_pdf_blocks(file_path: str, project_id: str, file_name: str, blocks: List[Block]) -> None:
    for element in _extract_pdf_elements(file_path):
        if element.kind == "table":
            table_text = format_table_rows(element.rows)
            content = f"{element.text}\n{table_text}".strip() if element.text else table_text
            append_block(
                blocks,
                project_id=project_id,
                file_name=file_name,
                page_num=element.page_num,
                content=content,
                block_type="table",
                source_document_path=file_path,
                extra_metadata={**dict(element.metadata), "parser": "docling_pdf_layout"},
            )
            continue
        for chunk in split_text_chunks(element.text):
            append_block(
                blocks,
                project_id=project_id,
                file_name=file_name,
                page_num=element.page_num,
                content=chunk,
                block_type="text",
                source_document_path=file_path,
                extra_metadata={**dict(element.metadata), "parser": "docling_pdf_layout"},
            )

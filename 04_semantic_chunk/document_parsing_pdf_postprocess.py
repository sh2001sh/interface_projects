from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from document_parsing_common import normalize_text_line
from document_parsing_layout_analysis import (
    blended_split_ratio,
    canonical_line_signature,
    center_spanning,
    collect_repetition_candidates,
    element_span_ratio,
    infer_document_split_ratio,
    line_alnum_density,
    position_band,
    table_alignment_similarity,
    table_edge_continuity,
    table_schema_similarity,
    text_shape_signature,
)
from document_parsing_pdf_types import ParsedElement


TABLE_ID_PATTERN = re.compile(r"\bTABLE\s+([A-Z0-9.\-]+)", flags=re.IGNORECASE)
TABLE_SHEET_PATTERN = re.compile(r"\bSHEET\s+(\d+)(?:\s+OF\s+(\d+))?\b", flags=re.IGNORECASE)
TABLE_CONTINUATION_PATTERN = re.compile(r"\b(?:continued|续表|续页)\b", flags=re.IGNORECASE)
TABLE_TITLE_LINE_PATTERN = re.compile(r"^(?:TABLE|表)\b", flags=re.IGNORECASE)
PAGE_NUMBER_PATTERN = re.compile(
    r"^(?:[A-Z]-\s*\d+|[A-Z]+-\d+|\d+|第\s*\d+\s*页|page\s*\d+(?:\s*of\s*\d+)?)$",
    flags=re.IGNORECASE,
)
APPENDIX_SHEET_PATTERN = re.compile(
    r"^(?:\(?SHEET\s*\d+\s*OF\s*\d+\)?\s*)?(?:APPENDIX\s+[A-Z]|ANNEX\s+[A-Z])(?:\b.*)?$",
    flags=re.IGNORECASE,
)
HEADER_LINE_PATTERN = re.compile(
    r"^(?:MIL-STD-[A-Z0-9.\-]+|APPENDIX\s+[A-Z]|ANNEX\s+[A-Z]|(?:TABLE|表)\s+[A-Z0-9.\-]+.*(?:SHEET|页)\s*\d+.*)$",
    flags=re.IGNORECASE,
)
FOOTER_LINE_PATTERN = re.compile(r"^(?:\(?sheet\s*\d+\s*of\s*\d+\)?|[A-Z]-\d+|[A-Z]\.\d+-\d+|C-\d+)$", flags=re.IGNORECASE)
WHITESPACE_OR_RULE_PATTERN = re.compile(r"^[\s\-_=\.\u2500-\u257f]+$")
INTENTIONALLY_BLANK_PATTERN = re.compile(
    r"^(?:[-\s]*)?(?:this\s+page\s+)?intentionally\s+left\s+blank(?:[-\s]*)?$",
    flags=re.IGNORECASE,
)

PAGE_LAYOUT_SINGLE = "single"
PAGE_LAYOUT_DOUBLE = "double"
PAGE_LAYOUT_MIXED = "mixed"
MULTI_SPACE_SPLIT_PATTERN = re.compile(r"\s{6,}")
REPEATED_SECTION_MARKER_PATTERN = re.compile(
    r"^(?=[A-Z0-9./()\-]{3,28}(?:\s*\(CONTINUED\))?$)(?=[A-Z0-9./()\-]*\d)[A-Z0-9./()\-]{3,28}(?:\s*\(CONTINUED\))?$",
    flags=re.IGNORECASE,
)
TABLE_SECTION_ROW_PATTERN = re.compile(
    r"^(?P<section>[A-Z]\d+(?:\.\d+)?[A-Z]\d*(?:\s*\(CONTINUED\))?)\b",
    flags=re.IGNORECASE,
)


@dataclass
class PageLayoutProfile:
    layout: str
    split_ratio: Optional[float] = None


def _extract_table_identifier(value: str) -> str:
    match = TABLE_ID_PATTERN.search(str(value or ""))
    return str(match.group(1) or "").strip().upper() if match else ""


def _extract_sheet_info(value: str) -> Tuple[int, int]:
    match = TABLE_SHEET_PATTERN.search(str(value or ""))
    if not match:
        return 0, 0
    return int(match.group(1) or 0), int(match.group(2) or 0)


def _normalize_table_key(value: str) -> str:
    normalized = normalize_text_line(value)
    identifier = _extract_table_identifier(normalized)
    if identifier:
        return identifier
    normalized = TABLE_SHEET_PATTERN.sub("", normalized)
    normalized = TABLE_CONTINUATION_PATTERN.sub("", normalized)
    return normalized.strip(" .:-").upper()


def _normalize_table_title_for_compare(value: str) -> str:
    normalized = normalize_text_line(value).upper()
    normalized = TABLE_SHEET_PATTERN.sub("", normalized)
    normalized = TABLE_CONTINUATION_PATTERN.sub("", normalized)
    normalized = re.sub(r"\(CONTINUED\)", "", normalized)
    normalized = re.sub(r"\bSHEET\s*\d+\s*OF\s*\d+\b", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" .:-")


def _page_span_ratio(element: ParsedElement) -> float:
    return element_span_ratio(element)


def _is_near_top(element: ParsedElement) -> bool:
    return element.top_ratio is not None and element.top_ratio <= 0.12


def _is_near_bottom(element: ParsedElement) -> bool:
    return element.bottom_ratio is not None and element.bottom_ratio <= 0.10


def _is_wide_element(element: ParsedElement) -> bool:
    return center_spanning(element)


def _is_noise_line(text: str) -> bool:
    normalized = normalize_text_line(text)
    if not normalized:
        return True
    if WHITESPACE_OR_RULE_PATTERN.fullmatch(normalized):
        return True
    if INTENTIONALLY_BLANK_PATTERN.fullmatch(normalized):
        return True
    compact = re.sub(r"[^A-Z0-9\u4e00-\u9fff]+", "", normalized.upper())
    return len(compact) < 3


def _looks_like_low_information_text(text: str) -> bool:
    lines = [normalize_text_line(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return True
    if all(_is_noise_line(line) for line in lines):
        return True
    joined = normalize_text_line(" ".join(lines))
    if INTENTIONALLY_BLANK_PATTERN.fullmatch(joined):
        return True
    compact = re.sub(r"[^A-Z0-9\u4e00-\u9fff]+", "", joined.upper())
    if len(compact) < 3:
        return True
    if len(lines) <= 3:
        noisy_lines = sum(1 for line in lines if _is_noise_line(line))
        if noisy_lines == len(lines):
            return True
    return False


def _looks_like_section_marker(text: str) -> bool:
    normalized = normalize_text_line(text)
    if not normalized:
        return False
    if re.fullmatch(r"[A-Z](?:[.-]\d+)+", normalized, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:SECTION|CHAPTER|APPENDIX|ANNEX)\s+[A-Z0-9.-]+", normalized, flags=re.IGNORECASE):
        return True
    return False


def _is_title_like(element: ParsedElement) -> bool:
    if element.kind != "text":
        return False
    text = normalize_text_line(element.text)
    if _is_noise_line(text) or len(text) > 240:
        return False
    if TABLE_TITLE_LINE_PATTERN.match(text):
        return True
    return bool(re.search(r"\b(SHEET|页)\s*\d+\s*(?:OF|/)\s*\d+", text, flags=re.IGNORECASE))


def _looks_like_page_furniture(line: str, element: ParsedElement) -> bool:
    normalized = normalize_text_line(line)
    if not normalized:
        return True
    if WHITESPACE_OR_RULE_PATTERN.fullmatch(normalized):
        return True
    if PAGE_NUMBER_PATTERN.fullmatch(normalized) and (_is_near_top(element) or _is_near_bottom(element)):
        return True
    if FOOTER_LINE_PATTERN.fullmatch(normalized) and _is_near_bottom(element):
        return True
    if APPENDIX_SHEET_PATTERN.fullmatch(normalized) and _is_near_top(element):
        return True
    if HEADER_LINE_PATTERN.fullmatch(normalized) and _is_near_top(element) and not _is_title_like(element):
        return True
    return False


def _is_repeated_furniture(
    line: str,
    element: ParsedElement,
    repeated_lines: set[str],
    repeated_canonical_lines: set[str],
) -> bool:
    exact = normalize_text_line(line).upper()
    canonical = canonical_line_signature(line)
    if exact in repeated_lines or canonical in repeated_canonical_lines:
        return _is_near_top(element) or _is_near_bottom(element)
    return False


def _furniture_score(
    line: str,
    element: ParsedElement,
    repeated_lines: set[str],
    repeated_canonical_lines: set[str],
    repeated_shapes: set[str],
) -> int:
    normalized = normalize_text_line(line)
    if not normalized:
        return 10
    score = 0
    near_top = _is_near_top(element)
    near_bottom = _is_near_bottom(element)
    if _is_repeated_furniture(normalized, element, repeated_lines, repeated_canonical_lines):
        if PAGE_NUMBER_PATTERN.fullmatch(normalized) or FOOTER_LINE_PATTERN.fullmatch(normalized):
            score += 3
        elif HEADER_LINE_PATTERN.fullmatch(normalized) or APPENDIX_SHEET_PATTERN.fullmatch(normalized):
            score += 2
        else:
            score += 1
    shape = text_shape_signature(normalized)
    if shape and shape in repeated_shapes and (near_top or near_bottom):
        score += 2
    if PAGE_NUMBER_PATTERN.fullmatch(normalized):
        score += 4
    if FOOTER_LINE_PATTERN.fullmatch(normalized):
        score += 4
    if APPENDIX_SHEET_PATTERN.fullmatch(normalized):
        score += 4
    if HEADER_LINE_PATTERN.fullmatch(normalized):
        score += 2
    if _looks_like_section_marker(normalized) and near_bottom:
        score += 2
    density = line_alnum_density(normalized)
    if density < 0.45:
        score += 1
    if len(normalized) <= 12:
        score += 1
    return score


def _copy_with_text(element: ParsedElement, text: str) -> ParsedElement:
    return ParsedElement(
        kind=element.kind,
        page_num=element.page_num,
        text=text,
        metadata=dict(element.metadata),
        rows=[list(row) for row in element.rows],
        top_ratio=element.top_ratio,
        bottom_ratio=element.bottom_ratio,
        left_ratio=element.left_ratio,
        right_ratio=element.right_ratio,
        center_ratio=element.center_ratio,
        width_ratio=element.width_ratio,
        height_ratio=element.height_ratio,
        source_index=element.source_index,
        label=element.label,
        column_role=element.column_role,
    )


def strip_page_furniture(elements: List[ParsedElement]) -> List[ParsedElement]:
    repeated_lines, repeated_canonical_lines, repeated_shapes = collect_repetition_candidates(elements)
    cleaned: List[ParsedElement] = []
    for element in elements:
        if element.kind != "text":
            cleaned.append(element)
            continue
        kept_lines = []
        for raw_line in str(element.text or "").splitlines():
            normalized = normalize_text_line(raw_line)
            if not normalized:
                continue
            if _looks_like_page_furniture(normalized, element):
                continue
            if _furniture_score(
                normalized,
                element,
                repeated_lines,
                repeated_canonical_lines,
                repeated_shapes,
            ) >= 6 and not _is_title_like(_copy_with_text(element, normalized)):
                continue
            kept_lines.append(normalized)
        text = "\n".join(kept_lines).strip()
        if text and not _looks_like_low_information_text(text):
            cleaned.append(_copy_with_text(element, text))
    return cleaned


def _default_reading_sort_key(element: ParsedElement) -> Tuple[float, float, int]:
    return (
        float(element.top_ratio if element.top_ratio is not None else 1.0),
        float(element.left_ratio if element.left_ratio is not None else 1.0),
        int(element.source_index if element.source_index is not None else 10**9),
    )


def _is_center_spanning(element: ParsedElement) -> bool:
    return center_spanning(element)


def _layout_candidate_elements(page_elements: List[ParsedElement]) -> List[ParsedElement]:
    return [
        element
        for element in page_elements
        if element.kind in {"text", "table"} and element.center_ratio is not None
    ]


def _candidate_split_ratio(elements: List[ParsedElement]) -> Optional[float]:
    centers = sorted(
        float(element.center_ratio)
        for element in elements
        if element.center_ratio is not None and 0.18 <= float(element.center_ratio) <= 0.82
    )
    if len(centers) < 4:
        return None
    best_gap = 0.0
    best_split = None
    for left, right in zip(centers, centers[1:]):
        gap = right - left
        split = (left + right) / 2.0
        if gap > best_gap and 0.34 <= split <= 0.66:
            best_gap = gap
            best_split = split
    if best_split is None or best_gap < 0.08:
        return None
    return best_split


def _classify_text_role(element: ParsedElement, split_ratio: Optional[float] = None) -> str:
    if _is_center_spanning(element):
        return "full"
    center = element.center_ratio
    if center is None:
        return "full"
    if split_ratio is None:
        split_ratio = 0.5
    band = max(0.03, min(0.08, (_page_span_ratio(element) * 0.3) + 0.03))
    if center <= split_ratio - band:
        return "left"
    if center >= split_ratio + band:
        return "right"
    return "full"


def _classify_page_layout(page_elements: List[ParsedElement]) -> PageLayoutProfile:
    layout_elements = _layout_candidate_elements(page_elements)
    if len(layout_elements) < 4:
        return PageLayoutProfile(layout=PAGE_LAYOUT_SINGLE)
    narrow = [element for element in layout_elements if not _is_center_spanning(element)]
    if len(narrow) < 4:
        return PageLayoutProfile(layout=PAGE_LAYOUT_SINGLE)
    split_ratio = blended_split_ratio(_candidate_split_ratio(narrow), infer_document_split_ratio(layout_elements))
    if split_ratio is None:
        return PageLayoutProfile(layout=PAGE_LAYOUT_SINGLE)
    left = [element for element in narrow if _classify_text_role(element, split_ratio) == "left"]
    right = [element for element in narrow if _classify_text_role(element, split_ratio) == "right"]
    if len(left) < 2 or len(right) < 2:
        return PageLayoutProfile(layout=PAGE_LAYOUT_SINGLE)
    center_crossing = sum(
        1
        for element in narrow
        if element.left_ratio is not None
        and element.right_ratio is not None
        and element.left_ratio < split_ratio - 0.02
        and element.right_ratio > split_ratio + 0.02
        and (element.width_ratio or 0.0) < 0.58
    )
    wide = [element for element in layout_elements if _is_center_spanning(element)]
    if wide or center_crossing > 0:
        return PageLayoutProfile(layout=PAGE_LAYOUT_MIXED, split_ratio=split_ratio)
    return PageLayoutProfile(layout=PAGE_LAYOUT_DOUBLE, split_ratio=split_ratio)


def _sort_column(elements: List[ParsedElement]) -> List[ParsedElement]:
    return sorted(elements, key=_default_reading_sort_key)


def _mark_column_role(element: ParsedElement, role: str) -> ParsedElement:
    metadata = dict(element.metadata)
    metadata.setdefault("column_role", role)
    return _copy_with_metadata(element, metadata, column_role=role)


def _copy_with_metadata(
    element: ParsedElement,
    metadata: Dict[str, object],
    *,
    column_role: Optional[str] = None,
) -> ParsedElement:
    return ParsedElement(
        kind=element.kind,
        page_num=element.page_num,
        text=element.text,
        metadata=dict(metadata),
        rows=[list(row) for row in element.rows],
        top_ratio=element.top_ratio,
        bottom_ratio=element.bottom_ratio,
        left_ratio=element.left_ratio,
        right_ratio=element.right_ratio,
        center_ratio=element.center_ratio,
        width_ratio=element.width_ratio,
        height_ratio=element.height_ratio,
        source_index=element.source_index,
        label=element.label,
        column_role=element.column_role if column_role is None else column_role,
    )


def _vertical_interval(element: ParsedElement) -> Tuple[float, float]:
    top = float(element.top_ratio if element.top_ratio is not None else 1.0)
    height = float(element.height_ratio if element.height_ratio is not None else 0.0)
    if element.bottom_ratio is not None:
        bottom = 1.0 - float(element.bottom_ratio)
    else:
        bottom = top + height
    if bottom < top:
        bottom = top + max(height, 0.01)
    return top, bottom


def _is_local_narrow_region(element: ParsedElement) -> bool:
    if element.kind != "table":
        return False
    width = float(element.width_ratio or 0.0)
    left = float(element.left_ratio if element.left_ratio is not None else 0.0)
    right = float(element.right_ratio if element.right_ratio is not None else 1.0)
    center = float(element.center_ratio if element.center_ratio is not None else 0.5)
    crosses_gutter = left < 0.46 and right > 0.54
    if crosses_gutter:
        return False
    return width <= 0.64 and (center <= 0.46 or center >= 0.54)


def _split_table_columns(element: ParsedElement) -> List[ParsedElement]:
    if element.kind != "table":
        return [element]
    rows = [list(row) for row in element.rows]
    if len(rows) < 2:
        return [element]
    col_count = max((len(row) for row in rows), default=0)
    if col_count < 4:
        return [element]
    width = float(element.width_ratio or 0.0)
    center = float(element.center_ratio or 0.5)
    if width < 0.68 or not (0.42 <= center <= 0.58):
        return [element]

    split_index = col_count // 2
    left_rows = []
    right_rows = []
    for row in rows:
        padded = list(row) + [""] * max(0, col_count - len(row))
        left = padded[:split_index]
        right = padded[split_index:]
        if any(normalize_text_line(cell) for cell in left):
            left_rows.append(left)
        if any(normalize_text_line(cell) for cell in right):
            right_rows.append(right)
    if len(left_rows) < 2 or len(right_rows) < 2:
        return [element]

    left_ratio = float(element.left_ratio if element.left_ratio is not None else 0.0)
    right_ratio = float(element.right_ratio if element.right_ratio is not None else 1.0)
    mid_ratio = (left_ratio + right_ratio) / 2.0
    header_rows = int(element.metadata.get("header_row_count") or 0)

    def build_part(part_rows: List[List[str]], role: str, part_left: float, part_right: float) -> ParsedElement:
        part_width = max(0.0, part_right - part_left)
        part_center = (part_left + part_right) / 2.0
        title = normalize_text_line(" ".join(cell for cell in part_rows[0] if normalize_text_line(cell)))
        metadata = dict(element.metadata)
        metadata.update(
            {
                "row_count": len(part_rows),
                "col_count": max((len(row) for row in part_rows), default=0),
                "header_row_count": min(header_rows, len(part_rows)),
                "split_from_wide_table": True,
                "split_part": role,
            }
        )
        if title:
            metadata["title"] = title
            metadata["table_title"] = title
        return ParsedElement(
            kind="table",
            page_num=element.page_num,
            text=title or element.text,
            metadata=metadata,
            rows=part_rows,
            top_ratio=element.top_ratio,
            bottom_ratio=element.bottom_ratio,
            left_ratio=part_left,
            right_ratio=part_right,
            center_ratio=part_center,
            width_ratio=part_width,
            height_ratio=element.height_ratio,
            source_index=element.source_index,
            label=element.label,
            column_role=role,
        )

    return [
        build_part(left_rows, "left", left_ratio, mid_ratio),
        build_part(right_rows, "right", mid_ratio, right_ratio),
    ]


def _row_text(row: List[str]) -> str:
    return normalize_text_line(" ".join(cell for cell in row if normalize_text_line(cell)))


def _row_is_section_start(row: List[str]) -> Optional[str]:
    text = _row_text(row)
    if not text:
        return None
    match = TABLE_SECTION_ROW_PATTERN.match(text)
    if not match:
        return None
    return normalize_text_line(match.group("section"))


def _split_table_on_section_rows(element: ParsedElement) -> List[ParsedElement]:
    if element.kind != "table":
        return [element]
    title = normalize_text_line(str(element.metadata.get("title") or element.text or ""))
    if "DATA ELEMENT SUMMARY" in title.upper():
        return [element]
    rows = [list(row) for row in element.rows]
    if len(rows) < 4:
        return [element]

    split_points: List[int] = []
    for index in range(1, len(rows)):
        section = _row_is_section_start(rows[index])
        if not section:
            continue
        split_points.append(index)

    if not split_points:
        return [element]

    starts = [0] + split_points
    ranges: List[Tuple[int, int]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(rows)
        if end - start >= 2:
            ranges.append((start, end))
    if len(ranges) < 2:
        return [element]

    top_abs = float(element.top_ratio if element.top_ratio is not None else 0.0)
    bottom_abs = 1.0 - float(element.bottom_ratio if element.bottom_ratio is not None else 0.0)
    if bottom_abs <= top_abs:
        bottom_abs = top_abs + max(float(element.height_ratio or 0.0), 0.01)
    total_span = max(0.01, bottom_abs - top_abs)

    split_parts: List[ParsedElement] = []
    for part_index, (start, end) in enumerate(ranges, start=1):
        part_rows = rows[start:end]
        if len(part_rows) < 2:
            continue
        title = _row_text(part_rows[0]) or str(element.text or "")
        start_fraction = start / max(1, len(rows))
        end_fraction = end / max(1, len(rows))
        part_top = top_abs + (total_span * start_fraction)
        part_bottom = top_abs + (total_span * end_fraction)
        metadata = dict(element.metadata)
        metadata.update(
            {
                "title": title,
                "row_count": len(part_rows),
                "col_count": max((len(row) for row in part_rows), default=0),
                "split_from_sectioned_table": True,
                "split_section_index": part_index,
                "split_section_total": len(ranges),
            }
        )
        split_parts.append(
            ParsedElement(
                kind="table",
                page_num=element.page_num,
                text=title,
                metadata=metadata,
                rows=part_rows,
                top_ratio=part_top,
                bottom_ratio=max(0.0, 1.0 - part_bottom),
                left_ratio=element.left_ratio,
                right_ratio=element.right_ratio,
                center_ratio=element.center_ratio,
                width_ratio=element.width_ratio,
                height_ratio=element.height_ratio,
                source_index=element.source_index,
                label=element.label,
                column_role=element.column_role,
            )
        )
    return split_parts or [element]


def _split_wide_text_block(element: ParsedElement) -> List[ParsedElement]:
    if element.kind != "text":
        return [element]
    width = float(element.width_ratio or 0.0)
    center = float(element.center_ratio or 0.5)
    text = str(element.text or "").strip()
    if width < 0.68 or not (0.42 <= center <= 0.58) or len(text) < 80:
        return [element]
    lines = [line.rstrip() for line in text.splitlines() if normalize_text_line(line)]
    if len(lines) < 4:
        return [element]

    left_lines: List[str] = []
    right_lines: List[str] = []
    split_hits = 0
    for line in lines:
        parts = MULTI_SPACE_SPLIT_PATTERN.split(line, maxsplit=1)
        if len(parts) != 2:
            return [element]
        left, right = normalize_text_line(parts[0]), normalize_text_line(parts[1])
        if not left or not right:
            return [element]
        left_lines.append(left)
        right_lines.append(right)
        split_hits += 1
    if split_hits < 4:
        return [element]

    left_ratio = float(element.left_ratio if element.left_ratio is not None else 0.0)
    right_ratio = float(element.right_ratio if element.right_ratio is not None else 1.0)
    mid_ratio = (left_ratio + right_ratio) / 2.0

    def build_part(lines_part: List[str], role: str, part_left: float, part_right: float) -> ParsedElement:
        part_width = max(0.0, part_right - part_left)
        part_center = (part_left + part_right) / 2.0
        metadata = dict(element.metadata)
        metadata.update({"split_from_wide_text": True, "split_part": role})
        return ParsedElement(
            kind="text",
            page_num=element.page_num,
            text="\n".join(lines_part),
            metadata=metadata,
            rows=[list(row) for row in element.rows],
            top_ratio=element.top_ratio,
            bottom_ratio=element.bottom_ratio,
            left_ratio=part_left,
            right_ratio=part_right,
            center_ratio=part_center,
            width_ratio=part_width,
            height_ratio=element.height_ratio,
            source_index=element.source_index,
            label=element.label,
            column_role=role,
        )

    return [
        build_part(left_lines, "left", left_ratio, mid_ratio),
        build_part(right_lines, "right", mid_ratio, right_ratio),
    ]


def _split_column_text_block_on_repeated_markers(element: ParsedElement) -> List[ParsedElement]:
    if element.kind != "text":
        return [element]
    lines = [normalize_text_line(line) for line in str(element.text or "").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 4:
        return [element]

    marker_indexes = [index for index, line in enumerate(lines) if REPEATED_SECTION_MARKER_PATTERN.fullmatch(line)]
    if len(marker_indexes) < 2:
        return [element]
    if marker_indexes[0] != 0:
        return [element]
    if element.column_role not in {"left", "right"}:
        return [element]

    segments: List[Tuple[int, int]] = []
    segment_start = 0
    for marker_index in marker_indexes[1:]:
        if marker_index <= segment_start:
            continue
        segments.append((segment_start, marker_index))
        segment_start = marker_index
    if segment_start < len(lines):
        segments.append((segment_start, len(lines)))
    if len(segments) < 2:
        return [element]

    top_abs = float(element.top_ratio if element.top_ratio is not None else 0.0)
    bottom_abs = 1.0 - float(element.bottom_ratio if element.bottom_ratio is not None else 0.0)
    if bottom_abs <= top_abs:
        bottom_abs = top_abs + max(float(element.height_ratio or 0.0), 0.01)
    total_span = max(0.01, bottom_abs - top_abs)
    left_ratio = element.left_ratio
    right_ratio = element.right_ratio
    center_ratio = element.center_ratio
    width_ratio = element.width_ratio
    height_ratio = element.height_ratio

    split_parts: List[ParsedElement] = []
    for segment_index, (segment_start, segment_end) in enumerate(segments, start=1):
        segment_lines = lines[segment_start:segment_end]
        if not segment_lines:
            continue
        start_fraction = segment_start / max(1, len(lines))
        end_fraction = segment_end / max(1, len(lines))
        segment_top = top_abs + (total_span * start_fraction)
        segment_bottom = top_abs + (total_span * end_fraction)
        metadata = dict(element.metadata)
        metadata.update(
            {
                "split_from_column_text": True,
                "split_segment_index": segment_index,
                "split_segment_total": len(segments),
            }
        )
        split_parts.append(
            ParsedElement(
                kind="text",
                page_num=element.page_num,
                text="\n".join(segment_lines),
                metadata=metadata,
                rows=[list(row) for row in element.rows],
                top_ratio=segment_top,
                bottom_ratio=max(0.0, 1.0 - segment_bottom),
                left_ratio=left_ratio,
                right_ratio=right_ratio,
                center_ratio=center_ratio,
                width_ratio=width_ratio,
                height_ratio=height_ratio,
                source_index=element.source_index,
                label=element.label,
                column_role=element.column_role,
            )
        )
    return split_parts or [element]


def _split_wide_side_by_side_elements(page_elements: List[ParsedElement]) -> List[ParsedElement]:
    split_elements: List[ParsedElement] = []
    for element in page_elements:
        table_parts = _split_table_columns(element)
        if len(table_parts) > 1:
            for part in table_parts:
                section_parts = _split_table_on_section_rows(part)
                for section_part in section_parts:
                    split_elements.extend(_split_column_text_block_on_repeated_markers(section_part))
            continue
        direct_table_parts = _split_table_on_section_rows(element)
        if len(direct_table_parts) > 1:
            split_elements.extend(direct_table_parts)
            continue
        text_parts = _split_wide_text_block(element)
        for part in text_parts:
            split_elements.extend(_split_column_text_block_on_repeated_markers(part))
    return split_elements


def _mark_region_role(element: ParsedElement, role: str) -> ParsedElement:
    metadata = dict(element.metadata)
    metadata.setdefault("region_role", role)
    metadata.setdefault("column_role", element.column_role or "full")
    return _copy_with_metadata(element, metadata, column_role=element.column_role or "full")


def _overlaps_vertically(previous: ParsedElement, current: ParsedElement) -> bool:
    prev_top, prev_bottom = _vertical_interval(previous)
    curr_top, curr_bottom = _vertical_interval(current)
    return curr_top <= prev_bottom + 0.018 and curr_bottom >= prev_top - 0.018


def _sort_single_layout_regions(ordered: List[ParsedElement]) -> List[ParsedElement]:
    regions: List[List[ParsedElement]] = []
    for element in ordered:
        if not regions:
            regions.append([element])
            continue
        last_region = regions[-1]
        if any(_overlaps_vertically(item, element) for item in last_region):
            last_region.append(element)
        else:
            regions.append([element])

    output: List[ParsedElement] = []
    for region in regions:
        sorted_region = sorted(region, key=_default_reading_sort_key)
        local_tables = [item for item in sorted_region if _is_local_narrow_region(item)]
        if not local_tables:
            output.extend(_mark_region_role(item, "flow") for item in sorted_region)
            continue
        consumed_ids = set()
        for item in sorted_region:
            if id(item) in consumed_ids:
                continue
            if item in local_tables:
                overlapping_tables = [
                    table for table in local_tables if id(table) not in consumed_ids and _overlaps_vertically(item, table)
                ]
                left_tables = [table for table in overlapping_tables if float(table.center_ratio or 0.5) < 0.5]
                right_tables = [table for table in overlapping_tables if float(table.center_ratio or 0.5) >= 0.5]
                output.extend(_mark_region_role(table, "local_left") for table in sorted(left_tables, key=_default_reading_sort_key))
                output.extend(_mark_region_role(table, "local_right") for table in sorted(right_tables, key=_default_reading_sort_key))
                consumed_ids.update(id(table) for table in overlapping_tables)
                continue
            output.append(_mark_region_role(item, "flow"))
            consumed_ids.add(id(item))
    return output


def _sort_double_column_region(region: List[ParsedElement], split_ratio: Optional[float]) -> List[ParsedElement]:
    left_column = _sort_column([_mark_column_role(element, "left") for element in region if _classify_text_role(element, split_ratio) == "left"])
    right_column = _sort_column([_mark_column_role(element, "right") for element in region if _classify_text_role(element, split_ratio) == "right"])
    full_width = _sort_column([_mark_column_role(element, "full") for element in region if _classify_text_role(element, split_ratio) not in {"left", "right"}])
    return left_column + right_column + full_width


def _region_before_anchor(remaining: List[ParsedElement], anchor_top: float) -> List[ParsedElement]:
    return [
        element
        for element in remaining
        if float(element.top_ratio if element.top_ratio is not None else 1.0) < anchor_top
    ]


def _reorder_mixed_layout_page(ordered: List[ParsedElement], split_ratio: Optional[float]) -> List[ParsedElement]:
    spanning = [_mark_column_role(element, "full") for element in ordered if _is_center_spanning(element)]
    if not spanning:
        return _sort_double_column_region(ordered, split_ratio)
    remaining = [element for element in ordered if not _is_center_spanning(element)]
    output: List[ParsedElement] = []
    for anchor in spanning:
        anchor_top = float(anchor.top_ratio if anchor.top_ratio is not None else 1.0)
        region = _region_before_anchor(remaining, anchor_top)
        if region:
            output.extend(_sort_double_column_region(region, split_ratio))
            region_ids = {id(item) for item in region}
            remaining = [element for element in remaining if id(element) not in region_ids]
        output.append(anchor)
    if remaining:
        output.extend(_sort_double_column_region(remaining, split_ratio))
    return output


def _reorder_page_elements(page_elements: List[ParsedElement]) -> List[ParsedElement]:
    split_page_elements = _split_wide_side_by_side_elements(page_elements)
    ordered = sorted(split_page_elements, key=_default_reading_sort_key)
    layout = _classify_page_layout(ordered)
    def with_page_layout(items: List[ParsedElement]) -> List[ParsedElement]:
        output = []
        for item in items:
            metadata = dict(item.metadata)
            metadata.setdefault("page_layout", layout.layout)
            if layout.split_ratio is not None:
                metadata.setdefault("page_split_ratio", layout.split_ratio)
            output.append(_copy_with_metadata(item, metadata))
        return output

    if layout.layout == PAGE_LAYOUT_SINGLE:
        return with_page_layout(
            _sort_single_layout_regions(
                [
                    element if str(element.column_role or "") in {"left", "right"} else _mark_column_role(element, "full")
                    for element in ordered
                ]
            )
        )
    if layout.layout == PAGE_LAYOUT_DOUBLE:
        return with_page_layout(_sort_double_column_region(ordered, layout.split_ratio))
    return with_page_layout(_reorder_mixed_layout_page(ordered, layout.split_ratio))


def reorder_reading_order(elements: List[ParsedElement]) -> List[ParsedElement]:
    pages: Dict[int, List[ParsedElement]] = defaultdict(list)
    for element in elements:
        pages[element.page_num].append(element)
    reordered: List[ParsedElement] = []
    for page_num in sorted(pages):
        reordered.extend(_reorder_page_elements(pages[page_num]))
    return reordered


def _shared_header_row_count(previous: ParsedElement, current: ParsedElement) -> int:
    prev_count = max(1, int(previous.metadata.get("header_row_count") or 0))
    curr_count = max(1, int(current.metadata.get("header_row_count") or 0))
    limit = min(prev_count, curr_count, len(previous.rows), len(current.rows))
    matched = 0
    for index in range(limit):
        prev_row = [normalize_text_line(cell).upper() for cell in previous.rows[index]]
        curr_row = [normalize_text_line(cell).upper() for cell in current.rows[index]]
        if prev_row != curr_row:
            break
        matched += 1
    return matched


def _table_title_score(value: str) -> int:
    normalized = normalize_text_line(value)
    if not normalized:
        return 0
    if WHITESPACE_OR_RULE_PATTERN.fullmatch(normalized):
        return 0
    score = len(normalized)
    if TABLE_TITLE_LINE_PATTERN.match(normalized):
        score += 40
    if TABLE_SHEET_PATTERN.search(normalized):
        score += 20
    if TABLE_CONTINUATION_PATTERN.search(normalized):
        score += 10
    if HEADER_LINE_PATTERN.fullmatch(normalized):
        score -= 15
    return score


def _best_table_title(*values: str) -> str:
    candidates = [normalize_text_line(value) for value in values if normalize_text_line(value)]
    if not candidates:
        return ""
    ranked = sorted(candidates, key=_table_title_score, reverse=True)
    best = ranked[0]
    if WHITESPACE_OR_RULE_PATTERN.fullmatch(best):
        return ""
    return best


def _best_merged_title(previous: ParsedElement, current: ParsedElement) -> str:
    previous_title = normalize_text_line(str(previous.metadata.get("title") or previous.text or ""))
    current_title = normalize_text_line(str(current.metadata.get("title") or current.text or ""))
    previous_compare = _normalize_table_title_for_compare(previous_title)
    current_compare = _normalize_table_title_for_compare(current_title)
    if previous_title and previous_compare and previous_compare == current_compare:
        return previous_title
    return _best_table_title(previous_title, current_title, str(previous.text or ""), str(current.text or ""))


def _table_signature(element: ParsedElement) -> Tuple[str, int, int]:
    title = str(element.metadata.get("title") or element.text or "")
    return _normalize_table_title_for_compare(title), int(element.metadata.get("col_count") or 0), int(element.metadata.get("header_row_count") or 0)


def _column_count_close(previous: ParsedElement, current: ParsedElement) -> bool:
    prev_cols = int(previous.metadata.get("col_count") or 0)
    curr_cols = int(current.metadata.get("col_count") or 0)
    if prev_cols <= 0 or curr_cols <= 0:
        return False
    return abs(prev_cols - curr_cols) <= 1


def _column_similarity(previous: ParsedElement, current: ParsedElement) -> float:
    prev_cols = int(previous.metadata.get("col_count") or 0)
    curr_cols = int(current.metadata.get("col_count") or 0)
    if prev_cols <= 0 or curr_cols <= 0:
        return 0.0
    return 1.0 - (abs(prev_cols - curr_cols) / max(prev_cols, curr_cols))


def _weak_or_missing_title(element: ParsedElement) -> bool:
    title = normalize_text_line(str(element.metadata.get("title") or element.text or ""))
    if not title:
        return True
    return _table_title_score(title) < 30


def _is_short_bridge_text(element: ParsedElement) -> bool:
    if element.kind != "text":
        return False
    text = normalize_text_line(element.text)
    if not text or len(text) > 120:
        return False
    if "|" in text:
        return False
    return True


def _table_merge_score(previous: ParsedElement, current: ParsedElement) -> int:
    previous_title = str(previous.metadata.get("title") or previous.text or "")
    current_title = str(current.metadata.get("title") or current.text or "")
    previous_key = _normalize_table_key(previous_title)
    current_key = _normalize_table_key(current_title)
    previous_compare = _normalize_table_title_for_compare(previous_title)
    current_compare = _normalize_table_title_for_compare(current_title)
    previous_sheet_index, previous_sheet_total = _extract_sheet_info(previous_title)
    previous_end_sheet_index = int(previous.metadata.get("end_sheet_index") or previous_sheet_index or 0)
    previous_end_sheet_total = int(previous.metadata.get("sheet_total") or previous_sheet_total or 0)
    current_sheet_index, current_sheet_total = _extract_sheet_info(current_title)
    same_title = bool(previous_key and current_key and previous_key == current_key)
    same_title_loose = bool(previous_compare and current_compare and previous_compare == current_compare)
    header_match_count = _shared_header_row_count(previous, current)
    header_aligned = header_match_count > 0
    continuation = bool(
        TABLE_CONTINUATION_PATTERN.search(previous_title)
        or TABLE_CONTINUATION_PATTERN.search(current_title)
        or TABLE_SHEET_PATTERN.search(previous_title)
        or TABLE_SHEET_PATTERN.search(current_title)
    )
    score = 0
    if same_title:
        score += 5
    elif same_title_loose:
        score += 4
    if previous_end_sheet_index > 0 and current_sheet_index == previous_end_sheet_index + 1:
        score += 4
        if previous_end_sheet_total == 0 or current_sheet_total == 0 or previous_end_sheet_total == current_sheet_total:
            score += 1
    if continuation:
        score += 2
    if header_aligned:
        score += min(3, header_match_count + 1)
    column_similarity = _column_similarity(previous, current)
    schema_similarity = table_schema_similarity(
        previous.rows,
        current.rows,
        int(previous.metadata.get("header_row_count") or 0),
        int(current.metadata.get("header_row_count") or 0),
    )
    alignment_similarity = table_alignment_similarity(previous, current)
    edge_continuity = table_edge_continuity(previous, current)
    if column_similarity >= 1.0:
        score += 3
    elif column_similarity >= 0.8:
        score += 2
    if schema_similarity >= 0.95:
        score += 3
    elif schema_similarity >= 0.75:
        score += 2
    if alignment_similarity >= 0.8:
        score += 2
    elif alignment_similarity >= 0.6:
        score += 1
    if edge_continuity >= 0.8:
        score += 2
    elif edge_continuity >= 0.5:
        score += 1
    if _weak_or_missing_title(previous) or _weak_or_missing_title(current):
        score += 1 if header_aligned else 0
    if previous_key and current_key and previous_key != current_key:
        score -= 4
    if previous_compare and current_compare and previous_compare != current_compare and not continuation:
        score -= 2
    if schema_similarity < 0.4 and column_similarity < 0.6:
        score -= 3
    return score


def _merge_two_tables(previous: ParsedElement, current: ParsedElement) -> ParsedElement:
    skip_rows = _shared_header_row_count(previous, current)
    merged_rows = list(previous.rows) + list(current.rows[skip_rows:])
    merged_title = _best_merged_title(previous, current)
    merged_pages = list(previous.metadata.get("merged_pages") or [previous.page_num])
    if current.page_num not in merged_pages:
        merged_pages.append(current.page_num)
    metadata = dict(previous.metadata)
    current_sheet_index, current_sheet_total = _extract_sheet_info(str(current.metadata.get("title") or current.text or ""))
    metadata.update(
        {
            "title": merged_title,
            "title_key": _normalize_table_title_for_compare(merged_title),
            "table_id": _extract_table_identifier(merged_title),
            "row_count": len(merged_rows),
            "col_count": max((len(row) for row in merged_rows), default=0),
            "header_row_count": max(
                int(previous.metadata.get("header_row_count") or 0),
                int(current.metadata.get("header_row_count") or 0),
                skip_rows,
            ),
            "end_page": int(current.metadata.get("end_page") or current.page_num),
            "end_sheet_index": current_sheet_index or int(metadata.get("end_sheet_index") or 0),
            "sheet_total": current_sheet_total or int(metadata.get("sheet_total") or 0),
            "merged_pages": merged_pages,
            "merged_cross_page": True,
        }
    )
    return ParsedElement(
        kind="table",
        page_num=previous.page_num,
        text=merged_title,
        metadata=metadata,
        rows=merged_rows,
        top_ratio=previous.top_ratio,
        bottom_ratio=current.bottom_ratio,
        left_ratio=previous.left_ratio,
        right_ratio=previous.right_ratio,
        center_ratio=previous.center_ratio,
        width_ratio=previous.width_ratio,
        height_ratio=previous.height_ratio,
        source_index=previous.source_index,
        label=previous.label,
        column_role=previous.column_role,
    )


def _should_merge_tables(previous: ParsedElement, current: ParsedElement) -> bool:
    if previous.kind != "table" or current.kind != "table":
        return False
    if _is_local_region_table(previous) or _is_local_region_table(current):
        return False
    previous_role = str(previous.column_role or previous.metadata.get("column_role") or "")
    current_role = str(current.column_role or current.metadata.get("column_role") or "")
    if previous_role in {"left", "right"} and current_role in {"left", "right"} and previous_role != current_role:
        return False
    previous_end_page = int(previous.metadata.get("end_page") or previous.page_num)
    page_gap = int(current.page_num) - previous_end_page

    score = _table_merge_score(previous, current)
    previous_title = str(previous.metadata.get("title") or previous.text or "")
    current_title = str(current.metadata.get("title") or current.text or "")
    previous_key = _normalize_table_key(previous_title)
    current_key = _normalize_table_key(current_title)
    previous_pages = {
        int(value)
        for value in (previous.metadata.get("merged_pages") or [])
        if isinstance(value, int) or (isinstance(value, str) and str(value).isdigit())
    }

    if current.page_num in previous_pages and previous_key and current_key and previous_key == current_key:
        if score >= 4:
            return True
        return False

    if page_gap == 0:
        same_page_same_title = bool(previous_key and current_key and previous_key == current_key)
        if same_page_same_title and score >= 4:
            return True
        return False

    if page_gap < 1:
        return False

    if page_gap == 1:
        return score >= 6

    previous_sheet_index, previous_sheet_total = _extract_sheet_info(previous_title)
    current_sheet_index, current_sheet_total = _extract_sheet_info(current_title)
    same_title = bool(previous_key and current_key and previous_key == current_key)
    sheet_gap = current_sheet_index - previous_sheet_index if previous_sheet_index and current_sheet_index else 0
    sheet_total_match = (
        previous_sheet_total == 0
        or current_sheet_total == 0
        or previous_sheet_total == current_sheet_total
    )
    if same_title and sheet_gap > 0 and sheet_total_match and score >= 8:
        return True

    return False


def _normalize_table_element_metadata(element: ParsedElement) -> ParsedElement:
    metadata = dict(element.metadata)
    sheet_index, sheet_total = _extract_sheet_info(str(element.metadata.get("title") or element.text or ""))
    title = _best_table_title(str(element.metadata.get("title") or element.text or ""))
    metadata.setdefault("end_page", element.page_num)
    metadata.setdefault("end_sheet_index", sheet_index)
    metadata.setdefault("sheet_total", sheet_total)
    metadata.setdefault("merged_pages", [element.page_num])
    metadata.setdefault("merged_cross_page", False)
    metadata["title"] = title
    metadata["title_key"] = _normalize_table_title_for_compare(title)
    metadata["table_id"] = _extract_table_identifier(title)
    return ParsedElement(
        kind=element.kind,
        page_num=element.page_num,
        text=title,
        metadata=metadata,
        rows=[list(row) for row in element.rows],
        top_ratio=element.top_ratio,
        bottom_ratio=element.bottom_ratio,
        left_ratio=element.left_ratio,
        right_ratio=element.right_ratio,
        center_ratio=element.center_ratio,
        width_ratio=element.width_ratio,
        height_ratio=element.height_ratio,
        source_index=element.source_index,
        label=element.label,
        column_role=element.column_role,
    )


def _is_local_region_table(element: ParsedElement) -> bool:
    return element.kind == "table" and str(element.metadata.get("region_role") or "").startswith("local_")


def _should_absorb_same_page_fragment(previous: ParsedElement, current: ParsedElement) -> bool:
    if previous.kind != "table" or current.kind != "table":
        return False
    previous_title = str(previous.metadata.get("title") or previous.text or "")
    current_title = str(current.metadata.get("title") or current.text or "")
    previous_key = _normalize_table_key(previous_title)
    current_key = _normalize_table_key(current_title)
    if not previous_key or previous_key != current_key:
        return False
    previous_pages = {
        int(value)
        for value in (previous.metadata.get("merged_pages") or [])
        if isinstance(value, int) or (isinstance(value, str) and str(value).isdigit())
    }
    if current.page_num not in previous_pages:
        return False
    if previous.metadata.get("merged_cross_page") or current.metadata.get("merged_cross_page"):
        return True
    previous_local = _is_local_region_table(previous)
    current_local = _is_local_region_table(current)
    if previous_local and current_local:
        return True
    score = _table_merge_score(previous, current)
    return score >= 4


def merge_cross_page_tables(elements: List[ParsedElement]) -> List[ParsedElement]:
    merged: List[ParsedElement] = []
    for element in elements:
        if element.kind == "table":
            element = _normalize_table_element_metadata(element)
        if merged and _should_absorb_same_page_fragment(merged[-1], element):
            merged[-1] = _merge_two_tables(merged[-1], element)
            continue
        if merged and _should_merge_tables(merged[-1], element):
            merged[-1] = _merge_two_tables(merged[-1], element)
            continue
        if len(merged) >= 2 and merged[-1].kind == "text" and _is_short_bridge_text(merged[-1]) and _should_merge_tables(merged[-2], element):
            merged[-2] = _merge_two_tables(merged[-2], element)
            continue
        if element.kind == "table" and not _is_local_region_table(element):
            for index in range(len(merged) - 1, -1, -1):
                candidate = merged[index]
                if candidate.kind != "table" or _is_local_region_table(candidate):
                    continue
                if _should_merge_tables(candidate, element):
                    merged[index] = _merge_two_tables(candidate, element)
                    break
            else:
                merged.append(element)
            continue
        merged.append(element)
    return merged


__all__ = [
    "ParsedElement",
    "merge_cross_page_tables",
    "reorder_reading_order",
    "strip_page_furniture",
]

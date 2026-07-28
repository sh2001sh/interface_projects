from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from document_parsing_common import normalize_text_line
from document_parsing_pdf_types import ParsedElement


Box = Tuple[float, float, float, float]
TOKEN_PATTERN = re.compile(r"[A-Z0-9]+")
FIELD_CODING_HEADER_PATTERN = re.compile(
    r"\bDFI\s+DUI\s+DUI/DI\s+NAME\s+DI\s+BIT\s+CODE\s+DUI/DI\s+EXPLANATION\b",
    flags=re.IGNORECASE,
)
FIELD_CODING_ROW_PATTERN = re.compile(r"(?<!\d)(\d{3,4})\s+(\d{3})\b")
FIELD_CODING_TITLE_PATTERN = re.compile(
    r"\bFIELD\s+CODING\s+FOR\s+.+?\(?SHEET\s+\d+(?:\s+OF\s+\d+)?\)?",
    flags=re.IGNORECASE,
)
FIELD_CODING_HEADER = ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"]
WORD_MAP_TITLE_PATTERN = re.compile(r"\bWORD\s+MAP\b", flags=re.IGNORECASE)
WORD_DESCRIPTION_TITLE_PATTERN = re.compile(r"\bWORD\s+DESCRIPTION\b", flags=re.IGNORECASE)
WORD_NUMBER_LINE_PATTERN = re.compile(r"\bWORD\s+NUMBER\s*:\s*([A-Z]\d+\.\d+[A-Z]?\d*)", flags=re.IGNORECASE)
WORD_TITLE_LINE_PATTERN = re.compile(r"\bWORD\s+TITLE\s*:\s*(.+)$", flags=re.IGNORECASE)
ASCII_RULE_PATTERN = re.compile(r"[-:_]{4,}")
EXPLANATION_START_PATTERN = re.compile(
    r"\b(?:SPECIFIES|INDICATES|IDENTIFIES|CONTAINS|PROVIDES|DEFINES|DISPLAY|DISPLAYS|SEQUENCE|GENERAL)\b",
    flags=re.IGNORECASE,
)


def _element_box(element: ParsedElement) -> Optional[Box]:
    if (
        element.left_ratio is None
        or element.right_ratio is None
        or element.top_ratio is None
        or element.bottom_ratio is None
    ):
        return None
    return (
        max(0.0, float(element.left_ratio)),
        max(0.0, float(element.top_ratio)),
        min(1.0, float(element.right_ratio)),
        min(1.0, 1.0 - float(element.bottom_ratio)),
    )


def _expanded_table_box(element: ParsedElement) -> Optional[Box]:
    box = _element_box(element)
    if box is None:
        return None
    return (
        max(0.0, box[0] - 0.12),
        max(0.0, box[1] - 0.04),
        min(1.0, box[2] + 0.12),
        min(1.0, box[3] + 0.04),
    )


def _intersection_ratio(inner: Box, outer: Box) -> float:
    left = max(inner[0], outer[0])
    top = max(inner[1], outer[1])
    right = min(inner[2], outer[2])
    bottom = min(inner[3], outer[3])
    if right <= left or bottom <= top:
        return 0.0
    inner_area = max(1e-6, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return ((right - left) * (bottom - top)) / inner_area


def _center_inside(box: Box, container: Box) -> bool:
    x = (box[0] + box[2]) / 2.0
    y = (box[1] + box[3]) / 2.0
    return container[0] <= x <= container[2] and container[1] <= y <= container[3]


def _compact_text(value: str) -> str:
    normalized = normalize_text_line(value).upper()
    return re.sub(r"[^A-Z0-9]+", "", normalized)


def _tokens(value: str) -> Set[str]:
    normalized = normalize_text_line(value).upper()
    return set(TOKEN_PATTERN.findall(normalized))


def _table_text_parts(table: ParsedElement) -> Iterable[str]:
    yield str(table.text or "")
    yield str(table.metadata.get("title") or "")
    for row in table.rows:
        for cell in row:
            yield str(cell or "")


def _table_compact_text(table: ParsedElement) -> str:
    return _compact_text(" ".join(_table_text_parts(table)))


def _table_tokens(table: ParsedElement) -> Set[str]:
    output: Set[str] = set()
    for part in _table_text_parts(table):
        output.update(_tokens(part))
    return output


def _content_covered_by_table(text: ParsedElement, table: ParsedElement) -> bool:
    text_compact = _compact_text(text.text)
    if not text_compact or len(text_compact) < 4:
        return False
    table_compact = _table_compact_text(table)
    if len(text_compact) >= 8 and text_compact in table_compact:
        return True

    text_tokens = _tokens(text.text)
    if len(text_tokens) < 3:
        table_tokens = _table_tokens(table)
        return len(text_tokens) >= 2 and text_tokens <= table_tokens
    matched = len(text_tokens & _table_tokens(table))
    return matched >= max(3, int(len(text_tokens) * 0.75))


def _in_table_region(text: ParsedElement, table: ParsedElement) -> bool:
    text_box = _element_box(text)
    table_box = _expanded_table_box(table)
    if text_box is None or table_box is None:
        return False
    if _center_inside(text_box, table_box):
        return True
    return _intersection_ratio(text_box, table_box) >= 0.25


def suppress_duplicate_table_text(elements: Sequence[ParsedElement]) -> List[ParsedElement]:
    """Remove text fragments duplicated by table extraction on the same page."""

    tables_by_page: Dict[int, List[ParsedElement]] = defaultdict(list)
    for element in elements:
        if element.kind == "table":
            tables_by_page[int(element.page_num)].append(element)

    cleaned: List[ParsedElement] = []
    for element in elements:
        if element.kind != "text":
            cleaned.append(element)
            continue
        duplicate = any(
            _in_table_region(element, table) and _content_covered_by_table(element, table)
            for table in tables_by_page.get(int(element.page_num), [])
        )
        if not duplicate:
            cleaned.append(element)
    return cleaned


def _extract_promoted_title(prefix: str) -> str:
    normalized = normalize_text_line(prefix)
    matches = list(FIELD_CODING_TITLE_PATTERN.finditer(normalized))
    if matches:
        return normalize_text_line(matches[-1].group(0))
    if 8 <= len(normalized) <= 240:
        return normalized
    return ""


def _split_promoted_row_chunks(body: str) -> List[Tuple[str, str, str]]:
    matches = list(FIELD_CODING_ROW_PATTERN.finditer(body))
    chunks: List[Tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chunk = normalize_text_line(body[match.end() : end])
        if chunk:
            chunks.append((match.group(1), match.group(2), chunk))
    return chunks


def _split_name_and_explanation(value: str) -> Tuple[str, str, str]:
    parts = re.split(r"\s*[-_=]{4,}\s*", normalize_text_line(value), maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        return parts[0].strip(), "", parts[1].strip()
    marker = EXPLANATION_START_PATTERN.search(value)
    if marker and marker.start() >= 4:
        return value[: marker.start()].strip(" -"), "", value[marker.start() :].strip()
    return normalize_text_line(value), "", ""


def _field_coding_text_to_table(element: ParsedElement) -> Optional[ParsedElement]:
    normalized = normalize_text_line(element.text)
    header_match = FIELD_CODING_HEADER_PATTERN.search(normalized)
    if not header_match:
        return None
    rows = [FIELD_CODING_HEADER]
    for dfi, dui, chunk in _split_promoted_row_chunks(normalized[header_match.end() :]):
        name, bit_code, explanation = _split_name_and_explanation(chunk)
        rows.append([dfi, dui, name, bit_code, explanation])
    if len(rows) < 2:
        return None
    title = _extract_promoted_title(normalized[: header_match.start()])
    if not title:
        return None
    metadata = dict(element.metadata)
    metadata.update(
        {
            "title": title,
            "col_count": len(FIELD_CODING_HEADER),
            "row_count": len(rows),
            "header_row_count": 1,
            "promoted_from_text": True,
        }
    )
    return ParsedElement(
        kind="table",
        page_num=element.page_num,
        text=title,
        metadata=metadata,
        rows=rows,
        top_ratio=element.top_ratio,
        bottom_ratio=element.bottom_ratio,
        left_ratio=element.left_ratio,
        right_ratio=element.right_ratio,
        center_ratio=element.center_ratio,
        width_ratio=element.width_ratio,
        height_ratio=element.height_ratio,
        source_index=element.source_index,
        label="table",
        column_role=element.column_role,
    )


def _copy_element_geometry(source: ParsedElement, *, text: str, rows: List[List[str]], metadata: Dict[str, object]) -> ParsedElement:
    return ParsedElement(
        kind="table",
        page_num=source.page_num,
        text=text,
        metadata=dict(metadata),
        rows=rows,
        top_ratio=source.top_ratio,
        bottom_ratio=source.bottom_ratio,
        left_ratio=source.left_ratio,
        right_ratio=source.right_ratio,
        center_ratio=source.center_ratio,
        width_ratio=source.width_ratio,
        height_ratio=source.height_ratio,
        source_index=source.source_index,
        label="table",
        column_role=source.column_role,
    )


def _merge_text_geometry(elements: Sequence[ParsedElement]) -> Dict[str, Optional[float]]:
    tops = [float(item.top_ratio) for item in elements if item.top_ratio is not None]
    bottoms = [1.0 - float(item.bottom_ratio) for item in elements if item.bottom_ratio is not None]
    lefts = [float(item.left_ratio) for item in elements if item.left_ratio is not None]
    rights = [float(item.right_ratio) for item in elements if item.right_ratio is not None]
    top = min(tops) if tops else None
    bottom_abs = max(bottoms) if bottoms else None
    left = min(lefts) if lefts else None
    right = max(rights) if rights else None
    return {
        "top_ratio": top,
        "bottom_ratio": 1.0 - bottom_abs if bottom_abs is not None else None,
        "left_ratio": left,
        "right_ratio": right,
        "center_ratio": ((left + right) / 2.0) if left is not None and right is not None else None,
        "width_ratio": (right - left) if left is not None and right is not None else None,
        "height_ratio": (bottom_abs - top) if top is not None and bottom_abs is not None else None,
    }


def _table_from_text_group(
    group: Sequence[ParsedElement],
    *,
    title: str,
    rows: List[List[str]],
    table_type: str,
    header_row_count: int = 1,
) -> ParsedElement:
    first = group[0]
    geometry = _merge_text_geometry(group)
    metadata = dict(first.metadata)
    metadata.update(
        {
            "title": title,
            "col_count": max((len(row) for row in rows), default=0),
            "row_count": len(rows),
            "header_row_count": header_row_count,
            "promoted_from_text": True,
            "promoted_table_type": table_type,
        }
    )
    return ParsedElement(
        kind="table",
        page_num=first.page_num,
        text=title,
        metadata=metadata,
        rows=rows,
        top_ratio=geometry["top_ratio"],
        bottom_ratio=geometry["bottom_ratio"],
        left_ratio=geometry["left_ratio"],
        right_ratio=geometry["right_ratio"],
        center_ratio=geometry["center_ratio"],
        width_ratio=geometry["width_ratio"],
        height_ratio=geometry["height_ratio"],
        source_index=first.source_index,
        label="table",
        column_role=first.column_role,
    )


def _field_coding_group_to_table(group: Sequence[ParsedElement]) -> Optional[ParsedElement]:
    text = " ".join(normalize_text_line(item.text) for item in group if normalize_text_line(item.text))
    if not FIELD_CODING_HEADER_PATTERN.search(text):
        return None
    synthetic = ParsedElement(kind="text", page_num=group[0].page_num, text=text, metadata=dict(group[0].metadata))
    table = _field_coding_text_to_table(synthetic)
    if table is None:
        return None
    geometry = _merge_text_geometry(group)
    metadata = dict(table.metadata)
    metadata["promoted_group_size"] = len(group)
    return ParsedElement(
        kind="table",
        page_num=group[0].page_num,
        text=table.text,
        metadata=metadata,
        rows=table.rows,
        top_ratio=geometry["top_ratio"],
        bottom_ratio=geometry["bottom_ratio"],
        left_ratio=geometry["left_ratio"],
        right_ratio=geometry["right_ratio"],
        center_ratio=geometry["center_ratio"],
        width_ratio=geometry["width_ratio"],
        height_ratio=geometry["height_ratio"],
        source_index=group[0].source_index,
        label="table",
        column_role=group[0].column_role,
    )


def _ascii_lines_to_rows(lines: Sequence[str]) -> List[List[str]]:
    rows: List[List[str]] = []
    for line in lines:
        normalized = normalize_text_line(line)
        if not normalized:
            continue
        if ASCII_RULE_PATTERN.fullmatch(normalized.replace(" ", "")):
            rows.append([normalized])
            continue
        parts = [part.strip() for part in re.split(r"\s{2,}|\s+\|\s+|\|", normalized) if part.strip()]
        rows.append(parts if len(parts) > 1 else [normalized])
    return rows


def _word_map_group_to_table(group: Sequence[ParsedElement]) -> Optional[ParsedElement]:
    lines = [normalize_text_line(item.text) for item in group if normalize_text_line(item.text)]
    joined = " ".join(lines)
    if not (WORD_MAP_TITLE_PATTERN.search(joined) or WORD_DESCRIPTION_TITLE_PATTERN.search(joined)):
        return None
    has_ascii = sum(1 for line in lines if ASCII_RULE_PATTERN.search(line) or ":" in line) >= 3
    has_word_number = any(WORD_NUMBER_LINE_PATTERN.search(line) for line in lines)
    if not has_word_number or not has_ascii:
        return None
    number = ""
    title_line = ""
    for line in lines:
        number_match = WORD_NUMBER_LINE_PATTERN.search(line)
        if number_match and not number:
            number = number_match.group(1).upper()
        title_match = WORD_TITLE_LINE_PATTERN.search(line)
        if title_match and not title_line:
            title_line = normalize_text_line(title_match.group(1))
    table_type = "word_description" if WORD_DESCRIPTION_TITLE_PATTERN.search(joined) else "word_map"
    title = f"{table_type.replace('_', ' ').upper()}: {number}" if number else table_type.replace("_", " ").upper()
    if title_line:
        title = f"{title} {title_line}"
    rows = _ascii_lines_to_rows(lines)
    if len(rows) < 4:
        return None
    return _table_from_text_group(group, title=title, rows=rows, table_type=table_type, header_row_count=0)


def _promote_page_text_groups(elements: Sequence[ParsedElement]) -> List[ParsedElement]:
    output: List[ParsedElement] = []
    index = 0
    while index < len(elements):
        element = elements[index]
        if element.kind != "text":
            output.append(element)
            index += 1
            continue
        line = normalize_text_line(element.text)
        if FIELD_CODING_TITLE_PATTERN.search(line):
            group = [element]
            cursor = index + 1
            while cursor < len(elements) and elements[cursor].kind == "text" and len(group) < 80:
                next_line = normalize_text_line(elements[cursor].text)
                if cursor > index + 1 and FIELD_CODING_TITLE_PATTERN.search(next_line):
                    break
                group.append(elements[cursor])
                if cursor > index + 3 and cursor + 1 < len(elements) and FIELD_CODING_TITLE_PATTERN.search(normalize_text_line(elements[cursor + 1].text)):
                    break
                cursor += 1
            table = _field_coding_group_to_table(group)
            if table is not None:
                output.append(table)
                index += len(group)
                continue
        if WORD_MAP_TITLE_PATTERN.search(line) or WORD_DESCRIPTION_TITLE_PATTERN.search(line):
            group = [element]
            cursor = index + 1
            while cursor < len(elements) and elements[cursor].kind == "text" and len(group) < 50:
                next_line = normalize_text_line(elements[cursor].text)
                if cursor > index + 1 and (
                    WORD_MAP_TITLE_PATTERN.search(next_line)
                    or WORD_DESCRIPTION_TITLE_PATTERN.search(next_line)
                    or FIELD_CODING_TITLE_PATTERN.search(next_line)
                ):
                    break
                group.append(elements[cursor])
                cursor += 1
            table = _word_map_group_to_table(group)
            if table is not None:
                output.append(table)
                index += len(group)
                continue
        output.append(element)
        index += 1
    return output


def promote_text_tables(elements: Sequence[ParsedElement]) -> List[ParsedElement]:
    """Promote dense structured table-like text blocks into table elements."""

    by_page: Dict[int, List[ParsedElement]] = defaultdict(list)
    for element in elements:
        by_page[int(element.page_num)].append(element)

    page_promoted: List[ParsedElement] = []
    for page_num in sorted(by_page):
        page_promoted.extend(_promote_page_text_groups(by_page[page_num]))

    promoted: List[ParsedElement] = []
    for element in page_promoted:
        if element.kind == "text":
            table = _field_coding_text_to_table(element)
            promoted.append(table if table is not None else element)
            continue
        promoted.append(element)
    return promoted


__all__ = ["promote_text_tables", "suppress_duplicate_table_text"]

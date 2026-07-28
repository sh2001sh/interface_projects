from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from document_parsing_common import normalize_text_line
from document_parsing_pdf_types import ParsedElement


logger = logging.getLogger(__name__)

FIELD_CODING_TITLE_PATTERN = re.compile(r"\bFIELD\s+CODING\s+FOR\b", flags=re.IGNORECASE)
FIELD_CODING_ANCHOR_PATTERN = re.compile(r"^\d{3,4}\s+\d{3}\b")
WORD_DESCRIPTION_TITLE_PATTERN = re.compile(r"\bWORD\s+DESCRIPTION\b", flags=re.IGNORECASE)
WORD_MAP_TITLE_PATTERN = re.compile(r"\bWORD\s+MAP\b", flags=re.IGNORECASE)
RULE_PATTERN = re.compile(r"^[\-_=.\u2500-\u257f]+$")


@dataclass(frozen=True)
class NativeWord:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass(frozen=True)
class NativeLine:
    words: Tuple[NativeWord, ...]

    @property
    def text(self) -> str:
        return normalize_text_line(" ".join(word.text for word in self.words))

    @property
    def top(self) -> float:
        return min((word.top for word in self.words), default=1.0)

    @property
    def bottom(self) -> float:
        return max((word.bottom for word in self.words), default=0.0)


def _extract_words(page: object) -> List[NativeWord]:
    raw_words = page.extract_words(
        x_tolerance=1,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
    )
    width = float(getattr(page, "width", 0) or 0)
    height = float(getattr(page, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return []
    words: List[NativeWord] = []
    for word in raw_words:
        text = normalize_text_line(str(word.get("text") or ""))
        if not text:
            continue
        words.append(
            NativeWord(
                text=text,
                x0=max(0.0, float(word["x0"]) / width),
                x1=min(1.0, float(word["x1"]) / width),
                top=max(0.0, float(word["top"]) / height),
                bottom=min(1.0, float(word["bottom"]) / height),
            )
        )
    return words


def _group_lines(words: Sequence[NativeWord]) -> List[NativeLine]:
    lines: List[List[NativeWord]] = []
    for word in sorted(words, key=lambda item: (item.top, item.x0)):
        if not lines:
            lines.append([word])
            continue
        current_top = sum(item.top for item in lines[-1]) / len(lines[-1])
        if abs(word.top - current_top) <= 0.006:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [NativeLine(tuple(sorted(line, key=lambda item: item.x0))) for line in lines]


def _line_has_tokens(line: NativeLine, tokens: Sequence[str]) -> bool:
    text = f" {line.text.upper()} "
    return all(f" {token.upper()} " in text for token in tokens)


def _find_line_index(lines: Sequence[NativeLine], pattern: re.Pattern[str]) -> int:
    for index, line in enumerate(lines):
        if pattern.search(line.text):
            return index
    return -1


def _find_field_header_index(lines: Sequence[NativeLine]) -> int:
    for index, line in enumerate(lines):
        if _line_has_tokens(line, ["DFI", "DUI", "DUI/DI", "NAME", "DI", "BIT", "CODE", "EXPLANATION"]):
            return index
    return -1


def _text_for_words(words: Iterable[NativeWord]) -> str:
    return normalize_text_line(" ".join(word.text for word in words))


def _column_starts_from_header(header: NativeLine, labels: Sequence[str]) -> Optional[List[float]]:
    starts: List[float] = []
    used = -1
    upper_labels = [label.upper() for label in labels]
    words = list(header.words)
    for label in upper_labels:
        found = -1
        for index, word in enumerate(words):
            if index <= used:
                continue
            if word.text.upper() == label:
                found = index
                break
        if found < 0:
            return None
        used = found
        starts.append(words[found].x0)
    return starts


def _assign_cells(line: NativeLine, starts: Sequence[float]) -> List[str]:
    cells: List[List[NativeWord]] = [[] for _ in starts]
    for word in line.words:
        column_index = 0
        for index, start in enumerate(starts):
            if word.x0 >= start - 0.012:
                column_index = index
        cells[column_index].append(word)
    return [_text_for_words(cell) for cell in cells]


def _table_bbox(lines: Sequence[NativeLine]) -> Tuple[float, float, float, float]:
    words = [word for line in lines for word in line.words]
    return (
        min((word.x0 for word in words), default=0.0),
        min((word.top for word in words), default=0.0),
        max((word.x1 for word in words), default=1.0),
        max((word.bottom for word in words), default=1.0),
    )


def _build_table_element(
    *,
    page_num: int,
    title: str,
    rows: List[List[str]],
    lines: Sequence[NativeLine],
    source: str,
) -> Optional[ParsedElement]:
    if len(rows) < 3:
        return None
    left, top, right, bottom = _table_bbox(lines)
    metadata = {
        "title": title,
        "col_count": max((len(row) for row in rows), default=0),
        "row_count": len(rows),
        "header_row_count": 1,
        "native_text_fallback": True,
        "native_text_fallback_source": source,
    }
    return ParsedElement(
        kind="table",
        page_num=page_num,
        text=title,
        rows=rows,
        metadata=metadata,
        top_ratio=top,
        bottom_ratio=max(0.0, 1.0 - bottom),
        left_ratio=left,
        right_ratio=right,
        center_ratio=(left + right) / 2.0,
        width_ratio=max(0.0, right - left),
        height_ratio=max(0.0, bottom - top),
        label="table",
    )


def _field_coding_table(page_num: int, lines: Sequence[NativeLine]) -> Optional[ParsedElement]:
    header_index = _find_field_header_index(lines)
    if header_index < 0:
        return None
    title_index = _find_line_index(lines[:header_index], FIELD_CODING_TITLE_PATTERN)
    if title_index < 0:
        return None
    header = lines[header_index]
    starts = _column_starts_from_header(header, ["DFI", "DUI", "DUI/DI", "DI", "DUI/DI"])
    if starts is None or len(starts) != 5:
        return None
    rows = [["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"]]
    table_lines = [header]
    anchor_count = 0
    for line in lines[header_index + 1 :]:
        text = line.text
        if not text or RULE_PATTERN.fullmatch(text):
            table_lines.append(line)
            continue
        if text.upper().startswith("THIS PAGE INTENTIONALLY LEFT BLANK"):
            break
        cells = _assign_cells(line, starts)
        if any(cells):
            if FIELD_CODING_ANCHOR_PATTERN.match(" ".join(cell for cell in cells[:2] if cell)):
                anchor_count += 1
                rows.append(cells)
            elif len(rows) > 1:
                for index, cell in enumerate(cells):
                    if cell:
                        rows[-1][index] = normalize_text_line(f"{rows[-1][index]} {cell}")
            else:
                rows.append(cells)
            table_lines.append(line)
    if anchor_count < 1:
        return None
    return _build_table_element(
        page_num=page_num,
        title=lines[title_index].text,
        rows=rows,
        lines=table_lines,
        source="field_coding",
    )


def _word_description_table(page_num: int, lines: Sequence[NativeLine]) -> Optional[ParsedElement]:
    title_index = _find_line_index(lines, WORD_DESCRIPTION_TITLE_PATTERN)
    if title_index < 0:
        return None
    reference_index = -1
    for index, line in enumerate(lines[title_index + 1 :], start=title_index + 1):
        if "REFERENCE" in line.text.upper() and "BIT" in line.text.upper():
            reference_index = index
            break
    if reference_index < 0 or reference_index + 1 >= len(lines):
        return None
    detail_header = lines[reference_index + 1]
    if not _line_has_tokens(detail_header, ["DFI/DUI", "DATA", "FIELD", "DESCRIPTOR"]):
        return None
    starts = [0.23, 0.291, 0.473, 0.527, 0.582]
    rows = [["REFERENCE DFI/DUI", "DATA FIELD DESCRIPTOR", "BIT POSITION", "# BITS", "RESOLUTION, CODING, ETC"]]
    table_lines = [lines[reference_index], detail_header]
    anchor_count = 0
    for line in lines[reference_index + 2 :]:
        text = line.text
        if not text or RULE_PATTERN.fullmatch(text):
            continue
        cells = _assign_cells(line, starts)
        if FIELD_CODING_ANCHOR_PATTERN.match(" ".join(cell for cell in cells[:1] if cell)):
            anchor_count += 1
        if any(cells):
            rows.append(cells)
            table_lines.append(line)
    if anchor_count < 2:
        return None
    return _build_table_element(
        page_num=page_num,
        title=lines[title_index].text,
        rows=rows,
        lines=table_lines,
        source="word_description",
    )


def _word_map_table(page_num: int, lines: Sequence[NativeLine]) -> Optional[ParsedElement]:
    title_index = _find_line_index(lines, WORD_MAP_TITLE_PATTERN)
    if title_index < 0:
        return None
    bit_lines = [line for line in lines if len(re.findall(r"\b\d{2}\b", line.text)) >= 8]
    rule_lines = [line for line in lines if RULE_PATTERN.fullmatch(line.text)]
    if not bit_lines or len(rule_lines) < 3:
        return None
    rows = [[word.text for word in line.words] for line in lines[title_index + 1 :] if line.words]
    if len(rows) < 6:
        return None
    return _build_table_element(
        page_num=page_num,
        title=lines[title_index].text,
        rows=rows,
        lines=lines[title_index:],
        source="word_map",
    )


def _native_tables_for_page(page_num: int, page: object) -> List[ParsedElement]:
    words = _extract_words(page)
    if len(words) < 20:
        return []
    lines = _group_lines(words)
    candidates = [
        _field_coding_table(page_num, lines),
        _word_description_table(page_num, lines),
        _word_map_table(page_num, lines),
    ]
    return [candidate for candidate in candidates if candidate is not None]


def _page_has_docling_table(elements: Sequence[ParsedElement], page_num: int) -> bool:
    return any(element.kind == "table" and element.page_num == page_num for element in elements)


def _replaces_text(element: ParsedElement, table: ParsedElement) -> bool:
    if element.kind != "text" or element.page_num != table.page_num:
        return False
    if element.top_ratio is None or table.top_ratio is None:
        return False
    text = normalize_text_line(element.text).upper()
    title = normalize_text_line(table.text).upper()
    if title and title in text:
        return True
    table_top = float(table.top_ratio)
    table_bottom = 1.0 - float(table.bottom_ratio or 0.0)
    element_top = float(element.top_ratio)
    element_bottom = 1.0 - float(element.bottom_ratio or 0.0)
    return element_top >= table_top - 0.02 and element_bottom <= table_bottom + 0.02


def supplement_native_layout_tables(file_path: str, elements: Sequence[ParsedElement]) -> List[ParsedElement]:
    """Add native-text table reconstructions for pages where Docling missed the table."""

    try:
        import pdfplumber
    except Exception as exc:  # pragma: no cover - depends on optional runtime package
        logger.warning("pdfplumber unavailable; native layout fallback skipped: %s", exc)
        return list(elements)

    try:
        with pdfplumber.open(file_path) as pdf:
            fallback_tables: List[ParsedElement] = []
            for page_index, page in enumerate(pdf.pages, start=1):
                if _page_has_docling_table(elements, page_index):
                    continue
                fallback_tables.extend(_native_tables_for_page(page_index, page))
    except Exception as exc:
        logger.warning("native layout fallback failed for %s: %s", file_path, exc)
        return list(elements)

    if not fallback_tables:
        return list(elements)

    output: List[ParsedElement] = []
    for element in elements:
        if any(_replaces_text(element, table) for table in fallback_tables):
            continue
        output.append(element)
    output.extend(fallback_tables)
    return output


__all__ = ["supplement_native_layout_tables"]

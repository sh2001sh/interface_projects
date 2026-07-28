from __future__ import annotations

import math
import re
from collections import defaultdict
from statistics import median
from typing import DefaultDict, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from document_parsing_common import normalize_text_line
from document_parsing_pdf_types import ParsedElement


_ROMAN_NUMERAL_PATTERN = re.compile(r"\b[IVXLCDM]+\b", flags=re.IGNORECASE)
_NUMBER_PATTERN = re.compile(r"\d+")
_NONWORD_PATTERN = re.compile(r"[^A-Z0-9\u4e00-\u9fff]+")


def text_shape_signature(value: str) -> str:
    normalized = normalize_text_line(value).upper()
    normalized = re.sub(r"[A-Z]+", "A", normalized)
    normalized = re.sub(r"\d+", "9", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def canonical_line_signature(value: str) -> str:
    normalized = normalize_text_line(value).upper()
    normalized = _ROMAN_NUMERAL_PATTERN.sub("R", normalized)
    normalized = _NUMBER_PATTERN.sub("#", normalized)
    normalized = re.sub(r"\bSHEET\s+#(?:\s+OF\s+#)?\b", "SHEET #", normalized)
    normalized = re.sub(r"\bPAGE\s+#(?:\s+OF\s+#)?\b", "PAGE #", normalized)
    normalized = re.sub(r"\b第\s*#\s*页\b", "第#页", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" .:-")


def lexical_token_signature(value: str) -> Tuple[str, ...]:
    normalized = normalize_text_line(value).upper()
    tokens = [token for token in _NONWORD_PATTERN.split(normalized) if token]
    stable_tokens = [token for token in tokens if len(token) >= 2 and not token.isdigit()]
    return tuple(stable_tokens[:12])


def line_alnum_density(value: str) -> float:
    normalized = normalize_text_line(value)
    if not normalized:
        return 0.0
    alnum = sum(1 for char in normalized if char.isalnum())
    return alnum / max(1, len(normalized))


def position_band(element: ParsedElement) -> str:
    top_ratio = float(element.top_ratio if element.top_ratio is not None else 1.0)
    bottom_ratio = float(element.bottom_ratio if element.bottom_ratio is not None else 1.0)
    if top_ratio <= 0.22:
        return "top"
    if bottom_ratio <= 0.18:
        return "bottom"
    return "body"


def element_span_ratio(element: ParsedElement) -> float:
    if element.height_ratio is not None:
        return max(0.0, min(1.0, float(element.height_ratio)))
    if element.top_ratio is None or element.bottom_ratio is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - float(element.top_ratio) - float(element.bottom_ratio)))


def center_spanning(element: ParsedElement) -> bool:
    width_ratio = float(element.width_ratio or 0.0)
    if width_ratio >= 0.58:
        return True
    if element.left_ratio is None or element.right_ratio is None:
        return False
    return float(element.left_ratio) <= 0.44 and float(element.right_ratio) >= 0.56


def collect_repetition_candidates(
    elements: Sequence[ParsedElement],
) -> Tuple[Set[str], Set[str], Set[str]]:
    exact_stats: DefaultDict[str, Dict[str, object]] = defaultdict(
        lambda: {"pages": set(), "top": set(), "bottom": set(), "positions": []}
    )
    canonical_stats: DefaultDict[str, Dict[str, object]] = defaultdict(
        lambda: {"pages": set(), "top": set(), "bottom": set(), "positions": [], "tokens": set(), "lengths": []}
    )
    shape_stats: DefaultDict[str, Dict[str, object]] = defaultdict(
        lambda: {"pages": set(), "top": set(), "bottom": set()}
    )

    seen_pages: Set[int] = set()
    for element in elements:
        if element.kind != "text":
            continue
        seen_pages.add(int(element.page_num))
        band = position_band(element)
        for raw_line in str(element.text or "").splitlines():
            line = normalize_text_line(raw_line)
            if not line or len(line) > 140:
                continue
            exact = normalize_text_line(line).upper()
            canonical = canonical_line_signature(line)
            shape = text_shape_signature(line)
            if not canonical or not shape:
                continue
            token_signature = lexical_token_signature(line)
            band_bucket = "top" if band == "top" else "bottom" if band == "bottom" else None
            page_num = int(element.page_num)

            exact_stats[exact]["pages"].add(page_num)
            exact_stats[exact]["positions"].append(float(element.top_ratio if element.top_ratio is not None else 1.0))
            canonical_stats[canonical]["pages"].add(page_num)
            canonical_stats[canonical]["positions"].append(
                float(element.top_ratio if element.top_ratio is not None else 1.0)
            )
            canonical_stats[canonical]["tokens"].add(token_signature)
            canonical_stats[canonical]["lengths"].append(len(line))
            shape_stats[shape]["pages"].add(page_num)

            if band_bucket:
                exact_stats[exact][band_bucket].add(page_num)
                canonical_stats[canonical][band_bucket].add(page_num)
                shape_stats[shape][band_bucket].add(page_num)

    total_pages = max(1, len(seen_pages))
    exact_lines: Set[str] = set()
    canonical_lines: Set[str] = set()
    shapes: Set[str] = set()
    min_repeat_pages = min(total_pages, max(2, int(math.ceil(total_pages * 0.08))))
    min_canonical_pages = min(total_pages, max(3, int(math.ceil(total_pages * 0.10))))

    for line, stats in exact_stats.items():
        pages = len(stats["pages"])
        top_pages = len(stats["top"])
        bottom_pages = len(stats["bottom"])
        positions = list(stats["positions"])
        if pages >= min_repeat_pages and (top_pages >= 2 or bottom_pages >= 2):
            if positions and max(positions) - min(positions) <= 0.06:
                exact_lines.add(line)

    for line, stats in canonical_stats.items():
        pages = len(stats["pages"])
        top_pages = len(stats["top"])
        bottom_pages = len(stats["bottom"])
        positions = list(stats["positions"])
        token_variants = list(stats["tokens"])
        avg_length = (sum(int(item) for item in stats["lengths"]) / max(1, len(stats["lengths"])))
        if pages < min_canonical_pages:
            continue
        if top_pages < 2 and bottom_pages < 2:
            continue
        if positions and max(positions) - min(positions) > 0.08:
            continue
        if avg_length > 72 and len(token_variants) > 3:
            continue
        canonical_lines.add(line)

    for shape, stats in shape_stats.items():
        pages = len(stats["pages"])
        top_pages = len(stats["top"])
        bottom_pages = len(stats["bottom"])
        if pages >= max(3, min_repeat_pages) and (top_pages >= 2 or bottom_pages >= 2):
            shapes.add(shape)

    return exact_lines, canonical_lines, shapes


def infer_document_split_ratio(elements: Sequence[ParsedElement]) -> Optional[float]:
    pages: DefaultDict[int, List[ParsedElement]] = defaultdict(list)
    for element in elements:
        if element.kind == "text":
            pages[int(element.page_num)].append(element)

    page_splits: List[float] = []
    all_centers: List[float] = []
    for page_elements in pages.values():
        narrow_centers = sorted(
            float(element.center_ratio)
            for element in page_elements
            if element.center_ratio is not None
            and not center_spanning(element)
            and 0.18 <= float(element.center_ratio) <= 0.82
        )
        all_centers.extend(narrow_centers)
        if len(narrow_centers) < 4:
            continue
        best_gap = 0.0
        best_split = None
        for left, right in zip(narrow_centers, narrow_centers[1:]):
            gap = right - left
            split = (left + right) / 2.0
            if gap > best_gap and 0.34 <= split <= 0.66:
                best_gap = gap
                best_split = split
        if best_split is not None and best_gap >= 0.08:
            page_splits.append(best_split)

    if len(page_splits) >= 3:
        return float(median(page_splits))
    if len(all_centers) < 6:
        return None
    all_centers = sorted(all_centers)
    best_gap = 0.0
    best_split = None
    for left, right in zip(all_centers, all_centers[1:]):
        gap = right - left
        split = (left + right) / 2.0
        if gap > best_gap and 0.34 <= split <= 0.66:
            best_gap = gap
            best_split = split
    if best_split is None or best_gap < 0.08:
        return None
    return best_split


def blended_split_ratio(local_split: Optional[float], document_split: Optional[float]) -> Optional[float]:
    if local_split is None:
        return document_split
    if document_split is None:
        return local_split
    if abs(local_split - document_split) <= 0.08:
        return round((local_split * 0.65) + (document_split * 0.35), 4)
    return local_split


def table_alignment_similarity(previous: ParsedElement, current: ParsedElement) -> float:
    ratios: List[float] = []
    for left_attr, right_attr in [("left_ratio", "right_ratio"), ("center_ratio", "center_ratio")]:
        prev_value = getattr(previous, left_attr)
        curr_value = getattr(current, right_attr)
        if prev_value is None or curr_value is None:
            continue
        ratios.append(max(0.0, 1.0 - abs(float(prev_value) - float(curr_value)) / 0.20))
    if not ratios:
        return 0.0
    return round(sum(ratios) / len(ratios), 4)


def table_edge_continuity(previous: ParsedElement, current: ParsedElement) -> float:
    previous_bottom = float(previous.bottom_ratio if previous.bottom_ratio is not None else 1.0)
    current_top = float(current.top_ratio if current.top_ratio is not None else 1.0)
    score = 0.0
    if previous_bottom <= 0.18:
        score += 0.5
    if current_top <= 0.18:
        score += 0.5
    return round(score, 4)


def table_header_schema(rows: Sequence[Sequence[str]], header_row_count: int) -> Tuple[Tuple[str, ...], ...]:
    limit = max(0, min(int(header_row_count or 0), len(rows)))
    if limit <= 0 and rows:
        limit = 1
    signature: List[Tuple[str, ...]] = []
    for index in range(limit):
        row = rows[index]
        signature.append(tuple(normalize_text_line(cell).upper() for cell in row))
    return tuple(signature)


def table_schema_similarity(
    previous_rows: Sequence[Sequence[str]],
    current_rows: Sequence[Sequence[str]],
    previous_header_rows: int,
    current_header_rows: int,
) -> float:
    previous_schema = table_header_schema(previous_rows, previous_header_rows)
    current_schema = table_header_schema(current_rows, current_header_rows)
    if previous_schema and current_schema and previous_schema == current_schema:
        return 1.0

    previous_width = max((len(row) for row in previous_rows), default=0)
    current_width = max((len(row) for row in current_rows), default=0)
    if previous_width <= 0 or current_width <= 0:
        return 0.0

    width_similarity = max(0.0, 1.0 - (abs(previous_width - current_width) / max(previous_width, current_width)))
    if not previous_schema or not current_schema:
        return round(width_similarity, 4)

    compare_limit = min(len(previous_schema), len(current_schema))
    matched = 0
    for index in range(compare_limit):
        previous_row = previous_schema[index]
        current_row = current_schema[index]
        if previous_row == current_row:
            matched += 1
            continue
        previous_tokens = {token for token in previous_row if token}
        current_tokens = {token for token in current_row if token}
        if previous_tokens and current_tokens:
            overlap = len(previous_tokens & current_tokens) / max(len(previous_tokens | current_tokens), 1)
            if overlap >= 0.6:
                matched += 0.6
    row_similarity = matched / max(compare_limit, 1)
    return round((width_similarity * 0.4) + (row_similarity * 0.6), 4)


__all__ = [
    "blended_split_ratio",
    "canonical_line_signature",
    "center_spanning",
    "collect_repetition_candidates",
    "element_span_ratio",
    "infer_document_split_ratio",
    "line_alnum_density",
    "position_band",
    "table_alignment_similarity",
    "table_edge_continuity",
    "table_schema_similarity",
    "text_shape_signature",
]

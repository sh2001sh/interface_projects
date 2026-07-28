from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from protocol_extractor import enrich_protocol_metadata


HEADER_TOP_RATIO = 0.12
FOOTER_BOTTOM_RATIO = 0.9
LINE_CLUSTER_TOLERANCE = 3.0
MIN_COLUMN_LINES = 4


@dataclass
class TextLine:
    text: str
    top: float
    bottom: float
    x0: float
    x1: float

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize_table_rows(table: list) -> list:
    rows = []
    max_cols = 0
    for row in table or []:
        cleaned = [_normalize_cell(cell) for cell in (row or [])]
        if any(cleaned):
            rows.append(cleaned)
            max_cols = max(max_cols, len(cleaned))

    if max_cols == 0:
        return []

    normalized = []
    for row in rows:
        if len(row) < max_cols:
            normalized.append(row + [""] * (max_cols - len(row)))
        else:
            normalized.append(row[:max_cols])
    return normalized


def _row_signature(row: list) -> str:
    return "|".join([_normalize_cell(cell).lower() for cell in row if _normalize_cell(cell)])


def _row_blank_ratio(row: list) -> float:
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


def _row_shape_similarity(left: list, right: list) -> float:
    size = max(len(left), len(right), 1)
    score = 0
    for idx in range(size):
        left_shape = _cell_shape(left[idx] if idx < len(left) else "")
        right_shape = _cell_shape(right[idx] if idx < len(right) else "")
        if left_shape == right_shape:
            score += 1
    return score / size


def _looks_like_header_row(row: list) -> bool:
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


def _format_table_to_text(table: list) -> str:
    if not table:
        return ""
    return "\n".join(" | ".join(str(cell) if cell is not None else "" for cell in row) for row in table)


def _parse_text_tables(text: str, page_num: int) -> list:
    tables = []

    if "WORD NUMBER:" in text or "WORD MAP" in text:
        word_maps = re.split(r"WORD NUMBER:", text)
        for wm in word_maps[1:]:
            lines = wm.strip().split("\n")
            if len(lines) < 3:
                continue

            word_num_match = re.match(r"\s*(J[\d.]+[IE]?)", lines[0])
            word_num = word_num_match.group(1) if word_num_match else "Unknown"

            title = ""
            for line in lines:
                if "WORD TITLE:" not in line:
                    continue
                title_match = re.search(r"WORD TITLE:\s*(.+)", line)
                if title_match:
                    title = title_match.group(1).strip()
                break

            table_rows = []
            for line in lines:
                if line.count(":") >= 2 and not line.strip().startswith("---"):
                    cells = [c.strip() for c in line.split(":") if c.strip()]
                    if cells:
                        table_rows.append(cells)

            if table_rows:
                tables.append(
                    {
                        "page_num": page_num,
                        "content": f"Word Map: {word_num}\nTitle: {title}\n" + _format_table_to_text(table_rows),
                        "type": "table",
                        "metadata": {
                            "word_number": word_num,
                            "word_title": title,
                            "row_count": len(table_rows),
                            "col_count": max(len(r) for r in table_rows) if table_rows else 0,
                            "table_type": "word_map",
                        },
                    }
                )

    bit_pattern = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*[\|\t]\s*(\d+)\s*[-–~]\s*(\d+)\s*[\|\t]\s*(.+)$", re.MULTILINE)
    bit_matches = bit_pattern.findall(text)
    if bit_matches:
        rows = []
        for field_name, bit_start, bit_end, meaning in bit_matches:
            rows.append([field_name, f"{bit_start}-{bit_end}", meaning.strip()])
        if rows:
            tables.append(
                {
                    "page_num": page_num,
                    "content": "Bit Field Table\n" + _format_table_to_text(rows),
                    "type": "table",
                    "metadata": {
                        "row_count": len(rows),
                        "col_count": 3,
                        "table_type": "bit_field",
                    },
                }
            )

    j_msg_pattern = re.compile(r"(J[\d.]+[IE]?)\s*[\|\t:]\s*(.+?)(?:\s*[\|\t]\s*(.+))?$", re.MULTILINE)
    j_matches = j_msg_pattern.findall(text)
    if len(j_matches) >= 3:
        rows = []
        for match in j_matches[:20]:
            msg_code = match[0]
            desc = match[1].strip() if len(match) > 1 else ""
            extra = match[2].strip() if len(match) > 2 else ""
            rows.append([msg_code, desc, extra] if extra else [msg_code, desc])
        if rows:
            tables.append(
                {
                    "page_num": page_num,
                    "content": "J-Message Format\n" + _format_table_to_text(rows),
                    "type": "table",
                    "metadata": {
                        "row_count": len(rows),
                        "col_count": max(len(r) for r in rows),
                        "table_type": "j_message",
                    },
                }
            )

    return tables


def _cluster_words_to_lines(words: list[dict[str, Any]]) -> list[TextLine]:
    ordered = sorted(words, key=lambda item: (float(item["top"]), float(item["x0"])))
    grouped: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_top = 0.0

    for word in ordered:
        word_top = float(word["top"])
        if not current:
            current = [word]
            current_top = word_top
            continue
        if abs(word_top - current_top) <= LINE_CLUSTER_TOLERANCE:
            current.append(word)
            current_top = (current_top * (len(current) - 1) + word_top) / len(current)
        else:
            grouped.append(current)
            current = [word]
            current_top = word_top

    if current:
        grouped.append(current)

    lines = []
    for group in grouped:
        sorted_group = sorted(group, key=lambda item: float(item["x0"]))
        text = " ".join(item["text"] for item in sorted_group).strip()
        if not text:
            continue
        lines.append(
            TextLine(
                text=text,
                top=min(float(item["top"]) for item in sorted_group),
                bottom=max(float(item["bottom"]) for item in sorted_group),
                x0=min(float(item["x0"]) for item in sorted_group),
                x1=max(float(item["x1"]) for item in sorted_group),
            )
        )
    return lines


def _normalize_line_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


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


def _is_header_line(line: TextLine, page_width: float, page_height: float) -> bool:
    text = _normalize_line_text(line.text)
    lower = text.lower()
    if not text:
        return False
    if line.top > page_height * HEADER_TOP_RATIO:
        return False
    if lower == "mil-std-6016d":
        return True
    if lower in {"inch-pound", "superseding"}:
        return True
    if re.fullmatch(r"section\s+\d+[a-z]?", lower):
        return True
    if re.fullmatch(r"appendix\s+[a-z0-9]+", lower):
        return True
    if "this page is intentionally left blank" in lower:
        return True
    if _is_page_marker(text):
        return True
    if line.width <= page_width * 0.35 and re.fullmatch(r"[A-Za-z0-9.\-()/ ]+", text) and text == text.upper():
        return True
    return False


def _is_footer_line(line: TextLine, page_height: float) -> bool:
    text = _normalize_line_text(line.text)
    lower = text.lower()
    if not text:
        return False
    if line.bottom < page_height * FOOTER_BOTTOM_RATIO:
        return False
    if lower == "mil-std-6016d":
        return True
    if "this page is intentionally left blank" in lower:
        return True
    if _is_page_marker(text):
        return True
    return False


def _clean_page_lines(lines: list[TextLine], page_width: float, page_height: float) -> list[TextLine]:
    cleaned = []
    for line in lines:
        if _is_header_line(line, page_width, page_height):
            continue
        if _is_footer_line(line, page_height):
            continue
        cleaned.append(line)
    return cleaned


def _classify_line(line: TextLine, page_width: float) -> str:
    if line.width >= page_width * 0.72:
        return "full"
    if line.x0 <= page_width * 0.45 and line.x1 >= page_width * 0.55:
        return "full"
    if line.center_x < page_width * 0.48 and line.x1 <= page_width * 0.64:
        return "left"
    if line.center_x > page_width * 0.52 and line.x0 >= page_width * 0.36:
        return "right"
    return "full"


def _flush_column_band(band: list[tuple[str, TextLine]]) -> list[TextLine]:
    if not band:
        return []
    left = sorted([line for side, line in band if side == "left"], key=lambda item: (item.top, item.x0))
    right = sorted([line for side, line in band if side == "right"], key=lambda item: (item.top, item.x0))
    if len(left) >= MIN_COLUMN_LINES and len(right) >= MIN_COLUMN_LINES:
        return left + right
    return sorted([line for _, line in band], key=lambda item: (item.top, item.x0))


def _reorder_lines_for_layout(lines: list[TextLine], page_width: float) -> list[TextLine]:
    ordered: list[TextLine] = []
    band: list[tuple[str, TextLine]] = []
    for line in sorted(lines, key=lambda item: (item.top, item.x0)):
        side = _classify_line(line, page_width)
        if side == "full":
            ordered.extend(_flush_column_band(band))
            band = []
            ordered.append(line)
            continue
        band.append((side, line))
    ordered.extend(_flush_column_band(band))
    return ordered


def _line_overlaps_table(line: TextLine, table_bboxes: list[tuple[float, float, float, float]]) -> bool:
    for x0, top, x1, bottom in table_bboxes:
        vertical_overlap = min(line.bottom, bottom) - max(line.top, top)
        if vertical_overlap <= 0:
            continue
        horizontal_overlap = min(line.x1, x1) - max(line.x0, x0)
        if horizontal_overlap > 0:
            return True
    return False


def _extract_sheet_info(title: str) -> tuple[Optional[int], Optional[int], str]:
    match = re.search(r"\(sheet\s+(\d+)\s+of\s+(\d+)\)", title, re.IGNORECASE)
    if not match:
        return None, None, re.sub(r"\s+", " ", title).strip().lower()
    index = int(match.group(1))
    total = int(match.group(2))
    base = re.sub(r"\(sheet\s+\d+\s+of\s+\d+\)", "", title, flags=re.IGNORECASE)
    return index, total, re.sub(r"\s+", " ", base).strip().lower()


def _find_table_title(lines: list[TextLine], table_top: float) -> str:
    candidates = [line for line in lines if line.bottom <= table_top + 2]
    if not candidates:
        return ""
    candidates = sorted(candidates, key=lambda item: item.bottom, reverse=True)[:4]
    for line in candidates:
        text = _normalize_line_text(line.text)
        lower = text.lower()
        if "sheet" in lower or lower.startswith("table ") or lower.startswith("field coding") or lower.startswith("figure "):
            return text
    return _normalize_line_text(candidates[0].text)


def _extract_page_tables(page, page_num: int, cleaned_lines: list[TextLine]) -> tuple[list, list[tuple[float, float, float, float]]]:
    table_blocks = []
    table_bboxes: list[tuple[float, float, float, float]] = []
    page_height = float(page.height or 1)

    for table_idx, table_obj in enumerate(page.find_tables() or []):
        rows = _normalize_table_rows(table_obj.extract() if hasattr(table_obj, "extract") else [])
        if not rows:
            continue
        bbox = getattr(table_obj, "bbox", None) or (0.0, 0.0, float(page.width or 0), page_height)
        x0, top, x1, bottom = [float(value) for value in bbox]
        title = _find_table_title(cleaned_lines, top)
        sheet_index, sheet_total, title_base = _extract_sheet_info(title)
        table_bboxes.append((x0, top, x1, bottom))
        table_blocks.append(
            {
                "page_num": page_num,
                "content": _format_table_to_text(rows),
                "type": "table",
                "metadata": {
                    "row_count": len(rows),
                    "col_count": max(len(r) for r in rows),
                    "near_top": top <= page_height * 0.24,
                    "near_bottom": bottom >= page_height * 0.7,
                    "source_pages": [page_num],
                    "table_index": table_idx,
                    "last_page_num": page_num,
                    "table_bbox": [x0, top, x1, bottom],
                    "table_title": title,
                    "table_title_base": title_base,
                    "sheet_index": sheet_index,
                    "sheet_total": sheet_total,
                },
                "_rows": rows,
                "_order": (page_num, 0, table_idx),
            }
        )

    return table_blocks, table_bboxes


def _should_merge_cross_page_table(previous: dict, current: dict) -> tuple[bool, bool]:
    previous_meta = previous.get("metadata", {})
    current_meta = current.get("metadata", {})

    if current["page_num"] != previous_meta.get("last_page_num", previous["page_num"]) + 1:
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


def _merge_cross_page_tables(table_blocks: list) -> list:
    if not table_blocks:
        return []

    merged = []
    for current in table_blocks:
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

                source_pages = merged[-1]["metadata"].setdefault("source_pages", [merged[-1]["page_num"]])
                for page_num in current["metadata"].get("source_pages", [current["page_num"]]):
                    if page_num not in source_pages:
                        source_pages.append(page_num)

                merged[-1]["content"] = _format_table_to_text(merged[-1]["_rows"])
                merged[-1]["metadata"]["row_count"] = len(merged[-1]["_rows"])
                merged[-1]["metadata"]["col_count"] = max(
                    int(merged[-1]["metadata"].get("col_count", 0)),
                    int(current["metadata"].get("col_count", 0)),
                )
                merged[-1]["metadata"]["cross_page_merged"] = len(source_pages) > 1
                merged[-1]["metadata"]["last_page_num"] = max(source_pages)
                merged[-1]["metadata"]["near_bottom"] = bool(current["metadata"].get("near_bottom"))
                merged[-1]["metadata"]["sheet_index"] = current["metadata"].get("sheet_index")
                continue
        merged.append(current)

    for table_block in merged:
        table_block.pop("_rows", None)
        table_block["metadata"].pop("last_page_num", None)
    return merged


def _emit_progress(progress_callback: Optional[Callable[[dict], None]], payload: dict) -> None:
    if not progress_callback:
        return
    progress_callback(payload)


def process_pdf_with_pages(
    file_path: str,
    enable_llm_postprocess: bool = False,
    page_batch_size: int = 100,
    progress_callback: Optional[Callable[[dict], None]] = None,
    progress_start: float = 0.0,
    progress_end: float = 100.0,
) -> list:
    """Parse PDF pages with header/footer filtering, mixed column reorder, and cross-page table merge."""
    import pdfplumber

    text_blocks = []
    table_blocks = []
    with pdfplumber.open(file_path) as pdf:
        total_pages = len(pdf.pages)
        resolved_batch_size = max(1, int(page_batch_size or 100))
        for batch_start in range(0, total_pages, resolved_batch_size):
            batch_end = min(total_pages, batch_start + resolved_batch_size)
            for page_index in range(batch_start, batch_end):
                page_num = page_index + 1
                page = pdf.pages[page_index]
                words = page.extract_words(use_text_flow=False) or []
                lines = _cluster_words_to_lines(words)
                cleaned_lines = _clean_page_lines(lines, float(page.width or 1), float(page.height or 1))
                page_table_blocks, table_bboxes = _extract_page_tables(page, page_num, cleaned_lines)
                base_table_count = len(page_table_blocks)
                table_blocks.extend(page_table_blocks)

                text_for_table_detection = "\n".join(line.text for line in cleaned_lines)
                for idx, table in enumerate(_parse_text_tables(text_for_table_detection, page_num)):
                    page_table_blocks.append(
                        {
                            "page_num": page_num,
                            "content": table["content"],
                            "type": "table",
                            "metadata": table.get("metadata", {}),
                            "_order": (page_num, 0, base_table_count + idx),
                        }
                    )
                if len(page_table_blocks) > base_table_count:
                    table_blocks.extend(page_table_blocks[base_table_count:])

                non_table_lines = [line for line in cleaned_lines if not _line_overlaps_table(line, table_bboxes)]
                ordered_lines = _reorder_lines_for_layout(non_table_lines, float(page.width or 1))
                text = "\n".join(line.text for line in ordered_lines).strip()
                if text:
                    text_blocks.append(
                        {
                            "page_num": page_num,
                            "content": text,
                            "type": "text",
                            "metadata": {},
                            "_order": (page_num, 1, 0),
                        }
                    )

            batch_progress = progress_start + (batch_end / max(total_pages, 1)) * (progress_end - progress_start)
            _emit_progress(
                progress_callback,
                {
                    "stage": "processing_pdf_pages",
                    "message": f"已解析 PDF 页 {batch_start + 1}-{batch_end} / {total_pages}",
                    "progress": batch_progress,
                    "processed_pages": batch_end,
                    "total_pages": total_pages,
                },
            )

    merged_table_blocks = _merge_cross_page_tables(table_blocks)
    blocks = merged_table_blocks + text_blocks
    blocks.sort(key=lambda item: item.get("_order", (item["page_num"], 1, 0)))

    enriched_blocks = []
    for block in blocks:
        normalized_block = {
            "page_num": block["page_num"],
            "content": block["content"],
            "type": block["type"],
            "metadata": block.get("metadata", {}),
        }
        enriched_blocks.append(enrich_protocol_metadata(normalized_block, enable_llm_postprocess))
    return enriched_blocks

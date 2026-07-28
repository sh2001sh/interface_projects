from __future__ import annotations

import re
from typing import Any, Optional


WIDE_TEXT_LAYOUT_SPLIT_PATTERN = re.compile(r"^(?P<left>.+?\S)\s{6,}(?P<right>\S.+)$")
WIDE_TEXT_ANCHOR_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.()/-]{2,}$")
WIDE_TEXT_SECTION_MARKER_PATTERN = re.compile(
    r"^(?=[A-Z0-9./()\-]{3,28}(?:\s*\(CONTINUED\))?$)(?=[A-Z0-9./()\-]*\d)[A-Z0-9./()\-]{3,28}(?:\s*\(CONTINUED\))?$",
    flags=re.IGNORECASE,
)
WIDE_TEXT_HEADER_PATTERN = re.compile(
    r"^(?:MIL-STD-[A-Z0-9.\-]+|J13\.2 MESSAGE SUMMARY(?: \(CONTINUED\))?|DATA ELEMENT SUMMARY(?: \(CONTINUED\))?)$",
    flags=re.IGNORECASE,
)
WIDE_TEXT_FOOTER_PATTERN = re.compile(
    r"^(?:\d+-\d+|\d+\.\d+-\d+|PAGE\s*\d+(?:\s*OF\s*\d+)?)$",
    flags=re.IGNORECASE,
)


def normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_line_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def infer_layout_split_index(lines: list[str]) -> Optional[int]:
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


def split_layout_lines_by_index(lines: list[str], split_index: int) -> tuple[list[str], list[str]]:
    left_lines: list[str] = []
    right_lines: list[str] = []
    for raw_line in lines:
        if not raw_line.strip():
            continue
        line = raw_line.rstrip("\n")
        left = normalize_line_text(line[:split_index])
        right = normalize_line_text(line[split_index:])
        if left:
            left_lines.append(left)
        if right:
            right_lines.append(right)
    return left_lines, right_lines


def pick_layout_anchor(text: str) -> str:
    for raw_line in str(text or "").splitlines():
        normalized = normalize_line_text(raw_line)
        if not normalized:
            continue
        for token in normalized.split():
            if WIDE_TEXT_ANCHOR_PATTERN.fullmatch(token):
                return token
    return ""


def find_layout_anchor_index(lines: list[str], anchor: str) -> int:
    if not anchor:
        return 0
    anchor_normalized = normalize_line_text(anchor)
    for index, line in enumerate(lines):
        if anchor_normalized and anchor_normalized in normalize_line_text(line):
            return index
    return 0


def filter_layout_lines(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if not WIDE_TEXT_HEADER_PATTERN.fullmatch(normalize_line_text(line))
        and not WIDE_TEXT_FOOTER_PATTERN.fullmatch(normalize_line_text(line))
    ]

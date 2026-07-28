from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from database.models import Block


PROTOCOL_ANCHOR_PATTERN = re.compile(r"\b(J\d+\.\d+[A-Z]?\d*)\b", flags=re.IGNORECASE)
FIELD_NAME_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_./\-]{2,}\b")
MAPPING_PAIR_PATTERN = re.compile(r"-?\d+\s*(?:=|->|→)\s*[A-Za-z_][A-Za-z0-9_./\-]*")
FORMULA_HINT_PATTERN = re.compile(r"(?:formula|公式|convert|转换|resolution|分辨率|value\s*[\*\/\+\-])", flags=re.IGNORECASE)
RULE_EVIDENCE_PATTERN = re.compile(
    r"(?:formula|公式|mapping|映射|range|范围|resolution|分辨率|bit|位|value\s*[\*\/\+\-]|->|→|=)",
    flags=re.IGNORECASE,
)
HEADER_FOOTER_PATTERN = re.compile(
    r"(?:^table\s+\d|^figure\s+\d|^note\b|^附注|continued|page\s+\d+|保密|密级|页眉|页脚)",
    flags=re.IGNORECASE,
)
SEPARATOR_LINE_PATTERN = re.compile(r"^[=\-_.|/\\\s]{4,}$")
TABLE_REF_PATTERN = re.compile(r"\bTABLE\s+([A-Z0-9.\-]*\d[A-Z0-9.\-]*)", flags=re.IGNORECASE)
WORD_NUMBER_PATTERN = re.compile(r"\bWORD\s+NUMBER\s*:?\s*([A-Z]\d+\.\d+[A-Z]?\d*)", flags=re.IGNORECASE)
WORD_TITLE_PATTERN = re.compile(r"\b(?:WORD\s+TITLE|TITLE)\s*:?\s*([A-Z][A-Z0-9 /_\-]{3,80})", flags=re.IGNORECASE)
FIELD_CODING_PATTERN = re.compile(r"\bFIELD\s+CODING\s+FOR\s+([A-Z]\d+\.\d+[A-Z]?\d*)", flags=re.IGNORECASE)
FIELD_DESCRIPTION_PATTERN = re.compile(r"\bWORD\s+DESCRIPTION\b|\bFIELD\s+CODING\b|\bDFI\b|\bDUI\b", flags=re.IGNORECASE)
WORD_MAP_PATTERN = re.compile(r"\bWORD\s+MAP\b", flags=re.IGNORECASE)
TABLE_SHEET_PATTERN = re.compile(r"\bSHEET\s+\d+\s+OF\s+\d+\b", flags=re.IGNORECASE)
TABLE_OF_CONTENTS_PATTERN = re.compile(r"\b(?:shall|transaction|stimulus|record of|preparation for transmission)\b", flags=re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return len(text) // 3


def get_block_content(block: Block) -> str:
    content = block.cleaned_content or block.content
    return content.strip() if content else ""


def merge_block_contents(blocks: Sequence[Block]) -> Tuple[str, int]:
    contents: List[str] = []
    total_tokens = 0
    for block in blocks:
        content = get_block_content(block)
        if content:
            contents.append(content)
            total_tokens += estimate_tokens(content)
    return "\n\n".join(contents), total_tokens


def extract_protocol_anchor(content: str, metadata: Dict[str, Any]) -> str:
    if isinstance(metadata, dict):
        for key in ("protocol", "word_number"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                match = PROTOCOL_ANCHOR_PATTERN.search(value.strip())
                if match:
                    return match.group(1).upper()
    match = PROTOCOL_ANCHOR_PATTERN.search(content or "")
    return match.group(1).upper() if match else ""


def normalize_protocol_family(anchor: str) -> str:
    value = str(anchor or "").strip().upper()
    if not value:
        return ""
    match = re.fullmatch(r"(J\d+\.\d+[A-Z]?)(\d+)", value)
    if not match:
        return value
    prefix, suffix = match.groups()
    return prefix if len(suffix) == 1 and prefix[-1:].isalpha() else value


def extract_section_keys(content: str, metadata: Dict[str, Any], protocol_anchor: str = "") -> Set[str]:
    text = str(content or "")
    keys: Set[str] = set()

    for pattern, prefix in [
        (TABLE_REF_PATTERN, "table"),
        (WORD_NUMBER_PATTERN, "word"),
        (FIELD_CODING_PATTERN, "field_coding"),
    ]:
        for match in pattern.findall(text):
            normalized = str(match or "").strip().upper()
            if normalized:
                keys.add(f"{prefix}:{normalized}")
                family = normalize_protocol_family(normalized)
                if family and family != normalized:
                    keys.add(f"family:{family}")

    for match in WORD_TITLE_PATTERN.findall(text):
        normalized = re.sub(r"\s+", " ", str(match or "").strip().upper())
        if normalized:
            keys.add(f"title:{normalized}")

    if protocol_anchor:
        keys.add(f"anchor:{protocol_anchor}")
        family = normalize_protocol_family(protocol_anchor)
        if family and family != protocol_anchor:
            keys.add(f"family:{family}")

    return keys


def extract_structure_tags(content: str, block_type: str, metadata: Dict[str, Any]) -> Set[str]:
    text = str(content or "")
    upper_text = text.upper()
    tags: Set[str] = set()

    tags.add("table" if block_type == "table" else "textual")

    if WORD_MAP_PATTERN.search(upper_text):
        tags.add("word_map")
    if FIELD_DESCRIPTION_PATTERN.search(upper_text):
        tags.add("field_definition")
    if TABLE_SHEET_PATTERN.search(upper_text):
        tags.add("table_sheet")
    if TABLE_REF_PATTERN.search(upper_text):
        tags.add("table_reference")
    if TABLE_OF_CONTENTS_PATTERN.search(upper_text):
        tags.add("narrative")

    protocol_fields = metadata.get("protocol_fields") if isinstance(metadata, dict) else None
    if isinstance(protocol_fields, list) and protocol_fields:
        tags.add("field_rich")

    if "word_map" in tags and "field_definition" in tags:
        tags.add("message_layout")

    return tags


def _is_plausible_field_name(token: str) -> bool:
    cleaned = str(token or "").strip().upper()
    if len(cleaned) < 3:
        return False
    if cleaned in {"FIELD", "FIELDS", "VALUE", "VALUES", "TABLE", "TABLES"}:
        return False
    if "_" in cleaned:
        return True
    if PROTOCOL_ANCHOR_PATTERN.fullmatch(cleaned):
        return True
    return bool(re.fullmatch(r"[A-Z0-9./\-]{4,}", cleaned))


def extract_field_names(content: str, metadata: Dict[str, Any]) -> Set[str]:
    field_names: Set[str] = set()
    protocol_fields = metadata.get("protocol_fields") if isinstance(metadata, dict) else None
    if isinstance(protocol_fields, list):
        for field in protocol_fields:
            if not isinstance(field, dict):
                continue
            raw_name = str(field.get("field_name", "")).strip().upper()
            if _is_plausible_field_name(raw_name):
                field_names.add(raw_name)
    for token in FIELD_NAME_PATTERN.findall((content or "").upper()):
        if _is_plausible_field_name(token):
            field_names.add(token)
    return field_names


def count_mapping_pairs(text: str) -> int:
    if not text:
        return 0
    return len(MAPPING_PAIR_PATTERN.findall(text))


def _iter_nonempty_lines(text: str) -> List[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def estimate_noise_penalty(content: str, block_type: str, metadata: Dict[str, Any]) -> float:
    text = str(content or "").strip()
    if not text:
        return 18.0

    lines = _iter_nonempty_lines(text)
    line_count = len(lines) or 1
    short_lines = sum(1 for line in lines if len(line) <= 6)
    separator_lines = sum(1 for line in lines if SEPARATOR_LINE_PATTERN.fullmatch(line))
    header_footer_hits = sum(1 for line in lines if HEADER_FOOTER_PATTERN.search(line))
    punctuation_chars = sum(1 for ch in text if not ch.isalnum() and not ch.isspace())
    punctuation_ratio = punctuation_chars / max(len(text), 1)
    alpha_numeric_ratio = sum(1 for ch in text if ch.isalnum()) / max(len(text), 1)

    penalty = 0.0
    if block_type in {"image", "figure"}:
        penalty += 6.0
    if line_count >= 4 and short_lines / line_count >= 0.55:
        penalty += 5.0
    if separator_lines:
        penalty += min(4.0, separator_lines * 1.5)
    if header_footer_hits:
        penalty += min(6.0, header_footer_hits * 2.0)
    if punctuation_ratio >= 0.32:
        penalty += 3.0
    if alpha_numeric_ratio <= 0.38:
        penalty += 2.0
    if len(text) < 36 and not RULE_EVIDENCE_PATTERN.search(text):
        penalty += 4.0
    if metadata.get("protocol_fields") in (None, []) and count_mapping_pairs(text) == 0 and not FORMULA_HINT_PATTERN.search(text):
        penalty += 2.0

    return round(penalty, 4)


def estimate_evidence_score(
    content: str,
    block_type: str,
    protocol_anchor: str,
    field_names: Set[str],
    formula_count: int,
    mapping_pair_count: int,
    bit_coverage_count: int,
    range_coverage_count: int,
) -> float:
    score = 0.0
    if protocol_anchor:
        score += 12.0
    score += min(18.0, len(field_names) * 1.8)
    score += min(16.0, formula_count * 3.0)
    score += min(16.0, mapping_pair_count * 2.0)
    score += min(10.0, (bit_coverage_count + range_coverage_count) * 1.5)
    if RULE_EVIDENCE_PATTERN.search(content):
        score += 6.0
    if block_type == "table" and (mapping_pair_count > 0 or formula_count > 0):
        score += 4.0
    return round(score, 4)


def determine_block_semantic_type(blocks: Sequence[Block]) -> str:
    if not blocks:
        return "unknown"
    block_types = [b.block_type for b in blocks]
    metadata_list = [b.metadata if isinstance(b.metadata, dict) else {} for b in blocks]

    if "table" in block_types:
        joined_content = " ".join(get_block_content(b) for b in blocks)
        if WORD_MAP_PATTERN.search(joined_content) or FIELD_DESCRIPTION_PATTERN.search(joined_content):
            return "field_definition"
        return "table_data"
    if "code" in block_types:
        return "code_example"

    all_content = " ".join(get_block_content(b) for b in blocks)
    all_content_lower = all_content.lower()
    protocol_field_count = sum(
        len(metadata.get("protocol_fields", []))
        for metadata in metadata_list
        if isinstance(metadata.get("protocol_fields"), list)
    )

    if WORD_MAP_PATTERN.search(all_content) or FIELD_DESCRIPTION_PATTERN.search(all_content):
        return "field_definition"
    if protocol_field_count >= max(1, len(blocks)):
        return "field_definition"

    field_keywords = ["字段", "field", "位宽", "bit", "范围", "range", "单位", "unit"]
    if any(kw in all_content_lower for kw in field_keywords):
        return "field_definition"

    conversion_keywords = ["公式", "formula", "计算", "calculate", "转换", "convert", "映射", "map"]
    if any(kw in all_content_lower for kw in conversion_keywords):
        return "conversion_rule"

    protocol_keywords = ["协议", "protocol", "概述", "overview", "用途", "purpose", "介绍", "introduction"]
    if any(kw in all_content_lower for kw in protocol_keywords):
        return "protocol_description"

    return "general_content"


@dataclass
class MergeContext:
    shared_section_keys: Set[str]
    shared_table_keys: Set[str]
    shared_fields: Set[str]
    same_message_layout: bool
    same_table_series: bool
    page_gap: int
    token_pressure: float
    evidence_gap: float
    structure_transition: str


def build_merge_context(
    current_group: Sequence[Dict[str, Any]],
    next_feature: Dict[str, Any],
    max_token_size: int,
) -> MergeContext:
    prev_feature = current_group[-1]
    prev_section_keys = set(prev_feature.get("section_keys", set()))
    next_section_keys = set(next_feature.get("section_keys", set()))
    shared_section_keys = prev_section_keys & next_section_keys
    prev_table_keys = {item for item in prev_section_keys if item.startswith("table:")}
    next_table_keys = {item for item in next_section_keys if item.startswith("table:")}
    shared_table_keys = prev_table_keys & next_table_keys
    prev_structure_tags = set(prev_feature.get("structure_tags", set()))
    next_structure_tags = set(next_feature.get("structure_tags", set()))
    page_gap = max(0, int(next_feature["page_num"]) - int(prev_feature["page_num"]))
    same_message_layout = bool({"message_layout", "word_map", "field_definition"} & prev_structure_tags) and bool(
        {"message_layout", "word_map", "field_definition"} & next_structure_tags
    )
    same_table_series = bool(shared_table_keys) and "table_reference" in prev_structure_tags and "table_reference" in next_structure_tags
    shared_fields = set(prev_feature.get("field_names", set())) & set(next_feature.get("field_names", set()))
    projected_tokens = sum(int(item["token_count"]) for item in current_group) + int(next_feature["token_count"])
    token_pressure = projected_tokens / max(int(max_token_size), 1)
    evidence_gap = abs(
        float(prev_feature.get("evidence_score", 0.0) or 0.0)
        - float(next_feature.get("evidence_score", 0.0) or 0.0)
    )
    if prev_feature["block_type"] == next_feature["block_type"]:
        structure_transition = "same"
    elif {"table", "code"} & {prev_feature["block_type"], next_feature["block_type"]}:
        structure_transition = "hard"
    else:
        structure_transition = "soft"
    return MergeContext(
        shared_section_keys=shared_section_keys,
        shared_table_keys=shared_table_keys,
        shared_fields=shared_fields,
        same_message_layout=same_message_layout,
        same_table_series=same_table_series,
        page_gap=page_gap,
        token_pressure=token_pressure,
        evidence_gap=evidence_gap,
        structure_transition=structure_transition,
    )


__all__ = [
    "FIELD_DESCRIPTION_PATTERN",
    "FORMULA_HINT_PATTERN",
    "HEADER_FOOTER_PATTERN",
    "MAPPING_PAIR_PATTERN",
    "MergeContext",
    "PROTOCOL_ANCHOR_PATTERN",
    "RULE_EVIDENCE_PATTERN",
    "SEPARATOR_LINE_PATTERN",
    "TABLE_OF_CONTENTS_PATTERN",
    "TABLE_REF_PATTERN",
    "TABLE_SHEET_PATTERN",
    "WORD_MAP_PATTERN",
    "WORD_NUMBER_PATTERN",
    "WORD_TITLE_PATTERN",
    "build_merge_context",
    "count_mapping_pairs",
    "determine_block_semantic_type",
    "estimate_evidence_score",
    "estimate_noise_penalty",
    "estimate_tokens",
    "extract_field_names",
    "extract_protocol_anchor",
    "extract_section_keys",
    "extract_structure_tags",
    "get_block_content",
    "merge_block_contents",
    "normalize_protocol_family",
]

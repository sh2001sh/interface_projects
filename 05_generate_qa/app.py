from runtime_config import apply_runtime_environment, get_service_runner_config
from streaming_utils import is_stream_requested, stream_flask_handler
# 接口4: QA对生成
# POST /api/knowledge/generate_qa

import os
import sys
import json
import time
import uuid
import re
from typing import List, Dict, Any, Optional, Tuple, Set
from flask import Flask, request, jsonify

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from database.mysql_client import MySQLClient
except Exception:
    MySQLClient = None
from database.models import Block, Chunk, QAPair
from llm.local_llm import LocalLLM, get_llm
from llm.prompt_templates import PromptTemplates
from utils.file_store import FileStore
from job_runtime import (
    build_status_response,
    build_stream_response,
    build_submit_response,
    complete_job,
    fail_job,
    start_job,
    update_job,
)


apply_runtime_environment()

app = Flask(__name__)


class _UnavailableDBClient:
    """测试或依赖缺失时的占位 DB 客户端。"""

    def connection(self):
        raise RuntimeError("数据库客户端不可用")

    def __getattr__(self, name: str):
        raise RuntimeError(f"数据库客户端不可用: {name}")


# 初始化客户端
db_client = MySQLClient() if MySQLClient is not None else _UnavailableDBClient()
llm_client: Optional[LocalLLM] = None
file_store = FileStore()
try:
    db_client.init_tables()
except Exception as exc:
    print(f"数据库表初始化失败: {exc}")

TASK_TYPE_ALIASES = {
    "协议理解类": "protocol_understanding",
    "协议理解": "protocol_understanding",
    "understanding": "protocol_understanding",
    "protocol_understanding": "protocol_understanding",
    "协议转换类": "protocol_conversion",
    "协议转换": "protocol_conversion",
    "conversion": "protocol_conversion",
    "protocol_conversion": "protocol_conversion",
}

CONVERSION_MODE_ALIASES = {
    "转义": "transcoding",
    "transcoding": "transcoding",
    "转换": "mapping",
    "mapping": "mapping",
}

USE_LLM_QUALITY_CHECK = os.getenv("USE_LLM_QUALITY_CHECK", "false").lower() == "true"
QA_GENERATION_RETRY = max(0, int(os.getenv("QA_GENERATION_RETRY", "1")))
QA_GENERATION_BATCH_SIZE = max(1, int(os.getenv("QA_GENERATION_BATCH_SIZE", "8")))
QA_GENERATION_MAX_ATTEMPTS = max(1, int(os.getenv("QA_GENERATION_MAX_ATTEMPTS", "6")))
QA_SELECTION_TOP_K_DEFAULT = max(1, int(os.getenv("QA_SELECTION_TOP_K_DEFAULT", "10")))
FAST_BATCH_MAX_CHARS = max(2000, int(os.getenv("FAST_BATCH_MAX_CHARS", "7000")))
FAST_BATCH_MIN_UNITS = max(1, int(os.getenv("FAST_BATCH_MIN_UNITS", "3")))
FAST_BATCH_MAX_UNITS = max(1, int(os.getenv("FAST_BATCH_MAX_UNITS", "4")))
FAST_OVERGEN_FACTOR = max(2, int(os.getenv("FAST_OVERGEN_FACTOR", "3")))
UNDERSTANDING_SEMANTIC_TYPES = {"field_definition", "protocol_description", "single_block", "general_content"}
CONVERSION_SEMANTIC_TYPES = {"conversion_rule", "table_data"}
RULE_SIGNAL_PATTERN = re.compile(
    r"(?:formula|公式|mapping|映射|range|范围|resolution|分辨率|bit|位|value\s*[\*\/\+\-]|->|→|=)",
    flags=re.IGNORECASE,
)
FIELD_CODING_HINT_PATTERN = re.compile(
    r"(?:FIELD CODING FOR|DI BIT CODE|DUI/DI EXPLANATION|NO STATEMENT\s+\d+|ILLEGAL\s+\d+|UNDEFINED\s+\d+)",
    flags=re.IGNORECASE,
)
STRUCTURED_BLOCK_HINT_PATTERN = re.compile(
    r"(?:DATA ELEMENT SUMMARY|WORD MAP|WORD DESCRIPTION|REFERENCE DFI/DUI|BIT POSITION|WORD NUMBER:\s*J\d|FIELD CODING FOR)",
    flags=re.IGNORECASE,
)
FAST_NUMERIC_SIGNAL_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:px|dpi|mm|cm|bit|bits|hz|fps|kbps|mbps|knots|m/s|deg|°|ft|feet|meter|meters)",
    flags=re.IGNORECASE,
)
FAST_ENUM_SIGNAL_PATTERN = re.compile(
    r"(?:\b\d+\s*(?:=|->|→)\s*[A-Za-z_][A-Za-z0-9_/\- ]+|\b(?:NO STATEMENT|ILLEGAL|UNDEFINED)\s+\d+)",
    flags=re.IGNORECASE,
)
NOISE_HINT_PATTERN = re.compile(
    r"(?:^table\s+\d|^figure\s+\d|continued|page\s+\d+|页眉|页脚|保密|密级|^\W*$)",
    flags=re.IGNORECASE,
)
TOC_HINT_PATTERN = re.compile(
    r"(?:table of contents|list of tables|paragraph\s*\|\s*title|record formats|transmit tables)",
    flags=re.IGNORECASE,
)


def get_llm_client() -> LocalLLM:
    """获取LLM客户端单例"""
    global llm_client
    if llm_client is None:
        llm_client = get_llm()
    return llm_client


def normalize_task_type(value: Optional[str]) -> str:
    if not value:
        return "protocol_understanding"
    raw = str(value).strip()
    return TASK_TYPE_ALIASES.get(raw) or TASK_TYPE_ALIASES.get(raw.lower(), "protocol_understanding")


def normalize_conversion_mode(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "" or raw.lower() in {"null", "none"}:
        return None
    normalized = CONVERSION_MODE_ALIASES.get(raw) or CONVERSION_MODE_ALIASES.get(raw.lower())
    return normalized


def looks_like_block_formula(text: str) -> bool:
    """判断文本是否为多行块公式。"""
    cleaned = LocalLLM._sanitize_response_text(str(text or "")).strip()
    if "\n" not in cleaned:
        return False
    return any(
        token in cleaned
        for token in ("\nif ", "\nfor ", "\nwhile ", "\nelse:", "result =", "\nresult =")
    ) or cleaned.startswith(("if ", "for ", "while ", "result ="))


def extract_formula_only(text: str) -> str:
    """从文本中抽取公式，若无明显公式则返回原文本首行"""
    if not text:
        return ""
    sanitized = LocalLLM._sanitize_response_text(str(text)).strip()
    if looks_like_block_formula(sanitized):
        return sanitized
    formula_patterns = [
        r"(?:公式|formula|conversion)\s*[:：=]\s*([^\n;，。]+)",
        r"((?:-?\d+\s*(?:=|->|→)\s*[A-Za-z_][A-Za-z0-9_./\-]*)(?:\s*(?:,|，|;|；|and|AND)\s*-?\d+\s*(?:=|->|→)\s*[A-Za-z_][A-Za-z0-9_./\-]*)*)",
        r"([A-Za-z_][A-Za-z0-9_\s]*\s*=\s*[^;\n]+)",
        r"((?:value|x|val)\s*(?:\s*[\*\/\+\-]\s*[0-9A-Za-z_().]+)+)",
    ]
    for pattern in formula_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            if match.lastindex:
                formula = match.group(1).strip()
            else:
                formula = match.group(0).strip()
            formula = re.sub(r"\s+(?:and|AND)\s+", ", ", formula)
            return formula
    fallback = text.splitlines()[0].strip()
    fallback = re.sub(r"\s+(?:and|AND)\s+", ", ", fallback)
    return fallback


def contains_arithmetic_expression(text: str) -> bool:
    """判断文本是否包含可计算算术表达式。"""
    if not text:
        return False
    if looks_like_block_formula(text):
        return bool(re.search(r"[\*\/\+\-]", text) or re.search(r"\b(?:signed|unsigned|scale|clip|int|float|min|max|sum)\s*\(", text))
    return bool(
        re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*[\*\/\+\-]\s*[0-9A-Za-z_(]", text)
        or (
            re.search(r"\b(?:value|raw|physical|lat|lon|x|val)\b", text, flags=re.IGNORECASE)
            and re.search(r"[\*\/\+\-]", text)
        )
    )


def infer_conversion_mode(formula_text: str) -> str:
    """根据转换表达式内容推断conversion_mode。"""
    text = str(formula_text or "").strip()
    if not text:
        return "mapping"
    if looks_like_block_formula(text):
        if contains_arithmetic_expression(text):
            return "transcoding"
        return "mapping"
    if re.search(r"(?:->|→|=>)", text):
        return "mapping"
    if re.search(r"-?\d+\s*=\s*[A-Za-z_]", text):
        return "mapping"
    if re.search(r"[A-Za-z_]+\s*=\s*-?\d+", text):
        return "mapping"
    if contains_arithmetic_expression(text):
        return "transcoding"
    return "mapping"


def contains_mapping_relation(text: str) -> bool:
    """判断文本是否包含离散值映射关系。"""
    if not text:
        return False
    if looks_like_block_formula(text):
        return "result =" in text or "if " in text
    return bool(re.search(r"-?\d+\s*(?:=|->|→)\s*[A-Za-z_]", text))


def normalize_conversion_payload(
    answer: str,
    conversion_formula: Optional[str],
    conversion_mode: Optional[str],
) -> Dict[str, str]:
    """规范化协议转换类字段，确保公式与模式一致。"""
    normalized_answer = extract_formula_only(str(answer or "").strip())
    normalized_formula = extract_formula_only(str(conversion_formula or normalized_answer).strip())
    if not looks_like_block_formula(normalized_answer) and normalized_formula != normalized_answer:
        normalized_formula = normalized_answer

    inferred_mode = infer_conversion_mode(normalized_formula or normalized_answer)
    normalized_mode = normalize_conversion_mode(conversion_mode) or inferred_mode
    if normalized_mode != inferred_mode:
        normalized_mode = inferred_mode

    return {
        "answer": normalized_answer,
        "conversion_formula": normalized_formula,
        "conversion_mode": normalized_mode,
    }


def normalize_source_fields_value(value: Any, fallback: Optional[str] = None) -> List[str]:
    """规范化 source_fields。"""
    normalized: List[str] = []
    if isinstance(value, list):
        normalized = [str(item).strip().upper() for item in value if str(item).strip()]
    elif isinstance(value, str):
        normalized = [item.strip().upper() for item in value.split(",") if item.strip()]
    if normalized:
        return normalized
    fallback_name = str(fallback or "").strip().upper()
    return [fallback_name] if fallback_name else []


def _normalize_structured_ascii_layout(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[-]{20,}", "\n", normalized)
    normalized = re.sub(r"\n{2,}", "\n", normalized)
    return normalized


def _is_section_like_field_name(field_name: str) -> bool:
    normalized = str(field_name or "").strip().upper()
    if not normalized:
        return False
    if normalized in {"PARAGRAPH", "TITLE", "TABLE", "APPENDIX", "SECTION"}:
        return True
    if re.fullmatch(r"[A-Z]\.\d+(?:\.\d+)*", normalized):
        return True
    return False


def _has_structured_field_evidence(bit_segment: str, details: str) -> bool:
    combined = f"{bit_segment} | {details}".strip()
    if not combined:
        return False
    if re.search(r"(?:range|范围|resolution|分辨率|unit|单位|bit|bits|位宽|起始位|offset|length|枚举|取值|meaning|含义)", combined, flags=re.IGNORECASE):
        return True
    if re.search(r"\b\d+\s*(?:bit|bits)\b", combined, flags=re.IGNORECASE):
        return True
    if re.search(r"-?\d+(?:\.\d+)?\s*(?:to|TO|~|～|—|–)\s*-?\d+(?:\.\d+)?", combined):
        return True
    if re.search(r"-?\d+(?:\.\d+)?\s*[A-Za-z%°/]+", combined):
        return True
    if FIELD_CODING_HINT_PATTERN.search(combined):
        return True
    return False


def _has_conversion_evidence(details: str) -> bool:
    text = str(details or "").strip()
    if not text:
        return False
    if contains_mapping_relation(text):
        return True
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^=\n]+", text) and contains_arithmetic_expression(text):
        return True
    if re.search(r"\b(?:raw|value|physical|scaled|result)\b", text, flags=re.IGNORECASE) and contains_arithmetic_expression(text):
        return True
    return False


def _split_field_coding_name_and_details(text: str) -> Tuple[str, str]:
    normalized = re.sub(r"\s+", " ", str(text or "").strip()).strip(" |-")
    if not normalized:
        return "", ""

    markers = [
        " SPECIFIES ",
        " IDENTIFIES ",
        " INDICATES ",
        " CONTAINS ",
        " IS USED ",
        " IS THE ",
        " SHALL ",
        " THE ",
    ]
    for marker in markers:
        if marker not in normalized:
            continue
        left, right = normalized.split(marker, 1)
        candidate = _normalize_field_name(left)
        if _looks_like_field_name(candidate):
            return candidate, f"{marker.strip()} {right}".strip()

    candidate = _normalize_field_name(normalized)
    if _looks_like_field_name(candidate):
        return candidate, ""
    return "", normalized


def _extract_enum_mapping_text_from_context(details: str, limit: int = 4) -> Optional[str]:
    enum_pairs = _extract_enum_pairs(details, limit=limit)
    if not enum_pairs:
        return None
    return "映射" + "，".join(f"{value}={label}" for value, label in enum_pairs)


def _extract_enum_pairs(details: str, limit: Optional[int] = None) -> List[Tuple[str, str]]:
    text = re.sub(r"\s+", " ", str(details or "").upper()).strip()
    if not text:
        return []

    matches = re.findall(
        r"([A-Z][A-Z0-9/().,\-]*(?:\s+[A-Z][A-Z0-9/().,\-]*){0,6})\s+(\d{1,6})(?=\s+(?:[A-Z][A-Z0-9/().,\-]+(?:\s+[A-Z][A-Z0-9/().,\-]+){0,6}\s+\d{1,6}|$))",
        text,
    )
    pairs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for label, value in matches:
        normalized_label = re.sub(r"\s+", " ", label).strip(" ,.-")
        if not normalized_label or normalized_label in {"BIT", "BITS", "CODE", "DI", "DUI"}:
            continue
        key = (value, normalized_label)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((value, normalized_label))
        if limit is not None and len(pairs) >= limit:
            break
    return pairs


def _is_enum_value_label(field_name: str, info: Optional[Dict[str, str]] = None) -> bool:
    normalized = _normalize_field_name(field_name)
    if not normalized:
        return False
    if normalized in {"DFI", "DUI", "DI", "BIT CODE", "CODING", "CHARACTER"}:
        return True
    if re.fullmatch(r"[0-9A-Z/().,\-]{1,3}", normalized):
        return True
    details_text = str((info or {}).get("details") or "").strip()
    bit_segment = str((info or {}).get("bit_segment") or "").strip()
    if re.fullmatch(r"\d{1,3}", bit_segment) and re.fullmatch(r"\d{1,3}\s+BITS?", details_text, flags=re.IGNORECASE):
        return True
    if len(normalized.split()) <= 4 and re.search(r"\b(?:NO STATEMENT|REFUEL|ORBIT|RECALL|ENGAGE|INTERVENE|DISUSED)\b", normalized):
        return True
    return False


def _is_pure_field_coding_mapping_chunk(content: str, chunk_pattern: str) -> bool:
    if chunk_pattern != "field_coding":
        return False
    compact = re.sub(r"\s+", " ", str(content or "")).upper()
    if "CODING | CHARACTER" in compact:
        return True
    lines = [re.sub(r"\s+", " ", str(line or "").strip()).upper() for line in str(content or "").splitlines()]
    mapping_rows = 0
    for line in lines:
        if re.fullmatch(r"(?:[01]{4,8}|\d{1,5}|[A-Z0-9])\s*\|\s*[A-Z0-9.\-]", line):
            mapping_rows += 1
    if mapping_rows >= 3 and not any("DUI/DI NAME" in line or "DFI | DUI" in line for line in lines):
        return True
    return False


def _extract_field_coding_contexts(content: str) -> List[Tuple[str, str, str]]:
    contexts: List[Tuple[str, str, str]] = []
    if not FIELD_CODING_HINT_PATTERN.search(str(content or "")):
        return contexts

    for raw_line in _normalize_structured_ascii_layout(content).splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not re.match(r"^\d+\s*\|\s*\d+\s*\|", line):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            continue
        field_column = parts[2]
        bit_code = parts[3] if len(parts) > 3 else ""
        tail_details = " | ".join(part for part in parts[4:] if part)
        field_name, inline_details = _split_field_coding_name_and_details(field_column)
        details = " | ".join(part for part in [inline_details, tail_details] if part).strip(" |")
        if not field_name:
            continue
        if not details and bit_code:
            details = f"code {bit_code}"
        if not _has_structured_field_evidence(bit_code, details or field_column):
            continue
        contexts.append((field_name, bit_code, details or field_column))
    return contexts


def _extract_reference_layout_contexts(content: str) -> List[Tuple[str, str, str]]:
    contexts: List[Tuple[str, str, str]] = []
    if not re.search(r"(?:REFERENCE DFI/DUI|BIT POSITION|RESOLUTION, CODING, ETC)", str(content or ""), flags=re.IGNORECASE):
        return contexts

    word_anchor = _extract_word_anchor_from_text(content)
    for raw_line in _normalize_structured_ascii_layout(content).splitlines():
        line = re.sub(r"\s+", " ", str(raw_line or "").strip())
        if not line or not re.match(r"^\d+\s*\|", line):
            continue
        if re.search(r"(?:DATA FIELD DESCRIPTOR|BIT POSITION|REFERENCE DFI/DUI)", line, flags=re.IGNORECASE):
            continue
        normalized_line = re.sub(r"^\d+\s*\|\s*\d+\s*\|?\s*", "", line)
        width_match = re.search(r"\|\s*(\d{1,3})\s*\|\s*([^|]*)\s*\|?\s*$", normalized_line)
        if not width_match:
            continue
        bit_width = width_match.group(1).strip()
        tail = str(width_match.group(2) or "").strip(" |-")
        prefix = normalized_line[:width_match.start()]
        prefix = re.sub(r"\|\s*", " ", prefix).strip()
        bit_match = re.search(r"(.+?)\s+(\d+\s*-\s*\d+|\d+)\s*$", prefix)
        if not bit_match:
            continue
        field_name = _normalize_field_name(bit_match.group(1))
        bit_segment = re.sub(r"\s+", "", bit_match.group(2))
        if not _looks_like_field_name(field_name):
            continue
        details = f"{bit_width} bits"
        if bit_segment:
            details += f" | bit range {bit_segment}"
        if tail:
            details += f" | {tail}"
        if word_anchor:
            details += f" | word {word_anchor}"
        contexts.append((field_name, bit_segment or bit_width, details))
    if contexts:
        return contexts

    compact = re.sub(r"\s+", " ", _normalize_structured_ascii_layout(content)).strip()
    pattern = re.compile(
        r"(?:\d+\s*\|\s*\d+\s*\|\s*)?"
        r"([A-Z][A-Z0-9,()/\- ]{2,}?)\s*"
        r"(\d+\s*-\s*\d+|\d+)\s*\|\s*"
        r"(\d{1,3})\s*\|\s*"
        r"([^|]{0,120})",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(compact):
        field_name = _normalize_field_name(match.group(1))
        bit_segment = re.sub(r"\s+", "", match.group(2))
        bit_width = match.group(3).strip()
        tail = str(match.group(4) or "").strip(" |-")
        if not _looks_like_field_name(field_name):
            continue
        details = f"{bit_width} bits"
        if bit_segment:
            details += f" | bit range {bit_segment}"
        if tail:
            details += f" | {tail}"
        if word_anchor:
            details += f" | word {word_anchor}"
        contexts.append((field_name, bit_segment or bit_width, details))
    return contexts


def _is_ascii_width_vector_line(line: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(line or "").strip())
    if not normalized:
        return False
    if not re.fullmatch(r":?\s*(?:\d{1,3}\s*:\s*){1,}\d{1,3}\s*:?", normalized):
        return False
    return len(re.findall(r"\d{1,3}", normalized)) >= 2


def _is_ascii_bit_index_line(line: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(line or "").strip())
    if not normalized:
        return False
    if re.search(r"[A-Z]", normalized, flags=re.IGNORECASE):
        return False
    return bool(re.fullmatch(r":?\s*(?:\d{1,3}\s*)+(?::\s*(?:\d{1,3}\s*)+)+:?", normalized))


def _split_ascii_column_parts(line: str, expected_count: int) -> List[str]:
    raw_parts = [part.strip(" -<>") for part in str(line or "").split(":")]
    if raw_parts and raw_parts[-1] == "":
        raw_parts = raw_parts[:-1]
    while len(raw_parts) > expected_count and raw_parts and raw_parts[0] in {"", "<---", "--->"}:
        raw_parts = raw_parts[1:]
    while len(raw_parts) > expected_count and raw_parts and raw_parts[-1] in {"", "<---", "--->"}:
        raw_parts = raw_parts[:-1]
    if len(raw_parts) > expected_count:
        raw_parts = raw_parts[-expected_count:]
    if len(raw_parts) < expected_count:
        raw_parts.extend([""] * (expected_count - len(raw_parts)))
    return raw_parts


def _collapse_ascii_column_label(parts: List[str]) -> str:
    fragments = [fragment.strip(" -<>") for fragment in parts if fragment.strip(" -<>") and fragment.strip() not in {"<---", "--->"}]
    return _normalize_field_name(" ".join(fragments))


def _extract_ascii_word_map_contexts(content: str) -> List[Tuple[str, str, str]]:
    contexts: List[Tuple[str, str, str]] = []
    if "WORD MAP" not in str(content or "").upper():
        return contexts

    word_anchor = _extract_word_anchor_from_text(content)
    lines = [re.sub(r"\s+", " ", str(line or "").strip()) for line in _normalize_structured_ascii_layout(content).splitlines()]
    for index, line in enumerate(lines):
        if not _is_ascii_width_vector_line(line):
            continue
        widths = re.findall(r"\b\d{1,3}\b", line)
        if len(widths) < 2:
            continue
        label_lines: List[str] = []
        probe = index - 1
        while probe >= 0 and len(label_lines) < 4:
            candidate = lines[probe]
            if not candidate:
                break
            if _is_ascii_width_vector_line(candidate):
                break
            if _is_ascii_bit_index_line(candidate):
                probe -= 1
                continue
            if candidate.startswith("WORD NUMBER") or candidate.startswith("WORD TITLE") or candidate == "WORD MAP":
                break
            label_lines.append(candidate)
            probe -= 1
        label_lines.reverse()
        if not label_lines:
            continue
        column_count = len(widths)
        columns: List[List[str]] = [[] for _ in range(column_count)]
        for label_line in label_lines:
            if ":" not in label_line:
                continue
            parts = _split_ascii_column_parts(label_line, column_count)
            for col_index, part in enumerate(parts[:column_count]):
                columns[col_index].append(part)
        for width, fragments in zip(widths, columns):
            label = _collapse_ascii_column_label(fragments)
            if not _looks_like_field_name(label):
                continue
            details = f"{width} bits | word map"
            if word_anchor:
                details += f" | word {word_anchor}"
            contexts.append((label, width, details))
    if contexts:
        return contexts

    compact = re.sub(r"[-]{5,}", " ", _normalize_structured_ascii_layout(content))
    compact = re.sub(r"\s+", " ", compact).strip()
    pattern = re.compile(
        r"([A-Z][A-Z0-9,()/\- ]{2,}?(?:\s*:\s*[A-Z][A-Z0-9,()/\- ]{1,40}){2,})\s+((?:\d{1,3}\s*:\s*){2,}\d{1,3})",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(compact):
        labels = [item.strip(" -<>()") for item in match.group(1).split(":") if item.strip(" -<>()")]
        widths = [item.strip() for item in match.group(2).split(":") if item.strip()]
        if len(labels) != len(widths):
            continue
        for label, width in zip(labels, widths):
            if not _looks_like_field_name(label):
                continue
            details = f"{width} bits | word map"
            if word_anchor:
                details += f" | word {word_anchor}"
            contexts.append((_normalize_field_name(label), width, details))
    return contexts


def _extract_data_element_summary_part_pairs(parts: List[str]) -> List[Tuple[str, str, str]]:
    contexts: List[Tuple[str, str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    normalized_parts = [re.sub(r"\s+", " ", str(part or "").strip()).strip(" |-") for part in parts]
    part_count = len(normalized_parts)
    for index, part in enumerate(normalized_parts):
        if not part:
            continue
        candidates: List[Tuple[str, str]] = []
        label = _normalize_field_name(part)
        if _looks_like_field_name(label):
            for probe in range(index + 1, min(index + 3, part_count)):
                width_text = normalized_parts[probe]
                if re.fullmatch(r"\d{1,3}", width_text):
                    candidates.append((label, width_text))
                    break
        if index + 1 < part_count:
            combined_label = _normalize_field_name(f"{part} {normalized_parts[index + 1]}")
            if _looks_like_field_name(combined_label):
                for probe in range(index + 2, min(index + 4, part_count)):
                    width_text = normalized_parts[probe]
                    if re.fullmatch(r"\d{1,3}", width_text):
                        candidates.append((combined_label, width_text))
                        break
        for field_name, width_text in candidates:
            key = (field_name, width_text)
            if key in seen:
                continue
            seen.add(key)
            contexts.append((field_name, width_text, f"{width_text} bits | data element summary"))
    return contexts


def _extract_word_anchor_from_text(text: str) -> str:
    match = re.search(r"\b(J\d+\.\d+[A-Z0-9]*)\b", str(text or ""), flags=re.IGNORECASE)
    return str(match.group(1)).upper() if match else ""


def _extract_data_element_summary_inline_pairs(text: str) -> List[Tuple[str, str, str]]:
    contexts: List[Tuple[str, str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    compact = re.sub(r"\s+", " ", str(text or "").strip())
    if not compact:
        return contexts
    upper = compact.upper()
    if "DATA ELEMENT" not in upper and "ELEMENT__________________# BITS" not in upper:
        return contexts
    word_anchor = _extract_word_anchor_from_text(compact)
    pattern = re.compile(
        r"([A-Z][A-Z0-9,()/\-]*(?:\s+[A-Z][A-Z0-9,()/\-]*){0,6})\s+(\d{1,3})(?=\s+(?:\d{1,3}\s+)?[A-Z]|$)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(compact):
        field_name = _normalize_field_name(match.group(1))
        field_name = re.sub(r"^(?:BITS|DATA)\s+", "", field_name).strip()
        width_text = match.group(2)
        if not _looks_like_field_name(field_name):
            continue
        key = (field_name, width_text)
        if key in seen:
            continue
        seen.add(key)
        details_parts = [f"{width_text} bits", "data element summary"]
        if word_anchor:
            details_parts.append(f"word {word_anchor}")
        contexts.append((field_name, width_text, " | ".join(details_parts)))
    return contexts


def _extract_data_element_summary_contexts(content: str) -> List[Tuple[str, str, str]]:
    contexts: List[Tuple[str, str, str]] = []
    normalized_content = _normalize_structured_ascii_layout(content)
    normalized_upper = normalized_content.upper()
    if "DATA ELEMENT SUMMARY" not in normalized_upper and "ELEMENT__________________# BITS" not in normalized_upper:
        return contexts

    seen: Set[Tuple[str, str]] = set()
    current_word_anchor = ""
    for raw_line in normalized_content.splitlines():
        line = re.sub(r"\s+", " ", str(raw_line or "").strip())
        if not line:
            continue
        if "MESSAGE SUMMARY" in line.upper():
            continue
        line_word_anchor = _extract_word_anchor_from_text(line)
        if line_word_anchor:
            current_word_anchor = line_word_anchor
        if "|" in line:
            line_contexts = _extract_data_element_summary_part_pairs([part for part in line.split("|")])
        else:
            line_contexts = _extract_data_element_summary_inline_pairs(line)
        for field_name, width_text, details in line_contexts:
            field_name = re.sub(r"^(?:BITS|DATA)\s+", "", _normalize_field_name(field_name)).strip()
            if not _looks_like_field_name(field_name):
                continue
            key = (field_name, width_text)
            if key in seen:
                continue
            seen.add(key)
            final_details = details
            if current_word_anchor and f"word {current_word_anchor}" not in final_details:
                final_details = f"{final_details} | word {current_word_anchor}"
            contexts.append((field_name, width_text, final_details))
    return contexts


def _extract_simple_mapping_rows(content: str, limit: int = 12) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for raw_line in str(content or "").splitlines():
        line = re.sub(r"\s+", " ", str(raw_line or "").strip())
        match = re.match(r"^([01]{3,8}|\d{1,6}|[A-Z0-9]{1,8})\s*\|\s*([A-Z0-9.\-]{1,16})$", line)
        if not match:
            continue
        left = match.group(1).strip()
        right = match.group(2).strip()
        key = (left, right)
        if key in seen:
            continue
        seen.add(key)
        rows.append(key)
        if len(rows) >= limit:
            break
    return rows


def _normalize_field_name(field_name: str) -> str:
    text = str(field_name or "").upper()
    text = re.sub(r"（[^）]*）", "", text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,|:-")
    return text


FIELD_NAME_SIGNAL_TOKENS = {
    "ADDRESS",
    "ADDRESSEE",
    "ALTITUDE",
    "ANGLE",
    "ASSOCIATED",
    "BEARING",
    "CHARACTER",
    "CODE",
    "COMPLIANCE",
    "CONTINUATION",
    "COORDINATE",
    "DESIGNATOR",
    "DISCRETE",
    "DISTANCE",
    "ELEVATION",
    "FORMAT",
    "FREQUENCY",
    "IDENTIFIER",
    "INDEX",
    "INDICATOR",
    "LABEL",
    "LASER",
    "LATITUDE",
    "LENGTH",
    "LONGITUDE",
    "MISSION",
    "MODE",
    "NUMBER",
    "OBJECTIVE",
    "ORIGINATOR",
    "PARTY",
    "POSITION",
    "POWER",
    "PRECISION",
    "RATE",
    "RECEIPT",
    "RELATED",
    "SIGNAL",
    "SOURCE",
    "SPARE",
    "SPEED",
    "STRENGTH",
    "TARGET",
    "THIRD",
    "TIME",
    "TRACK",
    "TYPE",
    "VELOCITY",
    "WORD",
}


INTENT_PRIORITY_BY_PATTERN = {
    "field_coding": ["meaning", "bit_width", "layout", "range", "unit", "resolution"],
    "data_element_summary": ["bit_width", "range", "unit", "resolution", "layout", "meaning"],
    "reference_layout": ["bit_width", "layout", "range", "unit", "resolution", "meaning"],
    "word_map": ["bit_width", "layout", "range", "unit", "resolution", "meaning"],
    "word_description": ["meaning", "bit_width", "range", "unit", "layout", "resolution"],
    "message_summary": ["meaning", "other"],
    "general": ["meaning", "bit_width", "range", "unit", "layout", "resolution", "other"],
}


def _has_field_name_signal_token(normalized: str) -> bool:
    tokens = re.findall(r"[A-Z][A-Z0-9/.-]*", normalized)
    return any(token in FIELD_NAME_SIGNAL_TOKENS for token in tokens)


def _looks_like_field_name(field_name: str) -> bool:
    normalized = _normalize_field_name(field_name)
    if not normalized:
        return False
    if _is_section_like_field_name(normalized):
        return False
    if "_" in normalized:
        return False
    if normalized in {
        "DATA ELEMENT",
        "DATA ELEMENT SUMMARY",
        "WORD DESCRIPTION",
        "WORD MAP",
        "REFERENCE DFI/DUI",
        "REFERENCE BIT",
        "BITS",
        "BITS",
        "POSITION",
        "FIELD CODING",
        "RESOLUTION, CODING, ETC",
        "MINUTE",
        "HOUR",
        "SECOND",
        "THROUGH",
        "DEGREES",
        "FEET",
        "UNITS",
        "NUMERIC",
        "NO STATEMENT",
        "TABLE",
        "MESSAGE",
        "MESSAGE USE",
        "APPLICABLE",
        "APPLICABLE RECEIVE TABLE(S)",
        "APPLICABLE TRANSMIT TABLE(S)",
        "IMP",
        "REQ",
        "REQUIREMENTS",
        "RECEIVE",
        "TRANSMIT",
        "DATA",
        "FUNCTION",
        "PLATFORM",
        "SITUATIONAL",
        "AWARENESS",
        "STATUS",
        "REPORT",
        "ASSIGNMENT",
        "ASSIGNMENTS",
        "CONTROL",
        "RADIO",
        "RELAY",
    }:
        return False
    if re.search(r"MIL-STD|SHEET|CONTINUED|MESSAGE SUMMARY|WORD NUMBER|WORD TITLE", normalized):
        return False
    if re.fullmatch(r"J\d+\.\d+[A-Z0-9]*", normalized):
        return False
    if re.search(r"\b(?:TABLE|MESSAGE|RECEIVE|TRANSMIT|REQUIREMENTS|FUNCTION)\b", normalized):
        if len(normalized.split()) <= 3 and not _has_field_name_signal_token(normalized):
            return False
    if re.match(r"^\d{1,3}\s+", normalized):
        return False
    if re.search(r"\b(?:THROUGH|UNITS?|DEGREES?|NO STATEMENT|PRECISION|INCREMENTS?)\b", normalized):
        return False
    if re.fullmatch(r"(?:LSB|LSBS|FT|BIT|BITS)", normalized):
        return False
    if not re.search(r"[A-Z]", normalized):
        return False
    if re.fullmatch(r"[#0-9 .\-]+", normalized):
        return False
    if len(normalized) < 3:
        return False
    return True


def _prioritize_seed_pairs(
    seed_pairs: List[Dict[str, Any]],
    target_count: int,
    *,
    chunk_pattern: str,
) -> List[Dict[str, Any]]:
    if target_count <= 0 or len(seed_pairs) <= target_count:
        return seed_pairs[: max(0, target_count)]

    intent_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for seed in seed_pairs:
        qa_task_type = normalize_task_type(seed.get("qa_task_type"))
        intent = _question_intent_bucket(str(seed.get("question") or ""), qa_task_type)
        intent_buckets.setdefault(intent, []).append(seed)

    selected: List[Dict[str, Any]] = []
    seen_questions: Set[str] = set()

    def try_add(seed: Dict[str, Any]) -> bool:
        normalized_question = _normalize_text_key(seed.get("question"))
        if not normalized_question or normalized_question in seen_questions:
            return False
        seen_questions.add(normalized_question)
        selected.append(seed)
        return True

    priority_order = INTENT_PRIORITY_BY_PATTERN.get(chunk_pattern, INTENT_PRIORITY_BY_PATTERN["general"])
    while len(selected) < target_count:
        progress = False
        for intent in priority_order:
            bucket = intent_buckets.get(intent) or []
            if not bucket:
                continue
            seed = bucket.pop(0)
            if try_add(seed):
                progress = True
                if len(selected) >= target_count:
                    break
        if len(selected) >= target_count:
            break
        if not progress:
            break

    if len(selected) < target_count:
        for seed in seed_pairs:
            if try_add(seed) and len(selected) >= target_count:
                break

    return selected[:target_count]


def _upsert_field_context(
    context: Dict[str, Dict[str, str]],
    field_name: str,
    bit_segment: str,
    details: str,
) -> None:
    normalized = _normalize_field_name(field_name)
    if not _looks_like_field_name(normalized):
        return
    bit_segment = str(bit_segment or "").strip()
    details = str(details or "").strip(" |")
    if not _has_structured_field_evidence(bit_segment, details):
        return
    existing = context.get(normalized)
    if existing is None:
        context[normalized] = {
            "bit_segment": bit_segment,
            "details": details,
        }
        return
    if not existing.get("bit_segment") and bit_segment:
        existing["bit_segment"] = bit_segment
    if details:
        old_details = str(existing.get("details") or "").strip()
        if not old_details:
            existing["details"] = details
        elif details not in old_details:
            existing["details"] = f"{old_details} | {details}"


def _extract_inline_field_width_pairs(text: str) -> List[Tuple[str, str]]:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return []
    if "|" not in normalized:
        delimiter_count = len(re.findall(r"\b\d{1,3}\b", normalized))
        uppercase_count = len(re.findall(r"\b[A-Z][A-Z0-9/(),.#\-]*\b", normalized))
        if delimiter_count < 2 or uppercase_count < 4:
            return []
    matches = list(
        re.finditer(
            r"([A-Z][A-Z0-9/(),.# \-]{2,}?)\s*\|\s*(\d{1,3})(?=\s*(?:\||$))",
            normalized,
        )
    )
    if len(matches) >= 2:
        return [
            (_normalize_field_name(match.group(1)), match.group(2))
            for match in matches
            if _looks_like_field_name(match.group(1))
        ]
    matches = list(
        re.finditer(
            r"([A-Z][A-Z0-9/().#\-]*(?:\s+[A-Z][A-Z0-9/().,#\-]*){0,7})\s+(\d{1,3})(?=\s+(?:[A-Z][A-Z0-9/().,#\-]*\s*){1,8}|$)",
            normalized,
        )
    )
    pairs: List[Tuple[str, str]] = []
    for match in matches:
        field_name = _normalize_field_name(match.group(1))
        width = match.group(2)
        if _looks_like_field_name(field_name):
            pairs.append((field_name, width))
    return pairs


def _extract_word_map_contexts(text: str) -> List[Tuple[str, str, str]]:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if compact.count("|") < 2:
        return []
    parts = [part.strip() for part in compact.split("|")]
    if len(parts) < 4:
        return []
    candidate_fields = _extract_inline_field_width_pairs(parts[1])
    width_values = re.findall(r"\b\d{1,3}\b", parts[-1])
    if len(candidate_fields) < 2 or len(width_values) < len(candidate_fields):
        return []
    contexts: List[Tuple[str, str, str]] = []
    bit_ranges = re.findall(r"\d+\s*-\s*\d+|\b\d+\b", parts[-2])
    for index, (field_name, _embedded_width) in enumerate(candidate_fields):
        width = width_values[index]
        details_parts = [f"{width} bits"]
        if index < len(bit_ranges):
            details_parts.append(f"bit range {bit_ranges[index]}")
        contexts.append((field_name, width, " | ".join(details_parts)))
    return contexts


def _extract_multiline_field_width_contexts(content: str) -> List[Tuple[str, str, str]]:
    contexts: List[Tuple[str, str, str]] = []
    lines = [str(raw_line or "").strip() for raw_line in content.splitlines()]
    standalone_widths = [line for line in lines if re.fullmatch(r"\d{1,3}", line)]
    current_field: Optional[str] = None
    current_width: Optional[str] = None
    current_details: List[str] = []
    width_cursor = 0

    def flush_current() -> None:
        nonlocal current_field, current_width, current_details
        if current_field and current_width:
            detail_text = " | ".join(part for part in current_details if part).strip()
            contexts.append((current_field, current_width, detail_text or f"{current_width} bits"))
        current_field = None
        current_width = None
        current_details = []

    for line in lines:
        upper = line.upper()
        if not line:
            flush_current()
            continue
        if TOC_HINT_PATTERN.search(line) or "CONTINUED" in upper:
            flush_current()
            continue
        match = re.fullmatch(r"(\d{1,3})\s+(.+)", upper)
        if match:
            flush_current()
            width = match.group(1)
            field_name = _normalize_field_name(match.group(2))
            if _looks_like_field_name(field_name):
                current_field = field_name
                if len(standalone_widths) >= 3 and width_cursor < len(standalone_widths):
                    current_width = standalone_widths[width_cursor]
                    width_cursor += 1
                    current_details.append(f"code {width}")
                else:
                    current_width = width
            continue
        if current_field and re.fullmatch(r"\([^)]{1,12}\)", line):
            current_field = _normalize_field_name(f"{current_field} {line}")
            continue
        if current_field and (
            re.search(r"(?:NO STATEMENT|NUMERIC|DEGREES|DEGREE|NORTH|SOUTH|EAST|WEST|PRECISION|FEET|UNITS|INCREMENTS)", upper)
            or re.search(r"\b\d+\b", upper)
        ):
            current_details.append(line)
            if len(current_details) >= 3:
                flush_current()
            continue
        if current_field:
            flush_current()

    flush_current()
    return contexts


def build_field_context(content: str) -> Dict[str, Dict[str, str]]:
    """从原始协议文本构建字段上下文，供理解类答案增强。"""
    context: Dict[str, Dict[str, str]] = {}
    for field_name, bit_segment, details in _extract_data_element_summary_contexts(content):
        _upsert_field_context(context, field_name, bit_segment, details)
    for field_name, bit_segment, details in _extract_reference_layout_contexts(content):
        _upsert_field_context(context, field_name, bit_segment, details)
    for field_name, bit_segment, details in _extract_ascii_word_map_contexts(content):
        _upsert_field_context(context, field_name, bit_segment, details)
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.search(
            r"\b(?:MESSAGE USE|APPLICABLE RECEIVE TABLE\(S\)|APPLICABLE TRANSMIT TABLE\(S\)|IMP\s*REQ|APPLICATION\s*\|)",
            line,
            flags=re.IGNORECASE,
        ):
            continue
        for field_name, bit_segment, details in _extract_word_map_contexts(line):
            _upsert_field_context(context, field_name, bit_segment, details)
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                field_name = parts[0].strip().upper()
                bit_segment = parts[1].strip() if len(parts) > 1 else ""
                details = " | ".join([p for p in parts[2:] if p]).strip()
                if _looks_like_field_name(field_name):
                    _upsert_field_context(context, field_name, bit_segment, details)
                if len(parts) >= 4:
                    inline_pairs = _extract_inline_field_width_pairs(line)
                    for pair_field, pair_width in inline_pairs:
                        _upsert_field_context(context, pair_field, pair_width, f"{pair_width} bits")
                continue
        inline_pairs = _extract_inline_field_width_pairs(line)
        if len(inline_pairs) >= 2:
            for pair_field, pair_width in inline_pairs:
                _upsert_field_context(context, pair_field, pair_width, f"{pair_width} bits")
    for field_name, bit_segment, details in _extract_field_coding_contexts(content):
        _upsert_field_context(context, field_name, bit_segment, details)
    for field_name, bit_segment, details in _extract_multiline_field_width_contexts(content):
        _upsert_field_context(context, field_name, bit_segment, details)
    bad_keys = [field_name for field_name, info in context.items() if _is_enum_value_label(field_name, info)]
    for field_name in bad_keys:
        context.pop(field_name, None)
    return context


def build_topic_context(content: str) -> List[str]:
    """从块内容中抽取真实标题/主题锚点，供低信号块生成理解题。"""
    topics: List[str] = []
    seen: Set[str] = set()
    for raw_line in content.splitlines():
        line = str(raw_line or "").strip()
        if not line or len(line) > 120:
            continue
        upper = line.upper()
        if TOC_HINT_PATTERN.search(line) or "CONTINUED" in upper:
            continue
        if "|" in line:
            parts = [part.strip() for part in line.split("|") if part.strip()]
            for part in parts:
                if len(part) < 4:
                    continue
                if re.search(r"[A-Z]", part) and not re.fullmatch(r"[A-Z0-9.\-]+", part):
                    key = part.upper()
                    if key not in seen:
                        seen.add(key)
                        topics.append(part)
            continue
        if re.search(r"(?:APPENDIX|SECTION|INTRODUCTION|ASSOCIATION|NETWORK|MESSAGE|REPORT|RECORD)", upper):
            if upper not in seen:
                seen.add(upper)
                topics.append(line)
    return topics[:20]


def match_field_name(question: str, field_context: Dict[str, Dict[str, str]]) -> Optional[str]:
    """根据问题文本匹配字段名。"""
    question_upper = str(question or "").upper()
    for field_name in field_context:
        if field_name in question_upper:
            return field_name
    return None


def _extract_bit_width_from_context(info: Dict[str, str]) -> Optional[int]:
    bit_segment = str(info.get("bit_segment") or "").strip()
    details = str(info.get("details") or "").strip()
    candidates = [bit_segment, details]
    for text in candidates:
        if not text:
            continue
        match = re.search(r"\b(\d{1,3})\s*(?:位|bit|bits)\b", text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
        match = re.search(r"\bBITS?\s*[:=]?\s*(\d{1,3})\b", text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
        match = re.fullmatch(r"\d{1,3}", text)
        if match:
            return int(text)
    return None


def _extract_range_text_from_context(details: str) -> Optional[str]:
    text = str(details or "").strip()
    if not text:
        return None
    match = re.search(
        r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:to|TO|~|～|—|–|-)\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    left = match.group(1).replace(",", "")
    right = match.group(2).replace(",", "")
    return f"范围{left}到{right}"


def _extract_resolution_text_from_context(details: str) -> Optional[str]:
    text = str(details or "").strip()
    if not text:
        return None
    match = re.search(
        r"(?:resolution|分辨率)\s*[:：]?\s*([+\-]?\d+(?:\.\d+)?\s*[A-Za-z%°/]+)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return f"分辨率{match.group(1).strip()}"


def _extract_unit_text_from_context(details: str) -> Optional[str]:
    text = str(details or "").strip()
    if not text:
        return None
    match = re.search(r"(?:unit|单位)\s*[:：]?\s*([A-Za-z%°/\u4e00-\u9fff]+)", text, flags=re.IGNORECASE)
    if match:
        return f"单位{match.group(1).strip()}"
    unitish = re.findall(r"\b(?:degrees?|feet|meters?|knots?|seconds?|hz|khz|mhz|ghz|lsb|lsbs)\b", text, flags=re.IGNORECASE)
    if unitish:
        return f"单位{unitish[0]}"
    return None


def _build_field_intent_map(
    chunk_pattern: str,
    field_context: Dict[str, Dict[str, str]],
) -> Dict[str, List[str]]:
    field_intents: Dict[str, List[str]] = {}
    for field_name, info in field_context.items():
        if _is_enum_value_label(field_name, info):
            continue
        details = str(info.get("details") or "").strip()
        bit_segment = str(info.get("bit_segment") or "").strip()
        bit_width = _extract_bit_width_from_context(info)
        intents: List[str] = []
        if _extract_enum_pairs(details, limit=2):
            intents.append("meaning")
        if bit_width is not None:
            intents.append("bit_width")
        if bit_segment and bit_segment != str(bit_width or ""):
            intents.append("layout")
        if _extract_range_text_from_context(details):
            intents.append("range")
        if _extract_unit_text_from_context(details):
            intents.append("unit")
        if _extract_resolution_text_from_context(details):
            intents.append("resolution")
        if contains_arithmetic_expression(details):
            intents.append("conversion_formula")
        elif contains_mapping_relation(details) and not _extract_enum_pairs(details, limit=2):
            intents.append("conversion_mapping")
        if chunk_pattern == "message_summary" and re.search(r"(?:用途|purpose|used to|used by)", details, flags=re.IGNORECASE):
            intents.append("meaning")
        if intents:
            seen = set()
            field_intents[field_name] = [intent for intent in intents if not (intent in seen or seen.add(intent))]
    return field_intents


def extract_field_name_from_question(question: str) -> Optional[str]:
    """从问题文本中提取疑似源字段名。"""
    matches = re.findall(r"[A-Z][A-Z0-9_./\-]{2,}", str(question or "").upper())
    if not matches:
        return None
    stop_words = {"WHAT", "HOW", "WHY", "THE", "AND", "FOR", "LINK16", "PROTOCOL"}
    for candidate in matches:
        if candidate not in stop_words:
            return candidate
    return matches[0]


def enrich_conversion_payload_with_context(
    question: str,
    payload: Dict[str, str],
    field_context: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    """当转换答案不完整时，从块内字段上下文回填公式与源字段，不依赖外部知识图谱。"""
    # 从问题文本提取源字段名（基于块内容，不从知识图谱查询）
    source_field = match_field_name(question, field_context) or extract_field_name_from_question(question)
    if source_field and not payload.get("source_field"):
        payload["source_field"] = source_field

    mode = payload.get("conversion_mode") or "mapping"
    payload["conversion_mode"] = mode
    answer = payload.get("answer", "")

    needs_mapping = mode == "mapping" and not contains_mapping_relation(answer)
    needs_formula = mode == "transcoding" and not contains_arithmetic_expression(answer)
    if not (needs_mapping or needs_formula):
        return payload

    # 仅从块内字段上下文的 details 中回填，不从外部知识图谱查询
    field_name = payload.get("source_field") or source_field
    details = field_context.get(field_name or "", {}).get("details", "") if field_name else ""
    fallback = extract_formula_only(details) if details else ""

    if needs_mapping and contains_mapping_relation(fallback):
        payload["answer"] = fallback
        payload["conversion_formula"] = fallback
    elif needs_formula and contains_arithmetic_expression(fallback):
        payload["answer"] = fallback
        payload["conversion_formula"] = fallback

    return payload


def enhance_understanding_answer(
    question: str,
    answer: str,
    field_context: Dict[str, Dict[str, str]],
) -> str:
    """基于字段上下文增强过短的协议理解类答案。"""
    normalized_answer = str(answer or "").strip()
    matched_field = match_field_name(question, field_context)
    if not matched_field:
        return normalized_answer

    info = field_context.get(matched_field, {})
    details = info.get("details", "").strip()
    bit_width = _extract_bit_width_from_context(info)
    intent = _question_intent_bucket(question, "protocol_understanding")

    if intent in {"range", "resolution", "unit"}:
        enriched_parts: List[str] = []
        if bit_width is not None and not re.search(r"(?:\b\d+\s*(?:位|bit|bits)\b)", normalized_answer, flags=re.IGNORECASE):
            enriched_parts.append(f"占用{bit_width}位")
        if intent == "range" and not re.search(r"(?:范围|range)", normalized_answer, flags=re.IGNORECASE):
            range_text = _extract_range_text_from_context(details)
            if range_text:
                enriched_parts.append(range_text)
        if intent == "resolution" and not re.search(r"(?:分辨率|resolution)", normalized_answer, flags=re.IGNORECASE):
            resolution_text = _extract_resolution_text_from_context(details)
            if resolution_text:
                enriched_parts.append(resolution_text)
        if intent == "unit" and not re.search(r"(?:单位|unit)", normalized_answer, flags=re.IGNORECASE):
            unit_text = _extract_unit_text_from_context(details)
            if unit_text:
                enriched_parts.append(unit_text)
        if normalized_answer and enriched_parts:
            return f"{normalized_answer}，{'，'.join(enriched_parts)}"

    if len(normalized_answer) >= 16 and re.search(r"\d", normalized_answer):
        return normalized_answer

    segments: List[str] = []

    bit_segment = info.get("bit_segment", "").strip()
    if bit_segment:
        segments.append(f"{matched_field}位段{bit_segment}")

    resolution_match = re.search(
        r"(?:resolution|分辨率)\s*([+\-]?\d+(?:\.\d+)?\s*[A-Za-z%°/]+)",
        details,
        flags=re.IGNORECASE,
    )
    if resolution_match:
        segments.append(f"分辨率{resolution_match.group(1).strip()}")

    range_match = re.search(
        r"(?:range|范围)\s*([+\-]?\d+(?:\.\d+)?)\s*(?:to|TO|~|～|—|–|-)\s*([+\-]?\d+(?:\.\d+)?)",
        details,
        flags=re.IGNORECASE,
    )
    if range_match:
        segments.append(f"范围{range_match.group(1)}到{range_match.group(2)}")

    mapping_pairs = re.findall(
        r"-?\d+\s*(?:=|->|→)\s*[A-Za-z_][A-Za-z0-9_./\-]*",
        details,
        flags=re.IGNORECASE,
    )
    if mapping_pairs:
        segments.append(f"映射{', '.join(mapping_pairs[:4])}")

    enum_mapping_text = _extract_enum_mapping_text_from_context(details, limit=4)
    if enum_mapping_text and enum_mapping_text not in segments:
        segments.append(enum_mapping_text)

    if not segments and details:
        segments.append(details)
    if not segments:
        return normalized_answer

    enhanced = "，".join(segments).strip("，")
    if not enhanced:
        return normalized_answer
    if not enhanced.endswith("。"):
        enhanced += "。"
    return enhanced


def _is_too_short_understanding_answer(
    question: str,
    answer: str,
    field_context: Optional[Dict[str, Dict[str, str]]] = None,
    seed_kind: Optional[str] = None,
) -> bool:
    normalized_answer = str(answer or "").strip()
    normalized_question = _normalize_text_key(question)
    normalized_seed_kind = str(seed_kind or "").strip()
    question_intent = _question_intent_bucket(question, "protocol_understanding")
    if normalized_seed_kind in {"enum_mapping", "simple_mapping"}:
        return not bool(re.search(r"(?:\d+\s*(?:表示|对应|=|->|→)\s*[A-Z0-9])", normalized_answer, flags=re.IGNORECASE))
    if normalized_seed_kind == "message_purpose":
        return len(normalized_answer) < 24 or "主要用于" not in normalized_answer
    if normalized_seed_kind == "word_title":
        return len(normalized_answer) < 16 or "对应的字名称是" not in normalized_answer
    if normalized_seed_kind in {"bit_width", "range", "unit", "resolution"} or question_intent in {"bit_width", "range", "unit", "resolution"}:
        spec_signal_count = 0
        if re.search(r"\d", normalized_answer):
            spec_signal_count += 1
        if re.search(r"(?:位|bit|bits|范围|range|单位|unit|分辨率|resolution|word map|布局)", normalized_answer, flags=re.IGNORECASE):
            spec_signal_count += 1
        matched_field = match_field_name(question, field_context or {})
        if matched_field and matched_field in normalized_answer.upper():
            spec_signal_count += 1
        return spec_signal_count < 2
    if len(normalized_answer) < 10:
        return True

    # 拦截“字段位段1。”、“字段占用1位。”这类只有一个碎片信息的答案
    short_pattern = (
        r"^[A-Z][A-Z0-9_()./\-]*"
        r"(?:字段)?"
        r"(?:占用\d+位|位段\d+(?:-\d+)?|位宽\d+|bit\s*\d+(?:-\d+)?)"
        r"[。.]?$"
    )
    if re.fullmatch(short_pattern, normalized_answer, flags=re.IGNORECASE):
        return True
    if re.fullmatch(
        r"^[A-Z][A-Z0-9_(),./\- ]*(?:位段\d+(?:-\d+)?|位宽\d+|占用\d+位)[。.]?$",
        normalized_answer,
        flags=re.IGNORECASE,
    ):
        return True

    spec_signal_count = 0
    if re.search(r"\d", normalized_answer):
        spec_signal_count += 1
    if re.search(r"(?:范围|range|分辨率|resolution|单位|unit|取值|映射|代表|含义|用途|位段|bit)", normalized_answer, flags=re.IGNORECASE):
        spec_signal_count += 1
    if re.search(r"(?:表示|用于|说明|代表|meaning|used for)", normalized_answer, flags=re.IGNORECASE):
        spec_signal_count += 1
    if "映射" in normalized_question or "取值" in normalized_question or "代表什么" in normalized_question:
        if not re.search(r"(?:=|->|→|映射|取值|代表)", normalized_answer, flags=re.IGNORECASE):
            return True
    if spec_signal_count < 2:
        matched_field = match_field_name(question, field_context or {})
        if matched_field:
            details = str((field_context or {}).get(matched_field, {}).get("details") or "")
            if _extract_enum_mapping_text_from_context(details, limit=2):
                return True
        return True
    return False


def _field_signal_score(field_name: str, info: Dict[str, str]) -> float:
    bit_segment = str(info.get("bit_segment") or "").strip()
    details = str(info.get("details") or "").strip()
    score = 0.0
    if bit_segment:
        score += 2.0
    if re.search(r"\d", details):
        score += 2.0
    if re.search(r"(?:range|范围|resolution|分辨率|unit|单位|bit|位)", details, flags=re.IGNORECASE):
        score += 2.0
    if re.search(r"(?:meaning|含义|代表|用途|enum|取值)", details, flags=re.IGNORECASE):
        score += 1.5
    if contains_mapping_relation(details):
        score += 2.0
    if contains_arithmetic_expression(details):
        score += 2.0
    score += min(len(details) / 80.0, 2.0)
    if len(field_name) <= 2:
        score -= 1.5
    return round(score, 4)


def _classify_chunk_pattern(content: str) -> str:
    normalized = str(content or "").upper()
    if FIELD_CODING_HINT_PATTERN.search(normalized):
        return "field_coding"
    if "PURPOSE" in normalized:
        return "message_summary"
    if "DATA ELEMENT SUMMARY" in normalized:
        return "data_element_summary"
    if "MESSAGE SUMMARY" in normalized:
        return "message_summary"
    if re.search(r"(?:REFERENCE DFI/DUI|BIT POSITION|RESOLUTION, CODING, ETC)", normalized):
        return "reference_layout"
    if "WORD MAP" in normalized:
        return "word_map"
    if "WORD DESCRIPTION" in normalized:
        return "word_description"
    return "general"


def _build_algorithmic_seed_pairs(
    content: str,
    field_context: Dict[str, Dict[str, str]],
    chunk_pattern: str,
    target_count: int,
) -> List[Dict[str, Any]]:
    seeds: List[Dict[str, Any]] = []
    seen_questions: Set[str] = set()

    def add_pair(
        question: str,
        answer: str,
        qa_task_type: str = "protocol_understanding",
        *,
        source_field: Optional[str] = None,
        seed_kind: Optional[str] = None,
    ) -> None:
        normalized_question = _normalize_text_key(question)
        if not question or not answer or normalized_question in seen_questions:
            return
        seen_questions.add(normalized_question)
        seeds.append({
            "question": question,
            "answer": answer,
            "qa_task_type": qa_task_type,
            "conversion_mode": None,
            "conversion_formula": None,
            "source_field": source_field,
            "seed_kind": seed_kind,
        })

    ranked_fields = sorted(
        field_context.items(),
        key=lambda item: _field_signal_score(item[0], item[1]),
        reverse=True,
    )
    pure_mapping_chunk = _is_pure_field_coding_mapping_chunk(content, chunk_pattern)

    if chunk_pattern == "field_coding":
        if pure_mapping_chunk:
            simple_rows = _extract_simple_mapping_rows(content, limit=6)
            if simple_rows:
                preview = "，".join(f"{left}对应{right}" for left, right in simple_rows[:4]) + "。"
                add_pair("该编码表中的关键编码值与字符映射是什么？", preview, seed_kind="simple_mapping")
            return seeds[:target_count]
        for field_name, info in ranked_fields:
            if _is_enum_value_label(field_name, info):
                continue
            details = str(info.get("details") or "").strip()
            enum_pairs = _extract_enum_pairs(details, limit=4)
            if enum_pairs:
                answer = "，".join(f"{value}表示{label}" for value, label in enum_pairs[:4]) + "。"
                add_pair(
                    f"{field_name}字段有哪些关键取值及其含义？",
                    answer,
                    source_field=field_name,
                    seed_kind="enum_mapping",
                )
                for value, label in enum_pairs[: min(3, len(enum_pairs))]:
                    add_pair(
                        f"{field_name}的取值{value}表示什么？",
                        f"{field_name}取值{value}表示{label}。",
                        source_field=field_name,
                        seed_kind="enum_mapping",
                    )
                if len(seeds) >= target_count:
                    return seeds[:target_count]
            bit_width = _extract_bit_width_from_context(info)
            if bit_width is not None and bit_width <= 64 and not enum_pairs:
                add_pair(
                    f"{field_name}字段占用多少位？",
                    f"{field_name}字段占用{bit_width}位。",
                    source_field=field_name,
                    seed_kind="bit_width",
                )
                if len(seeds) >= target_count:
                    return seeds[:target_count]
        simple_rows = _extract_simple_mapping_rows(content, limit=6)
        if simple_rows and not seeds:
            preview = "，".join(f"{left}对应{right}" for left, right in simple_rows[:4]) + "。"
            add_pair("该编码表中的关键编码值与字符映射是什么？", preview, seed_kind="simple_mapping")
            if len(seeds) >= target_count:
                return seeds[:target_count]

    if chunk_pattern in {"data_element_summary", "reference_layout", "word_map", "word_description"}:
        for field_name, info in ranked_fields:
            bit_width = _extract_bit_width_from_context(info)
            details = str(info.get("details") or "").strip()
            bit_segment = str(info.get("bit_segment") or "").strip()
            word_anchor = _extract_word_anchor_from_text(details)
            if bit_width is not None:
                question_text = f"{field_name}字段占用多少位？"
                answer_parts: List[str] = []
                if chunk_pattern == "data_element_summary":
                    if word_anchor:
                        question_text = f"{word_anchor}中的{field_name}字段占用多少位？"
                        answer_parts.append(f"在{word_anchor}数据元素摘要中")
                    else:
                        answer_parts.append("在该数据元素摘要中")
                elif chunk_pattern == "word_map":
                    if word_anchor:
                        question_text = f"{word_anchor}中的{field_name}字段占用多少位？"
                        answer_parts.append(f"在{word_anchor}字布局中")
                elif chunk_pattern == "reference_layout":
                    if word_anchor:
                        question_text = f"{word_anchor}中的{field_name}字段占用多少位？"
                        answer_parts.append(f"在{word_anchor}参考布局中")
                answer_parts.append(f"{field_name}字段占用{bit_width}位")
                if bit_segment and bit_segment != str(bit_width):
                    answer_parts.append(f"位段为{bit_segment}")
                add_pair(
                    question_text,
                    "，".join(answer_parts) + "。",
                    source_field=field_name,
                    seed_kind="bit_width",
                )
                if len(seeds) >= target_count:
                    return seeds[:target_count]
            range_text = _extract_range_text_from_context(details)
            if range_text and bit_width is not None:
                question_text = f"{field_name}字段的范围是多少？"
                answer_prefix = ""
                if chunk_pattern == "data_element_summary":
                    if word_anchor:
                        question_text = f"{word_anchor}中的{field_name}字段范围是多少？"
                        answer_prefix = f"在{word_anchor}数据元素摘要中，"
                    else:
                        answer_prefix = "在该数据元素摘要中，"
                elif chunk_pattern == "word_map" and word_anchor:
                    question_text = f"{word_anchor}中的{field_name}字段范围是多少？"
                    answer_prefix = f"在{word_anchor}字布局中，"
                elif chunk_pattern == "reference_layout" and word_anchor:
                    question_text = f"{word_anchor}中的{field_name}字段范围是多少？"
                    answer_prefix = f"在{word_anchor}参考布局中，"
                add_pair(
                    question_text,
                    f"{answer_prefix}{field_name}字段{range_text}，占用{bit_width}位。",
                    source_field=field_name,
                    seed_kind="range",
                )
                if len(seeds) >= target_count:
                    return seeds[:target_count]
            unit_text = _extract_unit_text_from_context(details)
            if unit_text and bit_width is not None:
                question_text = f"{field_name}字段使用什么单位？"
                answer_prefix = ""
                if chunk_pattern == "data_element_summary":
                    if word_anchor:
                        question_text = f"{word_anchor}中的{field_name}字段使用什么单位？"
                        answer_prefix = f"在{word_anchor}数据元素摘要中，"
                    else:
                        answer_prefix = "在该数据元素摘要中，"
                elif chunk_pattern == "word_map" and word_anchor:
                    question_text = f"{word_anchor}中的{field_name}字段使用什么单位？"
                    answer_prefix = f"在{word_anchor}字布局中，"
                elif chunk_pattern == "reference_layout" and word_anchor:
                    question_text = f"{word_anchor}中的{field_name}字段使用什么单位？"
                    answer_prefix = f"在{word_anchor}参考布局中，"
                add_pair(
                    question_text,
                    f"{answer_prefix}{field_name}字段{unit_text}，占用{bit_width}位。",
                    source_field=field_name,
                    seed_kind="unit",
                )
                if len(seeds) >= target_count:
                    return seeds[:target_count]
            resolution_text = _extract_resolution_text_from_context(details)
            if resolution_text and bit_width is not None:
                question_text = f"{field_name}字段的分辨率是多少？"
                answer_prefix = ""
                if chunk_pattern == "data_element_summary":
                    if word_anchor:
                        question_text = f"{word_anchor}中的{field_name}字段分辨率是多少？"
                        answer_prefix = f"在{word_anchor}数据元素摘要中，"
                    else:
                        answer_prefix = "在该数据元素摘要中，"
                elif chunk_pattern == "word_map" and word_anchor:
                    question_text = f"{word_anchor}中的{field_name}字段分辨率是多少？"
                    answer_prefix = f"在{word_anchor}字布局中，"
                elif chunk_pattern == "reference_layout" and word_anchor:
                    question_text = f"{word_anchor}中的{field_name}字段分辨率是多少？"
                    answer_prefix = f"在{word_anchor}参考布局中，"
                add_pair(
                    question_text,
                    f"{answer_prefix}{field_name}字段{resolution_text}，占用{bit_width}位。",
                    source_field=field_name,
                    seed_kind="resolution",
                )
                if len(seeds) >= target_count:
                    return seeds[:target_count]
        if chunk_pattern == "word_map":
            word_number_match = re.search(r"WORD NUMBER:\s*([A-Z0-9.]+)", str(content or ""), flags=re.IGNORECASE)
            title_match = re.search(r"WORD TITLE:\s*([A-Z0-9,()/\- ]+)", str(content or ""), flags=re.IGNORECASE)
            if word_number_match and title_match:
                add_pair(
                    f"{word_number_match.group(1)}对应的字名称是什么？",
                    f"{word_number_match.group(1)}对应的字名称是{title_match.group(1).strip()}。",
                    seed_kind="word_title",
                )
        if chunk_pattern == "data_element_summary":
            compact = re.sub(r"\s+", " ", str(content or "")).strip()
            summary_rows = re.findall(
                r"([A-Z][A-Z0-9,()/\- ]{2,}?)\s*\|\s*(\d{1,3})",
                compact,
                flags=re.IGNORECASE,
            )
            for raw_field_name, raw_width in summary_rows:
                field_name = _normalize_field_name(raw_field_name)
                if not _looks_like_field_name(field_name):
                    continue
                bit_width = int(raw_width)
                add_pair(
                    f"{field_name}字段占用多少位？",
                    f"{field_name}字段占用{bit_width}位。",
                    source_field=field_name,
                    seed_kind="bit_width",
                )
                if len(seeds) >= target_count:
                    return seeds[:target_count]

    if chunk_pattern == "message_summary":
        compact = re.sub(r"\s+", " ", str(content or "")).strip()
        message_match = re.search(r"\b(J\d+\.\d+[A-Z0-9]*)\b", compact)
        purpose_match = re.search(r"PURPOSE\s+(.*?)(?:DATA ELEMENT SUMMARY|$)", compact, flags=re.IGNORECASE)
        if message_match and purpose_match:
            message_name = message_match.group(1)
            purpose_text = purpose_match.group(1).strip(" .")
            if purpose_text:
                add_pair(
                    f"{message_name}消息的主要用途是什么？",
                    f"{message_name}消息主要用于{purpose_text}。",
                    seed_kind="message_purpose",
                )
                if "acknowledge" in purpose_text.lower() or "receipt/compliance" in purpose_text.lower():
                    add_pair(
                        f"{message_name}消息是否支持回执或执行确认？",
                        f"{message_name}消息支持通过receipt/compliance action进行回执或执行确认。",
                        seed_kind="message_purpose",
                    )
    return _prioritize_seed_pairs(
        seeds,
        target_count,
        chunk_pattern=chunk_pattern,
    )


def _build_question_type_catalog(
    chunk_pattern: str,
    field_context: Dict[str, Dict[str, str]],
    conversion_candidates: List[Dict[str, str]],
) -> List[str]:
    catalog: List[str] = []
    has_bit_width = False
    has_layout = False
    has_range = False
    has_unit = False
    has_resolution = False
    has_enum = False

    for field_name, info in field_context.items():
        if _is_enum_value_label(field_name, info):
            continue
        details = str(info.get("details") or "").strip()
        bit_segment = str(info.get("bit_segment") or "").strip()
        if _extract_bit_width_from_context(info) is not None:
            has_bit_width = True
        if bit_segment and bit_segment != str(_extract_bit_width_from_context(info) or ""):
            has_layout = True
        if _extract_range_text_from_context(details):
            has_range = True
        if _extract_unit_text_from_context(details):
            has_unit = True
        if _extract_resolution_text_from_context(details):
            has_resolution = True
        if _extract_enum_pairs(details, limit=2):
            has_enum = True

    if has_enum or chunk_pattern == "field_coding":
        catalog.append("enum_meaning: 询问字段关键取值及含义，适用于编码表、枚举表、离散值说明")
    if has_range:
        catalog.append("range: 询问字段数值范围、上下限或可表示区间")
    if has_unit:
        catalog.append("unit: 询问字段单位或量纲")
    if has_resolution:
        catalog.append("resolution: 询问字段分辨率、精度或最小增量")
    if has_layout or chunk_pattern in {"reference_layout", "word_map"}:
        catalog.append("layout: 询问字段位段、起止位置或布局关系")
    if has_bit_width:
        catalog.append("bit_width: 询问字段占用位数或位宽")
    if chunk_pattern == "word_map":
        catalog.append("word_title: 询问字编号对应的字名称或字标题")
    if chunk_pattern == "message_summary":
        catalog.append("message_purpose: 询问消息用途、场景或是否支持回执/执行确认")
    if any(item.get("mode") == "transcoding" for item in conversion_candidates):
        catalog.append("conversion_formula: 询问原始值到物理值的转换公式")
    if any(item.get("mode") == "mapping" for item in conversion_candidates):
        catalog.append("conversion_mapping: 询问离散值或跨语义映射关系")
    return catalog


def _question_plan_matches_allowed_evidence(
    item: Dict[str, Any],
    field_context: Dict[str, Dict[str, str]],
    field_intent_map: Dict[str, List[str]],
    topic_context: Optional[List[str]] = None,
) -> bool:
    question = str(item.get("question") or "").strip()
    qa_task_type = normalize_task_type(item.get("qa_task_type") or item.get("task_type"))
    if not question:
        return False
    intent = _question_intent_bucket(question, qa_task_type)
    if qa_task_type == "protocol_conversion":
        return intent in {"conversion_formula", "conversion_mapping", "conversion_other"}

    source_field = str(item.get("source_field") or "").strip().upper()
    matched_field = source_field or match_field_name(question, field_context) or ""
    if matched_field and matched_field in field_intent_map:
        allowed = set(field_intent_map.get(matched_field) or [])
        if intent in {"other", "meaning"}:
            return bool(allowed)
        return intent in allowed
    if intent in {"message_purpose", "other", "meaning"} and topic_context:
        normalized_question = question.upper()
        return any(str(topic).upper() in normalized_question for topic in topic_context)
    return False


def _build_chunk_generation_plan(
    content: str,
    count: int,
    requested_task_types: List[str],
    requested_conversion_modes: List[str],
) -> Dict[str, Any]:
    field_context = build_field_context(content)
    topic_context = build_topic_context(content)
    chunk_pattern = _classify_chunk_pattern(content)
    field_intent_map = _build_field_intent_map(chunk_pattern, field_context)
    ranked_fields: List[Tuple[str, Dict[str, str], float]] = sorted(
        (
            (field_name, info, _field_signal_score(field_name, info))
            for field_name, info in field_context.items()
        ),
        key=lambda item: item[2],
        reverse=True,
    )

    understanding_fields: List[str] = []
    conversion_candidates: List[Dict[str, str]] = []
    enum_candidates: List[str] = []
    pure_mapping_chunk = _is_pure_field_coding_mapping_chunk(content, chunk_pattern)
    for field_name, info, score in ranked_fields:
        if score <= 0:
            continue
        if _is_enum_value_label(field_name, info):
            continue
        details = str(info.get("details") or "").strip()
        bit_segment = str(info.get("bit_segment") or "").strip()
        enum_mapping_text = _extract_enum_mapping_text_from_context(details, limit=3)
        if bit_segment or re.search(r"\d", details) or enum_mapping_text:
            understanding_fields.append(field_name)
        if re.search(r"(?:=|->|→)", details) or enum_mapping_text:
            enum_candidates.append(field_name)
        if contains_mapping_relation(details) and not (chunk_pattern == "field_coding" and enum_mapping_text):
            conversion_candidates.append({
                "field_name": field_name,
                "mode": "mapping",
                "evidence": extract_formula_only(details),
            })
        elif _has_conversion_evidence(details):
            conversion_candidates.append({
                "field_name": field_name,
                "mode": "transcoding",
                "evidence": extract_formula_only(details),
            })

    unique_conversion_candidates: List[Dict[str, str]] = []
    seen_conversion_fields: Set[str] = set()
    for candidate in conversion_candidates:
        field_name = candidate["field_name"]
        if field_name in seen_conversion_fields:
            continue
        seen_conversion_fields.add(field_name)
        unique_conversion_candidates.append(candidate)

    effective_task_types = list(requested_task_types)
    effective_conversion_modes = list(requested_conversion_modes)
    if pure_mapping_chunk:
        effective_task_types = [task for task in effective_task_types if task != "protocol_conversion"]
        effective_conversion_modes = []
    if "protocol_conversion" in effective_task_types and not unique_conversion_candidates:
        effective_task_types = [task for task in effective_task_types if task != "protocol_conversion"]
    if not effective_task_types:
        effective_task_types = ["protocol_understanding"]
    if "protocol_conversion" not in effective_task_types:
        effective_conversion_modes = []

    understanding_target = count
    conversion_target = 0
    if (
        "protocol_understanding" in effective_task_types
        and "protocol_conversion" in effective_task_types
        and unique_conversion_candidates
    ):
        conversion_target = min(
            max(1, count // 3),
            len(unique_conversion_candidates),
            max(1, count - 1),
        )
        understanding_target = max(1, count - conversion_target)
    elif "protocol_conversion" in effective_task_types:
        conversion_target = min(count, len(unique_conversion_candidates))
        if conversion_target <= 0:
            effective_task_types = ["protocol_understanding"]
            understanding_target = count
        else:
            understanding_target = 0

    plan_lines: List[str] = []
    if understanding_fields:
        plan_lines.append(
            "优先围绕以下真实字段生成协议理解题（不要跳出此列表）："
        )
        for field_name in understanding_fields[: max(4, min(12, count * 2))]:
            info = field_context.get(field_name, {})
            hint_parts = []
            if info.get("bit_segment"):
                hint_parts.append(f"位段={info['bit_segment']}")
            if info.get("details"):
                hint_parts.append(f"证据={str(info['details'])[:120]}")
            plan_lines.append(f"- {field_name}: {'; '.join(hint_parts)}")
    if enum_candidates:
        plan_lines.append("如果字段存在离散取值/枚举语义，优先从以下字段生成取值含义题：")
        for field_name in enum_candidates[: min(6, len(enum_candidates))]:
            details = str(field_context.get(field_name, {}).get("details") or "")
            mapping_hint = _extract_enum_mapping_text_from_context(details, limit=3)
            if mapping_hint:
                plan_lines.append(f"- {field_name}: {mapping_hint}")
            else:
                plan_lines.append(f"- {field_name}")
    if unique_conversion_candidates and "protocol_conversion" in effective_task_types:
        plan_lines.append("仅允许围绕以下真实转换证据生成协议转换题：")
        for candidate in unique_conversion_candidates[: max(2, min(8, count))]:
            plan_lines.append(
                f"- {candidate['field_name']} | mode={candidate['mode']} | evidence={candidate['evidence'][:120]}"
            )
    question_type_catalog = _build_question_type_catalog(
        chunk_pattern,
        field_context,
        unique_conversion_candidates,
    )
    if question_type_catalog:
        plan_lines.append("本块允许的大类题型如下，请先选择适合当前块证据的题型，再自行构造具体问题：")
        for item in question_type_catalog:
            plan_lines.append(f"- {item}")
    if field_intent_map:
        plan_lines.append("字段级题型开放约束如下，只能为字段选择其已开放的题型：")
        for field_name, intents in list(field_intent_map.items())[: max(4, min(10, count * 2))]:
            plan_lines.append(f"- {field_name}: {', '.join(intents)}")
    if not understanding_fields and topic_context:
        plan_lines.append("当前块缺少结构化字段表，不要为了补数量生成章节归属、附录归属或文档描述类问题：")
        for topic in topic_context[: min(8, len(topic_context))]:
            plan_lines.append(f"- {topic}")
        plan_lines.append("若无法生成围绕真实字段规格、枚举语义或结构关系的理解题，则跳过该块。")
    if chunk_pattern == "field_coding":
        plan_lines.append("这是字段编码/枚举块，优先生成取值含义题，不要只生成位宽题。")
    elif chunk_pattern in {"data_element_summary", "reference_layout", "word_map"}:
        plan_lines.append("这是结构化字段摘要/布局块，优先生成位宽、位段、范围、单位、分辨率等不同题型，不要让位宽题占满大多数名额。")
    plan_lines.append("若同一字段同时具备范围、单位、分辨率、枚举或结构证据，优先这些信息量更高的问题，再考虑位宽题。")
    plan_lines.append("问题类型必须尽量分散；除非证据不足，位宽类问题不要超过总问题数的一半。")
    plan_lines.append(
        "禁止生成未出现在上述字段/规则清单中的问题；禁止复用同一字段的同一问题意图。"
    )

    algorithmic_seed_pairs = _build_algorithmic_seed_pairs(
        content=content,
        field_context=field_context,
        chunk_pattern=chunk_pattern,
        target_count=max(count * 2, 6),
    )

    return {
        "chunk_pattern": chunk_pattern,
        "field_context": field_context,
        "topic_context": topic_context,
        "understanding_fields": understanding_fields,
        "conversion_candidates": unique_conversion_candidates,
        "enum_candidates": enum_candidates,
        "effective_task_types": effective_task_types,
        "effective_conversion_modes": effective_conversion_modes,
        "understanding_target": understanding_target,
        "conversion_target": conversion_target,
        "instruction": "\n".join(plan_lines).strip(),
        "algorithmic_seed_pairs": algorithmic_seed_pairs,
        "field_intent_map": field_intent_map,
    }


def _estimate_unit_generation_weight(
    content: str,
    generation_plan: Dict[str, Any],
    *,
    is_requirement_chunk: bool,
) -> float:
    field_context = generation_plan.get("field_context") or {}
    understanding_fields = generation_plan.get("understanding_fields") or []
    conversion_candidates = generation_plan.get("conversion_candidates") or []
    topic_context = generation_plan.get("topic_context") or []

    weight = 1.0
    weight += min(4.0, len(understanding_fields) * 0.35)
    weight += min(3.5, len(conversion_candidates) * 0.6)
    weight += min(2.0, len(field_context) * 0.15)
    weight += min(1.5, len(topic_context) * 0.1)
    weight += min(2.0, len(str(content or "")) / 1200.0)
    if is_requirement_chunk:
        weight += 0.8
    return round(max(0.5, weight), 4)


def _estimate_unit_supported_capacity(
    content: str,
    generation_plan: Dict[str, Any],
    *,
    is_requirement_chunk: bool,
) -> int:
    seed_pairs = generation_plan.get("algorithmic_seed_pairs") or []
    field_context = generation_plan.get("field_context") or {}
    chunk_pattern = str(generation_plan.get("chunk_pattern") or "general")
    understanding_fields = generation_plan.get("understanding_fields") or []
    conversion_candidates = generation_plan.get("conversion_candidates") or []
    topic_context = generation_plan.get("topic_context") or []

    if is_requirement_chunk:
        return max(1, min(3, len(seed_pairs) or 1))

    structured_capacity = 0
    for field_name, info in field_context.items():
        if _is_enum_value_label(field_name, info):
            continue
        details = str(info.get("details") or "").strip()
        bit_width = _extract_bit_width_from_context(info)
        enum_pairs = _extract_enum_pairs(details, limit=4)
        range_text = _extract_range_text_from_context(details)
        unit_text = _extract_unit_text_from_context(details)
        resolution_text = _extract_resolution_text_from_context(details)
        per_field_capacity = 0
        if enum_pairs:
            per_field_capacity += 1
        if bit_width is not None:
            per_field_capacity += 1
        if range_text:
            per_field_capacity += 1
        if unit_text:
            per_field_capacity += 1
        if resolution_text:
            per_field_capacity += 1
        if contains_mapping_relation(details) or contains_arithmetic_expression(details):
            per_field_capacity += 1
        structured_capacity += min(3, per_field_capacity)

    capacity = max(len(seed_pairs), structured_capacity)
    capacity += min(3, len(conversion_candidates))
    if chunk_pattern == "message_summary":
        capacity = max(capacity, 1 if seed_pairs else 0)
    elif chunk_pattern == "word_description":
        capacity = max(capacity, 1 if seed_pairs else 0)
    elif chunk_pattern in {"word_map", "reference_layout", "field_coding", "data_element_summary"}:
        capacity = max(capacity, min(10, len(understanding_fields) + len(seed_pairs)))
    elif chunk_pattern == "general":
        capacity = max(capacity, min(3, len(topic_context)))

    if len(str(content or "")) >= 1200:
        capacity += 1
    if len(str(content or "")) >= 2400:
        capacity += 1

    if chunk_pattern == "general" and not seed_pairs and len(understanding_fields) <= 2 and not conversion_candidates:
        capacity = min(capacity, 2)

    return max(0, min(12, int(capacity)))


def _allocate_unit_target_counts(
    weighted_units: List[Dict[str, Any]],
    average_count: int,
) -> Dict[str, int]:
    if not weighted_units:
        return {}

    base_average = max(1, int(average_count or 1))
    total_budget = base_average * len(weighted_units)
    total_weight = sum(float(unit.get("allocation_weight", 0.0) or 0.0) for unit in weighted_units)
    if total_weight <= 0:
        total_weight = float(len(weighted_units))

    allocations: Dict[str, int] = {}
    remainders: List[Tuple[float, str]] = []
    assigned_total = 0
    global_min_target = 1
    global_max_target = max(base_average + 2, int(round(base_average * 1.8)))
    indexed_units = {str(unit.get("unit_id") or ""): unit for unit in weighted_units}

    for unit in weighted_units:
        unit_id = str(unit.get("unit_id") or "")
        unit_min_target = max(global_min_target, int(unit.get("minimum_target", global_min_target) or global_min_target))
        raw_supported_capacity = int(unit.get("supported_capacity", global_max_target) or 0)
        supported_capacity = raw_supported_capacity if raw_supported_capacity > 0 else 0
        if supported_capacity <= 0:
            allocations[unit_id] = 0
            remainders.append((0.0, unit_id))
            continue
        unit_max_target = max(unit_min_target, min(global_max_target, supported_capacity))
        raw_share = total_budget * (float(unit.get("allocation_weight", 0.0) or 0.0) / total_weight)
        target = int(raw_share)
        if target < unit_min_target:
            target = unit_min_target
        if target > unit_max_target:
            target = unit_max_target
        allocations[unit_id] = target
        assigned_total += target
        remainders.append((raw_share - int(raw_share), unit_id))

    if assigned_total < total_budget:
        progress = True
        while assigned_total < total_budget and progress:
            progress = False
            for _remainder, unit_id in sorted(remainders, reverse=True):
                if assigned_total >= total_budget:
                    break
                unit = indexed_units.get(unit_id)
                unit_min_target = max(global_min_target, int((unit or {}).get("minimum_target", global_min_target) or global_min_target))
                raw_supported_capacity = int((unit or {}).get("supported_capacity", global_max_target) or 0)
                supported_capacity = raw_supported_capacity if raw_supported_capacity > 0 else 0
                if supported_capacity <= 0:
                    continue
                unit_max_target = max(unit_min_target, min(global_max_target, supported_capacity))
                if allocations[unit_id] >= unit_max_target:
                    continue
                allocations[unit_id] += 1
                assigned_total += 1
                progress = True
    elif assigned_total > total_budget:
        progress = True
        while assigned_total > total_budget and progress:
            progress = False
            for _remainder, unit_id in sorted(remainders):
                if assigned_total <= total_budget:
                    break
                unit = indexed_units.get(unit_id)
                unit_min_target = max(global_min_target, int((unit or {}).get("minimum_target", global_min_target) or global_min_target))
                if allocations[unit_id] <= unit_min_target:
                    continue
                allocations[unit_id] -= 1
                assigned_total -= 1
                progress = True

    return allocations


def _fast_probe_unit(content: str) -> Dict[str, Any]:
    """用轻量规则探测块内是否存在可支撑 QA 的信号。"""
    text = str(content or "").strip()
    if not text:
        return {
            "word_count": 0,
            "char_count": 0,
            "has_numeric": False,
            "has_enum": False,
            "has_layout_signal": False,
            "has_formula_signal": False,
            "has_structured_signal": False,
            "has_topic_signal": False,
        }

    word_count = len(re.findall(r"\S+", text))
    char_count = len(text)
    has_numeric = bool(FAST_NUMERIC_SIGNAL_PATTERN.search(text) or re.search(r"\b\d+\b", text))
    has_enum = bool(FAST_ENUM_SIGNAL_PATTERN.search(text))
    has_layout_signal = bool(re.search(r"(?:BIT POSITION|# BITS|bits?\b|位段|位宽|WORD MAP|WORD NUMBER)", text, flags=re.IGNORECASE))
    has_formula_signal = bool(re.search(r"(?:=|->|→|formula|公式|mapping|映射|scale|resolution|分辨率)", text, flags=re.IGNORECASE))
    has_structured_signal = bool(STRUCTURED_BLOCK_HINT_PATTERN.search(text) or FIELD_CODING_HINT_PATTERN.search(text))
    has_topic_signal = bool(re.search(r"\bJ\d+\.\d+[A-Z0-9]*\b", text, flags=re.IGNORECASE))
    return {
        "word_count": word_count,
        "char_count": char_count,
        "has_numeric": has_numeric,
        "has_enum": has_enum,
        "has_layout_signal": has_layout_signal,
        "has_formula_signal": has_formula_signal,
        "has_structured_signal": has_structured_signal,
        "has_topic_signal": has_topic_signal,
    }


def _estimate_fast_supported_capacity(probe: Dict[str, Any], content: str) -> int:
    """基于轻量特征估计单块可支撑的 QA 上限。"""
    char_count = int(probe.get("char_count") or 0)
    word_count = int(probe.get("word_count") or 0)
    if char_count <= 0 or word_count <= 0:
        return 0

    capacity = 1
    if probe.get("has_structured_signal"):
        capacity += 2
    if probe.get("has_numeric"):
        capacity += 1
    if probe.get("has_enum"):
        capacity += 1
    if probe.get("has_layout_signal"):
        capacity += 1
    if probe.get("has_formula_signal"):
        capacity += 1
    if char_count >= 600:
        capacity += 1
    if char_count >= 1400:
        capacity += 1
    if char_count >= 2400:
        capacity += 1

    return max(0, min(12, capacity))


def _estimate_fast_unit_priority(
    probe: Dict[str, Any],
    generation_plan: Dict[str, Any],
    content: str,
) -> float:
    """估计一个块在 fast-path 中是否值得占用平均预算。"""
    field_context = generation_plan.get("field_context") or {}
    conversion_candidates = generation_plan.get("conversion_candidates") or []
    enum_candidates = generation_plan.get("enum_candidates") or []
    chunk_pattern = str(generation_plan.get("chunk_pattern") or "general")
    char_count = int(probe.get("char_count") or 0)

    score = 0.0
    score += min(2.5, len(field_context) * 0.18)
    score += min(1.8, len(conversion_candidates) * 0.7)
    score += min(1.6, len(enum_candidates) * 0.45)
    if probe.get("has_structured_signal"):
        score += 0.9
    if probe.get("has_layout_signal"):
        score += 0.5
    if probe.get("has_enum"):
        score += 0.8
    if probe.get("has_formula_signal"):
        score += 0.8
    if char_count >= 500:
        score += 0.4
    if char_count >= 1200:
        score += 0.4
    if chunk_pattern in {"word_map", "reference_layout", "field_coding", "data_element_summary"}:
        score += 0.6
    elif chunk_pattern == "message_summary":
        score += 0.2
    elif chunk_pattern == "word_description":
        score -= 0.4
    return round(max(0.0, score), 4)


def _compute_fast_unit_target_counts(prepared_units: List[Dict[str, Any]], average_count: int) -> Dict[str, int]:
    """用基础配额 + 长度系数 + 支撑上限分配单块目标数。"""
    if not prepared_units:
        return {}

    base_average = max(1, int(average_count or 1))
    base_quota = min(3, base_average)
    allocations: Dict[str, int] = {}
    expansion_room: List[Tuple[int, int, str]] = []
    shrink_order: List[Tuple[int, int, str]] = []
    total_assigned = 0

    max_word_count = max(int(item.get("probe", {}).get("word_count") or 0) for item in prepared_units) or 1
    budget_units = [
        item for item in prepared_units
        if float(item.get("priority_score") or 0.0) >= 1.6
        or int(item.get("supported_capacity") or 0) >= max(4, base_average)
    ]
    total_budget = base_average * max(1, len(budget_units))

    for item in prepared_units:
        unit_id = str(item.get("unit_id") or "")
        supported_capacity = max(0, int(item.get("supported_capacity") or 0))
        priority_score = float(item.get("priority_score") or 0.0)
        minimum_target = max(0, int(item.get("minimum_target", 1 if priority_score >= 1.6 else 0) or 0))
        if supported_capacity <= 0:
            allocations[unit_id] = 0
            continue

        word_count = int(item.get("probe", {}).get("word_count") or 0)
        length_factor = 0.55 + min(0.75, word_count / max_word_count)
        raw_quota = int(round(base_quota * length_factor))
        if priority_score >= 3.0:
            raw_quota += 1
        quota = max(minimum_target, raw_quota)
        quota = min(quota, supported_capacity)
        allocations[unit_id] = quota
        total_assigned += quota
        expansion_room.append((supported_capacity - quota, word_count, unit_id))
        shrink_order.append((word_count, supported_capacity, unit_id))

    if total_assigned < total_budget:
        for room, word_count, unit_id in sorted(expansion_room, key=lambda item: (item[0], item[1]), reverse=True):
            while room > 0 and total_assigned < total_budget:
                allocations[unit_id] += 1
                room -= 1
                total_assigned += 1
    elif total_assigned > total_budget:
        for word_count, _supported_capacity, unit_id in sorted(shrink_order):
            minimum_target = max(0, int(next(
                (item.get("minimum_target") or 0) for item in prepared_units if str(item.get("unit_id") or "") == unit_id
            )))
            while allocations[unit_id] > minimum_target and total_assigned > total_budget:
                allocations[unit_id] -= 1
                total_assigned -= 1

    return allocations


def _build_fast_generation_instruction(
    generation_plan: Dict[str, Any],
    probe: Dict[str, Any],
) -> str:
    """为单块生成 batch prompt 辅助说明。"""
    hints: List[str] = []
    chunk_pattern = str(generation_plan.get("chunk_pattern") or "general")
    understanding_fields = generation_plan.get("understanding_fields") or []
    enum_candidates = generation_plan.get("enum_candidates") or []
    conversion_candidates = generation_plan.get("conversion_candidates") or []
    question_type_catalog = _build_question_type_catalog(
        chunk_pattern,
        generation_plan.get("field_context") or {},
        conversion_candidates,
    )

    if understanding_fields:
        hints.append(f"优先围绕真实字段提问: {', '.join(understanding_fields[:6])}")
    if enum_candidates:
        hints.append(f"可优先生成枚举含义题: {', '.join(enum_candidates[:4])}")
    if conversion_candidates:
        hints.append("若片段中存在明确转换或映射证据，可生成少量转换题。")
    if question_type_catalog:
        hints.append(f"优先题型: {', '.join(item.split(':', 1)[0] for item in question_type_catalog[:5])}")
    if probe.get("has_layout_signal"):
        hints.append("该片段适合位宽、位段、结构关系类问题。")
    if probe.get("has_numeric"):
        hints.append("优先使用片段中的具体数值、范围或单位来回答。")
    if chunk_pattern == "field_coding":
        hints.append("这是字段编码/枚举片段，不要只生成位宽题。")
        hints.append("若只看到枚举说明，优先问取值含义，不要问范围。")
    elif chunk_pattern == "message_summary":
        hints.append("这是消息用途摘要片段，只生成用途、场景、回执/确认机制类问题。")
        hints.append("不要生成位宽、位段、范围、单位类问题。")
    elif chunk_pattern == "word_description":
        hints.append("这是字级结构说明片段，优先问结构组成、字段布局、字宽，不要泛化到不存在的范围或单位。")
    elif chunk_pattern in {"word_map", "reference_layout"}:
        hints.append("若字段只给了位段/位宽而没有明确范围或单位，不要生成范围/单位题。")
    elif chunk_pattern == "data_element_summary":
        hints.append("优先问字段属于哪个字、字段位宽、结构位置，不要臆造枚举值。")
    return "\n".join(hints).strip()


def _build_generation_batches(prepared_units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按字符预算将多个块合并成一个 batch。"""
    batches: List[Dict[str, Any]] = []
    current_units: List[Dict[str, Any]] = []
    current_chars = 0
    current_family = ""

    def unit_family(item: Dict[str, Any]) -> str:
        pattern = str(item.get("chunk_pattern") or "general")
        if pattern in {"field_coding"}:
            return "field_coding"
        if pattern in {"word_map", "reference_layout", "data_element_summary"}:
            return "layout"
        if pattern in {"message_summary", "word_description"}:
            return "summary"
        return "general"

    def flush_batch() -> None:
        nonlocal current_units, current_chars, current_family
        if not current_units:
            return
        batch_index = len(batches) + 1
        segment_quota_lines: List[str] = []
        content_parts: List[str] = []
        total_target = 0
        for item in current_units:
            unit_id = str(item.get("unit_id") or "")
            quota = int(item.get("target_count") or 0)
            total_target += quota
            segment_quota_lines.append(f"- {unit_id}: 建议候选数 {max(quota * FAST_OVERGEN_FACTOR, quota + 1)}")
            content_parts.append(f"[SEGMENT_ID: {unit_id}]\n{item.get('content')}")
        batches.append({
            "batch_id": f"batch_{batch_index}",
            "units": list(current_units),
            "batch_target_total": total_target,
            "candidate_count": max(total_target * FAST_OVERGEN_FACTOR, total_target + len(current_units)),
            "segment_quota_text": "\n".join(segment_quota_lines),
            "prompt_context_text": "\n\n".join(content_parts),
        })
        current_units = []
        current_chars = 0
        current_family = ""

    for item in prepared_units:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        family = unit_family(item)
        projected = current_chars + len(content)
        current_units_count = len(current_units)
        if current_units and (
            projected > FAST_BATCH_MAX_CHARS
            or current_units_count >= FAST_BATCH_MAX_UNITS
            or (current_family and family != current_family and current_units_count >= 2)
        ):
            flush_batch()
        current_units.append(item)
        current_chars += len(content)
        if not current_family:
            current_family = family
        if len(current_units) >= FAST_BATCH_MIN_UNITS and current_chars >= FAST_BATCH_MAX_CHARS:
            flush_batch()

    flush_batch()
    return batches


def _trim_fast_answer_text(answer: str) -> str:
    text = re.sub(r"\s+", " ", str(answer or "").strip())
    return text[:200].strip()


def _normalize_fast_batch_candidates(
    qa_pairs: List[Dict[str, Any]],
    batch_unit_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for qa in qa_pairs:
        segment_id = str(qa.get("segment_id") or "").strip()
        if not segment_id or segment_id not in batch_unit_map:
            continue
        question = str(qa.get("question") or "").strip()
        answer = _trim_fast_answer_text(qa.get("answer") or "")
        if not question or not answer:
            continue
        qa_task_type = normalize_task_type(qa.get("qa_task_type") or qa.get("task_type"))
        conversion_mode = normalize_conversion_mode(qa.get("conversion_mode"))
        conversion_formula = qa.get("conversion_formula")
        source_field = str(qa.get("source_field") or "").strip() or None
        if qa_task_type == "protocol_conversion":
            conversion_payload = normalize_conversion_payload(
                answer=answer,
                conversion_formula=conversion_formula,
                conversion_mode=conversion_mode,
            )
            answer = conversion_payload["answer"]
            conversion_formula = conversion_payload["conversion_formula"]
            conversion_mode = conversion_payload["conversion_mode"]
        else:
            conversion_mode = None
            conversion_formula = None
        normalized.append({
            "segment_id": segment_id,
            "question": question,
            "answer": answer,
            "qa_task_type": qa_task_type,
            "conversion_mode": conversion_mode,
            "conversion_formula": conversion_formula,
            "source_field": source_field,
            "source_fields": normalize_source_fields_value(qa.get("source_fields"), fallback=source_field),
            "extracted_info": None,
        })
    return normalized


def _parse_fast_batch_response(response: str) -> List[Dict[str, Any]]:
    cleaned_response = LocalLLM._sanitize_response_text(response)
    parsed = LocalLLM.parse_json_from_response(cleaned_response, prefer=list)
    if isinstance(parsed, dict):
        for key in ("qa_pairs", "data", "items"):
            maybe_list = parsed.get(key)
            if isinstance(maybe_list, list):
                parsed = maybe_list
                break
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _generate_fast_batch_candidates(
    llm: LocalLLM,
    batch: Dict[str, Any],
    *,
    system_prompt: Optional[str],
    user_instruction: Optional[str],
) -> List[Dict[str, Any]]:
    system, user = PromptTemplates.format_qa_fast_batch_generation(
        content=str(batch.get("prompt_context_text") or ""),
        candidate_count=int(batch.get("candidate_count") or 0),
        segment_quota_text=str(batch.get("segment_quota_text") or ""),
        user_instruction=user_instruction or "",
    )
    if system_prompt:
        system = f"{system}\n\n补充要求：\n{system_prompt}"

    max_new_tokens = max(2048, min(8192, int(batch.get("candidate_count") or 1) * 220))
    response = llm.generate(
        prompt=user,
        system_prompt=system,
        max_new_tokens=max_new_tokens,
        temperature=0.55,
    )
    qa_pairs = _parse_fast_batch_response(response)
    if qa_pairs:
        return qa_pairs

    for _ in range(QA_GENERATION_RETRY):
        retry_user = (
            f"{user}\n\n"
            "重试要求：只返回严格JSON数组；每个元素必须包含segment_id、question、answer。"
        )
        response = llm.generate(
            prompt=retry_user,
            system_prompt=system,
            max_new_tokens=max_new_tokens,
            temperature=0.2,
        )
        qa_pairs = _parse_fast_batch_response(response)
        if qa_pairs:
            return qa_pairs
    return []


def _score_fast_candidate(
    qa: Dict[str, Any],
    unit_info: Dict[str, Any],
) -> int:
    answer = str(qa.get("answer") or "")
    question = str(qa.get("question") or "")
    probe = unit_info.get("probe") or {}
    score = 0
    score += min(20, len(answer))
    if re.search(r"\d", answer):
        score += 8
    if probe.get("has_enum") and re.search(r"(?:表示|对应|=|->|→)", answer, flags=re.IGNORECASE):
        score += 6
    if probe.get("has_layout_signal") and re.search(r"(?:位|bit|bits|位置|BIT POSITION)", answer, flags=re.IGNORECASE):
        score += 4
    if probe.get("has_formula_signal") and qa.get("qa_task_type") == "protocol_conversion":
        score += 6
    if re.search(r"(?:范围|单位|分辨率|含义|用途|映射)", question + answer, flags=re.IGNORECASE):
        score += 4
    return score


def _is_fast_candidate_acceptable(
    qa: Dict[str, Any],
    unit_info: Dict[str, Any],
) -> bool:
    """fast-path 使用较轻量的候选校验，避免复用旧链路中过重的字段级误杀。"""
    question = str(qa.get("question") or "").strip()
    answer = str(qa.get("answer") or "").strip()
    qa_task_type = normalize_task_type(qa.get("qa_task_type") or qa.get("task_type"))
    field_context = unit_info.get("field_context") or {}
    topic_context = unit_info.get("topic_context") or []
    probe = unit_info.get("probe") or {}
    matched_field = match_field_name(question, field_context)

    if not question or not answer:
        return False
    if _looks_like_placeholder_answer(answer):
        return False
    if _is_low_value_understanding_question(question, answer):
        return False
    if _looks_generic_non_protocol_qa(question, answer):
        return False

    if qa_task_type == "protocol_conversion":
        if not (re.search(r"(?:=|->|→)", answer) or contains_arithmetic_expression(answer) or looks_like_block_formula(answer)):
            return False
        if matched_field or qa.get("source_field"):
            return True
        return bool(probe.get("has_formula_signal") or probe.get("has_enum"))

    if matched_field:
        if _is_too_short_understanding_answer(question, answer, field_context):
            return False
        return True

    if topic_context:
        normalized_question = question.upper()
        if any(str(topic).upper() in normalized_question for topic in topic_context):
            return len(answer) >= 10

    if probe.get("has_numeric") and re.search(r"\d", answer):
        return len(answer) >= 10
    if probe.get("has_enum") and re.search(r"(?:表示|对应|=|->|→)", answer, flags=re.IGNORECASE):
        return len(answer) >= 10
    if probe.get("has_layout_signal") and re.search(r"(?:位|bit|bits|位置|布局)", answer, flags=re.IGNORECASE):
        return len(answer) >= 10

    return len(answer) >= 16


def _filter_fast_batch_candidates(
    candidates: List[Dict[str, Any]],
    batch: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    unit_map = {str(item.get("unit_id") or ""): item for item in (batch.get("units") or [])}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    seen_questions: Set[str] = set()

    for qa in candidates:
        segment_id = str(qa.get("segment_id") or "").strip()
        if segment_id not in unit_map:
            continue
        question = str(qa.get("question") or "").strip()
        answer = str(qa.get("answer") or "").strip()
        if not question or not answer:
            continue
        if len(answer) < 5 or len(answer) > 200:
            continue
        if _looks_like_placeholder_answer(answer):
            continue
        question_key = _normalize_text_key(question)
        if question_key in seen_questions:
            continue

        unit_info = unit_map[segment_id]
        if not _is_fast_candidate_acceptable(qa, unit_info):
            continue

        qa["candidate_score"] = _score_fast_candidate(qa, unit_info)
        seen_questions.add(question_key)
        grouped.setdefault(segment_id, []).append(qa)

    selected: List[Dict[str, Any]] = []
    leftover: List[Dict[str, Any]] = []
    shortfall: Dict[str, int] = {}

    for segment_id, unit_info in unit_map.items():
        quota = int(unit_info.get("target_count") or 0)
        segment_items = sorted(grouped.get(segment_id, []), key=lambda item: item.get("candidate_score", 0), reverse=True)
        selected.extend(segment_items[:quota])
        leftover.extend(segment_items[quota:])
        if len(segment_items) < quota:
            shortfall[segment_id] = quota - len(segment_items)

    total_target = int(batch.get("batch_target_total") or 0)
    if len(selected) < total_target and leftover:
        for qa in sorted(leftover, key=lambda item: item.get("candidate_score", 0), reverse=True):
            selected.append(qa)
            if len(selected) >= total_target:
                break

    selected = selected[:total_target]
    return selected, shortfall


def _finalize_global_qa_pool(
    batch_qas: List[Dict[str, Any]],
    target_total: int,
) -> List[Dict[str, Any]]:
    seen: Set[Tuple[str, str, str]] = set()
    final_pairs: List[Dict[str, Any]] = []
    for qa in sorted(batch_qas, key=lambda item: item.get("candidate_score", 0), reverse=True):
        key = (
            str(qa.get("segment_id") or ""),
            _normalize_text_key(qa.get("question") or ""),
            _normalize_text_key(qa.get("answer") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        final_pairs.append(qa)
        if len(final_pairs) >= target_total:
            break
    return final_pairs[:target_total]


def _resolve_unit_target_window(
    planned_target: int,
    remaining_total: int,
    remaining_units: int,
    minimum_target: int = 1,
) -> Tuple[int, int]:
    safe_remaining_total = max(0, int(remaining_total or 0))
    if safe_remaining_total <= 0:
        return 0, 0

    safe_remaining_units = max(1, int(remaining_units or 1))
    unit_min_target = max(1, int(minimum_target or 1))
    planned = max(unit_min_target, int(planned_target or unit_min_target))
    rolling_average = max(unit_min_target, (safe_remaining_total + safe_remaining_units - 1) // safe_remaining_units)
    soft_target = min(safe_remaining_total, planned)
    flex_bonus = max(2, rolling_average // 2)
    max_target = min(
        safe_remaining_total,
        max(soft_target, rolling_average + 2, planned + flex_bonus),
    )
    return soft_target, max_target


GENERIC_CONVERSION_HINTS = (
    "ascii",
    "十六进制",
    "摄氏度",
    "华氏度",
    "弧度",
    "度数",
    "角度",
    "ord(",
    "hex(",
    "format(",
)


PLACEHOLDER_ANSWER_PATTERN = re.compile(
    r"(?:未明确说明|未说明|未指定|未提供|未知|无法判断|无法确定|没有给出|缺少相关信息)",
    flags=re.IGNORECASE,
)


def _normalize_text_key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _looks_like_placeholder_answer(answer: str) -> bool:
    normalized = _normalize_text_key(answer)
    if normalized in {"null", "none", "文档中未提供相关信息"}:
        return True
    return bool(PLACEHOLDER_ANSWER_PATTERN.search(str(answer or "")))


def _detect_prompt_task_bias(text: Optional[str]) -> Optional[str]:
    normalized = _normalize_text_key(text)
    if not normalized:
        return None

    understanding_hits = sum(
        1 for token in ("协议理解", "protocol_understanding", "字段定义", "位宽", "范围", "分辨率")
        if token in normalized
    )
    conversion_hits = sum(
        1 for token in ("协议转换", "protocol_conversion", "conversion_mode", "转换公式", "只输出公式", "mapping", "transcoding")
        if token in normalized
    )
    if understanding_hits and not conversion_hits:
        return "protocol_understanding"
    if conversion_hits and not understanding_hits:
        return "protocol_conversion"
    return None


def _sanitize_custom_prompt_text(text: Optional[str], task_types: List[str]) -> str:
    normalized_types = [normalize_task_type(item) for item in (task_types or []) if item]
    if not normalized_types:
        return str(text or "").strip()
    bias = _detect_prompt_task_bias(text)
    if not bias:
        return str(text or "").strip()
    if len(set(normalized_types)) == 1 and bias != normalized_types[0]:
        return ""
    if len(set(normalized_types)) > 1:
        return ""
    return str(text or "").strip()


def _question_intent_bucket(question: str, qa_task_type: str) -> str:
    normalized = _normalize_text_key(question)
    if qa_task_type == "protocol_conversion":
        if any(token in normalized for token in ("公式", "formula", "计算", "换算")):
            return "conversion_formula"
        if any(token in normalized for token in ("映射", "对应", "取值", "代表")):
            return "conversion_mapping"
        return "conversion_other"
    if any(token in normalized for token in ("多少位", "位宽", "bit", "bits")):
        return "bit_width"
    if any(token in normalized for token in ("范围", "量程", "最小值", "最大值")):
        return "range"
    if any(token in normalized for token in ("分辨率", "精度", "resolution")):
        return "resolution"
    if any(token in normalized for token in ("单位", "unit")):
        return "unit"
    if any(token in normalized for token in ("含义", "表示什么", "代表什么", "语义", "用途")):
        return "meaning"
    if any(token in normalized for token in ("位置", "起始位", "字节", "偏移")):
        return "layout"
    if any(token in normalized for token in ("章节", "附录", "哪一节", "哪一章", "属于哪个章节", "属于哪一节", "文档中")):
        return "document_structure"
    return "other"


def _is_low_value_understanding_question(question: str, answer: str) -> bool:
    normalized_question = _normalize_text_key(question)
    normalized_answer = _normalize_text_key(answer)
    if any(
        token in normalized_question
        for token in ("属于哪个章节", "属于哪一节", "属于哪一章", "文档中", "哪个章节", "哪一章节", "哪一节", "哪一章", "附录")
    ):
        return True
    if any(
        token in normalized_question
        for token in ("出现在哪个", "出现于哪个", "出现在什么", "位段和证据", "证据是什么", "在哪个数据元素")
    ):
        return True
    if "描述是什么" in normalized_question and not re.search(r"\d|范围|分辨率|位|单位|取值|含义", normalized_answer):
        return True
    if re.fullmatch(r"j\d+\.\d+[a-z0-9]*章节", normalized_answer):
        return True
    if re.match(r"^\d{1,3}\s+", str(question or "").strip().upper()):
        return True
    if re.search(r"\b(?:THROUGH|UNITS?|DEGREES?|NO STATEMENT|PRECISION|INCREMENTS?)\b", str(question or "").strip().upper()):
        return True
    return False


def _looks_generic_non_protocol_qa(question: str, answer: str) -> bool:
    combined = f"{question}\n{answer}".lower()
    if any(token in combined for token in GENERIC_CONVERSION_HINTS):
        return True
    return False


def _is_requirement_mapping_context(
    content: str,
    topic_context: Optional[List[str]] = None,
) -> bool:
    combined = f"{content}\n{' | '.join(str(topic or '') for topic in (topic_context or []))}".upper()
    return bool(
        re.search(
            r"(?:RECEIVE REQUIREMENTS|TRANSMIT REQUIREMENTS|APPLICABLE RECEIVE TABLE|APPLICABLE TRANSMIT TABLE|MESSAGE USE|IMP REQ)",
            combined,
        )
    )


def _is_requirement_mapping_question(question: str, answer: str) -> bool:
    normalized_question = _normalize_text_key(question)
    if any(token in normalized_question for token in ("多少位", "位宽", "bit", "bits", "范围", "量程", "分辨率", "单位")):
        return False
    if any(token in normalized_question for token in ("标题", "描述", "含义", "版本", "最小值", "最大值", "枚举", "哪些", "是什么", "定义", "用途", "名称")):
        return False
    if re.search(r"(?:对应|适用).{0,8}(?:哪个表|哪一个表|表格|表名|接收表|传输表)", normalized_question):
        return True
    return False


def _is_requirement_chunk(content: str, field_context: Dict[str, Dict[str, str]], topic_context: Optional[List[str]] = None) -> bool:
    return _is_requirement_mapping_context(content, topic_context)


def _validate_generated_qa_against_context(
    qa: Dict[str, Any],
    field_context: Dict[str, Dict[str, str]],
    topic_context: Optional[List[str]] = None,
    content: str = "",
) -> Tuple[bool, Optional[str]]:
    question = str(qa.get("question", "")).strip()
    answer = str(qa.get("answer", "")).strip()
    qa_task_type = str(qa.get("qa_task_type") or "").strip()
    seed_kind = str(qa.get("seed_kind") or "").strip()
    if not question or not answer:
        return False, "问答内容为空"

    matched_field = match_field_name(question, field_context)
    extracted_field = str(qa.get("source_field") or "").strip().upper()
    if qa_task_type == "protocol_understanding":
        question_intent = _question_intent_bucket(question, qa_task_type)
        if _looks_like_placeholder_answer(answer):
            return False, "理解题答案为空洞或无文档依据"
        if _is_low_value_understanding_question(question, answer):
            return False, "理解题缺少任务价值"
        if _is_too_short_understanding_answer(question, answer, field_context, seed_kind=seed_kind):
            return False, "理解题答案过短或缺少关键规格信息"
        if _is_requirement_mapping_context(content, topic_context):
            if not _is_requirement_mapping_question(question, answer):
                return False, "requirements表仅保留用途/对应表名类问题"
            return True, None
        if seed_kind == "message_purpose":
            return True, None
        if seed_kind == "word_title":
            return True, None
        if seed_kind in {"enum_mapping", "simple_mapping"}:
            if not (matched_field or extracted_field or FIELD_CODING_HINT_PATTERN.search(content)):
                return False, "枚举题未命中真实字段或编码表上下文"
            if not re.search(r"(?:\d+\s*(?:表示|对应|=|->|→)\s*[A-Z0-9])", answer, flags=re.IGNORECASE):
                return False, "枚举题答案缺少明确映射关系"
            if question_intent not in {"meaning", "other"} and not any(token in question for token in ("对应", "取值", "含义", "表示")):
                return False, "枚举映射答案与问题意图不匹配"
            return True, None
        if not matched_field:
            if topic_context:
                normalized_question = question.upper()
                if not any(str(topic).upper() in normalized_question for topic in topic_context):
                    return False, "问题未命中文档中的真实字段或主题锚点"
            else:
                return False, "问题未命中文档中的真实字段名"
        if _looks_generic_non_protocol_qa(question, answer):
            return False, "疑似通用题而非协议字段题"
        if matched_field and not (
            re.search(r"\d", answer)
            or re.search(r"(?:范围|分辨率|位|bit|单位|映射|取值|代表|含义)", answer, flags=re.IGNORECASE)
        ):
            return False, "理解题答案缺少规格或语义信息"
        if question_intent == "bit_width" and not re.search(r"(?:\d+\s*(?:位|bit|bits))", answer, flags=re.IGNORECASE):
            return False, "位宽题答案缺少真实位宽"
        if question_intent == "range" and not re.search(r"(?:范围|-?\d+(?:\.\d+)?\s*(?:到|to|TO|~|～|—|–|-)\s*-?\d+(?:\.\d+)?)", answer, flags=re.IGNORECASE):
            return False, "范围题答案缺少真实范围"
        if question_intent == "unit" and not re.search(r"(?:单位|unit|°|knots|feet|meter|meters|m/s|hz|octal)", answer, flags=re.IGNORECASE):
            return False, "单位题答案缺少真实单位"
    elif qa_task_type == "protocol_conversion":
        if _looks_like_placeholder_answer(answer):
            return False, "转换题答案为空洞或无文档依据"
        if _looks_generic_non_protocol_qa(question, answer):
            return False, "疑似通用转换题而非协议转换题"
        if not (matched_field or extracted_field):
            return False, "转换题未命中真实字段锚点"
    return True, None


def _filter_generated_qa_pairs(
    qa_pairs: List[Dict[str, Any]],
    field_context: Dict[str, Dict[str, str]],
    target_count: int,
    max_per_field: Optional[int] = 2,
    topic_context: Optional[List[str]] = None,
    content: str = "",
) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    seen = set()
    seen_semantic = set()
    field_quota: Dict[str, int] = {}
    for qa in qa_pairs:
        question = str(qa.get("question", "")).strip()
        answer = str(qa.get("answer", "")).strip()
        qa_task_type = str(qa.get("qa_task_type") or "").strip()
        is_low_quality, _quality_reason = check_quality(
            question,
            answer,
            qa_task_type or "protocol_understanding",
            seed_kind=qa.get("seed_kind"),
        )
        if is_low_quality:
            continue
        key = (_normalize_text_key(question), _normalize_text_key(answer))
        if key in seen:
            continue
        ok, _reason = _validate_generated_qa_against_context(
            qa,
            field_context,
            topic_context=topic_context,
            content=content,
        )
        if not ok:
            continue
        matched_field = match_field_name(question, field_context) or str(qa.get("source_field") or "").strip().upper()
        semantic_key = (
            qa_task_type,
            matched_field,
            _question_intent_bucket(question, qa_task_type),
        )
        if matched_field and semantic_key in seen_semantic:
            continue
        if matched_field:
            current = field_quota.get(matched_field, 0)
            if max_per_field is not None and current >= max_per_field:
                continue
            field_quota[matched_field] = current + 1
        seen.add(key)
        if matched_field:
            seen_semantic.add(semantic_key)
        filtered.append(qa)
        if len(filtered) >= target_count:
            break
    return filtered


def _filter_requirement_mapping_pairs(
    qa_pairs: List[Dict[str, Any]],
    target_count: int,
) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    seen = set()
    for qa in qa_pairs:
        question = str(qa.get("question", "")).strip()
        answer = str(qa.get("answer", "")).strip()
        if not question or not answer:
            continue
        if not _is_requirement_mapping_question(question, answer):
            continue
        key = (_normalize_text_key(question), _normalize_text_key(answer))
        if key in seen:
            continue
        seen.add(key)
        qa["qa_task_type"] = "protocol_understanding"
        filtered.append(qa)
        if len(filtered) >= target_count:
            break
    return filtered


def _filter_unit_candidate_pairs(
    qa_pairs: List[Dict[str, Any]],
    *,
    is_requirement_chunk: bool,
    target_count: int,
    field_context: Dict[str, Dict[str, str]],
    topic_context: Optional[List[str]],
    content: str,
) -> List[Dict[str, Any]]:
    if target_count <= 0:
        return []
    if is_requirement_chunk:
        return _filter_requirement_mapping_pairs(qa_pairs, target_count)

    filtered: List[Dict[str, Any]] = []
    for max_per_field in (2, 3, None):
        filtered = _filter_generated_qa_pairs(
            qa_pairs,
            field_context=field_context,
            target_count=target_count,
            max_per_field=max_per_field,
            topic_context=topic_context,
            content=content,
        )
        if len(filtered) >= target_count:
            break
    return filtered


def build_task_spec(task_types: List[str], conversion_modes: List[str], count: int) -> str:
    readable_task_types = ", ".join(task_types)
    readable_modes = ", ".join(conversion_modes) if conversion_modes else "transcoding, mapping"
    understanding_min = 0
    conversion_min = 0
    if "protocol_understanding" in task_types and "protocol_conversion" in task_types:
        conversion_min = max(1, count // 3)
        understanding_min = max(1, count - conversion_min)
    elif "protocol_conversion" in task_types:
        conversion_min = count
    else:
        understanding_min = count

    coverage_rule = (
        f"最终结果必须严格包含{understanding_min}条protocol_understanding与{conversion_min}条protocol_conversion，总数严格等于{count}条。"
        if understanding_min and conversion_min
        else (
            f"所有问答均为protocol_conversion，必须严格生成{conversion_min}条。"
            if conversion_min
            else f"所有问答均为protocol_understanding，必须严格生成{understanding_min}条。"
        )
    )
    return (
        f"必须覆盖任务类型: {readable_task_types}。\n"
        f"{coverage_rule}\n"
        f"协议转换类允许的conversion_mode: {readable_modes}。\n"
        "对协议转换类，answer 必须输出可执行的值到值公式，允许单行表达式、mapping_table，或多行 if/else/for 公式块。\n"
        "如使用多行公式块，必须把最终结果赋给 result，conversion_formula 与 answer 保持一致。\n"
        "若输出为离散映射表（如5=10, 1=20），conversion_mode必须为mapping。\n"
        "数量要求是强约束：不得少于、不得多于要求数量；如一次未满足，继续补齐后再输出。\n"
        "只输出JSON数组，不要输出<think>、解释、前后缀文本。"
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _chunk_quality_score(chunk: Chunk) -> float:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    quality_score = float(metadata.get("quality_score", 0) or 0)
    field_count = float(metadata.get("field_count", 0) or 0)
    formula_count = float(metadata.get("formula_count", 0) or 0)
    mapping_pair_count = float(metadata.get("mapping_pair_count", 0) or 0)
    bit_coverage_count = float(metadata.get("bit_coverage_count", 0) or 0)
    evidence_score = float(metadata.get("evidence_score", 0) or 0)
    noise_penalty = float(metadata.get("noise_penalty", 0) or 0)
    noisy_block_count = float(metadata.get("noisy_block_count", 0) or 0)
    return (
        quality_score
        + field_count * 1.8
        + formula_count * 1.5
        + mapping_pair_count * 1.2
        + bit_coverage_count
        + evidence_score * 0.45
        - noise_penalty * 1.1
        - noisy_block_count * 1.5
    )


def _normalize_target_protocol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw


def _chunk_matches_target_protocol(chunk: Chunk, target_protocol: str) -> bool:
    target = _normalize_target_protocol(target_protocol)
    if not target:
        return False
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    anchor = str(metadata.get("protocol_anchor") or "").strip().upper()
    if anchor.startswith(target):
        return True
    field_names = metadata.get("field_names") if isinstance(metadata.get("field_names"), list) else []
    for name in field_names:
        if str(name).strip().upper().startswith(target):
            return True
    return target in str(chunk.content_snapshot or "").upper()


def _estimate_text_noise_penalty(text: str) -> float:
    cleaned = str(text or "").strip()
    if not cleaned:
        return 12.0
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    line_count = len(lines) or 1
    short_lines = sum(1 for line in lines if len(line) <= 6)
    noise_hits = sum(1 for line in lines if NOISE_HINT_PATTERN.search(line))
    punctuation_chars = sum(1 for ch in cleaned if not ch.isalnum() and not ch.isspace())
    punctuation_ratio = punctuation_chars / max(len(cleaned), 1)
    penalty = 0.0
    if line_count >= 4 and short_lines / line_count >= 0.55:
        penalty += 4.0
    if noise_hits:
        penalty += min(6.0, noise_hits * 1.5)
    if punctuation_ratio >= 0.32:
        penalty += 2.0
    if len(cleaned) < 36 and not RULE_SIGNAL_PATTERN.search(cleaned):
        penalty += 3.0
    if TOC_HINT_PATTERN.search(cleaned):
        penalty += 12.0
    return round(penalty, 4)


def _is_toc_like_chunk(chunk: Chunk) -> bool:
    text = str(chunk.content_snapshot or "").strip()
    if not text:
        return False
    if FIELD_CODING_HINT_PATTERN.search(text):
        return False
    if STRUCTURED_BLOCK_HINT_PATTERN.search(text):
        return False
    if RULE_SIGNAL_PATTERN.search(text):
        return False
    if TOC_HINT_PATTERN.search(text):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    section_like = sum(
        1
        for line in lines[:80]
        if re.match(r"^[A-Z]\.?\d+(?:\.\d+)*\b", line)
        or re.match(r"^[A-Z]\d+(?:\.\d+)*\b", line)
    )
    if section_like >= 12:
        return True
    toc_keyword_hits = sum(
        1
        for line in lines[:80]
        if re.search(r"(?:INTRODUCTION|TRANSMIT TABLES|RECORD FORMATS|TABLE OF CONTENTS|APPENDIX|GENERAL CONSIDERATIONS)", line, flags=re.IGNORECASE)
    )
    if toc_keyword_hits >= 8:
        return True
    title_like = sum(
        1
        for line in lines[:40]
        if "|" in line and len(line) <= 120 and not RULE_SIGNAL_PATTERN.search(line)
    )
    return title_like >= 8


def _build_chunk_adjustment_map(chunks: List[Chunk], target_protocol: str) -> Dict[str, Dict[str, float]]:
    """Build target/evidence/noise adjustments for QA chunk selection."""
    target = _normalize_target_protocol(target_protocol)
    keywords = {target} if target else set()
    major_match = re.match(r"(J\d+)", target)
    if major_match:
        keywords.add(major_match.group(1))

    all_block_ids: List[int] = []
    for chunk in chunks:
        all_block_ids.extend([bid for bid in (chunk.source_block_ids or []) if isinstance(bid, int)])

    block_map: Dict[int, Block] = {}
    if all_block_ids:
        try:
            blocks = db_client.get_blocks_by_ids(list(dict.fromkeys(all_block_ids)))
            block_map = {block.block_id: block for block in blocks}
        except Exception:
            block_map = {}

    adjustment_map: Dict[str, Dict[str, float]] = {}
    for chunk in chunks:
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        chunk_text = f"{chunk.content_snapshot or ''} {' '.join(str(v) for v in (metadata.get('field_names') or []))}".upper()
        anchor = str(metadata.get("protocol_anchor") or "").strip().upper()
        field_count = float(metadata.get("field_count", 0) or 0)
        formula_count = float(metadata.get("formula_count", 0) or 0)
        mapping_pair_count = float(metadata.get("mapping_pair_count", 0) or 0)
        evidence_score = float(metadata.get("evidence_score", 0) or 0)
        noise_penalty = float(metadata.get("noise_penalty", 0) or 0)

        target_bonus = 0.0
        if target:
            if anchor == target:
                target_bonus += 20.0
            elif anchor and anchor.startswith(target):
                target_bonus += 16.0
            elif _chunk_matches_target_protocol(chunk, target):
                target_bonus += 12.0

        for keyword in keywords:
            if keyword and keyword in chunk_text:
                target_bonus = max(target_bonus, 18.0 if keyword == target else 10.0)

        field_bonus = min(16.0, field_count * 1.6)
        formula_bonus = min(18.0, formula_count * 2.2 + mapping_pair_count * 1.8)
        selection_bonus = target_bonus + field_bonus + formula_bonus + min(18.0, evidence_score * 0.35)

        for bid in chunk.source_block_ids or []:
            block = block_map.get(bid)
            if not block:
                continue
            content = str((block.cleaned_content or block.content or "")).strip()
            upper_content = content.upper()
            if target:
                for keyword in keywords:
                    if keyword and keyword in upper_content:
                        target_bonus = max(target_bonus, 26.0 if keyword == target else 14.0)
                        selection_bonus = max(selection_bonus, target_bonus + field_bonus + formula_bonus)
            if RULE_SIGNAL_PATTERN.search(content):
                formula_bonus = max(formula_bonus, 8.0)
            noise_penalty = max(noise_penalty, _estimate_text_noise_penalty(content))

        adjustment_map[chunk.chunk_id] = {
            "target_bonus": round(target_bonus, 4),
            "field_bonus": round(field_bonus, 4),
            "formula_bonus": round(formula_bonus, 4),
            "selection_bonus": round(target_bonus + field_bonus + formula_bonus + min(18.0, evidence_score * 0.35), 4),
            "noise_penalty": round(min(24.0, noise_penalty), 4),
        }
    return adjustment_map


def _build_target_bonus_map(chunks: List[Chunk], target_protocol: str) -> Dict[str, float]:
    """基于chunk关联原始块内容计算目标协议相关性加权。"""
    adjustment_map = _build_chunk_adjustment_map(chunks, target_protocol)
    return {chunk_id: item.get("target_bonus", 0.0) for chunk_id, item in adjustment_map.items()}


def _score_chunk_for_understanding(chunk: Chunk, target_protocol: str = "") -> float:
    score = _chunk_quality_score(chunk)
    semantic_type = str(chunk.semantic_type or "").strip()
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    score += 10.0 if semantic_type in UNDERSTANDING_SEMANTIC_TYPES else 0.0
    score += float(metadata.get("field_count", 0) or 0) * 1.3
    score += float(metadata.get("bit_coverage_count", 0) or 0) * 1.2
    score += float(metadata.get("range_coverage_count", 0) or 0) * 1.0
    if _chunk_matches_target_protocol(chunk, target_protocol):
        score += 12.0
    return score


def _score_chunk_for_conversion(chunk: Chunk, target_protocol: str = "") -> float:
    score = _chunk_quality_score(chunk)
    semantic_type = str(chunk.semantic_type or "").strip()
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    score += 12.0 if semantic_type in CONVERSION_SEMANTIC_TYPES else 0.0
    score += float(metadata.get("formula_count", 0) or 0) * 1.8
    score += float(metadata.get("mapping_pair_count", 0) or 0) * 1.6
    if _chunk_matches_target_protocol(chunk, target_protocol):
        score += 12.0
    return score


def llm_rerank_chunk_ids(
    candidate_chunks: List[Chunk],
    task_types: List[str],
    selection_limit: int,
    target_protocol: str = "",
) -> Optional[List[str]]:
    """对规则选出的候选chunks进行轻量LLM重排。"""
    if not candidate_chunks:
        return None
    try:
        llm = get_llm_client()
        payload = {
            "selection_limit": selection_limit,
            "task_types": task_types,
            "target_protocol": target_protocol,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "semantic_type": chunk.semantic_type,
                    "quality_score": _chunk_quality_score(chunk),
                    "metadata": chunk.metadata or {},
                    "content_preview": (chunk.content_snapshot or "")[:260],
                }
                for chunk in candidate_chunks[:20]
            ],
        }
        system_prompt = (
            "你是协议问答选块助手。"
            "从给定chunks中挑选最适合生成高质量QA的chunk_id。"
            "返回JSON: {\"selected_chunk_ids\": [\"chk_xxx\", ...]}，顺序即优先级。"
        )
        result = llm.extract_json(json.dumps(payload, ensure_ascii=False), system_prompt=system_prompt)
        if isinstance(result, dict) and isinstance(result.get("selected_chunk_ids"), list):
            selected = []
            seen = set()
            valid_ids = {chunk.chunk_id for chunk in candidate_chunks}
            for chunk_id in result["selected_chunk_ids"]:
                cid = str(chunk_id).strip()
                if cid and cid in valid_ids and cid not in seen:
                    selected.append(cid)
                    seen.add(cid)
                if len(selected) >= selection_limit:
                    break
            if selected:
                return selected
    except Exception:
        return None
    return None


def select_chunks_for_qa(
    chunks: List[Chunk],
    task_types: List[str],
    count: int,
    selection_config: Optional[Dict[str, Any]] = None,
) -> List[Chunk]:
    """根据任务类型自动选择最优chunks，优先规则，必要时LLM重排。"""
    if not chunks:
        return []
    selection_config = selection_config or {}
    top_k = _safe_int(selection_config.get("top_k_chunks"), QA_SELECTION_TOP_K_DEFAULT)
    top_k = max(1, min(40, top_k))
    enable_llm_rerank = bool(selection_config.get("enable_llm_rerank", False))
    target_protocol = _normalize_target_protocol(
        selection_config.get("target_protocol")
        or selection_config.get("target_protocol_anchor")
        or selection_config.get("target_keyword")
    )
    preferred_chunks = [chunk for chunk in chunks if not _is_toc_like_chunk(chunk)]
    candidate_chunks = preferred_chunks or chunks
    adjustment_map = _build_chunk_adjustment_map(candidate_chunks, target_protocol)

    understanding_quota = 0
    conversion_quota = 0
    if "protocol_understanding" in task_types and "protocol_conversion" in task_types:
        conversion_quota = max(1, top_k // 3)
        understanding_quota = max(1, top_k - conversion_quota)
    elif "protocol_conversion" in task_types:
        conversion_quota = top_k
    else:
        understanding_quota = top_k

    by_understanding = sorted(
        candidate_chunks,
        key=lambda chunk: _score_chunk_for_understanding(chunk, target_protocol=target_protocol)
        + adjustment_map.get(chunk.chunk_id, {}).get("selection_bonus", 0.0)
        - adjustment_map.get(chunk.chunk_id, {}).get("noise_penalty", 0.0),
        reverse=True,
    )
    by_conversion = sorted(
        candidate_chunks,
        key=lambda chunk: _score_chunk_for_conversion(chunk, target_protocol=target_protocol)
        + adjustment_map.get(chunk.chunk_id, {}).get("selection_bonus", 0.0)
        - adjustment_map.get(chunk.chunk_id, {}).get("noise_penalty", 0.0),
        reverse=True,
    )
    by_overall = sorted(
        candidate_chunks,
        key=lambda chunk: _chunk_quality_score(chunk)
        + adjustment_map.get(chunk.chunk_id, {}).get("selection_bonus", 0.0)
        - adjustment_map.get(chunk.chunk_id, {}).get("noise_penalty", 0.0)
        + (8.0 if _chunk_matches_target_protocol(chunk, target_protocol) else 0.0),
        reverse=True,
    )

    selected_ids: List[str] = []
    selected_set: Set[str] = set()

    if understanding_quota > 0:
        for chunk in by_understanding:
            if chunk.chunk_id in selected_set:
                continue
            selected_ids.append(chunk.chunk_id)
            selected_set.add(chunk.chunk_id)
            if len(selected_ids) >= understanding_quota:
                break

    if conversion_quota > 0:
        conversion_added = 0
        for chunk in by_conversion:
            if chunk.chunk_id in selected_set:
                continue
            selected_ids.append(chunk.chunk_id)
            selected_set.add(chunk.chunk_id)
            conversion_added += 1
            if conversion_added >= conversion_quota:
                break

    for chunk in by_overall:
        if len(selected_ids) >= top_k:
            break
        if chunk.chunk_id in selected_set:
            continue
        selected_ids.append(chunk.chunk_id)
        selected_set.add(chunk.chunk_id)

    chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    selected_chunks = [chunk_by_id[chunk_id] for chunk_id in selected_ids if chunk_id in chunk_by_id]

    if enable_llm_rerank and selected_chunks:
        llm_ids = llm_rerank_chunk_ids(
            selected_chunks,
            task_types=task_types,
            selection_limit=top_k,
            target_protocol=target_protocol,
        )
        if llm_ids:
            selected_chunks = [chunk_by_id[chunk_id] for chunk_id in llm_ids if chunk_id in chunk_by_id]

    return selected_chunks[:top_k]


def get_chunks_by_ids(chunk_ids: List[str]) -> List[Chunk]:
    """根据chunk_id列表获取语义块"""
    if not chunk_ids:
        return []
    return db_client.get_chunks_by_ids(chunk_ids)


def get_source_content(
    source_ids: List[str],
    dataset_id: Optional[str] = None,
    source_id_type: str = "chunk",
) -> tuple:
    """
    根据source_ids获取源数据内容
    source_id_type 为 chunk 时，source_ids 按语义块标识读取；
    source_id_type 为 block 时，source_ids 按 block_id 读取。

    返回: (内容文本, 来源block_id列表, dataset_id)
    """
    all_content = []
    all_block_ids = []
    resolved_dataset_id = str(dataset_id or "").strip() or None

    chunk_ids = []
    block_ids = []
    if source_id_type == "block":
        for sid in source_ids:
            try:
                block_ids.append(int(sid))
            except (ValueError, TypeError):
                continue
    else:
        chunk_ids = [str(sid).strip() for sid in source_ids if str(sid).strip()]

    chunk_block_ids: List[int] = []

    # 获取chunks
    if chunk_ids:
        chunks = db_client.get_chunks_by_ids(chunk_ids)
        for chunk in chunks:
            chunk_block_ids.extend(chunk.source_block_ids or [])
        chunk_block_ids = [bid for bid in chunk_block_ids if isinstance(bid, int)]

        block_map: Dict[int, Block] = {}
        if chunk_block_ids:
            try:
                chunk_blocks = db_client.get_blocks_by_ids(chunk_block_ids)
                block_map = {block.block_id: block for block in chunk_blocks}
            except Exception:
                block_map = {}

        for chunk in chunks:
            if resolved_dataset_id is None:
                resolved_dataset_id = chunk.dataset_id
            chunk_content = str(chunk.content_snapshot or "").strip()
            metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
            if not chunk_content and isinstance(metadata.get("merged_content"), str):
                chunk_content = metadata.get("merged_content", "").strip()

            chunk_text_parts: List[str] = []
            if not chunk_content:
                for bid in chunk.source_block_ids or []:
                    if bid in block_map:
                        block = block_map[bid]
                        chunk_text_parts.append((block.cleaned_content or block.content or "").strip())
            chunk_text = chunk_content or "\n".join([p for p in chunk_text_parts if p])
            all_content.append(chunk_text)
            all_block_ids.extend(chunk.source_block_ids)

    # 获取blocks
    if block_ids:
        blocks = db_client.get_blocks_by_ids(block_ids, dataset_id=resolved_dataset_id)
        for block in blocks:
            content = block.cleaned_content or block.content
            all_content.append(content)
            all_block_ids.append(block.block_id)

    # 去重block_ids
    seen = set()
    unique_block_ids = []
    for bid in all_block_ids:
        if bid not in seen:
            seen.add(bid)
            unique_block_ids.append(bid)

    dedup_contents = []
    seen_content = set()
    for text in all_content:
        normalized = str(text or "").strip()
        if not normalized or normalized in seen_content:
            continue
        seen_content.add(normalized)
        dedup_contents.append(normalized)

    combined_content = "\n\n".join(dedup_contents)
    return combined_content, unique_block_ids, resolved_dataset_id


def _dedup_int_ids(values: List[int]) -> List[int]:
    """按原顺序去重整数ID。"""
    deduped: List[int] = []
    seen: Set[int] = set()
    for value in values:
        if not isinstance(value, int) or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def get_generation_units(
    source_ids: List[str],
    dataset_id: Optional[str] = None,
    source_id_type: str = "chunk",
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """将输入块解析为逐块生成单元。"""
    resolved_dataset_id = str(dataset_id or "").strip() or None
    generation_units: List[Dict[str, Any]] = []

    if source_id_type == "block":
        ordered_block_ids: List[int] = []
        for sid in source_ids:
            try:
                ordered_block_ids.append(int(sid))
            except (TypeError, ValueError):
                continue
        if not ordered_block_ids:
            return [], resolved_dataset_id
        block_map: Dict[int, Block] = {}
        for block in db_client.get_blocks_by_ids(ordered_block_ids, dataset_id=resolved_dataset_id):
            block_map[int(block.block_id)] = block
            metadata = block.metadata if isinstance(block.metadata, dict) else {}
            try:
                logical_block_id = int(metadata.get("legacy_block_id"))
            except (TypeError, ValueError):
                continue
            block_map.setdefault(logical_block_id, block)
        for block_id in ordered_block_ids:
            block = block_map.get(block_id)
            if block is None:
                continue
            content = str(block.cleaned_content or block.content or "").strip()
            generation_units.append({
                "unit_id": str(block.block_id),
                "content": content,
                "source_block_ids": [block.block_id],
                "source_chunk_ids": [],
            })
        return generation_units, resolved_dataset_id

    ordered_chunk_ids = [str(sid).strip() for sid in source_ids if str(sid).strip()]
    if not ordered_chunk_ids:
        return [], resolved_dataset_id

    chunk_map: Dict[str, Chunk] = {}
    for chunk in db_client.get_chunks_by_ids(ordered_chunk_ids):
        chunk_map.setdefault(chunk.chunk_id, chunk)
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        chunk_record_id = str(metadata.get("chunk_record_id") or "").strip()
        if chunk_record_id:
            chunk_map.setdefault(chunk_record_id, chunk)
    all_block_ids: List[int] = []
    for chunk_id in ordered_chunk_ids:
        chunk = chunk_map.get(chunk_id)
        if chunk is None:
            continue
        all_block_ids.extend([bid for bid in (chunk.source_block_ids or []) if isinstance(bid, int)])
    block_map: Dict[int, Block] = {}
    if all_block_ids:
        block_map = {
            block.block_id: block
            for block in db_client.get_blocks_by_ids(_dedup_int_ids(all_block_ids))
        }

    for chunk_id in ordered_chunk_ids:
        chunk = chunk_map.get(chunk_id)
        if chunk is None:
            continue
        if resolved_dataset_id is None and chunk.dataset_id:
            resolved_dataset_id = chunk.dataset_id
        chunk_content = str(chunk.content_snapshot or "").strip()
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        if not chunk_content and isinstance(metadata.get("merged_content"), str):
            chunk_content = metadata.get("merged_content", "").strip()
        if not chunk_content:
            chunk_text_parts: List[str] = []
            for bid in chunk.source_block_ids or []:
                block = block_map.get(bid)
                if block is None:
                    continue
                text = str(block.cleaned_content or block.content or "").strip()
                if text:
                    chunk_text_parts.append(text)
            chunk_content = "\n".join(chunk_text_parts).strip()
        generation_units.append({
            "unit_id": chunk.chunk_id,
            "content": chunk_content,
            "source_block_ids": _dedup_int_ids([bid for bid in (chunk.source_block_ids or []) if isinstance(bid, int)]),
            "source_chunk_ids": [chunk.chunk_id],
        })
    return generation_units, resolved_dataset_id


def generate_qa_pairs(
    content: str,
    count: int,
    system_prompt: str = None,
    user_instruction: str = None,
    task_types: Optional[List[str]] = None,
    conversion_modes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """调用LLM生成QA对，数量不足时持续补齐到count。"""
    llm = get_llm_client()
    normalized_task_types = task_types or ["protocol_understanding", "protocol_conversion"]
    normalized_conversion_modes = conversion_modes or ["transcoding", "mapping"]
    target_count = max(1, int(count))
    batch_size = min(QA_GENERATION_BATCH_SIZE, target_count)
    min_attempts = (target_count + batch_size - 1) // batch_size
    max_attempts = max(QA_GENERATION_MAX_ATTEMPTS, min_attempts * 3)
    collected_pairs: List[Dict[str, Any]] = []
    seen_pairs: Set[Tuple[str, str, str, str]] = set()
    attempts = 0

    while len(collected_pairs) < target_count and attempts < max_attempts:
        remaining_count = target_count - len(collected_pairs)
        batch_count = min(batch_size, remaining_count)
        batch_instruction = _build_batch_user_instruction(
            base_instruction=user_instruction,
            generated_pairs=collected_pairs,
            remaining_count=remaining_count,
        )
        batch_pairs = _generate_qa_pairs_two_step(
            llm=llm,
            content=content,
            count=batch_count,
            system_prompt=system_prompt,
            user_instruction=batch_instruction,
            task_types=normalized_task_types,
            conversion_modes=normalized_conversion_modes,
        )
        if not batch_pairs:
            batch_task_spec = build_task_spec(normalized_task_types, normalized_conversion_modes, batch_count)
            batch_pairs = _generate_qa_pairs_batch(
                llm=llm,
                content=content,
                count=batch_count,
                system_prompt=system_prompt,
                user_instruction=batch_instruction,
                task_spec=batch_task_spec,
                task_types=normalized_task_types,
                conversion_modes=normalized_conversion_modes,
            )
        attempts += 1
        if not batch_pairs:
            continue
        for qa in batch_pairs:
            pair_key = _qa_pair_dedup_key(qa)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            collected_pairs.append(qa)
            if len(collected_pairs) >= target_count:
                break

    if len(collected_pairs) < target_count:
        return collected_pairs

    return collected_pairs[:target_count]


def _generate_qa_pairs_batch(
    llm: LocalLLM,
    content: str,
    count: int,
    system_prompt: str = None,
    user_instruction: str = None,
    task_spec: str = None,
    task_types: Optional[List[str]] = None,
    conversion_modes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """调用LLM生成单批QA对。"""
    normalized_task_types = [normalize_task_type(item) for item in (task_types or []) if item]
    if not normalized_task_types:
        normalized_task_types = ["protocol_understanding", "protocol_conversion"]

    if normalized_task_types == ["protocol_understanding"]:
        base_system, base_user = PromptTemplates.format_qa_understanding(content=content, count=count)
        user = "\n\n".join(part for part in [base_user, user_instruction] if str(part or "").strip())
        system = f"{base_system}\n\n补充要求：\n{system_prompt}" if system_prompt else base_system
    elif normalized_task_types == ["protocol_conversion"]:
        base_system, base_user = PromptTemplates.format_qa_conversion(content=content, count=count)
        extra_rules: List[str] = []
        normalized_modes = [mode for mode in (conversion_modes or []) if mode]
        if normalized_modes:
            extra_rules.append(f"仅允许以下conversion_mode: {', '.join(normalized_modes)}。")
        if task_spec:
            extra_rules.append(task_spec)
        if user_instruction:
            extra_rules.append(user_instruction)
        user = "\n\n".join(part for part in [base_user, "\n".join(extra_rules).strip()] if str(part or "").strip())
        system = f"{base_system}\n\n补充要求：\n{system_prompt}" if system_prompt else base_system
    else:
        system, user = PromptTemplates.format_qa_generate(
            content=content,
            count=count,
            system_prompt=system_prompt,
            user_instruction=user_instruction,
            task_spec=task_spec,
        )

    max_new_tokens = max(2048, min(8192, count * 320))
    response = llm.generate(
        prompt=user,
        system_prompt=system,
        max_new_tokens=max_new_tokens,
        temperature=0.7,
    )
    qa_pairs = parse_qa_response(response)
    if qa_pairs:
        return qa_pairs

    for _ in range(QA_GENERATION_RETRY):
        retry_user = (
            f"{user}\n\n"
            f"重试要求：本次必须返回恰好{count}条问答，只返回JSON数组，每个元素必须包含question和answer字段，不要任何解释文字。"
        )
        response = llm.generate(
            prompt=retry_user,
            system_prompt=system,
            max_new_tokens=max_new_tokens,
            temperature=0.2,
        )
        qa_pairs = parse_qa_response(response)
        if qa_pairs:
            break
    return qa_pairs


def _qa_pair_dedup_key(qa: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """返回用于补齐过程的QA去重键。"""
    question = re.sub(r"\s+", " ", str(qa.get("question", "")).strip().lower())
    answer = re.sub(r"\s+", " ", str(qa.get("answer", "")).strip().lower())
    qa_task_type = normalize_task_type(qa.get("qa_task_type") or qa.get("task_type"))
    conversion_mode = normalize_conversion_mode(qa.get("conversion_mode")) or ""
    return qa_task_type, conversion_mode, question, answer


def _build_batch_user_instruction(
    base_instruction: Optional[str],
    generated_pairs: List[Dict[str, Any]],
    remaining_count: int,
) -> str:
    """构造补齐批次提示，尽量避免重复问题。"""
    instructions: List[str] = []
    normalized_base = str(base_instruction or "").strip()
    if normalized_base:
        instructions.append(normalized_base)
    instructions.append(f"本次输出必须恰好包含{remaining_count}条新的问答。")
    if generated_pairs:
        existing_questions = [
            str(item.get("question", "")).strip()
            for item in generated_pairs[-12:]
            if str(item.get("question", "")).strip()
        ]
        instructions.append("禁止与已生成问答重复，尤其不要重复以下问题：")
        instructions.extend(f"- {question}" for question in existing_questions)
    return "\n".join(instructions)


def parse_qa_response(response: str) -> List[Dict[str, Any]]:
    """解析LLM响应中的QA对"""
    qa_pairs: List[Dict[str, Any]] = []
    cleaned_response = LocalLLM._sanitize_response_text(response)

    parsed = LocalLLM.parse_json_from_response(cleaned_response, prefer=list)
    if isinstance(parsed, list):
        qa_pairs = parsed
    elif isinstance(parsed, dict):
        for key in ("qa_pairs", "data", "items"):
            maybe_list = parsed.get(key)
            if isinstance(maybe_list, list):
                qa_pairs = maybe_list
                break

    # 验证和清理QA对格式
    valid_pairs = []
    for qa in qa_pairs:
        if isinstance(qa, dict) and "question" in qa and "answer" in qa:
            qa_task_type = normalize_task_type(qa.get("qa_task_type") or qa.get("task_type"))
            conversion_mode = normalize_conversion_mode(qa.get("conversion_mode"))
            conversion_formula = qa.get("conversion_formula")
            answer = str(qa.get("answer", "")).strip()

            if qa_task_type == "protocol_conversion":
                conversion_payload = normalize_conversion_payload(
                    answer=answer,
                    conversion_formula=conversion_formula,
                    conversion_mode=conversion_mode,
                )
                answer = conversion_payload["answer"]
                conversion_formula = conversion_payload["conversion_formula"]
                conversion_mode = conversion_payload["conversion_mode"]

            valid_pairs.append({
                "question": str(qa.get("question", "")).strip(),
                "answer": answer,
                "qa_task_type": qa_task_type,
                "conversion_mode": conversion_mode,
                "conversion_formula": conversion_formula,
                "source_fields": normalize_source_fields_value(qa.get("source_fields"), fallback=qa.get("source_field")),
                "concept_name": str(qa.get("concept_name") or qa.get("concept") or "").strip() or None,
                "formula_kind": str(qa.get("formula_kind") or "").strip() or None,
                "target_protocol_type": str(qa.get("target_protocol_type") or "").strip() or None,
                "target_message_code": str(qa.get("target_message_code") or "").strip().upper() or None,
                "target_field": str(qa.get("target_field") or "").strip() or None,
                "source_field": str(qa.get("source_field") or "").strip() or None,
                "extracted_info": qa.get("extracted_info") if isinstance(qa.get("extracted_info"), dict) else None,
            })

    return valid_pairs


def _parse_question_plan_response(response: str) -> List[Dict[str, Any]]:
    """解析问题规划阶段返回的问题列表。"""
    cleaned_response = LocalLLM._sanitize_response_text(response)
    parsed = LocalLLM.parse_json_from_response(cleaned_response, prefer=list)
    if not isinstance(parsed, list) and isinstance(parsed, dict):
        for key in ("questions", "items", "data"):
            maybe_list = parsed.get(key)
            if isinstance(maybe_list, list):
                parsed = maybe_list
                break
    if not isinstance(parsed, list):
        return []

    planned_questions: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        planned_questions.append({
            "question": question,
            "qa_task_type": normalize_task_type(item.get("qa_task_type") or item.get("task_type")),
            "conversion_mode": normalize_conversion_mode(item.get("conversion_mode")),
            "source_field": str(item.get("source_field") or "").strip() or None,
        })
    return planned_questions


def _generate_question_plan(
    llm: LocalLLM,
    content: str,
    count: int,
    *,
    system_prompt: Optional[str] = None,
    user_instruction: Optional[str] = None,
    task_spec: Optional[str] = None,
    field_context: Optional[Dict[str, Dict[str, str]]] = None,
    field_intent_map: Optional[Dict[str, List[str]]] = None,
    topic_context: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    base_system, base_user = PromptTemplates.format_qa_question_planning(
        content=content,
        count=count,
        task_spec=task_spec or "",
        user_instruction=user_instruction or "",
    )
    system = f"{base_system}\n\n补充要求：\n{system_prompt}" if system_prompt else base_system
    max_new_tokens = max(1024, min(4096, count * 160))
    response = llm.generate(
        prompt=base_user,
        system_prompt=system,
        max_new_tokens=max_new_tokens,
        temperature=0.4,
    )
    questions = _parse_question_plan_response(response)
    if questions and field_context is not None and field_intent_map is not None:
        questions = [
            item for item in questions
            if _question_plan_matches_allowed_evidence(
                item,
                field_context,
                field_intent_map,
                topic_context=topic_context,
            )
        ]
    if questions:
        return questions

    for _ in range(QA_GENERATION_RETRY):
        retry_user = (
            f"{base_user}\n\n"
            f"重试要求：本次只返回恰好{count}个问题的JSON数组，不要回答。"
        )
        response = llm.generate(
            prompt=retry_user,
            system_prompt=system,
            max_new_tokens=max_new_tokens,
            temperature=0.2,
        )
        questions = _parse_question_plan_response(response)
        if questions and field_context is not None and field_intent_map is not None:
            questions = [
                item for item in questions
                if _question_plan_matches_allowed_evidence(
                    item,
                    field_context,
                    field_intent_map,
                    topic_context=topic_context,
                )
            ]
        if questions:
            return questions
    return []


def _generate_answer_for_question(
    llm: LocalLLM,
    content: str,
    question_plan: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    question_payload = json.dumps(
        {
            "question": question_plan.get("question"),
            "qa_task_type": question_plan.get("qa_task_type"),
            "conversion_mode": question_plan.get("conversion_mode"),
            "source_field": question_plan.get("source_field"),
        },
        ensure_ascii=False,
    )
    system, user = PromptTemplates.format_qa_answer_generation(
        content=content,
        question_payload=question_payload,
    )
    response = llm.generate(
        prompt=user,
        system_prompt=system,
        max_new_tokens=1024,
        temperature=0.3,
    )
    parsed = LocalLLM.parse_json_from_response(LocalLLM._sanitize_response_text(response), prefer=dict)
    if not (isinstance(parsed, dict) and parsed.get("question") and parsed.get("answer")):
        return None
    qa_pairs = parse_qa_response(json.dumps([parsed], ensure_ascii=False))
    if not qa_pairs:
        return None
    qa = qa_pairs[0]
    qa["question"] = str(question_plan.get("question") or qa.get("question") or "").strip()
    qa["qa_task_type"] = normalize_task_type(question_plan.get("qa_task_type") or qa.get("qa_task_type"))
    qa["conversion_mode"] = normalize_conversion_mode(question_plan.get("conversion_mode") or qa.get("conversion_mode"))
    if question_plan.get("source_field") and not qa.get("source_field"):
        qa["source_field"] = question_plan.get("source_field")
    return qa


def _generate_answers_for_questions(
    llm: LocalLLM,
    content: str,
    question_plan: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not question_plan:
        return []
    questions_payload = json.dumps(question_plan, ensure_ascii=False)
    system, user = PromptTemplates.format_qa_batch_answer_generation(
        content=content,
        questions_payload=questions_payload,
    )
    response = llm.generate(
        prompt=user,
        system_prompt=system,
        max_new_tokens=max(1536, min(6144, len(question_plan) * 320)),
        temperature=0.3,
    )
    qa_pairs = parse_qa_response(response)
    if not qa_pairs:
        return []

    question_map = {
        str(item.get("question") or "").strip(): item
        for item in question_plan
        if str(item.get("question") or "").strip()
    }
    normalized_pairs: List[Dict[str, Any]] = []
    for qa in qa_pairs:
        original_question = str(qa.get("question") or "").strip()
        plan_item = question_map.get(original_question)
        if not plan_item:
            continue
        qa["question"] = original_question
        qa["qa_task_type"] = normalize_task_type(plan_item.get("qa_task_type") or qa.get("qa_task_type"))
        qa["conversion_mode"] = normalize_conversion_mode(plan_item.get("conversion_mode") or qa.get("conversion_mode"))
        if plan_item.get("source_field") and not qa.get("source_field"):
            qa["source_field"] = plan_item.get("source_field")
        normalized_pairs.append(qa)
    return normalized_pairs


def _generate_qa_pairs_two_step(
    llm: LocalLLM,
    content: str,
    count: int,
    *,
    system_prompt: Optional[str] = None,
    user_instruction: Optional[str] = None,
    task_types: Optional[List[str]] = None,
    conversion_modes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    normalized_task_types = task_types or ["protocol_understanding", "protocol_conversion"]
    normalized_conversion_modes = conversion_modes or ["transcoding", "mapping"]
    task_spec = build_task_spec(normalized_task_types, normalized_conversion_modes, count)
    generation_plan = _build_chunk_generation_plan(
        content=content,
        count=count,
        requested_task_types=normalized_task_types,
        requested_conversion_modes=normalized_conversion_modes,
    )
    question_plan = _generate_question_plan(
        llm=llm,
        content=content,
        count=count,
        system_prompt=system_prompt,
        user_instruction=user_instruction,
        task_spec=task_spec,
        field_context=generation_plan.get("field_context") or {},
        field_intent_map=generation_plan.get("field_intent_map") or {},
        topic_context=generation_plan.get("topic_context") or [],
    )
    if not question_plan:
        return []

    generated_pairs = _generate_answers_for_questions(llm, content, question_plan)
    if not generated_pairs:
        generated_pairs = []
        for question_item in question_plan:
            qa = _generate_answer_for_question(llm, content, question_item)
            if not qa:
                continue
            generated_pairs.append(qa)

    seen_pairs: Set[Tuple[str, str, str, str]] = set()
    deduped_pairs: List[Dict[str, Any]] = []
    for qa in generated_pairs:
        pair_key = _qa_pair_dedup_key(qa)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        deduped_pairs.append(qa)
        if len(deduped_pairs) >= count:
            break
    return deduped_pairs[:count]


def check_quality(
    question: str,
    answer: str,
    qa_task_type: str = "protocol_understanding",
    seed_kind: Optional[str] = None,
) -> tuple:
    """检查QA对质量"""
    if USE_LLM_QUALITY_CHECK:
        llm = get_llm_client()

        # 使用模板格式化prompt
        system, user = PromptTemplates.format_quality_check(question, answer)

        # 调用LLM检测质量
        result = llm.extract_json(user, system_prompt=system)

        if result:
            is_low_quality = result.get("is_low_quality", False)
            reason = result.get("reason", "")
            return is_low_quality, reason

    # 基于规则的简单质量检查
    is_low_quality = False
    reasons = []

    # 问题过短
    if len(question.strip()) < 8:
        is_low_quality = True
        reasons.append("问题过短")

    if qa_task_type == "protocol_conversion":
        formula = extract_formula_only(answer)
        if not formula or len(formula) < 3:
            is_low_quality = True
            reasons.append("转换公式无效")
        mode = infer_conversion_mode(formula)
        if mode == "transcoding" and not contains_arithmetic_expression(formula):
            is_low_quality = True
            reasons.append("transcoding缺少可计算公式")
        if mode == "mapping" and not (re.search(r"(?:=|->|→)", formula) or looks_like_block_formula(formula)):
            is_low_quality = True
            reasons.append("mapping缺少映射关系")
        return is_low_quality, "; ".join(reasons) if reasons else "质量合格"

    answer_text = answer.strip()
    if len(answer_text) < 6:
        is_low_quality = True
        reasons.append("答案过短")
    if _is_too_short_understanding_answer(question, answer_text, seed_kind=seed_kind):
        is_low_quality = True
        reasons.append("答案信息量不足")

    has_numeric = bool(re.search(r"\d", answer_text))
    has_mapping = bool(re.search(r"-?\d+\s*(?:=|->|→)\s*[A-Za-z_]", answer_text))
    if not (has_numeric or has_mapping):
        is_low_quality = True
        reasons.append("答案缺乏具体数值")

    # 包含模糊表述
    vague_terms = ["可能", "也许", "大概", "不确定", "不清楚"]
    if any(term in answer for term in vague_terms):
        reasons.append("答案包含模糊表述")

    return is_low_quality, "; ".join(reasons) if reasons else "质量合格"


def save_qa_pairs(
    qa_pairs: List[Dict[str, Any]],
    source_block_ids: List[int],
    dataset_id: str = None,
    instruction: str = "",
    persist_file: bool = True,
) -> List[QAPair]:
    """保存QA对到数据库和文件存储"""
    saved_pairs = []
    timestamp = int(time.time())

    for i, qa in enumerate(qa_pairs):
        # 生成QA ID
        qa_id = f"qa_{timestamp}_{i}_{uuid.uuid4().hex[:6]}"

        # 质量检查
        is_low_quality, quality_reason = check_quality(
            qa["question"],
            qa["answer"],
            qa.get("qa_task_type", "protocol_understanding"),
            seed_kind=qa.get("seed_kind"),
        )

        # 创建QAPair对象
        extracted_info = qa.get("extracted_info") if isinstance(qa.get("extracted_info"), dict) else {}
        if qa.get("source_field") and not extracted_info.get("source_field"):
            extracted_info["source_field"] = qa.get("source_field")
        if qa.get("source_fields") and not extracted_info.get("source_fields"):
            extracted_info["source_fields"] = qa.get("source_fields")
        if qa.get("target_field") and not extracted_info.get("target_field"):
            extracted_info["target_field"] = qa.get("target_field")
        if qa.get("concept_name") and not extracted_info.get("concept_name"):
            extracted_info["concept_name"] = qa.get("concept_name")
        if qa.get("formula_kind") and not extracted_info.get("formula_kind"):
            extracted_info["formula_kind"] = qa.get("formula_kind")

        qa_pair = QAPair(
            qa_id=qa_id,
            dataset_id=dataset_id or None,
            source_block_ids=[str(bid) for bid in source_block_ids],
            question=qa["question"],
            answer=qa["answer"],
            qa_task_type=qa.get("qa_task_type", "protocol_understanding"),
            conversion_mode=qa.get("conversion_mode"),
            conversion_formula=qa.get("conversion_formula"),
            source_field=qa.get("source_field"),
            source_fields=qa.get("source_fields") or normalize_source_fields_value(qa.get("source_field")),
            target_field=qa.get("target_field"),
            concept_name=qa.get("concept_name"),
            formula_kind=qa.get("formula_kind") or ("python_block" if looks_like_block_formula(qa.get("conversion_formula") or qa.get("answer")) else None),
            target_protocol_type=qa.get("target_protocol_type"),
            target_message_code=qa.get("target_message_code"),
            instruction=instruction,
            is_low_quality=is_low_quality,
            quality_reason=quality_reason if is_low_quality else None,
            extracted_info=extracted_info or None,
        )

        saved_pairs.append(qa_pair)

    # 保存到文件存储
    if persist_file and dataset_id and saved_pairs:
        try:
            file_store.save_qa_pairs(dataset_id, [qa.to_dict() for qa in saved_pairs])
        except Exception as e:
            print(f"保存QA对到文件失败: {e}")

    return saved_pairs


def _request_payload() -> Dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def generate_qa_async_requested(payload: Optional[Dict[str, Any]] = None) -> bool:
    resolved_payload = payload if isinstance(payload, dict) else _request_payload()
    return _as_bool((resolved_payload or {}).get("async"), default=True)


def _normalize_chunk_from_payload(item: Dict[str, Any], dataset_id_hint: str = "", project_id_hint: str = "") -> Chunk:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return Chunk(
        chunk_id=str(item.get("chunk_id") or f"chunk_{uuid.uuid4().hex[:8]}"),
        project_id=str(item.get("project_id") or project_id_hint or metadata.get("project_id") or "file_path_project").strip(),
        dataset_id=str(item.get("dataset_id") or dataset_id_hint or metadata.get("dataset_id") or "").strip(),
        source_block_ids=[int(bid) for bid in item.get("source_block_ids", []) if str(bid).strip()],
        semantic_type=str(item.get("semantic_type") or metadata.get("semantic_type") or "general_content"),
        content_snapshot=str(item.get("content_snapshot") or ""),
        metadata=metadata,
    )


def _load_chunks_from_file(chunks_file_path: str) -> Tuple[str, str, List[Chunk], Dict[str, Dict[str, Any]]]:
    resolved_path = os.path.abspath(os.path.expanduser(str(chunks_file_path or "").strip()))
    if not resolved_path or not os.path.exists(resolved_path):
        raise FileNotFoundError(f"chunks_file_path不存在: {chunks_file_path}")
    with open(resolved_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("chunks文件内容必须为JSON对象")
    chunk_items = payload.get("chunks")
    if not isinstance(chunk_items, list) or not chunk_items:
        raise ValueError("chunks文件缺少chunks数组")
    dataset_id = str(payload.get("dataset_id") or "").strip()
    project_id = str(payload.get("project_id") or "").strip()
    chunk_models: List[Chunk] = []
    payload_map: Dict[str, Dict[str, Any]] = {}
    for item in chunk_items:
        if not isinstance(item, dict):
            continue
        chunk = _normalize_chunk_from_payload(item, dataset_id_hint=dataset_id, project_id_hint=project_id)
        if not dataset_id and chunk.dataset_id:
            dataset_id = chunk.dataset_id
        if not project_id and chunk.project_id:
            project_id = chunk.project_id
        chunk_models.append(chunk)
        payload_map[chunk.chunk_id] = item
    if not chunk_models:
        raise ValueError("chunks文件中没有可用语义块")
    return dataset_id, project_id, chunk_models, payload_map


def _collect_content_from_chunk_payloads(chunk_payloads: List[Dict[str, Any]]) -> Tuple[str, List[int]]:
    text_parts: List[str] = []
    block_ids: List[int] = []
    for item in chunk_payloads:
        merged_content = str(item.get("merged_content") or "").strip()
        if merged_content:
            text_parts.append(merged_content)
        else:
            source_blocks = item.get("source_blocks") or []
            source_parts = []
            for block in source_blocks:
                if not isinstance(block, dict):
                    continue
                text = str(block.get("cleaned_content") or block.get("content") or "").strip()
                if text:
                    source_parts.append(text)
                try:
                    block_id = int(block.get("block_id") or 0)
                except (TypeError, ValueError):
                    block_id = 0
                if block_id:
                    block_ids.append(block_id)
            if source_parts:
                text_parts.append("\n\n".join(source_parts))
            elif item.get("content_snapshot"):
                text_parts.append(str(item.get("content_snapshot")))
        for block_id in item.get("source_block_ids", []) or []:
            try:
                normalized_block_id = int(block_id)
            except (TypeError, ValueError):
                continue
            block_ids.append(normalized_block_id)
    dedup_texts = []
    seen_texts = set()
    for text in text_parts:
        normalized = str(text or "").strip()
        if not normalized or normalized in seen_texts:
            continue
        seen_texts.add(normalized)
        dedup_texts.append(normalized)
    unique_block_ids = []
    seen_ids = set()
    for block_id in block_ids:
        if block_id in seen_ids:
            continue
        seen_ids.add(block_id)
        unique_block_ids.append(block_id)
    return "\n\n".join(dedup_texts), unique_block_ids


def _build_generation_units_from_chunk_payloads(chunk_payloads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将chunk文件载荷解析为逐chunk生成单元。"""
    generation_units: List[Dict[str, Any]] = []
    for item in chunk_payloads:
        if not isinstance(item, dict):
            continue
        content, block_ids = _collect_content_from_chunk_payloads([item])
        chunk_id = str(item.get("chunk_id") or "").strip()
        generation_units.append({
            "unit_id": chunk_id or f"chunk_{len(generation_units)}",
            "content": str(content or "").strip(),
            "source_block_ids": block_ids,
            "source_chunk_ids": [chunk_id] if chunk_id else [],
        })
    return generation_units


def _select_chunks_from_file(
    chunks_file_path: str,
    selected_chunk_ids: Optional[List[str]] = None,
) -> Tuple[str, str, List[Chunk], List[Dict[str, Any]]]:
    dataset_id, project_id, chunk_models, payload_map = _load_chunks_from_file(chunks_file_path)
    normalized_ids = [str(item).strip() for item in (selected_chunk_ids or []) if str(item).strip()]
    if not normalized_ids:
        return dataset_id, project_id, chunk_models, [payload_map[chunk.chunk_id] for chunk in chunk_models if chunk.chunk_id in payload_map]

    selected_set = set(normalized_ids)
    selected_chunks = [chunk for chunk in chunk_models if chunk.chunk_id in selected_set]
    selected_payloads = [payload_map[chunk_id] for chunk_id in normalized_ids if chunk_id in payload_map]
    return dataset_id, project_id, selected_chunks, selected_payloads


def _execute_generate_qa(
    data: Dict[str, Any],
    progress_callback: Optional[callable] = None,
) -> Tuple[Dict[str, Any], int]:
    """执行 QA 生成主流程。"""
    def emit(stage: str, message: str, progress: float, extra: Optional[Dict[str, Any]] = None) -> None:
        if progress_callback is not None:
            progress_callback(stage, message, progress, extra)

    try:
        if not data:
            return {"code": 400, "message": "请求体不能为空", "data": None}, 400

        source_chunk_ids = data.get("source_chunk_ids")
        source_id_type = "chunk"
        if source_chunk_ids is None:
            source_chunk_ids = data.get("source_block_ids", [])
            source_id_type = "block"
        if isinstance(source_chunk_ids, (str, int)):
            source_chunk_ids = [source_chunk_ids]
        elif not isinstance(source_chunk_ids, list):
            source_chunk_ids = []
        source_chunk_ids = [str(item).strip() for item in source_chunk_ids if str(item).strip()]

        dataset_id = str(data.get("dataset_id") or "").strip() or None
        chunks_file_path = str(data.get("chunks_file_path") or "").strip()
        selection_config = data.get("selection_config", {}) or {}
        if not isinstance(selection_config, dict):
            selection_config = {}
        prompt_config = data.get("prompt_config", {})
        task_config = data.get("task_config", {})
        count = max(1, int(data.get("count", 5) or 5))

        raw_task_types = task_config.get("task_types", ["protocol_understanding", "protocol_conversion"])
        task_types = []
        for item in raw_task_types:
            normalized = normalize_task_type(item)
            if normalized not in task_types:
                task_types.append(normalized)
        if not task_types:
            task_types = ["protocol_understanding", "protocol_conversion"]

        raw_conversion_modes = task_config.get("conversion_modes", ["transcoding", "mapping"])
        conversion_modes = []
        for item in raw_conversion_modes:
            normalized_mode = normalize_conversion_mode(item)
            if normalized_mode and normalized_mode not in conversion_modes:
                conversion_modes.append(normalized_mode)
        if not conversion_modes:
            conversion_modes = ["transcoding", "mapping"]

        auto_select = bool(selection_config.get("auto_select", False))
        legacy_dataset_lookup = bool(dataset_id) and not source_chunk_ids and not chunks_file_path

        if auto_select and not chunks_file_path:
            return {"code": 400, "message": "自动选块仅支持 chunks_file_path 输入", "data": None}, 400

        if not source_chunk_ids and not chunks_file_path and not legacy_dataset_lookup:
            return {"code": 400, "message": "source_chunk_ids或chunks_file_path至少提供一个", "data": None}, 400

        emit("resolving_inputs", "正在解析 QA 生成输入", 8.0)
        selected_chunk_ids: List[str] = []
        selection_mode = "manual"
        file_chunk_models: List[Chunk] = []
        file_chunk_payloads: List[Dict[str, Any]] = []
        file_dataset_id = ""

        if chunks_file_path:
            file_dataset_id, _, file_chunk_models, file_chunk_payloads = _select_chunks_from_file(chunks_file_path)
            if file_dataset_id and not dataset_id:
                dataset_id = file_dataset_id

        if legacy_dataset_lookup:
            selection_mode = "legacy_dataset"
            dataset_chunks = db_client.get_chunks_by_dataset(dataset_id)
            if not dataset_chunks:
                dataset_chunks = [
                    _normalize_chunk_from_payload(item, dataset_id_hint=dataset_id)
                    for item in file_store.load_chunks(dataset_id)
                    if isinstance(item, dict)
                ]
            if not dataset_chunks:
                return {"code": 404, "message": f"未找到dataset_id={dataset_id}的语义块", "data": None}, 404
            selected_chunks = select_chunks_for_qa(
                chunks=dataset_chunks,
                task_types=task_types,
                count=count,
                selection_config=selection_config,
            )
            if not selected_chunks:
                return {"code": 400, "message": "旧接口兼容模式下未找到满足条件的chunk", "data": None}, 400
            selected_chunk_ids = [str(chunk.metadata.get("chunk_record_id") or chunk.chunk_id) for chunk in selected_chunks]
            source_chunk_ids = selected_chunk_ids
        elif auto_select:
            selection_mode = "auto"
            dataset_chunks = list(file_chunk_models)
            if not dataset_chunks:
                return {"code": 400, "message": "自动选块需要 chunks_file_path 中提供 chunks 数据", "data": None}, 400
            selected_chunks = select_chunks_for_qa(
                chunks=dataset_chunks,
                task_types=task_types,
                count=count,
                selection_config=selection_config,
            )
            if not selected_chunks:
                return {"code": 400, "message": "自动选块失败，未找到满足条件的chunk", "data": None}, 400
            selected_chunk_ids = [chunk.chunk_id for chunk in selected_chunks]
            source_chunk_ids = selected_chunk_ids
            if chunks_file_path:
                _, _, _, file_chunk_payloads = _select_chunks_from_file(chunks_file_path, selected_chunk_ids=selected_chunk_ids)
        else:
            selected_chunk_ids = list(source_chunk_ids)

        resolved_dataset_id = file_dataset_id or dataset_id
        generation_units: List[Dict[str, Any]] = []
        if chunks_file_path:
            _, _, _, selected_payloads = _select_chunks_from_file(chunks_file_path, selected_chunk_ids=selected_chunk_ids)
            payloads_for_generation = selected_payloads or file_chunk_payloads
            generation_units = _build_generation_units_from_chunk_payloads(payloads_for_generation)
        if not generation_units:
            generation_units, resolved_dataset_id = get_generation_units(
                source_chunk_ids,
                dataset_id=dataset_id,
                source_id_type=source_id_type,
            )
        if resolved_dataset_id:
            dataset_id = resolved_dataset_id

        if not generation_units:
            return {"code": 400, "message": "无法获取可用的生成块", "data": None}, 400

        empty_unit_ids = [unit["unit_id"] for unit in generation_units if not str(unit.get("content") or "").strip()]
        if empty_unit_ids:
            return {"code": 400, "message": f"以下块缺少可用内容，无法逐块生成QA: {empty_unit_ids}", "data": None}, 400

        emit(
            "generating",
            "已解析生成块，开始批量生成 QA",
            20.0,
            {
                "dataset_id": dataset_id,
                "block_count": len(generation_units),
                "count_per_block": count,
                "selection_mode": selection_mode,
            },
        )
        raw_system_prompt = prompt_config.get("system_prompt")
        raw_user_instruction = prompt_config.get("user_instruction")
        system_prompt = _sanitize_custom_prompt_text(raw_system_prompt, task_types)
        user_instruction = _sanitize_custom_prompt_text(raw_user_instruction, task_types)
        instruction = system_prompt or raw_system_prompt or ""
        saved_pairs: List[QAPair] = []
        qa_pairs_response: List[Dict[str, Any]] = []
        skipped_units: List[Dict[str, Any]] = []
        effective_unit_count = 0

        prepared_units: List[Dict[str, Any]] = []
        for unit in generation_units:
            unit_content = str(unit.get("content") or "").strip()
            generation_plan = _build_chunk_generation_plan(
                content=unit_content,
                count=count,
                requested_task_types=task_types,
                requested_conversion_modes=conversion_modes,
            )
            probe_chunk = Chunk(
                chunk_id=str(unit.get("unit_id") or ""),
                project_id="",
                dataset_id=str(dataset_id or ""),
                source_block_ids=[],
                semantic_type="general_content",
                content_snapshot=unit_content,
                metadata={},
            )
            if _is_toc_like_chunk(probe_chunk):
                skipped_units.append({
                    "unit_id": unit.get("unit_id"),
                    "source_block_ids": unit.get("source_block_ids") or [],
                    "source_chunk_ids": unit.get("source_chunk_ids") or [],
                    "reason": "toc_chunk",
                    "detail": "块内容属于目录/章节索引，已前置跳过，不参与QA生成",
                })
                continue

            field_context = generation_plan["field_context"]
            topic_context = generation_plan["topic_context"]
            understanding_fields = generation_plan["understanding_fields"]
            conversion_candidates = generation_plan["conversion_candidates"]
            unit_task_types = generation_plan["effective_task_types"]
            unit_conversion_modes = generation_plan["effective_conversion_modes"] or conversion_modes
            chunk_pattern = generation_plan.get("chunk_pattern") or "general"
            is_requirement_chunk = _is_requirement_chunk(unit_content, field_context, topic_context)
            probe = _fast_probe_unit(unit_content)
            supported_capacity = _estimate_fast_supported_capacity(probe, unit_content)
            priority_score = _estimate_fast_unit_priority(probe, generation_plan, unit_content)
            has_field_coding_signal = FIELD_CODING_HINT_PATTERN.search(unit_content) is not None
            if (
                not field_context
                and len(topic_context) <= 1
                and not is_requirement_chunk
                and not has_field_coding_signal
                and not probe.get("has_structured_signal")
                and not probe.get("has_numeric")
                and not probe.get("has_enum")
            ):
                skipped_units.append({
                    "unit_id": unit.get("unit_id"),
                    "source_block_ids": unit.get("source_block_ids") or [],
                    "source_chunk_ids": unit.get("source_chunk_ids") or [],
                    "reason": "low_signal_chunk",
                    "detail": "块内缺少可支撑高质量QA的字段、规则或足够主题锚点，已跳过",
                })
                continue
            if chunk_pattern == "general" and not conversion_candidates and not (
                probe.get("has_numeric") or probe.get("has_enum") or probe.get("has_layout_signal")
            ):
                skipped_units.append({
                    "unit_id": unit.get("unit_id"),
                    "source_block_ids": unit.get("source_block_ids") or [],
                    "source_chunk_ids": unit.get("source_chunk_ids") or [],
                    "reason": "general_chunk_without_support",
                    "detail": "通用块缺少可稳定支撑问答的数值、枚举、布局或转换信号，已跳过",
                })
                continue
            if supported_capacity <= 0:
                skipped_units.append({
                    "unit_id": unit.get("unit_id"),
                    "source_block_ids": unit.get("source_block_ids") or [],
                    "source_chunk_ids": unit.get("source_chunk_ids") or [],
                    "reason": "unsupported_chunk",
                    "detail": "块缺少可稳定支撑QA的结构化证据，直接跳过",
                })
                continue
            if priority_score < 0.9 and not is_requirement_chunk:
                skipped_units.append({
                    "unit_id": unit.get("unit_id"),
                    "source_block_ids": unit.get("source_block_ids") or [],
                    "source_chunk_ids": unit.get("source_chunk_ids") or [],
                    "reason": "weak_fast_path_unit",
                    "detail": "块的结构化信号和可答信息不足，不纳入 fast-path 平均预算",
                })
                continue

            prepared_units.append({
                "unit_id": str(unit.get("unit_id") or ""),
                "unit": unit,
                "content": unit_content,
                "generation_plan": generation_plan,
                "field_context": field_context,
                "topic_context": topic_context,
                "understanding_fields": understanding_fields,
                "conversion_candidates": conversion_candidates,
                "unit_task_types": unit_task_types,
                "unit_conversion_modes": unit_conversion_modes,
                "chunk_pattern": chunk_pattern,
                "is_requirement_chunk": is_requirement_chunk,
                "has_field_coding_signal": has_field_coding_signal,
                "minimum_target": 2 if is_requirement_chunk and count == 1 else 1,
                "supported_capacity": supported_capacity,
                "probe": probe,
                "priority_score": priority_score,
                "fast_instruction": _build_fast_generation_instruction(generation_plan, probe),
            })

        if not prepared_units:
            return {
                "code": 200,
                "message": "success",
                "data": {
                    "task_id": f"gen_{int(time.time())}",
                    "dataset_id": dataset_id,
                    "qa_file_path": None,
                    "total_count": 0,
                    "high_quality_count": 0,
                    "low_quality_count": 0,
                    "count_per_block": count,
                    "block_count": len(generation_units),
                    "effective_block_count": 0,
                    "skipped_block_count": len(skipped_units),
                    "selection_mode": selection_mode,
                    "selected_chunk_ids": selected_chunk_ids,
                    "skipped_units": skipped_units,
                    "qa_pairs": [],
                },
            }, 200

        unit_target_counts = _compute_fast_unit_target_counts(prepared_units, count)
        target_total_count = sum(unit_target_counts.values())
        effective_unit_count = len(prepared_units)
        print(
            "QA generation prepared units:",
            json.dumps(
                [
                    {
                        "unit_id": item.get("unit_id"),
                        "pattern": item.get("chunk_pattern"),
                        "fields": len(item.get("field_context") or {}),
                        "probe": item.get("probe"),
                        "supported_capacity": item.get("supported_capacity"),
                        "planned_target": unit_target_counts.get(str(item.get("unit_id") or ""), count),
                    }
                    for item in prepared_units
                ],
                ensure_ascii=False,
            ),
        )
        for prepared in prepared_units:
            prepared["target_count"] = int(unit_target_counts.get(str(prepared.get("unit_id") or ""), count) or count)

        generation_batches = _build_generation_batches(prepared_units)
        llm = get_llm_client()
        all_selected_qas: List[Dict[str, Any]] = []
        model_call_count = 0
        batch_shortfall_summary: Dict[str, int] = {}

        total_batches = max(1, len(generation_batches))
        for index, batch in enumerate(generation_batches, start=1):
            emit(
                "generating",
                f"正在生成第 {index}/{total_batches} 个 batch 的 QA",
                20.0 + (index - 1) / total_batches * 68.0,
                {
                    "dataset_id": dataset_id,
                    "batch_id": batch.get("batch_id"),
                    "current_batch": index,
                    "total_batches": total_batches,
                },
            )
            batch_instruction_parts = [str(user_instruction or "").strip()]
            for unit_info in batch.get("units") or []:
                fast_instruction = str(unit_info.get("fast_instruction") or "").strip()
                if fast_instruction:
                    batch_instruction_parts.append(
                        f"[{unit_info.get('unit_id')}]\n{fast_instruction}"
                    )
            batch_user_instruction = "\n\n".join(part for part in batch_instruction_parts if part)
            raw_candidates = _generate_fast_batch_candidates(
                llm,
                batch,
                system_prompt=system_prompt,
                user_instruction=batch_user_instruction,
            )
            model_call_count += 1 + (1 if not raw_candidates and QA_GENERATION_RETRY > 0 else 0)
            batch_unit_map = {str(item.get("unit_id") or ""): item for item in (batch.get("units") or [])}
            normalized_candidates = _normalize_fast_batch_candidates(raw_candidates, batch_unit_map)
            selected_qas, batch_shortfall = _filter_fast_batch_candidates(normalized_candidates, batch)
            for segment_id, deficit in batch_shortfall.items():
                batch_shortfall_summary[segment_id] = batch_shortfall_summary.get(segment_id, 0) + deficit
            print(
                f"QA batch result batch_id={batch.get('batch_id')} "
                f"unit_count={len(batch.get('units') or [])} candidate_count={len(normalized_candidates)} "
                f"filtered_count={len(selected_qas)} target={batch.get('batch_target_total')} shortfall={sum(batch_shortfall.values())}"
            )
            all_selected_qas.extend(selected_qas)

        final_qas = _finalize_global_qa_pool(all_selected_qas, target_total_count)
        if len(final_qas) < target_total_count:
            print(
                f"WARNING: QA generation shortfall final_count={len(final_qas)} "
                f"target_total={target_total_count} model_calls={model_call_count}"
            )

        segment_unit_map = {str(item.get("unit_id") or ""): item for item in prepared_units}
        segment_qas: Dict[str, List[Dict[str, Any]]] = {}
        for qa in final_qas:
            segment_id = str(qa.get("segment_id") or "").strip()
            if segment_id:
                segment_qas.setdefault(segment_id, []).append(qa)

        for prepared in prepared_units:
            unit_id = str(prepared.get("unit_id") or "")
            unit = prepared["unit"]
            unit_qas = segment_qas.get(unit_id, [])
            if not unit_qas:
                skipped_units.append({
                    "unit_id": unit.get("unit_id"),
                    "source_block_ids": unit.get("source_block_ids") or [],
                    "source_chunk_ids": unit.get("source_chunk_ids") or [],
                    "reason": "insufficient_filtered_qas",
                    "detail": f"batch 生成后无可用QA，计划均值目标{prepared.get('target_count')}条",
                })
                continue

            for qa in unit_qas:
                if qa.get("qa_task_type") == "protocol_understanding":
                    qa["answer"] = enhance_understanding_answer(
                        question=qa.get("question", ""),
                        answer=qa.get("answer", ""),
                        field_context=prepared.get("field_context") or {},
                    )

            unit_saved_pairs = save_qa_pairs(
                qa_pairs=unit_qas,
                source_block_ids=unit.get("source_block_ids") or [],
                dataset_id=dataset_id,
                instruction=instruction,
                persist_file=False,
            )
            saved_pairs.extend(unit_saved_pairs)

            unit_source_chunk_ids = unit.get("source_chunk_ids") or []
            for qa in unit_saved_pairs:
                qa_pairs_response.append({
                    "qa_id": qa.qa_id,
                    "insturctor": qa.instruction,
                    "question": qa.question,
                    "answer": qa.answer,
                    "qa_task_type": qa.qa_task_type,
                    "conversion_mode": qa.conversion_mode,
                    "conversion_formula": qa.conversion_formula,
                    "source_field": (qa.extracted_info or {}).get("source_field") if qa.extracted_info else None,
                    "target_field": (qa.extracted_info or {}).get("target_field") if qa.extracted_info else None,
                    "is_low_quality": qa.is_low_quality,
                    "reason": qa.quality_reason,
                    "quality_reason": qa.quality_reason,
                    "source_block_ids": qa.source_block_ids,
                    "source_chunk_ids": unit_source_chunk_ids,
                })

        if not saved_pairs and prepared_units:
            raise RuntimeError("所有有效块均未生成可保存的QA")

        emit(
            "persisting",
            "QA 已生成，正在落盘保存",
            92.0,
            {"dataset_id": dataset_id, "total_count": len(saved_pairs)},
        )
        qa_file_path = None
        if dataset_id and saved_pairs:
            try:
                qa_file_path = file_store.save_qa_pairs(dataset_id, [qa.to_dict() for qa in saved_pairs])
            except Exception:
                qa_file_path = None

        task_id = f"gen_{int(time.time())}"
        result = {
            "code": 200,
            "message": "success",
            "data": {
                "task_id": task_id,
                "dataset_id": dataset_id,
                "qa_file_path": qa_file_path,
                "total_count": len(saved_pairs),
                "high_quality_count": sum(1 for qa in saved_pairs if not qa.is_low_quality),
                "low_quality_count": sum(1 for qa in saved_pairs if qa.is_low_quality),
                "count_per_block": count,
                "block_count": len(generation_units),
                "effective_block_count": effective_unit_count,
                "skipped_block_count": len(skipped_units),
                "selection_mode": selection_mode,
                "selected_chunk_ids": selected_chunk_ids,
                "skipped_units": skipped_units,
                "qa_pairs": qa_pairs_response,
            },
        }
        emit(
            "completed",
            "QA 生成任务完成",
            100.0,
            {"dataset_id": dataset_id, "total_count": len(saved_pairs)},
        )
        return result, 200

    except Exception as exc:
        return {"code": 500, "message": f"生成QA对失败: {str(exc)}", "data": None}, 500


def run_generate_qa_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    response_body, status_code = _execute_generate_qa(data)
    return {"status_code": status_code, "result": response_body}


def submit_generate_qa_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_chunk_ids = payload.get("source_chunk_ids") or payload.get("source_block_ids") or []
    metadata = {
        "dataset_id": str(payload.get("dataset_id") or "").strip() or None,
        "chunk_count": len(source_chunk_ids) if isinstance(source_chunk_ids, list) else None,
        "count_per_block": payload.get("count"),
        "async_mode": True,
    }
    return start_job(
        "generate_qa",
        lambda job_id: _run_generate_qa_job(job_id, payload),
        metadata=metadata,
    )


def _run_generate_qa_job(job_id: str, payload: Dict[str, Any]) -> None:
    def emit(stage: str, message: str, progress: float, extra: Optional[Dict[str, Any]] = None) -> None:
        update_job(
            job_id,
            status="running",
            stage=stage,
            message=message,
            progress=progress,
            extra=extra,
        )

    emit("preparing", "开始准备 QA 生成任务", 1.0)
    response_body, status_code = _execute_generate_qa(payload, progress_callback=emit)
    if status_code >= 400:
        fail_job(job_id, str(response_body.get("message") or "QA 生成失败"), result=response_body)
        return
    complete_job(job_id, response_body)


@app.route("/api/knowledge/generate_qa", methods=["POST"])
def generate_qa():
    """QA对生成接口。默认异步，async=false 时同步执行。"""
    payload = _request_payload()
    if generate_qa_async_requested(payload):
        job = submit_generate_qa_job(payload or {})
        return build_submit_response(job)
    response_body, status_code = _execute_generate_qa(payload or {})
    return jsonify(response_body), status_code


@app.route("/api/knowledge/generate_qa/status", methods=["GET"])
def generate_qa_status():
    job_id = str(request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    return build_status_response(job_id)


@app.route("/api/knowledge/generate_qa/stream", methods=["GET"])
def generate_qa_stream():
    job_id = str(request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    return build_stream_response(job_id)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    server_config = get_service_runner_config()
    app.run(
        host=server_config.get("host", "0.0.0.0"),
        port=int(server_config.get("port", 6105)),
        debug=bool(server_config.get("debug", True)),
        threaded=bool(server_config.get("threaded", False)),
    )


if __name__ == "__main__":
    server_config = get_service_runner_config()
    app.run(
        host=server_config.get("host", "0.0.0.0"),
        port=int(server_config.get("port", 6105)),
        debug=bool(server_config.get("debug", False)),
        threaded=bool(server_config.get("threaded", False)),
    )

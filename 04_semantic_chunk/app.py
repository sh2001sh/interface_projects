from runtime_config import apply_runtime_environment, get_service_runner_config
from streaming_utils import is_stream_requested, stream_flask_handler
# 接口6: 语义单元智能划分与重组
# POST /api/data/semantic_chunk

import os
import sys
import json
import uuid
import time
import logging
import re
import inspect
from pathlib import Path
from typing import Callable, List, Dict, Any, Optional, Tuple, Set
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.mysql_client import MySQLClient
from database.models import Block, Chunk
from document_parsing import load_blocks_from_document_paths
from semantic_chunk_features import (
    build_merge_context,
    count_mapping_pairs as count_mapping_pairs_feature,
    determine_block_semantic_type,
    estimate_evidence_score as estimate_evidence_score_feature,
    estimate_noise_penalty as estimate_noise_penalty_feature,
    estimate_tokens as estimate_feature_tokens,
    extract_field_names as extract_feature_field_names,
    extract_protocol_anchor as extract_feature_protocol_anchor,
    extract_section_keys as extract_feature_section_keys,
    extract_structure_tags as extract_feature_structure_tags,
    get_block_content as get_feature_block_content,
    merge_block_contents as merge_feature_block_contents,
    normalize_protocol_family as normalize_feature_protocol_family,
)
from llm.local_llm import LocalLLM, get_llm
from protocol_conversion import build_protocol_doc_index
from protocol_conversion.trained_doc_index import DEFAULT_SHARD_MAX_CHARS, DEFAULT_SHARD_MAX_PAGES
from utils.file_store import FileStore
from job_runtime import (
    build_status_response,
    build_stream_response,
    build_submit_response,
    complete_job,
    fail_job,
    get_job_snapshot,
    start_job,
    update_job,
)

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


apply_runtime_environment()

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.json.ensure_ascii = False

# 初始化客户端
mysql_client = MySQLClient()
llm_client: Optional[LocalLLM] = None
file_store = FileStore()
try:
    mysql_client.init_tables()
except Exception as exc:
    logger.warning("数据库表初始化失败: %s", exc)

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
AUTO_ASYNC_TRIGGER_MB = 20
AUTO_ASYNC_TRIGGER_PDF_PAGES = 120
AUTO_ASYNC_TRIGGER_ESTIMATED_SHARDS = 8
AUTO_ASYNC_TRIGGER_ESTIMATED_CHARS = 240000
AUTO_ASYNC_SAMPLE_PAGES = 8


def get_llm_client() -> LocalLLM:
    """获取LLM客户端（延迟初始化）"""
    global llm_client
    if llm_client is None:
        llm_client = get_llm()
    return llm_client



def estimate_tokens(text: str) -> int:
    return estimate_feature_tokens(text)


def get_block_content(block: Block) -> str:
    return get_feature_block_content(block)


def merge_block_contents(blocks: List[Block]) -> Tuple[str, int]:
    return merge_feature_block_contents(blocks)


def extract_protocol_anchor(content: str, metadata: Dict[str, Any]) -> str:
    return extract_feature_protocol_anchor(content, metadata)


def normalize_protocol_family(anchor: str) -> str:
    return normalize_feature_protocol_family(anchor)


def extract_section_keys(content: str, metadata: Dict[str, Any], protocol_anchor: str = "") -> Set[str]:
    return extract_feature_section_keys(content, metadata, protocol_anchor=protocol_anchor)


def extract_structure_tags(content: str, block_type: str, metadata: Dict[str, Any]) -> Set[str]:
    return extract_feature_structure_tags(content, block_type, metadata)


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
    return extract_feature_field_names(content, metadata)


def count_mapping_pairs(text: str) -> int:
    return count_mapping_pairs_feature(text)


def _iter_nonempty_lines(text: str) -> List[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def estimate_noise_penalty(content: str, block_type: str, metadata: Dict[str, Any]) -> float:
    return estimate_noise_penalty_feature(content, block_type, metadata)


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
    return estimate_evidence_score_feature(
        content,
        block_type,
        protocol_anchor,
        field_names,
        formula_count,
        mapping_pair_count,
        bit_coverage_count,
        range_coverage_count,
    )


def collect_block_features(block: Block) -> Dict[str, Any]:
    """聚合单个块的结构化特征，用于规则分块和质量评分。"""
    content = get_block_content(block)
    metadata = block.metadata if isinstance(block.metadata, dict) else {}
    protocol_fields = metadata.get("protocol_fields") if isinstance(metadata.get("protocol_fields"), list) else []

    formula_count = 0
    bit_coverage_count = 0
    range_coverage_count = 0
    mapping_pair_count = count_mapping_pairs(content)

    for field in protocol_fields:
        if not isinstance(field, dict):
            continue
        formula_text = str(field.get("formula") or "").strip()
        meaning_text = str(field.get("meaning") or "").strip()
        if formula_text:
            formula_count += 1
        if field.get("bit_start") is not None or field.get("bit_length") is not None:
            bit_coverage_count += 1
        if field.get("range_min") is not None or field.get("range_max") is not None:
            range_coverage_count += 1
        mapping_pair_count += count_mapping_pairs(formula_text) + count_mapping_pairs(meaning_text)

    content_has_formula = bool(FORMULA_HINT_PATTERN.search(content)) or formula_count > 0
    semantic_hint = determine_semantic_type([block])
    protocol_anchor = extract_protocol_anchor(content, metadata)
    protocol_family = normalize_protocol_family(protocol_anchor)
    field_names = extract_field_names(content, metadata)
    section_keys = extract_section_keys(content, metadata, protocol_anchor=protocol_anchor)
    structure_tags = extract_structure_tags(
        content,
        str(getattr(block, "block_type", "") or "text").lower(),
        metadata,
    )
    noise_penalty = estimate_noise_penalty(content, str(getattr(block, "block_type", "") or "text").lower(), metadata)
    evidence_score = estimate_evidence_score(
        content=content,
        block_type=str(getattr(block, "block_type", "") or "text").lower(),
        protocol_anchor=protocol_anchor,
        field_names=field_names,
        formula_count=formula_count + (1 if content_has_formula else 0),
        mapping_pair_count=mapping_pair_count,
        bit_coverage_count=bit_coverage_count,
        range_coverage_count=range_coverage_count,
    )

    return {
        "block": block,
        "block_id": block.block_id,
        "page_num": int(getattr(block, "page_num", 0) or 0),
        "block_type": str(getattr(block, "block_type", "") or "text").lower(),
        "region_role": str(metadata.get("region_role") or "").strip(),
        "content": content,
        "token_count": estimate_tokens(content),
        "protocol_anchor": protocol_anchor,
        "protocol_family": protocol_family,
        "field_names": field_names,
        "section_keys": section_keys,
        "structure_tags": structure_tags,
        "formula_count": formula_count + (1 if content_has_formula else 0),
        "mapping_pair_count": mapping_pair_count,
        "bit_coverage_count": bit_coverage_count,
        "range_coverage_count": range_coverage_count,
        "semantic_hint": semantic_hint,
        "noise_penalty": noise_penalty,
        "evidence_score": evidence_score,
    }


def _is_low_value_chunk_source(feature: Dict[str, Any]) -> bool:
    """Return true for extracted text fragments that should not become chunks."""

    if feature.get("block_type") in {"table", "code"}:
        return False
    if feature.get("protocol_anchor") or feature.get("section_keys") or feature.get("field_names"):
        return False
    content = str(feature.get("content") or "").strip()
    if not content:
        return True
    compact = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", content)
    noise_penalty = float(feature.get("noise_penalty", 0.0) or 0.0)
    token_count = int(feature.get("token_count", 0) or 0)
    if len(compact) < 3:
        return True
    if float(feature.get("evidence_score", 0.0) or 0.0) >= 6.0:
        return False
    if len(compact) < 8 and noise_penalty >= 4.0:
        return True
    if token_count <= 16 and noise_penalty >= 8.0:
        return True
    if noise_penalty >= 10.0 and len(compact) < 24:
        return True
    return False


def _is_local_region_table(feature: Dict[str, Any]) -> bool:
    return feature.get("block_type") == "table" and str(feature.get("region_role") or "").startswith("local_")


def _score_merge_candidate(
    current_group: List[Dict[str, Any]],
    next_feature: Dict[str, Any],
    max_token_size: int,
) -> Tuple[int, List[str], int]:
    """评估下一个块是否应并入当前组。"""
    prev_feature = current_group[-1]
    reasons: List[str] = []
    score = 0
    if _is_local_region_table(prev_feature) or _is_local_region_table(next_feature):
        return -99, ["局部区域表格独立成块"], sum(item["token_count"] for item in current_group) + next_feature["token_count"]
    if prev_feature["block_type"] == "table" or next_feature["block_type"] == "table":
        return -99, ["表格块独立成块"], sum(item["token_count"] for item in current_group) + next_feature["token_count"]
    prev_anchor = prev_feature["protocol_anchor"]
    next_anchor = next_feature["protocol_anchor"]

    projected_tokens = sum(item["token_count"] for item in current_group) + next_feature["token_count"]
    if projected_tokens > int(max_token_size * 1.35):
        return -99, ["超过最大token预算"], projected_tokens
    context = build_merge_context(current_group, next_feature, max_token_size)
    if projected_tokens > max_token_size:
        score -= 2
        reasons.append("接近token上限")
    elif context.token_pressure <= 0.82 and (
        context.shared_section_keys
        or context.same_message_layout
        or context.same_table_series
        or context.shared_fields
        or (prev_anchor and next_anchor)
    ):
        score += 1
        reasons.append("token余量充足")

    prev_family = prev_feature.get("protocol_family", "")
    next_family = next_feature.get("protocol_family", "")
    prev_structure_tags = set(prev_feature.get("structure_tags", set()))
    next_structure_tags = set(next_feature.get("structure_tags", set()))
    page_gap = context.page_gap

    if context.shared_section_keys:
        score += 5
        reasons.append("章节标识一致")
    elif context.same_message_layout and prev_family and next_family and prev_family == next_family:
        score += 4
        reasons.append("同消息布局")
    elif context.same_table_series and page_gap <= 1 and prev_family and next_family and prev_family == next_family:
        score += 3
        reasons.append("表格序列连续")

    if prev_anchor and next_anchor:
        if prev_anchor == next_anchor:
            score += 2
            reasons.append("同协议锚点")
        elif prev_family and next_family and prev_family == next_family:
            score += 1
            reasons.append("同协议族")
        else:
            score -= 3
            reasons.append("协议锚点变化")

    if context.shared_fields:
        score += 3
        reasons.append("字段重叠")
    elif context.same_message_layout and prev_family and next_family and prev_family == next_family:
        score += 1
        reasons.append("消息字段上下文连续")

    if page_gap <= 1:
        score += 1
        reasons.append("页码连续")
    elif page_gap == 2 and (
        context.same_message_layout
        or context.same_table_series
        or bool(context.shared_section_keys)
        or bool(context.shared_fields)
    ):
        reasons.append("跨页但上下文延续")
    elif page_gap >= 3:
        score -= 2
        reasons.append("页码跨度大")

    if prev_feature["block_type"] == next_feature["block_type"]:
        if (
            context.shared_section_keys
            or context.same_message_layout
            or context.same_table_series
            or context.shared_fields
            or prev_feature["semantic_hint"] == next_feature["semantic_hint"]
        ):
            score += 1
            reasons.append("块类型一致")
    elif context.structure_transition == "hard":
        if context.shared_section_keys or context.same_message_layout or context.same_table_series:
            score += 1
            reasons.append("跨结构但同一章节")
        else:
            score -= 3
            reasons.append("结构类型差异")

    current_group_tags = set().union(*(item.get("structure_tags", set()) for item in current_group))
    if (
        "narrative" in current_group_tags
        and next_feature["block_type"] == "table"
        and not (context.shared_section_keys or context.same_message_layout or context.same_table_series)
    ):
        score -= 4
        reasons.append("叙述转表格切换")
    if (
        ("table" in current_group_tags or "table_reference" in current_group_tags)
        and "narrative" in next_structure_tags
        and not (context.shared_section_keys or context.same_message_layout or context.same_table_series)
    ):
        score -= 4
        reasons.append("表格转叙述切换")

    if prev_feature["semantic_hint"] == next_feature["semantic_hint"]:
        score += 1
        reasons.append("语义类型一致")

    if prev_feature["formula_count"] > 0 and next_feature["formula_count"] > 0:
        score += 1
        reasons.append("转换信息连续")

    if prev_feature.get("evidence_score", 0.0) >= 18.0 and next_feature.get("evidence_score", 0.0) >= 18.0:
        score += 1
        reasons.append("规则证据密集")

    prev_evidence = float(prev_feature.get("evidence_score", 0.0) or 0.0)
    next_noise = float(next_feature.get("noise_penalty", 0.0) or 0.0)
    next_evidence = float(next_feature.get("evidence_score", 0.0) or 0.0)
    if context.evidence_gap <= 8.0 and min(prev_evidence, next_evidence) >= 10.0:
        score += 1
        reasons.append("证据密度平滑")
    if next_noise >= 8.0 and next_evidence < 12.0:
        score -= 2
        reasons.append("候选块噪声偏高")

    if (
        prev_feature["block_type"] != next_feature["block_type"]
        and not (context.shared_section_keys or context.same_message_layout or context.same_table_series)
        and max(prev_evidence, next_evidence) >= 18.0
        and min(prev_evidence, next_evidence) <= 10.0
    ):
        score -= 3
        reasons.append("强弱证据混合")

    return score, reasons, projected_tokens


def llm_should_merge_blocks(
    current_group: List[Dict[str, Any]],
    next_feature: Dict[str, Any],
    max_token_size: int,
) -> Tuple[bool, str]:
    """
    在规则分数不确定时，使用LLM做一次边界判断。
    仅返回是否合并和简短原因。
    """
    try:
        llm = get_llm_client()
        prev = current_group[-1]
        context = build_merge_context(current_group, next_feature, max_token_size)
        payload = {
            "max_token_size": max_token_size,
            "token_pressure": round(context.token_pressure, 4),
            "page_gap": context.page_gap,
            "structure_transition": context.structure_transition,
            "current_group": {
                "block_ids": [item["block_id"] for item in current_group],
                "protocol_anchor": prev.get("protocol_anchor", ""),
                "protocol_family": prev.get("protocol_family", ""),
                "semantic_hint": prev.get("semantic_hint", ""),
                "section_keys": sorted(prev.get("section_keys", []))[:8],
                "structure_tags": sorted(prev.get("structure_tags", []))[:8],
                "token_estimate": sum(item["token_count"] for item in current_group),
                "evidence_score": prev.get("evidence_score", 0.0),
                "noise_penalty": prev.get("noise_penalty", 0.0),
                "content_preview": (prev.get("content") or "")[:420],
            },
            "candidate_block": {
                "block_id": next_feature.get("block_id"),
                "protocol_anchor": next_feature.get("protocol_anchor", ""),
                "protocol_family": next_feature.get("protocol_family", ""),
                "semantic_hint": next_feature.get("semantic_hint", ""),
                "section_keys": sorted(next_feature.get("section_keys", []))[:8],
                "structure_tags": sorted(next_feature.get("structure_tags", []))[:8],
                "token_estimate": next_feature.get("token_count", 0),
                "evidence_score": next_feature.get("evidence_score", 0.0),
                "noise_penalty": next_feature.get("noise_penalty", 0.0),
                "content_preview": (next_feature.get("content") or "")[:420],
            },
        }
        system_prompt = (
            "你是文档分块边界判断器。"
            "根据上下文特征判断candidate_block是否应与current_group合并。"
            "优先保持同一语义片段和同一版面连续性。"
            "只输出JSON: {\"merge\": true/false, \"reason\": \"<=20字\"}"
        )
        user_prompt = json.dumps(payload, ensure_ascii=False)
        result = llm.extract_json(user_prompt, system_prompt=system_prompt)
        if isinstance(result, dict) and "merge" in result:
            return bool(result.get("merge")), str(result.get("reason", "")).strip()
    except Exception as exc:
        logger.warning("LLM边界判断失败，回退规则: %s", exc)
    return False, ""


def determine_group_semantic_type(features: List[Dict[str, Any]]) -> str:
    """根据组内统计特征确定chunk语义类型。"""
    if not features:
        return "general_content"
    table_count = sum(1 for item in features if item["block_type"] == "table")
    mapping_count = sum(item["mapping_pair_count"] for item in features)
    formula_count = sum(item["formula_count"] for item in features)
    field_names = set().union(*(item["field_names"] for item in features))
    bit_or_range_count = sum(item["bit_coverage_count"] + item["range_coverage_count"] for item in features)
    structure_tags = set().union(*(item.get("structure_tags", set()) for item in features))

    if {"word_map", "field_definition", "message_layout"} & structure_tags:
        return "field_definition"
    if mapping_count > 0:
        return "table_data" if table_count >= max(1, len(features) // 2) else "conversion_rule"
    if formula_count >= 2:
        return "conversion_rule"
    if field_names and bit_or_range_count > 0:
        return "field_definition"
    if table_count > 0:
        return "table_data"
    if any(item["protocol_anchor"] for item in features):
        return "protocol_description"
    return "general_content"


def build_chunk_metadata(features: List[Dict[str, Any]], reason: str, method: str) -> Dict[str, Any]:
    """生成chunk级质量统计，供后续QA选块使用。"""
    block_count = len(features)
    token_estimate = sum(item["token_count"] for item in features)
    field_names = sorted(set().union(*(item["field_names"] for item in features)))
    structure_tags = sorted(set().union(*(item.get("structure_tags", set()) for item in features)))
    section_keys = sorted(set().union(*(item.get("section_keys", set()) for item in features)))
    formula_count = sum(item["formula_count"] for item in features)
    mapping_pair_count = sum(item["mapping_pair_count"] for item in features)
    bit_coverage_count = sum(item["bit_coverage_count"] for item in features)
    range_coverage_count = sum(item["range_coverage_count"] for item in features)
    evidence_score = round(sum(float(item.get("evidence_score", 0.0) or 0.0) for item in features), 4)
    noise_penalty = round(sum(float(item.get("noise_penalty", 0.0) or 0.0) for item in features), 4)
    noisy_block_count = sum(1 for item in features if float(item.get("noise_penalty", 0.0) or 0.0) >= 8.0)

    protocol_anchor = ""
    for item in features:
        if item["protocol_anchor"]:
            protocol_anchor = item["protocol_anchor"]
            break

    base_quality_score = (
        min(40, len(field_names) * 3)
        + min(20, formula_count * 2)
        + min(15, mapping_pair_count)
        + min(15, bit_coverage_count + range_coverage_count)
        + (10 if protocol_anchor else 0)
    )
    quality_score = max(
        0.0,
        round(
            base_quality_score
            + min(24.0, evidence_score * 0.35)
            - min(24.0, noise_penalty * 0.8)
            - noisy_block_count * 1.5,
            4,
        ),
    )

    return {
        "protocol_anchor": protocol_anchor,
        "field_names": field_names[:24],
        "field_count": len(field_names),
        "structure_tags": structure_tags[:12],
        "section_keys": section_keys[:12],
        "formula_count": formula_count,
        "mapping_pair_count": mapping_pair_count,
        "bit_coverage_count": bit_coverage_count,
        "range_coverage_count": range_coverage_count,
        "token_estimate": token_estimate,
        "quality_score": quality_score,
        "evidence_score": evidence_score,
        "noise_penalty": noise_penalty,
        "noisy_block_count": noisy_block_count,
        "block_count": block_count,
        "merge_method": method,
        "reason": reason,
    }


def normalize_target_protocol(value: Any) -> str:
    return str(value or "").strip().upper()


def block_matches_target_protocol(block: Block, target_protocol: str) -> bool:
    """判断块是否与目标协议相关。"""
    target = normalize_target_protocol(target_protocol)
    if not target:
        return False

    content = get_block_content(block).upper()
    metadata = block.metadata if isinstance(block.metadata, dict) else {}
    anchor = extract_protocol_anchor(content, metadata).upper()

    major_match = re.match(r"(J\d+)", target)
    major = major_match.group(1) if major_match else target

    if target in content or target in anchor:
        return True
    if major and (major in content or anchor.startswith(major)):
        return True

    field_names = extract_field_names(content, metadata)
    for field_name in field_names:
        normalized = str(field_name).upper()
        if target in normalized or (major and major in normalized):
            return True
    return False


def filter_blocks_by_target_protocol(
    blocks: List[Block],
    target_protocol: str,
    page_window: int = 0,
) -> List[Block]:
    """
    按目标协议筛选块，并可扩展邻近页上下文。
    例如 target_protocol=J12.0 时，优先保留J12相关块。
    """
    target = normalize_target_protocol(target_protocol)
    if not target:
        return blocks

    matched_pages: Set[int] = set()
    matched_block_ids: Set[int] = set()
    for block in blocks:
        if block_matches_target_protocol(block, target):
            matched_pages.add(int(getattr(block, "page_num", 0) or 0))
            matched_block_ids.add(block.block_id)

    if not matched_pages:
        return blocks

    selected_pages: Set[int] = set()
    for page in matched_pages:
        selected_pages.add(page)
        for offset in range(1, max(0, page_window) + 1):
            selected_pages.add(page - offset)
            selected_pages.add(page + offset)

    filtered = [block for block in blocks if int(getattr(block, "page_num", 0) or 0) in selected_pages]
    logger.info(
        "按目标协议筛选块: target=%s, 原始=%d, 筛选后=%d, 覆盖页=%d",
        target,
        len(blocks),
        len(filtered),
        len(selected_pages),
    )
    return filtered if filtered else blocks


def rule_semantic_chunking(
    blocks: List[Block],
    max_token_size: int,
    use_llm_fallback: bool = True,
) -> List[Dict[str, Any]]:
    """
    规则优先的语义chunk生成：
    - 先按协议锚点/字段重叠/页连续性聚合
    - 规则不确定时再用LLM做边界判定
    """
    if not blocks:
        return []

    ordered_blocks = sorted(blocks, key=lambda item: (item.page_num, item.block_id))
    features = [
        feature
        for feature in (collect_block_features(block) for block in ordered_blocks)
        if not _is_low_value_chunk_source(feature)
    ]
    if not features:
        return []
    groups: List[List[Dict[str, Any]]] = []
    current_group: List[Dict[str, Any]] = [features[0]]
    llm_calls = 0
    max_llm_calls = int(os.getenv("SEMANTIC_CHUNK_MAX_LLM_BOUNDARY_CALLS", "8"))

    for next_feature in features[1:]:
        score, reasons, projected_tokens = _score_merge_candidate(current_group, next_feature, max_token_size)
        should_merge = score >= 3
        reason = ";".join(reasons) if reasons else "规则聚合"

        uncertain = 1 <= score < 3 and projected_tokens <= int(max_token_size * 1.2)
        if uncertain and use_llm_fallback and llm_calls < max_llm_calls:
            llm_merge, llm_reason = llm_should_merge_blocks(current_group, next_feature, max_token_size)
            llm_calls += 1
            if llm_merge:
                should_merge = True
                reason = f"{reason};LLM边界判定:{llm_reason or 'merge'}"

        if should_merge:
            current_group.append(next_feature)
            current_group[-1]["merge_reason"] = reason
        else:
            groups.append(current_group)
            current_group = [next_feature]

    if current_group:
        groups.append(current_group)

    chunk_suggestions: List[Dict[str, Any]] = []
    for group in groups:
        semantic_type = determine_group_semantic_type(group)
        reason = group[-1].get("merge_reason") or "规则聚合"
        chunk_suggestions.append(
            {
                "block_ids": [item["block_id"] for item in group],
                "semantic_type": semantic_type,
                "reason": reason,
                "metadata": build_chunk_metadata(group, reason=reason, method="rule+llm_boundary"),
            }
        )
    return chunk_suggestions


def analyze_semantic_relations(
    blocks: List[Block],
    max_token_size: int = 1024,
    use_llm_fallback: bool = True,
) -> List[Dict[str, Any]]:
    """
    规则优先分析块之间的语义关联性，必要时用LLM做边界兜底。

    Args:
        blocks: 文档块列表
        max_token_size: 最大token大小限制
        use_llm_fallback: 是否启用LLM边界判定兜底

    Returns:
        语义分块建议列表
    """
    if not blocks:
        return []

    # 如果只有一个块，直接返回
    if len(blocks) == 1:
        return [{
            "block_ids": [blocks[0].block_id],
            "semantic_type": "single_block",
            "reason": "单块内容",
            "metadata": build_chunk_metadata([collect_block_features(blocks[0])], reason="单块内容", method="single"),
        }]

    try:
        rule_chunks = rule_semantic_chunking(
            blocks=blocks,
            max_token_size=max_token_size,
            use_llm_fallback=use_llm_fallback,
        )
        if rule_chunks:
            return rule_chunks
    except Exception as e:
        logger.error(f"规则分块失败，回退基础策略: {e}")

    # 降级方案：按块类型和顺序简单分组
    return fallback_chunking(blocks, max_token_size)


def fallback_chunking(
    blocks: List[Block],
    max_token_size: int = 1024
) -> List[Dict[str, Any]]:
    """
    降级分块方案：按顺序和token限制简单分组
    """
    chunks = []
    current_group = []
    current_tokens = 0
    current_type = None

    for block in blocks:
        content = get_block_content(block)
        tokens = estimate_tokens(content)

        # 检查是否需要开始新组
        should_start_new = False

        # 1. token超限
        if current_tokens + tokens > max_token_size and current_group:
            should_start_new = True

        # 2. 块类型变化（表格和代码通常独立成块）
        if block.block_type in ["table", "code"] and current_group:
            should_start_new = True

        if should_start_new:
            # 保存当前组
            if current_group:
                group_features = [collect_block_features(item) for item in current_group]
                chunks.append({
                    "block_ids": [b.block_id for b in current_group],
                    "semantic_type": determine_semantic_type(current_group),
                    "reason": "按token限制和类型分组",
                    "metadata": build_chunk_metadata(
                        group_features,
                        reason="按token限制和类型分组",
                        method="fallback",
                    ),
                })
            current_group = []
            current_tokens = 0

        current_group.append(block)
        current_tokens += tokens

    # 保存最后一组
    if current_group:
        group_features = [collect_block_features(item) for item in current_group]
        chunks.append({
            "block_ids": [b.block_id for b in current_group],
            "semantic_type": determine_semantic_type(current_group),
            "reason": "最后一块",
            "metadata": build_chunk_metadata(group_features, reason="最后一块", method="fallback"),
        })

    return chunks


def determine_semantic_type(blocks: List[Block]) -> str:
    """
    根据块内容确定语义类型
    """
    if not blocks:
        return "unknown"

    # 检查块类型
    block_types = [b.block_type for b in blocks]
    metadata_list = [b.metadata if isinstance(b.metadata, dict) else {} for b in blocks]

    if "table" in block_types:
        joined_content = " ".join(get_block_content(b) for b in blocks)
        if WORD_MAP_PATTERN.search(joined_content) or FIELD_DESCRIPTION_PATTERN.search(joined_content):
            return "field_definition"
        return "table_data"
    if "code" in block_types:
        return "code_example"

    # 检查内容特征
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

    # 字段定义特征
    field_keywords = ["字段", "field", "位宽", "bit", "范围", "range", "单位", "unit"]
    if any(kw in all_content_lower for kw in field_keywords):
        return "field_definition"

    # 转换规则特征
    conversion_keywords = ["公式", "formula", "计算", "calculate", "转换", "convert", "映射", "map"]
    if any(kw in all_content_lower for kw in conversion_keywords):
        return "conversion_rule"

    # 协议描述特征
    protocol_keywords = ["协议", "protocol", "概述", "overview", "用途", "purpose", "介绍", "introduction"]
    if any(kw in all_content_lower for kw in protocol_keywords):
        return "protocol_description"

    return "general_content"


def _compute_pairwise_scores(
    blocks: List[Block], max_token_size: int
) -> List[Tuple[int, int, List[str]]]:
    """计算 chunk 内相邻 block 之间的合并分数，用于找到最佳语义切分点。"""
    features = [collect_block_features(block) for block in blocks]
    scores = []
    for i in range(len(features) - 1):
        group = [features[i]]
        score, reasons, _ = _score_merge_candidate(group, features[i + 1], max_token_size)
        scores.append((i, score, reasons))
    return scores


def _split_block_list(
    blocks: List[Block], split_after_idx: int
) -> Tuple[List[Block], List[Block]]:
    """在指定位置之后切分 block 列表。"""
    return blocks[: split_after_idx + 1], blocks[split_after_idx + 1 :]


def semantic_split_chunk(
    blocks: List[Block],
    max_token_size: int,
    semantic_type: str,
    metadata: Optional[Dict[str, Any]] = None,
    _depth: int = 0,
) -> List[Dict[str, Any]]:
    """在语义最弱的边界切分超限 chunk，语义一致性优先于 token 上限。

    原则：
    1. 不超限 → 直接保留
    2. 超限但存在弱语义边界（合并分数 < 2）→ 在最弱处切分
    3. 超限但所有边界都强关联 → 保留完整 chunk，避免破坏语义一致性
    """
    total_tokens = sum(estimate_tokens(get_block_content(b)) for b in blocks)
    max_depth = max(1, len(blocks) // 2)

    if total_tokens <= max_token_size or len(blocks) <= 1 or _depth >= max_depth:
        group_features = [collect_block_features(b) for b in blocks]
        reason = "语义完整保留" if total_tokens > max_token_size else "token限制内"
        merged_metadata = dict(metadata or {})
        merged_metadata.update(
            build_chunk_metadata(group_features, reason=reason, method="semantic_split")
        )
        return [{
            "block_ids": [b.block_id for b in blocks],
            "semantic_type": semantic_type,
            "reason": reason,
            "metadata": merged_metadata,
        }]

    # 计算相邻 block 间的合并分数
    pairwise = _compute_pairwise_scores(blocks, max_token_size)

    # 找到最弱的语义边界（分数最低的切分点）
    # 仅当存在分数 < 2 的弱边界时才切分
    weak_candidates = [(i, score, reasons) for i, score, reasons in pairwise if score < 2]
    weak_candidates.sort(key=lambda x: x[1])  # 分数从低到高

    if not weak_candidates:
        group_features = [collect_block_features(b) for b in blocks]
        merged_metadata = dict(metadata or {})
        merged_metadata.update(
            build_chunk_metadata(
                group_features,
                reason=f"语义强关联保留(total_tokens={total_tokens})",
                method="semantic_split",
            )
        )
        return [{
            "block_ids": [b.block_id for b in blocks],
            "semantic_type": semantic_type,
            "reason": "语义强关联保留",
            "metadata": merged_metadata,
        }]

    # 在最弱边界处切分
    split_idx, split_score, split_reasons = weak_candidates[0]
    left_blocks, right_blocks = _split_block_list(blocks, split_idx)
    reasons_text = ";".join(split_reasons) if split_reasons else f"score={split_score}"

    return semantic_split_chunk(
        left_blocks, max_token_size, semantic_type, metadata, _depth + 1
    ) + semantic_split_chunk(
        right_blocks, max_token_size, semantic_type, metadata, _depth + 1
    )


def refine_chunks_by_token_limit(
    chunk_suggestions: List[Dict[str, Any]],
    blocks_dict: Dict[int, Block],
    max_token_size: int = 1024,
) -> List[Dict[str, Any]]:
    """对超限 chunk 在语义边界处拆分，语义一致性优先于 token 上限。"""
    refined_chunks = []

    for suggestion in chunk_suggestions:
        block_ids = suggestion.get("block_ids", [])
        semantic_type = suggestion.get("semantic_type", "general_content")
        metadata = suggestion.get("metadata") if isinstance(suggestion.get("metadata"), dict) else {}

        blocks = [blocks_dict[bid] for bid in block_ids if bid in blocks_dict]
        if not blocks:
            continue

        total_tokens = sum(estimate_tokens(get_block_content(b)) for b in blocks)

        if total_tokens <= max_token_size:
            refined_chunks.append(suggestion)
        else:
            sub_chunks = semantic_split_chunk(
                blocks, max_token_size, semantic_type, metadata
            )
            refined_chunks.extend(sub_chunks)

    return refined_chunks


def generate_content_snapshot(blocks: List[Block], max_length: int = 500) -> str:
    """
    生成内容快照
    """
    contents = []
    total_length = 0

    for block in blocks:
        content = get_block_content(block)
        if total_length + len(content) > max_length:
            # 截断
            remaining = max_length - total_length
            if remaining > 0:
                contents.append(content[:remaining] + "...")
            break
        contents.append(content)
        total_length += len(content)

    return "\n\n".join(contents)



def build_chunks_from_suggestions(
    chunks: List[Dict[str, Any]],
    blocks_dict: Dict[int, Block],
    project_id: str,
    dataset_id: str
) -> List[Chunk]:
    """
    根据分块建议构造语义块对象；接口只返回结果，不写数据库。
    """
    saved_chunks = []
    timestamp = int(time.time())

    for idx, chunk_data in enumerate(chunks):
        block_ids = chunk_data.get("block_ids", [])
        semantic_type = chunk_data.get("semantic_type", "general_content")
        chunk_metadata = chunk_data.get("metadata") if isinstance(chunk_data.get("metadata"), dict) else {}

        # 获取对应的块
        blocks = [blocks_dict[bid] for bid in block_ids if bid in blocks_dict]
        if not blocks:
            continue

        # 生成chunk_id
        chunk_id = f"chk_{timestamp}_{idx}_{uuid.uuid4().hex[:8]}"

        # 生成内容快照
        content_snapshot = generate_content_snapshot(blocks)

        if not chunk_metadata:
            features = [collect_block_features(block) for block in blocks]
            chunk_metadata = build_chunk_metadata(features, reason=chunk_data.get("reason", ""), method="save_fallback")

        # 创建Chunk对象
        chunk = Chunk(
            chunk_id=chunk_id,
            project_id=project_id,
            dataset_id=dataset_id,
            source_block_ids=block_ids,
            semantic_type=semantic_type,
            content_snapshot=content_snapshot,
            metadata={
                **chunk_metadata,
                "reason": chunk_data.get("reason", ""),
                "block_count": len(blocks),
                "created_method": "semantic_chunk_api",
            }
        )

        saved_chunks.append(chunk)

    return saved_chunks


def _request_payload() -> Dict[str, Any]:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    if not request.form:
        return {}

    list_fields = {"source_block_ids", "document_paths", "file_names"}
    parsed: Dict[str, Any] = {}
    for key in request.form.keys():
        values = request.form.getlist(key)
        if not values:
            continue
        parsed_values = [_decode_form_value(item) for item in values]
        value: Any = parsed_values if len(parsed_values) > 1 else parsed_values[0]
        if key in list_fields and not isinstance(value, list):
            value = [] if value in (None, "") else [value]
        parsed[key] = value
    return parsed


def _file_size_from_path(file_path: str) -> int:
    try:
        return os.path.getsize(file_path)
    except OSError:
        return 0


def _decode_form_value(value: Any) -> Any:
    if isinstance(value, (dict, list, bool, int, float)) or value is None:
        return value
    text = str(value).strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered == "null":
        return None
    if text.startswith("[") or text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


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


def semantic_chunk_async_requested(payload: Optional[Dict[str, Any]] = None) -> bool:
    resolved_payload = payload if isinstance(payload, dict) else _request_payload()
    return _as_bool((resolved_payload or {}).get("async"), default=True)


def _should_async(explicit_async: bool, file_sizes: List[int]) -> bool:
    if explicit_async:
        return True
    threshold = max(1, int(AUTO_ASYNC_TRIGGER_MB)) * 1024 * 1024
    return any(int(size or 0) >= threshold for size in file_sizes)


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    if numerator <= 0:
        return 0
    return (numerator + denominator - 1) // denominator


def _sample_page_indexes(total_pages: int, sample_limit: int) -> List[int]:
    resolved_total_pages = max(0, int(total_pages or 0))
    resolved_sample_limit = max(1, int(sample_limit or 1))
    if resolved_total_pages <= 0:
        return []
    if resolved_total_pages <= resolved_sample_limit:
        return list(range(resolved_total_pages))
    if resolved_sample_limit == 1:
        return [0]

    indexes: List[int] = []
    seen = set()
    for position in range(resolved_sample_limit):
        page_index = round(position * (resolved_total_pages - 1) / (resolved_sample_limit - 1))
        if page_index in seen:
            continue
        seen.add(page_index)
        indexes.append(page_index)
    return indexes


def _inspect_document_async_profile(file_path: str) -> Dict[str, Any]:
    resolved_path = str(file_path or "").strip()
    suffix = Path(resolved_path).suffix.lower()
    file_size = _file_size_from_path(resolved_path)
    profile: Dict[str, Any] = {
        "path": resolved_path,
        "file_name": Path(resolved_path).name,
        "suffix": suffix,
        "file_size": file_size,
        "page_count": None,
        "sampled_page_count": 0,
        "estimated_total_chars": None,
        "estimated_shards": None,
    }
    if suffix != ".pdf" or not resolved_path:
        return profile

    try:
        import pdfplumber

        with pdfplumber.open(resolved_path) as pdf:
            total_pages = len(pdf.pages)
            sample_indexes = _sample_page_indexes(total_pages, AUTO_ASYNC_SAMPLE_PAGES)
            sampled_chars = 0
            for page_index in sample_indexes:
                text = str(pdf.pages[page_index].extract_text() or "").strip()
                sampled_chars += len(text)

        sampled_page_count = len(sample_indexes)
        avg_chars_per_page = (sampled_chars / sampled_page_count) if sampled_page_count else 0.0
        estimated_total_chars = int(avg_chars_per_page * total_pages)
        estimated_shards = max(
            1 if total_pages > 0 else 0,
            _ceil_div(estimated_total_chars, DEFAULT_SHARD_MAX_CHARS),
            _ceil_div(total_pages, DEFAULT_SHARD_MAX_PAGES),
        )
        profile.update(
            {
                "page_count": total_pages,
                "sampled_page_count": sampled_page_count,
                "sampled_chars": sampled_chars,
                "avg_chars_per_page": round(avg_chars_per_page, 2),
                "estimated_total_chars": estimated_total_chars,
                "estimated_shards": estimated_shards,
            }
        )
        return profile
    except Exception as exc:
        profile["inspection_error"] = str(exc)
        return profile


def _build_update_doc_index_async_decision(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resolved_payload = payload if isinstance(payload, dict) else _request_payload()
    explicit_async = _as_bool((resolved_payload or {}).get("async"), default=False)
    document_paths = _normalize_document_paths(
        (resolved_payload or {}).get("document_paths") or (resolved_payload or {}).get("document_path")
    )
    file_sizes = [_file_size_from_path(path) for path in document_paths]
    threshold_bytes = max(1, int(AUTO_ASYNC_TRIGGER_MB)) * 1024 * 1024
    document_profiles = [_inspect_document_async_profile(path) for path in document_paths]
    reasons: List[str] = []

    if explicit_async:
        reasons.append("explicit_async")

    for path, file_size in zip(document_paths, file_sizes):
        if int(file_size or 0) >= threshold_bytes:
            reasons.append(f"file_size:{Path(path).name}")

    for profile in document_profiles:
        file_name = str(profile.get("file_name") or "unknown")
        page_count = int(profile.get("page_count") or 0)
        estimated_shards = int(profile.get("estimated_shards") or 0)
        estimated_total_chars = int(profile.get("estimated_total_chars") or 0)
        if page_count >= AUTO_ASYNC_TRIGGER_PDF_PAGES:
            reasons.append(f"pdf_page_count:{file_name}:{page_count}")
        if estimated_shards >= AUTO_ASYNC_TRIGGER_ESTIMATED_SHARDS:
            reasons.append(f"pdf_estimated_shards:{file_name}:{estimated_shards}")
        if estimated_total_chars >= AUTO_ASYNC_TRIGGER_ESTIMATED_CHARS:
            reasons.append(f"pdf_estimated_chars:{file_name}:{estimated_total_chars}")

    deduped_reasons: List[str] = []
    seen = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        deduped_reasons.append(reason)

    return {
        "should_async": bool(deduped_reasons),
        "explicit_async": explicit_async,
        "threshold_bytes": threshold_bytes,
        "document_profiles": document_profiles,
        "reasons": deduped_reasons,
    }


def _collect_uploaded_document_files() -> List[Any]:
    uploaded_files: List[Any] = []
    single_file = request.files.get("file")
    if single_file is not None and getattr(single_file, "filename", ""):
        uploaded_files.append(single_file)
    for item in request.files.getlist("files"):
        if item is None or not getattr(item, "filename", ""):
            continue
        uploaded_files.append(item)
    return uploaded_files


def _build_uploaded_document_dir(project_id: str = "") -> Path:
    root = Path(file_store.base_dir) / "pageindex_uploads" / (str(project_id or "").strip() or "shared")
    batch_dir = root / f"upload_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    return batch_dir


def _persist_uploaded_document(uploaded_file: Any, target_dir: Path) -> Tuple[str, str]:
    original_name = Path(str(getattr(uploaded_file, "filename", "") or "")).name.strip()
    normalized_name = original_name or secure_filename(f"pageindex_doc_{uuid.uuid4().hex[:8]}")
    if not normalized_name:
        normalized_name = f"pageindex_doc_{uuid.uuid4().hex[:8]}.bin"

    target_path = target_dir / normalized_name
    if target_path.exists():
        target_path = target_dir / f"{target_path.stem}_{uuid.uuid4().hex[:6]}{target_path.suffix}"

    uploaded_file.save(target_path)
    if target_path.stat().st_size <= 0:
        try:
            target_path.unlink()
        except OSError:
            logger.warning("删除空上传文件失败: %s", target_path, exc_info=True)
        display_name = original_name or normalized_name
        raise ValueError(f"上传文件为空: {display_name}；请检查multipart file/files字段或上传文件流是否已被提前读取")
    return str(target_path.resolve()), original_name or normalized_name


def prepare_update_doc_index_payload() -> Dict[str, Any]:
    payload = dict(_request_payload())
    uploaded_files = _collect_uploaded_document_files()
    if not uploaded_files:
        return payload

    project_id = str(payload.get("project_id") or "").strip()
    upload_dir = _build_uploaded_document_dir(project_id=project_id)
    saved_paths: List[str] = []
    uploaded_file_names: List[str] = []

    for uploaded_file in uploaded_files:
        saved_path, original_name = _persist_uploaded_document(uploaded_file, upload_dir)
        saved_paths.append(saved_path)
        uploaded_file_names.append(original_name)

    existing_paths = _normalize_document_paths(payload.get("document_paths") or payload.get("document_path"))
    existing_file_names = _normalize_string_list(payload.get("file_names"), "file_names")

    merged_paths: List[str] = []
    seen_paths = set()
    for item in saved_paths + existing_paths:
        if not item or item in seen_paths:
            continue
        seen_paths.add(item)
        merged_paths.append(item)

    merged_file_names: List[str] = []
    seen_names = set()
    for item in uploaded_file_names + existing_file_names:
        text = str(item or "").strip()
        if not text or text in seen_names:
            continue
        seen_names.add(text)
        merged_file_names.append(text)

    payload["document_paths"] = merged_paths
    payload["file_names"] = merged_file_names
    return payload


def update_doc_index_async_requested(payload: Optional[Dict[str, Any]] = None) -> bool:
    return bool(_build_update_doc_index_async_decision(payload).get("should_async"))


def _summarize_update_doc_index_request(
    payload: Optional[Dict[str, Any]] = None,
    async_decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    resolved_payload = payload if isinstance(payload, dict) else {}
    document_paths = _normalize_document_paths(
        resolved_payload.get("document_paths") or resolved_payload.get("document_path")
    )
    file_names = _normalize_string_list(resolved_payload.get("file_names"), "file_names")
    if document_paths:
        derived_file_names = _derive_file_names_from_document_paths(document_paths)
        file_names = derived_file_names + [name for name in file_names if name not in set(derived_file_names)]
    file_sizes = [_file_size_from_path(path) for path in document_paths]
    decision = async_decision if isinstance(async_decision, dict) else _build_update_doc_index_async_decision(resolved_payload)
    document_profiles = decision.get("document_profiles") or []
    return {
        "project_id": str(resolved_payload.get("project_id") or "").strip() or None,
        "document_count": len(document_paths),
        "file_names": file_names,
        "total_bytes": sum(int(size or 0) for size in file_sizes),
        "max_bytes": max((int(size or 0) for size in file_sizes), default=0),
        "threshold_bytes": int(decision.get("threshold_bytes") or 0),
        "explicit_async": bool(decision.get("explicit_async")),
        "max_page_count": max((int(profile.get("page_count") or 0) for profile in document_profiles), default=0),
        "max_estimated_shards": max((int(profile.get("estimated_shards") or 0) for profile in document_profiles), default=0),
        "max_estimated_chars": max((int(profile.get("estimated_total_chars") or 0) for profile in document_profiles), default=0),
        "async_reasons": list(decision.get("reasons") or []),
    }


def _compute_document_fingerprint(file_path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_document_fingerprint_map(document_paths: List[str]) -> Dict[str, str]:
    fingerprints: Dict[str, str] = {}
    for raw_path in _normalize_document_paths(document_paths):
        file_name = Path(raw_path).name.strip()
        if not file_name or file_name in fingerprints:
            continue
        fingerprints[file_name] = _compute_document_fingerprint(raw_path)
    return fingerprints


def _registry_matches_documents(registry: Dict[str, Any], file_names: List[str], document_fingerprints: Dict[str, str]) -> bool:
    registry_files = sorted(
        {
            str(item.get("file_name") or "").strip()
            for item in (registry.get("documents") or [])
            if isinstance(item, dict) and str(item.get("file_name") or "").strip()
        }
    )
    expected_files = sorted({str(item).strip() for item in file_names if str(item).strip()})
    if expected_files and registry_files and registry_files != expected_files:
        return False

    stored_fingerprints = registry.get("source_document_fingerprints") or {}
    if document_fingerprints and isinstance(stored_fingerprints, dict):
        normalized_stored = {
            str(name).strip(): str(fingerprint).strip()
            for name, fingerprint in stored_fingerprints.items()
            if str(name).strip() and str(fingerprint).strip()
        }
        return normalized_stored == {
            str(name).strip(): str(fingerprint).strip()
            for name, fingerprint in document_fingerprints.items()
            if str(name).strip() and str(fingerprint).strip()
        }

    return bool(expected_files) and registry_files == expected_files


def _find_matching_registry_for_documents(
    *,
    project_id: str,
    file_names: List[str],
    document_fingerprints: Dict[str, str],
) -> Dict[str, Any]:
    resolved_project_id = str(project_id or "").strip()
    if not resolved_project_id:
        return {}
    for registry in file_store.list_pageindex_registries(resolved_project_id):
        if _registry_matches_documents(registry, file_names, document_fingerprints):
            return registry
    return {}


def _serialize_block(block: Block) -> Dict[str, Any]:
    payload = block.to_dict()
    payload["type"] = payload.get("block_type")
    return payload


def _normalize_block_from_payload(item: Dict[str, Any], default_project_id: str = "") -> Block:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    page_num = int(item.get("page_num") or 0)
    raw_page_range = item.get("page_range") if isinstance(item.get("page_range"), list) else metadata.get("merged_pages")
    page_range: List[int] = []
    if isinstance(raw_page_range, list):
        for value in raw_page_range:
            try:
                normalized_page = int(value)
            except (TypeError, ValueError):
                continue
            if normalized_page > 0 and normalized_page not in page_range:
                page_range.append(normalized_page)
    if not page_range and page_num > 0:
        end_page = int(metadata.get("end_page") or 0)
        if end_page > 0:
            start_page, stop_page = sorted((page_num, end_page))
            page_range = list(range(start_page, stop_page + 1))
        else:
            page_range = [page_num]
    return Block(
        block_id=int(item.get("block_id") or 0),
        project_id=str(item.get("project_id") or default_project_id or "file_path_project").strip(),
        file_name=str(item.get("file_name") or metadata.get("file_name") or "").strip(),
        page_num=page_num,
        content=str(item.get("content") or ""),
        block_type=str(item.get("block_type") or item.get("type") or "text"),
        cleaned_content=item.get("cleaned_content"),
        page_range=page_range or None,
        metadata=metadata,
    )


def _load_blocks_from_file(blocks_file_path: str, project_id_hint: str = "") -> Tuple[str, List[Block], Dict[str, Any]]:
    resolved_path = os.path.abspath(os.path.expanduser(str(blocks_file_path or "").strip()))
    if not resolved_path or not os.path.exists(resolved_path):
        raise FileNotFoundError(f"blocks_file_path不存在: {blocks_file_path}")
    with open(resolved_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("blocks文件内容必须为JSON对象")
    block_items = payload.get("blocks")
    if not isinstance(block_items, list) or not block_items:
        raise ValueError("blocks文件缺少blocks数组")
    project_id = str(project_id_hint or payload.get("project_id") or "").strip()
    blocks = [_normalize_block_from_payload(item, default_project_id=project_id) for item in block_items if isinstance(item, dict)]
    if not blocks:
        raise ValueError("blocks文件中没有可用块")
    project_id = project_id or blocks[0].project_id or f"proj_{int(time.time())}"
    for index, block in enumerate(blocks, start=1):
        if not block.project_id:
            block.project_id = project_id
        if not block.block_id:
            block.block_id = index
    payload["resolved_path"] = resolved_path
    return project_id, blocks, payload


def _build_chunk_models(
    chunks: List[Dict[str, Any]],
    blocks_dict: Dict[int, Block],
    project_id: str,
    dataset_id: str,
) -> List[Chunk]:
    built_chunks: List[Chunk] = []
    timestamp = int(time.time())
    for idx, chunk_data in enumerate(chunks):
        block_ids = [int(bid) for bid in chunk_data.get("block_ids", []) if int(bid) in blocks_dict]
        if not block_ids:
            continue
        blocks = [blocks_dict[bid] for bid in block_ids]
        semantic_type = chunk_data.get("semantic_type", "general_content")
        chunk_metadata = chunk_data.get("metadata") if isinstance(chunk_data.get("metadata"), dict) else {}
        if not chunk_metadata:
            features = [collect_block_features(block) for block in blocks]
            chunk_metadata = build_chunk_metadata(features, reason=chunk_data.get("reason", ""), method="save_fallback")
        built_chunks.append(
            Chunk(
                chunk_id=f"chk_{timestamp}_{idx}_{uuid.uuid4().hex[:8]}",
                project_id=project_id,
                dataset_id=dataset_id,
                source_block_ids=block_ids,
                semantic_type=semantic_type,
                content_snapshot=generate_content_snapshot(blocks),
                metadata={
                    **chunk_metadata,
                    "reason": chunk_data.get("reason", ""),
                    "block_count": len(blocks),
                    "created_method": "semantic_chunk_api",
                },
            )
        )
    return built_chunks


def _persist_chunk_models(chunk_models: List[Chunk], blocks_dict: Dict[int, Block]) -> None:
    """Compatibility no-op: persistence is handled by the external caller."""
    return None


def _chunk_payload(chunk: Chunk, blocks_dict: Dict[int, Block]) -> Dict[str, Any]:
    source_blocks = [_serialize_block(blocks_dict[bid]) for bid in chunk.source_block_ids if bid in blocks_dict]
    merged_content = "\n\n".join(
        [
            (blocks_dict[bid].cleaned_content or blocks_dict[bid].content or "").strip()
            for bid in chunk.source_block_ids
            if bid in blocks_dict and (blocks_dict[bid].cleaned_content or blocks_dict[bid].content)
        ]
    )
    payload = chunk.to_dict()
    payload["source_blocks"] = source_blocks
    payload["merged_content"] = merged_content
    payload["source_block_count"] = len(source_blocks)
    return payload


def _build_doc_index_response(project_id: str, doc_index_result: Dict[str, Any]) -> Dict[str, Any]:
    compatibility_dir = Path(".index") / "pageindex" / project_id / str(doc_index_result["doc_set_id"])
    compatibility_dir.mkdir(parents=True, exist_ok=True)
    (compatibility_dir / "registry.json").write_text(
        json.dumps(doc_index_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    storage_path = str(compatibility_dir.resolve()) + "/"
    return {
        "doc_set_id": doc_index_result["doc_set_id"],
        "index_ref": doc_index_result["index_ref"],
        "status": doc_index_result["status"],
        "document_count": doc_index_result["document_count"],
        "storage_path": storage_path,
    }


def _normalize_string_list(raw_value: Any, field_name: str) -> List[str]:
    if raw_value in (None, ""):
        return []
    if isinstance(raw_value, str):
        values = [raw_value]
    elif isinstance(raw_value, list):
        values = raw_value
    else:
        raise ValueError(f"{field_name}必须是字符串或数组")
    result: List[str] = []
    seen = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalize_document_paths(raw_value: Any) -> List[str]:
    paths = _normalize_string_list(raw_value, "document_paths")
    return [str(Path(path).expanduser()) for path in paths]


def _derive_file_names_from_document_paths(document_paths: List[str]) -> List[str]:
    file_names: List[str] = []
    seen = set()
    for raw_path in document_paths:
        file_name = Path(raw_path).name.strip()
        if not file_name:
            raise ValueError(f"document_paths中存在无法识别文件名的路径: {raw_path}")
        if file_name in seen:
            continue
        seen.add(file_name)
        file_names.append(file_name)
    return file_names


def _select_project_scoped_blocks(blocks: List[Block], fallback_seed: Optional[str] = None) -> Tuple[str, List[Block]]:
    grouped_blocks: Dict[str, List[Block]] = {}
    for block in blocks:
        project_key = str(getattr(block, "project_id", "") or "").strip()
        if project_key:
            grouped_blocks.setdefault(project_key, []).append(block)

    if len(grouped_blocks) == 1:
        project_id = next(iter(grouped_blocks))
        return project_id, grouped_blocks[project_id]

    if len(grouped_blocks) > 1:
        preferred_project_id, preferred_blocks = max(
            grouped_blocks.items(),
            key=lambda item: (
                max(int(getattr(block, "block_id", 0) or 0) for block in item[1]),
                len(item[1]),
                item[0],
            ),
        )
        return preferred_project_id, preferred_blocks

    seed = str(fallback_seed or "").strip() or "rag_pageindex"
    return f"rag_{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:12]}", blocks


def _normalize_return_mode(raw_value: Any) -> str:
    value = str(raw_value or "content").strip().lower()
    if value not in {"content", "path", "both"}:
        raise ValueError("return_mode仅支持 content、path、both")
    return value


def _load_blocks_from_payload(payload: Any, project_id_hint: str = "") -> Tuple[str, List[Block], Dict[str, Any]]:
    source_payload: Dict[str, Any]
    if isinstance(payload, list):
        block_items = payload
        source_payload = {"blocks": payload}
        project_id = project_id_hint
    elif isinstance(payload, dict):
        source_payload = payload
        if isinstance(payload.get("data"), dict):
            return _load_blocks_from_payload(payload.get("data"), project_id_hint=project_id_hint)
        block_items = payload.get("blocks") or payload.get("cleaned_blocks") or payload.get("items")
        project_id = str(payload.get("project_id") or project_id_hint or "").strip()
    else:
        raise ValueError("载荷内容不是有效的blocks JSON")

    if not isinstance(block_items, list) or not block_items:
        raise ValueError("载荷内容缺少blocks数组")
    blocks = [_normalize_block_from_payload(item, default_project_id=project_id) for item in block_items if isinstance(item, dict)]
    if not blocks:
        raise ValueError("载荷内容中没有可用块")
    project_id = project_id or blocks[0].project_id or f"proj_{int(time.time())}"
    for index, block in enumerate(blocks, start=1):
        if not block.project_id:
            block.project_id = project_id
        if not block.block_id:
            block.block_id = index
    return project_id, blocks, source_payload


def _load_blocks_from_pipeline_payload(content_id: str, project_id_hint: str = "") -> Tuple[str, List[Block], Dict[str, Any], Dict[str, Any]]:
    record = mysql_client.get_pipeline_payload(content_id)
    if not record:
        raise FileNotFoundError(f"未找到content_id对应的数据库内容: {content_id}")

    payload = record.get("payload")
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict):
            for key in ("cleaned_blocks_file_path", "blocks_file_path"):
                file_path = str(data.get(key) or "").strip()
                if file_path and os.path.exists(file_path):
                    project_id, blocks, source_payload = _load_blocks_from_file(file_path, project_id_hint=project_id_hint or str(record.get("project_id") or ""))
                    return project_id, blocks, source_payload, record

    try:
        project_id, blocks, source_payload = _load_blocks_from_payload(payload, project_id_hint=project_id_hint or str(record.get("project_id") or ""))
        return project_id, blocks, source_payload, record
    except ValueError:
        file_path = str(record.get("file_path") or "").strip()
        if file_path and os.path.exists(file_path):
            project_id, blocks, source_payload = _load_blocks_from_file(file_path, project_id_hint=project_id_hint or str(record.get("project_id") or ""))
            return project_id, blocks, source_payload, record
        raise


def _load_blocks_from_dataset_payload(dataset_id: str, project_id_hint: str = "") -> Tuple[str, List[Block], Dict[str, Any], Dict[str, Any]]:
    if getattr(mysql_client, "get_schema_mode", None) and mysql_client.get_schema_mode() == "protobridge_dev":
        record = mysql_client.get_dataset_document_blocks_payload(dataset_id)
    else:
        record = mysql_client.get_latest_pipeline_payload_by_dataset(
            dataset_id,
            payload_types=["cleaned_blocks", "blocks", "upload_split_blocks", "upload_split"],
        )
    if not record:
        raise FileNotFoundError(f"未找到dataset_id对应的块内容: {dataset_id}")
    project_id, blocks, source_payload = _load_blocks_from_payload(
        record.get("payload"),
        project_id_hint=project_id_hint or str(record.get("project_id") or ""),
    )
    return project_id, blocks, source_payload, record


def _normalize_document_id_list(raw_dataset_id: Any) -> List[int]:
    """将 dataset_id/document_id 引用解析为 document_id 列表，支持 ds_864。"""
    if raw_dataset_id is None:
        return []
    raw_values = raw_dataset_id if isinstance(raw_dataset_id, list) else [raw_dataset_id]
    normalized_ids: List[int] = []
    seen_ids = set()
    for item in raw_values:
        raw = str(item or "").strip()
        if not raw:
            continue
        resolved_ids: List[int] = []
        if getattr(mysql_client, "get_schema_mode", None) and mysql_client.get_schema_mode() == "protobridge_dev":
            resolved_ids = mysql_client.resolve_dataset_document_ids(raw)
        if not resolved_ids and raw.startswith("ds_") and raw[3:].isdigit():
            resolved_ids = [int(raw[3:])]
        try:
            normalized = int(raw)
        except (TypeError, ValueError) as exc:
            if not resolved_ids:
                raise ValueError(f"dataset_id无法解析为document_id: {raw}") from exc
        else:
            if not resolved_ids:
                resolved_ids = [normalized]
        for normalized in resolved_ids:
            if normalized in seen_ids:
                continue
            seen_ids.add(normalized)
            normalized_ids.append(normalized)
    return normalized_ids


def _load_blocks_from_document_ids(document_ids: List[int], project_id_hint: str = "") -> Tuple[str, str, List[Block], Dict[str, Any], Dict[str, Any]]:
    if not document_ids:
        raise FileNotFoundError("未找到dataset_id对应的块内容")

    if getattr(mysql_client, "get_schema_mode", None) and mysql_client.get_schema_mode() == "protobridge_dev":
        record = mysql_client.get_document_blocks_payloads(document_ids)
    else:
        record = {}
    if not record:
        raise FileNotFoundError(f"未找到dataset_id对应的块内容: {document_ids[0]}")
    project_id, blocks, source_payload = _load_blocks_from_payload(
        record.get("payload"),
        project_id_hint=project_id_hint or str(record.get("project_id") or ""),
    )
    return str(document_ids[0]), project_id, blocks, source_payload, record


def _split_blocks_by_document_id(blocks: List[Block]) -> List[Tuple[int, List[Block]]]:
    grouped: Dict[int, List[Block]] = {}
    order: List[int] = []
    for block in blocks:
        metadata = block.metadata if isinstance(block.metadata, dict) else {}
        try:
            document_id = int(metadata.get("document_id"))
        except (TypeError, ValueError):
            continue
        if document_id not in grouped:
            grouped[document_id] = []
            order.append(document_id)
        grouped[document_id].append(block)
    return [(document_id, grouped[document_id]) for document_id in order]


def _build_chunks_for_document(
    *,
    document_id: int,
    blocks: List[Block],
    dataset_id: str,
    max_token_size: int,
    use_llm_boundary_fallback: bool,
    target_protocol: str,
    target_page_window: int,
) -> List[Chunk]:
    document_blocks = list(blocks)
    if target_protocol:
        document_blocks = filter_blocks_by_target_protocol(
            document_blocks,
            target_protocol=target_protocol,
            page_window=target_page_window,
        )
    if not document_blocks:
        return []

    document_project_id = str(document_blocks[0].project_id or f"doc_{document_id}")
    document_blocks_dict = {int(block.block_id): block for block in document_blocks}
    analyze_params = inspect.signature(analyze_semantic_relations).parameters
    if "use_llm_fallback" in analyze_params:
        chunk_suggestions = analyze_semantic_relations(
            document_blocks,
            max_token_size=max_token_size,
            use_llm_fallback=use_llm_boundary_fallback,
        )
    else:
        chunk_suggestions = analyze_semantic_relations(document_blocks, max_token_size)
    refined_chunks = refine_chunks_by_token_limit(chunk_suggestions, document_blocks_dict, max_token_size)
    chunk_models = _build_chunk_models(refined_chunks, document_blocks_dict, document_project_id, dataset_id or str(document_id))
    for chunk in chunk_models:
        if isinstance(chunk.metadata, dict):
            chunk.metadata.setdefault("document_id", document_id)
    _persist_chunk_models(chunk_models, document_blocks_dict)
    return chunk_models


def _execute_semantic_chunk(
    data: Dict[str, Any],
    progress_callback: Optional[Callable[[str, str, float, Optional[Dict[str, Any]]], None]] = None,
) -> Tuple[Dict[str, Any], int]:
    """执行语义分块主流程。"""
    def emit(stage: str, message: str, progress: float, extra: Optional[Dict[str, Any]] = None) -> None:
        if progress_callback is not None:
            progress_callback(stage, message, progress, extra)

    try:
        if not data:
            return {"code": 400, "message": "请求体不能为空", "data": None}, 400

        config = data.get("config", {})
        if not isinstance(config, dict):
            config = {}

        blocks_file_path = str(data.get("blocks_file_path") or data.get("cleaned_blocks_file_path") or "").strip()
        project_id_hint = str(data.get("project_id") or "").strip()
        raw_dataset_id = data.get("dataset_id")
        document_ids = _normalize_document_id_list(raw_dataset_id) if raw_dataset_id not in (None, "") else []
        raw_dataset_ref = str(raw_dataset_id or "").strip()
        dataset_id = raw_dataset_ref or (str(document_ids[0]) if document_ids else "")
        content_id = str(data.get("content_id") or data.get("blocks_content_id") or data.get("document_id") or "").strip()
        direct_blocks_payload = data.get("blocks") if data.get("blocks") not in (None, "") else (data.get("cleaned_blocks") or data.get("blocks_content"))
        source_block_ids = data.get("source_block_ids")
        max_token_size = config.get("max_token_size", 1024)
        use_llm_boundary_fallback = bool(config.get("use_llm_boundary_fallback", True))
        target_protocol = normalize_target_protocol(config.get("target_protocol"))
        build_doc_index_enabled = bool(config.get("build_doc_index", False))
        return_mode = _normalize_return_mode(data.get("return_mode"))
        try:
            target_page_window = max(0, int(config.get("target_page_window", 0) or 0))
        except (TypeError, ValueError):
            target_page_window = 0

        emit("loading_source", "正在加载语义分块输入数据", 8.0)
        source_payload: Dict[str, Any] = {}
        source_record: Dict[str, Any] = {}
        if blocks_file_path:
            project_id, blocks, source_payload = _load_blocks_from_file(blocks_file_path, project_id_hint=project_id_hint)
        elif direct_blocks_payload not in (None, ""):
            project_id, blocks, source_payload = _load_blocks_from_payload(direct_blocks_payload, project_id_hint=project_id_hint)
        elif content_id:
            project_id, blocks, source_payload, source_record = _load_blocks_from_pipeline_payload(content_id, project_id_hint=project_id_hint)
        elif document_ids or dataset_id:
            dataset_id, project_id, blocks, source_payload, source_record = _load_blocks_from_document_ids(
                document_ids or [int(dataset_id)],
                project_id_hint=project_id_hint,
            )
            if raw_dataset_ref:
                dataset_id = raw_dataset_ref
        else:
            project_id = project_id_hint
            if not project_id:
                return {"code": 400, "message": "缺少blocks_file_path、blocks_content、content_id、document_id或dataset_id参数", "data": None}, 400
            if source_block_ids:
                blocks = mysql_client.get_blocks_by_ids(source_block_ids)
            else:
                blocks = mysql_client.get_blocks_by_project(project_id)
            source_payload = {}

        if source_record and not dataset_id:
            dataset_id = str(source_record.get("dataset_id") or "").strip()
        dataset_id = dataset_id or f"ds_{int(time.time())}_{uuid.uuid4().hex[:8]}"

        source_block_filter_applied = False
        if source_block_ids and dataset_id and getattr(mysql_client, "get_schema_mode", None) and mysql_client.get_schema_mode() == "protobridge_dev":
            logical_blocks = mysql_client.get_document_blocks_by_logical_ids(dataset_id, source_block_ids)
            if logical_blocks:
                blocks = logical_blocks
                source_block_filter_applied = True

        if source_block_ids and not source_block_filter_applied:
            block_id_set = {int(bid) for bid in source_block_ids}
            blocks = [block for block in blocks if int(getattr(block, "block_id", 0) or 0) in block_id_set]

        if not blocks:
            return {"code": 404, "message": "未找到可用于语义分块的数据块", "data": None}, 404

        emit(
            "chunking",
            "已加载输入块，开始执行语义分块",
            28.0,
            {
                "project_id": project_id,
                "dataset_id": dataset_id,
                "block_count": len(blocks),
                "target_protocol": target_protocol or None,
            },
        )

        chunk_models: List[Chunk] = []
        if document_ids:
            total_documents = max(1, len(document_ids))
            for index, (document_id, document_blocks) in enumerate(_split_blocks_by_document_id(blocks), start=1):
                emit(
                    "chunking",
                    f"正在处理文档 {index}/{total_documents}",
                    min(72.0, 28.0 + (index / total_documents) * 42.0),
                    {
                        "project_id": project_id,
                        "dataset_id": dataset_id,
                        "document_id": document_id,
                        "document_block_count": len(document_blocks),
                    },
                )
                chunk_models.extend(
                    _build_chunks_for_document(
                        document_id=document_id,
                        blocks=document_blocks,
                        dataset_id=dataset_id,
                        max_token_size=max_token_size,
                        use_llm_boundary_fallback=use_llm_boundary_fallback,
                        target_protocol=target_protocol,
                        target_page_window=target_page_window,
                    )
                )
        else:
            if target_protocol:
                blocks = filter_blocks_by_target_protocol(blocks, target_protocol=target_protocol, page_window=target_page_window)
                if not blocks:
                    return {"code": 404, "message": f"未找到目标协议{target_protocol}相关块", "data": None}, 404
            blocks_dict = {int(block.block_id): block for block in blocks}
            analyze_params = inspect.signature(analyze_semantic_relations).parameters
            if "use_llm_fallback" in analyze_params:
                chunk_suggestions = analyze_semantic_relations(blocks, max_token_size=max_token_size, use_llm_fallback=use_llm_boundary_fallback)
            else:
                chunk_suggestions = analyze_semantic_relations(blocks, max_token_size)
            refined_chunks = refine_chunks_by_token_limit(chunk_suggestions, blocks_dict, max_token_size)
            chunk_models = _build_chunk_models(refined_chunks, blocks_dict, project_id, dataset_id)
            _persist_chunk_models(chunk_models, blocks_dict)

        if not chunk_models:
            if target_protocol:
                return {"code": 404, "message": f"未找到目标协议{target_protocol}相关块", "data": None}, 404
            return {"code": 404, "message": "未生成有效语义块", "data": None}, 404

        response_blocks_dict = {int(block.block_id): block for block in blocks}
        chunks_payload = [_chunk_payload(chunk, response_blocks_dict) for chunk in chunk_models]

        emit(
            "saving",
            "语义分块完成，正在写入结果",
            82.0,
            {
                "project_id": project_id,
                "dataset_id": dataset_id,
                "chunk_count": len(chunks_payload),
            },
        )
        chunks_file_path = None
        if return_mode in {"path", "both"}:
            chunks_file_path = file_store.save_chunks(dataset_id, chunks_payload)

        doc_index = None
        if build_doc_index_enabled:
            emit(
                "building_doc_index",
                "正在构建文档索引",
                90.0,
                {"project_id": project_id, "dataset_id": dataset_id},
            )
            doc_index_result = build_protocol_doc_index(
                project_id=project_id,
                dataset_id=dataset_id,
                blocks=blocks,
                protocol_type=target_protocol or "",
                source_block_ids=[int(block.block_id) for block in blocks],
                file_store=file_store,
            )
            doc_index = _build_doc_index_response(project_id, doc_index_result)

        result = {
            "code": 200,
            "message": "success",
            "data": {
                "project_id": project_id,
                "dataset_id": dataset_id,
                "source_content_id": content_id or str(source_record.get("content_id") or "").strip() or None,
                "payload_type": "semantic_chunks",
                "source_blocks_file_path": source_payload.get("resolved_path") or blocks_file_path or None,
                "chunks_file_path": chunks_file_path,
                "total_chunks": len(chunks_payload),
                "target_protocol": target_protocol or None,
                "chunks": chunks_payload if return_mode in {"content", "both"} else None,
                "doc_index": doc_index,
            },
        }
        emit(
            "completed",
            "语义分块任务完成",
            100.0,
            {
                "project_id": project_id,
                "dataset_id": dataset_id,
                "chunk_count": len(chunks_payload),
            },
        )
        return result, 200

    except FileNotFoundError as exc:
        return {"code": 400, "message": str(exc), "data": None}, 400
    except ValueError as exc:
        return {"code": 400, "message": str(exc), "data": None}, 400
    except Exception as exc:
        logger.exception("语义分块处理失败: %s", exc)
        return {"code": 500, "message": f"处理失败: {str(exc)}", "data": None}, 500


def run_semantic_chunk_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    response_body, status_code = _execute_semantic_chunk(data)
    return {"status_code": status_code, "result": response_body}


def submit_semantic_chunk_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_chunk_ids = payload.get("source_block_ids") or []
    raw_dataset_id = payload.get("dataset_id")
    document_ids = _normalize_document_id_list(raw_dataset_id) if raw_dataset_id not in (None, "") else []
    dataset_ref = str(raw_dataset_id or "").strip()
    metadata = {
        "project_id": str(payload.get("project_id") or "").strip() or None,
        "dataset_id": dataset_ref or (str(document_ids[0]) if document_ids else None),
        "source_block_count": len(source_chunk_ids) if isinstance(source_chunk_ids, list) else None,
        "content_id": str(payload.get("content_id") or payload.get("document_id") or "").strip() or None,
        "async_mode": True,
    }
    return start_job(
        "semantic_chunk",
        lambda job_id: _run_semantic_chunk_job(job_id, payload),
        metadata=metadata,
    )


def _run_semantic_chunk_job(job_id: str, payload: Dict[str, Any]) -> None:
    def emit(stage: str, message: str, progress: float, extra: Optional[Dict[str, Any]] = None) -> None:
        update_job(
            job_id,
            status="running",
            stage=stage,
            message=message,
            progress=progress,
            extra=extra,
        )

    emit("preparing", "开始准备语义分块任务", 1.0)
    response_body, status_code = _execute_semantic_chunk(payload, progress_callback=emit)
    if status_code >= 400:
        fail_job(job_id, str(response_body.get("message") or "语义分块失败"), result=response_body)
        return
    complete_job(job_id, response_body)


@app.route("/api/data/semantic_chunk", methods=["POST"])
def semantic_chunk():
    """语义单元智能划分与重组接口。默认异步，async=false 时同步执行。"""
    payload = _request_payload()
    if semantic_chunk_async_requested(payload):
        job = submit_semantic_chunk_job(payload or {})
        return build_submit_response(job)
    response_body, status_code = _execute_semantic_chunk(payload or {})
    return jsonify(response_body), status_code


@app.route("/api/data/update_doc_index", methods=["POST"])
def update_doc_index():
    """新文件上传后更新或重建文档索引。优先使用 document_path/document_paths。"""
    try:
        payload = prepare_update_doc_index_payload()
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc), "data": None}), 400
    async_decision = _build_update_doc_index_async_decision(payload)
    request_summary = _summarize_update_doc_index_request(payload, async_decision=async_decision)
    async_requested = bool(async_decision.get("should_async"))
    logger.info(
        "update_doc_index request received: async=%s explicit_async=%s project_id=%s document_count=%s total_bytes=%s max_bytes=%s max_page_count=%s max_estimated_shards=%s file_names=%s async_reasons=%s",
        async_requested,
        request_summary["explicit_async"],
        request_summary["project_id"],
        request_summary["document_count"],
        request_summary["total_bytes"],
        request_summary["max_bytes"],
        request_summary["max_page_count"] or None,
        request_summary["max_estimated_shards"] or None,
        request_summary["file_names"] or None,
        request_summary["async_reasons"] or None,
    )
    if (
        not async_requested
        and request_summary["threshold_bytes"] > 0
        and request_summary["max_bytes"] >= int(request_summary["threshold_bytes"] * 0.9)
    ):
        logger.warning(
            "update_doc_index large synchronous request near auto-async threshold: project_id=%s max_bytes=%s threshold_bytes=%s file_names=%s",
            request_summary["project_id"],
            request_summary["max_bytes"],
            request_summary["threshold_bytes"],
            request_summary["file_names"] or None,
        )
    if async_requested:
        if not request_summary["explicit_async"]:
            logger.info(
                "update_doc_index auto async activated: project_id=%s file_names=%s async_reasons=%s",
                request_summary["project_id"],
                request_summary["file_names"] or None,
                request_summary["async_reasons"] or None,
            )
        job = submit_update_doc_index_job(payload, async_decision=async_decision)
        return build_submit_response(job)
    response_body, status_code = _execute_update_doc_index(payload)
    return jsonify(response_body), status_code


def run_update_doc_index_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    response_body, status_code = _execute_update_doc_index(data)
    return {
        "status_code": status_code,
        "result": response_body,
    }


def submit_update_doc_index_job(
    payload: Dict[str, Any],
    async_decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    document_paths = _normalize_document_paths(payload.get("document_paths") or payload.get("document_path"))
    file_names = _normalize_string_list(payload.get("file_names"), "file_names")
    project_id = str(payload.get("project_id") or "").strip()
    decision = async_decision if isinstance(async_decision, dict) else _build_update_doc_index_async_decision(payload)
    document_profiles = decision.get("document_profiles") or []
    metadata = {
        "project_id": project_id or None,
        "document_count": len(document_paths),
        "file_names": file_names or None,
        "async_mode": True,
        "async_reasons": list(decision.get("reasons") or []),
        "max_page_count": max((int(profile.get("page_count") or 0) for profile in document_profiles), default=0) or None,
        "max_estimated_shards": max((int(profile.get("estimated_shards") or 0) for profile in document_profiles), default=0) or None,
    }
    return start_job(
        "update_doc_index",
        lambda job_id: _run_update_doc_index_job(job_id, payload),
        metadata=metadata,
    )


def _run_update_doc_index_job(job_id: str, payload: Dict[str, Any]) -> None:
    def emit(stage: str, message: str, progress: float, extra: Optional[Dict[str, Any]] = None) -> None:
        update_job(
            job_id,
            status="running",
            stage=stage,
            message=message,
            progress=progress,
            extra=extra,
        )

    emit("preparing", "开始准备索引更新任务", 1.0)
    response_body, status_code = _execute_update_doc_index(payload, progress_callback=emit)
    if status_code >= 400:
        fail_job(job_id, str(response_body.get("message") or "索引更新失败"), result=response_body)
        return
    complete_job(job_id, response_body)


def _execute_update_doc_index(
    data: Dict[str, Any],
    progress_callback: Optional[Callable[[str, str, float, Optional[Dict[str, Any]]], None]] = None,
) -> Tuple[Dict[str, Any], int]:
    started_at = time.perf_counter()
    try:
        def emit(stage: str, message: str, progress: float, extra: Optional[Dict[str, Any]] = None) -> None:
            if progress_callback is not None:
                progress_callback(stage, message, progress, extra)

        if not isinstance(data, dict):
            return {"code": 400, "message": "请求体必须是JSON对象或表单对象", "data": None}, 400

        blocks_file_path = str(data.get("blocks_file_path") or data.get("cleaned_blocks_file_path") or "").strip()
        project_id = str(data.get("project_id") or "").strip()
        dataset_id = str(data.get("dataset_id") or "").strip()
        source_block_ids = data.get("source_block_ids") or []
        document_paths = _normalize_document_paths(data.get("document_paths") or data.get("document_path"))
        file_names = _normalize_string_list(data.get("file_names"), "file_names")
        target_protocol = normalize_target_protocol(data.get("target_protocol"))
        doc_set_id = str(data.get("doc_set_id") or "").strip()
        index_ref = str(data.get("index_ref") or "").strip()
        rebuild = _as_bool(data.get("rebuild", True), default=True)
        document_fingerprints = _build_document_fingerprint_map(document_paths) if document_paths else {}

        if source_block_ids and not isinstance(source_block_ids, list):
            return {"code": 400, "message": "source_block_ids必须是数组", "data": None}, 400

        if document_paths:
            derived_file_names = _derive_file_names_from_document_paths(document_paths)
            file_names = derived_file_names + [name for name in file_names if name not in set(derived_file_names)]

        emit("preparing", "已解析请求参数，正在检查现有索引", 8.0)
        registry_hint: Dict[str, Any] = {}
        if document_paths and project_id and not doc_set_id and not index_ref:
            registry_hint = _find_matching_registry_for_documents(
                project_id=project_id,
                file_names=file_names,
                document_fingerprints=document_fingerprints,
            )
        if not registry_hint:
            registry_hint = file_store.resolve_pageindex_registry(
                project_id=project_id,
                dataset_id=dataset_id,
                doc_set_id=doc_set_id,
                index_ref=index_ref,
            )
        if registry_hint and not project_id:
            project_id = str(registry_hint.get("project_id") or "").strip()
        if registry_hint and not doc_set_id:
            doc_set_id = str(registry_hint.get("doc_set_id") or "").strip()
        if registry_hint and not index_ref:
            index_ref = str(registry_hint.get("index_ref") or "").strip()
        if registry_hint and document_paths and not rebuild:
            resolved_project_id = project_id or str(registry_hint.get("project_id") or "").strip()
            doc_index = _build_doc_index_response(resolved_project_id, registry_hint)
            return {
                "code": 200,
                "message": "success",
                "data": {
                    "project_id": resolved_project_id,
                    "dataset_id": str(registry_hint.get("dataset_id") or "").strip() or None,
                    "doc_set_id": doc_index["doc_set_id"],
                    "index_ref": doc_index["index_ref"],
                    "status": doc_index["status"],
                    "document_count": doc_index["document_count"],
                    "source_block_count": None,
                    "file_names": file_names or None,
                    "document_paths": document_paths or None,
                    "storage_path": doc_index["storage_path"],
                    "rebuild": rebuild,
                }
            }, 200

        if blocks_file_path:
            emit("loading_blocks", "正在从块文件加载文档块", 20.0)
            resolved_project_id, blocks, _ = _load_blocks_from_file(blocks_file_path, project_id_hint=project_id)
            project_id = project_id or resolved_project_id
        elif document_paths:
            emit("loading_documents", "正在解析上传文档并生成索引块", 20.0)
            resolved_project_id, blocks = load_blocks_from_document_paths(document_paths, project_id_hint=project_id)
            project_id = project_id or resolved_project_id
        elif file_names:
            emit("loading_blocks", "正在按文件名读取已有文档块", 20.0)
            blocks = mysql_client.get_blocks_by_file_names(file_names, project_id=project_id)
            selected_project_id, selected_blocks = _select_project_scoped_blocks(
                blocks,
                fallback_seed="|".join(file_names),
            )
            project_id = project_id or selected_project_id
            if not str(data.get("project_id") or "").strip():
                blocks = selected_blocks
        else:
            if not project_id:
                return {"code": 400, "message": "缺少document_path、document_paths、blocks_file_path、file/files或project_id参数", "data": None}, 400
            if source_block_ids:
                emit("loading_blocks", "正在按块 ID 读取已有文档块", 20.0)
                blocks = mysql_client.get_blocks_by_ids(source_block_ids)
            else:
                emit("loading_blocks", "正在按项目读取已有文档块", 20.0)
                blocks = mysql_client.get_blocks_by_project(project_id)

        if not registry_hint:
            registry_hint = file_store.resolve_pageindex_registry(
                project_id=project_id,
                dataset_id=dataset_id,
                doc_set_id=doc_set_id,
                index_ref=index_ref,
            )
        if registry_hint:
            if not doc_set_id:
                doc_set_id = str(registry_hint.get("doc_set_id") or "").strip()
            if not index_ref:
                index_ref = str(registry_hint.get("index_ref") or "").strip()

        if file_names:
            file_name_set = {str(name).strip() for name in file_names if str(name).strip()}
            blocks = [block for block in blocks if str(getattr(block, "file_name", "") or "").strip() in file_name_set]
        if source_block_ids and not document_paths:
            source_id_set = {int(bid) for bid in source_block_ids}
            blocks = [block for block in blocks if int(getattr(block, "block_id", 0) or 0) in source_id_set]
        if target_protocol:
            blocks = filter_blocks_by_target_protocol(blocks, target_protocol=target_protocol, page_window=0)

        if not blocks:
            if document_paths:
                return {"code": 404, "message": "未找到这些文档对应的已上传数据块，请先调用 upload_split 上传文档并生成块数据", "data": None}, 404
            return {"code": 404, "message": "未找到可用于更新索引的数据块", "data": None}, 404

        emit(
            "building_index",
            "文档内容已准备完成，正在构建 PageIndex 索引",
            70.0,
            {"project_id": project_id, "source_block_count": len(blocks)},
        )
        logger.info(
            "update_doc_index building PageIndex: project_id=%s dataset_id=%s doc_set_id=%s source_block_count=%s file_names=%s document_paths=%s rebuild=%s",
            project_id,
            dataset_id or None,
            doc_set_id or None,
            len(blocks),
            file_names or None,
            document_paths or None,
            rebuild,
        )
        doc_index_result = build_protocol_doc_index(
            project_id=project_id,
            dataset_id=dataset_id,
            blocks=blocks,
            protocol_type=target_protocol or "",
            file_names=file_names or None,
            document_paths=document_paths or None,
            document_fingerprints=document_fingerprints or None,
            source_block_ids=[int(getattr(block, "block_id", 0) or 0) for block in blocks],
            doc_set_id=doc_set_id,
            index_ref=index_ref,
            rebuild=rebuild,
            file_store=file_store,
        )
        doc_index = _build_doc_index_response(project_id, doc_index_result)
        emit(
            "persisted",
            "索引构建完成，正在整理响应",
            95.0,
            {"doc_set_id": doc_index["doc_set_id"], "index_ref": doc_index["index_ref"]},
        )
        logger.info(
            "update_doc_index completed: project_id=%s dataset_id=%s doc_set_id=%s index_ref=%s document_count=%s indexed_shard_count=%s duration_seconds=%.3f",
            project_id,
            dataset_id or None,
            doc_index["doc_set_id"],
            doc_index["index_ref"],
            doc_index["document_count"],
            int(doc_index_result.get("indexed_shard_count") or 0),
            time.perf_counter() - started_at,
        )

        return {
            "code": 200,
            "message": "success",
            "data": {
                "project_id": project_id,
                "dataset_id": dataset_id or None,
                "doc_set_id": doc_index["doc_set_id"],
                "index_ref": doc_index["index_ref"],
                "status": doc_index["status"],
                "document_count": doc_index["document_count"],
                "source_block_count": len(blocks),
                "file_names": file_names or None,
                "document_paths": document_paths or None,
                "storage_path": doc_index["storage_path"],
                "rebuild": rebuild,
            }
        }, 200
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}, 400
    except FileNotFoundError as e:
        return {"code": 400, "message": str(e), "data": None}, 400
    except Exception as e:
        logger.exception(
            "更新文档索引失败: %s (duration_seconds=%.3f)",
            e,
            time.perf_counter() - started_at,
        )
        return {"code": 500, "message": f"更新文档索引失败: {str(e)}", "data": None}, 500


@app.route("/api/data/update_doc_index/status", methods=["GET"])
def update_doc_index_status():
    job_id = str(request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    return build_status_response(job_id)


@app.route("/api/data/update_doc_index/stream", methods=["GET"])
def update_doc_index_stream():
    job_id = str(request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    return build_stream_response(job_id)


@app.route("/api/data/semantic_chunk/preview", methods=["POST"])
def preview_semantic_chunk():
    """
    语义分块预览接口（不保存到数据库）

    输入参数同上，但不保存结果
    """
    try:
        data = request.json
        if not data:
            return jsonify({
                "code": 400,
                "message": "请求体不能为空",
                "data": None
            }), 400

        project_id = data.get("project_id")
        source_block_ids = data.get("source_block_ids")
        config = data.get("config", {})
        max_token_size = config.get("max_token_size", 1024)
        use_llm_boundary_fallback = bool(config.get("use_llm_boundary_fallback", True))
        target_protocol = normalize_target_protocol(config.get("target_protocol"))
        try:
            target_page_window = max(0, int(config.get("target_page_window", 0) or 0))
        except (TypeError, ValueError):
            target_page_window = 0

        if not project_id:
            return jsonify({
                "code": 400,
                "message": "缺少project_id参数",
                "data": None
            }), 400

        # 获取数据块
        if source_block_ids:
            blocks = mysql_client.get_blocks_by_ids(source_block_ids)
        else:
            blocks = mysql_client.get_blocks_by_project(project_id)
        if not blocks:
            return jsonify({
                "code": 404,
                "message": "未找到可用于语义分块的数据块",
                "data": None
            }), 404

        if target_protocol:
            blocks = filter_blocks_by_target_protocol(
                blocks,
                target_protocol=target_protocol,
                page_window=target_page_window,
            )
            if not blocks:
                return jsonify({
                    "code": 404,
                    "message": f"未找到目标协议{target_protocol}相关块",
                    "data": None
                }), 404

        blocks_dict = {b.block_id: b for b in blocks}

        # 分析语义
        analyze_params = inspect.signature(analyze_semantic_relations).parameters
        if "use_llm_fallback" in analyze_params:
            chunk_suggestions = analyze_semantic_relations(
                blocks,
                max_token_size=max_token_size,
                use_llm_fallback=use_llm_boundary_fallback,
            )
        else:
            chunk_suggestions = analyze_semantic_relations(blocks, max_token_size)
        refined_chunks = refine_chunks_by_token_limit(
            chunk_suggestions, blocks_dict, max_token_size
        )

        # 构建预览结果
        preview_chunks = []
        for idx, chunk_data in enumerate(refined_chunks):
            block_ids = chunk_data.get("block_ids", [])
            blocks_in_chunk = [blocks_dict[bid] for bid in block_ids if bid in blocks_dict]
            content_snapshot = generate_content_snapshot(blocks_in_chunk, max_length=200)
            full_content, tokens = merge_block_contents(blocks_in_chunk)

            preview_chunks.append({
                "chunk_index": idx,
                "source_block_ids": block_ids,
                "semantic_type": chunk_data.get("semantic_type", "general_content"),
                "content_preview": content_snapshot,
                "estimated_tokens": tokens,
                "reason": chunk_data.get("reason", ""),
                "metadata": chunk_data.get("metadata", {}),
            })

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "total_blocks": len(blocks),
                "total_chunks": len(preview_chunks),
                "max_token_size": max_token_size,
                "target_protocol": target_protocol or None,
                "chunks": preview_chunks
            }
        })

    except Exception as e:
        logger.exception(f"预览失败: {e}")
        return jsonify({
            "code": 500,
            "message": f"预览失败: {str(e)}",
            "data": None
        }), 500


@app.route("/api/data/semantic_chunk/status", methods=["GET"])
def semantic_chunk_status():
    job_id = str(request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    return build_status_response(job_id)


@app.route("/api/data/semantic_chunk/stream", methods=["GET"])
def semantic_chunk_stream():
    job_id = str(request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    return build_stream_response(job_id)


@app.route("/api/data/semantic_chunk/status/<task_id>", methods=["GET"])
def get_chunk_status(task_id: str):
    """
    获取分块任务状态

    注意：当前实现是同步的，此接口保留用于未来异步任务支持
    """
    if get_job_snapshot(task_id) is not None:
        return build_status_response(task_id)
    return jsonify({
        "code": 200,
        "message": "success",
        "data": {
            "task_id": task_id,
            "status": "completed",
            "message": "分块任务已完成"
        }
    })


@app.route("/health", methods=["GET"])
def health():
    """健康检查接口"""
    return jsonify({"status": "healthy", "service": "semantic_chunk"})

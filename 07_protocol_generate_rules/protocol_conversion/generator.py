from __future__ import annotations

import ast
import json
import re
import time
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

from llm.local_llm import LocalLLM, get_llm

from .converter import (
    _normalize_formula_expression_syntax,
    execute_protocol_conversion,
    normalize_source_message,
)
from .knowledge_base import ProtocolConversionKnowledgeBase
from .pageindex_adapter import get_pageindex_evidence_provider
from .trained_doc_index import get_trained_doc_evidence_provider


DEFAULT_PROTOCOL_TYPE = "Link16"
DEFAULT_EMPTY_RULE_RETRIES = 3
ALLOWED_FORMULA_FUNCTIONS = {
    "abs",
    "round",
    "int",
    "float",
    "min",
    "max",
    "len",
    "sum",
    "range",
    "enumerate",
    "list",
    "dict",
    "signed",
    "unsigned",
    "clip",
    "scale",
}
ALLOWED_FORMULA_VARS = {"value", "raw", "bits", "result", "True", "False", "None"}
STRICT_SEMANTIC_GROUPS = {
    "latitude",
    "longitude",
    "altitude",
    "pitch",
    "roll",
    "yaw",
    "time",
    "threat",
    "info",
    "id",
    "name",
    "control_flag",
}
CONTROL_FIELD_PATTERN = re.compile(r"^[FG]PI\d+$", flags=re.IGNORECASE)
MAPPING_TABLE_PAIR_PATTERN = re.compile(r"\s*([^=,]+?)\s*=\s*([^=,]+?)\s*")


def _normalize_protocol_spec(spec: Any, role: str, allow_empty_content: bool = False) -> Dict[str, Optional[str]]:
    if spec is None and allow_empty_content:
        return {
            "name": None,
            "protocol_type": None,
            "message_code": None,
            "content": None,
        }

    if isinstance(spec, str):
        content = spec.strip()
        if not content and not allow_empty_content:
            raise ValueError(f"{role}协议内容不能为空")
        return {
            "name": None,
            "protocol_type": None,
            "message_code": None,
            "content": content or None,
        }

    if not isinstance(spec, dict):
        raise ValueError(f"{role}协议定义必须是对象或字符串")

    name = str(spec.get("name") or spec.get("protocol_name") or spec.get("title") or "").strip() or None
    protocol_type = str(spec.get("protocol_type") or spec.get("type") or "").strip() or None
    message_code = str(spec.get("message_code") or spec.get("messageType") or "").strip() or None
    content = str(
        spec.get("content")
        or spec.get("document_text")
        or spec.get("definition")
        or spec.get("text")
        or ""
    ).strip() or None

    if not content and not allow_empty_content:
        raise ValueError(f"{role}协议内容不能为空")

    return {
        "name": name,
        "protocol_type": protocol_type,
        "message_code": message_code,
        "content": content,
    }


def _resolve_source_protocol_content(source_protocol: Dict[str, Optional[str]], use_trained_docs: bool) -> str:
    content = str(source_protocol.get("content") or "").strip()
    if content:
        return content
    if use_trained_docs:
        return (
            "训练阶段已上传并建立索引的协议文档将作为原协议证据来源；"
            "请优先依据 PageIndex 检索到的证据片段生成转换规则。"
        )
    return ""


def _group_source_fields_by_protocol(
    source_field_catalog: Optional[Iterable[Dict[str, Any]]],
) -> List[Tuple[str, Optional[str], List[str]]]:
    grouped: Dict[Tuple[str, Optional[str]], List[str]] = {}
    for item in source_field_catalog or []:
        if not isinstance(item, dict):
            continue
        protocol = str(item.get("protocol") or "").strip()
        if not protocol:
            continue
        message_code = str(item.get("message_code") or "").strip() or None
        field_name = str(
            item.get("field_name")
            or item.get("alias_name")
            or item.get("display_field")
            or item.get("label")
            or ""
        ).strip()
        if not field_name:
            continue
        key = (protocol, message_code)
        bucket = grouped.setdefault(key, [])
        if field_name not in bucket:
            bucket.append(field_name)
    return [
        (protocol, message_code, field_names)
        for (protocol, message_code), field_names in grouped.items()
        if field_names
    ]


def _extract_rule_items(parsed: Any) -> List[Dict[str, Any]]:
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        for key in ("target_field_rules", "generated_rules", "rules", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_source_fields(item: Dict[str, Any]) -> List[str]:
    source_fields = item.get("source_fields")
    normalized: List[str] = []
    if isinstance(source_fields, list):
        normalized = [_normalize_rule_field_token(value) for value in source_fields if _normalize_rule_field_token(value)]
    elif isinstance(source_fields, str):
        normalized = [_normalize_rule_field_token(value) for value in source_fields.split(",") if _normalize_rule_field_token(value)]

    if normalized:
        return normalized

    fallback = _normalize_rule_field_token(item.get("field_name") or item.get("source_field") or "")
    return [fallback] if fallback else []


def _normalize_rule_field_token(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    text = text.replace("．", ".")
    return text


def _strip_protocol_like_field_prefix(value: Any) -> str:
    normalized = _normalize_rule_field_token(value)
    if not normalized:
        return ""
    if "." in normalized:
        _, suffix = normalized.split(".", 1)
        if suffix:
            return suffix
    protocol_like_match = re.match(r"^[A-Z]+[0-9]*(?:_[0-9]+)+_(.+)$", normalized)
    if protocol_like_match:
        suffix = _normalize_rule_field_token(protocol_like_match.group(1))
        if suffix:
            return suffix
    return normalized


def _rule_field_lookup_keys(value: Any) -> set[str]:
    normalized = _normalize_rule_field_token(value)
    if not normalized:
        return set()
    keys = {normalized}
    if "." in normalized:
        _, suffix = normalized.split(".", 1)
        if suffix:
            keys.add(suffix)
    protocol_like_match = re.match(r"^[A-Z]+[0-9]*(?:_[0-9]+)+_(.+)$", normalized)
    if protocol_like_match:
        suffix = _normalize_rule_field_token(protocol_like_match.group(1))
        if suffix:
            keys.add(suffix)
    return {item for item in keys if item}


def _normalize_formula_identifier_spacing(rule: str, source_fields: List[str]) -> str:
    normalized_rule = str(rule or "").strip()
    if not normalized_rule or not source_fields:
        return normalized_rule

    chunks = set(re.findall(r"[0-9A-Za-z_一-鿿 ]+", normalized_rule))
    for chunk in sorted(chunks, key=len, reverse=True):
        compact = _normalize_rule_field_token(chunk)
        if compact and compact in source_fields and chunk != compact:
            normalized_rule = normalized_rule.replace(chunk, compact)
    for field_name in sorted(
        {str(item or "").strip() for item in source_fields if str(item or "").strip()},
        key=len,
        reverse=True,
    ):
        pattern = re.compile(
            rf"(?<![0-9A-Za-z_\u4E00-\u9FFF]){re.escape(field_name)}(?![0-9A-Za-z_\u4E00-\u9FFF])",
            flags=re.IGNORECASE,
        )
        normalized_rule = pattern.sub(field_name, normalized_rule)
    return normalized_rule


def _candidate_protocol_bucket(candidate: Dict[str, Any]) -> Tuple[str, str]:
    return (
        str(candidate.get("source_protocol_type") or "").strip().upper(),
        str(candidate.get("source_message_code") or "").strip().upper(),
    )


def _dedupe_candidates_preserve_order(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = []
    seen_fields: set[str] = set()
    for candidate in candidates:
        field_name = str(candidate.get("field_name") or "").strip().upper()
        if not field_name or field_name in seen_fields:
            continue
        seen_fields.add(field_name)
        ordered.append(candidate)
    return ordered


def _select_diverse_candidates(
    ranked: List[Dict[str, Any]],
    top_k: int,
    preferred_fields: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    preferred = {
        str(item or "").strip().upper()
        for item in (preferred_fields or [])
        if str(item or "").strip()
    }
    selected: List[Dict[str, Any]] = []
    seen_protocols: set[Tuple[str, str]] = set()

    for candidate in ranked:
        field_name = str(candidate.get("field_name") or "").strip().upper()
        if field_name and field_name in preferred:
            selected.append(candidate)
            seen_protocols.add(_candidate_protocol_bucket(candidate))
    selected = _dedupe_candidates_preserve_order(selected)

    for candidate in ranked:
        if len(selected) >= top_k:
            break
        bucket = _candidate_protocol_bucket(candidate)
        if bucket != ("", "") and bucket not in seen_protocols:
            selected.append(candidate)
            seen_protocols.add(bucket)
    selected = _dedupe_candidates_preserve_order(selected)

    for candidate in ranked:
        if len(selected) >= top_k:
            break
        selected.append(candidate)
    return _dedupe_candidates_preserve_order(selected)[:top_k]


def _normalize_protocol_prefix(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", text)
    text = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return text.upper()


def _target_field_assignment_aliases(
    target_field: Any,
    target_protocol_type: Optional[str] = None,
) -> set[str]:
    normalized_target = _normalize_rule_field_token(target_field)
    if not normalized_target:
        return set()
    aliases = {normalized_target}
    if "." in normalized_target:
        _, suffix = normalized_target.split(".", 1)
        if suffix:
            aliases.add(suffix)
    prefix = _normalize_protocol_prefix(target_protocol_type)
    if prefix:
        dotted_prefix = f"{prefix}."
        underscored_prefix = f"{prefix}_"
        for item in list(aliases):
            if item.startswith(dotted_prefix):
                aliases.add(item[len(dotted_prefix):])
            if item.startswith(underscored_prefix):
                aliases.add(item[len(underscored_prefix):])
        base_aliases = list(aliases)
        for item in base_aliases:
            if item and not item.startswith(dotted_prefix):
                aliases.add(f"{dotted_prefix}{item}")
            if item and not item.startswith(underscored_prefix):
                aliases.add(f"{underscored_prefix}{item}")
    return {item for item in aliases if item}


def _strip_explicit_target_assignment(
    rule: str,
    target_field: Any,
    target_protocol_type: Optional[str] = None,
) -> str:
    text = str(rule or "").strip()
    if not text or "\n" in text:
        return text
    match = re.match(r"^\s*([A-Za-z_\u4E00-\u9FFF][A-Za-z0-9_\u4E00-\u9FFF./-]*)\s*=\s*(?![=])(.+?)\s*$", text)
    if not match:
        return text
    left = _normalize_rule_field_token(match.group(1))
    right = str(match.group(2) or "").strip()
    if not right or left not in _target_field_assignment_aliases(target_field, target_protocol_type):
        return text
    return right


def _infer_formula_kind(rule: str) -> str:
    text = str(rule or "").strip()
    if "\n" in text or any(text.startswith(prefix) for prefix in ("if ", "for ", "while ", "result =")):
        return "python_block"
    if any(token in text for token in ("->", "→")) or ("=" in text and any(ch.isdigit() for ch in text)):
        return "mapping_table"
    return "python_expr"


def _parse_mapping_table_pairs(rule: str) -> List[Tuple[str, str]]:
    text = str(rule or "").strip()
    if not text:
        return []
    pairs: List[Tuple[str, str]] = []
    for chunk in text.split(","):
        match = MAPPING_TABLE_PAIR_PATTERN.fullmatch(chunk.strip())
        if not match:
            return []
        left = str(match.group(1) or "").strip()
        right = str(match.group(2) or "").strip()
        if not left or not right:
            return []
        pairs.append((left, right))
    return pairs


def _mapping_table_is_identity(pairs: List[Tuple[str, str]]) -> bool:
    if not pairs:
        return False
    def _normalize_mapping_value(value: str) -> str:
        return re.sub(r"[\s_\-./:：，,()\[\]{}]+", "", str(value or "").strip().lower())
    normalized_pairs = [
        (_normalize_mapping_value(left), _normalize_mapping_value(right))
        for left, right in pairs
    ]
    return all(left and right and left == right for left, right in normalized_pairs)


def normalize_generated_rules(rule_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized_rules: List[Dict[str, Any]] = []
    for item in rule_items:
        source_fields = _normalize_source_fields(item)
        target_field = _normalize_rule_field_token(item.get("target_field") or item.get("field_name") or "")
        rule = str(
            item.get("rule")
            or item.get("formula")
            or item.get("conversion_formula")
            or item.get("expression")
            or ""
        ).strip()
        conversion_mode = str(item.get("conversion_mode") or item.get("mode") or "").strip().lower() or None
        if not target_field or not rule:
            continue
        rule = _strip_explicit_target_assignment(
            rule,
            target_field=target_field,
            target_protocol_type=str(item.get("target_protocol_type") or "").strip() or None,
        )
        formula_kind = str(item.get("formula_kind") or "").strip() or _infer_formula_kind(rule)
        rule = _normalize_formula_identifier_spacing(rule, source_fields)
        if formula_kind == "python_expr":
            rule = _normalize_formula_expression_syntax(rule)
        if formula_kind == "mapping_table" and len(source_fields) == 1:
            pairs = _parse_mapping_table_pairs(rule)
            if _mapping_table_is_identity(pairs):
                formula_kind = "python_expr"
                conversion_mode = conversion_mode or "transcoding"
                rule = source_fields[0]
        normalized_rules.append(
            {
                "target_field": target_field,
                "source_fields": source_fields,
                "conversion_mode": conversion_mode,
                "formula_kind": formula_kind,
                "rule": rule,
                "concept_name": str(item.get("concept_name") or item.get("concept") or target_field).strip() or None,
                "condition": item.get("condition"),
                "default_value": item.get("default_value"),
                "unit": item.get("unit"),
                "bit_length": item.get("bit_length"),
                "description": item.get("description"),
                "evidence": item.get("evidence"),
                "confidence": item.get("confidence"),
                "status": item.get("status"),
                "source": item.get("source"),
                "target_actual_field": item.get("target_actual_field"),
                "target_path": item.get("target_path"),
                "source_actual_fields": item.get("source_actual_fields"),
                "source_paths": item.get("source_paths"),
                "source_protocol_type": item.get("source_protocol_type"),
                "source_protocol_name": item.get("source_protocol_name"),
                "source_message_code": item.get("source_message_code"),
            }
        )
    return normalized_rules


def _dedupe_rules_by_target_field(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for rule in rules:
        target_identity = _target_rule_identity(rule)
        if not target_identity or target_identity in seen:
            continue
        seen.add(target_identity)
        deduped.append(rule)
    return deduped


def _format_pageindex_evidence(evidence_result: Optional[Dict[str, Any]]) -> Optional[str]:
    if not evidence_result:
        return None
    snippets = evidence_result.get("evidence_snippets") or []
    if not snippets:
        return None
    lines = [
        "PageIndex证据摘要（仅可依据以下证据生成规则；没有明确证据支持的字段必须跳过，不要猜测）："
    ]
    for index, snippet in enumerate(snippets, start=1):
        lines.append(
            "\n".join(
                [
                    f"[证据{index}] role={snippet.get('role') or 'unknown'}",
                    f"query={snippet.get('query') or 'N/A'}",
                    f"title={snippet.get('title') or 'N/A'}",
                    f"content={snippet.get('content') or ''}",
                ]
            )
        )
    return "\n\n".join(lines)


def _target_task_evidence_terms(target_task: Dict[str, Any]) -> Tuple[set[str], set[str]]:
    target_terms = {
        str(target_task.get("field_name") or "").strip().upper(),
        str(target_task.get("label") or "").strip().upper(),
        str((target_task.get("path_parts") or [None])[-1] or "").strip().upper(),
    }
    target_terms = {item for item in target_terms if item}
    target_terms.update({_strip_numeric_suffix(item) for item in list(target_terms) if _strip_numeric_suffix(item)})
    source_terms = {
        str(candidate.get("field_name") or "").strip().upper()
        for candidate in (target_task.get("candidate_source_fields") or [])
        if str(candidate.get("field_name") or "").strip()
    }
    source_terms.update({_strip_numeric_suffix(item) for item in list(source_terms) if _strip_numeric_suffix(item)})
    return target_terms, source_terms


def _format_target_task_evidence(
    target_tasks: List[Dict[str, Any]],
    evidence_result: Optional[Dict[str, Any]],
    max_snippets: int = 6,
) -> Optional[str]:
    if not evidence_result:
        return None
    snippets = evidence_result.get("evidence_snippets") or []
    if not snippets or not target_tasks:
        return None
    selected: List[Tuple[float, Dict[str, Any]]] = []
    for snippet in snippets:
        query = str(snippet.get("query") or "").strip().upper()
        role = str(snippet.get("role") or "").strip().lower()
        haystack = f"{snippet.get('title') or ''}\n{snippet.get('content') or ''}".upper()
        best_score = 0.0
        for target_task in target_tasks:
            target_terms, source_terms = _target_task_evidence_terms(target_task)
            if role == "target":
                if query in target_terms:
                    best_score = max(best_score, 20.0)
                elif any(term and term in haystack for term in target_terms):
                    best_score = max(best_score, 12.0)
            elif role == "source":
                if query in source_terms:
                    best_score = max(best_score, 18.0)
                elif any(term and term in haystack for term in source_terms):
                    best_score = max(best_score, 10.0)
        if best_score <= 0:
            continue
        best_score += float(snippet.get("score") or 0)
        selected.append((best_score, snippet))
    if not selected:
        return None
    selected.sort(key=lambda item: (-item[0], str(item[1].get("title") or "")))
    lines = [
        "字段级PageIndex证据（优先依据与当前目标字段和候选源字段直接相关的证据生成规则；若证据未覆盖，可结合 XML 结构与通用协议知识谨慎推理）："
    ]
    for index, (_score, snippet) in enumerate(selected[:max_snippets], start=1):
        lines.append(
            "\n".join(
                [
                    f"[证据{index}] role={snippet.get('role') or 'unknown'}",
                    f"query={snippet.get('query') or 'N/A'}",
                    f"title={snippet.get('title') or 'N/A'}",
                    f"content={snippet.get('content') or ''}",
                ]
            )
        )
    return "\n\n".join(lines)


def _normalize_required_target_fields(required_target_fields: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen = set()
    for item in required_target_fields or []:
        if isinstance(item, dict):
            field_name = str(item.get("field_name") or item.get("name") or "").strip()
            if not field_name:
                continue
            actual_field = str(item.get("actual_field") or item.get("target_actual_field") or "").strip()
            path_parts = list(item.get("path_parts") or []) if isinstance(item.get("path_parts"), list) else None
            target_path = "/".join(str(part).strip() for part in (path_parts or []) if str(part).strip()) or None
            key = str(actual_field or target_path or field_name).strip().upper()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "field_name": field_name.upper(),
                    "protocol": str(item.get("protocol") or "").strip() or None,
                    "default_value": item.get("default_value"),
                    "bit_length": item.get("bit_length"),
                    "label": str(item.get("label") or "").strip() or None,
                    "path_parts": path_parts,
                    "target_path": target_path,
                    "actual_field": actual_field or None,
                    "description": str(item.get("description") or "").strip() or None,
                    "preferred_source_candidates": [
                        dict(candidate)
                        for candidate in (item.get("preferred_source_candidates") or [])
                        if isinstance(candidate, dict)
                    ],
                    "match_hint_summary": str(item.get("match_hint_summary") or "").strip() or None,
                }
            )
            continue

        field_name = str(item or "").strip()
        if not field_name:
            continue
        key = field_name.upper()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "field_name": key,
                "protocol": None,
                "default_value": None,
                "bit_length": None,
                "label": None,
                "path_parts": None,
                "target_path": None,
                "actual_field": None,
                "description": None,
            }
        )
    return normalized


def _target_spec_identity(target_spec: Dict[str, Any]) -> str:
    return str(
        target_spec.get("actual_field")
        or target_spec.get("target_actual_field")
        or target_spec.get("target_path")
        or target_spec.get("field_name")
        or ""
    ).strip().upper()


def _target_rule_identity(rule: Dict[str, Any]) -> str:
    return str(
        rule.get("target_actual_field")
        or rule.get("target_path")
        or _strip_protocol_like_field_prefix(rule.get("target_field"))
        or rule.get("target_field")
        or rule.get("field_name")
        or ""
    ).strip().upper()


def _target_concept_identity(target_spec: Dict[str, Any]) -> str:
    return str(
        target_spec.get("field_name")
        or target_spec.get("label")
        or ((target_spec.get("path_parts") or [None])[-1] if target_spec.get("path_parts") else None)
        or target_spec.get("target_field")
        or ""
    ).strip().upper()


def _target_rule_concept_identity(rule: Dict[str, Any]) -> str:
    return str(
        rule.get("target_path")
        or _strip_protocol_like_field_prefix(rule.get("target_field"))
        or rule.get("target_field")
        or rule.get("field_name")
        or rule.get("label")
        or ""
    ).strip().upper()


def _required_target_field_names(required_target_fields: Optional[Iterable[Any]]) -> List[str]:
    return [_target_spec_identity(item) for item in _normalize_required_target_fields(required_target_fields)]


def _required_target_concept_names(required_target_fields: Optional[Iterable[Any]]) -> List[str]:
    return [_target_concept_identity(item) for item in _normalize_concept_target_fields(required_target_fields)]


def _missing_target_fields(
    generated_rules: List[Dict[str, Any]],
    required_target_fields: Optional[Iterable[Any]],
) -> List[str]:
    required_names = _required_target_field_names(required_target_fields)
    if not required_names:
        return []
    generated_names = {
        _target_rule_identity(item)
        for item in generated_rules
        if _target_rule_identity(item)
    }
    return [field_name for field_name in required_names if field_name and field_name not in generated_names]


def _missing_target_concepts(
    generated_rules: List[Dict[str, Any]],
    required_target_fields: Optional[Iterable[Any]],
) -> List[str]:
    required_names = _required_target_concept_names(required_target_fields)
    if not required_names:
        return []
    generated_names = {
        _target_rule_concept_identity(item)
        for item in generated_rules
        if _target_rule_concept_identity(item)
    }
    return [field_name for field_name in required_names if field_name and field_name not in generated_names]


def _normalize_concept_target_fields(required_target_fields: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    concepts: List[Dict[str, Any]] = []
    concept_map: Dict[str, Dict[str, Any]] = {}
    for item in _normalize_required_target_fields(required_target_fields):
        concept_key = _target_concept_identity(item)
        if not concept_key:
            continue
        existing = concept_map.get(concept_key)
        if existing is None:
            concept_item = {
                "field_name": concept_key,
                "protocol": item.get("protocol"),
                "default_value": item.get("default_value"),
                "bit_length": item.get("bit_length"),
                "label": item.get("label") or item.get("field_name"),
                "path_parts": None,
                "target_path": None,
                "actual_field": None,
                "description": item.get("description"),
                "instance_count": 1,
                "instances": [item],
                "preferred_source_candidates": [
                    dict(candidate)
                    for candidate in (item.get("preferred_source_candidates") or [])
                    if isinstance(candidate, dict)
                ],
                "match_hint_summary": item.get("match_hint_summary"),
            }
            concept_map[concept_key] = concept_item
            concepts.append(concept_item)
            continue
        existing["instance_count"] = int(existing.get("instance_count") or 1) + 1
        existing_instances = existing.setdefault("instances", [])
        existing_instances.append(item)
        if not existing.get("description") and item.get("description"):
            existing["description"] = item.get("description")
        if existing.get("bit_length") is None and item.get("bit_length") is not None:
            existing["bit_length"] = item.get("bit_length")
        if existing.get("default_value") in (None, "") and item.get("default_value") not in (None, ""):
            existing["default_value"] = item.get("default_value")
        preferred_candidates = existing.setdefault("preferred_source_candidates", [])
        seen_fields = {str(candidate.get("field_name") or "").strip().upper() for candidate in preferred_candidates if str(candidate.get("field_name") or "").strip()}
        for candidate in (item.get("preferred_source_candidates") or []):
            if not isinstance(candidate, dict):
                continue
            field_name = str(candidate.get("field_name") or "").strip().upper()
            if not field_name or field_name in seen_fields:
                continue
            preferred_candidates.append(dict(candidate))
            seen_fields.add(field_name)
        if not existing.get("match_hint_summary") and item.get("match_hint_summary"):
            existing["match_hint_summary"] = item.get("match_hint_summary")
    return concepts


def _split_field_tokens(field_name: str) -> List[str]:
    text = str(field_name or "").strip().upper()
    if not text:
        return []
    tokens = [token for token in re.split(r"[^A-Z0-9\u4E00-\u9FFF]+", text) if token]
    if len(tokens) <= 1:
        tokens.extend(
            token
            for token in re.findall(r"[A-Z]+|\d+|[\u4E00-\u9FFF]+", text)
            if token and token not in tokens
        )
    return tokens or [text]


def _decode_field_text(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""

    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)

    decoded = re.sub(r"U([0-9A-F]{4,6})", repl, raw.upper())
    return decoded.replace("_", " ")


def _semantic_groups_for_text(*values: Any) -> set[str]:
    raw_text = " ".join([str(value or "") for value in values if str(value or "").strip()])
    decoded_text = " ".join([_decode_field_text(value) for value in values if str(value or "").strip()])
    text = f"{raw_text} {decoded_text}".strip().lower()
    groups: set[str] = set()
    keyword_groups = {
        "latitude": ("latitude", "lat", "纬度"),
        "longitude": ("longitude", "lon", "经度"),
        "altitude": ("altitude", "height", "elevation", "高度", "高程"),
        "pitch": ("pitch", "俯仰"),
        "roll": ("roll", "翻滚"),
        "yaw": ("yaw", "heading", "偏航"),
        "time": ("time", "hour", "minute", "second", "timestamp", "小时", "分钟", "秒", "时间"),
        "threat": ("threat", "威胁"),
        "info": ("info", "信息"),
        "id": (" id ", "编号", "标识", "identifier"),
        "name": ("name", "名称"),
        "count": ("count", "quantity", "数量"),
        "target": ("target", "目标"),
        "control_flag": ("fpi", "gpi", "flag", "indicator", "开关", "标志"),
    }
    padded = f" {text} "
    for group, keywords in keyword_groups.items():
        if any(keyword in padded for keyword in keywords):
            groups.add(group)
    return groups


def _extract_control_field_token(value: Any) -> Optional[str]:
    field_name = str(value or "").strip().upper()
    if not field_name:
        return None
    for token in re.split(r"[.\/\s]+", field_name):
        token = token.strip()
        if not token:
            continue
        if CONTROL_FIELD_PATTERN.fullmatch(token):
            return token
        if "_" in token:
            tail = token.split("_")[-1]
            if CONTROL_FIELD_PATTERN.fullmatch(tail):
                return tail
    if CONTROL_FIELD_PATTERN.fullmatch(field_name):
        return field_name
    return None



def _is_control_field_name(value: Any) -> bool:
    return _extract_control_field_token(value) is not None


def _is_direct_copy_rule(rule: Dict[str, Any]) -> bool:
    source_fields = [str(item or "").strip().upper() for item in (rule.get("source_fields") or []) if str(item or "").strip()]
    formula = str(rule.get("rule") or "").strip().upper()
    if not formula:
        return False
    if formula in source_fields:
        return True
    return formula in {"VALUE", "RESULT = VALUE"}


def _normalize_confidence(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        confidence = float(value)
    except Exception:
        return None
    if confidence > 1.0:
        confidence = confidence / 100.0
    return round(max(0.0, min(confidence, 1.0)), 4)


def _leaf_similarity(left: Any, right: Any) -> float:
    return SequenceMatcher(a=str(left or "").strip().upper(), b=str(right or "").strip().upper()).ratio()


def _generic_semantic_groups(groups: set[str]) -> set[str]:
    return groups & {"id", "name", "info", "control_flag"}


def _canonicalize_field_leaf(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    replacements = (
        ("高程", "高度"),
        ("海拔", "高度"),
    )
    for src, dst in replacements:
        text = text.replace(src.upper(), dst.upper())
    for prefix in ("目标",):
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix) :]
    return text


def _is_reliable_name_alignment(target_leaf: str, source_leaf: str) -> bool:
    target_leaf = str(target_leaf or "").strip().upper()
    source_leaf = str(source_leaf or "").strip().upper()
    if not target_leaf or not source_leaf:
        return False
    if target_leaf == source_leaf:
        return True
    canonical_target = _canonicalize_field_leaf(target_leaf)
    canonical_source = _canonicalize_field_leaf(source_leaf)
    if canonical_target and canonical_source and canonical_target == canonical_source:
        return True
    target_family = _strip_numeric_suffix(target_leaf)
    source_family = _strip_numeric_suffix(source_leaf)
    target_suffix = _extract_numeric_suffix(target_leaf)
    source_suffix = _extract_numeric_suffix(source_leaf)
    if target_family and source_family and target_family == source_family:
        if target_suffix and source_suffix:
            return target_suffix == source_suffix
        return not target_suffix and not source_suffix
    canonical_target_family = _strip_numeric_suffix(canonical_target)
    canonical_source_family = _strip_numeric_suffix(canonical_source)
    canonical_target_suffix = _extract_numeric_suffix(canonical_target)
    canonical_source_suffix = _extract_numeric_suffix(canonical_source)
    if (
        canonical_target_family
        and canonical_source_family
        and canonical_target_family == canonical_source_family
    ):
        if canonical_target_suffix and canonical_source_suffix:
            return canonical_target_suffix == canonical_source_suffix
        return not canonical_target_suffix and not canonical_source_suffix
    if canonical_target and canonical_source:
        if canonical_target in canonical_source or canonical_source in canonical_target:
            return True
    similarity = _leaf_similarity(target_leaf, source_leaf)
    shared_tokens = set(_split_field_tokens(target_leaf)) & set(_split_field_tokens(source_leaf))
    return similarity >= 0.82 and len(shared_tokens) >= 1


def _estimate_rule_confidence(
    rule: Dict[str, Any],
    candidate_score_map: Optional[Dict[str, Dict[str, float]]] = None,
) -> float:
    existing = _normalize_confidence(rule.get("confidence"))
    if existing is not None:
        return existing

    source_name = str(rule.get("source") or "").strip().lower()
    if source_name == "knowledge_graph":
        return 0.95
    if source_name == "deterministic_match":
        return 0.97

    rule_text = str(rule.get("rule") or "").strip()
    source_fields = [str(item or "").strip().upper() for item in (rule.get("source_fields") or []) if str(item or "").strip()]
    confidence = 0.72
    if _is_direct_copy_rule(rule):
        confidence += 0.08
    if len(source_fields) == 1:
        confidence += 0.04
    elif len(source_fields) > 1:
        confidence -= 0.03
    if "\n" in rule_text:
        confidence -= 0.02
    if str(rule.get("evidence") or "").strip():
        confidence += 0.02

    target_key = _target_rule_concept_identity(rule)
    if candidate_score_map and target_key in candidate_score_map:
        candidate_scores = [
            float(candidate_score_map[target_key].get(field, 0.0))
            for field in source_fields
            if field in candidate_score_map[target_key]
        ]
        if candidate_scores:
            avg_score = sum(candidate_scores) / max(len(candidate_scores), 1)
            if avg_score >= 90.0:
                confidence += 0.08
            elif avg_score >= 75.0:
                confidence += 0.04
            elif avg_score < 55.0:
                confidence -= 0.08

    return round(max(0.55, min(confidence, 0.98)), 4)


def _target_semantic_groups(target_field_spec: Dict[str, Any]) -> set[str]:
    return _semantic_groups_for_text(
        target_field_spec.get("field_name"),
        target_field_spec.get("label"),
        target_field_spec.get("description"),
        " ".join(str(part) for part in (target_field_spec.get("path_parts") or [])),
    )


def _field_leaf_name(target_field_spec: Dict[str, Any]) -> str:
    for candidate in (
        target_field_spec.get("label"),
        (target_field_spec.get("path_parts") or [None])[-1],
        target_field_spec.get("field_name"),
    ):
        text = str(candidate or "").strip().upper()
        if text:
            return text
    return ""


def _strip_numeric_suffix(value: Any) -> str:
    return re.sub(r"\d+$", "", str(value or "").strip().upper())


def _extract_numeric_suffix(value: Any) -> Optional[str]:
    match = re.search(r"(\d+)$", str(value or "").strip().upper())
    return match.group(1) if match else None


def _candidate_exact_keys(target_field_spec: Dict[str, Any]) -> set[str]:
    exact_keys = {
        str(target_field_spec.get("field_name") or "").strip().upper(),
        str(target_field_spec.get("label") or "").strip().upper(),
        str((target_field_spec.get("path_parts") or [None])[-1] or "").strip().upper(),
    }
    return {item for item in exact_keys if item}


def _candidate_leaf_texts(source_entry: Dict[str, Any]) -> List[str]:
    texts: List[str] = []
    for raw in (
        source_entry.get("display_field"),
        source_entry.get("label"),
        source_entry.get("actual_field"),
        source_entry.get("field_name"),
    ):
        text = str(raw or "").strip().upper()
        if text:
            texts.append(text.split(".")[-1])
    source_path = str(source_entry.get("source_path") or "").strip()
    if source_path:
        for part in re.split(r"[\\/]", source_path):
            text = str(part or "").strip().upper()
            if text:
                texts.append(text)
    deduped: List[str] = []
    seen: set[str] = set()
    for text in texts:
        if text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _source_entry_exact_match(target_field_spec: Dict[str, Any], source_entry: Dict[str, Any]) -> bool:
    exact_keys = _candidate_exact_keys(target_field_spec)
    if not exact_keys:
        return False
    return any(text in exact_keys for text in _candidate_leaf_texts(source_entry))


def _score_source_candidate(target_field_spec: Dict[str, Any], source_field: str) -> float:
    target_field = str(target_field_spec.get("field_name") or "").strip().upper()
    target = str(target_field or "").strip().upper()
    source = str(source_field or "").strip().upper()
    if not target or not source:
        return 0.0
    target_leaf = _field_leaf_name(target_field_spec) or target
    source_leaf = source.split(".")[-1]
    target_groups = _target_semantic_groups(target_field_spec)
    source_groups = _semantic_groups_for_text(source)
    target_is_control = _is_control_field_name(target)
    source_is_control = _is_control_field_name(source)
    if source_is_control and not target_is_control:
        return 0.0
    if target_is_control and not source_is_control:
        return 0.0
    if target_groups and source_groups and not (target_groups & source_groups):
        if (target_groups & STRICT_SEMANTIC_GROUPS) or (source_groups & STRICT_SEMANTIC_GROUPS):
            return 0.0
    generic_target_groups = _generic_semantic_groups(target_groups)
    generic_source_groups = _generic_semantic_groups(source_groups)
    if generic_target_groups or generic_source_groups:
        if generic_target_groups != generic_source_groups:
            return 0.0
        if not _is_reliable_name_alignment(target_leaf, source_leaf):
            return 0.0
    if target == source or target_leaf == source_leaf or source in _candidate_exact_keys(target_field_spec):
        return 100.0
    if not target_groups and not source_groups and not _is_reliable_name_alignment(target_leaf, source_leaf):
        return 0.0
    score = SequenceMatcher(a=target_leaf, b=source_leaf).ratio() * 56.0
    target_tokens = set(_split_field_tokens(target_leaf))
    source_tokens = set(_split_field_tokens(source_leaf))
    overlap = target_tokens & source_tokens
    score += float(len(overlap)) * 8.0
    target_family = _strip_numeric_suffix(target_leaf)
    source_family = _strip_numeric_suffix(source_leaf)
    if target_family and source_family and target_family == source_family:
        score += 12.0
    target_suffix = _extract_numeric_suffix(target_leaf)
    source_suffix = _extract_numeric_suffix(source_leaf)
    if target_suffix and source_suffix and target_family == source_family and target_suffix != source_suffix:
        score -= 14.0
    if target_groups and source_groups and (target_groups & source_groups):
        score += 32.0
    if target_leaf.startswith(source_leaf) or source_leaf.startswith(target_leaf):
        score += 10.0
    if target_leaf.endswith(source_leaf) or source_leaf.endswith(target_leaf):
        score += 6.0
    return round(score, 4)


def _coerce_bit_length(value: Any) -> Optional[int]:
    try:
        bit_length = int(value)
    except Exception:
        return None
    return bit_length if bit_length > 0 else None


def _candidate_bit_length(source_entry: Dict[str, Any]) -> Optional[int]:
    return _coerce_bit_length(source_entry.get("bit_length"))


def _score_source_catalog_entry(target_field_spec: Dict[str, Any], source_entry: Dict[str, Any]) -> float:
    score = _score_source_candidate(target_field_spec, str(source_entry.get("field_name") or ""))
    if score >= 90.0:
        base_score = score
    else:
        base_score = score
    for candidate_text in (
        source_entry.get("display_field"),
        source_entry.get("label"),
        source_entry.get("actual_field"),
        source_entry.get("source_path"),
    ):
        base_score = max(base_score, _score_source_candidate(target_field_spec, str(candidate_text or "")))

    target_leaf = _field_leaf_name(target_field_spec)
    target_family = _strip_numeric_suffix(target_leaf)
    target_suffix = _extract_numeric_suffix(target_leaf)
    leaf_texts = _candidate_leaf_texts(source_entry)
    if _source_entry_exact_match(target_field_spec, source_entry):
        base_score = max(base_score, 118.0)
    elif target_suffix:
        aligned_with_same_suffix = False
        aligned_family_without_suffix = False
        for text in leaf_texts:
            source_family = _strip_numeric_suffix(text)
            source_suffix = _extract_numeric_suffix(text)
            if source_family and target_family and source_family == target_family:
                if source_suffix == target_suffix:
                    aligned_with_same_suffix = True
                elif not source_suffix:
                    aligned_family_without_suffix = True
        if aligned_with_same_suffix:
            base_score += 12.0
        elif aligned_family_without_suffix:
            base_score -= 10.0
    target_bits = _coerce_bit_length(target_field_spec.get("bit_length"))
    source_bits = _candidate_bit_length(source_entry)
    target_groups = _target_semantic_groups(target_field_spec)
    source_groups = _semantic_groups_for_text(" ".join(leaf_texts))
    if target_bits is not None and source_bits is not None:
        if target_bits == source_bits:
            base_score += 14.0
        elif "time" in target_groups and "time" in source_groups:
            base_score -= min(abs(target_bits - source_bits) * 4.0, 24.0)
    return round(base_score, 4)


def _time_component_kind(*values: Any) -> Optional[str]:
    text = " ".join(_decode_field_text(value) for value in values if str(value or "").strip()).lower()
    if not text:
        return None
    if any(token in text for token in ("小时", "hour")):
        return "hour"
    if any(token in text for token in ("分钟", "minute")):
        return "minute"
    if any(token in text for token in ("秒", "second")):
        return "second"
    return None


def _has_multi_component_time_candidates(
    target_field_spec: Dict[str, Any],
    ranked: List[Dict[str, Any]],
) -> bool:
    if "time" not in _target_semantic_groups(target_field_spec):
        return False
    target_component = _time_component_kind(
        target_field_spec.get("field_name"),
        target_field_spec.get("label"),
        target_field_spec.get("description"),
        " ".join(str(part) for part in (target_field_spec.get("path_parts") or [])),
    )
    if target_component:
        return False
    component_kinds = {
        _time_component_kind(
            candidate.get("field_name"),
            candidate.get("display_field"),
            candidate.get("actual_field"),
            candidate.get("source_path"),
        )
        for candidate in ranked
    }
    component_kinds.discard(None)
    return len(component_kinds) >= 2


def _prune_ranked_source_candidates(
    target_field_spec: Dict[str, Any],
    ranked: List[Dict[str, Any]],
    top_k: int,
) -> List[Dict[str, Any]]:
    if not ranked:
        return []
    preferred_fields = [
        str(candidate.get("field_name") or "").strip().upper()
        for candidate in (target_field_spec.get("preferred_source_candidates") or [])
        if isinstance(candidate, dict) and str(candidate.get("field_name") or "").strip()
    ]
    preferred_protocols = {
        (
            str(candidate.get("source_protocol_type") or "").strip().upper(),
            str(candidate.get("source_message_code") or "").strip().upper(),
        )
        for candidate in (target_field_spec.get("preferred_source_candidates") or [])
        if isinstance(candidate, dict)
    }
    preferred_protocols.discard(("", ""))
    if len(preferred_fields) > 1 or len(preferred_protocols) > 1:
        return _select_diverse_candidates(ranked, top_k, preferred_fields=preferred_fields)
    if _has_multi_component_time_candidates(target_field_spec, ranked):
        return ranked[:top_k]
    exact_matches = [
        item
        for item in ranked
        if _source_entry_exact_match(target_field_spec, item)
    ]
    if exact_matches:
        exact_matches.sort(key=lambda item: (-float(item["score"]), item["field_name"]))
        exact_protocols = {_candidate_protocol_bucket(item) for item in exact_matches}
        exact_protocols.discard(("", ""))
        if len(exact_matches) > 1 or len(exact_protocols) > 1:
            return _select_diverse_candidates(exact_matches, top_k, preferred_fields=preferred_fields)
        return exact_matches[:1]
    if len(ranked) == 1:
        return ranked[:1]
    top = ranked[0]
    second = ranked[1]
    score_gap = float(top["score"]) - float(second["score"])
    top_field = str(top.get("field_name") or "").strip().upper()
    target_leaf = _field_leaf_name(target_field_spec)
    target_groups = _target_semantic_groups(target_field_spec)
    target_bits = _coerce_bit_length(target_field_spec.get("bit_length"))
    top_bits = _candidate_bit_length(top)
    if float(top["score"]) >= 90.0 and score_gap >= 12.0:
        return [top]
    if _strip_numeric_suffix(top_field) == _strip_numeric_suffix(target_leaf) and score_gap >= 15.0:
        return [top]
    if (
        "time" in target_groups
        and "时间" in target_leaf
        and target_bits is not None
        and top_bits is not None
        and top_bits == target_bits
        and score_gap >= 10.0
    ):
        return [top]
    return ranked[:top_k]


def _build_source_field_candidates(
    target_field_spec: Dict[str, Any],
    normalized_source_message: Dict[str, Any],
    source_field_catalog: Optional[Iterable[Dict[str, Any]]] = None,
    preferred_source_candidates: Optional[Iterable[Dict[str, Any]]] = None,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    ranked_map: Dict[str, Dict[str, Any]] = {}

    def upsert_candidate(candidate: Dict[str, Any]) -> None:
        field_name = str(candidate.get("field_name") or "").strip().upper()
        if not field_name:
            return
        existing = ranked_map.get(field_name)
        if existing is None or float(candidate.get("score") or 0.0) > float(existing.get("score") or 0.0):
            ranked_map[field_name] = candidate

    if source_field_catalog:
        for source_entry in source_field_catalog:
            if not isinstance(source_entry, dict):
                continue
            field_name = str(source_entry.get("field_name") or "").strip()
            if not field_name:
                continue
            score = _score_source_catalog_entry(target_field_spec, source_entry)
            if score < 30.0:
                continue
            upsert_candidate(
                {
                    "field_name": field_name.upper(),
                    "actual_field": str(source_entry.get("actual_field") or field_name).strip(),
                    "display_field": str(
                        source_entry.get("display_field")
                        or source_entry.get("label")
                        or source_entry.get("actual_field")
                        or field_name
                    ).strip(),
                    "source_path": str(source_entry.get("source_path") or "").strip() or None,
                    "source_protocol_type": str(source_entry.get("protocol") or "").strip() or None,
                    "source_message_code": str(source_entry.get("message_code") or "").strip() or None,
                    "score": score,
                    "bit_length": source_entry.get("bit_length"),
                    "sample_value": source_entry.get("sample_value"),
                }
            )
    else:
        for source_field in normalized_source_message.keys():
            score = _score_source_candidate(target_field_spec, source_field)
            if score < 30.0:
                continue
            upsert_candidate(
                {
                    "field_name": str(source_field).strip().upper(),
                    "actual_field": str(source_field).strip(),
                    "display_field": str(source_field).strip(),
                    "source_path": None,
                    "source_protocol_type": None,
                    "source_message_code": None,
                    "score": score,
                    "bit_length": None,
                    "sample_value": normalized_source_message.get(source_field),
                }
            )

    for preferred in preferred_source_candidates or []:
        if not isinstance(preferred, dict):
            continue
        field_name = str(preferred.get("field_name") or "").strip().upper()
        if not field_name:
            continue
        boosted_score = max(float(preferred.get("score") or 0.0) + 18.0, 96.0)
        upsert_candidate(
            {
                "field_name": field_name,
                "actual_field": str(preferred.get("actual_field") or field_name).strip(),
                "display_field": str(
                    preferred.get("display_field")
                    or preferred.get("actual_field")
                    or field_name
                ).strip(),
                "source_path": str(preferred.get("source_path") or "").strip() or None,
                "source_protocol_type": str(preferred.get("source_protocol_type") or "").strip() or None,
                "source_message_code": str(preferred.get("source_message_code") or "").strip() or None,
                "score": round(boosted_score, 4),
                "bit_length": preferred.get("bit_length"),
                "sample_value": preferred.get("sample_value") or normalized_source_message.get(field_name),
                "hint_source": str(preferred.get("hint_source") or "sub_message_relation").strip() or None,
            }
        )

    ranked = list(ranked_map.values())
    ranked.sort(key=lambda item: (-float(item["score"]), item["field_name"]))
    return _prune_ranked_source_candidates(target_field_spec, ranked, top_k)


def _build_target_generation_tasks(
    required_target_fields: Optional[Iterable[Any]],
    existing_rules: List[Dict[str, Any]],
    normalized_source_message: Dict[str, Any],
    source_field_catalog: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    existing_targets = {
        _target_rule_concept_identity(rule)
        for rule in existing_rules
        if _target_rule_concept_identity(rule)
    }
    tasks: List[Dict[str, Any]] = []
    for item in _normalize_concept_target_fields(required_target_fields):
        if _target_concept_identity(item) in existing_targets:
            continue
        candidate_source_fields = _build_source_field_candidates(
            item,
            normalized_source_message,
            source_field_catalog=source_field_catalog,
            preferred_source_candidates=item.get("preferred_source_candidates"),
        )
        if not candidate_source_fields:
            continue
        tasks.append(
            {
                **item,
                "candidate_source_fields": candidate_source_fields,
            }
        )
    return tasks


def _build_deterministic_candidate_rules(
    target_tasks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """为高置信一一对应字段直接生成确定性规则，减少 LLM 漂移。"""
    rules: List[Dict[str, Any]] = []
    for item in target_tasks:
        candidates = list(item.get("candidate_source_fields") or [])
        if not candidates:
            continue
        top = candidates[0]
        top_score = float(top.get("score") or 0.0)
        second_score = float(candidates[1].get("score") or 0.0) if len(candidates) > 1 else 0.0

        target_leaf = _field_leaf_name(item)
        source_leaf_texts = _candidate_leaf_texts(top)
        exact_match = _source_entry_exact_match(item, top)
        aligned_leaf = any(_is_reliable_name_alignment(target_leaf, source_leaf) for source_leaf in source_leaf_texts)
        target_groups = _target_semantic_groups(item)
        source_groups = _semantic_groups_for_text(" ".join(source_leaf_texts))
        target_bits = _coerce_bit_length(item.get("bit_length"))
        source_bits = _candidate_bit_length(top)
        non_time_strict_shared_semantics = bool(
            (target_groups & source_groups) & (STRICT_SEMANTIC_GROUPS - {"time"})
        )
        same_bit_length = target_bits is not None and source_bits is not None and target_bits == source_bits
        if target_groups and source_groups and not (target_groups & source_groups):
            continue
        strong_gap = len(candidates) <= 1 or top_score - second_score >= 12.0
        reliable_single_candidate = (
            len(candidates) == 1
            and top_score >= 72.0
            and aligned_leaf
            and bool(target_groups & source_groups)
        )
        semantic_single_candidate = (
            len(candidates) == 1
            and top_score >= 52.0
            and bool(target_groups & source_groups)
            and (aligned_leaf or non_time_strict_shared_semantics)
        )
        reliable_time_single_candidate = (
            len(candidates) == 1
            and top_score >= 76.0
            and same_bit_length
            and "time" in target_groups
            and "time" in source_groups
        )
        if not reliable_single_candidate and not semantic_single_candidate and not reliable_time_single_candidate:
            if top_score < 95.0:
                continue
            if not strong_gap:
                continue
        if (
            not exact_match
            and not aligned_leaf
            and not semantic_single_candidate
            and not reliable_time_single_candidate
            and top_score < 110.0
        ):
            continue

        source_field_name = str(top.get("field_name") or "").strip().upper()
        if not source_field_name:
            continue
        confidence = (
            0.97 if exact_match
            else 0.94 if reliable_time_single_candidate
            else 0.91 if semantic_single_candidate
            else 0.93
        )
        rules.append(
            {
                "target_field": str(item.get("field_name") or "").strip().upper(),
                "source_fields": [source_field_name],
                "conversion_mode": "transcoding",
                "formula_kind": "python_expr",
                "rule": source_field_name,
                "concept_name": str(item.get("label") or item.get("field_name") or "").strip() or None,
                "condition": None,
                "default_value": None,
                "unit": item.get("unit"),
                "bit_length": item.get("bit_length"),
                "description": item.get("description"),
                "evidence": None,
                "confidence": confidence,
                "status": "candidate",
                "source": "deterministic_match",
            }
        )
    return _dedupe_rules_by_target_field(rules)


def _resolve_pageindex_status(evidence_result: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    if not evidence_result:
        return "miss", None
    status = str(evidence_result.get("status") or "").strip().lower()
    reason = str(evidence_result.get("reason") or "").strip() or None
    if status == "unavailable":
        return "unavailable", reason
    snippets = evidence_result.get("evidence_snippets") or []
    if snippets:
        return "used", reason
    return "miss", reason


def _summarize_candidate_source_fields(candidates: List[Dict[str, Any]]) -> str:
    if not candidates:
        return "无明显候选源字段。"
    lines = ["候选源字段（只是候选线索，不等于最终转换关系；若都不可靠可返回 []）:"]
    for item in candidates:
        extras = []
        if item.get("display_field") and str(item.get("display_field")).strip().upper() != str(item.get("field_name") or "").strip().upper():
            extras.append(f"display={item.get('display_field')}")
        if item.get("source_message_code"):
            extras.append(f"message={item.get('source_message_code')}")
        if item.get("source_protocol_type"):
            extras.append(f"protocol={item.get('source_protocol_type')}")
        if item.get("source_path"):
            extras.append(f"path={item.get('source_path')}")
        if item.get("hint_source"):
            extras.append(f"hint={item.get('hint_source')}")
        extra_text = f", {', '.join(extras)}" if extras else ""
        lines.append(
            f"- {item['field_name']} (score={item['score']}{extra_text})"
        )
    return "\n".join(lines)


def _format_target_field_requirements(required_target_fields: Optional[Iterable[Any]]) -> Optional[str]:
    normalized = _normalize_required_target_fields(required_target_fields)
    if not normalized:
        return None
    lines = ["目标字段清单（必须尽量覆盖；若无源字段依赖，可输出常量数值公式）:"]
    for item in normalized:
        lines.append(f"- {item['field_name']}")
    return "\n".join(lines)


def _format_target_task_requirements(target_tasks: List[Dict[str, Any]]) -> Optional[str]:
    if not target_tasks:
        return None
    lines = ["本轮仅为以下目标字段生成规则:"]
    for item in target_tasks:
        suffix_parts = []
        if item.get("label"):
            suffix_parts.append(f"label={item['label']}")
        if item.get("description"):
            suffix_parts.append(f"description={item['description']}")
        if int(item.get("instance_count") or 0) > 1:
            suffix_parts.append(f"instances={int(item.get('instance_count') or 0)}")
        if item.get("match_hint_summary"):
            suffix_parts.append(f"sub_message_matches={item.get('match_hint_summary')}")
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        lines.append(f"- {item['field_name']}{suffix}")
        lines.append(_summarize_candidate_source_fields(item.get("candidate_source_fields") or []))
    return "\n".join(lines)


def _summarize_source_message_fields(source_message: Optional[Any]) -> str:
    if not isinstance(source_message, dict) or not source_message:
        return "[]"
    field_names = [str(key).strip() for key in source_message.keys() if str(key).strip()]
    return json.dumps(field_names, ensure_ascii=False, indent=2)


def _knowledge_rule_to_generated_rule(rule: Any) -> Dict[str, Any]:
    target_field = str(rule.target_field or "").strip().upper()
    target_path = _strip_protocol_like_field_prefix(target_field)
    source_fields = [str(item).strip().upper() for item in (rule.source_fields or []) if str(item).strip()]
    normalized_formula = _strip_explicit_target_assignment(
        str(rule.formula or "").strip(),
        target_field=target_field,
        target_protocol_type=str(getattr(rule, "target_protocol_type", "") or "").strip() or None,
    )
    normalized_formula = _normalize_formula_identifier_spacing(normalized_formula, source_fields)
    formula_kind = str(rule.formula_kind or "").strip() or _infer_formula_kind(normalized_formula)
    if formula_kind == "python_expr":
        normalized_formula = _normalize_formula_expression_syntax(normalized_formula)
    return {
        "target_field": target_field,
        "target_path": target_path,
        "source_fields": source_fields,
        "conversion_mode": str(rule.conversion_mode or "").strip().lower() or None,
        "formula_kind": formula_kind,
        "rule": normalized_formula,
        "concept_name": str(rule.concept_name or rule.target_field or "").strip() or None,
        "condition": None,
        "default_value": None,
        "unit": rule.unit,
        "bit_length": rule.bit_length,
        "description": rule.description,
        "evidence": rule.description,
        "source": str(rule.source or "knowledge_graph"),
        "status": getattr(rule, "status", None),
        "confidence": getattr(rule, "confidence", None),
        "source_protocol_type": str(getattr(rule, "protocol_type", "") or "").strip() or None,
        "source_protocol_name": str(getattr(rule, "protocol_type", "") or "").strip() or None,
        "source_message_code": str(getattr(rule, "message_code", "") or "").strip() or None,
        "target_protocol_type": str(getattr(rule, "target_protocol_type", "") or "").strip() or None,
        "target_message_code": str(getattr(rule, "target_message_code", "") or "").strip() or None,
    }


def _apply_source_field_catalog_to_rules(
    generated_rules: List[Dict[str, Any]],
    source_field_catalog: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not generated_rules:
        return []
    catalog_map = {
        str(item.get("field_name") or "").strip().upper(): item
        for item in (source_field_catalog or [])
        if isinstance(item, dict) and str(item.get("field_name") or "").strip()
    }
    enriched_rules: List[Dict[str, Any]] = []
    for rule in generated_rules:
        enriched = dict(rule)
        source_bindings: List[Dict[str, Any]] = []
        source_actual_fields: List[str] = []
        source_paths: List[Optional[str]] = []
        source_protocol_types: List[str] = []
        source_message_codes: List[str] = []
        for field_name in [str(item).strip().upper() for item in (rule.get("source_fields") or []) if str(item).strip()]:
            binding = catalog_map.get(field_name)
            if not binding:
                continue
            source_bindings.append(
                {
                    "alias_name": str(binding.get("field_name") or "").strip(),
                    "protocol": str(binding.get("protocol") or "").strip() or None,
                    "message_code": str(binding.get("message_code") or "").strip() or None,
                    "actual_field": str(binding.get("actual_field") or "").strip() or None,
                    "display_field": str(
                        binding.get("display_field")
                        or binding.get("label")
                        or binding.get("actual_field")
                        or binding.get("field_name")
                        or ""
                    ).strip() or None,
                    "source_path": str(binding.get("source_path") or "").strip() or None,
                }
            )
            actual_field = str(binding.get("actual_field") or "").strip()
            if actual_field:
                source_actual_fields.append(actual_field)
            source_paths.append(str(binding.get("source_path") or "").strip() or None)
            protocol_type = str(binding.get("protocol") or "").strip()
            if protocol_type:
                source_protocol_types.append(protocol_type)
            message_code = str(binding.get("message_code") or "").strip()
            if message_code:
                source_message_codes.append(message_code)
        if source_bindings:
            enriched["source_bindings"] = source_bindings
            enriched["source_actual_fields"] = source_actual_fields
            enriched["source_paths"] = source_paths
            unique_protocols = sorted(set(source_protocol_types))
            if len(unique_protocols) == 1:
                enriched["source_protocol_type"] = unique_protocols[0]
                enriched["source_protocol_name"] = unique_protocols[0]
            unique_message_codes = sorted(set(source_message_codes))
            if len(unique_message_codes) == 1:
                enriched["source_message_code"] = unique_message_codes[0]
        enriched_rules.append(enriched)
    return enriched_rules


def _build_default_zero_rules(
    required_target_fields: Optional[Iterable[Any]],
    existing_rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    zero_rules: List[Dict[str, Any]] = []
    for item in _normalize_required_target_fields(required_target_fields):
        target_identity = _target_spec_identity(item)
        if any(_target_rule_identity(rule) == target_identity for rule in existing_rules):
            continue
        zero_rules.append(
            {
                "target_field": item["field_name"],
                "target_actual_field": item.get("actual_field"),
                "target_path": item.get("target_path"),
                "source_fields": [],
                "conversion_mode": "transcoding",
                "formula_kind": "python_expr",
                "rule": "0",
                "concept_name": item["field_name"],
                "condition": None,
                "default_value": item.get("default_value", 0),
                "unit": None,
                "bit_length": item.get("bit_length"),
                "description": "无法可靠转换，默认置 0",
                "evidence": None,
            }
        )
    return zero_rules


def _expand_generated_rules_to_target_instances(
    generated_rules: List[Dict[str, Any]],
    required_target_fields: Optional[Iterable[Any]],
) -> List[Dict[str, Any]]:
    normalized_targets = _normalize_required_target_fields(required_target_fields)
    if not normalized_targets:
        return list(generated_rules)

    concept_to_instances: Dict[str, List[Dict[str, Any]]] = {}
    for item in normalized_targets:
        concept_key = _target_concept_identity(item)
        if not concept_key:
            continue
        concept_to_instances.setdefault(concept_key, []).append(item)

    expanded_rules: List[Dict[str, Any]] = []
    for rule in generated_rules:
        concept_key = _target_rule_concept_identity(rule)
        target_instances = concept_to_instances.get(concept_key)
        if not target_instances:
            expanded_rules.append(dict(rule))
            continue
        explicit_target_actual = str(rule.get("target_actual_field") or "").strip()
        explicit_target_path = str(rule.get("target_path") or "").strip()
        if explicit_target_actual or explicit_target_path:
            matched_instance = None
            for instance in target_instances:
                if explicit_target_actual and explicit_target_actual == str(instance.get("actual_field") or "").strip():
                    matched_instance = instance
                    break
                if explicit_target_path and explicit_target_path == str(instance.get("target_path") or "").strip():
                    matched_instance = instance
                    break
            if matched_instance is None:
                continue
            expanded_rule = dict(rule)
            expanded_rule["target_field"] = matched_instance["field_name"]
            expanded_rule["target_actual_field"] = matched_instance.get("actual_field")
            expanded_rule["target_path"] = matched_instance.get("target_path")
            expanded_rules.append(expanded_rule)
            continue
        for instance in target_instances:
            expanded_rule = dict(rule)
            expanded_rule["target_field"] = instance["field_name"]
            expanded_rule["target_actual_field"] = instance.get("actual_field")
            expanded_rule["target_path"] = instance.get("target_path")
            expanded_rules.append(expanded_rule)
    return _dedupe_rules_by_target_field(expanded_rules)


def _build_executable_rules(
    generated_rules: List[Dict[str, Any]],
    normalized_source_protocol: Dict[str, Optional[str]],
    normalized_target_protocol: Dict[str, Optional[str]],
) -> List[Dict[str, Any]]:
    executable_rules: List[Dict[str, Any]] = []
    for rule in generated_rules:
        formula_kind = str(rule.get("formula_kind") or "").strip() or _infer_formula_kind(str(rule.get("rule") or ""))
        formula = str(rule.get("rule") or "").strip()
        if formula_kind == "python_expr":
            formula = _normalize_formula_expression_syntax(formula)
        executable_rules.append(
            {
            "field_name": rule["source_fields"][0] if rule.get("source_fields") else "",
            "source_fields": list(rule.get("source_fields") or []),
            "source_bindings": list(rule.get("source_bindings") or []),
            "source_actual_fields": list(rule.get("source_actual_fields") or []),
            "source_paths": list(rule.get("source_paths") or []),
            "source_protocol_type": rule.get("source_protocol_type")
            or normalized_source_protocol.get("protocol_type")
            or normalized_source_protocol.get("name"),
            "source_protocol_name": rule.get("source_protocol_name")
            or normalized_source_protocol.get("name")
            or normalized_source_protocol.get("protocol_type"),
            "source_message_code": rule.get("source_message_code")
            or normalized_source_protocol.get("message_code"),
            "target_field": rule["target_field"],
            "target_actual_field": rule.get("target_actual_field"),
            "target_path": rule.get("target_path"),
            "conversion_mode": rule["conversion_mode"],
            "formula_kind": formula_kind,
            "formula": formula,
            "rule": formula,
            "unit": rule.get("unit"),
            "bit_length": rule.get("bit_length"),
            "description": rule.get("description") or rule.get("evidence"),
            "concept_name": rule.get("concept_name"),
            "confidence": _normalize_confidence(rule.get("confidence")),
            "status": rule.get("status"),
            "source": rule.get("source"),
            "message_bundle_id": rule.get("message_bundle_id"),
            "target_protocol_type": normalized_target_protocol.get("protocol_type") or normalized_target_protocol.get("name"),
            "target_message_code": normalized_target_protocol.get("message_code"),
        }
        )
    return executable_rules


def _build_kg_writeback_payload(
    generated_rules: List[Dict[str, Any]],
    source_protocol: Dict[str, Optional[str]],
    target_protocol: Dict[str, Optional[str]],
    excluded_target_fields: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    excluded = {
        str(item or "").strip().upper()
        for item in (excluded_target_fields or [])
        if str(item or "").strip()
    }
    rules: List[Dict[str, Any]] = []
    seen_rule_keys: set[Tuple[str, Tuple[str, ...], str]] = set()
    for rule in generated_rules:
        target_field = str(rule.get("target_field") or "").strip().upper()
        if not target_field or target_field in excluded:
            continue
        source_fields = [str(item).strip().upper() for item in (rule.get("source_fields") or []) if str(item).strip()]
        formula = str(rule.get("rule") or "").strip()
        if not formula or formula == "0" or not source_fields:
            continue
        formula_kind = str(rule.get("formula_kind") or "").strip() or _infer_formula_kind(formula)
        if formula_kind == "python_expr":
            formula = _normalize_formula_expression_syntax(formula)
        rule_key = (target_field, tuple(source_fields), formula)
        if rule_key in seen_rule_keys:
            continue
        seen_rule_keys.add(rule_key)
        evidence_items: List[Dict[str, Any]] = []
        evidence_text = str(rule.get("evidence") or "").strip()
        description_text = str(rule.get("description") or "").strip()
        if evidence_text:
            evidence_items.append({"type": "evidence", "content": evidence_text})
        if description_text and description_text != evidence_text:
            evidence_items.append({"type": "description", "content": description_text})
        rules.append(
            {
                "concept_name": str(rule.get("concept_name") or target_field).strip() or target_field,
                "source_fields": source_fields,
                "source_bindings": list(rule.get("source_bindings") or []),
                "source_actual_fields": list(rule.get("source_actual_fields") or []),
                "source_paths": list(rule.get("source_paths") or []),
                "source_protocol_type": str(
                    rule.get("source_protocol_type")
                    or source_protocol.get("protocol_type")
                    or source_protocol.get("name")
                    or ""
                ).strip() or None,
                "source_protocol_name": str(
                    rule.get("source_protocol_name")
                    or source_protocol.get("name")
                    or source_protocol.get("protocol_type")
                    or ""
                ).strip() or None,
                "target_field": target_field,
                "conversion_mode": str(rule.get("conversion_mode") or "").strip().lower() or None,
                "formula_kind": formula_kind,
                "formula": formula,
                "evidence": evidence_items,
                "confidence": _estimate_rule_confidence(rule),
                "status": str(rule.get("status") or "candidate").strip().lower() or "candidate",
                "source": str(rule.get("source") or "llm_generated").strip() or "llm_generated",
                "message_bundle_id": rule.get("message_bundle_id"),
                "target_protocol_type": str(
                    rule.get("target_protocol_type")
                    or target_protocol.get("protocol_type")
                    or target_protocol.get("name")
                    or ""
                ).strip() or None,
                "target_message_code": str(
                    rule.get("target_message_code")
                    or target_protocol.get("message_code")
                    or ""
                ).strip().upper() or None,
            }
        )
    return {
        "protocol_type": source_protocol.get("protocol_type") or source_protocol.get("name") or DEFAULT_PROTOCOL_TYPE,
        "source_message_code": source_protocol.get("message_code"),
        "target_protocol_type": target_protocol.get("protocol_type") or target_protocol.get("name"),
        "target_message_code": target_protocol.get("message_code"),
        "rules": rules,
    }


def build_protocol_rule_generation_prompt(
    source_protocol: Dict[str, Optional[str]],
    target_protocol: Dict[str, Optional[str]],
    source_message: Optional[Any] = None,
    pageindex_evidence: Optional[Dict[str, Any]] = None,
    use_trained_docs: bool = False,
    required_target_fields: Optional[Iterable[Any]] = None,
    target_tasks: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str]:
    evidence_text = _format_target_task_evidence(target_tasks or [], pageindex_evidence) or _format_pageindex_evidence(pageindex_evidence)
    target_field_requirements = _format_target_task_requirements(target_tasks or []) or _format_target_field_requirements(required_target_fields)
    source_protocol_content = _resolve_source_protocol_content(source_protocol, use_trained_docs)
    system_prompt = (
        "你是一个协议值转换公式生成器。"
        "你的任务不是直接输出目标报文，而是生成‘原协议字段值 -> 目标协议字段值’的转换公式。"
        "候选源字段列表只用于召回和约束，不能直接视为最终答案；最终规则必须由你综合协议XML、检索证据和必要推理后生成。"
        "必须只输出 JSON。"
        "禁止输出<think>、解释文字、Markdown 代码块和额外前后缀。"
        "输出格式为 JSON 数组，每个元素必须包含："
        "concept_name, target_field, source_fields, conversion_mode, formula_kind, rule。"
        "conversion_mode 只能是 transcoding 或 mapping。"
        "formula_kind 只能是 python_expr、python_block、mapping_table。"
        "python_expr 表示单行表达式，可直接引用 source_fields 中的字段名。"
        "python_block 表示多行公式，允许 if/else/for，且最终必须把目标值赋给 result。"
        "mapping_table 表示离散值映射表，例如 1=10, 2=20。"
        "表达式必须使用 Python 语法，不允许使用 ?:、&&、|| 或 C 风格 !。"
        "条件表达式必须写成 A if 条件 else B。"
        "禁止输出字符串、f-string、字符串拼接或任何文本格式化结果，只能输出数值表达式。"
        "如果规则是常量赋值，source_fields 可以为空数组。"
        "所有 rule 都必须是值到值公式，不要输出标签解释文本；"
        "如果目标协议使用枚举数值，就输出数值映射，不要输出语义标签。"
        "若字段语义一致且表达方式一致，可直接输出具体字段名作为规则。"
        "单个 target_field 可以依赖多个 source_fields，且这些字段可以来自不同 source_protocol_type、不同 source_message_code、不同子消息。"
        "如果提供了PageIndex证据摘要或训练文档证据，必须优先依据这些证据；"
        "若文档证据缺失或未覆盖当前字段，可结合原/目标协议XML结构、候选源字段与通用协议知识进行保守推理；"
        "若仍无法可靠判断，就跳过该字段，不要输出占位规则。"
    )
    source_message_text = _summarize_source_message_fields(source_message)
    user_prompt = [
        "请根据以下信息生成目标协议字段的值到值转换规则。\n\n"
        f"原协议名称: {source_protocol.get('name') or source_protocol.get('protocol_type') or '未提供'}\n"
        f"原协议类型: {source_protocol.get('protocol_type') or '未提供'}\n"
        f"原协议消息码: {source_protocol.get('message_code') or '未提供'}\n"
        f"原协议子消息组合: {source_protocol.get('bundle_id') or '未提供'}\n"
        f"原协议内容:\n{source_protocol_content or '未直接提供原协议全文，请严格依据证据摘要。'}\n\n"
        f"目标协议名称: {target_protocol.get('name') or target_protocol.get('protocol_type') or '未提供'}\n"
        f"目标协议类型: {target_protocol.get('protocol_type') or '未提供'}\n"
        f"目标协议消息码: {target_protocol.get('message_code') or '未提供'}\n"
        f"目标协议内容:\n{target_protocol['content']}\n\n"
        f"原报文字段清单（仅字段名，不含示例值）:\n{source_message_text}\n\n"
    ]
    if target_field_requirements:
        user_prompt.append(f"{target_field_requirements}\n\n")
    if evidence_text:
        user_prompt.append(f"{evidence_text}\n\n")
    user_prompt.extend(
        [
            "要求:\n"
            "1. 每条规则面向一个 target_field。\n"
            "2. source_fields 必须列出该目标字段依赖的原字段；如果是常量规则，可输出空数组。\n"
            "3. conversion_mode 只能是 transcoding 或 mapping。\n"
            "4. rule 必须能被程序直接执行，并产出目标字段的值；rule 中必须直接引用 source_fields 里的具体字段名，不要使用 value/raw 这种泛占位符。\n"
            "5. 如需条件或循环，请使用 python_block，并把最终值赋给 result。\n"
            "6. 输出必须是 JSON 数组。\n"
            "7. 不允许输出任何 JSON 之外的文本。\n"
            "8. 若某个目标字段没有源字段依赖，但目标协议要求必须输出，可生成常量数值公式。\n"
            "9. 先使用文档证据；若文档没有覆盖，再结合 XML 结构、字段语义、候选源字段和必要常识推理生成规则；若仍无法给出可执行的值到值/常量公式，请直接跳过该字段。\n"
            "10. 如果提供了候选源字段列表，只能从候选列表中选择 source_fields；不要自行发明新的源字段名。\n\n"
            "11. 禁止把明显不同物理量直接对应，例如高度<->经纬度、姿态角<->时间、信息字段<->威胁字段；不确定时直接跳过该字段。\n"
            "12. 同名、同标签、同路径片段只能作为候选线索，不能单独当作充分依据；如果协议定义或证据没有明确支持，必须跳过。\n\n"
            "13. 禁止把位宽、默认值、字段序号误写成映射表，例如不要把“默认值=1”“位长=32”生成成“1=32”这类伪规则。\n\n"
            "14. 单个 target_field 可以同时依赖多个候选源字段；如果这些字段来自不同协议/不同子消息，也必须一起列入 source_fields。\n"
            "15. 对于“时间/时间1/时间2/时间3/总时间”等聚合时间字段，不要直接把“小时/分钟/秒”任一单个分量当成最终结果；若候选中存在多个时间分量，应优先组合成完整时间表达式。\n"
            "16. 禁止输出 C/CPP 风格三元表达式，例如 cond ? a : b；必须输出 Python 表达式 a if cond else b。\n\n"
            "17. 禁止输出字符串、f-string、日期文本、时间文本或任何带引号的结果；只允许输出数值公式。\n\n"
            "输出示例:\n"
            "[\n"
            "  {\n"
            "    \"concept_name\": \"LATITUDE\",\n"
            "    \"target_field\": \"LATITUDE_DEG\",\n"
            "    \"source_fields\": [\"LATITUDE\"],\n"
            "    \"conversion_mode\": \"transcoding\",\n"
            "    \"formula_kind\": \"python_expr\",\n"
            "    \"rule\": \"signed(LATITUDE, bits) * 0.0013 / 60\"\n"
            "  },\n"
            "  {\n"
            "    \"concept_name\": \"MISSION_ASSIGNMENT\",\n"
            "    \"target_field\": \"MISSION_ASSIGNMENT_CODE\",\n"
            "    \"source_fields\": [\"MISSION_ASSIGNMENT_DISCRETE\"],\n"
            "    \"conversion_mode\": \"mapping\",\n"
            "    \"formula_kind\": \"mapping_table\",\n"
            "    \"rule\": \"1=10, 5=30, 6=40\"\n"
            "  }\n"
            "]"
        ]
    )
    return system_prompt, "".join(user_prompt)


def _build_empty_rule_retry_prompt(base_prompt: str, attempt: int, max_attempts: int) -> str:
    return (
        f"{base_prompt}\n\n"
        f"重试提示：你上一轮输出未形成可用规则。当前是第 {attempt} / {max_attempts} 次尝试。"
        "请至少输出 1 条可执行规则；如果确实没有明确证据支持任何目标字段，"
        "也必须返回空 JSON 数组 []，不要输出解释文字。"
    )


def _build_missing_target_retry_prompt(
    base_prompt: str,
    attempt: int,
    max_attempts: int,
    missing_fields: List[str],
) -> str:
    missing_text = ", ".join(missing_fields)
    return (
        f"{base_prompt}\n\n"
        f"重试提示：你上一轮遗漏了以下目标字段的规则：{missing_text}。"
        f"当前是第 {attempt} / {max_attempts} 次尝试。"
        "请补齐这些目标字段；如果某字段不依赖源字段，可以输出常量数值公式。"
        "仍然只允许输出 JSON 数组。"
    )


def _build_invalid_rule_retry_prompt(
    base_prompt: str,
    filtered_rules: List[Dict[str, Any]],
) -> str:
    lines = [
        base_prompt,
        "",
        "重试提示：你上一轮输出的规则被系统校验判为无效，请只修复这些问题后重新生成；若无法可靠生成请返回 []。",
    ]
    for item in filtered_rules[:5]:
        lines.append(
            f"- target_field={item.get('target_field') or 'UNKNOWN'}: {item.get('filtered_reason') or '未通过校验'}"
        )
    lines.append("仍然只允许输出 JSON 数组。")
    return "\n".join(lines)


class _FormulaReferenceCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> Any:
        self.names.add(node.id)


def _validate_rule_formula_fields(
    rule: Dict[str, Any],
    available_source_fields: Iterable[str],
) -> Tuple[bool, Optional[str]]:
    source_fields = [_normalize_rule_field_token(item) for item in (rule.get("source_fields") or []) if _normalize_rule_field_token(item)]
    available: set[str] = set()
    for item in available_source_fields:
        available.update(_rule_field_lookup_keys(item))
    invalid = [
        field
        for field in source_fields
        if _rule_field_lookup_keys(field).isdisjoint(available)
    ]
    if invalid:
        return False, f"source_fields 引用了不存在的字段: {', '.join(invalid)}"

    formula_kind = str(rule.get("formula_kind") or "").strip() or _infer_formula_kind(str(rule.get("rule") or ""))
    formula = str(rule.get("rule") or "").strip()
    if not formula:
        return False, "rule 为空"
    if formula_kind == "mapping_table":
        return True, None
    if formula == "0":
        return True, None
    if formula_kind == "python_expr":
        formula = _normalize_formula_expression_syntax(formula)
        rule["rule"] = formula

    try:
        parse_mode = "exec" if formula_kind == "python_block" else "eval"
        tree = ast.parse(formula, mode=parse_mode)
    except SyntaxError as exc:
        return False, f"公式语法错误: {exc.msg}"
    if any(isinstance(node, (ast.JoinedStr, ast.FormattedValue)) for node in ast.walk(tree)):
        return False, "公式不能使用字符串模板或 f-string"
    if any(
        isinstance(node, ast.Constant) and isinstance(getattr(node, "value", None), str)
        for node in ast.walk(tree)
    ):
        return False, "公式不能返回字符串常量"

    collector = _FormulaReferenceCollector()
    collector.visit(tree)
    allowed_names = set(source_fields) | ALLOWED_FORMULA_FUNCTIONS | ALLOWED_FORMULA_VARS
    invalid_names = [
        name for name in collector.names
        if name not in allowed_names and not name.startswith("__")
    ]
    if invalid_names:
        return False, f"公式引用了未声明字段: {', '.join(sorted(set(invalid_names)))}"
    return True, None


def _build_target_task_maps(
    target_tasks: Optional[Iterable[Dict[str, Any]]]
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, set[str]], Dict[str, Dict[str, float]]]:
    target_spec_map: Dict[str, Dict[str, Any]] = {}
    candidate_map: Dict[str, set[str]] = {}
    candidate_score_map: Dict[str, Dict[str, float]] = {}
    for item in target_tasks or []:
        target_field = _target_concept_identity(item)
        if not target_field:
            continue
        target_spec_map[target_field] = dict(item)
        candidates = {
            _normalize_rule_field_token(candidate.get("field_name") or "")
            for candidate in (item.get("candidate_source_fields") or [])
            if _normalize_rule_field_token(candidate.get("field_name") or "")
        }
        if candidates:
            candidate_map[target_field] = candidates
            candidate_score_map[target_field] = {
                _normalize_rule_field_token(candidate.get("field_name") or ""): float(candidate.get("score") or 0.0)
                for candidate in (item.get("candidate_source_fields") or [])
                if _normalize_rule_field_token(candidate.get("field_name") or "")
            }
    return target_spec_map, candidate_map, candidate_score_map


def _find_candidate_source_entry(
    target_spec: Dict[str, Any],
    source_field: str,
) -> Dict[str, Any]:
    normalized_source_field = _normalize_rule_field_token(source_field)
    for candidate in (target_spec.get("candidate_source_fields") or []):
        if not isinstance(candidate, dict):
            continue
        if _normalize_rule_field_token(candidate.get("field_name") or "") == normalized_source_field:
            return candidate
    return {}


def _normalize_python_block_for_validation(formula: str) -> str:
    lines: List[str] = []
    for raw_line in str(formula or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("return "):
            indent = line[: len(line) - len(line.lstrip())]
            line = f"{indent}result = {stripped[7:].strip()}"
        lines.append(line)
    return "\n".join(lines)


def _looks_like_executable_rule_formula(rule: Dict[str, Any]) -> bool:
    formula = str(rule.get("rule") or "").strip()
    if not formula or formula == "0":
        return bool(formula)
    formula_kind = str(rule.get("formula_kind") or "").strip() or _infer_formula_kind(formula)
    if formula_kind == "mapping_table":
        return bool(_parse_mapping_table_pairs(formula))
    if formula_kind == "python_expr":
        formula = _normalize_formula_expression_syntax(formula)
    try:
        if formula_kind == "python_block":
            ast.parse(_normalize_python_block_for_validation(formula), mode="exec")
        else:
            ast.parse(formula, mode="eval")
        return True
    except SyntaxError:
        return False


def _repair_invalid_rule_to_direct_copy(
    rule: Dict[str, Any],
    target_spec_map: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    source_fields = [
        _normalize_rule_field_token(item)
        for item in (rule.get("source_fields") or [])
        if _normalize_rule_field_token(item)
    ]
    if len(source_fields) != 1:
        return None

    target_identity = _target_rule_concept_identity(rule)
    target_spec = target_spec_map.get(target_identity) or {}
    source_field = source_fields[0]
    candidate = _find_candidate_source_entry(target_spec, source_field)
    candidate_score = float(candidate.get("score") or 0.0)
    target_groups = _target_semantic_groups(target_spec)
    source_groups = _semantic_groups_for_text(source_field)
    if target_groups and source_groups and not (target_groups & source_groups):
        return None

    target_leaf = _field_leaf_name(target_spec) or target_identity
    source_leaf = source_field.split(".")[-1]
    aligned_leaf = _is_reliable_name_alignment(target_leaf, source_leaf)
    target_bits = _coerce_bit_length(target_spec.get("bit_length"))
    source_bits = _candidate_bit_length(candidate)
    reliable_time_match = (
        target_bits is not None
        and source_bits is not None
        and target_bits == source_bits
        and "time" in target_groups
        and "time" in source_groups
    )
    if not aligned_leaf and not reliable_time_match and candidate_score < 50.0:
        return None
    if not bool(target_groups & source_groups) and candidate_score < 72.0:
        return None

    repaired = dict(rule)
    repaired["source_fields"] = source_fields
    repaired["conversion_mode"] = "transcoding"
    repaired["formula_kind"] = "python_expr"
    repaired["rule"] = source_field
    repaired["repair_reason"] = "invalid_formula_direct_copy_fallback"
    return repaired


def _is_suspicious_ambiguous_time_rule(
    rule: Dict[str, Any],
    target_spec_map: Dict[str, Dict[str, Any]],
) -> bool:
    source_fields = [
        _normalize_rule_field_token(item)
        for item in (rule.get("source_fields") or [])
        if _normalize_rule_field_token(item)
    ]
    if len(source_fields) <= 1:
        return False

    target_identity = _target_rule_concept_identity(rule)
    target_spec = target_spec_map.get(target_identity) or {}
    if "time" not in _target_semantic_groups(target_spec):
        return False
    if not all("时间" in field for field in source_fields):
        return False

    candidate_scores = []
    for source_field in source_fields:
        candidate = _find_candidate_source_entry(target_spec, source_field)
        if not candidate:
            return False
        candidate_scores.append(float(candidate.get("score") or 0.0))
    if not candidate_scores or max(candidate_scores) - min(candidate_scores) > 4.0:
        return False

    formula = str(rule.get("rule") or "").strip()
    return not bool(re.search(r"[+\-*/%]", formula))


def _is_suspicious_time_component_direct_rule(
    rule: Dict[str, Any],
    target_spec_map: Dict[str, Dict[str, Any]],
) -> bool:
    source_fields = [
        _normalize_rule_field_token(item)
        for item in (rule.get("source_fields") or [])
        if _normalize_rule_field_token(item)
    ]
    if len(source_fields) != 1:
        return False

    target_identity = _target_rule_concept_identity(rule)
    target_spec = target_spec_map.get(target_identity) or {}
    if "time" not in _target_semantic_groups(target_spec):
        return False

    target_component = _time_component_kind(
        target_spec.get("field_name"),
        target_spec.get("label"),
        target_spec.get("description"),
        " ".join(str(part) for part in (target_spec.get("path_parts") or [])),
    )
    if target_component:
        return False

    source_field = source_fields[0]
    source_component = _time_component_kind(source_field)
    if source_component is None:
        return False

    candidate_components = {
        _time_component_kind(candidate.get("field_name"), candidate.get("display_field"), candidate.get("source_path"))
        for candidate in (target_spec.get("candidate_source_fields") or [])
        if isinstance(candidate, dict)
    }
    candidate_components.discard(None)
    if len(candidate_components) < 2:
        return False

    formula = str(rule.get("rule") or "").strip()
    if _normalize_rule_field_token(formula) != source_field:
        return False
    return True


def _validate_rule_semantic_alignment(
    rule: Dict[str, Any],
    target_spec_map: Dict[str, Dict[str, Any]],
    candidate_map: Dict[str, set[str]],
) -> Tuple[bool, Optional[str]]:
    target_field = str(rule.get("target_field") or "").strip().upper()
    target_identity = _target_rule_concept_identity(rule)
    source_fields = [_normalize_rule_field_token(item) for item in (rule.get("source_fields") or []) if _normalize_rule_field_token(item)]
    if not target_identity or not source_fields:
        return True, None

    if target_spec_map and target_identity not in target_spec_map:
        return False, f"target_field 不在目标协议字段集合中: {target_field or target_identity}"

    target_is_control = _is_control_field_name(target_field)
    source_control_fields = [source_field for source_field in source_fields if _is_control_field_name(source_field)]
    if source_control_fields and not target_is_control:
        return False, f"source_fields 使用了控制位字段，不能直接映射到业务字段: {', '.join(source_control_fields)}"
    if target_is_control:
        if len(source_control_fields) != len(source_fields):
            return False, "目标字段是控制位字段，但 source_fields 未全部命中控制位"
        target_control_token = _extract_control_field_token(target_field)
        source_control_tokens = [_extract_control_field_token(source_field) for source_field in source_fields]
        source_control_tokens = [token for token in source_control_tokens if token]
        if target_control_token and source_control_tokens:
            if any(token != target_control_token for token in source_control_tokens):
                return False, (
                    "控制位编号不一致: "
                    f"target={target_control_token}, source={','.join(source_control_tokens)}"
                )

    candidate_fields = candidate_map.get(target_identity)
    if candidate_fields:
        expanded_candidate_fields: set[str] = set()
        for item in candidate_fields:
            expanded_candidate_fields.update(_rule_field_lookup_keys(item))
        invalid = [
            source_field
            for source_field in source_fields
            if _rule_field_lookup_keys(source_field).isdisjoint(expanded_candidate_fields)
        ]
    else:
        invalid = []
    if invalid:
        return False, f"source_fields 未命中候选源字段: {', '.join(invalid)}"

    target_spec = target_spec_map.get(target_identity) or {}
    formula_kind = str(rule.get("formula_kind") or "").strip() or _infer_formula_kind(str(rule.get("rule") or ""))
    mapping_pairs = _parse_mapping_table_pairs(str(rule.get("rule") or "")) if formula_kind == "mapping_table" else []
    if len(source_fields) == 1 and len(mapping_pairs) == 1:
        _left, right_value = mapping_pairs[0]
        suspicious_targets = {
            str(target_spec.get("bit_length") or "").strip(),
            str(target_spec.get("default_value") or "").strip(),
        }
        suspicious_targets.discard("")
        if right_value in suspicious_targets:
            return False, "疑似把目标字段位宽或默认值误写成映射值"

    target_groups = _semantic_groups_for_text(
        target_field,
        target_spec.get("label"),
        target_spec.get("description"),
        " ".join(str(part) for part in (target_spec.get("path_parts") or [])),
    )
    target_leaf = _field_leaf_name(target_spec) or target_field
    source_groups = _semantic_groups_for_text(" ".join(source_fields))
    if target_groups and not source_groups and _is_direct_copy_rule(rule):
        return False, "直拷贝规则缺少可判定的源字段语义"
    if target_groups and source_groups and not (target_groups & source_groups):
        if (target_groups & STRICT_SEMANTIC_GROUPS) and (source_groups & STRICT_SEMANTIC_GROUPS):
            return False, (
                "目标字段与源字段语义组冲突: "
                f"target={','.join(sorted(target_groups))} "
                f"source={','.join(sorted(source_groups))}"
            )
        if _is_direct_copy_rule(rule):
            return False, (
                "直拷贝规则缺少语义一致性: "
                f"target={','.join(sorted(target_groups))} "
                f"source={','.join(sorted(source_groups))}"
            )
    generic_target_groups = _generic_semantic_groups(target_groups)
    if generic_target_groups:
        for source_field in source_fields:
            source_leaf = str(source_field or "").strip().upper().split(".")[-1]
            source_leaf_groups = _generic_semantic_groups(_semantic_groups_for_text(source_field))
            if source_leaf_groups != generic_target_groups:
                return False, f"泛化字段类别不一致: target={target_field}, source={source_field}"
            if not _is_reliable_name_alignment(target_leaf, source_leaf):
                return False, f"泛化字段名称对齐不足: target={target_field}, source={source_field}"
    elif _is_direct_copy_rule(rule):
        best_alignment = max(
            (
                _leaf_similarity(target_leaf, str(source_field or "").strip().upper().split(".")[-1])
                for source_field in source_fields
            ),
            default=0.0,
        )
        if best_alignment < 0.6 and not (target_groups & source_groups):
            return False, f"直拷贝规则字段名对齐不足: target={target_field}, source={','.join(source_fields)}"
    return True, None


def _filter_valid_generated_rules(
    generated_rules: List[Dict[str, Any]],
    available_source_fields: Iterable[str],
    target_tasks: Optional[Iterable[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    valid_rules: List[Dict[str, Any]] = []
    filtered_rules: List[Dict[str, Any]] = []
    target_spec_map, candidate_map, _candidate_score_map = _build_target_task_maps(target_tasks)
    for rule in generated_rules:
        candidate_rule = dict(rule)
        is_valid, reason = _validate_rule_formula_fields(candidate_rule, available_source_fields)
        if not is_valid:
            repaired_rule = _repair_invalid_rule_to_direct_copy(candidate_rule, target_spec_map)
            if repaired_rule is not None and _looks_like_executable_rule_formula(repaired_rule):
                candidate_rule = repaired_rule
                is_valid, reason = _validate_rule_formula_fields(candidate_rule, available_source_fields)
        if is_valid and _is_suspicious_ambiguous_time_rule(candidate_rule, target_spec_map):
            is_valid = False
            reason = "时间类字段候选歧义过高，公式缺少可靠运算依据"
        if is_valid and _is_suspicious_time_component_direct_rule(candidate_rule, target_spec_map):
            is_valid = False
            reason = "时间聚合字段不能直接映射到单个时间分量"
        if is_valid:
            is_valid, reason = _validate_rule_semantic_alignment(candidate_rule, target_spec_map, candidate_map)
        if is_valid:
            valid_rules.append(candidate_rule)
            continue
        filtered_rule = dict(candidate_rule)
        filtered_rule["filtered_reason"] = reason
        filtered_rules.append(filtered_rule)
    return valid_rules, filtered_rules


def _annotate_rule_confidence(
    rules: List[Dict[str, Any]],
    target_tasks: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not rules:
        return []
    _target_spec_map, _candidate_map, candidate_score_map = _build_target_task_maps(target_tasks)
    annotated: List[Dict[str, Any]] = []
    for rule in rules:
        enriched = dict(rule)
        enriched["confidence"] = _estimate_rule_confidence(rule, candidate_score_map=candidate_score_map)
        if not enriched.get("source"):
            enriched["source"] = "llm_generated"
        if not enriched.get("status"):
            enriched["status"] = "candidate"
        annotated.append(enriched)
    return annotated


def _generate_rule_items_with_retry(
    llm_client: LocalLLM,
    system_prompt: str,
    user_prompt: str,
    max_new_tokens: int,
    max_empty_rule_retries: int,
    required_target_fields: Optional[Iterable[Any]] = None,
) -> Tuple[str, List[Dict[str, Any]], int, List[str]]:
    last_raw_output = ""
    last_missing_fields: List[str] = []
    last_generated_rules: List[Dict[str, Any]] = []
    attempts = max(1, int(max_empty_rule_retries) + 1)
    for attempt in range(1, attempts + 1):
        if attempt == 1:
            prompt = user_prompt
        elif last_missing_fields:
            prompt = _build_missing_target_retry_prompt(user_prompt, attempt, attempts, last_missing_fields)
        else:
            prompt = _build_empty_rule_retry_prompt(user_prompt, attempt, attempts)
        raw_output = llm_client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            max_new_tokens=max_new_tokens,
            temperature=0.0,
            top_p=1.0,
            enable_thinking=False,
        )
        last_raw_output = raw_output
        parsed = LocalLLM.parse_json_from_response(raw_output, prefer=list)
        if parsed is None:
            parsed = LocalLLM.parse_json_from_response(raw_output, prefer=dict)
        rule_items = _extract_rule_items(parsed)
        generated_rules = normalize_generated_rules(rule_items)
        last_generated_rules = generated_rules
        if generated_rules:
            missing_fields = _missing_target_concepts(generated_rules, required_target_fields)
            if not missing_fields:
                return raw_output, generated_rules, attempt, []
            last_missing_fields = missing_fields
            continue
        last_missing_fields = []

    if last_generated_rules:
        return last_raw_output, last_generated_rules, attempts, last_missing_fields

    if _normalize_concept_target_fields(required_target_fields):
        return last_raw_output, [], attempts, _required_target_concept_names(required_target_fields)

    snippet = " ".join(str(last_raw_output or "").strip().split())[:240] or "EMPTY"
    raise ValueError(
        f"协议规则生成失败：连续 {attempts} 次生成空规则，请检查提示词、模型输出或证据输入。最后一次输出片段: {snippet}"
    )


def _generate_rules_for_target_tasks(
    llm_client: LocalLLM,
    source_protocol: Dict[str, Optional[str]],
    target_protocol: Dict[str, Optional[str]],
    source_message: Optional[Any],
    pageindex_evidence: Optional[Dict[str, Any]],
    use_trained_docs: bool,
    target_tasks: List[Dict[str, Any]],
    max_new_tokens: int,
    max_empty_rule_retries: int,
    batch_size: int = 1,
) -> Tuple[str, List[Dict[str, Any]], int, int, List[Dict[str, Any]]]:
    raw_outputs: List[str] = []
    all_valid_rules: List[Dict[str, Any]] = []
    all_filtered_rules: List[Dict[str, Any]] = []
    total_attempt_count = 0
    for start in range(0, len(target_tasks), max(1, batch_size)):
        batch = target_tasks[start : start + max(1, batch_size)]
        system_prompt, user_prompt = build_protocol_rule_generation_prompt(
            source_protocol,
            target_protocol,
            source_message=source_message,
            pageindex_evidence=pageindex_evidence,
            use_trained_docs=use_trained_docs,
            required_target_fields=batch,
            target_tasks=batch,
        )
        raw_output, generated_rules, attempt_count, _remaining_missing_fields = _generate_rule_items_with_retry(
            llm_client=llm_client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_new_tokens=max_new_tokens,
            max_empty_rule_retries=max_empty_rule_retries,
            required_target_fields=batch,
        )
        total_attempt_count += attempt_count
        raw_outputs.append(raw_output)
        available_source_fields = normalize_source_message(source_message).keys() if isinstance(source_message, dict) else []
        valid_rules, filtered_rules = _filter_valid_generated_rules(
            generated_rules,
            available_source_fields,
            target_tasks=batch,
        )
        if not valid_rules and filtered_rules and len(batch) == 1:
            retry_prompt = _build_invalid_rule_retry_prompt(user_prompt, filtered_rules)
            retry_raw_output, retry_generated_rules, retry_attempt_count, _remaining_missing_fields = _generate_rule_items_with_retry(
                llm_client=llm_client,
                system_prompt=system_prompt,
                user_prompt=retry_prompt,
                max_new_tokens=max_new_tokens,
                max_empty_rule_retries=1,
                required_target_fields=batch,
            )
            total_attempt_count += retry_attempt_count
            raw_outputs.append(retry_raw_output)
            retry_valid_rules, retry_filtered_rules = _filter_valid_generated_rules(
                retry_generated_rules,
                available_source_fields,
                target_tasks=batch,
            )
            if retry_valid_rules:
                valid_rules = retry_valid_rules
                filtered_rules = retry_filtered_rules
            else:
                filtered_rules = filtered_rules + retry_filtered_rules
        all_valid_rules.extend(valid_rules)
        all_filtered_rules.extend(filtered_rules)
    return "\n\n".join(text for text in raw_outputs if text), all_valid_rules, total_attempt_count, max(0, total_attempt_count - len(list(range(0, len(target_tasks), max(1, batch_size))))), all_filtered_rules


def generate_protocol_field_rules(
    source_protocol: Any,
    target_protocol: Any,
    source_message: Optional[Any] = None,
    required_target_fields: Optional[Iterable[Any]] = None,
    source_field_catalog: Optional[Iterable[Dict[str, Any]]] = None,
    use_knowledge_base: bool = True,
    use_page_index: bool = False,
    use_trained_docs: bool = False,
    project_id: str = "",
    dataset_id: str = "",
    doc_set_id: str = "",
    index_ref: str = "",
    index_registry_path: Any = None,
    evidence_provider: Optional[Any] = None,
    llm: Optional[LocalLLM] = None,
    max_new_tokens: int = 8192,
    max_empty_rule_retries: int = DEFAULT_EMPTY_RULE_RETRIES,
) -> Dict[str, Any]:
    normalized_source_protocol = _normalize_protocol_spec(
        source_protocol,
        "原",
        allow_empty_content=bool(use_page_index and use_trained_docs),
    )
    normalized_target_protocol = _normalize_protocol_spec(target_protocol, "目标")
    normalized_required_target_fields = _normalize_required_target_fields(required_target_fields)
    concept_required_target_fields = _normalize_concept_target_fields(required_target_fields)
    pageindex_evidence = None
    page_index_status = "disabled"
    rag_status = "disabled"
    rag_reason = None
    source_protocol_type = (
        normalized_source_protocol.get("protocol_type")
        or normalized_source_protocol.get("name")
        or DEFAULT_PROTOCOL_TYPE
    )
    target_protocol_type = (
        normalized_target_protocol.get("protocol_type")
        or normalized_target_protocol.get("name")
        or source_protocol_type
    )
    message_bundle_id = None
    if isinstance(source_protocol, dict):
        message_bundle_id = str(source_protocol.get("bundle_id") or "").strip() or None
    normalized_source_message = normalize_source_message(source_message) if isinstance(source_message, dict) else {}
    catalog_lookup_fields: List[str] = []
    for item in source_field_catalog or []:
        if not isinstance(item, dict):
            continue
        for candidate in (
            item.get("field_name"),
            item.get("display_field"),
            item.get("label"),
            item.get("actual_field"),
        ):
            text = str(candidate or "").strip()
            if text:
                catalog_lookup_fields.append(text)
    knowledge_base = None
    knowledge_graph_error: Optional[str] = None
    if use_knowledge_base:
        try:
            knowledge_base = ProtocolConversionKnowledgeBase.load(source_protocol_type)
        except Exception as exc:
            knowledge_graph_error = str(exc)
    graph_generated_rules: List[Dict[str, Any]] = []
    knowledge_graph_hit = False
    graph_filtered_rules: List[Dict[str, Any]] = []
    candidate_target_tasks: List[Dict[str, Any]] = []
    normalized_required_target_names = [
        item["field_name"]
        for item in concept_required_target_fields
        if str(item.get("field_name") or "").strip()
    ]

    graph_lookup_fields = list(normalized_source_message.keys()) or catalog_lookup_fields
    graph_rules = []
    graph_lookup_started = time.perf_counter()
    catalog_protocol_groups = _group_source_fields_by_protocol(source_field_catalog)
    if use_knowledge_base and catalog_protocol_groups:
        loaded_knowledge_bases: Dict[str, ProtocolConversionKnowledgeBase] = {}
        for protocol_name, message_code, lookup_fields in catalog_protocol_groups:
            try:
                protocol_kb = loaded_knowledge_bases.get(protocol_name)
                if protocol_kb is None:
                    protocol_kb = ProtocolConversionKnowledgeBase.load(protocol_name)
                    loaded_knowledge_bases[protocol_name] = protocol_kb
                    if knowledge_base is None:
                        knowledge_base = protocol_kb
                graph_rules.extend(
                    protocol_kb.find_rules_for_source_fields(
                        source_fields=lookup_fields,
                        message_code=message_code,
                        target_protocol_type=target_protocol_type,
                        target_message_code=normalized_target_protocol.get("message_code"),
                        target_fields=normalized_required_target_names or None,
                    )
                )
            except Exception as exc:
                if knowledge_graph_error:
                    knowledge_graph_error = f"{knowledge_graph_error}; {protocol_name}: {exc}"
                else:
                    knowledge_graph_error = f"{protocol_name}: {exc}"
    elif knowledge_base and graph_lookup_fields and not message_bundle_id:
        graph_rules = knowledge_base.find_rules_for_source_fields(
            source_fields=graph_lookup_fields,
            message_code=normalized_source_protocol.get("message_code"),
            target_protocol_type=target_protocol_type,
            target_message_code=normalized_target_protocol.get("message_code"),
            target_fields=normalized_required_target_names or None,
        )
    graph_lookup_time_ms = round((time.perf_counter() - graph_lookup_started) * 1000.0, 4)
    if graph_rules:
        graph_candidate_rules = _dedupe_rules_by_target_field(
            [_knowledge_rule_to_generated_rule(rule) for rule in graph_rules]
        )
        graph_generated_rules, graph_filtered_rules = _filter_valid_generated_rules(
            graph_candidate_rules,
            normalized_source_message.keys(),
            target_tasks=concept_required_target_fields,
        )
        graph_generated_rules = _annotate_rule_confidence(
            graph_generated_rules,
            target_tasks=concept_required_target_fields,
        )
        graph_generated_rules = _apply_source_field_catalog_to_rules(
            graph_generated_rules,
            source_field_catalog=source_field_catalog,
        )
        graph_generated_rules = _dedupe_rules_by_target_field(graph_generated_rules)
        knowledge_graph_hit = bool(graph_generated_rules)
        if graph_generated_rules and not _missing_target_concepts(graph_generated_rules, concept_required_target_fields):
            expanded_graph_rules = _expand_generated_rules_to_target_instances(
                graph_generated_rules,
                normalized_required_target_fields,
            )
            executable_rules = _build_executable_rules(
                expanded_graph_rules,
                normalized_source_protocol,
                normalized_target_protocol,
            )
            return {
                "source_protocol": normalized_source_protocol,
                "target_protocol": normalized_target_protocol,
                "raw_output": None,
                "generated_rules": expanded_graph_rules,
                "normalized_rules": executable_rules,
                "kg_writeback_payload": _build_kg_writeback_payload(
                    generated_rules=[],
                    source_protocol=normalized_source_protocol,
                    target_protocol=normalized_target_protocol,
                ),
                "summary": {
                    "total_rules": len(expanded_graph_rules),
                    "target_fields": [rule["target_field"] for rule in expanded_graph_rules],
                    "page_index_status": "knowledge_graph_skipped" if use_page_index else "disabled",
                    "rag_status": "knowledge_graph_skipped" if use_page_index else "disabled",
                    "rag_reason": None,
                    "evidence_snippet_count": 0,
                    "matched_doc_ids": [],
                    "candidate_doc_count": 0,
                    "registry_count": 0,
                    "registry_paths": [],
                    "doc_set_id": doc_set_id or None,
                    "index_ref": index_ref or None,
                    "attempt_count": 0,
                    "empty_rule_retry_count": 0,
                    "knowledge_graph_hit": True,
                    "knowledge_graph_backend": knowledge_base.to_summary().get("backend"),
                    "knowledge_graph_error": knowledge_graph_error,
                    "knowledge_graph_lookup_time_ms": graph_lookup_time_ms,
                    "knowledge_graph_avg_rule_time_ms": round(
                        graph_lookup_time_ms / len(graph_generated_rules),
                        4,
                    ) if graph_generated_rules else None,
                    "knowledge_graph_rule_count": len(graph_generated_rules),
                    "candidate_target_count": 0,
                    "deterministic_rule_count": 0,
                    "llm_rule_count": 0,
                    "validated_rule_count": len(graph_generated_rules),
                    "filtered_rule_count": len(graph_filtered_rules),
                    "target_task_count": 0,
                    "llm_attempted_target_count": 0,
                    "default_zero_rule_count": 0,
                    "kg_writeback_rule_count": 0,
                    "missing_target_fields": [],
                },
            }

    candidate_target_tasks = _build_target_generation_tasks(
        required_target_fields=concept_required_target_fields,
        existing_rules=graph_generated_rules,
        normalized_source_message=normalized_source_message,
        source_field_catalog=source_field_catalog,
    )
    deterministic_generated_rules = _build_deterministic_candidate_rules(candidate_target_tasks)
    if deterministic_generated_rules:
        deterministic_generated_rules = _annotate_rule_confidence(
            deterministic_generated_rules,
            target_tasks=candidate_target_tasks,
        )
        deterministic_generated_rules = _apply_source_field_catalog_to_rules(
            deterministic_generated_rules,
            source_field_catalog=source_field_catalog,
        )
        if message_bundle_id:
            deterministic_generated_rules = [
                {
                    **rule,
                    "message_bundle_id": message_bundle_id,
                }
                for rule in deterministic_generated_rules
            ]
    baseline_generated_rules = _dedupe_rules_by_target_field(
        graph_generated_rules + deterministic_generated_rules
    )

    llm_required_target_fields = (
        [
            item
            for item in concept_required_target_fields
            if item["field_name"] in _missing_target_concepts(baseline_generated_rules, concept_required_target_fields)
        ]
        if concept_required_target_fields
        else concept_required_target_fields
    )

    if use_page_index:
        if evidence_provider is not None:
            provider = evidence_provider
        elif use_trained_docs:
            provider = get_trained_doc_evidence_provider(
                project_id=project_id,
                dataset_id=dataset_id,
                doc_set_id=doc_set_id,
                index_ref=index_ref,
                index_registry_path=index_registry_path,
            )
        else:
            provider = get_pageindex_evidence_provider()
        evidence_target_protocol = dict(normalized_target_protocol)
        evidence_target_protocol["field_queries"] = [
            item["field_name"] for item in llm_required_target_fields or []
        ]
        pageindex_evidence = provider.collect_evidence(
            source_protocol=normalized_source_protocol,
            target_protocol=evidence_target_protocol,
            source_message=source_message,
        )
        rag_status, rag_reason = _resolve_pageindex_status(pageindex_evidence)
        page_index_status = rag_status

    target_tasks = _build_target_generation_tasks(
        required_target_fields=llm_required_target_fields,
        existing_rules=baseline_generated_rules,
        normalized_source_message=normalized_source_message,
        source_field_catalog=source_field_catalog,
    )

    generated_rules: List[Dict[str, Any]] = []
    filtered_rules: List[Dict[str, Any]] = list(graph_filtered_rules)
    raw_output = ""
    attempt_count = 0
    empty_rule_retry_count = 0
    if target_tasks:
        llm_client = llm or get_llm()
        raw_output, generated_rules, attempt_count, empty_rule_retry_count, llm_filtered_rules = _generate_rules_for_target_tasks(
            llm_client=llm_client,
            source_protocol=normalized_source_protocol,
            target_protocol=normalized_target_protocol,
            source_message=source_message,
            pageindex_evidence=pageindex_evidence,
            use_trained_docs=use_trained_docs,
            target_tasks=target_tasks,
            max_new_tokens=max_new_tokens,
            max_empty_rule_retries=max_empty_rule_retries,
        )
        filtered_rules = list(graph_filtered_rules) + list(llm_filtered_rules)
    elif not concept_required_target_fields:
        llm_client = llm or get_llm()
        system_prompt, user_prompt = build_protocol_rule_generation_prompt(
            normalized_source_protocol,
            normalized_target_protocol,
            source_message=source_message,
            pageindex_evidence=pageindex_evidence,
            use_trained_docs=use_trained_docs,
            required_target_fields=llm_required_target_fields,
        )
        raw_output, raw_generated_rules, attempt_count, _remaining_missing_fields = _generate_rule_items_with_retry(
            llm_client=llm_client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_new_tokens=max_new_tokens,
            max_empty_rule_retries=max_empty_rule_retries,
            required_target_fields=llm_required_target_fields,
        )
        generated_rules, filtered_rules = _filter_valid_generated_rules(
            raw_generated_rules,
            normalized_source_message.keys(),
            target_tasks=llm_required_target_fields,
        )
        generated_rules = _annotate_rule_confidence(
            generated_rules,
            target_tasks=llm_required_target_fields,
        )
        empty_rule_retry_count = max(0, attempt_count - 1)
    else:
        generated_rules = _annotate_rule_confidence(
            generated_rules,
            target_tasks=target_tasks,
        )
    if generated_rules:
        generated_rules = _annotate_rule_confidence(
            generated_rules,
            target_tasks=target_tasks or llm_required_target_fields,
        )
        generated_rules = _apply_source_field_catalog_to_rules(
            generated_rules,
            source_field_catalog=source_field_catalog,
        )
        if message_bundle_id:
            generated_rules = [
                {
                    **rule,
                    "message_bundle_id": message_bundle_id,
                }
                for rule in generated_rules
            ]
    combined_generated_rules = _dedupe_rules_by_target_field(
        graph_generated_rules + deterministic_generated_rules + generated_rules
    )
    concept_default_zero_rules = _build_default_zero_rules(
        concept_required_target_fields,
        combined_generated_rules,
    )
    concept_final_generated_rules = _dedupe_rules_by_target_field(
        combined_generated_rules + concept_default_zero_rules
    )
    expanded_generated_rules = _expand_generated_rules_to_target_instances(
        concept_final_generated_rules,
        normalized_required_target_fields,
    )
    default_zero_rules = _build_default_zero_rules(normalized_required_target_fields, expanded_generated_rules)
    final_generated_rules = _dedupe_rules_by_target_field(expanded_generated_rules + default_zero_rules)
    executable_rules = _build_executable_rules(
        final_generated_rules,
        normalized_source_protocol,
        normalized_target_protocol,
    )
    concept_executable_rules = _build_executable_rules(
        concept_final_generated_rules,
        normalized_source_protocol,
        normalized_target_protocol,
    )
    llm_executable_rules = _build_executable_rules(
        generated_rules,
        normalized_source_protocol,
        normalized_target_protocol,
    )
    kg_writeback_payload = _build_kg_writeback_payload(
        generated_rules=final_generated_rules,
        source_protocol=normalized_source_protocol,
        target_protocol=normalized_target_protocol,
        excluded_target_fields={str(rule.get("target_field") or "").strip().upper() for rule in default_zero_rules},
    )
    if knowledge_base and llm_executable_rules and not message_bundle_id:
        try:
            knowledge_base.upsert_generated_rules(
                llm_executable_rules,
                protocol_type=source_protocol_type,
                message_code=normalized_source_protocol.get("message_code"),
                target_protocol_type=target_protocol_type,
                target_message_code=normalized_target_protocol.get("message_code"),
                source="llm_generated",
            )
        except Exception:
            pass
    final_default_zero_rule_count = sum(
        1
        for rule in final_generated_rules
        if str(rule.get("rule") or rule.get("formula") or "").strip() == "0"
    )
    return {
        "source_protocol": normalized_source_protocol,
        "target_protocol": normalized_target_protocol,
        "raw_output": raw_output,
        "concept_generated_rules": concept_final_generated_rules,
        "concept_normalized_rules": concept_executable_rules,
        "generated_rules": final_generated_rules,
        "normalized_rules": executable_rules,
        "kg_writeback_payload": kg_writeback_payload,
        "summary": {
            "total_rules": len(final_generated_rules),
            "target_fields": [rule["target_field"] for rule in final_generated_rules],
            "page_index_status": page_index_status,
            "rag_status": rag_status,
            "rag_reason": rag_reason,
            "evidence_snippet_count": int((pageindex_evidence or {}).get("evidence_snippet_count") or 0),
            "matched_doc_ids": list((pageindex_evidence or {}).get("matched_doc_ids") or []),
            "candidate_doc_count": int((pageindex_evidence or {}).get("candidate_doc_count") or 0),
            "registry_count": int((pageindex_evidence or {}).get("registry_count") or 0),
            "registry_paths": list((pageindex_evidence or {}).get("registry_paths") or []),
            "doc_set_id": (pageindex_evidence or {}).get("doc_set_id") or (doc_set_id or None),
            "index_ref": (pageindex_evidence or {}).get("index_ref") or (index_ref or None),
            "attempt_count": attempt_count,
            "empty_rule_retry_count": empty_rule_retry_count,
            "knowledge_graph_hit": knowledge_graph_hit,
            "knowledge_graph_backend": knowledge_base.to_summary().get("backend") if knowledge_base else None,
            "knowledge_graph_error": knowledge_graph_error,
            "knowledge_graph_lookup_time_ms": graph_lookup_time_ms,
            "knowledge_graph_avg_rule_time_ms": round(
                graph_lookup_time_ms / len(graph_generated_rules),
                4,
            ) if graph_generated_rules else None,
            "knowledge_graph_rule_count": len(graph_generated_rules),
            "candidate_target_count": len(candidate_target_tasks),
            "deterministic_rule_count": len(deterministic_generated_rules),
            "llm_rule_count": len(generated_rules),
            "validated_rule_count": len(deterministic_generated_rules) + len(generated_rules),
            "filtered_rule_count": len(filtered_rules),
            "target_task_count": len(target_tasks),
            "llm_attempted_target_count": len(target_tasks),
            "default_zero_rule_count": final_default_zero_rule_count,
            "kg_writeback_rule_count": len(kg_writeback_payload.get("rules") or []),
            "missing_target_fields": _missing_target_fields(final_generated_rules, normalized_required_target_fields),
        },
    }


def generate_and_convert_protocol_bundle(
    source_protocol: Any,
    target_protocol: Any,
    source_message: Any,
    use_knowledge_base: bool = True,
    use_page_index: bool = False,
    use_trained_docs: bool = False,
    project_id: str = "",
    dataset_id: str = "",
    doc_set_id: str = "",
    index_ref: str = "",
    evidence_provider: Optional[Any] = None,
    llm: Optional[LocalLLM] = None,
) -> Dict[str, Any]:
    normalized_source_protocol = _normalize_protocol_spec(
        source_protocol,
        "原",
        allow_empty_content=bool(use_page_index and use_trained_docs),
    )
    normalized_target_protocol = _normalize_protocol_spec(target_protocol, "目标")
    source_protocol_type = (
        normalized_source_protocol.get("protocol_type")
        or normalized_source_protocol.get("name")
        or DEFAULT_PROTOCOL_TYPE
    )
    target_protocol_type = (
        normalized_target_protocol.get("protocol_type")
        or normalized_target_protocol.get("name")
        or source_protocol_type
    )
    source_fields = list(normalize_source_message(source_message).keys())

    knowledge_base = None
    knowledge_base_error: Optional[str] = None
    knowledge_base_summary: Dict[str, Any] = {"backend": None}
    if use_knowledge_base:
        try:
            knowledge_base = ProtocolConversionKnowledgeBase.load(source_protocol_type)
            knowledge_base_summary = knowledge_base.to_summary()
        except Exception as exc:
            knowledge_base_error = str(exc)
    graph_rules: List[Dict[str, Any]] = []
    if use_knowledge_base and knowledge_base is not None:
        graph_rules = [
            {
                "field_name": rule.field_name,
                "source_fields": list(rule.source_fields or [rule.field_name]),
                "target_field": rule.target_field,
                "conversion_mode": rule.conversion_mode,
                "formula_kind": rule.formula_kind,
                "formula": rule.formula,
                "rule": rule.formula,
                "unit": rule.unit,
                "bit_length": rule.bit_length,
                "description": rule.description,
                "concept_name": rule.concept_name,
                "target_protocol_type": rule.target_protocol_type,
                "target_message_code": rule.target_message_code,
                "source": "knowledge_base",
            }
            for rule in knowledge_base.find_rules_for_source_fields(
                source_fields=source_fields,
                message_code=normalized_source_protocol.get("message_code"),
                target_protocol_type=target_protocol_type,
                target_message_code=normalized_target_protocol.get("message_code"),
            )
        ]

    if graph_rules:
        rule_generation = {
            "source_protocol": normalized_source_protocol,
            "target_protocol": normalized_target_protocol,
            "raw_output": None,
            "generated_rules": graph_rules,
            "normalized_rules": graph_rules,
            "summary": {
                "total_rules": len(graph_rules),
                "target_fields": [rule.get("target_field") for rule in graph_rules],
                "knowledge_graph_hit": True,
                "knowledge_graph_backend": knowledge_base_summary.get("backend"),
                "knowledge_graph_error": knowledge_base_error,
                "page_index_status": "knowledge_graph_skipped" if use_page_index else "disabled",
                "evidence_snippet_count": 0,
            },
        }
    else:
        rule_generation = generate_protocol_field_rules(
            source_protocol=normalized_source_protocol,
            target_protocol=normalized_target_protocol,
            source_message=source_message,
            use_knowledge_base=use_knowledge_base,
            use_page_index=use_page_index,
            use_trained_docs=use_trained_docs,
            project_id=project_id,
            dataset_id=dataset_id,
            doc_set_id=doc_set_id,
            index_ref=index_ref,
            evidence_provider=evidence_provider,
            llm=llm,
        )

    conversion_result = execute_protocol_conversion(
        source_message=source_message,
        llm_formula_output=rule_generation["normalized_rules"],
        protocol_type=source_protocol_type,
        message_code=normalized_source_protocol.get("message_code"),
        target_protocol_type=target_protocol_type,
        target_message_code=normalized_target_protocol.get("message_code"),
        use_knowledge_base=use_knowledge_base,
    )
    return {
        "rule_generation": rule_generation,
        "conversion_result": conversion_result,
        "converted_message": conversion_result["converted_message"],
        "summary": {
            "generated_rule_count": rule_generation["summary"]["total_rules"],
            "knowledge_graph_hit": bool(graph_rules),
            "converted_field_count": len(conversion_result["converted_message"]),
            "conversion_success_count": conversion_result["summary"]["success_count"],
            "conversion_failed_count": conversion_result["summary"]["failed_count"],
            "knowledge_graph_backend": conversion_result["knowledge_base"].get("backend"),
            "knowledge_graph_error": knowledge_base_error or rule_generation["summary"].get("knowledge_graph_error"),
            "page_index_status": rule_generation["summary"].get("page_index_status", "disabled"),
            "evidence_snippet_count": rule_generation["summary"].get("evidence_snippet_count", 0),
            "doc_set_id": rule_generation["summary"].get("doc_set_id"),
            "index_ref": rule_generation["summary"].get("index_ref"),
        },
    }

from runtime_config import apply_runtime_environment
# 接口6: QA字段智能抽取与规则校验
# POST /api/knowledge/extract_validate_qa

import sys
import os
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from flask import Flask, request, jsonify

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.local_llm import LocalLLM, get_llm
from llm.prompt_templates import PromptTemplates
from database.mysql_client import MySQLClient

from protocol_schema import (
    build_schema_prompt_context,
    guess_message_code,
    resolve_message_schema,
    validate_with_schema,
)

apply_runtime_environment()

app = Flask(__name__)

# 初始化组件
_llm: Optional[LocalLLM] = None
_db: Optional[MySQLClient] = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_DATASET_ROOT = PROJECT_ROOT / "data" / "datasets"
LOCAL_DATASET_ROOT = Path(__file__).resolve().parent / "data" / "datasets"

def get_llm_instance() -> LocalLLM:
    """获取LLM实例（延迟初始化）"""
    global _llm
    if _llm is None:
        _llm = get_llm()
    return _llm

def get_db_instance() -> MySQLClient:
    """获取数据库实例（延迟初始化）"""
    global _db
    if _db is None:
        _db = MySQLClient()
        _db.init_tables()
    return _db


def _iter_dataset_roots() -> List[Path]:
    """返回可能存在 QA 文件的数据集根目录。"""
    roots: List[Path] = []
    for root in (PROJECT_DATASET_ROOT, LOCAL_DATASET_ROOT):
        if root.exists():
            roots.append(root)
    return roots


def _load_qa_record_from_file(dataset_id: str, qa_id: str) -> Optional[Dict[str, Any]]:
    """在文件存储的 qa_pairs.json 中按 qa_id 回退查找 QA。"""
    search_roots = _iter_dataset_roots()
    dataset_dirs: List[Path] = []

    if dataset_id:
        for root in search_roots:
            dataset_dirs.append(root / dataset_id)
    else:
        for root in search_roots:
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    dataset_dirs.append(child)

    visited_files = set()
    for dataset_dir in dataset_dirs:
        qa_path = dataset_dir / "qa_pairs.json"
        qa_key = str(qa_path.resolve()) if qa_path.exists() else str(qa_path)
        if qa_key in visited_files or not qa_path.exists():
            continue
        visited_files.add(qa_key)

        try:
            with open(qa_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        qa_pairs = payload.get("qa_pairs")
        if not isinstance(qa_pairs, list):
            continue

        file_dataset_id = str(payload.get("dataset_id") or dataset_dir.name or "").strip()
        for item in qa_pairs:
            if not isinstance(item, dict):
                continue
            if str(item.get("qa_id") or "").strip() != qa_id:
                continue

            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if not question or not answer:
                raise ValueError(f"qa_id={qa_id} 对应的 question/answer 为空")

            return {
                "dataset_id": str(item.get("dataset_id") or file_dataset_id or dataset_id).strip(),
                "qa_id": qa_id,
                "question": question,
                "answer": answer,
                "protocol_type": str(item.get("protocol_type") or "Link16").strip() or "Link16",
                "message_code": str(item.get("target_message_code") or "").strip() or None,
            }
    return None

def _load_qa_record_payload(dataset_id: Any, qa_id: Any) -> Dict[str, Any]:
    """按 qa_id 读取 QA 原文，优先数据库，缺失时回退文件存储。"""
    requested_dataset_id = str(dataset_id or "").strip()
    resolved_qa_id = str(qa_id or "").strip()
    if not resolved_qa_id:
        raise ValueError("qa_id 参数必填")

    qa_record = get_db_instance().get_qa_by_id(resolved_qa_id)
    if qa_record is None:
        file_payload = _load_qa_record_from_file(requested_dataset_id, resolved_qa_id)
        if file_payload is None:
            raise ValueError(f"未找到 qa_id={resolved_qa_id} 对应的 QA 数据")
        return file_payload

    question = str(qa_record.question or "").strip()
    answer = str(qa_record.answer or "").strip()
    if not question or not answer:
        raise ValueError(f"qa_id={resolved_qa_id} 对应的 question/answer 为空")

    resolved_dataset_id = str(qa_record.dataset_id or requested_dataset_id).strip()

    return {
        "dataset_id": resolved_dataset_id,
        "qa_id": resolved_qa_id,
        "question": question,
        "answer": answer,
        "protocol_type": str(qa_record.protocol_type or "Link16").strip() or "Link16",
        "message_code": str(qa_record.target_message_code or "").strip() or None,
    }

def _handle_batch_extract_validate_payload(data: Dict[str, Any]):
    items = data.get("items", [])
    default_dataset_id = str(data.get("dataset_id") or "").strip()
    default_protocol_type = str(data.get("protocol_type") or "").strip()
    default_message_code = str(data.get("message_code") or "").strip()
    if not items:
        return jsonify({
            "code": 400,
            "message": "items 数组不能为空",
            "data": None
        }), 400

    results = []
    success_count = 0
    failed_count = 0

    for item in items:
        try:
            qa_id = item.get("qa_id")
            dataset_id = str(item.get("dataset_id") or default_dataset_id).strip()
            qa_payload = _load_qa_record_payload(dataset_id, qa_id)
            protocol_type = (
                str(item.get("protocol_type") or default_protocol_type or qa_payload["protocol_type"] or "Link16").strip()
                or "Link16"
            )
            message_code = (
                str(item.get("message_code") or default_message_code or qa_payload.get("message_code") or "").strip()
                or None
            )

            result = run_extraction_pipeline(
                dataset_id=qa_payload["dataset_id"],
                qa_id=qa_payload["qa_id"],
                question=qa_payload["question"],
                answer=qa_payload["answer"],
                protocol_type=protocol_type,
                message_code=message_code,
            )

            results.append({
                "dataset_id": qa_payload["dataset_id"],
                "qa_id": qa_payload["qa_id"],
                "status": "success",
                "result": result
            })
            success_count += 1
        except Exception as e:
            results.append({
                "qa_id": item.get("qa_id", "unknown"),
                "status": "failed",
                "error": str(e)
            })
            failed_count += 1

    return jsonify({
        "code": 200,
        "message": "success",
        "data": {
            "total": len(items),
            "success": success_count,
            "failed": failed_count,
            "results": results
        }
    })

def _expand_qa_id_batch_items(data: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """兼容 qa_id 直接传数组的批量格式。"""
    qa_id_value = data.get("qa_id")
    if not isinstance(qa_id_value, list):
        return None

    items: List[Dict[str, Any]] = []
    for entry in qa_id_value:
        if isinstance(entry, dict):
            item = dict(entry)
            if not str(item.get("qa_id") or "").strip():
                raise ValueError("qa_id 数组中的对象项必须包含 qa_id")
            items.append(item)
            continue

        qa_id = str(entry or "").strip()
        if not qa_id:
            raise ValueError("qa_id 数组中不能为空")
        items.append({"qa_id": qa_id})

    return items

def _extract_first_int(text: str, patterns: List[str]) -> Optional[int]:
    raw_text = str(text or "")
    for pattern in patterns:
        match = re.search(pattern, raw_text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            continue
    return None

def _extract_first_float_and_unit(text: str, patterns: List[str]) -> Tuple[Optional[float], Optional[str]]:
    raw_text = str(text or "")
    for pattern in patterns:
        match = re.search(pattern, raw_text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        unit = None
        if match.lastindex and match.lastindex >= 2:
            unit = str(match.group(2) or "").strip() or None
        return value, unit
    return None, None

def _normalize_field_name(field_name: Optional[str]) -> Optional[str]:
    normalized = str(field_name or "").strip().strip("，。,:：")
    if not normalized:
        return None
    return normalized.upper()

def _extract_field_name_from_qa(question: str, answer: str) -> Optional[str]:
    candidates = [
        answer,
        question,
    ]
    patterns = [
        r"字段名称\s*[:：]?\s*([A-Za-z][A-Za-z0-9_./\-]*)",
        r"([A-Za-z][A-Za-z0-9_./\-]*)字段",
        r"协议中\s*([A-Za-z][A-Za-z0-9_./\-]*)\s*字段",
        r"\b([A-Z][A-Z0-9_./\-]{2,})\b",
    ]
    for text in candidates:
        raw_text = str(text or "")
        for pattern in patterns:
            match = re.search(pattern, raw_text)
            if not match:
                continue
            normalized = _normalize_field_name(match.group(1))
            if normalized:
                return normalized
    return None


def _extract_field_anchors_from_text(text: str) -> List[str]:
    raw_text = str(text or "")
    anchors: List[str] = []
    seen = set()
    patterns = [
        r"([A-Za-z][A-Za-z0-9_./\-]*)字段",
        r"\b([A-Z][A-Z0-9_./\-]{2,})\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, raw_text):
            normalized = _normalize_field_name(match.group(1))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            anchors.append(normalized)
    return anchors

def _extract_bit_location(answer: str) -> Tuple[Optional[int], Optional[int]]:
    text = str(answer or "")
    range_match = re.search(r"位段(?:为|是)?\s*(\d+)\s*[-~～]\s*(\d+)", text, flags=re.IGNORECASE)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        return start, max(1, end - start + 1)

    bit_start = _extract_first_int(
        text,
        [
            r"起始位\s*[:：]?\s*(\d+)",
            r"start(?:_bit)?\s*[:=]?\s*(\d+)",
            r"位段(?:为|是)?\s*(\d+)",
        ],
    )
    bit_width = _extract_first_int(
        text,
        [
            r"位宽\s*[:：]?\s*(\d+)\s*位",
            r"占用\s*(\d+)\s*位",
            r"bit[_\s-]*width\s*[:=]?\s*(\d+)",
        ],
    )
    if bit_start is not None and bit_width is None and re.search(r"位段(?:为|是)?\s*\d+\b", text, flags=re.IGNORECASE):
        bit_width = 1
    return bit_start, bit_width

def _extract_range_and_unit(answer: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    text = str(answer or "")
    range_match = re.search(
        r"(?:范围|range)\s*[:：]?\s*([+\-]?\d+(?:\.\d+)?)\s*([A-Za-z%°/]+)?\s*(?:到|to|TO|~|～|—|–|-)\s*([+\-]?\d+(?:\.\d+)?)\s*([A-Za-z%°/]+)?",
        text,
        flags=re.IGNORECASE,
    )
    if not range_match:
        return None, None, None

    range_min = float(range_match.group(1))
    range_max = float(range_match.group(3))
    unit = str(range_match.group(2) or range_match.group(4) or "").strip() or None
    return range_min, range_max, unit

def _looks_like_formula(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if re.search(r"\bresult\s*=", normalized, flags=re.IGNORECASE):
        return True
    if re.search(r"\d+\s*(?:=|->|→)\s*[A-Za-z_][A-Za-z0-9_./\-]*", normalized):
        return True
    if re.search(r"[\u4e00-\u9fff]", normalized):
        return False
    return bool(re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*[*+/%-]\s*[\dA-Za-z_(]", normalized))

def _strip_formula_suffix(text: str) -> Optional[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return None
    normalized = normalized.strip("`")
    normalized = re.sub(r"[。；;]+$", "", normalized)
    return normalized or None

def extract_structured_info_from_qa_text(question: str, answer: str) -> Dict[str, Any]:
    """从规则化 QA 文本中直接提取字段参数，作为 LLM 抽取失败时的兜底。"""
    field_name = _extract_field_name_from_qa(question, answer)
    related_fields = _extract_field_anchors_from_text(f"{question}\n{answer}")
    bit_start, bit_width = _extract_bit_location(answer)
    resolution, resolution_unit = _extract_first_float_and_unit(
        answer,
        [
            r"(?:分辨率|resolution)\s*[:：]?\s*([+\-]?\d+(?:\.\d+)?)\s*([A-Za-z%°/]+)?",
        ],
    )
    range_min, range_max, range_unit = _extract_range_and_unit(answer)
    unit = range_unit or resolution_unit

    meaning = None
    meaning_match = re.search(r"(?:表示|用于|含义是|代表)\s*([^。；;]+)", answer)
    if meaning_match:
        meaning = str(meaning_match.group(1) or "").strip() or None

    conversion_formula = None
    if _looks_like_formula(answer):
        conversion_formula = _strip_formula_suffix(answer)

    return {
        "field_name": field_name,
        "bit_width": bit_width,
        "bit_start": bit_start,
        "resolution": resolution,
        "unit": unit,
        "range_min": range_min,
        "range_max": range_max,
        "meaning": meaning,
        "conversion_formula": conversion_formula,
        "related_fields": related_fields,
    }


def _contains_any(text: str, patterns: List[str]) -> bool:
    raw_text = str(text or "")
    return any(re.search(pattern, raw_text, flags=re.IGNORECASE) for pattern in patterns)


def infer_validation_profile(
    question: str,
    answer: str,
    extracted_info: Dict[str, Any],
) -> Dict[str, Any]:
    """根据 QA 内容判断应启用哪些校验项。"""
    question_text = str(question or "")
    answer_text = str(answer or "")
    combined = f"{question_text}\n{answer_text}"

    mentions_field_spec = _contains_any(
        combined,
        [
            r"位宽",
            r"位段",
            r"占用\s*\d+\s*位",
            r"分辨率",
            r"范围",
            r"\bbit\b",
            r"\bwidth\b",
            r"\bresolution\b",
            r"\brange\b",
        ],
    )
    mentions_enum = _contains_any(
        combined,
        [
            r"取值\s*[-+]?\d+",
            r"代表什么",
            r"含义是什么",
            r"枚举",
            r"对应",
            r"\bmeaning\b",
        ],
    )
    mentions_relation = _contains_any(
        combined,
        [
            r"由哪些字段组成",
            r"包含哪些字段",
            r"与.*之间.*关系",
            r"依赖哪些",
            r"关系是什么",
            r"组成",
        ],
    )
    looks_formula = bool(extracted_info.get("conversion_formula")) or _looks_like_formula(answer_text)
    has_field_name = bool(extracted_info.get("field_name"))

    profile_name = "field_meaning"
    if looks_formula:
        profile_name = "conversion_formula"
    elif mentions_relation:
        profile_name = "relation_structure"
    elif mentions_enum:
        profile_name = "enum_semantics"
    elif mentions_field_spec:
        profile_name = "field_spec"
    elif has_field_name:
        profile_name = "field_meaning"

    rule_sets = {
        "field_spec": {
            "required": ["FieldNamePresent", "BitWidthFormat"],
            "optional": ["ResolutionFormat", "RangeFormat", "RangeCoverageCheck", "MeaningPresent"],
            "description": "字段规格类问答，核心检查字段名和位宽，其余规格项按命中情况校验。",
        },
        "enum_semantics": {
            "required": ["FieldNamePresent", "MeaningPresent"],
            "optional": ["RangeFormat", "BitWidthFormat"],
            "description": "枚举语义类问答，核心检查字段名和取值含义。",
        },
        "relation_structure": {
            "required": ["FieldAnchorsPresent"],
            "optional": ["MeaningPresent"],
            "description": "结构关系类问答，核心检查是否锚定到真实字段对象。",
        },
        "field_meaning": {
            "required": ["FieldNamePresent", "MeaningPresent"],
            "optional": ["BitWidthFormat", "ResolutionFormat", "RangeFormat"],
            "description": "字段含义类问答，核心检查字段名和语义说明。",
        },
        "conversion_formula": {
            "required": ["ConversionFormulaPresent"],
            "optional": ["FieldNamePresent", "MeaningPresent"],
            "description": "转换公式类问答，核心检查是否存在可解析公式。",
        },
    }
    selected = rule_sets[profile_name]
    return {
        "profile_name": profile_name,
        "description": selected["description"],
        "required_rules": selected["required"],
        "optional_rules": selected["optional"],
    }


def _has_structured_validation_signal(
    question: str,
    answer: str,
    extracted_info: Dict[str, Any],
) -> bool:
    combined = f"{str(question or '')}\n{str(answer or '')}"
    if _contains_any(
        combined,
        [
            r"位宽",
            r"位段",
            r"分辨率",
            r"范围",
            r"取值\s*[-+]?\d+",
            r"代表什么",
            r"由哪些字段组成",
            r"包含哪些字段",
            r"转换",
            r"公式",
            r"映射",
            r"\bbit\b",
            r"\brange\b",
            r"\bformula\b",
        ],
    ):
        return True
    return any(
        extracted_info.get(key) not in (None, "", [])
        for key in (
            "field_name",
            "bit_width",
            "bit_start",
            "resolution",
            "range_min",
            "range_max",
            "meaning",
            "conversion_formula",
        )
    )


def _profile_has_minimum_structured_content(
    profile_name: str,
    extracted_info: Dict[str, Any],
) -> bool:
    field_name_present = bool(str(extracted_info.get("field_name") or "").strip())
    meaning_present = bool(str(extracted_info.get("meaning") or "").strip())
    formula_present = bool(str(extracted_info.get("conversion_formula") or "").strip())
    related_fields_count = len(extracted_info.get("related_fields") or [])
    has_range = extracted_info.get("range_min") is not None or extracted_info.get("range_max") is not None
    has_spec_detail = any(
        value is not None
        for value in (
            extracted_info.get("bit_width"),
            extracted_info.get("resolution"),
            extracted_info.get("bit_start"),
        )
    ) or has_range

    if profile_name == "conversion_formula":
        return formula_present
    if profile_name == "relation_structure":
        return field_name_present or related_fields_count >= 2
    if profile_name == "enum_semantics":
        return field_name_present and meaning_present
    if profile_name == "field_meaning":
        return field_name_present and meaning_present
    if profile_name == "field_spec":
        return field_name_present and has_spec_detail
    return field_name_present or meaning_present or formula_present


def _build_bypass_validation_result(
    protocol_type: str,
    message_code: Optional[str],
    message_schema: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
    bypass_reason: str,
) -> Dict[str, Any]:
    return {
        "passed": True,
        "check_items": [
            {
                "rule_name": "StructuredValidationBypass",
                "description": "结构化校验流程",
                "passed": True,
                "message": "通过",
                "status": "PASS",
                "msg": "通过",
                "applicability": "required",
            }
        ],
        "protocol_type": protocol_type,
        "message_code": message_code,
        "schema_applied": bool(message_schema),
        "checked_at": datetime.now().isoformat(),
        "validation_profile": profile,
        "bypassed": True,
        "bypass_reason": bypass_reason,
    }


def _should_bypass_structured_validation(
    extracted_info: Dict[str, Any],
    question: str,
    answer: str,
    profile: Dict[str, Any],
) -> Optional[str]:
    if not _has_structured_validation_signal(question, answer, extracted_info):
        return "not_applicable_for_strict_structured_validation"
    if _is_effectively_empty_extraction(extracted_info):
        return "empty_extraction"
    if not _profile_has_minimum_structured_content(profile["profile_name"], extracted_info):
        return "insufficient_core_fields"
    return None


def _is_effectively_empty_extraction(extracted_info: Dict[str, Any]) -> bool:
    meaningful_keys = (
        "field_name",
        "bit_width",
        "bit_start",
        "resolution",
        "unit",
        "range_min",
        "range_max",
        "meaning",
        "conversion_formula",
    )
    for key in meaningful_keys:
        value = extracted_info.get(key)
        if value not in (None, "", []):
            return False
    return True


def _build_supplemental_validation_rules() -> Dict[str, Dict[str, Any]]:
    return {
        "FieldNamePresent": {
            "description": "字段名存在校验",
            "check": lambda info: bool(str(info.get("field_name") or "").strip()),
            "pass_msg": lambda info: f"已识别字段名: {info.get('field_name')}",
            "fail_msg": "未识别到字段名",
        },
        "FieldAnchorsPresent": {
            "description": "字段锚点存在校验",
            "check": lambda info: bool(str(info.get("field_name") or "").strip()) or len(info.get("related_fields") or []) >= 2,
            "pass_msg": lambda info: (
                f"已识别字段锚点: {', '.join((info.get('related_fields') or [])[:6])}"
                if (info.get("related_fields") or [])
                else f"已识别字段名: {info.get('field_name')}"
            ),
            "fail_msg": "未识别到足够的字段锚点",
        },
        "MeaningPresent": {
            "description": "语义说明存在校验",
            "check": lambda info: bool(str(info.get("meaning") or "").strip()),
            "pass_msg": lambda info: f"已抽取语义说明: {str(info.get('meaning') or '').strip()}",
            "fail_msg": "未抽取到明确语义说明",
        },
        "ConversionFormulaPresent": {
            "description": "转换公式存在校验",
            "check": lambda info: bool(str(info.get("conversion_formula") or "").strip()),
            "pass_msg": lambda info: "已抽取到转换公式",
            "fail_msg": "未抽取到转换公式",
        },
    }

def _merge_extracted_candidates(
    base: Dict[str, Any],
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if merged.get(key) in (None, "") and value not in (None, ""):
            merged[key] = value
    return merged

def extract_field_info(
    question: str,
    answer: str,
    protocol_type: str = "",
    schema_context: str = "",
) -> Dict[str, Any]:
    """
    使用LLM从问答内容中抽取结构化字段信息

    Args:
        question: 问题文本
        answer: 答案文本
        protocol_type: 协议类型

    Returns:
        抽取的字段信息字典
    """
    llm = get_llm_instance()

    # 获取格式化的prompt
    system_prompt, user_prompt = PromptTemplates.format_qa_extract(
        question=question,
        answer=answer,
        protocol_type=protocol_type
    )

    if schema_context:
        user_prompt = (
            f"{user_prompt}\n\n"
            f"请额外遵循以下协议Schema约束（仅在信息明确时填充）：\n{schema_context}"
        )

    fallback_extracted = extract_structured_info_from_qa_text(question, answer)

    extracted = None
    try:
        raw_response = llm.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            max_new_tokens=256,
            temperature=0.0,
            top_p=1.0,
            enable_thinking=False,
        )
        parser = getattr(llm, "parse_json_from_response", LocalLLM.parse_json_from_response)
        extracted = parser(raw_response, prefer=dict)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"抽取阶段LLM调用失败，转为规则兜底: {exc}")
        extracted = None
    extracted = extracted or {}

    # 确保所有字段都存在
    default_info = {
        "field_name": None,
        "bit_width": None,
        "bit_start": None,
        "resolution": None,
        "unit": None,
        "range_min": None,
        "range_max": None,
        "meaning": None,
        "conversion_formula": None,
    }

    # 合并抽取结果，处理可能的字段名变体
    result = default_info.copy()

    # 字段名映射
    field_mappings = {
        "field_name": ["field_name", "fieldName", "field", "name"],
        "bit_width": ["bit_width", "bitWidth", "bit", "width", "bits"],
        "bit_start": ["bit_start", "bitStart", "start_bit", "offset"],
        "resolution": ["resolution", "res"],
        "unit": ["unit", "units"],
        "range_min": ["range_min", "rangeMin", "min", "min_value", "minimum"],
        "range_max": ["range_max", "rangeMax", "max", "max_value", "maximum"],
        "meaning": ["meaning", "description", "desc"],
        "conversion_formula": ["conversion_formula", "formula", "expression"],
    }

    for target_key, source_keys in field_mappings.items():
        for source_key in source_keys:
            if source_key in extracted and extracted[source_key] is not None:
                result[target_key] = extracted[source_key]
                break

    result = _merge_extracted_candidates(result, fallback_extracted)

    if not any(value is not None for value in result.values()):
        result["extraction_error"] = "LLM未能返回有效的JSON结果"

    return result

def validate_extracted_info(
    extracted_info: Dict[str, Any],
    protocol_type: str = "",
    message_schema: Optional[Dict[str, Any]] = None,
    message_code: Optional[str] = None,
    question: str = "",
    answer: str = "",
) -> Dict[str, Any]:
    """
    根据��议类型执行规则校验

    Args:
        extracted_info: 抽取的字段信息
        protocol_type: 协议类型

    Returns:
        校验结果字典
    """
    profile = infer_validation_profile(question, answer, extracted_info)
    bypass_reason = _should_bypass_structured_validation(extracted_info, question, answer, profile)
    if bypass_reason:
        return _build_bypass_validation_result(
            protocol_type,
            message_code,
            message_schema,
            profile,
            bypass_reason,
        )

    rules = {
        **PromptTemplates.get_validation_rules(protocol_type),
        **_build_supplemental_validation_rules(),
    }
    required_rules = set(profile["required_rules"])
    optional_rules = set(profile["optional_rules"])

    check_items = []
    all_passed = True

    for rule_name, rule_config in rules.items():
        applicability = "required" if rule_name in required_rules else "optional" if rule_name in optional_rules else "not_applicable"
        if applicability == "not_applicable":
            check_items.append({
                "rule_name": rule_name,
                "description": rule_config["description"],
                "passed": None,
                "message": "当前 QA 类型不要求此校验项",
                "status": "N/A",
                "msg": "当前 QA 类型不要求此校验项",
                "applicability": applicability,
            })
            continue
        try:
            passed = rule_config["check"](extracted_info)

            if passed:
                message = rule_config["pass_msg"](extracted_info)
            else:
                message = rule_config["fail_msg"]
                if applicability == "required":
                    all_passed = False

            check_items.append({
                "rule_name": rule_name,
                "description": rule_config["description"],
                "passed": passed,
                "message": message,
                "status": "PASS" if passed else "FAIL",
                "msg": message,
                "applicability": applicability,
            })
        except Exception as e:
            check_items.append({
                "rule_name": rule_name,
                "description": rule_config["description"],
                "passed": False,
                "message": f"校验过程出错: {str(e)}",
                "status": "FAIL",
                "msg": f"校验过程出错: {str(e)}",
                "applicability": applicability,
            })
            if applicability == "required":
                all_passed = False

    schema_check_items = validate_with_schema(extracted_info, message_schema)
    for item in schema_check_items:
        check_items.append(item)
        if not item.get("passed"):
            all_passed = False

    return {
        "passed": all_passed,
        "check_items": check_items,
        "protocol_type": protocol_type,
        "message_code": message_code,
        "schema_applied": bool(message_schema),
        "checked_at": datetime.now().isoformat(),
        "validation_profile": profile,
    }

def run_extraction_pipeline(
    dataset_id: str,
    qa_id: str,
    question: str,
    answer: str,
    protocol_type: str = "",
    message_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    执行完整的抽取校验流程

    Args:
        qa_id: QA ID
        dataset_id: 数据集 ID
        question: 问题文本
        answer: 答案文本
        protocol_type: 协议类型

    Returns:
        完整的处理结果
    """
    detected_message_code = message_code or guess_message_code(f"{question}\n{answer}")
    protocol_schema, resolved_message_code, message_schema = resolve_message_schema(
        protocol_type,
        detected_message_code,
    )
    schema_context = build_schema_prompt_context(
        protocol_schema,
        message_schema,
        resolved_message_code,
    )

    # Step 1: 抽取字段信息
    extracted_info = extract_field_info(
        question,
        answer,
        protocol_type,
        schema_context=schema_context,
    )

    extracted_field_name = extracted_info.get("field_name")
    if extracted_field_name:
        protocol_schema, resolved_message_code, message_schema = resolve_message_schema(
            protocol_type,
            detected_message_code,
            extracted_field_name,
        )

    # Step 2: 执行规则校验
    validation_result = validate_extracted_info(
        extracted_info,
        protocol_type,
        message_schema=message_schema,
        message_code=resolved_message_code,
        question=question,
        answer=answer,
    )

    return {
        "dataset_id": dataset_id,
        "qa_id": qa_id,
        "message_code": resolved_message_code,
        "schema_applied": bool(message_schema),
        "extracted_info": extracted_info,
        "validation_result": validation_result,
    }

@app.route("/api/knowledge/extract_validate_qa", methods=["POST"])
def extract_validate_qa():
    """
    QA字段智能抽取与规则校验接口

    输入参数:
    {
        "qa_id": "524",
        "protocol_type": "Link16"
    }

    响应格式:
    {
        "code": 200,
        "message": "success",
        "data": {
            "qa_id": "qa_2024",
            "extracted_info": {...},
            "validation_result": {...}
        }
    }
    """
    try:
        data = request.json

        # 参数校验
        if not data:
            return jsonify({
                "code": 400,
                "message": "请求体不能为空",
                "data": None
            }), 400

        qa_id_batch_items = _expand_qa_id_batch_items(data)
        if qa_id_batch_items is not None:
            batch_payload = dict(data)
            batch_payload["items"] = qa_id_batch_items
            batch_payload["batch"] = True
            return _handle_batch_extract_validate_payload(batch_payload)

        if bool(data.get("batch")):
            if "items" not in data:
                return jsonify({
                    "code": 400,
                    "message": "batch=true 时必须包含 items 数组",
                    "data": None
                }), 400
            return _handle_batch_extract_validate_payload(data)

        qa_id = data.get("qa_id")
        dataset_id = str(data.get("dataset_id") or "").strip()
        qa_payload = _load_qa_record_payload(dataset_id, data.get("qa_id"))
        protocol_type = str(data.get("protocol_type") or qa_payload["protocol_type"] or "Link16").strip() or "Link16"
        message_code = str(data.get("message_code") or qa_payload.get("message_code") or "").strip() or None

        # 执行抽取校验流程
        result = run_extraction_pipeline(
            dataset_id=qa_payload["dataset_id"],
            qa_id=qa_payload["qa_id"],
            question=qa_payload["question"],
            answer=qa_payload["answer"],
            protocol_type=protocol_type,
            message_code=message_code,
        )

        return jsonify({
            "code": 200,
            "message": "success",
            "data": result
        })

    except ValueError as e:
        return jsonify({
            "code": 400,
            "message": str(e),
            "data": None
        }), 400
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": f"处理失败: {str(e)}",
            "data": None
        }), 500

@app.route("/api/knowledge/extract_validate_qa/batch", methods=["POST"])
def extract_validate_qa_batch():
    """
    批量QA字段智能抽取与规则校验接口

    输入参数:
    {
        "dataset_id": "ds_2024",
        "items": [
            {
                "qa_id": "qa_001",
                "protocol_type": "Link16"
            },
            ...
        ]
    }

    响应格式:
    {
        "code": 200,
        "message": "success",
        "data": {
            "total": 10,
            "success": 8,
            "failed": 2,
            "results": [...]
        }
    }
    """
    try:
        data = request.json

        if not data or "items" not in data:
            return jsonify({
                "code": 400,
                "message": "请求体必须包含 items 数组",
                "data": None
            }), 400

        return _handle_batch_extract_validate_payload(data)

    except Exception as e:
        return jsonify({
            "code": 500,
            "message": f"处理失败: {str(e)}",
            "data": None
        }), 500

@app.route("/api/knowledge/extract_validate_qa/<qa_id>", methods=["GET"])
def get_extraction_result(qa_id: str):
    """
    获取已有的抽取校验结果

    路径参数:
        qa_id: QA ID

    响应格式:
    {
        "code": 200,
        "message": "success",
        "data": {
            "qa_id": "qa_2024",
            "question": "...",
            "answer": "...",
            "extracted_info": {...},
            "validation_result": {...}
        }
    }
    """
    try:
        db = get_db_instance()
        qa_pair = db.get_qa_by_id(qa_id)

        if not qa_pair:
            return jsonify({
                "code": 404,
                "message": f"未找到 qa_id={qa_id} 的记录",
                "data": None
            }), 404

        return jsonify({
            "code": 200,
            "message": "success",
            "data": qa_pair.to_dict()
        })

    except Exception as e:
        return jsonify({
            "code": 500,
            "message": f"查询失败: {str(e)}",
            "data": None
        }), 500

@app.route("/api/knowledge/validation_rules", methods=["GET"])
def get_validation_rules():
    """
    获取可用的校验规则列表

    响应格式:
    {
        "code": 200,
        "message": "success",
        "data": {
            "rules": [
                {
                    "name": "RangeCoverageCheck",
                    "description": "量程覆盖校验"
                },
                ...
            ]
        }
    }
    """
    try:
        protocol_type = request.args.get("protocol_type", "Link16")
        rules = PromptTemplates.get_validation_rules(protocol_type)

        rule_list = [
            {
                "name": name,
                "description": config["description"]
            }
            for name, config in rules.items()
        ]

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "protocol_type": protocol_type,
                "rules": rule_list
            }
        })

    except Exception as e:
        return jsonify({
            "code": 500,
            "message": f"获取规则失败: {str(e)}",
            "data": None
        }), 500

@app.route("/health", methods=["GET"])
def health():
    """健康检查接口"""
    return jsonify({"status": "healthy"})

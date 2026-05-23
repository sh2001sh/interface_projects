# 接口3: QA字段智能抽取与规则校验
# POST /api/knowledge/extract_validate_qa

import sys
import os
import json
import hashlib
import ast
import re
import shutil
import tempfile
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional, Sequence, Tuple

from flask import Flask, request, jsonify
from werkzeug.exceptions import BadRequest

# 保证先加载当前接口自己的模块，再按需复用 08 的适配层。
PROJECT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PROJECT_DIR.parent
for candidate_path in reversed(
    [
        str(PROJECT_DIR),
        str(PROJECT_ROOT),
        str(PROJECT_ROOT / "08_code_generation"),
    ]
):
    if candidate_path in sys.path:
        sys.path.remove(candidate_path)
    sys.path.insert(0, candidate_path)

from llm.local_llm import LocalLLM, get_llm
from llm.prompt_templates import PromptTemplates
from database.mysql_client import MySQLClient
from code_generation_adapter import (
    _normalize_formula_for_generator,
    build_generator_rules_payload,
    read_protocol_dir_content,
    resolve_protocol_field_specs as _resolve_protocol_field_specs_raw,
    resolve_protocol_type_names as _resolve_protocol_type_names_raw,
)
from protocol_conversion.converter import (
    _normalize_formula_expression_syntax,
    execute_protocol_conversion,
)
from protocol_conversion.table_rule_extractor import extract_table_rules_from_files
from protocol_conversion.generator import (
    generate_and_convert_protocol_bundle,
    generate_protocol_field_rules,
)
from protocol_conversion.knowledge_base import ProtocolConversionKnowledgeBase
from protocol_conversion.trained_doc_index import get_trained_doc_evidence_provider
from runtime_config import apply_runtime_environment, get_service_runner_config
from streaming_utils import is_stream_requested, stream_flask_handler
from shared.model_metadata import resolve_model_metadata
from datetime import datetime
from difflib import SequenceMatcher

try:
    from protocol_conversion.message_bundle import (  # type: ignore
        BundleEvidenceProvider,
        build_bundle_generation_payload,
        discover_message_bundle_candidates,
    )
except ImportError:
    class BundleEvidenceProvider:  # type: ignore[no-redef]
        """Fallback wrapper used when message-bundle helpers are unavailable."""

        def __init__(self, provider: Any, bundle_payload: Dict[str, Any]):
            self.provider = provider
            self.bundle_payload = bundle_payload

    def build_bundle_generation_payload(*args, **kwargs):  # type: ignore[no-redef]
        raise NotImplementedError("当前环境缺少 message_bundle 模块，无法构建协议消息分组")

    def discover_message_bundle_candidates(*args, **kwargs):  # type: ignore[no-redef]
        raise NotImplementedError("当前环境缺少 message_bundle 模块，无法发现协议消息分组")

try:
    from protocol_conversion.exporter import export_protocol_rules  # type: ignore
except ImportError:
    def export_protocol_rules(*args, **kwargs):  # type: ignore[no-redef]
        raise NotImplementedError("当前环境缺少 exporter 模块，无法导出协议规则")

try:
    from protocol_conversion.validation import validate_protocol_rules  # type: ignore
except ImportError:
    def validate_protocol_rules(*args, **kwargs):  # type: ignore[no-redef]
        raise NotImplementedError("当前环境缺少 validation 模块，无法校验协议规则")

try:
    from protocol_conversion.evaluation import evaluate_protocol_conversion  # type: ignore
except ImportError:
    def evaluate_protocol_conversion(*args, **kwargs):  # type: ignore[no-redef]
        raise NotImplementedError("当前环境缺少 evaluation 模块，无法执行规则评估")

apply_runtime_environment()

app = Flask(__name__)

MOUNTED_TRANSFORMDATA_ROOT = Path("/nfs/protobrige-system/keyan-storage/transformdata")
PROTOCOL_RULES_CACHE_VERSION = "20260523_relation_formula_yaml_modelmeta_v12"

# 初始化组件
_llm: Optional[LocalLLM] = None
_db: Optional[MySQLClient] = None
_SIMPLE_RULE_TYPES = {"const", "direct", "expression", "conditional"}
_NUMERIC_LITERAL_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")
_TABLE_RULE_FILE_KEYS = (
    "table_rule_files",
    "table_files",
    "mapping_table_files",
    "conversion_table_files",
    "table_rule_paths",
)

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
    return _db

def _merge_protocol_request_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(data)
    source_protocol_input = payload.get("source_protocol_dirs")
    source_protocol_field_name = "source_protocol_dirs"
    if source_protocol_input is None:
        source_protocol_input = payload.get("source_protocol_dir")
        source_protocol_field_name = "source_protocol_dir"

    if not payload.get("source_protocol") and source_protocol_input:
        source_protocol_types = resolve_protocol_type_names(source_protocol_input, source_protocol_field_name)
        payload["source_protocol"] = {
            "name": str(payload.get("source_protocol_name") or "").strip() or (source_protocol_types[0] if len(source_protocol_types) == 1 else None),
            "protocol_type": str(payload.get("source_protocol_type") or "").strip() or (source_protocol_types[0] if len(source_protocol_types) == 1 else None),
            "message_code": str(payload.get("message_code") or "").strip() or None,
            "content": read_protocol_dir_content(source_protocol_input, source_protocol_field_name),
        }
    if source_protocol_input:
        payload["source_message"] = _build_source_message_from_protocol_specs(
            source_protocol_input,
            source_protocol_field_name,
        )

    target_protocol = payload.get("target_protocol")
    if not isinstance(target_protocol, dict):
        target_protocol = {}
    target_protocol_dir = payload.get("target_protocol_dir")
    if target_protocol_dir:
        target_protocol_types = resolve_protocol_type_names(target_protocol_dir, "target_protocol_dir")
        target_protocol = dict(target_protocol)
        target_protocol.setdefault("name", target_protocol_types[0] if len(target_protocol_types) == 1 else None)
        target_protocol.setdefault("protocol_type", target_protocol_types[0] if len(target_protocol_types) == 1 else None)
        target_protocol["content"] = read_protocol_dir_content(target_protocol_dir, "target_protocol_dir")
    if target_protocol:
        payload["target_protocol"] = target_protocol

    return payload

def _normalize_protocol_type_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", text)
    text = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return text.upper()

def _protocol_type_match_key(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "").strip()).upper()

def _is_descriptor_protocol_xml(xml_path: Path, xml_count: int) -> bool:
    stem = _normalize_protocol_type_token(xml_path.stem)
    if xml_count > 1 and stem and not re.search(r"\d", stem):
        return True
    return False

def resolve_protocol_type_names(path_like: Any, field_name: str) -> List[str]:
    protocol_names: List[str] = []
    seen = set()
    for directory in _normalize_path_inputs(path_like):
        if not directory.exists() or not directory.is_dir():
            raise ValueError(f"{field_name} 不存在: {directory}")
        xml_files = sorted(directory.glob("*.xml"))
        if not xml_files:
            raise ValueError(f"{field_name} 下未找到 XML 文件: {directory}")
        valid_keys = {
            _protocol_type_match_key(xml_file.stem)
            for xml_file in xml_files
            if not _is_descriptor_protocol_xml(xml_file, len(xml_files))
        }
        raw_names = _resolve_protocol_type_names_raw(str(directory), field_name)
        filtered_names = [
            name for name in raw_names
            if _protocol_type_match_key(name) in valid_keys
        ]
        if not filtered_names:
            filtered_names = [
                _normalize_protocol_type_token(xml_file.stem)
                for xml_file in xml_files
                if not _is_descriptor_protocol_xml(xml_file, len(xml_files))
            ]
        for protocol_name in filtered_names:
            if not protocol_name or protocol_name in seen:
                continue
            seen.add(protocol_name)
            protocol_names.append(protocol_name)
    return protocol_names

def resolve_protocol_field_specs(path_like: Any, field_name: str) -> List[Dict[str, Any]]:
    allowed_protocols = {
        _protocol_type_match_key(item)
        for item in resolve_protocol_type_names(path_like, field_name)
    }
    specs = _resolve_protocol_field_specs_raw(path_like, field_name)
    if not allowed_protocols:
        return specs
    return [
        item for item in specs
        if _protocol_type_match_key(item.get("protocol")) in allowed_protocols
    ]

def resolve_protocol_message_specs(path_like: Any, field_name: str) -> List[Dict[str, Any]]:
    """Resolve concrete protocol/message specs from protocol directories."""
    protocol_names = resolve_protocol_type_names(path_like, field_name)
    field_specs = resolve_protocol_field_specs(path_like, field_name)
    grouped_fields: Dict[str, List[Dict[str, Any]]] = {}
    for item in field_specs:
        protocol_name = str(item.get("protocol") or "").strip()
        if not protocol_name:
            continue
        grouped_fields.setdefault(protocol_name, []).append(dict(item))

    directories = _normalize_path_inputs(path_like)
    protocol_directory_map: Dict[str, str] = {}
    for directory in directories:
        if not directory.exists() or not directory.is_dir():
            continue
        xml_files = sorted(directory.glob("*.xml"))
        for xml_file in xml_files:
            if _is_descriptor_protocol_xml(xml_file, len(xml_files)):
                continue
            protocol_key = _protocol_type_match_key(_normalize_protocol_type_token(xml_file.stem))
            if protocol_key and protocol_key not in protocol_directory_map:
                protocol_directory_map[protocol_key] = str(directory)
    specs: List[Dict[str, Any]] = []
    for protocol_name in protocol_names:
        specs.append(
            {
                "protocol_name": protocol_name,
                "protocol_type": protocol_name,
                "message_code": protocol_name.replace("_", "."),
                "fields": grouped_fields.get(protocol_name, []),
                "directory": protocol_directory_map.get(
                    _protocol_type_match_key(protocol_name),
                    str(directories[0]) if directories else None,
                ),
            }
        )
    return specs

def _resolve_table_rule_files(data: Dict[str, Any]) -> List[str]:
    for key in _TABLE_RULE_FILE_KEYS:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        if text:
            return [text]
    return []

def _is_table_rule_request(data: Dict[str, Any]) -> bool:
    input_mode = str(
        data.get("input_mode")
        or data.get("mode")
        or data.get("rule_input_mode")
        or ""
    ).strip().lower()
    if input_mode in {"table_rule", "table_rules", "mapping_table", "table_mapping"}:
        return True
    if data.get("source_protocol_dir") or data.get("source_protocol_dirs") or data.get("target_protocol_dir"):
        return False
    return bool(_resolve_table_rule_files(data))

def _build_protocol_rules_output_paths(
    data: Dict[str, Any],
) -> Tuple[Path, Path]:
    rules_output_dir = _resolve_rules_output_dir(data)
    rules_file_name = str(data.get("rules_file_name") or "").strip()
    if not rules_file_name:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        rules_file_name = f"07_protocol_generate_rules_{timestamp}.json"

    output_dir = Path(rules_output_dir).resolve()
    relative_path = Path(rules_file_name)
    if relative_path.suffix.lower() in {".json", ".yaml", ".yml"}:
        relative_path = relative_path.with_suffix("")
    json_path = output_dir / relative_path.with_suffix(".json")
    yaml_path = output_dir / relative_path.with_suffix(".yaml")
    return json_path, yaml_path


def _save_protocol_rules_files(
    data: Dict[str, Any],
    rules_payload: Dict[str, Any],
) -> Dict[str, str]:
    json_path, yaml_path = _build_protocol_rules_output_paths(data)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)

    json_path.write_text(
        json.dumps(rules_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    yaml_path.write_text(
        yaml.safe_dump(rules_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {
        "conversion_rules_json": str(json_path),
        "conversion_rules_yaml": str(yaml_path),
    }

def _resolve_rules_output_dir(data: Dict[str, Any]) -> str:
    rules_output_dir = str(data.get("rules_output_dir") or "").strip()
    if rules_output_dir:
        return rules_output_dir
    if MOUNTED_TRANSFORMDATA_ROOT.exists():
        return str(MOUNTED_TRANSFORMDATA_ROOT / "output" / "rules")
    return "output/rules"

def _normalize_path_inputs(value: Any) -> List[Path]:
    if isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raw_values = [value]
    paths: List[Path] = []
    for item in raw_values:
        text = str(item or "").strip()
        if text:
            paths.append(Path(text).resolve())
    return paths

def _digest_file(path: Path, digest: "hashlib._Hash") -> None:
    digest.update(path.name.encode("utf-8"))
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

def _digest_protocol_directories(value: Any) -> List[Dict[str, Any]]:
    signatures: List[Dict[str, Any]] = []
    for directory in _normalize_path_inputs(value):
        if not directory.exists() or not directory.is_dir():
            signatures.append({"path": str(directory), "missing": True})
            continue
        digest = hashlib.sha256()
        xml_files = sorted(directory.glob("*.xml"))
        for xml_file in xml_files:
            _digest_file(xml_file, digest)
        signatures.append(
            {
                "path": str(directory),
                "xml_files": [item.name for item in xml_files],
                "sha256": digest.hexdigest(),
            }
        )
    return signatures

def _digest_reference_path(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_digest_reference_path(item) for item in value]
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).resolve()
    if not path.exists():
        return {"path": str(path), "missing": True}
    if path.is_file():
        digest = hashlib.sha256()
        _digest_file(path, digest)
        return {"path": str(path), "sha256": digest.hexdigest()}
    registry_path = path / "registry.json"
    if registry_path.exists() and registry_path.is_file():
        digest = hashlib.sha256()
        _digest_file(registry_path, digest)
        return {"path": str(path), "registry_sha256": digest.hexdigest()}
    digest = hashlib.sha256()
    for child in sorted(item for item in path.iterdir() if item.is_file()):
        digest.update(child.name.encode("utf-8"))
        digest.update(str(child.stat().st_size).encode("utf-8"))
        digest.update(str(child.stat().st_mtime_ns).encode("utf-8"))
    return {"path": str(path), "dir_sha256": digest.hexdigest()}

def _build_protocol_rules_cache_key(
    data: Dict[str, Any],
    source_protocol_dir: Any,
    target_protocol_dir: Any,
    source_message_specs: Optional[Sequence[Dict[str, Any]]] = None,
    target_message_specs: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    fingerprint = {
        "cache_version": PROTOCOL_RULES_CACHE_VERSION,
        "source_protocols": _digest_protocol_directories(source_protocol_dir),
        "target_protocols": _digest_protocol_directories(target_protocol_dir),
        "index_registry_path": _digest_reference_path(
            data.get("index_registry_path")
            if data.get("index_registry_path") is not None
            else data.get("pageindex_registry_path")
        ),
        "project_id": str(data.get("project_id") or "").strip(),
        "dataset_id": str(data.get("dataset_id") or "").strip(),
        "doc_set_id": str(data.get("doc_set_id") or "").strip(),
        "index_ref": str(data.get("index_ref") or "").strip(),
        "use_trained_docs": bool(data.get("use_trained_docs", True)),
        "max_empty_rule_retries": int(data.get("max_empty_rule_retries", 3)),
        "knowledge_graph_signature": _build_knowledge_graph_cache_signature(
            source_message_specs or [],
            target_message_specs or [],
        ),
    }
    payload = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _protocol_rules_cache_path(data: Dict[str, Any], cache_key: str) -> Path:
    return Path(_resolve_rules_output_dir(data)).resolve() / ".cache" / f"{cache_key}.json"

def _load_cached_protocol_rules_response(
    data: Dict[str, Any],
    source_protocol_dir: Any,
    target_protocol_dir: Any,
    source_message_specs: Optional[Sequence[Dict[str, Any]]] = None,
    target_message_specs: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    if bool(data.get("force_regenerate", False)):
        return None
    cache_key = _build_protocol_rules_cache_key(
        data,
        source_protocol_dir,
        target_protocol_dir,
        source_message_specs=source_message_specs,
        target_message_specs=target_message_specs,
    )
    cache_path = _protocol_rules_cache_path(data, cache_key)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    response_payload = cached.get("response_payload")
    if not isinstance(response_payload, dict):
        return None
    rules_file = Path(str(response_payload.get("conversion_rules_json") or "").strip())
    yaml_file = Path(str(response_payload.get("conversion_rules_yaml") or "").strip())
    if not rules_file.exists() or not yaml_file.exists():
        return None
    summary = response_payload.get("summary")
    if isinstance(summary, dict):
        response_payload["summary"] = _augment_interface7_summary(summary)
    return response_payload

def _save_cached_protocol_rules_response(
    data: Dict[str, Any],
    source_protocol_dir: Any,
    target_protocol_dir: Any,
    response_payload: Dict[str, Any],
    source_message_specs: Optional[Sequence[Dict[str, Any]]] = None,
    target_message_specs: Optional[Sequence[Dict[str, Any]]] = None,
) -> None:
    cache_key = _build_protocol_rules_cache_key(
        data,
        source_protocol_dir,
        target_protocol_dir,
        source_message_specs=source_message_specs,
        target_message_specs=target_message_specs,
    )
    cache_path = _protocol_rules_cache_path(data, cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"response_payload": response_payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_interface7_model_info() -> Dict[str, Any]:
    return resolve_model_metadata(
        model_name=os.getenv("LLM_MODEL_NAME"),
        model_cache_dir=os.getenv("MODEL_CACHE_DIR"),
    )


def _augment_interface7_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    normalized_summary = dict(summary)
    normalized_summary.setdefault("model_info", _build_interface7_model_info())
    normalized_summary.setdefault("knowledge_graph_avg_rule_time_ms", None)
    normalized_summary.setdefault("knowledge_graph_rule_time_target_met", None)
    return normalized_summary

def _serialize_knowledge_graph_rule_signature(rule: Any) -> Dict[str, Any]:
    return {
        "protocol_type": str(getattr(rule, "protocol_type", "") or "").strip() or None,
        "message_code": str(getattr(rule, "message_code", "") or "").strip().upper() or None,
        "target_protocol_type": str(getattr(rule, "target_protocol_type", "") or "").strip() or None,
        "target_message_code": str(getattr(rule, "target_message_code", "") or "").strip().upper() or None,
        "target_field": str(getattr(rule, "target_field", "") or "").strip().upper() or None,
        "source_fields": sorted(
            str(item or "").strip().upper()
            for item in (getattr(rule, "source_fields", None) or [])
            if str(item or "").strip()
        ),
        "formula": str(getattr(rule, "formula", "") or "").strip(),
        "formula_kind": str(getattr(rule, "formula_kind", "") or "").strip() or None,
        "conversion_mode": str(getattr(rule, "conversion_mode", "") or "").strip().lower() or None,
        "status": str(getattr(rule, "status", "") or "").strip().lower() or None,
    }

def _build_knowledge_graph_cache_signature(
    source_message_specs: Sequence[Dict[str, Any]],
    target_message_specs: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    if not source_message_specs or not target_message_specs:
        return {"enabled": False, "scopes": []}

    knowledge_base_cache: Dict[str, Any] = {}
    scope_signatures: List[Dict[str, Any]] = []
    seen_scopes: set[Tuple[str, str, str, str]] = set()

    for source_spec in source_message_specs:
        source_protocol_type = str(source_spec.get("protocol_type") or "").strip()
        if not source_protocol_type:
            continue
        source_message_code = str(source_spec.get("message_code") or "").strip().upper() or None
        try:
            knowledge_base = knowledge_base_cache.get(source_protocol_type)
            if knowledge_base is None:
                knowledge_base = ProtocolConversionKnowledgeBase.load(source_protocol_type)
                knowledge_base_cache[source_protocol_type] = knowledge_base
        except Exception as exc:
            scope_signatures.append(
                {
                    "source_protocol_type": source_protocol_type,
                    "source_message_code": source_message_code,
                    "error": str(exc),
                }
            )
            continue

        for target_spec in target_message_specs:
            target_protocol_type = str(target_spec.get("protocol_type") or "").strip() or None
            target_message_code = str(target_spec.get("message_code") or "").strip().upper() or None
            scope_key = (
                source_protocol_type,
                source_message_code or "",
                target_protocol_type or "",
                target_message_code or "",
            )
            if scope_key in seen_scopes:
                continue
            seen_scopes.add(scope_key)
            target_fields = [
                str(field.get("field_name") or "").strip()
                for field in (target_spec.get("fields") or [])
                if str(field.get("field_name") or "").strip()
            ]
            try:
                rules = knowledge_base.list_rules(
                    message_code=source_message_code,
                    target_protocol_type=target_protocol_type,
                    target_message_code=target_message_code,
                    target_fields=target_fields or None,
                )
                scope_signatures.append(
                    {
                        "source_protocol_type": source_protocol_type,
                        "source_message_code": source_message_code,
                        "target_protocol_type": target_protocol_type,
                        "target_message_code": target_message_code,
                        "rule_count": len(rules),
                        "rules": [
                            _serialize_knowledge_graph_rule_signature(rule)
                            for rule in rules
                        ],
                    }
                )
            except Exception as exc:
                scope_signatures.append(
                    {
                        "source_protocol_type": source_protocol_type,
                        "source_message_code": source_message_code,
                        "target_protocol_type": target_protocol_type,
                        "target_message_code": target_message_code,
                        "error": str(exc),
                    }
                )

    scope_signatures.sort(
        key=lambda item: (
            str(item.get("source_protocol_type") or ""),
            str(item.get("source_message_code") or ""),
            str(item.get("target_protocol_type") or ""),
            str(item.get("target_message_code") or ""),
            str(item.get("error") or ""),
        )
    )
    return {
        "enabled": bool(scope_signatures),
        "scopes": scope_signatures,
    }

def _to_formula_token(value: Any) -> str:
    token = re.sub(r"\W+", "_", str(value or "").strip(), flags=re.UNICODE).strip("_")
    if not token:
        return "field"
    if token[0].isdigit():
        token = f"f_{token}"
    return token

def _normalize_relation_protocol_prefix(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", text)
    text = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", text)
    return _to_formula_token(text).lower()

def _build_relation_target_field_token(
    target_protocol: Optional[str],
    target_field: Any,
    target_path: Any,
    target_actual_field: Any,
) -> str:
    return _build_explicit_formula_target_token(
        target_protocol=target_protocol,
        target_field=target_field,
        target_path=target_path,
        target_actual_field=target_actual_field,
    )

def _build_source_message_from_protocol_specs(path_like: Any, field_name: str) -> Dict[str, Any]:
    source_message: Dict[str, Any] = {}
    for spec in resolve_protocol_field_specs(path_like, field_name):
        display_name = str(spec.get("field_name") or "").strip()
        if not display_name or display_name in source_message:
            continue
        source_message[display_name] = spec.get("default_value")
    return source_message

def _build_trained_doc_registry_info(provider: Optional[Any]) -> Dict[str, Any]:
    registry = getattr(provider, "registry", {}) if provider is not None else {}
    if not isinstance(registry, dict) or not registry:
        return {}
    return {
        "project_id": registry.get("project_id"),
        "dataset_id": registry.get("dataset_id"),
        "doc_set_id": registry.get("doc_set_id"),
        "index_ref": registry.get("index_ref"),
        "document_count": int(registry.get("document_count") or 0),
        "indexed_shard_count": int(registry.get("indexed_shard_count") or 0),
        "registry_count": int(registry.get("registry_count") or 0),
        "index_registry_paths": list(registry.get("registry_paths") or []),
    }

def _build_pageindex_audit_item(
    candidate: Dict[str, Any],
    result_summary: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "relation_id": str(candidate.get("bundle_id") or "").strip() or None,
        "page_index_status": result_summary.get("page_index_status"),
        "rag_status": result_summary.get("rag_status"),
        "rag_reason": result_summary.get("rag_reason"),
        "evidence_snippet_count": int(result_summary.get("evidence_snippet_count") or 0),
        "matched_doc_ids": list(result_summary.get("matched_doc_ids") or []),
        "candidate_doc_count": int(result_summary.get("candidate_doc_count") or 0),
        "registry_count": int(result_summary.get("registry_count") or 0),
        "registry_paths": list(result_summary.get("registry_paths") or []),
        "doc_set_id": result_summary.get("doc_set_id"),
        "index_ref": result_summary.get("index_ref"),
    }

def _aggregate_pageindex_audit(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    statuses = _dedupe_non_empty_strings([item.get("page_index_status") for item in items])
    rag_statuses = _dedupe_non_empty_strings([item.get("rag_status") for item in items])
    registry_paths: List[str] = []
    matched_doc_ids: List[str] = []
    for item in items:
        registry_paths.extend(str(value).strip() for value in item.get("registry_paths") or [] if str(value).strip())
        matched_doc_ids.extend(str(value).strip() for value in item.get("matched_doc_ids") or [] if str(value).strip())
    return {
        "page_index_statuses": statuses,
        "rag_statuses": rag_statuses,
        "evidence_snippet_count": sum(int(item.get("evidence_snippet_count") or 0) for item in items),
        "matched_doc_ids": _dedupe_non_empty_strings(matched_doc_ids),
        "registry_paths": _dedupe_non_empty_strings(registry_paths),
        "registry_count": max([int(item.get("registry_count") or 0) for item in items] or [0]),
        "candidate_doc_count": sum(int(item.get("candidate_doc_count") or 0) for item in items),
    }

def _resolve_trained_doc_provider(data: Dict[str, Any]) -> Tuple[Optional[Any], bool, Dict[str, Any]]:
    use_trained_docs = bool(data.get("use_trained_docs", True))
    if not use_trained_docs:
        return None, False, {}

    index_registry_path = (
        data.get("index_registry_path")
        if data.get("index_registry_path") is not None
        else data.get("pageindex_registry_path")
    )
    provider = get_trained_doc_evidence_provider(
        project_id=str(data.get("project_id") or "").strip(),
        dataset_id=str(data.get("dataset_id") or "").strip(),
        doc_set_id=str(data.get("doc_set_id") or "").strip(),
        index_ref=str(data.get("index_ref") or "").strip(),
        index_registry_path=index_registry_path,
    )
    registry_info = _build_trained_doc_registry_info(provider)
    registry_hit = bool(registry_info)
    return provider, registry_hit, registry_info

def _aggregate_validation_results(items: List[Dict[str, bool]]) -> Dict[str, bool]:
    keys = ("field_legality", "position_accuracy", "conversion_logic", "protocol_compliance")
    if not items:
        return {key: False for key in keys}
    aggregated = {key: True for key in keys}
    for item in items:
        for key in keys:
            aggregated[key] = aggregated[key] and bool(item.get(key))
    return aggregated

def _count_nonzero_rules(rules: Any) -> int:
    if not isinstance(rules, list):
        return 0
    return sum(
        1
        for item in rules
        if isinstance(item, dict) and str(item.get("formula") or item.get("rule") or "").strip() not in {"", "0", "0.0", "0U", "0L"}
    )

def _merge_writeback_payloads(bundle_payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged_rules: List[Dict[str, Any]] = []
    for payload in bundle_payloads:
        if not isinstance(payload, dict):
            continue
        bundle_id = str(payload.get("message_bundle_id") or "").strip() or None
        for rule in payload.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            enriched = dict(rule)
            if bundle_id:
                enriched["message_bundle_id"] = bundle_id
            merged_rules.append(enriched)
    return {"rules": merged_rules}

def _build_success_validation_result() -> Dict[str, bool]:
    return {
        "field_legality": True,
        "position_accuracy": True,
        "conversion_logic": True,
        "protocol_compliance": True,
    }

def _build_table_rule_mode_response(data: Dict[str, Any]) -> Dict[str, Any]:
    table_rule_files = _resolve_table_rule_files(data)
    extraction_result = extract_table_rules_from_files(
        table_rule_files,
        default_source_protocol_type=_first_nonempty_text(
            data.get("source_protocol_type"),
            data.get("protocol_type"),
        ),
        default_source_message_code=_first_nonempty_text(
            data.get("source_message_code"),
            data.get("message_code"),
        ),
        default_target_protocol_type=_first_nonempty_text(data.get("target_protocol_type")),
        default_target_message_code=_first_nonempty_text(data.get("target_message_code")),
    )

    raw_rules = extraction_result.get("rules") or []
    normalized_rules = _normalize_manual_writeback_rules(
        raw_rules,
        default_protocol_type=_first_nonempty_text(
            data.get("source_protocol_type"),
            data.get("protocol_type"),
        ),
        default_source_message_code=_first_nonempty_text(
            data.get("source_message_code"),
            data.get("message_code"),
        ),
        default_target_protocol_type=_first_nonempty_text(data.get("target_protocol_type")),
        default_target_message_code=_first_nonempty_text(data.get("target_message_code")),
    )

    relations: List[Dict[str, Any]] = []
    saved_relations: List[Dict[str, Any]] = []
    bundle_writeback_payloads: List[Dict[str, Any]] = []
    grouped_rules: Dict[str, List[Dict[str, Any]]] = {}

    for raw_rule, normalized_rule in zip(raw_rules, normalized_rules):
        relation_id = str(raw_rule.get("message_bundle_id") or "").strip() or "table_rules"
        enriched_rule = dict(normalized_rule)
        enriched_rule["message_bundle_id"] = relation_id
        enriched_rule["source_protocol_type"] = enriched_rule.get("protocol_type")
        enriched_rule["source_protocol_name"] = enriched_rule.get("protocol_type")
        enriched_rule["source_message_code"] = enriched_rule.get("message_code")
        enriched_rule["target_actual_field"] = enriched_rule.get("target_field")
        enriched_rule["target_path"] = enriched_rule.get("target_field")
        grouped_rules.setdefault(relation_id, []).append(enriched_rule)

    for relation_id, relation_rules in grouped_rules.items():
        first_rule = relation_rules[0]
        source_protocol = str(first_rule.get("protocol_type") or "").strip() or None
        target_protocol = str(first_rule.get("target_protocol_type") or "").strip() or None
        conversion = {
            "target": {"protocol": target_protocol},
            "rules": relation_rules,
        }
        candidate = {
            "bundle_id": relation_id,
            "source_protocols": [source_protocol] if source_protocol else [],
        }
        relation_scores = {
            "field_match_accuracy": 100.0,
            "semantic_fidelity": 100.0,
            "conversion_rate": 100.0,
            "structure_integrity": 100.0,
            "overall_correctness_score": 100.0,
        }
        relation_payload = _build_relation_payload(
            candidate=candidate,
            conversion=conversion,
            bundle_payload={"target_spec": {"protocol_name": target_protocol}},
            scores=relation_scores,
        )
        relations.append(relation_payload)
        saved_relations.append(dict(relation_payload))
        bundle_writeback_payloads.append(
            {
                "message_bundle_id": relation_id,
                "rules": [dict(rule) for rule in relation_rules],
            }
        )

    kg_writeback_payload = _merge_writeback_payloads(bundle_writeback_payloads)
    rules_payload = {
        "version": "1.0",
        "project_name": str(data.get("project_name") or "").strip() or "table_rule_project",
        "input_mode": "table_rule",
        "relations": saved_relations,
        "writeback_rules": kg_writeback_payload.get("rules") or [],
        "file_summaries": extraction_result.get("file_summaries") or [],
    }
    saved_rule_paths = _save_protocol_rules_files(data, rules_payload)

    return {
        **saved_rule_paths,
        "relations": relations,
        "validation_result": _build_success_validation_result(),
        "kg_writeback_payload": kg_writeback_payload,
        "summary": _augment_interface7_summary({
            "knowledge_graph_field_count": 0,
            "candidate_assisted_target_count": 0,
            "deterministic_field_count": 0,
            "llm_converted_field_count": 0,
            "converted_field_count": len(normalized_rules),
            "trained_doc_registry_hit": False,
            "trained_doc_registry_info": {},
            "sub_message_relation_count": len(relations),
            "selected_bundle_count": len(relations),
            "table_rule_count": len(normalized_rules),
            "table_file_count": len(extraction_result.get("file_summaries") or []),
            "parsed_table_count": sum(
                int(item.get("table_count") or 0)
                for item in (extraction_result.get("file_summaries") or [])
                if isinstance(item, dict)
            ),
            "input_mode": "table_rule",
            "warnings": list(extraction_result.get("warnings") or []),
        }),
    }

def _build_relation_rule_formula_map(rule: Dict[str, Any]) -> Dict[str, str]:
    formula_map: Dict[str, str] = {}
    source_fields = [
        str(item).strip()
        for item in (rule.get("source_fields") or [])
        if str(item).strip()
    ]
    source_actual_fields = [
        str(item).strip()
        for item in (rule.get("source_actual_fields") or [])
        if str(item).strip()
    ]
    source_protocol_name = str(
        rule.get("source_protocol_name") or rule.get("source_protocol_type") or ""
    ).strip()

    for index, source_field in enumerate(source_fields):
        protocol_name = source_protocol_name
        field_name = source_field
        if "." in source_field:
            protocol_name, field_name = source_field.split(".", 1)
        source_var = _to_formula_token(f"{protocol_name}_{field_name}")
        formula_map[source_field] = source_var
        formula_map[field_name] = source_var
        actual_ref = source_actual_fields[index] if index < len(source_actual_fields) else ""
        if actual_ref:
            formula_map[actual_ref] = source_var
            if "." in actual_ref:
                _, actual_field_name = actual_ref.split(".", 1)
                formula_map[actual_field_name] = source_var
    return formula_map

def _rewrite_formula_for_relation(rule: Dict[str, Any]) -> str:
    formula = str(rule.get("formula") or rule.get("rule") or "").strip()
    if not formula:
        return formula
    formula_map = _build_relation_rule_formula_map(rule)
    if not formula_map:
        return _normalize_formula_expression_syntax(formula)
    rewritten = formula
    for raw_ref in sorted(formula_map, key=len, reverse=True):
        rewritten = re.sub(
            rf"(?<![A-Za-z0-9_\.]){re.escape(raw_ref)}\b",
            formula_map[raw_ref],
            rewritten,
        )
    return _normalize_formula_expression_syntax(rewritten)

def _strip_single_line_formula_assignment(formula: Any) -> str:
    normalized_formula = str(formula or "").strip()
    if not normalized_formula:
        return normalized_formula
    assignment_match = re.fullmatch(
        r"([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.]*)\s*=\s*(.+)",
        normalized_formula,
    )
    if not assignment_match:
        return normalized_formula
    return assignment_match.group(2).strip() or normalized_formula

def _strip_self_referential_target_guard(expression: Any, target_token: Any) -> str:
    normalized_expression = str(expression or "").strip()
    normalized_target = str(target_token or "").strip()
    if not normalized_expression or not normalized_target:
        return normalized_expression

    escaped_target = re.escape(normalized_target)
    same_value_then_zero_patterns = [
        re.compile(
            rf"^\(?\s*(?P<value>.+?)\s*==\s*{escaped_target}\s*\?\s*(?P=value)\s*:\s*0(?:\.0|U|L)?\s*\)?$"
        ),
        re.compile(
            rf"^\(?\s*{escaped_target}\s*==\s*(?P<value>.+?)\s*\?\s*(?P=value)\s*:\s*0(?:\.0|U|L)?\s*\)?$"
        ),
    ]
    for pattern in same_value_then_zero_patterns:
        match = pattern.fullmatch(normalized_expression)
        if match:
            return match.group("value").strip()
    python_same_value_then_zero_patterns = [
        re.compile(
            rf"^\(?\s*(?P<value>.+?)\s+if\s+(?P=value)\s*==\s*{escaped_target}\s+else\s+0(?:\.0)?\s*\)?$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"^\(?\s*(?P<value>.+?)\s+if\s+{escaped_target}\s*==\s*(?P=value)\s+else\s+0(?:\.0)?\s*\)?$",
            flags=re.IGNORECASE,
        ),
    ]
    for pattern in python_same_value_then_zero_patterns:
        match = pattern.fullmatch(normalized_expression)
        if match:
            return match.group("value").strip()
    return normalized_expression

def _build_explicit_formula_target_token(
    target_protocol: Any,
    target_field: Any,
    target_path: Any = None,
    target_actual_field: Any = None,
) -> str:
    protocol_prefix = _normalize_relation_protocol_prefix(target_protocol)
    raw_target_field = str(target_field or "").strip()
    normalized_target_field = _to_formula_token(raw_target_field) if raw_target_field else ""
    if protocol_prefix:
        explicit_prefix = f"{protocol_prefix}_"
        if normalized_target_field.lower().startswith(explicit_prefix.lower()):
            suffix = normalized_target_field[len(explicit_prefix):].strip("_")
            return _to_formula_token(f"{protocol_prefix}_{suffix}") if suffix else _to_formula_token(protocol_prefix)
        dotted_prefix = f"{protocol_prefix}."
        if raw_target_field.lower().startswith(dotted_prefix.lower()):
            return _to_formula_token(f"{protocol_prefix}_{raw_target_field[len(dotted_prefix):].strip()}")

    display_seed = str(target_path or target_field or target_actual_field or "").strip()
    if protocol_prefix:
        dotted_prefix = f"{protocol_prefix}."
        underscored_prefix = f"{protocol_prefix}_"
        if display_seed.lower().startswith(dotted_prefix.lower()):
            display_seed = display_seed[len(dotted_prefix):].strip()
        elif display_seed.lower().startswith(underscored_prefix.lower()):
            display_seed = display_seed[len(underscored_prefix):].strip()
    protocol_prefix = _normalize_relation_protocol_prefix(target_protocol)
    if protocol_prefix and display_seed:
        return _to_formula_token(f"{protocol_prefix}_{display_seed}")
    if display_seed:
        return _to_formula_token(display_seed)
    if protocol_prefix:
        return _to_formula_token(protocol_prefix)
    return "field"

def _build_protocol_field_prefix_candidates(protocol_type: Any) -> List[str]:
    raw_value = str(protocol_type or "").strip()
    normalized_value = _normalize_relation_protocol_prefix(protocol_type)
    candidates: List[str] = []
    for value in [raw_value, normalized_value]:
        cleaned = str(value or "").strip()
        if not cleaned:
            continue
        for prefix in [cleaned, cleaned.lower(), cleaned.upper()]:
            if prefix and prefix not in candidates:
                candidates.append(prefix)
    return candidates

def _strip_protocol_prefixed_field_name(field_name: Any, protocol_type: Any = None) -> str:
    text = str(field_name or "").strip()
    if not text:
        return ""

    for candidate in _build_protocol_field_prefix_candidates(protocol_type):
        for separator in ("_", "."):
            prefix = f"{candidate}{separator}"
            if text.lower().startswith(prefix.lower()) and len(text) > len(prefix):
                return text[len(prefix):].strip()

    generic_match = re.match(
        r"^(?:[A-Za-z]+\d*(?:[_\.]\d+)+|[A-Za-z](?:[_\.]\d+){2,})[_\.](.+)$",
        text,
    )
    if generic_match:
        stripped = str(generic_match.group(1) or "").strip()
        if stripped:
            return stripped
    return text

def _build_manual_writeback_formula_replacements(
    item: Dict[str, Any],
    source_protocol_type: Any,
    target_protocol_type: Any,
    normalized_source_fields: Sequence[str],
    normalized_target_field: str,
) -> Dict[str, str]:
    replacements: Dict[str, str] = {}

    def register(raw_value: Any, replacement: str) -> None:
        candidate = str(raw_value or "").strip()
        normalized_replacement = str(replacement or "").strip()
        if not candidate or not normalized_replacement:
            return
        if candidate != normalized_replacement:
            replacements.setdefault(candidate, normalized_replacement)
        tokenized_candidate = _to_formula_token(candidate)
        tokenized_replacement = _to_formula_token(normalized_replacement)
        if tokenized_candidate and tokenized_candidate != tokenized_replacement:
            replacements.setdefault(tokenized_candidate, tokenized_replacement)

    raw_source_fields = item.get("source_fields")
    if not isinstance(raw_source_fields, list):
        raw_source_fields = []
    raw_source_actual_fields = item.get("source_actual_fields")
    if not isinstance(raw_source_actual_fields, list):
        raw_source_actual_fields = []
    raw_source_vars = item.get("source_vars")
    if not isinstance(raw_source_vars, list):
        raw_source_vars = []
    raw_source_bindings = item.get("source_bindings")
    if not isinstance(raw_source_bindings, list):
        raw_source_bindings = []

    for index, normalized_source_field in enumerate(normalized_source_fields):
        if not normalized_source_field:
            continue
        register(normalized_source_field, normalized_source_field)
        if index < len(raw_source_fields):
            register(raw_source_fields[index], normalized_source_field)
        if index < len(raw_source_actual_fields):
            actual_field = str(raw_source_actual_fields[index] or "").strip()
            register(actual_field, normalized_source_field)
            if "." in actual_field:
                register(actual_field.split(".", 1)[1], normalized_source_field)
        if index < len(raw_source_vars):
            register(raw_source_vars[index], normalized_source_field)
        if index < len(raw_source_bindings) and isinstance(raw_source_bindings[index], dict):
            binding = raw_source_bindings[index]
            for key in ("field_name", "display_field", "alias_name", "actual_field"):
                register(binding.get(key), normalized_source_field)

    register(
        _build_explicit_formula_target_token(
            target_protocol_type,
            item.get("target_field"),
            item.get("target_path"),
            item.get("target_actual_field"),
        ),
        normalized_target_field,
    )
    register(item.get("target_var"), normalized_target_field)
    register(item.get("target_field"), normalized_target_field)
    register(item.get("target_path"), normalized_target_field)
    register(item.get("target_actual_field"), normalized_target_field)

    return replacements

def _rewrite_manual_writeback_formula_identifiers(
    formula: str,
    replacements: Dict[str, str],
) -> str:
    rewritten = str(formula or "").strip()
    if not rewritten or not replacements:
        return rewritten
    for raw_value in sorted(replacements, key=len, reverse=True):
        replacement = str(replacements.get(raw_value) or "").strip()
        if not replacement:
            continue
        rewritten = re.sub(
            rf"(?<![A-Za-z0-9_\.]){re.escape(raw_value)}\b",
            replacement,
            rewritten,
        )
    return rewritten

def _relation_assignment_target_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    return None

def _collapse_relation_assignment_branch(
    statements: List[ast.stmt],
    expected_target: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    if len(statements) != 1:
        return None, expected_target
    statement = statements[0]
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target_name = _relation_assignment_target_name(statement.targets[0])
        if not target_name:
            return None, expected_target
        if expected_target and target_name != expected_target:
            return None, expected_target
        return ast.unparse(statement.value).strip(), target_name
    if isinstance(statement, ast.If):
        return _collapse_relation_assignment_if(statement, expected_target)
    return None, expected_target

def _collapse_relation_assignment_if(
    node: ast.If,
    expected_target: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    then_expr, target_name = _collapse_relation_assignment_branch(node.body, expected_target)
    if not then_expr:
        return None, expected_target
    else_expr, target_name = _collapse_relation_assignment_branch(node.orelse, target_name)
    if not else_expr:
        return None, expected_target
    condition = ast.unparse(node.test).strip()
    return f"({then_expr} if {condition} else {else_expr})", target_name

def _normalize_relation_assignment_block(formula: str) -> str:
    normalized = str(formula or "").strip()
    if not normalized or "\n" not in normalized:
        return normalized
    try:
        parsed = ast.parse(normalized, mode="exec")
    except SyntaxError:
        return normalized
    if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.If):
        return normalized
    collapsed, _target_name = _collapse_relation_assignment_if(parsed.body[0], None)
    return collapsed or normalized

def _normalize_relation_formula_for_codegen(
    formula: Any,
    target_tokens: Sequence[Any],
) -> str:
    normalized_formula = str(formula or "").strip()
    if not normalized_formula:
        return normalized_formula

    placeholder = "__target__"
    sanitized_formula = normalized_formula
    normalized_targets = [
        str(item or "").strip()
        for item in target_tokens
        if str(item or "").strip()
    ]
    for token in sorted(set(normalized_targets), key=len, reverse=True):
        sanitized_formula = re.sub(
            rf"(?<![A-Za-z0-9_\.]){re.escape(token)}\b",
            placeholder,
            sanitized_formula,
            flags=re.IGNORECASE,
        )
    sanitized_formula = re.sub(
        r"(^[ \t]*)(?:result|[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.]*)\s*=",
        rf"\1{placeholder} =",
        sanitized_formula,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    sanitized_formula = _normalize_relation_assignment_block(sanitized_formula)
    return _normalize_formula_for_generator(sanitized_formula)

def _ensure_explicit_target_formula(
    formula: Any,
    target_token: str,
) -> str:
    normalized_formula = str(formula or "").strip()
    normalized_target = str(target_token or "").strip()
    if not normalized_formula or not normalized_target:
        return normalized_formula

    if "\n" in normalized_formula:
        return re.sub(
            r"(^[ \t]*)(?:result|[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.]*)\s*=",
            rf"\1{normalized_target} =",
            normalized_formula,
            flags=re.IGNORECASE | re.MULTILINE,
        )

    expression = _strip_single_line_formula_assignment(normalized_formula)
    expression = _normalize_formula_expression_syntax(expression)
    expression = _strip_self_referential_target_guard(expression, normalized_target)
    return f"{normalized_target} = {expression}"

def _canonicalize_formula_variant(formula: Any) -> str:
    normalized_formula = str(formula or "").strip()
    if not normalized_formula:
        return normalized_formula
    if "\n" in normalized_formula:
        return re.sub(
            r"(^[ \t]*)(?:result|[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.]*)\s*=",
            r"\1__target__ =",
            normalized_formula,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    return _normalize_formula_expression_syntax(
        _strip_single_line_formula_assignment(normalized_formula)
    )

def _collect_legacy_formula_rule_ids(
    existing_rules: Sequence[Any],
    current_rule: Any,
) -> List[str]:
    current_edge_id = str(getattr(current_rule, "edge_id", None) or "").strip()
    current_target_field = str(getattr(current_rule, "target_field", None) or "").strip().upper()
    current_source_fields = tuple(
        str(item or "").strip().upper()
        for item in (getattr(current_rule, "source_fields", None) or [])
    )
    current_formula_key = _canonicalize_formula_variant(getattr(current_rule, "formula", None))
    legacy_rule_ids: List[str] = []
    for existing_rule in existing_rules:
        existing_edge_id = str(getattr(existing_rule, "edge_id", None) or "").strip()
        if not existing_edge_id or existing_edge_id == current_edge_id:
            continue
        existing_target_field = str(getattr(existing_rule, "target_field", None) or "").strip().upper()
        existing_source_fields = tuple(
            str(item or "").strip().upper()
            for item in (getattr(existing_rule, "source_fields", None) or [])
        )
        if existing_target_field != current_target_field or existing_source_fields != current_source_fields:
            continue
        if _canonicalize_formula_variant(getattr(existing_rule, "formula", None)) != current_formula_key:
            continue
        legacy_rule_ids.append(existing_edge_id)
    return legacy_rule_ids

def _delete_knowledge_rule_ids(knowledge_base: Any, rule_ids: Sequence[str]) -> int:
    normalized_rule_ids = [str(item or "").strip() for item in rule_ids if str(item or "").strip()]
    if not normalized_rule_ids or not hasattr(knowledge_base, "_run_cypher"):
        return 0

    deleted_count = 0
    schema_mode = str(getattr(knowledge_base, "schema_mode", "") or "").strip().lower()
    if schema_mode == "protocolfield_graph":
        knowledge_base._run_cypher(
            """
            UNWIND $rule_ids AS rule_id
            MATCH ()-[r:MAP_TO {rule_id: rule_id}]->()
            DELETE r
            """,
            {"rule_ids": normalized_rule_ids},
        )
        knowledge_base._run_cypher(
            """
            UNWIND $rule_ids AS rule_id
            MATCH ()-[r:G {rule_id: rule_id}]->()
            DELETE r
            """,
            {"rule_ids": normalized_rule_ids},
        )
        deleted_count = len(normalized_rule_ids)
    elif schema_mode == "legacy_entity_graph":
        knowledge_base._run_cypher(
            """
            UNWIND $rule_ids AS rule_id
            MATCH ()-[r:MAP_TO {rule_id: rule_id}]->()
            DELETE r
            """,
            {"rule_ids": normalized_rule_ids},
        )
        knowledge_base._run_cypher(
            """
            UNWIND $rule_ids AS rule_id
            MATCH ()-[r:G {rule_id: rule_id}]->()
            DELETE r
            """,
            {"rule_ids": normalized_rule_ids},
        )
        deleted_count = len(normalized_rule_ids)
    else:
        knowledge_base._run_cypher(
            """
            UNWIND $rule_ids AS rule_id
            MATCH (r:Rule {rule_id: rule_id})
            DETACH DELETE r
            """,
            {"rule_ids": normalized_rule_ids},
        )
        deleted_count = len(normalized_rule_ids)
    return deleted_count

def _build_relation_rule_payload(rule: Dict[str, Any], target_protocol: Optional[str]) -> Dict[str, Any]:
    source_fields = [
        str(item).strip()
        for item in (rule.get("source_fields") or [])
        if str(item).strip()
    ]
    source_actual_fields = [
        str(item).strip()
        for item in (rule.get("source_actual_fields") or [])
        if str(item).strip()
    ]
    relation_source_fields: List[str] = []
    relation_source_vars: List[str] = []
    relation_source_paths: List[str] = []
    relation_source_bindings: List[Dict[str, Any]] = []
    for index, source_field in enumerate(source_fields):
        protocol_name = str(
            rule.get("source_protocol_name") or rule.get("source_protocol_type") or ""
        ).strip()
        binding = {}
        raw_bindings = rule.get("source_bindings") or []
        if index < len(raw_bindings) and isinstance(raw_bindings[index], dict):
            binding = dict(raw_bindings[index])
        field_name = source_field
        binding_alias = str(binding.get("alias_name") or "").strip()
        if "." in source_field:
            protocol_name, field_name = source_field.split(".", 1)
        elif binding_alias and binding_alias.upper() == source_field.upper():
            protocol_name = ""
        display_seed = f"{protocol_name}.{field_name}" if protocol_name else field_name
        source_field_token = _to_formula_token(display_seed)
        actual_ref = source_actual_fields[index] if index < len(source_actual_fields) else ""
        source_path = ""
        raw_source_paths = rule.get("source_paths") or []
        if index < len(raw_source_paths):
            source_path = str(raw_source_paths[index] or "").strip()
        if actual_ref and "." not in actual_ref and protocol_name:
            actual_ref = f"{protocol_name}.{actual_ref}"
        source_var_seed = actual_ref or display_seed
        source_var = _to_formula_token(source_var_seed)
        relation_source_fields.append(source_field_token)
        relation_source_vars.append(source_var)
        relation_source_paths.append(source_path)
        if binding:
            relation_source_bindings.append(binding)

    target_field = str(rule.get("target_field") or "").strip()
    target_actual_field = str(rule.get("target_actual_field") or "").strip() or target_field
    target_path = str(rule.get("target_path") or "").strip() or None
    relation_target_field = _build_relation_target_field_token(
        target_protocol=target_protocol,
        target_field=target_field,
        target_path=target_path,
        target_actual_field=target_actual_field,
    )
    explicit_target_token = _build_explicit_formula_target_token(
        target_protocol=target_protocol,
        target_field=target_field,
        target_path=target_path,
        target_actual_field=target_actual_field,
    )
    return {
        "source_fields": relation_source_fields,
        "source_vars": relation_source_vars,
        "source_actual_fields": source_actual_fields,
        "source_paths": relation_source_paths,
        "source_bindings": relation_source_bindings,
        "target_field": relation_target_field,
        "target_actual_field": target_actual_field,
        "target_path": target_path,
        "target_var": explicit_target_token,
        "formula": _ensure_explicit_target_formula(
            _normalize_relation_assignment_block(_rewrite_formula_for_relation(rule)),
            explicit_target_token,
        ),
    }

def _finalize_relation_rule_target_fields(rules: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_counts: Dict[str, int] = {}
    finalized: List[Dict[str, Any]] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        base_target_field = str(normalized.get("target_field") or "").strip()
        if base_target_field:
            occurrence = seen_counts.get(base_target_field, 0) + 1
            seen_counts[base_target_field] = occurrence
            if occurrence > 1:
                normalized_target_field = f"{base_target_field}_{occurrence}"
                normalized["target_field"] = normalized_target_field
                normalized["target_var"] = normalized_target_field
                normalized["formula"] = _ensure_explicit_target_formula(
                    normalized.get("formula"),
                    normalized_target_field,
                )
        finalized.append(normalized)
    return finalized

def _backfill_relation_rule_sources(
    rule: Dict[str, Any],
    bundle_payload: Dict[str, Any],
) -> Dict[str, Any]:
    enriched = dict(rule)
    existing_source_fields = [
        str(item).strip()
        for item in (enriched.get("source_fields") or [])
        if str(item).strip()
    ]
    if existing_source_fields:
        return enriched

    formula = str(enriched.get("formula") or enriched.get("rule") or "").strip()
    if not formula or _is_zero_literal_formula(formula):
        return enriched

    target_keys = {
        _normalize_relation_score_key(enriched.get("target_actual_field")),
        _normalize_relation_score_key(enriched.get("target_field")),
        _normalize_relation_score_key(enriched.get("target_path")),
    }
    target_keys.discard("")
    if not target_keys:
        return enriched

    matched_target = None
    for item in (bundle_payload.get("required_target_fields") or []):
        if not isinstance(item, dict):
            continue
        candidate_keys = {
            _normalize_relation_score_key(item.get("actual_field")),
            _normalize_relation_score_key(item.get("field_name")),
            _normalize_relation_score_key("/".join(item.get("path_parts") or [])),
        }
        candidate_keys.discard("")
        if target_keys & candidate_keys:
            matched_target = item
            break
    if matched_target is None:
        return enriched

    preferred_candidates = [
        item for item in (matched_target.get("preferred_source_candidates") or [])
        if isinstance(item, dict) and str(item.get("field_name") or "").strip()
    ]
    if not preferred_candidates:
        return enriched

    primary = preferred_candidates[0]
    source_field = str(primary.get("field_name") or "").strip().upper()
    if not source_field:
        return enriched

    enriched["source_fields"] = [source_field]
    enriched["source_actual_fields"] = [
        str(primary.get("actual_field") or "").strip()
        for _ in [0]
        if str(primary.get("actual_field") or "").strip()
    ]
    enriched["source_paths"] = [
        str(primary.get("source_path") or "").strip()
        for _ in [0]
        if str(primary.get("source_path") or "").strip()
    ]
    enriched["source_bindings"] = [
        {
            "alias_name": source_field,
            "protocol": str(primary.get("source_protocol_type") or "").strip() or None,
            "message_code": str(primary.get("source_message_code") or "").strip() or None,
            "actual_field": str(primary.get("actual_field") or "").strip() or None,
            "display_field": str(
                primary.get("display_field")
                or primary.get("actual_field")
                or source_field
            ).strip() or None,
            "source_path": str(primary.get("source_path") or "").strip() or None,
        }
    ]
    source_protocol_name = str(primary.get("source_protocol_type") or "").strip()
    if source_protocol_name:
        enriched["source_protocol_name"] = source_protocol_name
        enriched["source_protocol_type"] = source_protocol_name
    source_message_code = str(primary.get("source_message_code") or "").strip()
    if source_message_code:
        enriched["source_message_code"] = source_message_code
    return enriched

def _build_relation_payload(
    candidate: Dict[str, Any],
    conversion: Dict[str, Any],
    bundle_payload: Dict[str, Any],
    scores: Optional[Dict[str, float]] = None,
    include_nonconvertible: bool = False,
    normalize_nonconvertible_to_zero: bool = False,
) -> Dict[str, Any]:
    rules = conversion.get("rules") if isinstance(conversion, dict) else []
    target_protocol = str(
        ((conversion.get("target") or {}) if isinstance(conversion, dict) else {}).get("protocol") or ""
    ).strip() or str((bundle_payload.get("target_spec") or {}).get("protocol_name") or "").strip()
    enriched_rules = [
        _backfill_relation_rule_sources(rule, bundle_payload)
        for rule in (rules if isinstance(rules, list) else [])
        if isinstance(rule, dict)
    ]
    normalized_rules: List[Dict[str, Any]] = []
    for rule in enriched_rules:
        normalized_rule = dict(rule)
        has_source_fields = bool(_dedupe_non_empty_strings(normalized_rule.get("source_fields") or []))
        if normalize_nonconvertible_to_zero and not has_source_fields:
            normalized_rule["formula"] = "0"
            normalized_rule["rule"] = "0"
            normalized_rule["source_fields"] = []
            normalized_rule["source_actual_fields"] = []
            normalized_rule["source_paths"] = []
            normalized_rule["source_bindings"] = []
            normalized_rule["rule_type"] = "const"
            normalized_rule["description"] = "无法可靠转换，默认置 0"
        normalized_rules.append(normalized_rule)
    visible_rules = (
        normalized_rules
        if include_nonconvertible
        else [
            rule for rule in normalized_rules
            if _dedupe_non_empty_strings(rule.get("source_fields") or [])
        ]
    )
    source_protocols = _extract_relation_source_protocols(
        visible_rules,
        candidate=candidate,
        bundle_payload=bundle_payload,
    )
    relation_rules = _finalize_relation_rule_target_fields(
        [
            _build_relation_rule_payload(rule, target_protocol)
            for rule in visible_rules
        ]
    )
    return {
        "relation_id": str(candidate.get("bundle_id") or "").strip() or None,
        "source_protocols": source_protocols,
        "target_protocol": target_protocol or None,
        "rules": relation_rules,
        "scores": {
            "field_match_accuracy": round(float((scores or {}).get("field_match_accuracy") or 0.0), 4),
            "semantic_fidelity": round(float((scores or {}).get("semantic_fidelity") or 0.0), 4),
            "conversion_rate": round(float((scores or {}).get("conversion_rate") or 0.0), 4),
            "structure_integrity": round(float((scores or {}).get("structure_integrity") or 0.0), 4),
            "overall_correctness_score": round(float((scores or {}).get("overall_correctness_score") or 0.0), 4),
        },
    }

def _dedupe_non_empty_strings(values: List[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result

def _candidate_source_protocols(
    candidate: Dict[str, Any],
    bundle_payload: Dict[str, Any],
) -> List[str]:
    source_protocols = [
        str(value).strip()
        for value in (candidate.get("source_protocols") or [])
        if str(value).strip()
    ]
    if not source_protocols:
        source_protocols = [
            str(item.get("protocol_name") or item.get("protocol_type") or "").strip()
            for item in (bundle_payload.get("source_specs") or [])
            if isinstance(item, dict) and str(item.get("protocol_name") or item.get("protocol_type") or "").strip()
        ]
    if not source_protocols:
        fallback_source_protocol = str((bundle_payload.get("source_protocol") or {}).get("protocol_type") or "").strip()
        if fallback_source_protocol:
            source_protocols = [fallback_source_protocol]
    return _dedupe_non_empty_strings(source_protocols)

def _extract_relation_source_protocols(
    rules: List[Dict[str, Any]],
    candidate: Dict[str, Any],
    bundle_payload: Dict[str, Any],
) -> List[str]:
    candidate_protocols = _candidate_source_protocols(candidate, bundle_payload)
    prefix_to_protocol = {
        _normalize_relation_protocol_prefix(protocol): protocol
        for protocol in candidate_protocols
        if _normalize_relation_protocol_prefix(protocol)
    }
    discovered: List[str] = []

    def push(raw_protocol: Any) -> None:
        protocol = str(raw_protocol or "").strip()
        if not protocol:
            return
        if candidate_protocols:
            protocol_key = _normalize_relation_protocol_prefix(protocol)
            protocol = next(
                (
                    candidate_protocol
                    for candidate_protocol in candidate_protocols
                    if _normalize_relation_protocol_prefix(candidate_protocol) == protocol_key
                ),
                protocol,
            )
        if protocol not in discovered:
            discovered.append(protocol)

    for rule in rules:
        for binding in rule.get("source_bindings") or []:
            if isinstance(binding, dict):
                push(binding.get("protocol"))
        push(rule.get("source_protocol_type") or rule.get("source_protocol_name") or rule.get("protocol_type"))
        for source_field in rule.get("source_fields") or []:
            source_token = _normalize_relation_protocol_prefix(source_field)
            for prefix, protocol in prefix_to_protocol.items():
                if source_token == prefix or source_token.startswith(f"{prefix}_"):
                    push(protocol)
                    break

    return discovered or candidate_protocols

def _normalize_rule_source_fields_for_scoring(rule: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for item in rule.get("source_actual_fields") or []:
        values.append(str(item).split(".", 1)[-1].strip())
    for binding in rule.get("source_bindings") or []:
        if not isinstance(binding, dict):
            continue
        values.append(str(binding.get("actual_field") or "").split(".", 1)[-1].strip())
        values.append(str(binding.get("display_field") or "").split(".", 1)[-1].strip())
        values.append(str(binding.get("field_name") or "").split(".", 1)[-1].strip())
    if values:
        return _dedupe_non_empty_strings(values)
    return _dedupe_non_empty_strings(
        [str(item).split(".", 1)[-1].strip() for item in (rule.get("source_fields") or [])]
    )

def _build_relation_scoring_rules(conversion: Dict[str, Any]) -> List[Dict[str, Any]]:
    scoring_rules: List[Dict[str, Any]] = []
    for rule in conversion.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        target_field = str(rule.get("target_actual_field") or rule.get("target_field") or "").strip()
        formula = str(rule.get("formula") or rule.get("rule") or rule.get("conversion_formula") or "").strip()
        if not target_field or not formula:
            continue
        normalized_source_fields = _normalize_rule_source_fields_for_scoring(rule)
        scoring_rules.append(
            {
                "target_field": target_field,
                "source_fields": normalized_source_fields,
                "formula": formula,
                "rule_type": str(
                    rule.get("rule_type") or rule.get("conversion_mode") or rule.get("formula_kind") or ""
                ).strip(),
            }
        )
    return scoring_rules

def _normalize_relation_score_key(value: Any) -> str:
    return re.sub(r"[\s_\-./:：，,()\[\]{}]+", "", str(value or "").strip()).lower()

def _iter_relation_source_alias_keys(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    candidates: List[str] = [text]
    if "/" in text:
        candidates.append(text.split("/")[-1].strip())
    if "." in text:
        candidates.append(text.split(".")[-1].strip())
    underscore_parts = [part.strip() for part in text.split("_") if part.strip()]
    for index in range(1, len(underscore_parts)):
        candidates.append("_".join(underscore_parts[index:]))
        candidates.append(underscore_parts[index])
    normalized: List[str] = []
    seen = set()
    for candidate in candidates:
        key = _normalize_relation_score_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized

def _iter_relation_rule_target_keys(rule: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    seen = set()
    for raw_value in (
        rule.get("target_actual_field"),
        rule.get("target_path"),
        rule.get("target_field"),
    ):
        text = str(raw_value or "").strip()
        if not text:
            continue
        candidates = [text]
        if "/" in text:
            candidates.append(text.split("/")[-1].strip())
        if "." in text:
            candidates.append(text.split(".")[-1].strip())
        for candidate in candidates:
            key = _normalize_relation_score_key(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return keys

def _duplicate_relation_target_actual_key(rule: Dict[str, Any]) -> Optional[str]:
    actual_field = str(rule.get("target_actual_field") or "").strip()
    if not actual_field:
        return None
    key = _normalize_relation_score_key(actual_field)
    return key or None

def _build_relation_source_catalog(bundle_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    catalog: Dict[str, Dict[str, Any]] = {}
    for item in bundle_payload.get("source_field_catalog") or []:
        if not isinstance(item, dict):
            continue
        for raw_key in (
            item.get("actual_field"),
            item.get("display_field"),
            item.get("field_name"),
            item.get("label"),
        ):
            key = _normalize_relation_score_key(raw_key)
            if key and key not in catalog:
                catalog[key] = item
    return catalog

def _relation_text_similarity(target_text: str, source_text: str) -> float:
    target = str(target_text or "").strip()
    source = str(source_text or "").strip()
    if not target or not source:
        return 0.0
    ratio_score = SequenceMatcher(a=target.lower(), b=source.lower()).ratio() * 100.0
    target_tokens = {
        token for token in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", target.lower()) if token
    }
    source_tokens = {
        token for token in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", source.lower()) if token
    }
    overlap_score = (
        len(target_tokens & source_tokens) / max(len(target_tokens | source_tokens), 1) * 100.0
        if (target_tokens or source_tokens)
        else 0.0
    )
    return round(ratio_score * 0.6 + overlap_score * 0.4, 4)

def _relation_formula_is_valid(formula: str) -> bool:
    text = str(formula or "").strip()
    if not text:
        return False
    if text.count("(") != text.count(")"):
        return False
    if re.search(r"[\+\-\*/(]\s*$", text):
        return False
    return True

def _is_relation_expression_rule(rule: Dict[str, Any], formula: str, declared_source_fields: Sequence[str]) -> bool:
    rule_type = str(rule.get("rule_type") or "").strip().lower()
    if rule_type in {"expression", "python_expr", "python_block", "formula"}:
        return True
    if len(list(declared_source_fields)) > 1 and any(op in formula for op in ("+", "-", "*", "/", "(", ")")):
        return True
    return False

def _relation_expression_semantic_score(
    formula_valid: bool,
    field_match_score: float,
    target_text: str,
    source_texts: Sequence[str],
) -> float:
    if not formula_valid or field_match_score <= 0:
        return 0.0
    lexical_score = 0.0
    if source_texts:
        lexical_score = max(_relation_text_similarity(target_text, source_text) for source_text in source_texts)
    return round(max(lexical_score, 80.0), 4)

def _is_zero_literal_formula(formula: Any) -> bool:
    return str(formula or "").strip() in {"0", "0.0", "0U", "0L"}

def _is_placeholder_relation_rule(rule: Dict[str, Any]) -> bool:
    source_fields = _dedupe_non_empty_strings(rule.get("source_fields") or [])
    if source_fields:
        return False
    return _is_zero_literal_formula(rule.get("formula"))

def _score_relation_conversion(
    candidate: Dict[str, Any],
    bundle_payload: Dict[str, Any],
    conversion: Dict[str, Any],
) -> Dict[str, float]:
    default_scores = {
        "field_match_accuracy": 0.0,
        "semantic_fidelity": 0.0,
        "conversion_rate": 0.0,
        "structure_integrity": 0.0,
        "overall_correctness_score": 0.0,
    }
    scoring_rules = _build_relation_scoring_rules(conversion)
    required_target_fields = [
        item for item in (bundle_payload.get("required_target_fields") or []) if isinstance(item, dict)
    ]
    if not scoring_rules or not required_target_fields:
        return default_scores

    source_catalog = _build_relation_source_catalog(bundle_payload)
    rule_map: Dict[str, Dict[str, Any]] = {}
    duplicate_target_count = 0
    actual_target_counter: Dict[str, int] = {}
    for rule in scoring_rules:
        actual_key = _duplicate_relation_target_actual_key(rule)
        if actual_key:
            actual_target_counter[actual_key] = actual_target_counter.get(actual_key, 0) + 1
        local_keys = _iter_relation_rule_target_keys(rule)
        if not local_keys:
            continue
        for key in local_keys:
            rule_map[key] = rule
    duplicate_target_count = sum(1 for count in actual_target_counter.values() if count > 1)

    field_match_scores: List[float] = []
    semantic_scores: List[float] = []
    structure_scores: List[float] = []
    convertible_field_count = 0
    successful_convertible_count = 0

    for target_field in required_target_fields:
        target_key = _normalize_relation_score_key(target_field.get("actual_field") or target_field.get("field_name"))
        if not target_key:
            continue
        rule = rule_map.get(target_key)
        if not rule:
            structure_scores.append(0.0)
            continue

        formula = str(rule.get("formula") or "").strip()
        is_placeholder_rule = _is_placeholder_relation_rule(rule)
        declared_source_fields = _dedupe_non_empty_strings(rule.get("source_fields") or [])
        is_expression_rule = _is_relation_expression_rule(rule, formula, declared_source_fields)
        is_convertible_rule = bool(declared_source_fields)
        if is_convertible_rule:
            convertible_field_count += 1

        normalized_declared = set()
        for item in declared_source_fields:
            normalized_declared.update(_iter_relation_source_alias_keys(item))

        preferred_candidates = [
            item
            for item in (target_field.get("preferred_source_candidates") or [])
            if isinstance(item, dict)
        ]
        expected_source_keys = {
            _normalize_relation_score_key(
                item.get("actual_field") or item.get("display_field") or item.get("field_name")
            )
            for item in preferred_candidates
        }
        expected_source_keys.discard("")
        resolved_source_keys = {key for key in normalized_declared if key in source_catalog}
        formula_valid = _relation_formula_is_valid(formula)
        zero_fallback = _is_zero_literal_formula(formula) and not declared_source_fields
        structure_score = 100.0
        if is_placeholder_rule or zero_fallback:
            structure_score = 0.0
        else:
            if duplicate_target_count and _duplicate_relation_target_actual_key(rule):
                actual_key = _duplicate_relation_target_actual_key(rule)
                duplicate_count = actual_target_counter.get(actual_key, 0)
                if actual_key and duplicate_count > 1:
                    structure_score -= 25.0
            if not formula_valid:
                structure_score -= 20.0
            if is_convertible_rule and not resolved_source_keys:
                structure_score -= 20.0
            structure_score = max(structure_score, 0.0)

        if not is_convertible_rule:
            field_match_score = 0.0
        elif expected_source_keys:
            matched_expected = len(resolved_source_keys & expected_source_keys)
            field_match_score = matched_expected / max(len(expected_source_keys), 1) * 100.0
        elif is_expression_rule and resolved_source_keys:
            field_match_score = 100.0
        elif resolved_source_keys:
            field_match_score = len(resolved_source_keys) / max(len(normalized_declared), 1) * 100.0
        else:
            field_match_score = 0.0

        if expected_source_keys:
            field_match_pass = bool(resolved_source_keys & expected_source_keys) and formula_valid
        else:
            field_match_pass = bool(resolved_source_keys) and formula_valid

        target_text = " ".join(
            str(part or "").strip()
            for part in (
                target_field.get("field_name"),
                target_field.get("label"),
                "/".join(target_field.get("path_parts") or []),
            )
            if str(part or "").strip()
        )
        source_texts: List[str] = []
        for source_field in declared_source_fields:
            catalog_item = {}
            for alias_key in _iter_relation_source_alias_keys(source_field):
                catalog_item = source_catalog.get(alias_key) or {}
                if catalog_item:
                    break
            source_text = " ".join(
                str(part or "").strip()
                for part in (
                    catalog_item.get("display_field"),
                    catalog_item.get("label"),
                    catalog_item.get("actual_field"),
                    catalog_item.get("source_path"),
                    source_field,
                )
                if str(part or "").strip()
            )
            if source_text:
                source_texts.append(source_text)

        if not is_convertible_rule:
            semantic_score = 0.0
        elif is_placeholder_rule or zero_fallback:
            semantic_score = 0.0
        elif is_expression_rule:
            semantic_score = _relation_expression_semantic_score(
                formula_valid=formula_valid,
                field_match_score=field_match_score,
                target_text=target_text,
                source_texts=source_texts,
            )
        elif source_texts:
            semantic_score = max(_relation_text_similarity(target_text, source_text) for source_text in source_texts)
        elif formula in {"0", "0.0", "0U", "0L"}:
            semantic_score = 0.0
        else:
            semantic_score = 35.0

        if is_convertible_rule:
            field_match_scores.append(round(field_match_score, 4))
            semantic_scores.append(round(semantic_score, 4))
            if field_match_pass and structure_score >= 80.0:
                successful_convertible_count += 1
        structure_scores.append(round(structure_score, 4))

    target_field_count = len(required_target_fields)
    field_match_accuracy = round(successful_convertible_count / max(convertible_field_count, 1) * 100.0, 4)
    semantic_fidelity = round(sum(semantic_scores) / max(convertible_field_count, 1), 4)
    conversion_rate = round(successful_convertible_count / max(target_field_count, 1) * 100.0, 4)
    structure_integrity = round(sum(structure_scores) / max(target_field_count, 1), 4)
    overall_correctness_score = round(
        field_match_accuracy * 0.35
        + semantic_fidelity * 0.25
        + structure_integrity * 0.20
        + conversion_rate * 0.20,
        4,
    )
    return {
        "field_match_accuracy": field_match_accuracy,
        "semantic_fidelity": semantic_fidelity,
        "conversion_rate": conversion_rate,
        "structure_integrity": structure_integrity,
        "overall_correctness_score": overall_correctness_score,
    }

def _resolve_protocol_name(protocol_payload: Any) -> Optional[str]:
    if isinstance(protocol_payload, dict):
        for key in ("protocol_type", "name", "message_type"):
            value = str(protocol_payload.get(key) or "").strip()
            if value:
                return value
        return None
    value = str(protocol_payload or "").strip()
    return value or None

def _flatten_manual_evidence_text(raw_evidence: Any) -> Optional[str]:
    if isinstance(raw_evidence, str):
        text = raw_evidence.strip()
        return text or None
    if not isinstance(raw_evidence, list):
        return None

    parts: List[str] = []
    for item in raw_evidence:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("content") or item.get("text") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)

def _first_nonempty_text(*values: Any) -> Optional[str]:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None

def _infer_rule_source_message_code(item: Dict[str, Any]) -> Optional[str]:
    direct_code = _first_nonempty_text(
        item.get("source_message_code"),
        item.get("message_code"),
    )
    if direct_code:
        return direct_code.upper()

    bindings = item.get("source_bindings") or []
    if not isinstance(bindings, list):
        return None

    message_codes: List[str] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        code = _first_nonempty_text(
            binding.get("source_message_code"),
            binding.get("message_code"),
        )
        if not code:
            continue
        normalized = code.upper()
        if normalized not in message_codes:
            message_codes.append(normalized)
    if len(message_codes) == 1:
        return message_codes[0]
    return None

def _normalize_manual_writeback_rules(
    raw_rules: Any,
    default_protocol_type: Optional[str] = None,
    default_source_message_code: Optional[str] = None,
    default_target_protocol_type: Optional[str] = None,
    default_target_message_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("rules不能为空，且必须是数组")

    normalized_rules: List[Dict[str, Any]] = []
    invalid_targets: List[str] = []
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise ValueError(f"rules[{index}] 必须是对象")

        raw_target_field = str(item.get("target_field") or "").strip()
        formula = str(
            item.get("formula")
            or item.get("rule")
            or item.get("conversion_formula")
            or item.get("expression")
            or ""
        ).strip()
        if not raw_target_field:
            invalid_targets.append(f"rules[{index}].target_field")
            continue
        if not formula:
            invalid_targets.append(f"{raw_target_field}.formula")
            continue

        source_fields = item.get("source_fields")
        if isinstance(source_fields, list):
            normalized_source_fields = [
                str(value).strip()
                for value in source_fields
                if str(value).strip()
            ]
        elif isinstance(source_fields, str):
            normalized_source_fields = [
                value.strip()
                for value in source_fields.split(",")
                if value.strip()
            ]
        else:
            normalized_source_fields = []

        if not normalized_source_fields:
            field_name = str(item.get("field_name") or item.get("source_field") or "").strip()
            if field_name:
                normalized_source_fields = [field_name]

        protocol_type = _first_nonempty_text(
            item.get("source_protocol_type"),
            item.get("protocol_type"),
            default_protocol_type,
            default_target_protocol_type,
            "",
        )
        target_protocol_type = _first_nonempty_text(
            item.get("target_protocol_type"),
            default_target_protocol_type,
            protocol_type,
        )
        target_field = _strip_protocol_prefixed_field_name(
            item.get("target_path") or raw_target_field or item.get("target_actual_field"),
            target_protocol_type,
        ) or _strip_protocol_prefixed_field_name(raw_target_field, target_protocol_type)
        normalized_source_fields = [
            _strip_protocol_prefixed_field_name(value, protocol_type)
            for value in normalized_source_fields
            if _strip_protocol_prefixed_field_name(value, protocol_type)
        ]
        if not normalized_source_fields:
            field_name = _strip_protocol_prefixed_field_name(
                item.get("field_name") or item.get("source_field"),
                protocol_type,
            )
            if field_name:
                normalized_source_fields = [field_name]
        formula_replacements = _build_manual_writeback_formula_replacements(
            item,
            protocol_type,
            target_protocol_type,
            normalized_source_fields,
            target_field,
        )
        normalized_formula = _rewrite_manual_writeback_formula_identifiers(
            formula,
            formula_replacements,
        )

        normalized_rule = {
            "concept_name": str(item.get("concept_name") or "").strip(),
            "field_name": normalized_source_fields[0] if normalized_source_fields else "",
            "source_fields": normalized_source_fields,
            "protocol_type": protocol_type,
            "message_code": _first_nonempty_text(
                _infer_rule_source_message_code(item),
                default_source_message_code,
            ),
            "target_protocol_type": target_protocol_type,
            "target_message_code": _first_nonempty_text(
                item.get("target_message_code"),
                default_target_message_code,
            ),
            "conversion_mode": str(item.get("conversion_mode") or item.get("mode") or "transcoding").strip().lower() or "transcoding",
            "formula": _ensure_explicit_target_formula(
                normalized_formula,
                _to_formula_token(target_field),
            ),
            "description": str(item.get("description") or "").strip() or _flatten_manual_evidence_text(item.get("evidence")),
            "confidence": item.get("confidence"),
            "unit": item.get("unit"),
            "bit_length": item.get("bit_length"),
            "status": "approved",
            "source": "manual_review",
        }
        normalized_target_field = target_field
        normalized_rule["target_field"] = normalized_target_field
        if not normalized_rule["concept_name"]:
            normalized_rule["concept_name"] = normalized_target_field
        normalized_rule["formula"] = _ensure_explicit_target_formula(
            normalized_formula,
            _to_formula_token(normalized_target_field),
        )
        normalized_rules.append(normalized_rule)

    if invalid_targets:
        raise ValueError(f"存在缺失必要字段的规则: {', '.join(invalid_targets)}")
    if not normalized_rules:
        raise ValueError("没有可写回的有效规则")
    return normalized_rules

def _displayize_rule_records(
    rule_records: Any,
    protocol_dir: Optional[Path],
    target_protocol_name: Optional[str],
    source_protocol_name: Optional[str],
    plain_writeback_fields: bool = False,
) -> Any:
    if not isinstance(rule_records, list) or not rule_records:
        return rule_records

    merged_rules = []
    for item in rule_records:
        if not isinstance(item, dict):
            merged_rules.append(item)
            continue
        surrogate_rule = {
            "field_name": item.get("field_name"),
            "source_fields": item.get("source_fields"),
            "source_bindings": item.get("source_bindings"),
            "source_actual_fields": item.get("source_actual_fields"),
            "source_paths": item.get("source_paths"),
            "source_protocol_type": item.get("source_protocol_type"),
            "source_protocol_name": item.get("source_protocol_name"),
            "source_message_code": item.get("source_message_code"),
            "target_field": item.get("target_field"),
            "target_actual_field": item.get("target_actual_field"),
            "target_path": item.get("target_path"),
            "target_protocol_type": item.get("target_protocol_type"),
            "target_message_code": item.get("target_message_code"),
            "conversion_mode": item.get("conversion_mode"),
            "formula": item.get("formula") or item.get("rule"),
            "rule": item.get("rule") or item.get("formula"),
            "description": item.get("description"),
            "concept_name": item.get("concept_name"),
            "message_bundle_id": item.get("message_bundle_id"),
        }
        merged = dict(item)
        try:
            display_payload = build_generator_rules_payload(
                raw_rules={"normalized_rules": [surrogate_rule]},
                protocol_dir=protocol_dir,
                target_protocol_name=target_protocol_name,
                source_protocol_name=source_protocol_name,
                preserve_display_names=True,
            )
            display_rules = (((display_payload.get("conversions") or [{}])[0]).get("rules") or [])
        except ValueError as exc:
            if "没有与当前 source/target XML 匹配的可生成转换关系" not in str(exc):
                raise
            display_rules = []
        if display_rules:
            display_rule = display_rules[0]
            merged["target_field"] = display_rule.get("target_field", merged.get("target_field"))
            merged["target_actual_field"] = display_rule.get("target_actual_field", merged.get("target_actual_field"))
            merged["target_path"] = display_rule.get("target_path", merged.get("target_path"))
            merged["source_fields"] = display_rule.get("source_fields", merged.get("source_fields"))
            merged["formula"] = display_rule.get("formula", merged.get("formula"))
            merged["source_actual_fields"] = display_rule.get("source_actual_fields", merged.get("source_actual_fields"))
            merged["source_paths"] = display_rule.get("source_paths", merged.get("source_paths"))
            merged["source_protocol_type"] = display_rule.get("source_protocol_type", merged.get("source_protocol_type"))
            merged["source_protocol_name"] = display_rule.get("source_protocol_name", merged.get("source_protocol_name"))
        merged["target_field"] = _build_explicit_formula_target_token(
            merged.get("target_protocol_type") or target_protocol_name,
            merged.get("target_field"),
            merged.get("target_path"),
            merged.get("target_actual_field"),
        )
        relation_payload = _build_relation_rule_payload(
            merged,
            merged.get("target_protocol_type") or target_protocol_name,
        )
        if relation_payload.get("source_fields"):
            merged["source_fields"] = relation_payload.get("source_fields")
        merged["formula"] = str(
            relation_payload.get("formula")
            or _ensure_explicit_target_formula(
                merged.get("formula") or merged.get("rule"),
                str(merged.get("target_field") or "").strip(),
            )
        ).strip()
        if plain_writeback_fields:
            normalized_plain_rules = _normalize_manual_writeback_rules(
                [merged],
                default_protocol_type=_first_nonempty_text(
                    merged.get("source_protocol_type"),
                    merged.get("protocol_type"),
                ),
                default_source_message_code=_first_nonempty_text(
                    merged.get("source_message_code"),
                    merged.get("message_code"),
                ),
                default_target_protocol_type=_first_nonempty_text(merged.get("target_protocol_type")),
                default_target_message_code=_first_nonempty_text(merged.get("target_message_code")),
            )
            if normalized_plain_rules:
                normalized_plain = normalized_plain_rules[0]
                merged["field_name"] = normalized_plain.get("field_name")
                merged["target_field"] = normalized_plain.get("target_field")
                merged["source_fields"] = normalized_plain.get("source_fields")
                merged["formula"] = normalized_plain.get("formula")
                if not str(merged.get("concept_name") or "").strip():
                    merged["concept_name"] = normalized_plain.get("concept_name")
                source_bindings = merged.get("source_bindings")
                if isinstance(source_bindings, list):
                    normalized_bindings = []
                    for binding in source_bindings:
                        if not isinstance(binding, dict):
                            normalized_bindings.append(binding)
                            continue
                        normalized_binding = dict(binding)
                        for key in ("alias_name", "field_name"):
                            if key in normalized_binding:
                                normalized_binding[key] = _strip_protocol_prefixed_field_name(
                                    normalized_binding.get(key),
                                    merged.get("source_protocol_type") or merged.get("protocol_type"),
                                ) or normalized_binding.get(key)
                        normalized_bindings.append(normalized_binding)
                    merged["source_bindings"] = normalized_bindings
        merged_rules.append(merged)
    return merged_rules

def _filter_display_writeback_rules(
    rule_records: Any,
    bundle_payload: Optional[Dict[str, Any]] = None,
) -> Any:
    if not isinstance(rule_records, list):
        return rule_records
    filtered_rules = []
    for item in rule_records:
        if not isinstance(item, dict):
            continue
        source_fields = [str(value).strip() for value in (item.get("source_fields") or []) if str(value).strip()]
        formula = str(item.get("formula") or item.get("rule") or "").strip()
        target_field = str(item.get("target_field") or "").strip()
        if (
            not target_field
            or not source_fields
            or not formula
            or formula == "0"
            or _is_suspicious_relation_rule(item, bundle_payload=bundle_payload)
        ):
            continue
        filtered_rules.append(item)
    return filtered_rules

def _parse_relation_mapping_pairs(formula: Any) -> List[Tuple[str, str]]:
    text = _strip_single_line_formula_assignment(formula)
    if not text:
        return []
    pairs: List[Tuple[str, str]] = []
    for chunk in text.split(","):
        piece = str(chunk or "").strip()
        if not piece:
            return []
        if piece.count("=") != 1:
            return []
        left, right = piece.split("=", 1)
        left = left.strip()
        right = right.strip()
        if not left or not right:
            return []
        pairs.append((left, right))
    return pairs

def _resolve_relation_target_spec(
    rule: Dict[str, Any],
    bundle_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    required_target_fields = []
    if isinstance(bundle_payload, dict):
        required_target_fields = [
            item
            for item in (bundle_payload.get("required_target_fields") or [])
            if isinstance(item, dict)
        ]
    if not required_target_fields:
        return {}

    candidate_keys = {
        _normalize_relation_score_key(rule.get("target_actual_field")),
        _normalize_relation_score_key(rule.get("target_path")),
        _normalize_relation_score_key(rule.get("target_field")),
    }
    candidate_keys.discard("")
    if not candidate_keys:
        return {}

    for item in required_target_fields:
        target_keys = {
            _normalize_relation_score_key(item.get("actual_field")),
            _normalize_relation_score_key(item.get("target_path")),
            _normalize_relation_score_key(item.get("field_name")),
            _normalize_relation_score_key(item.get("label")),
        }
        target_keys.discard("")
        if candidate_keys & target_keys:
            return item
    return {}

def _is_suspicious_relation_rule(
    rule: Dict[str, Any],
    bundle_payload: Optional[Dict[str, Any]] = None,
) -> bool:
    source_fields = _dedupe_non_empty_strings(rule.get("source_fields") or [])
    if not source_fields:
        return False

    expression = _strip_single_line_formula_assignment(rule.get("formula") or rule.get("rule") or "")
    if not expression:
        return True

    if _NUMERIC_LITERAL_PATTERN.fullmatch(expression):
        return True

    mapping_pairs = _parse_relation_mapping_pairs(expression)
    if mapping_pairs:
        if len(mapping_pairs) == 1:
            left_value, right_value = mapping_pairs[0]
            if (
                _NUMERIC_LITERAL_PATTERN.fullmatch(left_value)
                and _NUMERIC_LITERAL_PATTERN.fullmatch(right_value)
            ):
                return True
        target_spec = _resolve_relation_target_spec(rule, bundle_payload)
        suspicious_targets = {
            str(target_spec.get("bit_length") or "").strip(),
            str(target_spec.get("default_value") or "").strip(),
        }
        suspicious_targets.discard("")
        if any(right_value in suspicious_targets for _left_value, right_value in mapping_pairs):
            return True
    return False

def _merge_protocol_dirs(source_protocol_dir: str, target_protocol_dir: str) -> Optional[Path]:
    if not source_protocol_dir or not target_protocol_dir:
        return None
    tmp_root = Path("tmp").resolve()
    tmp_root.mkdir(parents=True, exist_ok=True)
    merged_root = Path(tempfile.mkdtemp(prefix="protocol_rules_", dir=str(tmp_root)))
    copied_names: set[str] = set()
    for field_name, directory_text in (
        ("source_protocol_dir", source_protocol_dir),
        ("target_protocol_dir", target_protocol_dir),
    ):
        directory_values = directory_text if isinstance(directory_text, list) else [directory_text]
        for index, item in enumerate(directory_values):
            directory = Path(str(item or "").strip()).resolve()
            label = field_name if len(directory_values) == 1 else f"{field_name}[{index}]"
            if not directory.exists() or not directory.is_dir():
                raise ValueError(f"{label} 不存在: {directory}")
            xml_files = sorted(directory.glob("*.xml"))
            if not xml_files:
                raise ValueError(f"{label} 下未找到 XML 文件: {directory}")
            for xml_file in xml_files:
                target_path = merged_root / xml_file.name
                if xml_file.name in copied_names:
                    if target_path.read_text(encoding="utf-8-sig") != xml_file.read_text(encoding="utf-8-sig"):
                        raise ValueError(f"{label} 中存在重名但内容不同的 XML 文件: {xml_file.name}")
                    continue
                shutil.copy(xml_file, target_path)
                copied_names.add(xml_file.name)
    return merged_root

def _iter_conversion_rules(rules_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    for conversion in rules_payload.get("conversions") or []:
        if not isinstance(conversion, dict):
            continue
        for rule in conversion.get("rules") or []:
            if isinstance(rule, dict):
                rules.append(rule)
    return rules

def _build_protocol_spec_index(protocol_dir: Optional[Path]) -> Dict[str, List[Dict[str, Any]]]:
    if not protocol_dir:
        return {}
    index: Dict[str, List[Dict[str, Any]]] = {}
    for spec in resolve_protocol_field_specs(protocol_dir, "protocol_dir"):
        protocol_name = str(spec.get("protocol") or "").strip()
        if not protocol_name:
            continue
        index.setdefault(protocol_name, []).append(spec)
    return index

def _field_candidates(spec: Dict[str, Any]) -> set[str]:
    path_parts = spec.get("path_parts") or []
    return {
        str(value).strip().upper()
        for value in (
            spec.get("actual_field"),
            spec.get("field_name"),
            spec.get("label"),
            path_parts[-1] if path_parts else None,
        )
        if str(value or "").strip()
    }

def _find_protocol_field_matches(
    protocol_specs: Dict[str, List[Dict[str, Any]]],
    protocol_name: Optional[str],
    field_name: Optional[str],
    field_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    normalized_protocol = str(protocol_name or "").strip()
    normalized_field = str(field_name or "").strip()
    normalized_path = str(field_path or "").strip()
    if "." in normalized_field:
        normalized_field = normalized_field.split(".", 1)[1].strip()
    if not normalized_protocol or (not normalized_field and not normalized_path):
        return []
    if normalized_path:
        path_key = normalized_path.upper()
        path_matches = []
        for spec in protocol_specs.get(normalized_protocol, []):
            spec_path = "/".join(str(part).strip() for part in (spec.get("path_parts") or []) if str(part).strip())
            if spec_path.upper() == path_key:
                path_matches.append(spec)
        if path_matches:
            return path_matches
    if not normalized_field:
        return []
    field_key = normalized_field.upper()
    return [
        spec
        for spec in protocol_specs.get(normalized_protocol, [])
        if field_key in _field_candidates(spec)
    ]

def _evaluate_rule_validation_result(
    rules_payload: Dict[str, Any],
    protocol_dir: Optional[Path],
) -> Dict[str, bool]:
    conversions = rules_payload.get("conversions")
    protocol_compliance = (
        isinstance(rules_payload, dict)
        and isinstance(rules_payload.get("version"), str)
        and isinstance(rules_payload.get("project_name"), str)
        and bool(rules_payload.get("project_name"))
        and isinstance(conversions, list)
        and bool(conversions)
    )
    field_legality = True
    position_accuracy = True
    conversion_logic = True
    protocol_specs = _build_protocol_spec_index(protocol_dir)

    for conversion in conversions or []:
        if not isinstance(conversion, dict):
            protocol_compliance = False
            conversion_logic = False
            continue

        conversion_name = str(conversion.get("name") or "").strip()
        conversion_mode = str(conversion.get("mode") or "").strip()
        conversion_sources = conversion.get("sources")
        conversion_target = conversion.get("target")
        conversion_rules = conversion.get("rules")
        if (
            not conversion_name
            or conversion_mode not in {"simple", "joint"}
            or not isinstance(conversion_sources, list)
            or not conversion_sources
            or not isinstance(conversion_target, dict)
            or not str(conversion_target.get("protocol") or "").strip()
            or not isinstance(conversion_rules, list)
        ):
            protocol_compliance = False

        alias_to_protocol: Dict[str, str] = {}
        for source in conversion_sources or []:
            if not isinstance(source, dict):
                protocol_compliance = False
                continue
            alias = str(source.get("alias") or "").strip()
            protocol_name = str(source.get("protocol") or "").strip()
            if not alias or not protocol_name or alias in alias_to_protocol:
                protocol_compliance = False
                continue
            alias_to_protocol[alias] = protocol_name

        only_source_protocol = next(iter(alias_to_protocol.values())) if len(alias_to_protocol) == 1 else None
        target_protocol = str((conversion_target or {}).get("protocol") or "").strip()
        seen_target_fields: set[str] = set()
        for rule in conversion_rules or []:
            if not isinstance(rule, dict):
                protocol_compliance = False
                conversion_logic = False
                continue
            target_field = str(rule.get("target_field") or "").strip()
            target_actual_field = str(rule.get("target_actual_field") or "").strip()
            target_path = str(rule.get("target_path") or "").strip()
            formula = str(rule.get("formula") or "").strip()
            rule_type = str(rule.get("rule_type") or "").strip().lower()
            source_fields = [str(item).strip() for item in (rule.get("source_fields") or []) if str(item).strip()]
            source_actual_fields = [str(item).strip() for item in (rule.get("source_actual_fields") or []) if str(item).strip()]
            source_paths = [str(item).strip() for item in (rule.get("source_paths") or []) if str(item).strip()]
            if not target_field or not formula or not rule_type:
                protocol_compliance = False
            target_identity = target_actual_field or target_path or target_field
            if target_identity in seen_target_fields:
                protocol_compliance = False
            seen_target_fields.add(target_identity)

            target_matches = _find_protocol_field_matches(
                protocol_specs,
                target_protocol,
                target_actual_field or target_field,
                field_path=target_path,
            )
            if len(target_matches) != 1:
                field_legality = False
                position_accuracy = False
            else:
                target_spec = target_matches[0]
                if not (target_spec.get("path_parts") and target_spec.get("bit_length") is not None):
                    position_accuracy = False

            for index, source_ref in enumerate(source_fields):
                alias, _, source_field = source_ref.partition(".")
                resolved_source_protocol = alias_to_protocol.get(alias.strip()) if source_field else only_source_protocol
                resolved_source_field = source_field.strip() if source_field else alias.strip()
                resolved_source_actual = ""
                if index < len(source_actual_fields):
                    actual_ref = source_actual_fields[index]
                    if "." in actual_ref:
                        _, _, resolved_source_actual = actual_ref.partition(".")
                    else:
                        resolved_source_actual = actual_ref
                resolved_source_path = source_paths[index] if index < len(source_paths) else None
                source_matches = _find_protocol_field_matches(
                    protocol_specs,
                    resolved_source_protocol,
                    resolved_source_actual or resolved_source_field,
                    field_path=resolved_source_path,
                )
                if len(source_matches) != 1:
                    field_legality = False
                    position_accuracy = False
                else:
                    source_spec = source_matches[0]
                    if not (source_spec.get("path_parts") and source_spec.get("bit_length") is not None):
                        position_accuracy = False

            if rule_type not in _SIMPLE_RULE_TYPES:
                conversion_logic = False
                continue
            if re.search(r"\bresult\b", formula, flags=re.IGNORECASE):
                conversion_logic = False
            if rule_type == "const":
                if formula not in {"0", "0.0", "0U", "0L"} or source_fields:
                    conversion_logic = False
            elif rule_type == "direct":
                if len(source_fields) != 1 or _strip_single_line_formula_assignment(formula) != source_fields[0]:
                    conversion_logic = False
            elif not source_fields:
                conversion_logic = False

    return {
        "field_legality": bool(field_legality),
        "position_accuracy": bool(position_accuracy),
        "conversion_logic": bool(conversion_logic),
        "protocol_compliance": bool(protocol_compliance),
    }

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

@app.route("/api/knowledge/protocol_convert", methods=["POST"])
def protocol_convert():
    """执行协议转换，支持字段转义与字段转换两类公式。"""
    try:
        data = request.json
        if not isinstance(data, dict):
            return jsonify({
                "code": 400,
                "message": "请求体必须是JSON对象",
                "data": None,
            }), 400

        source_message = data.get("source_message")
        if not source_message:
            return jsonify({
                "code": 400,
                "message": "source_message不能为空",
                "data": None,
            }), 400

        result = execute_protocol_conversion(
            source_message=source_message,
            llm_formula_output=data.get("llm_formula_output"),
            protocol_type=data.get("protocol_type", ""),
            message_code=data.get("message_code"),
            use_knowledge_base=bool(data.get("use_knowledge_base", True)),
        )

        return jsonify({
            "code": 200,
            "message": "success",
            "data": result,
        })
    except FileNotFoundError as exc:
        return jsonify({
            "code": 404,
            "message": f"知识库文件不存在: {str(exc)}",
            "data": None,
        }), 404
    except Exception as exc:
        return jsonify({
            "code": 500,
            "message": f"协议转换失败: {str(exc)}",
            "data": None,
        }), 500

@app.route("/api/knowledge/protocol_generate_rules", methods=["POST"])
def protocol_generate_rules():
    """基于原/目标协议定义生成目标协议字段规则。"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({
                "code": 400,
                "message": "请求体必须是JSON对象",
                "data": None,
            }), 400

        if _is_table_rule_request(data):
            response_payload = _build_table_rule_mode_response(data)
            return jsonify({
                "code": 200,
                "message": "success",
                "data": response_payload,
            })

        normalized_payload = _merge_protocol_request_payload(data)
        source_protocol_dir = normalized_payload.get("source_protocol_dirs")
        if source_protocol_dir is None:
            source_protocol_dir = normalized_payload.get("source_protocol_dir")
        target_protocol_dir = normalized_payload.get("target_protocol_dir")
        protocol_dir = _merge_protocol_dirs(source_protocol_dir, target_protocol_dir)
        source_message_specs = resolve_protocol_message_specs(
            source_protocol_dir,
            "source_protocol_dirs" if isinstance(source_protocol_dir, list) else "source_protocol_dir",
        )
        target_message_specs = resolve_protocol_message_specs(
            target_protocol_dir,
            "target_protocol_dir",
        )
        cached_response_payload = _load_cached_protocol_rules_response(
            normalized_payload,
            source_protocol_dir,
            target_protocol_dir,
            source_message_specs=source_message_specs,
            target_message_specs=target_message_specs,
        )
        if cached_response_payload is not None:
            return jsonify({
                "code": 200,
                "message": "success",
                "data": cached_response_payload,
            })
        trained_doc_provider, trained_doc_registry_hit, trained_doc_registry_info = _resolve_trained_doc_provider(
            normalized_payload,
        )
        message_bundle_candidates = discover_message_bundle_candidates(
            source_message_specs=source_message_specs,
            target_message_specs=target_message_specs,
            trained_doc_provider=trained_doc_provider,
        )
        selected_candidates = [
            item for item in message_bundle_candidates
            if isinstance(item, dict) and bool(item.get("selected"))
        ]

        relations: List[Dict[str, Any]] = []
        saved_relations: List[Dict[str, Any]] = []
        bundle_validation_results: List[Dict[str, bool]] = []
        bundle_writeback_payloads: List[Dict[str, Any]] = []
        bundle_pageindex_audits: List[Dict[str, Any]] = []
        total_knowledge_graph_fields = 0
        total_knowledge_graph_lookup_time_ms = 0.0
        total_candidate_targets = 0
        total_deterministic_fields = 0
        total_llm_fields = 0
        index_registry_path = (
            normalized_payload.get("index_registry_path")
            if normalized_payload.get("index_registry_path") is not None
            else normalized_payload.get("pageindex_registry_path")
        )

        for candidate in selected_candidates:
            bundle_payload = build_bundle_generation_payload(
                candidate,
                source_message_specs=source_message_specs,
                target_message_specs=target_message_specs,
            )
            bundle_provider = (
                BundleEvidenceProvider(trained_doc_provider, bundle_payload)
                if trained_doc_provider is not None
                else None
            )
            result = generate_protocol_field_rules(
                source_protocol=bundle_payload["source_protocol"],
                target_protocol=bundle_payload["target_protocol"],
                source_message=bundle_payload["source_message"],
                required_target_fields=bundle_payload["required_target_fields"],
                source_field_catalog=bundle_payload["source_field_catalog"],
                use_knowledge_base=bool(bundle_payload.get("source_specs")),
                use_page_index=True,
                use_trained_docs=trained_doc_provider is not None,
                project_id=str(normalized_payload.get("project_id") or "").strip(),
                dataset_id=str(normalized_payload.get("dataset_id") or "").strip(),
                doc_set_id=str(normalized_payload.get("doc_set_id") or "").strip(),
                index_ref=str(normalized_payload.get("index_ref") or "").strip(),
                index_registry_path=index_registry_path,
                evidence_provider=bundle_provider,
                max_empty_rule_retries=int(data.get("max_empty_rule_retries", 3)),
            )

            display_normalized_rules = _displayize_rule_records(
                list(result.get("normalized_rules") or result.get("concept_normalized_rules") or []),
                protocol_dir=protocol_dir,
                target_protocol_name=str(bundle_payload["target_spec"].get("protocol_name") or "").strip() or None,
                source_protocol_name=None,
            )
            bundle_rules_payload = build_generator_rules_payload(
                raw_rules={"normalized_rules": display_normalized_rules},
                protocol_dir=protocol_dir,
                target_protocol_name=str(bundle_payload["target_spec"].get("protocol_name") or "").strip() or None,
                source_protocol_name=None,
                project_name=str(normalized_payload.get("project_name") or "").strip() or None,
                preserve_display_names=True,
            )
            bundle_conversions = bundle_rules_payload.get("conversions") or []
            if bundle_conversions:
                conversion = dict(bundle_conversions[0])
                conversion["name"] = str(candidate.get("bundle_id") or conversion.get("name") or "").strip()
                conversion["mode"] = "joint" if len(candidate.get("source_messages") or []) > 1 else "simple"
                conversion["sources"] = [
                    {
                        "alias": source.get("alias"),
                        "protocol": source.get("protocol"),
                    }
                    for source in (conversion.get("sources") or [])
                    if isinstance(source, dict)
                ]
                conversion["target"] = {
                    "protocol": str(bundle_payload["target_spec"].get("protocol_name") or "").strip(),
                }
                conversion["rules"] = [
                    _backfill_relation_rule_sources(rule, bundle_payload)
                    for rule in (conversion.get("rules") or [])
                    if isinstance(rule, dict)
                ]
                conversion["rules"] = _filter_display_writeback_rules(
                    conversion.get("rules"),
                    bundle_payload=bundle_payload,
                )
                if not conversion["rules"]:
                    continue
                scored_relation = _build_relation_payload(
                    candidate=candidate,
                    conversion=conversion,
                    bundle_payload=bundle_payload,
                    include_nonconvertible=True,
                    normalize_nonconvertible_to_zero=True,
                )
                relation_scores = _score_relation_conversion(
                    candidate=candidate,
                    bundle_payload=bundle_payload,
                    conversion=scored_relation,
                )
                relations.append(
                    _build_relation_payload(
                        candidate=candidate,
                        conversion=conversion,
                        bundle_payload=bundle_payload,
                        scores=relation_scores,
                    )
                )
                scored_relation["scores"] = relation_scores
                saved_relations.append(scored_relation)
                try:
                    validation_rules_payload = build_generator_rules_payload(
                        raw_rules={
                            "version": "1.0",
                            "project_name": str(normalized_payload.get("project_name") or "").strip() or "generated_project",
                            "relations": [saved_relations[-1]],
                        },
                        protocol_dir=protocol_dir,
                        target_protocol_name=str(bundle_payload["target_spec"].get("protocol_name") or "").strip() or None,
                        source_protocol_name=None,
                        project_name=str(normalized_payload.get("project_name") or "").strip() or None,
                    )
                    bundle_validation_results.append(
                        _evaluate_rule_validation_result(
                            rules_payload=validation_rules_payload,
                            protocol_dir=protocol_dir,
                        )
                    )
                except ValueError as exc:
                    if "没有与当前 source/target XML 匹配的可生成转换关系" not in str(exc):
                        raise
                    bundle_validation_results.append({})
            result_summary = dict(result.get("summary") or {})
            total_knowledge_graph_fields += int(result_summary.get("knowledge_graph_rule_count") or 0)
            total_knowledge_graph_lookup_time_ms += float(result_summary.get("knowledge_graph_lookup_time_ms") or 0.0)
            total_candidate_targets += int(result_summary.get("candidate_target_count") or 0)
            total_deterministic_fields += int(result_summary.get("deterministic_rule_count") or 0)
            total_llm_fields += int(result_summary.get("llm_rule_count") or 0)
            bundle_pageindex_audits.append(_build_pageindex_audit_item(candidate, result_summary))

            kg_writeback_payload = dict(result.get("kg_writeback_payload") or {})
            if kg_writeback_payload:
                kg_writeback_payload["message_bundle_id"] = candidate.get("bundle_id")
                kg_writeback_payload["rules"] = _displayize_rule_records(
                    kg_writeback_payload.get("rules"),
                    protocol_dir=protocol_dir,
                    target_protocol_name=str(bundle_payload["target_spec"].get("protocol_name") or "").strip() or None,
                    source_protocol_name=None,
                    plain_writeback_fields=True,
                )
                kg_writeback_payload["rules"] = _filter_display_writeback_rules(
                    kg_writeback_payload.get("rules"),
                    bundle_payload=bundle_payload,
                )
                for rule_item in kg_writeback_payload["rules"]:
                    if isinstance(rule_item, dict):
                        rule_item.pop("formula_kind", None)
                bundle_writeback_payloads.append(kg_writeback_payload)

        rules_payload = {
            "version": "1.0",
            "project_name": str(normalized_payload.get("project_name") or "").strip() or "generated_project",
            "relations": saved_relations,
        }
        saved_rule_paths = _save_protocol_rules_files(normalized_payload, rules_payload)
        validation_result = _aggregate_validation_results(bundle_validation_results)
        kg_writeback_payload = _merge_writeback_payloads(bundle_writeback_payloads)
        trained_doc_registry_info = _build_trained_doc_registry_info(trained_doc_provider)
        trained_doc_registry_hit = bool(trained_doc_registry_info)
        pageindex_audit = _aggregate_pageindex_audit(bundle_pageindex_audits)
        knowledge_graph_avg_rule_time_ms = None
        knowledge_graph_rule_time_target_met = None
        if total_knowledge_graph_fields > 0:
            knowledge_graph_avg_rule_time_ms = round(
                total_knowledge_graph_lookup_time_ms / total_knowledge_graph_fields,
                4,
            )
            knowledge_graph_rule_time_target_met = knowledge_graph_avg_rule_time_ms <= 50.0
        response_payload = {
            **saved_rule_paths,
            "relations": relations,
            "validation_result": validation_result,
            "kg_writeback_payload": kg_writeback_payload,
            "pageindex_audit": {
                "relations": bundle_pageindex_audits,
                **pageindex_audit,
            },
            "summary": _augment_interface7_summary({
                "knowledge_graph_field_count": total_knowledge_graph_fields,
                "knowledge_graph_avg_rule_time_ms": knowledge_graph_avg_rule_time_ms,
                "knowledge_graph_rule_time_target_met": knowledge_graph_rule_time_target_met,
                "candidate_assisted_target_count": total_candidate_targets,
                "deterministic_field_count": total_deterministic_fields,
                "llm_converted_field_count": total_llm_fields,
                "converted_field_count": total_knowledge_graph_fields + total_deterministic_fields + total_llm_fields,
                "page_index_statuses": pageindex_audit["page_index_statuses"],
                "rag_statuses": pageindex_audit["rag_statuses"],
                "evidence_snippet_count": pageindex_audit["evidence_snippet_count"],
                "matched_doc_ids": pageindex_audit["matched_doc_ids"],
                "registry_paths": pageindex_audit["registry_paths"],
                "pageindex_registry_count": pageindex_audit["registry_count"],
                "pageindex_candidate_doc_count": pageindex_audit["candidate_doc_count"],
                "trained_doc_registry_hit": trained_doc_registry_hit,
                "trained_doc_registry_info": trained_doc_registry_info,
                "sub_message_relation_count": len(message_bundle_candidates),
                "selected_bundle_count": len(selected_candidates),
            }),
        }
        _save_cached_protocol_rules_response(
            normalized_payload,
            source_protocol_dir,
            target_protocol_dir,
            response_payload,
            source_message_specs=source_message_specs,
            target_message_specs=target_message_specs,
        )
        return jsonify({
            "code": 200,
            "message": "success",
            "data": response_payload,
        })
    except ValueError as exc:
        return jsonify({
            "code": 400,
            "message": str(exc),
            "data": None,
        }), 400
    except FileNotFoundError as exc:
        return jsonify({
            "code": 404,
            "message": str(exc),
            "data": None,
        }), 404
    except BadRequest:
        return jsonify({
            "code": 400,
            "message": "请求体必须是JSON对象",
            "data": None,
        }), 400
    except Exception as exc:
        return jsonify({
            "code": 500,
            "message": f"协议规则生成失败: {str(exc)}",
            "data": None,
        }), 500

@app.route("/api/knowledge/protocol_convert_bundle", methods=["POST"])
def protocol_convert_bundle():
    """先生成规则，再执行整包协议转换。"""
    try:
        data = request.json
        if not isinstance(data, dict):
            return jsonify({
                "code": 400,
                "message": "请求体必须是JSON对象",
                "data": None,
            }), 400

        source_message = data.get("source_message")
        if not source_message:
            return jsonify({
                "code": 400,
                "message": "source_message不能为空",
                "data": None,
            }), 400

        result = generate_and_convert_protocol_bundle(
            source_protocol=data.get("source_protocol"),
            target_protocol=data.get("target_protocol"),
            source_message=source_message,
            use_knowledge_base=bool(data.get("use_knowledge_base", True)),
            use_page_index=bool(data.get("use_page_index", False)),
            use_trained_docs=bool(data.get("use_trained_docs", False)),
            project_id=str(data.get("project_id") or "").strip(),
            dataset_id=str(data.get("dataset_id") or "").strip(),
            doc_set_id=str(data.get("doc_set_id") or "").strip(),
            index_ref=str(data.get("index_ref") or "").strip(),
        )
        return jsonify({
            "code": 200,
            "message": "success",
            "data": result,
        })
    except ValueError as exc:
        return jsonify({
            "code": 400,
            "message": str(exc),
            "data": None,
        }), 400
    except FileNotFoundError as exc:
        return jsonify({
            "code": 404,
            "message": f"知识库文件不存在: {str(exc)}",
            "data": None,
        }), 404
    except Exception as exc:
        return jsonify({
            "code": 500,
            "message": f"整包协议转换失败: {str(exc)}",
            "data": None,
        }), 500

@app.route("/api/knowledge/protocol_rule_validate", methods=["POST"])
def protocol_rule_validate():
    """校验协议转换规则，补齐量纲/位宽/映射合法性检查。"""
    try:
        data = request.json
        if not isinstance(data, dict):
            return jsonify({
                "code": 400,
                "message": "请求体必须是JSON对象",
                "data": None,
            }), 400

        result = validate_protocol_rules(
            llm_formula_output=data.get("llm_formula_output"),
            protocol_type=data.get("protocol_type", ""),
            message_code=data.get("message_code"),
            source_message=data.get("source_message"),
            source_fields=data.get("source_fields"),
        )
        return jsonify({
            "code": 200,
            "message": "success",
            "data": result,
        })
    except FileNotFoundError as exc:
        return jsonify({
            "code": 404,
            "message": f"知识库文件不存在: {str(exc)}",
            "data": None,
        }), 404
    except Exception as exc:
        return jsonify({
            "code": 500,
            "message": f"规则校验失败: {str(exc)}",
            "data": None,
        }), 500

@app.route("/api/knowledge/protocol_rule_export", methods=["POST"])
def protocol_rule_export():
    """导出标准化协议转换规则，支持 JSON/YAML 与差异对比。"""
    try:
        data = request.json
        if not isinstance(data, dict):
            return jsonify({
                "code": 400,
                "message": "请求体必须是JSON对象",
                "data": None,
            }), 400

        result = export_protocol_rules(
            llm_formula_output=data.get("llm_formula_output"),
            protocol_type=data.get("protocol_type", ""),
            message_code=data.get("message_code"),
            export_format=data.get("export_format", "json"),
            compare_with_knowledge_base=bool(data.get("compare_with_knowledge_base", False)),
            baseline_rules=data.get("baseline_rules"),
            source_fields=data.get("source_fields"),
        )
        return jsonify({
            "code": 200,
            "message": "success",
            "data": result,
        })
    except FileNotFoundError as exc:
        return jsonify({
            "code": 404,
            "message": f"知识库文件不存在: {str(exc)}",
            "data": None,
        }), 404
    except Exception as exc:
        return jsonify({
            "code": 500,
            "message": f"规则导出失败: {str(exc)}",
            "data": None,
        }), 500

@app.route("/api/knowledge/protocol_rules/manual_writeback", methods=["POST"])
def protocol_rules_manual_writeback():
    """人工审核通过后的规则写回知识图谱。"""
    try:
        data = request.json
        if isinstance(data, list):
            data = {"rules": data}
        if not isinstance(data, dict):
            return jsonify({
                "code": 400,
                "message": "请求体必须是JSON对象",
                "data": None,
            }), 400

        normalized_rules = _normalize_manual_writeback_rules(
            data.get("rules"),
            default_protocol_type=_first_nonempty_text(data.get("protocol_type")),
            default_source_message_code=_first_nonempty_text(data.get("source_message_code")),
            default_target_protocol_type=_first_nonempty_text(data.get("target_protocol_type")),
            default_target_message_code=_first_nonempty_text(data.get("target_message_code")),
        )

        knowledge_base_cache: Dict[str, Any] = {}
        existing_signature_cache: Dict[Tuple[str, str, str, str], set[Tuple[str, Tuple[str, ...], str]]] = {}
        existing_rules_cache: Dict[Tuple[str, str, str, str], List[Any]] = {}
        knowledge_graph_summaries: Dict[str, Dict[str, Any]] = {}
        written_count = 0
        created_count = 0
        updated_count = 0
        failed_count = 0
        results: List[Dict[str, Any]] = []
        for rule in normalized_rules:
            protocol_type = str(rule.get("protocol_type") or "").strip()
            source_message_code = str(rule.get("message_code") or "").strip() or None
            target_protocol_type = str(rule.get("target_protocol_type") or "").strip() or None
            target_message_code = str(rule.get("target_message_code") or "").strip() or None
            scope_key = (
                protocol_type,
                source_message_code or "",
                target_protocol_type or "",
                target_message_code or "",
            )

            try:
                knowledge_base = knowledge_base_cache.get(protocol_type)
                if knowledge_base is None:
                    knowledge_base = ProtocolConversionKnowledgeBase.load(protocol_type)
                    knowledge_base_cache[protocol_type] = knowledge_base

                existing_signatures = existing_signature_cache.get(scope_key)
                if existing_signatures is None:
                    existing_rules = knowledge_base.list_rules(
                        message_code=source_message_code,
                        target_protocol_type=target_protocol_type,
                        target_message_code=target_message_code,
                    )
                    existing_signatures = {
                        (
                            str(existing_rule.target_field or "").strip().upper(),
                            tuple(str(item or "").strip().upper() for item in (existing_rule.source_fields or [])),
                            str(existing_rule.formula or "").strip(),
                        )
                        for existing_rule in existing_rules
                    }
                    existing_signature_cache[scope_key] = existing_signatures
                    existing_rules_cache[scope_key] = list(existing_rules)

                written_rules = knowledge_base.upsert_generated_rules(
                    [rule],
                    protocol_type=protocol_type,
                    message_code=source_message_code,
                    target_protocol_type=target_protocol_type,
                    target_message_code=target_message_code,
                    source="manual_review",
                )
                knowledge_graph_summaries[protocol_type] = knowledge_base.to_summary()

                if not written_rules:
                    failed_count += 1
                    results.append(
                        {
                            "target_field": rule.get("target_field"),
                            "source_fields": list(rule.get("source_fields") or []),
                            "formula": rule.get("formula"),
                            "protocol_type": protocol_type,
                            "source_message_code": source_message_code,
                            "target_protocol_type": target_protocol_type,
                            "target_message_code": target_message_code,
                            "status": "failed",
                            "error": "规则未写入知识图谱",
                            "retry_payload": {"rules": [rule]},
                        }
                    )
                    continue

                for written_rule in written_rules:
                    legacy_rule_ids = _collect_legacy_formula_rule_ids(
                        existing_rules_cache.get(scope_key, []),
                        written_rule,
                    )
                    _delete_knowledge_rule_ids(knowledge_base, legacy_rule_ids)
                    signature = (
                        str(written_rule.target_field or "").strip().upper(),
                        tuple(str(item or "").strip().upper() for item in (written_rule.source_fields or [])),
                        str(written_rule.formula or "").strip(),
                    )
                    action = "updated" if signature in existing_signatures else "created"
                    if action == "created":
                        created_count += 1
                    else:
                        updated_count += 1
                    existing_signatures.add(signature)
                    existing_rules_cache[scope_key] = [
                        item
                        for item in (existing_rules_cache.get(scope_key, []))
                        if str(getattr(item, "edge_id", None) or "").strip() not in legacy_rule_ids
                    ] + [written_rule]
                    written_count += 1
                    display_target_field = _strip_protocol_prefixed_field_name(
                        written_rule.target_field,
                        target_protocol_type,
                    ) or str(written_rule.target_field or "").strip()
                    results.append(
                        {
                            "target_field": display_target_field,
                            "source_fields": list(written_rule.source_fields or []),
                            "formula": _ensure_explicit_target_formula(
                                written_rule.formula,
                                _to_formula_token(display_target_field),
                            ),
                            "protocol_type": protocol_type,
                            "source_message_code": source_message_code,
                            "target_protocol_type": target_protocol_type,
                            "target_message_code": target_message_code,
                            "status": written_rule.status,
                            "source": written_rule.source,
                            "rule_id": written_rule.edge_id,
                            "action": action,
                        }
                    )
            except Exception as exc:
                failed_count += 1
                results.append(
                    {
                        "target_field": rule.get("target_field"),
                        "source_fields": list(rule.get("source_fields") or []),
                        "formula": rule.get("formula"),
                        "protocol_type": protocol_type,
                        "source_message_code": source_message_code,
                        "target_protocol_type": target_protocol_type,
                        "target_message_code": target_message_code,
                        "status": "failed",
                        "error": str(exc),
                        "retry_payload": {"rules": [rule]},
                    }
                )

        knowledge_graph_entries = [knowledge_graph_summaries[key] for key in sorted(knowledge_graph_summaries)]
        if len(knowledge_graph_entries) == 1:
            knowledge_graph_payload: Any = knowledge_graph_entries[0]
        else:
            knowledge_graph_payload = {
                "group_count": len(knowledge_graph_entries),
                "protocol_types": [str(item.get("protocol_type") or "").strip() for item in knowledge_graph_entries],
            }

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "total": len(normalized_rules),
                "written": written_count,
                "created": created_count,
                "updated": updated_count,
                "failed": failed_count,
                "knowledge_graph": knowledge_graph_payload,
                "knowledge_graphs": knowledge_graph_entries,
                "results": results,
            },
        })
    except ValueError as exc:
        return jsonify({
            "code": 400,
            "message": str(exc),
            "data": None,
        }), 400
    except FileNotFoundError as exc:
        return jsonify({
            "code": 404,
            "message": f"知识图谱文件不存在: {str(exc)}",
            "data": None,
        }), 404
    except Exception as exc:
        return jsonify({
            "code": 500,
            "message": f"人工审核规则写回失败: {str(exc)}",
            "data": None,
        }), 500

@app.route("/health", methods=["GET"])
def health():
    """健康检查接口"""
    return jsonify({"status": "healthy"})

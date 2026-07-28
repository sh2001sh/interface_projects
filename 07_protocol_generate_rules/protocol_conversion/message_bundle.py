from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from code_generation_adapter import read_protocol_dir_content


def _nonempty_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalize_specs(specs: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in specs or []:
        if not isinstance(item, dict):
            continue
        protocol_name = _nonempty_text(item.get("protocol_name") or item.get("protocol_type"))
        message_code = _nonempty_text(item.get("message_code"))
        if not protocol_name and not message_code:
            continue
        normalized.append(dict(item))
    return normalized


def _unique_preserve_order(values: Iterable[Any]) -> List[str]:
    ordered: List[str] = []
    seen = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _build_bundle_id(source_specs: Sequence[Dict[str, Any]], target_spec: Dict[str, Any]) -> str:
    source_tokens = _unique_preserve_order(
        item.get("message_code") or item.get("protocol_name") or item.get("protocol_type")
        for item in source_specs
    )
    target_token = str(
        target_spec.get("message_code")
        or target_spec.get("protocol_name")
        or target_spec.get("protocol_type")
        or "TARGET"
    ).strip()
    source_part = "_".join(source_tokens) if source_tokens else "SOURCE"
    return f"{source_part}_to_{target_token}"


def discover_message_bundle_candidates(
    *,
    source_message_specs: Optional[Iterable[Any]] = None,
    target_message_specs: Optional[Iterable[Any]] = None,
    trained_doc_provider: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    del trained_doc_provider
    sources = _normalize_specs(source_message_specs)
    targets = _normalize_specs(target_message_specs)
    if not sources:
        raise ValueError("未找到可用的原协议消息定义")
    if not targets:
        raise ValueError("未找到可用的目标协议消息定义")

    candidates: List[Dict[str, Any]] = []
    for target_spec in targets:
        candidate_sources = list(sources)
        candidates.append(
            {
                "bundle_id": _build_bundle_id(candidate_sources, target_spec),
                "source_messages": _unique_preserve_order(
                    item.get("message_code") or item.get("protocol_name") or item.get("protocol_type")
                    for item in candidate_sources
                ),
                "source_protocols": _unique_preserve_order(
                    item.get("protocol_name") or item.get("protocol_type") or item.get("message_code")
                    for item in candidate_sources
                ),
                "target_message": _nonempty_text(
                    target_spec.get("message_code")
                    or target_spec.get("protocol_name")
                    or target_spec.get("protocol_type")
                ),
                "target_protocol": _nonempty_text(
                    target_spec.get("protocol_name")
                    or target_spec.get("protocol_type")
                    or target_spec.get("message_code")
                ),
                "selected": True,
            }
        )
    return candidates


def _spec_lookup_key(spec: Dict[str, Any]) -> set[str]:
    return {
        str(item).strip()
        for item in (
            spec.get("protocol_name"),
            spec.get("protocol_type"),
            spec.get("message_code"),
        )
        if str(item or "").strip()
    }


def _select_source_specs(
    candidate: Dict[str, Any],
    source_message_specs: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    requested = {
        str(item).strip()
        for item in (
            list(candidate.get("source_messages") or [])
            + list(candidate.get("source_protocols") or [])
        )
        if str(item or "").strip()
    }
    if not requested:
        return list(source_message_specs)
    selected = [item for item in source_message_specs if _spec_lookup_key(item) & requested]
    return selected or list(source_message_specs)


def _select_target_spec(
    candidate: Dict[str, Any],
    target_message_specs: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    requested = {
        str(item).strip()
        for item in (
            candidate.get("target_message"),
            candidate.get("target_protocol"),
        )
        if str(item or "").strip()
    }
    for item in target_message_specs:
        if _spec_lookup_key(item) & requested:
            return item
    if not target_message_specs:
        raise ValueError("未找到目标协议消息定义")
    return target_message_specs[0]


def _safe_read_protocol_dir_content(directory: Optional[str], field_name: str) -> Optional[str]:
    directory_text = str(directory or "").strip()
    if not directory_text:
        return None
    directory_path = Path(directory_text).resolve()
    if not directory_path.exists():
        return None
    try:
        return read_protocol_dir_content(str(directory_path), field_name)
    except Exception:
        return None


def _join_protocol_contents(specs: Sequence[Dict[str, Any]], field_name: str) -> Optional[str]:
    contents: List[str] = []
    for directory in _unique_preserve_order(item.get("directory") for item in specs):
        content = _safe_read_protocol_dir_content(directory, field_name)
        if content:
            contents.append(content)
    if not contents:
        return None
    return "\n\n".join(contents)


def _build_source_field_catalog(source_specs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    seen = set()
    for spec in source_specs:
        protocol_name = _nonempty_text(spec.get("protocol_name") or spec.get("protocol_type")) or "SOURCE"
        message_code = _nonempty_text(spec.get("message_code"))
        for field in spec.get("fields") or []:
            if not isinstance(field, dict):
                continue
            display_field = _nonempty_text(field.get("field_name") or field.get("label"))
            if not display_field:
                continue
            alias_name = f"{protocol_name}_{display_field}"
            if alias_name in seen:
                continue
            seen.add(alias_name)
            path_parts = [str(item).strip() for item in (field.get("path_parts") or []) if str(item).strip()]
            catalog.append(
                {
                    "field_name": alias_name,
                    "protocol": protocol_name,
                    "message_code": message_code,
                    "actual_field": _nonempty_text(field.get("actual_field")) or display_field,
                    "display_field": display_field,
                    "label": display_field,
                    "source_path": "/".join(path_parts) or display_field,
                    "sample_value": field.get("default_value"),
                }
            )
    return catalog


def _build_source_message(source_field_catalog: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        str(item.get("field_name")): item.get("sample_value")
        for item in source_field_catalog
        if str(item.get("field_name") or "").strip()
    }


def _build_required_target_fields(target_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    protocol_name = _nonempty_text(target_spec.get("protocol_name") or target_spec.get("protocol_type"))
    required: List[Dict[str, Any]] = []
    seen = set()
    for field in target_spec.get("fields") or []:
        if not isinstance(field, dict):
            continue
        field_name = _nonempty_text(field.get("field_name") or field.get("label"))
        if not field_name or field_name in seen:
            continue
        seen.add(field_name)
        path_parts = [str(item).strip() for item in (field.get("path_parts") or []) if str(item).strip()]
        required.append(
            {
                "protocol": protocol_name,
                "field_name": field_name,
                "actual_field": _nonempty_text(field.get("actual_field")) or field_name,
                "label": _nonempty_text(field.get("label")) or field_name,
                "path_parts": path_parts,
                "target_path": "/".join(path_parts) or field_name,
                "bit_length": field.get("bit_length"),
                "default_value": field.get("default_value"),
            }
        )
    return required


def build_bundle_generation_payload(
    candidate: Dict[str, Any],
    *,
    source_message_specs: Optional[Iterable[Any]] = None,
    target_message_specs: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    sources = _normalize_specs(source_message_specs)
    targets = _normalize_specs(target_message_specs)
    source_specs = _select_source_specs(candidate, sources)
    target_spec = _select_target_spec(candidate, targets)
    source_field_catalog = _build_source_field_catalog(source_specs)
    source_protocol_names = _unique_preserve_order(
        item.get("protocol_name") or item.get("protocol_type") for item in source_specs
    )
    source_message_codes = _unique_preserve_order(item.get("message_code") for item in source_specs)
    target_protocol_name = _nonempty_text(
        target_spec.get("protocol_name") or target_spec.get("protocol_type") or target_spec.get("message_code")
    )
    target_message_code = _nonempty_text(target_spec.get("message_code"))

    return {
        "bundle_id": _nonempty_text(candidate.get("bundle_id")) or _build_bundle_id(source_specs, target_spec),
        "source_specs": source_specs,
        "target_spec": dict(target_spec),
        "source_protocol": {
            "name": "+".join(source_protocol_names) if source_protocol_names else None,
            "protocol_type": "+".join(source_protocol_names) if source_protocol_names else None,
            "message_code": "+".join(source_message_codes) if len(source_message_codes) > 1 else (source_message_codes[0] if source_message_codes else None),
            "content": _join_protocol_contents(source_specs, "source_protocol_dirs"),
        },
        "target_protocol": {
            "name": target_protocol_name,
            "protocol_type": target_protocol_name,
            "message_code": target_message_code,
            "content": _join_protocol_contents([target_spec], "target_protocol_dir"),
        },
        "source_message": _build_source_message(source_field_catalog),
        "source_field_catalog": source_field_catalog,
        "required_target_fields": _build_required_target_fields(target_spec),
    }


class BundleEvidenceProvider:
    """Wrap a registry-backed evidence provider for a specific message bundle."""

    def __init__(self, provider: Any, bundle_payload: Dict[str, Any]):
        self.provider = provider
        self.bundle_payload = dict(bundle_payload or {})
        self.registry = getattr(provider, "registry", None)

    def collect_evidence(
        self,
        source_protocol: Dict[str, Any],
        target_protocol: Dict[str, Any],
        source_message: Optional[Any] = None,
        max_snippets_per_role: int = 3,
    ) -> Dict[str, Any]:
        merged_source_protocol = dict(source_protocol or {})
        merged_source_protocol.update(
            {
                key: value
                for key, value in (self.bundle_payload.get("source_protocol") or {}).items()
                if value not in {None, ""}
            }
        )
        merged_target_protocol = dict(target_protocol or {})
        merged_target_protocol.update(
            {
                key: value
                for key, value in (self.bundle_payload.get("target_protocol") or {}).items()
                if value not in {None, ""}
            }
        )
        field_queries = [
            str(item.get("field_name") or "").strip()
            for item in (self.bundle_payload.get("required_target_fields") or [])
            if isinstance(item, dict) and str(item.get("field_name") or "").strip()
        ]
        if field_queries and not merged_target_protocol.get("field_queries"):
            merged_target_protocol["field_queries"] = field_queries
        return self.provider.collect_evidence(
            source_protocol=merged_source_protocol,
            target_protocol=merged_target_protocol,
            source_message=source_message,
            max_snippets_per_role=max_snippets_per_role,
        )

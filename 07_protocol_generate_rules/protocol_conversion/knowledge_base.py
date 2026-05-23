from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover - optional dependency
    GraphDatabase = None


KB_DIR = Path(__file__).resolve().parents[1] / "data" / "protocol_conversion_kb"
GRAPH_FILE_MAP = {
    "link16": "link16_value_graph.json",
}
LEGACY_FILE_MAP = {
    "link16": "link16_conversion_rules.json",
}
CONCEPT_SUFFIXES = (
    "_LABEL",
    "_CODE",
    "_RAW",
    "_VALUE",
    "_FT",
    "_M",
    "_KM",
    "_DEG",
    "_RAD",
    "_MPS",
    "_KTS",
    "_HZ",
    "_SEC",
    "_MS",
    "_MIN",
)
FORMULA_BLOCK_PATTERN = re.compile(r"(?:\n|^)(?:if\s+|for\s+|while\s+|result\s*=)", re.IGNORECASE)
MAPPING_TABLE_PATTERN = re.compile(r"-?\d+(?:\.\d+)?\s*(?:=|->|→)\s*[^,;\n]+")
TRUTHY_VALUES = {"1", "true", "yes", "on"}
LEGACY_ENTITY_GRAPH_MODE = "legacy_entity_graph"
PROTOCOLFIELD_GRAPH_MODE = "protocolfield_graph"
NATIVE_V2_MODE = "native_v2"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in TRUTHY_VALUES


def _csv_env(name: str, default: List[str]) -> List[str]:
    value = str(os.getenv(name) or "").strip()
    if not value:
        return list(default)
    items = [str(item).strip().lower() for item in value.split(",")]
    return [item for item in items if item]


@dataclass
class KnowledgeGraphSettings:
    """Runtime configuration for protocol-conversion knowledge graph backends."""

    protocol_type: str = "Link16"
    backend: str = "local_json_graph"
    enabled: bool = False
    uri: str = ""
    username: str = "neo4j"
    password: str = ""
    database: str = "neo4j"
    connection_timeout_seconds: float = 5.0
    max_connection_pool_size: int = 50
    auto_init: bool = True
    json_fallback: bool = False
    write_fallback_json: bool = True
    read_statuses: List[str] = field(default_factory=lambda: ["approved", "verified"])
    default_write_status: str = "candidate"
    schema_mode: str = "auto"

    @classmethod
    def from_env(cls, protocol_type: str = "Link16") -> "KnowledgeGraphSettings":
        """Build graph settings from environment variables."""
        backend = str(os.getenv("PROTOCOL_CONVERSION_GRAPH_BACKEND") or "auto").strip().lower() or "auto"
        uri = str(os.getenv("PROTOCOL_CONVERSION_NEO4J_URI") or os.getenv("NEO4J_URI") or "").strip()
        enabled = _env_flag("PROTOCOL_CONVERSION_NEO4J_ENABLED", default=backend in {"auto", "neo4j"} and bool(uri))
        if backend in {"local_json", "json"}:
            enabled = False
        elif backend == "neo4j":
            enabled = bool(uri)

        return cls(
            protocol_type=str(protocol_type or "Link16").strip() or "Link16",
            backend="neo4j_graph" if enabled else "local_json_graph",
            enabled=enabled,
            uri=uri,
            username=str(os.getenv("PROTOCOL_CONVERSION_NEO4J_USERNAME") or os.getenv("NEO4J_USERNAME") or "neo4j").strip() or "neo4j",
            password=str(os.getenv("PROTOCOL_CONVERSION_NEO4J_PASSWORD") or os.getenv("NEO4J_PASSWORD") or "").strip(),
            database=str(os.getenv("PROTOCOL_CONVERSION_NEO4J_DATABASE") or os.getenv("NEO4J_DATABASE") or "neo4j").strip() or "neo4j",
            connection_timeout_seconds=float(
                os.getenv("PROTOCOL_CONVERSION_NEO4J_CONNECTION_TIMEOUT_SECONDS")
                or os.getenv("PROTOCOL_CONVERSION_NEO4J_TIMEOUT_SECONDS")
                or os.getenv("NEO4J_CONNECTION_TIMEOUT_SECONDS")
                or "5.0"
            ),
            max_connection_pool_size=int(
                os.getenv("PROTOCOL_CONVERSION_NEO4J_MAX_CONNECTION_POOL_SIZE")
                or os.getenv("NEO4J_MAX_CONNECTION_POOL_SIZE")
                or "50"
            ),
            auto_init=_env_flag("PROTOCOL_CONVERSION_NEO4J_AUTO_INIT", default=True),
            json_fallback=_env_flag("PROTOCOL_CONVERSION_JSON_FALLBACK", default=False),
            write_fallback_json=_env_flag("PROTOCOL_CONVERSION_WRITE_FALLBACK_JSON", default=True),
            read_statuses=_csv_env("PROTOCOL_CONVERSION_NEO4J_READ_STATUSES", ["approved", "verified"]),
            default_write_status=str(os.getenv("PROTOCOL_CONVERSION_NEO4J_WRITE_STATUS") or "candidate").strip().lower() or "candidate",
            schema_mode=str(os.getenv("PROTOCOL_CONVERSION_NEO4J_SCHEMA_MODE") or "auto").strip().lower() or "auto",
        )


@dataclass
class KnowledgeRule:
    """One value-to-value conversion rule resolved from the knowledge graph."""

    protocol_type: str
    message_code: Optional[str]
    field_name: str
    conversion_mode: str
    formula: str
    target_field: Optional[str]
    unit: Optional[str]
    aliases: List[str]
    source: str
    description: Optional[str] = None
    bit_length: Optional[int] = None
    source_fields: List[str] = field(default_factory=list)
    target_protocol_type: Optional[str] = None
    target_message_code: Optional[str] = None
    concept_name: Optional[str] = None
    edge_id: Optional[str] = None
    formula_kind: Optional[str] = None
    confidence: Optional[float] = None
    status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol_type": self.protocol_type,
            "message_code": self.message_code,
            "field_name": self.field_name,
            "source_fields": list(self.source_fields or [self.field_name]),
            "conversion_mode": self.conversion_mode,
            "formula": self.formula,
            "target_field": self.target_field,
            "target_protocol_type": self.target_protocol_type,
            "target_message_code": self.target_message_code,
            "unit": self.unit,
            "aliases": list(self.aliases),
            "source": self.source,
            "description": self.description,
            "bit_length": self.bit_length,
            "concept_name": self.concept_name,
            "edge_id": self.edge_id,
            "formula_kind": self.formula_kind,
            "confidence": self.confidence,
            "status": self.status,
        }


class ProtocolConversionKnowledgeBase:
    """Protocol conversion knowledge graph backed by a local JSON store."""

    _INSTANCE_CACHE: Dict[Tuple[Any, ...], "ProtocolConversionKnowledgeBase"] = {}
    _INSTANCE_CACHE_LOCK = threading.Lock()

    def __init__(self, protocol_type: str, payload: Dict[str, Any], file_path: Path):
        self.protocol_type = str(protocol_type or payload.get("protocol_type") or "Link16")
        self.payload = payload
        self.file_path = file_path
        self.embedding_model = str(payload.get("embedding_model") or "qwen3-0.6b-embedding")
        self.version = str(payload.get("version") or "graph-v1")
        self.backend = str(payload.get("backend") or "local_json_graph")
        self.description = str(payload.get("description") or "")
        self._concepts = payload.get("concepts") or []
        self._field_nodes = payload.get("field_nodes") or []
        self._edges = payload.get("edges") or []
        self._concept_by_id = {
            str(item.get("concept_id") or ""): item for item in self._concepts if item.get("concept_id")
        }
        self._field_by_id = {
            str(item.get("node_id") or ""): item for item in self._field_nodes if item.get("node_id")
        }
        self._refresh_local_indexes()

    @classmethod
    def _local_cache_key(cls, protocol_type: str) -> Tuple[str, str]:
        return ("local_json_graph", cls._normalize_protocol_type(protocol_type))

    @classmethod
    def _neo4j_cache_key(cls, protocol_type: str, settings: KnowledgeGraphSettings) -> Tuple[Any, ...]:
        return (
            "neo4j_graph",
            cls._normalize_protocol_type(protocol_type),
            bool(settings.enabled),
            str(settings.uri or "").strip(),
            str(settings.database or "").strip(),
            float(settings.connection_timeout_seconds),
            int(settings.max_connection_pool_size),
            str(getattr(settings, "schema_mode", "auto") or "auto").strip().lower(),
            bool(settings.json_fallback),
            tuple(str(item or "").strip().lower() for item in settings.read_statuses or []),
            str(settings.default_write_status or "").strip().lower(),
        )

    @classmethod
    def _get_cached_instance(cls, cache_key: Tuple[Any, ...]) -> Optional["ProtocolConversionKnowledgeBase"]:
        with cls._INSTANCE_CACHE_LOCK:
            return cls._INSTANCE_CACHE.get(cache_key)

    @classmethod
    def _store_cached_instance(cls, cache_key: Tuple[Any, ...], instance: "ProtocolConversionKnowledgeBase") -> "ProtocolConversionKnowledgeBase":
        with cls._INSTANCE_CACHE_LOCK:
            cls._INSTANCE_CACHE[cache_key] = instance
        return instance

    def _refresh_local_indexes(self) -> None:
        self._field_index = self._build_field_index(self._field_nodes)
        self._edges_by_source_node_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for edge in self._edges:
            source_node_id = str(edge.get("source_node_id") or "").strip()
            if source_node_id:
                self._edges_by_source_node_id[source_node_id].append(edge)
        self._allowed_read_statuses_cache = self._get_allowed_read_statuses()

    @classmethod
    def load(cls, protocol_type: str) -> "ProtocolConversionKnowledgeBase":
        """Load the preferred knowledge-base backend."""
        settings = KnowledgeGraphSettings.from_env(protocol_type=protocol_type)
        if not settings.enabled:
            return cls.load_local(protocol_type)
        if GraphDatabase is None:
            if settings.json_fallback:
                return cls.load_local(protocol_type)
            raise RuntimeError("neo4j driver is not installed")
        if not settings.uri:
            if settings.json_fallback:
                return cls.load_local(protocol_type)
            raise RuntimeError("Neo4j URI is not configured")
        if not settings.password:
            if settings.json_fallback:
                return cls.load_local(protocol_type)
            raise RuntimeError("Neo4j password is not configured")

        try:
            cache_key = cls._neo4j_cache_key(protocol_type, settings)
            cached_backend = cls._get_cached_instance(cache_key)
            if cached_backend is not None:
                return cached_backend
            neo4j_backend = Neo4jProtocolConversionKnowledgeBase(protocol_type=protocol_type, settings=settings)
        except Exception:
            if settings.json_fallback:
                return cls.load_local(protocol_type)
            raise

        if settings.json_fallback:
            local_backend = cls.load_local(protocol_type)
            return cls._store_cached_instance(
                cache_key,
                CompositeProtocolConversionKnowledgeBase(
                    protocol_type=protocol_type,
                    primary=neo4j_backend,
                    fallback=local_backend,
                ),
            )
        return cls._store_cached_instance(cache_key, neo4j_backend)

    @classmethod
    def load_local(cls, protocol_type: str) -> "ProtocolConversionKnowledgeBase":
        """Load the legacy local JSON graph backend only."""
        normalized = str(protocol_type or "link16").strip().lower()
        cache_key = cls._local_cache_key(normalized)
        cached_backend = cls._get_cached_instance(cache_key)
        if cached_backend is not None:
            return cached_backend
        graph_file = KB_DIR / GRAPH_FILE_MAP.get(normalized, GRAPH_FILE_MAP["link16"])
        if graph_file.exists():
            payload = json.loads(graph_file.read_text(encoding="utf-8"))
            return cls._store_cached_instance(
                cache_key,
                cls(protocol_type=str(payload.get("protocol_type") or protocol_type), payload=payload, file_path=graph_file),
            )

        legacy_file = KB_DIR / LEGACY_FILE_MAP.get(normalized, LEGACY_FILE_MAP["link16"])
        payload = cls._bootstrap_graph_payload(protocol_type=protocol_type, legacy_file=legacy_file)
        graph_file.parent.mkdir(parents=True, exist_ok=True)
        graph_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return cls._store_cached_instance(
            cache_key,
            cls(protocol_type=str(payload.get("protocol_type") or protocol_type), payload=payload, file_path=graph_file),
        )

    @classmethod
    def load_neo4j(
        cls,
        protocol_type: str,
        settings: Optional[KnowledgeGraphSettings] = None,
    ) -> "Neo4jProtocolConversionKnowledgeBase":
        """Load the Neo4j graph backend explicitly."""
        return Neo4jProtocolConversionKnowledgeBase(
            protocol_type=protocol_type,
            settings=settings or KnowledgeGraphSettings.from_env(protocol_type=protocol_type),
        )

    @staticmethod
    def _normalize_field_name(value: Any) -> str:
        return str(value or "").strip().upper()

    @classmethod
    def _normalize_protocol_prefix(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", text)
        text = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", text)
        text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
        return text.upper()

    @classmethod
    def _target_field_keys(cls, value: Any, target_protocol_type: Optional[str] = None) -> set[str]:
        normalized = cls._normalize_field_name(value)
        if not normalized:
            return set()
        keys = {normalized}
        if "." in normalized:
            _, suffix = normalized.split(".", 1)
            if suffix:
                keys.add(suffix)
        prefix = cls._normalize_protocol_prefix(target_protocol_type)
        if prefix:
            dotted_prefix = f"{prefix}."
            underscored_prefix = f"{prefix}_"
            for item in list(keys):
                if item.startswith(dotted_prefix):
                    keys.add(item[len(dotted_prefix):])
                if item.startswith(underscored_prefix):
                    keys.add(item[len(underscored_prefix):])
        return {item for item in keys if item}

    @classmethod
    def _source_field_keys(cls, value: Any, protocol_type: Optional[str] = None) -> set[str]:
        keys = cls._target_field_keys(value, protocol_type)
        normalized = cls._normalize_field_name(value)
        if not normalized:
            return keys
        protocol_like_match = re.match(r"^[A-Z]+[0-9]*(?:_[0-9]+)+_(.+)$", normalized)
        if protocol_like_match:
            suffix = cls._normalize_field_name(protocol_like_match.group(1))
            if suffix:
                keys.add(suffix)
        return {item for item in keys if item}

    @classmethod
    def _strip_protocol_field_prefix(cls, value: Any, protocol_type: Optional[str] = None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""

        prefix_candidates: List[str] = []
        for item in [protocol_type, cls._normalize_protocol_prefix(protocol_type)]:
            cleaned = str(item or "").strip()
            if not cleaned:
                continue
            for candidate in [cleaned, cleaned.lower(), cleaned.upper()]:
                if candidate and candidate not in prefix_candidates:
                    prefix_candidates.append(candidate)

        for candidate in prefix_candidates:
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

    @staticmethod
    def _to_formula_token(value: Any) -> str:
        token = re.sub(r"\W+", "_", str(value or "").strip(), flags=re.UNICODE).strip("_")
        if not token:
            return "field"
        if token[0].isdigit():
            token = f"f_{token}"
        return token

    @classmethod
    def _build_explicit_formula_target_token(cls, target_protocol: Any, target_field: Any) -> str:
        protocol_prefix = cls._normalize_protocol_prefix(target_protocol).lower()
        raw_target_field = str(target_field or "").strip()
        normalized_target_field = cls._to_formula_token(raw_target_field) if raw_target_field else ""
        if protocol_prefix:
            explicit_prefix = f"{protocol_prefix}_"
            if normalized_target_field.lower().startswith(explicit_prefix):
                suffix = normalized_target_field[len(explicit_prefix):].strip("_")
                return cls._to_formula_token(f"{protocol_prefix}_{suffix}") if suffix else cls._to_formula_token(protocol_prefix)
            dotted_prefix = f"{protocol_prefix}."
            if raw_target_field.lower().startswith(dotted_prefix):
                return cls._to_formula_token(f"{protocol_prefix}_{raw_target_field[len(dotted_prefix):].strip()}")

        display_seed = cls._strip_protocol_field_prefix(target_field, target_protocol)
        if protocol_prefix and display_seed:
            return cls._to_formula_token(f"{protocol_prefix}_{display_seed}")
        if display_seed:
            return cls._to_formula_token(display_seed)
        if protocol_prefix:
            return cls._to_formula_token(protocol_prefix)
        return "field"

    @classmethod
    def _formula_protocol_prefix_candidates(cls, protocol_type: Any) -> List[str]:
        raw_protocol = str(protocol_type or "").strip()
        normalized_protocol = cls._normalize_protocol_prefix(protocol_type)
        candidates: List[str] = []
        for value in [
            raw_protocol,
            raw_protocol.replace(".", "_"),
            raw_protocol.replace(".", ""),
            normalized_protocol,
            normalized_protocol.replace("_", ""),
        ]:
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
        return candidates

    @classmethod
    def _build_formula_identifier_replacements(
        cls,
        raw_source_fields: List[Any],
        normalized_source_fields: List[str],
        raw_target_values: List[Any],
        normalized_target_field: str,
        source_protocol_type: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
    ) -> Dict[str, str]:
        replacements: Dict[str, str] = {}

        def register(raw_value: Any, replacement: str) -> None:
            candidate = str(raw_value or "").strip()
            normalized_replacement = str(replacement or "").strip()
            if not candidate or not normalized_replacement:
                return
            candidate_variants = [candidate, candidate.lower(), candidate.upper()]
            for candidate_variant in candidate_variants:
                if candidate_variant != normalized_replacement:
                    replacements.setdefault(candidate_variant, normalized_replacement)
                tokenized_candidate = cls._to_formula_token(candidate_variant)
                tokenized_replacement = cls._to_formula_token(normalized_replacement)
                if tokenized_candidate and tokenized_candidate != tokenized_replacement:
                    replacements.setdefault(tokenized_candidate, tokenized_replacement)

        for index, normalized_source_field in enumerate(normalized_source_fields):
            if not normalized_source_field:
                continue
            register(normalized_source_field, normalized_source_field)
            register(
                cls._build_explicit_formula_target_token(source_protocol_type, normalized_source_field),
                normalized_source_field,
            )
            for prefix in cls._formula_protocol_prefix_candidates(source_protocol_type):
                register(f"{prefix}_{normalized_source_field}", normalized_source_field)
                register(f"{prefix}.{normalized_source_field}", normalized_source_field)
            if index < len(raw_source_fields):
                register(raw_source_fields[index], normalized_source_field)

        for raw_target_value in raw_target_values:
            register(raw_target_value, normalized_target_field)
        register(
            cls._build_explicit_formula_target_token(target_protocol_type, normalized_target_field),
            normalized_target_field,
        )
        for prefix in cls._formula_protocol_prefix_candidates(target_protocol_type):
            register(f"{prefix}_{normalized_target_field}", normalized_target_field)
            register(f"{prefix}.{normalized_target_field}", normalized_target_field)
        return replacements

    @classmethod
    def _rewrite_formula_identifiers(cls, formula: Any, replacements: Dict[str, str]) -> str:
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

    @classmethod
    def _strip_known_prefixed_formula_identifiers(cls, formula: Any, allowed_fields: List[str]) -> str:
        rewritten = str(formula or "").strip()
        if not rewritten or not allowed_fields:
            return rewritten

        allowed_map = {
            cls._normalize_field_name(item): str(item).strip()
            for item in allowed_fields
            if str(item).strip()
        }
        if not allowed_map:
            return rewritten

        token_pattern = re.compile(r"\b[\w\.]+\b", flags=re.UNICODE)
        tokens = sorted(set(token_pattern.findall(rewritten)), key=len, reverse=True)
        for token in tokens:
            if not token or ("_" not in token and "." not in token):
                continue
            stripped = cls._strip_protocol_field_prefix(token)
            replacement = allowed_map.get(cls._normalize_field_name(stripped))
            if not replacement or replacement == token:
                continue
            rewritten = re.sub(
                rf"(?<![A-Za-z0-9_\.]){re.escape(token)}\b",
                replacement,
                rewritten,
            )
        return rewritten

    @staticmethod
    def _normalize_message_code(value: Any) -> Optional[str]:
        cleaned = str(value or "").strip().upper()
        return cleaned or None

    @staticmethod
    def _normalize_protocol_type(value: Any) -> str:
        return str(value or "Link16").strip() or "Link16"

    @staticmethod
    def _normalize_status(value: Any, default: str = "approved") -> str:
        cleaned = str(value or "").strip().lower()
        return cleaned or default

    @staticmethod
    def _infer_formula_kind(formula: str) -> str:
        text = str(formula or "").strip()
        if FORMULA_BLOCK_PATTERN.search(text):
            return "python_block"
        if MAPPING_TABLE_PATTERN.search(text) and not any(token in text for token in ("if ", "for ", "result =", "+", "*", "/")):
            return "mapping_table"
        return "python_expr"

    @classmethod
    def _infer_concept_name(cls, source_field: str, target_field: Optional[str]) -> str:
        base = cls._normalize_field_name(target_field) or cls._normalize_field_name(source_field)
        for suffix in CONCEPT_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix) + 2:
                return base[: -len(suffix)]
        if base.endswith("_DISCRETE") and len(base) > 11:
            return base[:-9]
        return base

    @staticmethod
    def _concept_id(name: str) -> str:
        normalized = re.sub(r"[^A-Z0-9]+", "_", str(name or "").strip().upper()).strip("_") or "UNKNOWN"
        return f"concept::{normalized}"

    @classmethod
    def _field_node_id(cls, protocol_type: str, message_code: Optional[str], field_name: str) -> str:
        protocol = cls._normalize_protocol_type(protocol_type).upper()
        message = cls._normalize_message_code(message_code) or "ANY"
        field = cls._normalize_field_name(field_name)
        return f"field::{protocol}::{message}::{field}"

    @staticmethod
    def _edge_id(source_node_id: str, target_node_id: str, formula: str, conversion_mode: str) -> str:
        digest = hashlib.md5(f"{source_node_id}|{target_node_id}|{conversion_mode}|{formula}".encode("utf-8")).hexdigest()[:12]
        return f"edge::{digest}"

    @classmethod
    def _rule_node_id(
        cls,
        source_protocol_type: str,
        source_message_code: Optional[str],
        target_protocol_type: str,
        target_message_code: Optional[str],
        target_field: str,
        source_fields: Iterable[str],
        formula: str,
        conversion_mode: str,
    ) -> str:
        fingerprint = {
            "source_protocol_type": cls._normalize_protocol_type(source_protocol_type),
            "source_message_code": cls._normalize_message_code(source_message_code),
            "target_protocol_type": cls._normalize_protocol_type(target_protocol_type),
            "target_message_code": cls._normalize_message_code(target_message_code),
            "target_field": cls._normalize_field_name(target_field),
            "source_fields": sorted(cls._normalize_field_name(item) for item in source_fields if cls._normalize_field_name(item)),
            "formula": str(formula or "").strip(),
            "conversion_mode": str(conversion_mode or "").strip().lower(),
        }
        digest = hashlib.md5(json.dumps(fingerprint, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return f"rule::{digest}"

    @staticmethod
    def _evidence_id(rule_id: str, description: str, source: str) -> str:
        digest = hashlib.md5(f"{rule_id}|{source}|{description}".encode("utf-8")).hexdigest()[:16]
        return f"evidence::{digest}"

    @classmethod
    def _default_write_status(cls, source: str, explicit_status: Optional[str], fallback: str = "candidate") -> str:
        if explicit_status:
            return cls._normalize_status(explicit_status, default=fallback)
        if str(source or "").strip().lower() in {"llm", "llm_generated", "candidate", "llm_candidate"}:
            return fallback
        return "approved"

    @classmethod
    def _bootstrap_graph_payload(cls, protocol_type: str, legacy_file: Path) -> Dict[str, Any]:
        payload = json.loads(legacy_file.read_text(encoding="utf-8")) if legacy_file.exists() else {}
        concepts: List[Dict[str, Any]] = []
        concept_ids = set()
        field_nodes: List[Dict[str, Any]] = []
        field_node_ids = set()
        edges: List[Dict[str, Any]] = []

        for item in payload.get("rules") or []:
            source_protocol = cls._normalize_protocol_type(item.get("protocol_type") or protocol_type)
            message_code = cls._normalize_message_code(item.get("message_code"))
            field_name = cls._normalize_field_name(item.get("field_name"))
            if not field_name:
                continue
            target_field = cls._normalize_field_name(item.get("target_field")) or field_name
            concept_name = cls._infer_concept_name(field_name, target_field)
            concept_id = cls._concept_id(concept_name)
            if concept_id not in concept_ids:
                concepts.append(
                    {
                        "concept_id": concept_id,
                        "name": concept_name,
                        "aliases": [],
                        "description": f"Bootstrapped concept for {concept_name}.",
                    }
                )
                concept_ids.add(concept_id)

            source_node_id = cls._field_node_id(source_protocol, message_code, field_name)
            if source_node_id not in field_node_ids:
                field_nodes.append(
                    {
                        "node_id": source_node_id,
                        "protocol_type": source_protocol,
                        "message_code": message_code,
                        "field_name": field_name,
                        "aliases": [cls._normalize_field_name(alias) for alias in item.get("aliases") or [] if cls._normalize_field_name(alias)],
                        "unit": str(item.get("unit") or "").strip() or None,
                        "bit_length": item.get("bit_length"),
                        "concept_id": concept_id,
                        "role": "source",
                    }
                )
                field_node_ids.add(source_node_id)

            target_node_id = cls._field_node_id(
                item.get("target_protocol_type") or source_protocol,
                item.get("target_message_code") or message_code,
                target_field,
            )
            if target_node_id not in field_node_ids:
                field_nodes.append(
                    {
                        "node_id": target_node_id,
                        "protocol_type": cls._normalize_protocol_type(item.get("target_protocol_type") or source_protocol),
                        "message_code": cls._normalize_message_code(item.get("target_message_code") or message_code),
                        "field_name": target_field,
                        "aliases": [],
                        "unit": str(item.get("unit") or "").strip() or None,
                        "bit_length": item.get("bit_length"),
                        "concept_id": concept_id,
                        "role": "target",
                    }
                )
                field_node_ids.add(target_node_id)

            formula = str(item.get("formula") or "").strip()
            conversion_mode = str(item.get("conversion_mode") or "mapping").strip().lower() or "mapping"
            edges.append(
                {
                    "edge_id": cls._edge_id(source_node_id, target_node_id, formula, conversion_mode),
                    "source_node_id": source_node_id,
                    "target_node_id": target_node_id,
                    "source_fields": list(item.get("source_fields") or [field_name]),
                    "conversion_mode": conversion_mode,
                    "formula": formula,
                    "formula_kind": cls._infer_formula_kind(formula),
                    "description": str(item.get("description") or "").strip() or None,
                    "source": str(item.get("source") or "legacy_bootstrap"),
                    "confidence": item.get("confidence", 1.0),
                    "status": cls._normalize_status(item.get("status"), default="approved"),
                }
            )

        return {
            "protocol_type": payload.get("protocol_type") or protocol_type,
            "version": f"graph-{payload.get('version') or 'v1'}",
            "embedding_model": payload.get("embedding_model") or "qwen3-0.6b-embedding",
            "description": payload.get("description") or "Bootstrapped local protocol conversion knowledge graph.",
            "backend": "local_json_graph",
            "concepts": concepts,
            "field_nodes": field_nodes,
            "edges": edges,
        }

    @classmethod
    def _build_field_index(cls, field_nodes: List[Dict[str, Any]]) -> Dict[Tuple[str, Optional[str], str], List[Dict[str, Any]]]:
        index: Dict[Tuple[str, Optional[str], str], List[Dict[str, Any]]] = {}
        for item in field_nodes:
            protocol = cls._normalize_protocol_type(item.get("protocol_type"))
            message_code = cls._normalize_message_code(item.get("message_code"))
            names = [cls._normalize_field_name(item.get("field_name"))]
            names.extend(cls._normalize_field_name(alias) for alias in item.get("aliases") or [])
            for name in names:
                if not name:
                    continue
                index.setdefault((protocol, message_code, name), []).append(item)
        return index

    def _save(self) -> None:
        self.payload["concepts"] = self._concepts
        self.payload["field_nodes"] = self._field_nodes
        self.payload["edges"] = self._edges
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(json.dumps(self.payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._refresh_local_indexes()

    def _iter_source_nodes(self, field_name: str, message_code: Optional[str] = None) -> Iterable[Dict[str, Any]]:
        normalized_field = self._normalize_field_name(field_name)
        normalized_message = self._normalize_message_code(message_code)
        seen = set()
        if normalized_message is None:
            for (protocol, _, name), items in self._field_index.items():
                if protocol != self.protocol_type or name != normalized_field:
                    continue
                for item in items:
                    node_id = item.get("node_id")
                    if node_id and node_id not in seen:
                        seen.add(node_id)
                        yield item
            return
        for key in (
            (self.protocol_type, normalized_message, normalized_field),
            (self.protocol_type, None, normalized_field),
        ):
            for item in self._field_index.get(key, []):
                node_id = item.get("node_id")
                if node_id and node_id not in seen:
                    seen.add(node_id)
                    yield item

    def _edge_to_rule(self, edge: Dict[str, Any]) -> Optional[KnowledgeRule]:
        source_node = self._field_by_id.get(str(edge.get("source_node_id") or ""))
        target_node = self._field_by_id.get(str(edge.get("target_node_id") or ""))
        if not source_node:
            return None
        concept = self._concept_by_id.get(str(source_node.get("concept_id") or (target_node or {}).get("concept_id") or ""), {})
        return KnowledgeRule(
            protocol_type=self._normalize_protocol_type(source_node.get("protocol_type") or self.protocol_type),
            message_code=self._normalize_message_code(source_node.get("message_code")),
            field_name=self._normalize_field_name(source_node.get("field_name")),
            source_fields=[self._normalize_field_name(item) for item in edge.get("source_fields") or [] if self._normalize_field_name(item)] or [self._normalize_field_name(source_node.get("field_name"))],
            conversion_mode=str(edge.get("conversion_mode") or "mapping").strip().lower() or "mapping",
            formula=str(edge.get("formula") or "").strip(),
            target_field=self._normalize_field_name((target_node or {}).get("field_name")),
            target_protocol_type=self._normalize_protocol_type((target_node or {}).get("protocol_type")) if target_node and target_node.get("protocol_type") else None,
            target_message_code=self._normalize_message_code((target_node or {}).get("message_code")) if target_node else None,
            unit=str((target_node or {}).get("unit") or source_node.get("unit") or "").strip() or None,
            aliases=[self._normalize_field_name(alias) for alias in source_node.get("aliases") or [] if self._normalize_field_name(alias)],
            source=str(edge.get("source") or "knowledge_graph"),
            description=str(edge.get("description") or "").strip() or None,
            bit_length=(target_node or {}).get("bit_length") if target_node and target_node.get("bit_length") is not None else source_node.get("bit_length"),
            concept_name=str(concept.get("name") or "").strip() or None,
            edge_id=str(edge.get("edge_id") or "").strip() or None,
            formula_kind=str(edge.get("formula_kind") or "").strip() or None,
            confidence=edge.get("confidence"),
            status=self._normalize_status(edge.get("status"), default="approved"),
        )

    @staticmethod
    def _rule_signature(rule: KnowledgeRule) -> Tuple[str, Tuple[str, ...], str]:
        return (
            rule.target_field or rule.field_name,
            tuple(rule.source_fields or [rule.field_name]),
            rule.formula,
        )

    def _get_allowed_read_statuses(self) -> set[str]:
        settings = getattr(self, "settings", None)
        statuses = getattr(settings, "read_statuses", None)
        if statuses is None:
            statuses = KnowledgeGraphSettings.from_env(protocol_type=self.protocol_type).read_statuses
        return {self._normalize_status(item, default="") for item in statuses if self._normalize_status(item, default="")}

    def find_rule(
        self,
        field_name: str,
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_field: Optional[str] = None,
    ) -> Optional[KnowledgeRule]:
        rules = self.list_rules(
            message_code=message_code,
            field_names=[field_name],
            target_protocol_type=target_protocol_type,
            target_message_code=target_message_code,
            target_fields=[target_field] if target_field else None,
        )
        return rules[0] if rules else None

    def list_rules(
        self,
        message_code: Optional[str] = None,
        field_names: Optional[List[str]] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_fields: Optional[List[str]] = None,
    ) -> List[KnowledgeRule]:
        normalized_fields: set[str] = set()
        for item in field_names or []:
            normalized_fields.update(self._source_field_keys(item, self.protocol_type))
        normalized_targets: set[str] = set()
        for item in target_fields or []:
            normalized_targets.update(self._target_field_keys(item, target_protocol_type))
        normalized_message = self._normalize_message_code(message_code)
        normalized_target_protocol = str(target_protocol_type or "").strip() or None
        normalized_target_message = self._normalize_message_code(target_message_code)

        allowed_statuses = getattr(self, "_allowed_read_statuses_cache", self._get_allowed_read_statuses())
        candidates: List[Tuple[int, KnowledgeRule]] = []
        seen = set()

        for field_name in normalized_fields or [""]:
            nodes = list(self._iter_source_nodes(field_name, message_code=normalized_message)) if field_name else list(self._field_nodes)
            for node in nodes:
                node_id = node.get("node_id")
                if field_name:
                    candidate_edges = self._edges_by_source_node_id.get(str(node_id or "").strip(), [])
                else:
                    candidate_edges = self._edges
                for edge in candidate_edges:
                    if field_name and edge.get("source_node_id") != node_id:
                        continue
                    rule = self._edge_to_rule(edge)
                    if rule is None:
                        continue
                    if rule.status and allowed_statuses and rule.status not in allowed_statuses:
                        continue
                    if normalized_message and rule.message_code not in {None, normalized_message}:
                        continue
                    if normalized_target_protocol and rule.target_protocol_type not in {None, normalized_target_protocol}:
                        continue
                    if normalized_target_message and rule.target_message_code not in {None, normalized_target_message}:
                        continue
                    rule_target_keys = self._target_field_keys(
                        rule.target_field,
                        rule.target_protocol_type or normalized_target_protocol,
                    )
                    if normalized_targets and rule_target_keys.isdisjoint(normalized_targets):
                        continue
                    signature = self._rule_signature(rule)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    score = 0
                    if normalized_message and rule.message_code == normalized_message:
                        score += 30
                    elif rule.message_code is None:
                        score += 8
                    rule_source_keys = set()
                    for source_name in (rule.source_fields or [rule.field_name]):
                        rule_source_keys.update(self._source_field_keys(source_name, rule.protocol_type))
                    rule_source_keys.update(self._source_field_keys(rule.field_name, rule.protocol_type))
                    for alias in rule.aliases:
                        rule_source_keys.update(self._source_field_keys(alias, rule.protocol_type))
                    if field_name and field_name in rule_source_keys:
                        score += 12
                    if normalized_target_protocol and rule.target_protocol_type == normalized_target_protocol:
                        score += 16
                    if normalized_target_message and rule.target_message_code == normalized_target_message:
                        score += 12
                    if normalized_targets and not rule_target_keys.isdisjoint(normalized_targets):
                        score += 10
                    score += int(float(rule.confidence or 0.0) * 5)
                    candidates.append((score, rule))

        candidates.sort(key=lambda item: (-item[0], item[1].target_field or item[1].field_name, item[1].formula))
        return [rule for _, rule in candidates]

    def find_rules_for_source_fields(
        self,
        source_fields: Iterable[str],
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_fields: Optional[List[str]] = None,
    ) -> List[KnowledgeRule]:
        wanted: set[str] = set()
        for item in source_fields:
            wanted.update(self._source_field_keys(item, self.protocol_type))
        if not wanted:
            return []
        candidates = self.list_rules(
            message_code=message_code,
            field_names=list(wanted),
            target_protocol_type=target_protocol_type,
            target_message_code=target_message_code,
            target_fields=target_fields,
        )
        matched: List[KnowledgeRule] = []
        seen = set()
        for rule in candidates:
            source_field_groups = [
                self._source_field_keys(source_name, rule.protocol_type)
                for source_name in (rule.source_fields or [rule.field_name])
            ]
            if any(not group or group.isdisjoint(wanted) for group in source_field_groups):
                continue
            signature = self._rule_signature(rule)
            if signature in seen:
                continue
            seen.add(signature)
            matched.append(rule)
        return matched

    def _ensure_concept(self, concept_name: str) -> str:
        normalized = str(concept_name or "").strip() or "UNKNOWN"
        concept_id = self._concept_id(normalized)
        if concept_id in self._concept_by_id:
            return concept_id
        record = {
            "concept_id": concept_id,
            "name": normalized,
            "aliases": [],
            "description": f"LLM discovered concept {normalized}.",
        }
        self._concepts.append(record)
        self._concept_by_id[concept_id] = record
        return concept_id

    def _ensure_field_node(
        self,
        protocol_type: str,
        message_code: Optional[str],
        field_name: str,
        concept_id: str,
        role: str,
        aliases: Optional[Iterable[str]] = None,
        unit: Optional[str] = None,
        bit_length: Optional[int] = None,
    ) -> str:
        node_id = self._field_node_id(protocol_type, message_code, field_name)
        existing = self._field_by_id.get(node_id)
        normalized_aliases = [self._normalize_field_name(item) for item in (aliases or []) if self._normalize_field_name(item)]
        if existing is not None:
            current_aliases = {self._normalize_field_name(item) for item in existing.get("aliases") or [] if self._normalize_field_name(item)}
            for alias in normalized_aliases:
                if alias not in current_aliases:
                    existing.setdefault("aliases", []).append(alias)
            if unit and not existing.get("unit"):
                existing["unit"] = unit
            if bit_length is not None and existing.get("bit_length") is None:
                existing["bit_length"] = bit_length
            if concept_id and not existing.get("concept_id"):
                existing["concept_id"] = concept_id
            return node_id

        record = {
            "node_id": node_id,
            "protocol_type": self._normalize_protocol_type(protocol_type),
            "message_code": self._normalize_message_code(message_code),
            "field_name": self._normalize_field_name(field_name),
            "aliases": normalized_aliases,
            "unit": str(unit or "").strip() or None,
            "bit_length": bit_length,
            "concept_id": concept_id,
            "role": role,
        }
        self._field_nodes.append(record)
        self._field_by_id[node_id] = record
        for name in [record["field_name"], *record["aliases"]]:
            self._field_index.setdefault((record["protocol_type"], record["message_code"], name), []).append(record)
        return node_id

    def _normalize_rule_input(
        self,
        item: Any,
        protocol_type: Optional[str] = None,
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        source: str = "llm",
        default_status: str = "candidate",
    ) -> Optional[KnowledgeRule]:
        if isinstance(item, KnowledgeRule):
            rule = item
            if not rule.status:
                rule.status = self._default_write_status(rule.source or source, None, fallback=default_status)
            return rule
        if not isinstance(item, dict):
            return None

        source_protocol_value = item.get("protocol_type") or protocol_type or self.protocol_type
        target_protocol_value = item.get("target_protocol_type") or target_protocol_type or source_protocol_value
        field_name = self._normalize_field_name(
            self._strip_protocol_field_prefix(item.get("field_name") or item.get("source_field"), source_protocol_value)
        )
        source_fields = [
            self._normalize_field_name(self._strip_protocol_field_prefix(value, source_protocol_value))
            for value in item.get("source_fields") or ([] if field_name else [])
            if self._normalize_field_name(self._strip_protocol_field_prefix(value, source_protocol_value))
        ]
        if not source_fields and field_name:
            source_fields = [field_name]
        if not field_name and source_fields:
            field_name = source_fields[0]
        if not field_name:
            return None

        formula = str(
            item.get("formula")
            or item.get("rule")
            or item.get("conversion_formula")
            or item.get("expression")
            or ""
        ).strip()
        if not formula:
            return None

        target_field = self._normalize_field_name(
            self._strip_protocol_field_prefix(item.get("target_field"), target_protocol_value)
        ) or field_name
        raw_source_fields = item.get("source_fields") if isinstance(item.get("source_fields"), list) else []
        if not raw_source_fields and (item.get("field_name") or item.get("source_field")):
            raw_source_fields = [item.get("field_name") or item.get("source_field")]
        formula = self._rewrite_formula_identifiers(
            formula,
            self._build_formula_identifier_replacements(
                raw_source_fields=list(raw_source_fields),
                normalized_source_fields=list(source_fields),
                raw_target_values=[
                    item.get("target_field"),
                    item.get("target_actual_field"),
                    item.get("target_path"),
                ],
                normalized_target_field=target_field,
                source_protocol_type=source_protocol_value,
                target_protocol_type=target_protocol_value,
            ),
        )
        formula = self._strip_known_prefixed_formula_identifiers(
            formula,
            list(source_fields) + [target_field],
        )
        return KnowledgeRule(
            protocol_type=self._normalize_protocol_type(source_protocol_value),
            message_code=self._normalize_message_code(item.get("message_code") or message_code),
            field_name=field_name,
            source_fields=source_fields,
            conversion_mode=str(item.get("conversion_mode") or item.get("mode") or "mapping").strip().lower() or "mapping",
            formula=formula,
            target_field=target_field,
            target_protocol_type=str(target_protocol_value or "").strip() or None,
            target_message_code=self._normalize_message_code(item.get("target_message_code") or target_message_code),
            unit=str(item.get("unit") or "").strip() or None,
            aliases=[
                self._normalize_field_name(self._strip_protocol_field_prefix(alias, source_protocol_value))
                for alias in item.get("aliases") or []
                if self._normalize_field_name(self._strip_protocol_field_prefix(alias, source_protocol_value))
            ],
            source=str(item.get("source") or source),
            description=str(item.get("description") or item.get("evidence") or "").strip() or None,
            bit_length=item.get("bit_length"),
            concept_name=str(item.get("concept_name") or self._infer_concept_name(field_name, target_field)).strip() or None,
            edge_id=str(item.get("edge_id") or "").strip() or None,
            formula_kind=str(item.get("formula_kind") or self._infer_formula_kind(formula)).strip() or None,
            confidence=float(item.get("confidence")) if item.get("confidence") is not None else None,
            status=self._default_write_status(item.get("source") or source, item.get("status"), fallback=default_status),
        )

    def upsert_generated_rules(
        self,
        rules: Iterable[Any],
        protocol_type: Optional[str] = None,
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        source: str = "llm",
    ) -> List[KnowledgeRule]:
        written_rules: List[KnowledgeRule] = []
        for item in rules:
            rule = self._normalize_rule_input(
                item,
                protocol_type=protocol_type,
                message_code=message_code,
                target_protocol_type=target_protocol_type,
                target_message_code=target_message_code,
                source=source,
                default_status="candidate",
            )
            if rule is None:
                continue

            concept_name = rule.concept_name or self._infer_concept_name(rule.field_name, rule.target_field)
            concept_id = self._ensure_concept(concept_name)
            source_protocol = rule.protocol_type or protocol_type or self.protocol_type
            target_protocol = rule.target_protocol_type or target_protocol_type or source_protocol
            target_field = rule.target_field or rule.field_name
            source_fields = rule.source_fields or [rule.field_name]
            target_node_id = self._ensure_field_node(
                protocol_type=target_protocol,
                message_code=rule.target_message_code or target_message_code,
                field_name=target_field,
                concept_id=concept_id,
                role="target",
                unit=rule.unit,
                bit_length=rule.bit_length,
            )
            for source_field in source_fields:
                source_node_id = self._ensure_field_node(
                    protocol_type=source_protocol,
                    message_code=rule.message_code or message_code,
                    field_name=source_field,
                    concept_id=concept_id,
                    role="source",
                    aliases=rule.aliases,
                    unit=rule.unit,
                    bit_length=rule.bit_length,
                )
                edge_id = rule.edge_id or self._edge_id(source_node_id, target_node_id, rule.formula, rule.conversion_mode)
                edge_payload = {
                    "edge_id": edge_id,
                    "source_node_id": source_node_id,
                    "target_node_id": target_node_id,
                    "source_fields": source_fields,
                    "conversion_mode": rule.conversion_mode,
                    "formula": rule.formula,
                    "formula_kind": rule.formula_kind or self._infer_formula_kind(rule.formula),
                    "description": rule.description,
                    "source": rule.source or source,
                    "confidence": rule.confidence,
                    "status": self._normalize_status(rule.status, default="candidate"),
                }
                replaced = False
                for idx, existing in enumerate(self._edges):
                    if existing.get("edge_id") == edge_id:
                        self._edges[idx] = edge_payload
                        replaced = True
                        break
                if not replaced:
                    self._edges.append(edge_payload)
                edge_rule = self._edge_to_rule(edge_payload)
                if edge_rule is not None:
                    written_rules.append(edge_rule)

        if written_rules:
            self._save()
        return written_rules

    def to_summary(self) -> Dict[str, Any]:
        return {
            "protocol_type": self.protocol_type,
            "version": self.version,
            "embedding_model": self.embedding_model,
            "backend": self.backend,
            "concept_count": len(self._concepts),
            "field_node_count": len(self._field_nodes),
            "rule_count": len(self._edges),
            "file_path": str(self.file_path),
        }


class Neo4jProtocolConversionKnowledgeBase(ProtocolConversionKnowledgeBase):
    """Protocol conversion knowledge graph backed by Neo4j."""

    def __init__(self, protocol_type: str, settings: KnowledgeGraphSettings):
        if GraphDatabase is None:
            raise RuntimeError("neo4j driver is not installed")
        if not settings.uri:
            raise RuntimeError("Neo4j URI is not configured")

        self.protocol_type = self._normalize_protocol_type(protocol_type)
        self.settings = settings
        self.embedding_model = "qwen3-0.6b-embedding"
        self.version = "graph-v2-neo4j"
        self.backend = "neo4j_graph"
        self.description = "Neo4j-backed protocol conversion knowledge graph."
        self.file_path = None
        self.driver = GraphDatabase.driver(
            settings.uri,
            auth=(settings.username, settings.password),
            connection_timeout=settings.connection_timeout_seconds,
            max_connection_pool_size=settings.max_connection_pool_size,
        )
        self.driver.verify_connectivity()
        self.schema_mode = self._detect_schema_mode()
        if self.schema_mode == PROTOCOLFIELD_GRAPH_MODE:
            self.backend = "neo4j_protocolfield_graph"
            self.description = "Neo4j-backed protocol conversion knowledge graph (ProtocolField/Entity schema)."
        elif self.schema_mode == LEGACY_ENTITY_GRAPH_MODE:
            self.backend = "neo4j_legacy_entity_graph"
            self.description = "Neo4j-backed protocol conversion knowledge graph (legacy Entity/G schema)."
        else:
            self.backend = "neo4j_graph"
            self.description = "Neo4j-backed protocol conversion knowledge graph."
        self._external_alias_map: Optional[Dict[str, List[str]]] = None
        self._external_rules_cache: Optional[List[KnowledgeRule]] = None
        if settings.auto_init and self.schema_mode == NATIVE_V2_MODE:
            self.ensure_schema()

    def _run_cypher(self, query: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        with self.driver.session(database=self.settings.database) as session:
            result = session.run(query, parameters or {})
            records: List[Dict[str, Any]] = []
            for record in result:
                if hasattr(record, "data"):
                    records.append(record.data())
                else:  # pragma: no cover - fallback for lightweight fakes
                    records.append(dict(record))
            return records

    def _detect_schema_mode(self) -> str:
        configured = str(getattr(self.settings, "schema_mode", "auto") or "auto").strip().lower()
        if configured and configured != "auto":
            if configured in {"legacy", "legacy_entity", "legacy_entity_graph", "entity_graph", "old_schema"}:
                return LEGACY_ENTITY_GRAPH_MODE
            if configured in {"protocolfield_graph", "protocol_field_graph"}:
                return PROTOCOLFIELD_GRAPH_MODE
            if configured in {"native_v2", "native"}:
                return NATIVE_V2_MODE
            return configured
        try:
            labels = {
                str(item.get("label") or "").strip()
                for item in self._run_cypher("CALL db.labels() YIELD label RETURN label")
                if str(item.get("label") or "").strip()
            }
            rels = {
                str(item.get("relationshipType") or "").strip()
                for item in self._run_cypher("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")
                if str(item.get("relationshipType") or "").strip()
            }
        except Exception:
            return NATIVE_V2_MODE
        if {"ProtocolField", "Entity"}.issubset(labels) and "MAP_TO" in rels:
            return PROTOCOLFIELD_GRAPH_MODE
        if "Entity" in labels and "G" in rels:
            return LEGACY_ENTITY_GRAPH_MODE
        return NATIVE_V2_MODE

    @staticmethod
    def _uses_external_entity_graph(schema_mode: Optional[str]) -> bool:
        return str(schema_mode or "").strip().lower() in {PROTOCOLFIELD_GRAPH_MODE, LEGACY_ENTITY_GRAPH_MODE}

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_json_like_list(cls, value: Any) -> List[str]:
        if isinstance(value, list):
            return [cls._normalize_field_name(item) for item in value if cls._normalize_field_name(item)]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [cls._normalize_field_name(item) for item in parsed if cls._normalize_field_name(item)]
            return [
                cls._normalize_field_name(item)
                for item in re.split(r"[;,；，|/]+", text)
                if cls._normalize_field_name(item)
            ]
        return []

    @classmethod
    def _extract_expr_source_fields(cls, expr: str) -> List[str]:
        fields = [cls._normalize_field_name(item) for item in re.findall(r"【([^】]+)】", str(expr or "")) if cls._normalize_field_name(item)]
        seen: set[str] = set()
        ordered: List[str] = []
        for item in fields:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    @classmethod
    def _extract_formula_from_expr(cls, expr: Any, target_name: str) -> str:
        text = str(expr or "").strip()
        if not text:
            return ""
        if ":" in text:
            prefix, rest = text.split(":", 1)
            if cls._normalize_field_name(prefix) == cls._normalize_field_name(target_name):
                text = rest.strip()
        text = text.replace("【", "").replace("】", "")
        return text.strip()

    def _load_external_alias_map(self) -> Dict[str, List[str]]:
        if self._external_alias_map is not None:
            return self._external_alias_map
        alias_graph: Dict[str, set[str]] = defaultdict(set)
        try:
            relationship_types = {
                str(item.get("relationshipType") or "").strip()
                for item in self._run_cypher("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")
                if str(item.get("relationshipType") or "").strip()
            }
        except Exception:
            relationship_types = set()
        if "D" not in relationship_types:
            self._external_alias_map = {}
            return self._external_alias_map
        try:
            records = self._run_cypher(
                """
                MATCH (a:Entity)-[:D]-(b:Entity)
                RETURN a.name AS source_name, b.name AS target_name
                """
            )
        except Exception:
            records = []
        for record in records:
            source_name = self._normalize_field_name(self._strip_protocol_field_prefix(record.get("source_name")))
            target_name = self._normalize_field_name(self._strip_protocol_field_prefix(record.get("target_name")))
            if not source_name or not target_name or source_name == target_name:
                continue
            alias_graph[source_name].add(target_name)
            alias_graph[target_name].add(source_name)
        self._external_alias_map = {key: sorted(values) for key, values in alias_graph.items()}
        return self._external_alias_map

    def _invalidate_external_cache(self) -> None:
        self._external_alias_map = None
        self._external_rules_cache = None

    def _build_external_mapto_rules(self) -> List[KnowledgeRule]:
        records = self._run_cypher(
            """
            MATCH (src:ProtocolField)-[r:MAP_TO]->(target:ProtocolField)
            RETURN src.name AS source_name, target.name AS target_name, properties(r) AS rel_props
            """
        )
        alias_map = self._load_external_alias_map()
        grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for record in records:
            props = record.get("rel_props") or {}
            source_protocol = str(props.get("source_protocol_type") or self.protocol_type).strip() or self.protocol_type
            target_protocol = str(props.get("target_protocol_type") or "").strip() or None
            source_name = self._normalize_field_name(
                self._strip_protocol_field_prefix(record.get("source_name"), source_protocol)
            )
            target_name = self._normalize_field_name(
                self._strip_protocol_field_prefix(record.get("target_name"), target_protocol)
            )
            if not source_name or not target_name:
                continue
            raw_source_fields = self._parse_json_like_list(props.get("source_fields_json") or props.get("source_fields"))
            source_fields = [
                self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol))
                for item in raw_source_fields
                if self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol))
            ]
            if not source_fields:
                source_fields = [source_name]
            formula = str(props.get("formula") or props.get("expr") or props.get("expression") or "").strip() or source_name
            conversion_mode = str(props.get("conversion_mode") or props.get("mode") or "mapping").strip().lower() or "mapping"
            target_field = self._normalize_field_name(
                self._strip_protocol_field_prefix(
                    props.get("target_field") or props.get("target_actual_field") or target_name,
                    target_protocol,
                )
            ) or target_name
            formula = self._rewrite_formula_identifiers(
                formula,
                self._build_formula_identifier_replacements(
                    raw_source_fields=list(raw_source_fields) or [record.get("source_name")],
                    normalized_source_fields=list(source_fields),
                    raw_target_values=[
                        props.get("target_field"),
                        props.get("target_actual_field"),
                        record.get("target_name"),
                    ],
                    normalized_target_field=target_field,
                    source_protocol_type=source_protocol,
                    target_protocol_type=target_protocol,
                ),
            )
            formula = self._strip_known_prefixed_formula_identifiers(
                formula,
                list(source_fields) + [target_field],
            )
            rule_id = str(props.get("rule_id") or "").strip()
            key = (
                "mapto",
                rule_id or None,
                target_field,
                tuple(source_fields),
                formula,
                source_protocol or None,
                str(props.get("source_message_code") or "").strip().upper() or None,
                target_protocol,
                str(props.get("target_message_code") or "").strip().upper() or None,
            )
            state = grouped.setdefault(
                key,
                {
                    "field_name": source_fields[0],
                    "source_fields": list(source_fields),
                    "aliases": [],
                    "conversion_mode": conversion_mode,
                    "formula": formula,
                    "target_field": target_field,
                    "target_protocol_type": target_protocol,
                    "target_message_code": str(props.get("target_message_code") or "").strip().upper() or None,
                    "protocol_type": self._normalize_protocol_type(source_protocol),
                    "message_code": str(props.get("source_message_code") or "").strip().upper() or None,
                    "description": str(props.get("desc") or props.get("description") or "").strip() or None,
                    "concept_name": str(props.get("concept_name") or props.get("target_concept") or target_name).strip() or target_name,
                    "edge_id": rule_id or None,
                    "formula_kind": str(props.get("formula_kind") or self._infer_formula_kind(formula)).strip() or self._infer_formula_kind(formula),
                    "confidence": self._coerce_float(props.get("confidence")),
                    "status": self._normalize_status(props.get("status"), default="approved"),
                    "source": str(props.get("source") or "knowledge_graph").strip() or "knowledge_graph",
                },
            )
            for alias in alias_map.get(source_name, []):
                if alias not in state["aliases"] and alias not in state["source_fields"]:
                    state["aliases"].append(alias)
            if source_name not in state["source_fields"]:
                state["source_fields"].append(source_name)
        return [
            KnowledgeRule(
                protocol_type=state["protocol_type"],
                message_code=state["message_code"],
                field_name=state["field_name"],
                source_fields=list(state["source_fields"]),
                conversion_mode=state["conversion_mode"],
                formula=state["formula"],
                target_field=state["target_field"],
                target_protocol_type=state["target_protocol_type"],
                target_message_code=state["target_message_code"],
                unit=None,
                aliases=list(state["aliases"]),
                source=state["source"],
                description=state["description"],
                bit_length=None,
                concept_name=state["concept_name"],
                edge_id=state["edge_id"],
                formula_kind=state["formula_kind"],
                confidence=state["confidence"],
                status=state["status"],
            )
            for state in grouped.values()
        ]

    def _build_external_expression_rules(self) -> List[KnowledgeRule]:
        records = self._run_cypher(
            """
            MATCH (src:Entity)-[r:G]->(target:Entity)
            RETURN src.name AS source_name, target.name AS target_name, properties(r) AS rel_props
            """
        )
        alias_map = self._load_external_alias_map()
        grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for record in records:
            props = record.get("rel_props") or {}
            source_protocol = str(props.get("source_protocol_type") or self.protocol_type).strip() or self.protocol_type
            target_protocol = str(props.get("target_protocol_type") or "").strip() or None
            source_name = self._normalize_field_name(
                self._strip_protocol_field_prefix(record.get("source_name"), source_protocol)
            )
            target_name = self._normalize_field_name(
                self._strip_protocol_field_prefix(record.get("target_name"), target_protocol)
            )
            if not source_name or not target_name:
                continue
            expr = str(props.get("expr") or props.get("formula") or props.get("expression") or "").strip()
            raw_source_fields = self._parse_json_like_list(props.get("source_fields_json") or props.get("source_fields"))
            source_fields = [
                self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol))
                for item in raw_source_fields
                if self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol))
            ]
            if not source_fields:
                source_fields = [
                    self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol))
                    for item in self._parse_json_like_list(props.get("fullLabelFrom"))
                    if self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol))
                ]
            if not source_fields:
                source_fields = [
                    self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol))
                    for item in self._extract_expr_source_fields(expr)
                    if self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol))
                ]
            if not source_fields:
                source_fields = [source_name]
            formula = self._extract_formula_from_expr(expr or props.get("formula"), target_name) or source_name
            target_field = self._normalize_field_name(
                self._strip_protocol_field_prefix(
                    props.get("target_field") or props.get("target_actual_field") or target_name,
                    target_protocol,
                )
            ) or target_name
            formula = self._rewrite_formula_identifiers(
                formula,
                self._build_formula_identifier_replacements(
                    raw_source_fields=list(raw_source_fields) or [record.get("source_name")],
                    normalized_source_fields=list(source_fields),
                    raw_target_values=[
                        props.get("target_field"),
                        props.get("target_actual_field"),
                        record.get("target_name"),
                    ],
                    normalized_target_field=target_field,
                    source_protocol_type=source_protocol,
                    target_protocol_type=target_protocol,
                ),
            )
            formula = self._strip_known_prefixed_formula_identifiers(
                formula,
                list(source_fields) + [target_field],
            )
            rule_id = str(props.get("rule_id") or "").strip()
            key = (
                "g",
                rule_id or None,
                target_field,
                tuple(source_fields),
                formula,
                source_protocol or None,
                str(props.get("source_message_code") or "").strip().upper() or None,
                target_protocol,
                str(props.get("target_message_code") or "").strip().upper() or None,
            )
            state = grouped.setdefault(
                key,
                {
                    "field_name": source_fields[0],
                    "source_fields": list(source_fields),
                    "aliases": [],
                    "conversion_mode": str(props.get("conversion_mode") or "transcoding").strip().lower() or "transcoding",
                    "formula": formula,
                    "target_field": target_field,
                    "target_protocol_type": target_protocol,
                    "target_message_code": str(props.get("target_message_code") or "").strip().upper() or None,
                    "protocol_type": self._normalize_protocol_type(source_protocol),
                    "message_code": str(props.get("source_message_code") or "").strip().upper() or None,
                    "description": str(props.get("desc") or props.get("description") or props.get("name") or "").strip() or None,
                    "concept_name": str(props.get("concept_name") or target_name).strip() or target_name,
                    "edge_id": rule_id or None,
                    "formula_kind": str(props.get("formula_kind") or self._infer_formula_kind(formula)).strip() or self._infer_formula_kind(formula),
                    "confidence": self._coerce_float(props.get("confidence")),
                    "status": self._normalize_status(props.get("status"), default="approved"),
                    "source": str(props.get("source") or "knowledge_graph").strip() or "knowledge_graph",
                },
            )
            for source_field in source_fields:
                for alias in alias_map.get(source_field, []):
                    if alias not in state["aliases"] and alias not in state["source_fields"]:
                        state["aliases"].append(alias)
            if source_name not in state["source_fields"]:
                state["source_fields"].append(source_name)
        return [
            KnowledgeRule(
                protocol_type=state["protocol_type"],
                message_code=state["message_code"],
                field_name=state["field_name"],
                source_fields=list(state["source_fields"]),
                conversion_mode=state["conversion_mode"],
                formula=state["formula"],
                target_field=state["target_field"],
                target_protocol_type=state["target_protocol_type"],
                target_message_code=state["target_message_code"],
                unit=None,
                aliases=list(state["aliases"]),
                source=state["source"],
                description=state["description"],
                bit_length=None,
                concept_name=state["concept_name"],
                edge_id=state["edge_id"],
                formula_kind=state["formula_kind"],
                confidence=state["confidence"],
                status=state["status"],
            )
            for state in grouped.values()
        ]

    def _load_external_rules(self) -> List[KnowledgeRule]:
        if self._external_rules_cache is not None:
            return list(self._external_rules_cache)
        rules: List[KnowledgeRule] = []
        if self.schema_mode == PROTOCOLFIELD_GRAPH_MODE:
            rules = self._build_external_mapto_rules()
        existing_signatures = {self._rule_signature(rule) for rule in rules}
        for rule in self._build_external_expression_rules():
            signature = self._rule_signature(rule)
            if signature in existing_signatures:
                continue
            existing_signatures.add(signature)
            rules.append(rule)
        self._external_rules_cache = list(rules)
        return rules

    def ensure_schema(self) -> None:
        """Create required constraints and indexes for the graph model."""
        if getattr(self, "schema_mode", NATIVE_V2_MODE) != NATIVE_V2_MODE:
            return
        statements = [
            "CREATE CONSTRAINT concept_id_unique IF NOT EXISTS FOR (c:Concept) REQUIRE c.concept_id IS UNIQUE",
            "CREATE CONSTRAINT field_node_id_unique IF NOT EXISTS FOR (f:Field) REQUIRE f.node_id IS UNIQUE",
            "CREATE CONSTRAINT rule_id_unique IF NOT EXISTS FOR (r:Rule) REQUIRE r.rule_id IS UNIQUE",
            "CREATE CONSTRAINT evidence_id_unique IF NOT EXISTS FOR (e:Evidence) REQUIRE e.evidence_id IS UNIQUE",
            "CREATE INDEX field_lookup IF NOT EXISTS FOR (f:Field) ON (f.protocol_type, f.message_code, f.field_name)",
            "CREATE INDEX rule_status_lookup IF NOT EXISTS FOR (r:Rule) ON (r.status, r.target_protocol_type, r.target_message_code, r.target_field)",
        ]
        for statement in statements:
            self._run_cypher(statement)

    def _query_rule_records(self) -> List[Dict[str, Any]]:
        query = """
        MATCH (r:Rule)-[:USES_SOURCE]->(src:Field)
        WHERE src.protocol_type = $protocol_type
        WITH r, collect(DISTINCT src{.*}) AS sources
        MATCH (r)-[:PRODUCES_TARGET]->(target:Field)
        OPTIONAL MATCH (r)-[:ABOUT_CONCEPT]->(concept:Concept)
        RETURN r{.*} AS rule, sources, target{.*} AS target, concept{.*} AS concept
        """
        return self._run_cypher(query, {"protocol_type": self.protocol_type})

    def _record_to_rule(self, record: Dict[str, Any]) -> Optional[KnowledgeRule]:
        rule_props = record.get("rule") or {}
        target = record.get("target") or {}
        concept = record.get("concept") or {}
        sources = record.get("sources") or []
        if not rule_props or not sources:
            return None

        source_fields = [
            self._normalize_field_name(item)
            for item in rule_props.get("source_fields") or [source.get("field_name") for source in sources]
            if self._normalize_field_name(item)
        ]
        first_source = sources[0]
        aliases: List[str] = []
        for source in sources:
            for alias in source.get("aliases") or []:
                normalized = self._normalize_field_name(alias)
                if normalized and normalized not in aliases:
                    aliases.append(normalized)

        source_protocol_value = rule_props.get("source_protocol_type") or first_source.get("protocol_type") or self.protocol_type
        target_protocol_value = rule_props.get("target_protocol_type") or target.get("protocol_type")
        normalized_field_name = self._normalize_field_name(
            self._strip_protocol_field_prefix(first_source.get("field_name"), source_protocol_value)
        )
        normalized_source_fields = [
            self._normalize_field_name(self._strip_protocol_field_prefix(item, source_protocol_value))
            for item in source_fields
        ] or [normalized_field_name]
        normalized_target_field = self._normalize_field_name(
            self._strip_protocol_field_prefix(
                rule_props.get("target_field") or target.get("field_name"),
                target_protocol_value,
            )
        )
        normalized_formula = self._rewrite_formula_identifiers(
            str(rule_props.get("formula") or "").strip(),
            self._build_formula_identifier_replacements(
                raw_source_fields=source_fields,
                normalized_source_fields=normalized_source_fields,
                raw_target_values=[
                    rule_props.get("target_field"),
                    target.get("field_name"),
                ],
                normalized_target_field=normalized_target_field,
                source_protocol_type=source_protocol_value,
                target_protocol_type=target_protocol_value,
            ),
        )
        normalized_formula = self._strip_known_prefixed_formula_identifiers(
            normalized_formula,
            list(normalized_source_fields) + [normalized_target_field],
        )

        return KnowledgeRule(
            protocol_type=self._normalize_protocol_type(source_protocol_value),
            message_code=self._normalize_message_code(rule_props.get("source_message_code") or first_source.get("message_code")),
            field_name=normalized_field_name,
            source_fields=normalized_source_fields,
            conversion_mode=str(rule_props.get("conversion_mode") or "mapping").strip().lower() or "mapping",
            formula=normalized_formula,
            target_field=normalized_target_field,
            target_protocol_type=self._normalize_protocol_type(target_protocol_value) if target_protocol_value else None,
            target_message_code=self._normalize_message_code(rule_props.get("target_message_code") or target.get("message_code")),
            unit=str(target.get("unit") or first_source.get("unit") or "").strip() or None,
            aliases=[
                self._normalize_field_name(
                    self._strip_protocol_field_prefix(
                        alias,
                        source_protocol_value,
                    )
                )
                for alias in aliases
                if self._normalize_field_name(
                    self._strip_protocol_field_prefix(
                        alias,
                        source_protocol_value,
                    )
                )
            ],
            source=str(rule_props.get("source") or "knowledge_graph"),
            description=str(rule_props.get("description") or "").strip() or None,
            bit_length=target.get("bit_length") if target.get("bit_length") is not None else first_source.get("bit_length"),
            concept_name=str(rule_props.get("concept_name") or concept.get("name") or "").strip() or None,
            edge_id=str(rule_props.get("rule_id") or "").strip() or None,
            formula_kind=str(rule_props.get("formula_kind") or "").strip() or None,
            confidence=rule_props.get("confidence"),
            status=self._normalize_status(rule_props.get("status"), default="approved"),
        )

    def find_rule(
        self,
        field_name: str,
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_field: Optional[str] = None,
    ) -> Optional[KnowledgeRule]:
        rules = self.list_rules(
            message_code=message_code,
            field_names=[field_name],
            target_protocol_type=target_protocol_type,
            target_message_code=target_message_code,
            target_fields=[target_field] if target_field else None,
        )
        return rules[0] if rules else None

    def list_rules(
        self,
        message_code: Optional[str] = None,
        field_names: Optional[List[str]] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_fields: Optional[List[str]] = None,
    ) -> List[KnowledgeRule]:
        if self._uses_external_entity_graph(self.schema_mode):
            normalized_fields: set[str] = set()
            for item in field_names or []:
                normalized_fields.update(self._source_field_keys(item, self.protocol_type))
            normalized_targets: set[str] = set()
            for item in target_fields or []:
                normalized_targets.update(self._target_field_keys(item, target_protocol_type))
            normalized_message = self._normalize_message_code(message_code)
            normalized_target_protocol = str(target_protocol_type or "").strip() or None
            normalized_target_message = self._normalize_message_code(target_message_code)
            allowed_statuses = self._get_allowed_read_statuses()
            candidates: List[Tuple[int, KnowledgeRule]] = []
            seen = set()
            for rule in self._load_external_rules():
                if rule.status and allowed_statuses and rule.status not in allowed_statuses:
                    continue
                source_names = set()
                for source_name in (rule.source_fields or [rule.field_name]):
                    source_names.update(self._source_field_keys(source_name, rule.protocol_type))
                source_names.update(self._source_field_keys(rule.field_name, rule.protocol_type))
                for alias in rule.aliases:
                    source_names.update(self._source_field_keys(alias, rule.protocol_type))
                if normalized_fields and source_names.isdisjoint(normalized_fields):
                    continue
                if normalized_message and rule.message_code not in {None, normalized_message}:
                    continue
                if normalized_target_protocol and rule.target_protocol_type not in {None, normalized_target_protocol}:
                    continue
                if normalized_target_message and rule.target_message_code not in {None, normalized_target_message}:
                    continue
                rule_target_keys = self._target_field_keys(
                    rule.target_field,
                    rule.target_protocol_type or normalized_target_protocol,
                )
                if normalized_targets and rule_target_keys.isdisjoint(normalized_targets):
                    continue
                signature = self._rule_signature(rule)
                if signature in seen:
                    continue
                seen.add(signature)
                score = 0
                if normalized_message and rule.message_code == normalized_message:
                    score += 30
                elif rule.message_code is None:
                    score += 8
                if normalized_fields and rule.field_name in normalized_fields:
                    score += 12
                elif normalized_fields and not set(rule.aliases).isdisjoint(normalized_fields):
                    score += 6
                if normalized_target_protocol and rule.target_protocol_type == normalized_target_protocol:
                    score += 16
                if normalized_target_message and rule.target_message_code == normalized_target_message:
                    score += 12
                if normalized_targets and not rule_target_keys.isdisjoint(normalized_targets):
                    score += 10
                score += int(float(rule.confidence or 0.0) * 5)
                candidates.append((score, rule))
            candidates.sort(key=lambda item: (-item[0], item[1].target_field or item[1].field_name, item[1].formula))
            return [rule for _, rule in candidates]

        normalized_fields = {self._normalize_field_name(item) for item in (field_names or []) if self._normalize_field_name(item)}
        normalized_targets: set[str] = set()
        for item in target_fields or []:
            normalized_targets.update(self._target_field_keys(item, target_protocol_type))
        normalized_message = self._normalize_message_code(message_code)
        normalized_target_protocol = str(target_protocol_type or "").strip() or None
        normalized_target_message = self._normalize_message_code(target_message_code)

        candidates: List[Tuple[int, KnowledgeRule]] = []
        seen = set()
        for record in self._query_rule_records():
            rule = self._record_to_rule(record)
            if rule is None:
                continue
            if rule.status and self.settings.read_statuses and rule.status not in set(self.settings.read_statuses):
                continue

            source_names = set(rule.source_fields or [rule.field_name])
            source_names.add(rule.field_name)
            source_names.update(rule.aliases)
            if normalized_fields and source_names.isdisjoint(normalized_fields):
                continue
            if normalized_message and rule.message_code not in {None, normalized_message}:
                continue
            if normalized_target_protocol and rule.target_protocol_type not in {None, normalized_target_protocol}:
                continue
            if normalized_target_message and rule.target_message_code not in {None, normalized_target_message}:
                continue
            rule_target_keys = self._target_field_keys(
                rule.target_field,
                rule.target_protocol_type or normalized_target_protocol,
            )
            if normalized_targets and rule_target_keys.isdisjoint(normalized_targets):
                continue

            signature = self._rule_signature(rule)
            if signature in seen:
                continue
            seen.add(signature)
            score = 0
            if normalized_message and rule.message_code == normalized_message:
                score += 30
            elif rule.message_code is None:
                score += 8
            if normalized_fields and rule.field_name in normalized_fields:
                score += 12
            elif normalized_fields and not set(rule.aliases).isdisjoint(normalized_fields):
                score += 6
            if normalized_target_protocol and rule.target_protocol_type == normalized_target_protocol:
                score += 16
            if normalized_target_message and rule.target_message_code == normalized_target_message:
                score += 12
            if normalized_targets and not rule_target_keys.isdisjoint(normalized_targets):
                score += 10
            score += int(float(rule.confidence or 0.0) * 5)
            candidates.append((score, rule))

        candidates.sort(key=lambda item: (-item[0], item[1].target_field or item[1].field_name, item[1].formula))
        return [rule for _, rule in candidates]

    def find_rules_for_source_fields(
        self,
        source_fields: Iterable[str],
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_fields: Optional[List[str]] = None,
    ) -> List[KnowledgeRule]:
        wanted: set[str] = set()
        for item in source_fields:
            wanted.update(self._source_field_keys(item, self.protocol_type))
        if not wanted:
            return []
        candidates = self.list_rules(
            message_code=message_code,
            field_names=list(wanted),
            target_protocol_type=target_protocol_type,
            target_message_code=target_message_code,
            target_fields=target_fields,
        )
        matched: List[KnowledgeRule] = []
        seen = set()
        for rule in candidates:
            source_field_groups = [
                self._source_field_keys(source_name, rule.protocol_type)
                for source_name in (rule.source_fields or [rule.field_name])
            ]
            if any(not group or group.isdisjoint(wanted) for group in source_field_groups):
                continue
            signature = self._rule_signature(rule)
            if signature in seen:
                continue
            seen.add(signature)
            matched.append(rule)
        return matched

    def upsert_generated_rules(
        self,
        rules: Iterable[Any],
        protocol_type: Optional[str] = None,
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        source: str = "llm",
    ) -> List[KnowledgeRule]:
        if self._uses_external_entity_graph(self.schema_mode):
            written_rules: List[KnowledgeRule] = []
            for item in rules:
                rule = self._normalize_rule_input(
                    item,
                    protocol_type=protocol_type,
                    message_code=message_code,
                    target_protocol_type=target_protocol_type,
                    target_message_code=target_message_code,
                    source=source,
                    default_status=self.settings.default_write_status,
                )
                if rule is None:
                    continue
                source_protocol = rule.protocol_type or protocol_type or self.protocol_type
                source_message = rule.message_code or message_code
                target_protocol = rule.target_protocol_type or target_protocol_type or source_protocol
                target_message = rule.target_message_code or target_message_code
                source_fields = rule.source_fields or [rule.field_name]
                target_field = rule.target_field or rule.field_name
                concept_name = rule.concept_name or self._infer_concept_name(rule.field_name, target_field)
                rule_id = rule.edge_id or self._rule_node_id(
                    source_protocol_type=source_protocol,
                    source_message_code=source_message,
                    target_protocol_type=target_protocol,
                    target_message_code=target_message,
                    target_field=target_field,
                    source_fields=source_fields,
                    formula=rule.formula,
                    conversion_mode=rule.conversion_mode,
                )
                map_query = """
                UNWIND $source_fields AS source_field
                MERGE (src:ProtocolField {name: source_field})
                MERGE (target:ProtocolField {name: $target_field})
                MERGE (src)-[r:MAP_TO {rule_id: $rule_id}]->(target)
                SET r.desc = $description,
                    r.type = $relation_type,
                    r.formula = $formula,
                    r.formula_kind = $formula_kind,
                    r.source_fields_json = $source_fields_json,
                    r.conversion_mode = $conversion_mode,
                    r.source_protocol_type = $source_protocol_type,
                    r.source_message_code = $source_message_code,
                    r.target_protocol_type = $target_protocol_type,
                    r.target_message_code = $target_message_code,
                    r.target_field = $target_field,
                    r.target_actual_field = $target_field,
                    r.concept_name = $concept_name,
                    r.source = $source,
                    r.status = $status,
                    r.confidence = $confidence
                """
                map_parameters = {
                    "rule_id": rule_id,
                    "source_fields": list(source_fields),
                    "target_field": target_field,
                    "description": rule.description,
                    "relation_type": rule.conversion_mode,
                    "formula": rule.formula,
                    "formula_kind": rule.formula_kind or self._infer_formula_kind(rule.formula),
                    "source_fields_json": json.dumps(list(source_fields), ensure_ascii=False),
                    "conversion_mode": rule.conversion_mode,
                    "source_protocol_type": source_protocol,
                    "source_message_code": source_message,
                    "target_protocol_type": target_protocol,
                    "target_message_code": target_message,
                    "concept_name": concept_name,
                    "source": rule.source or source,
                    "status": self._normalize_status(rule.status, default=self.settings.default_write_status),
                    "confidence": rule.confidence,
                }
                if self.schema_mode == PROTOCOLFIELD_GRAPH_MODE:
                    self._run_cypher(map_query, map_parameters)
                else:
                    self._run_cypher(
                        """
                        MATCH ()-[r:MAP_TO {rule_id: $rule_id}]->()
                        DELETE r
                        """,
                        {"rule_id": rule_id},
                    )
                    self._run_cypher(
                        """
                        MATCH ()-[r:G {rule_id: $rule_id}]->()
                        DELETE r
                        """,
                        {"rule_id": rule_id},
                    )
                entity_query = """
                UNWIND $source_fields AS source_field
                MERGE (src:Entity {name: source_field})
                MERGE (target:Entity {name: $target_entity})
                MERGE (src)-[r:G {rule_id: $rule_id}]->(target)
                SET r.name = $relation_name,
                    r.desc = $description,
                    r.expr = $expr,
                    r.fullLabelFrom = $full_label_from,
                    r.mainbrach = coalesce(r.mainbrach, ''),
                    r.formula = $formula,
                    r.formula_kind = $formula_kind,
                    r.source_fields_json = $source_fields_json,
                    r.conversion_mode = $conversion_mode,
                    r.source_protocol_type = $source_protocol_type,
                    r.source_message_code = $source_message_code,
                    r.target_protocol_type = $target_protocol_type,
                    r.target_message_code = $target_message_code,
                    r.target_field = $target_field,
                    r.concept_name = $concept_name,
                    r.source = $source,
                    r.status = $status,
                    r.confidence = $confidence
                """
                entity_target_name = concept_name if self.schema_mode == PROTOCOLFIELD_GRAPH_MODE else target_field
                entity_expr = f"{concept_name}:{rule.formula}" if self.schema_mode == PROTOCOLFIELD_GRAPH_MODE else rule.formula
                entity_parameters = {
                    "rule_id": rule_id,
                    "source_fields": list(source_fields),
                    "target_entity": entity_target_name,
                    "relation_name": concept_name or target_field,
                    "description": rule.description,
                    "expr": entity_expr,
                    "full_label_from": ";".join(source_fields),
                    "formula": rule.formula,
                    "formula_kind": rule.formula_kind or self._infer_formula_kind(rule.formula),
                    "source_fields_json": json.dumps(list(source_fields), ensure_ascii=False),
                    "conversion_mode": rule.conversion_mode,
                    "source_protocol_type": source_protocol,
                    "source_message_code": source_message,
                    "target_protocol_type": target_protocol,
                    "target_message_code": target_message,
                    "target_field": target_field,
                    "concept_name": concept_name,
                    "source": rule.source or source,
                    "status": self._normalize_status(rule.status, default=self.settings.default_write_status),
                    "confidence": rule.confidence,
                }
                self._run_cypher(entity_query, entity_parameters)
                written_rules.append(
                    KnowledgeRule(
                        protocol_type=source_protocol,
                        message_code=self._normalize_message_code(source_message),
                        field_name=self._normalize_field_name(source_fields[0]),
                        source_fields=[self._normalize_field_name(item) for item in source_fields],
                        conversion_mode=rule.conversion_mode,
                        formula=rule.formula,
                        target_field=self._normalize_field_name(target_field),
                        target_protocol_type=self._normalize_protocol_type(target_protocol),
                        target_message_code=self._normalize_message_code(target_message),
                        unit=rule.unit,
                        aliases=[self._normalize_field_name(alias) for alias in rule.aliases],
                        source=rule.source or source,
                        description=rule.description,
                        bit_length=rule.bit_length,
                        concept_name=concept_name,
                        edge_id=rule_id,
                        formula_kind=rule.formula_kind or self._infer_formula_kind(rule.formula),
                        confidence=rule.confidence,
                        status=self._normalize_status(rule.status, default=self.settings.default_write_status),
                    )
                )
            self._invalidate_external_cache()
            return written_rules

        written_rules: List[KnowledgeRule] = []
        for item in rules:
            rule = self._normalize_rule_input(
                item,
                protocol_type=protocol_type,
                message_code=message_code,
                target_protocol_type=target_protocol_type,
                target_message_code=target_message_code,
                source=source,
                default_status=self.settings.default_write_status,
            )
            if rule is None:
                continue

            source_protocol = rule.protocol_type or protocol_type or self.protocol_type
            source_message = rule.message_code or message_code
            target_protocol = rule.target_protocol_type or target_protocol_type or source_protocol
            target_message = rule.target_message_code or target_message_code
            source_fields = rule.source_fields or [rule.field_name]
            target_field = rule.target_field or rule.field_name
            concept_name = rule.concept_name or self._infer_concept_name(rule.field_name, target_field)
            concept_id = self._concept_id(concept_name)
            target_node_id = self._field_node_id(target_protocol, target_message, target_field)
            rule_id = rule.edge_id or self._rule_node_id(
                source_protocol_type=source_protocol,
                source_message_code=source_message,
                target_protocol_type=target_protocol,
                target_message_code=target_message,
                target_field=target_field,
                source_fields=source_fields,
                formula=rule.formula,
                conversion_mode=rule.conversion_mode,
            )

            query = """
            MERGE (c:Concept {concept_id: $concept_id})
            SET c.name = $concept_name,
                c.description = coalesce(c.description, $concept_description)
            MERGE (target:Field {node_id: $target_node_id})
            SET target.protocol_type = $target_protocol_type,
                target.message_code = $target_message_code,
                target.field_name = $target_field,
                target.unit = $unit,
                target.bit_length = $bit_length
            MERGE (target)-[:EXPRESSES]->(c)
            MERGE (r:Rule {rule_id: $rule_id})
            SET r.source_protocol_type = $source_protocol_type,
                r.source_message_code = $source_message_code,
                r.target_protocol_type = $target_protocol_type,
                r.target_message_code = $target_message_code,
                r.target_field = $target_field,
                r.source_fields = $source_fields,
                r.conversion_mode = $conversion_mode,
                r.formula = $formula,
                r.formula_kind = $formula_kind,
                r.source = $source,
                r.description = $description,
                r.confidence = $confidence,
                r.status = $status,
                r.concept_name = $concept_name
            MERGE (r)-[:PRODUCES_TARGET]->(target)
            MERGE (r)-[:ABOUT_CONCEPT]->(c)
            WITH r, c
            UNWIND $source_nodes AS source_node
            MERGE (src:Field {node_id: source_node.node_id})
            SET src.protocol_type = source_node.protocol_type,
                src.message_code = source_node.message_code,
                src.field_name = source_node.field_name,
                src.aliases = source_node.aliases,
                src.unit = source_node.unit,
                src.bit_length = source_node.bit_length
            MERGE (src)-[:EXPRESSES]->(c)
            MERGE (r)-[:USES_SOURCE]->(src)
            """
            parameters = {
                "concept_id": concept_id,
                "concept_name": concept_name,
                "concept_description": f"Protocol conversion concept for {concept_name}.",
                "rule_id": rule_id,
                "source_protocol_type": source_protocol,
                "source_message_code": source_message,
                "target_protocol_type": target_protocol,
                "target_message_code": target_message,
                "target_field": target_field,
                "target_node_id": target_node_id,
                "source_fields": source_fields,
                "conversion_mode": rule.conversion_mode,
                "formula": rule.formula,
                "formula_kind": rule.formula_kind or self._infer_formula_kind(rule.formula),
                "source": rule.source or source,
                "description": rule.description,
                "confidence": rule.confidence,
                "status": self._normalize_status(rule.status, default=self.settings.default_write_status),
                "unit": rule.unit,
                "bit_length": rule.bit_length,
                "source_nodes": [
                    {
                        "node_id": self._field_node_id(source_protocol, source_message, source_field),
                        "protocol_type": source_protocol,
                        "message_code": source_message,
                        "field_name": source_field,
                        "aliases": list(rule.aliases),
                        "unit": rule.unit,
                        "bit_length": rule.bit_length,
                    }
                    for source_field in source_fields
                ],
            }
            self._run_cypher(query, parameters)

            if rule.description:
                evidence_query = """
                MERGE (e:Evidence {evidence_id: $evidence_id})
                SET e.source_type = $source,
                    e.snippet = $snippet
                WITH e
                MATCH (r:Rule {rule_id: $rule_id})
                MERGE (r)-[:SUPPORTED_BY]->(e)
                """
                self._run_cypher(
                    evidence_query,
                    {
                        "evidence_id": self._evidence_id(rule_id, rule.description, rule.source or source),
                        "source": rule.source or source,
                        "snippet": rule.description,
                        "rule_id": rule_id,
                    },
                )

            written_rules.append(
                KnowledgeRule(
                    protocol_type=source_protocol,
                    message_code=self._normalize_message_code(source_message),
                    field_name=self._normalize_field_name(source_fields[0]),
                    source_fields=[self._normalize_field_name(item) for item in source_fields],
                    conversion_mode=rule.conversion_mode,
                    formula=rule.formula,
                    target_field=self._normalize_field_name(target_field),
                    target_protocol_type=self._normalize_protocol_type(target_protocol),
                    target_message_code=self._normalize_message_code(target_message),
                    unit=rule.unit,
                    aliases=[self._normalize_field_name(alias) for alias in rule.aliases],
                    source=rule.source or source,
                    description=rule.description,
                    bit_length=rule.bit_length,
                    concept_name=concept_name,
                    edge_id=rule_id,
                    formula_kind=rule.formula_kind or self._infer_formula_kind(rule.formula),
                    confidence=rule.confidence,
                    status=self._normalize_status(rule.status, default=self.settings.default_write_status),
                )
            )
        return written_rules

    def to_summary(self) -> Dict[str, Any]:
        return {
            "protocol_type": self.protocol_type,
            "version": self.version,
            "embedding_model": self.embedding_model,
            "backend": self.backend,
            "uri": self.settings.uri,
            "database": self.settings.database,
            "schema_mode": getattr(self, "schema_mode", "native_v2"),
            "read_statuses": list(self.settings.read_statuses),
            "default_write_status": self.settings.default_write_status,
        }


class CompositeProtocolConversionKnowledgeBase(ProtocolConversionKnowledgeBase):
    """Composite repository that prefers Neo4j and falls back to local JSON."""

    def __init__(
        self,
        protocol_type: str,
        primary: Neo4jProtocolConversionKnowledgeBase,
        fallback: ProtocolConversionKnowledgeBase,
    ):
        self.protocol_type = self._normalize_protocol_type(protocol_type)
        self.primary = primary
        self.fallback = fallback
        self.embedding_model = getattr(primary, "embedding_model", fallback.embedding_model)
        self.version = f"{getattr(primary, 'version', 'neo4j_graph')}+{fallback.version}"
        self.backend = "neo4j_graph+local_json_graph"
        self.description = "Composite Neo4j-first protocol conversion knowledge graph."
        self.file_path = fallback.file_path

    def find_rule(
        self,
        field_name: str,
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_field: Optional[str] = None,
    ) -> Optional[KnowledgeRule]:
        rules = self.list_rules(
            message_code=message_code,
            field_names=[field_name],
            target_protocol_type=target_protocol_type,
            target_message_code=target_message_code,
            target_fields=[target_field] if target_field else None,
        )
        return rules[0] if rules else None

    def list_rules(
        self,
        message_code: Optional[str] = None,
        field_names: Optional[List[str]] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_fields: Optional[List[str]] = None,
    ) -> List[KnowledgeRule]:
        try:
            primary_rules = self.primary.list_rules(
                message_code=message_code,
                field_names=field_names,
                target_protocol_type=target_protocol_type,
                target_message_code=target_message_code,
                target_fields=target_fields,
            )
            if primary_rules:
                return primary_rules
        except Exception:
            pass

        return self.fallback.list_rules(
            message_code=message_code,
            field_names=field_names,
            target_protocol_type=target_protocol_type,
            target_message_code=target_message_code,
            target_fields=target_fields,
        )

    def find_rules_for_source_fields(
        self,
        source_fields: Iterable[str],
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        target_fields: Optional[List[str]] = None,
    ) -> List[KnowledgeRule]:
        try:
            primary_rules = self.primary.find_rules_for_source_fields(
                source_fields=source_fields,
                message_code=message_code,
                target_protocol_type=target_protocol_type,
                target_message_code=target_message_code,
                target_fields=target_fields,
            )
            if primary_rules:
                return primary_rules
        except Exception:
            pass

        return self.fallback.find_rules_for_source_fields(
            source_fields=source_fields,
            message_code=message_code,
            target_protocol_type=target_protocol_type,
            target_message_code=target_message_code,
            target_fields=target_fields,
        )

    def upsert_generated_rules(
        self,
        rules: Iterable[Any],
        protocol_type: Optional[str] = None,
        message_code: Optional[str] = None,
        target_protocol_type: Optional[str] = None,
        target_message_code: Optional[str] = None,
        source: str = "llm",
    ) -> List[KnowledgeRule]:
        materialized = list(rules)
        primary_rules: List[KnowledgeRule] = []
        fallback_rules: List[KnowledgeRule] = []

        try:
            primary_rules = self.primary.upsert_generated_rules(
                materialized,
                protocol_type=protocol_type,
                message_code=message_code,
                target_protocol_type=target_protocol_type,
                target_message_code=target_message_code,
                source=source,
            )
        except Exception:
            primary_rules = []

        if self.primary.settings.write_fallback_json or not primary_rules:
            fallback_rules = self.fallback.upsert_generated_rules(
                materialized,
                protocol_type=protocol_type,
                message_code=message_code,
                target_protocol_type=target_protocol_type,
                target_message_code=target_message_code,
                source=source,
            )

        return primary_rules or fallback_rules

    def to_summary(self) -> Dict[str, Any]:
        primary_summary = self.primary.to_summary()
        fallback_summary = self.fallback.to_summary()
        return {
            "protocol_type": self.protocol_type,
            "version": self.version,
            "embedding_model": self.embedding_model,
            "backend": self.backend,
            "primary": primary_summary,
            "fallback": fallback_summary,
            "file_path": str(self.file_path),
        }

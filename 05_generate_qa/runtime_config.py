"""Runtime config loader for this interface project."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_NAME = PROJECT_ROOT.name
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# 模型缓存根目录 — 优先使用环境变量，默认 ~/model_cache
_model_cache = Path(os.getenv("MODEL_CACHE_DIR", str(Path.home() / "model_cache")))

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "server": {"host": "0.0.0.0", "port": 6105, "debug": True, "threaded": False},
    "llm": {
        "openai_api_key": "local-qwen",
        "openai_base_url": "http://127.0.0.1:8000/v1",
        "openai_model": "qwen-local",
        "llm_model_name": "Qwen/Qwen3-4B",
        "use_vllm": True,
        "vllm_url": "http://127.0.0.1:8000",
    },
    "embedding": {
        "model_name": "qwen3-0.6b-embedding",
        "model_dir": str(_model_cache / "Qwen" / "Qwen3-Embedding-0.6B"),
    },
    "reranker": {
        "model_name": "Qwen3-Reranker-0.6B",
        "model_dir": str(_model_cache / "Qwen3-Reranker-0.6B"),
    },
    "databases": {
        "mysql": {
            "host": "127.0.0.1",
            "port": 3306,
            "user": "root",
            "password": "password",
            "database": "protocol_db",
            "use_sqlite": False,
            "auto_fallback_sqlite": True,
        },
        "milvus": {
            "host": "127.0.0.1",
            "port": 19530,
            "db": "protocol_db",
            "uri": "",
            "lite_uri": "./runtime/milvus/milvus_lite.db",
            "auto_fallback_lite": True,
        },
        "neo4j": {
            "enabled": True,
            "uri": "bolt://127.0.0.1:7687",
            "username": "neo4j",
            "password": "change_me",
            "database": "neo4j",
            "timeout_seconds": 5.0,
            "auto_init": True,
            "read_statuses": ["approved", "verified"],
            "write_status": "candidate",
        },
    },
}

_CACHE: Dict[str, "RuntimeConfig"] = {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def _resolve_path(raw_path: Any, default_path: Path | None = None) -> str:
    text = str(raw_path or "").strip()
    if text:
        path = Path(os.path.expanduser(text))
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return str(path)
    if default_path is None:
        return ""
    return str(default_path.resolve())


def _model_label(model_config: Dict[str, Any], fallback: str) -> str:
    model_name = str(model_config.get("model_name") or "").strip()
    if model_name:
        return model_name
    model_dir = str(model_config.get("model_dir") or "").strip()
    if model_dir:
        return Path(os.path.expanduser(model_dir)).name or fallback
    return fallback


def _default_runtime_paths() -> Dict[str, Path]:
    runtime_root = PROJECT_ROOT / "runtime"
    return {
        "runtime_root": runtime_root,
        "data_root": runtime_root / "data",
        "temp_root": runtime_root / "tmp",
        "deliverables_root": runtime_root / "deliverables",
        "finetune_output_dir": runtime_root / "finetune" / "models",
        "finetune_checkpoint_dir": runtime_root / "finetune" / "checkpoints",
        "milvus_lite_uri": runtime_root / "milvus" / "milvus_lite.db",
    }


def _infer_model_cache_dir(embedding: Dict[str, Any], reranker: Dict[str, Any]) -> str:
    explicit = str(os.getenv("MODEL_CACHE_DIR") or "").strip()
    if explicit:
        return _resolve_path(explicit)
    for raw in [embedding.get("model_dir"), reranker.get("model_dir")]:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(os.path.expanduser(text)).resolve()
        if path.parent.name.lower() == "qwen" and path.parent.parent != path.parent:
            return str(path.parent.parent)
        return str(path.parent)
    return str(Path(os.path.expanduser("~/model_cache")).resolve())


class RuntimeConfig:
    """Loads local config.yaml and exports env defaults for one interface project."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        configured_path = (
            config_path
            or os.getenv("INTERFACE_PROJECT_CONFIG_PATH")
            or DEFAULT_CONFIG_PATH
        )
        self.config_path = Path(configured_path).expanduser()
        if not self.config_path.is_absolute():
            self.config_path = (PROJECT_ROOT / self.config_path).resolve()
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.config_path.exists():
            loaded = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置文件必须是对象: {self.config_path}")
            payload = loaded
        return _deep_merge(DEFAULT_CONFIG, payload)

    def get_server(self) -> Dict[str, Any]:
        server = self.data.get("server", {})
        merged = {"host": "0.0.0.0", "port": 6105, "debug": True, "threaded": False}
        if isinstance(server, dict):
            merged.update(server)
        merged["port"] = int(merged.get("port") or 6105)
        merged["debug"] = bool(merged.get("debug", True))
        merged["threaded"] = bool(merged.get("threaded", False))
        return merged

    def _runtime_paths(self) -> Dict[str, str]:
        defaults = _default_runtime_paths()
        milvus = self.data["databases"]["milvus"]
        return {
            "runtime_root": _resolve_path("", defaults["runtime_root"]),
            "data_root": _resolve_path("", defaults["data_root"]),
            "temp_root": _resolve_path("", defaults["temp_root"]),
            "deliverables_root": _resolve_path("", defaults["deliverables_root"]),
            "finetune_output_dir": _resolve_path("", defaults["finetune_output_dir"]),
            "finetune_checkpoint_dir": _resolve_path("", defaults["finetune_checkpoint_dir"]),
            "milvus_lite_uri": _resolve_path(milvus.get("lite_uri"), defaults["milvus_lite_uri"]),
        }

    def get_shared_config(self, project_root: str | Path | None = None) -> Dict[str, Any]:
        runtime_paths = self._runtime_paths()
        llm = self.data["llm"]
        embedding = self.data["embedding"]
        reranker = self.data["reranker"]
        mysql = self.data["databases"]["mysql"]
        milvus = self.data["databases"]["milvus"]
        return {
            "PROJECT_ROOT": str(PROJECT_ROOT if project_root is None else Path(project_root).resolve()),
            "PROJECT_NAME": PROJECT_NAME,
            "MODEL_CACHE_DIR": _infer_model_cache_dir(embedding, reranker),
            "EMBED_MODEL_NAME": _model_label(embedding, "qwen3-0.6b-embedding"),
            "RERANK_MODEL_NAME": _model_label(reranker, "Qwen3-Reranker-0.6B"),
            "LLM_MODEL_NAME": str(llm["llm_model_name"] or llm["openai_model"]),
            "USE_VLLM": bool(llm["use_vllm"]),
            "VLLM_URL": str(llm["vllm_url"] or llm["openai_base_url"]),
            "MYSQL_HOST": str(mysql["host"]),
            "MYSQL_PORT": int(mysql["port"]),
            "MYSQL_USER": str(mysql["user"]),
            "MYSQL_PASSWORD": str(mysql["password"]),
            "MYSQL_DATABASE": str(mysql["database"]),
            "MILVUS_HOST": str(milvus["host"]),
            "MILVUS_PORT": int(milvus["port"]),
            "MILVUS_DB": str(milvus["db"]),
            "DATA_DIR": runtime_paths["data_root"],
            "SERVICE_PORTS": {},
            "API_ENDPOINTS": {
                "upload_split": "/api/data/upload_split",
                "clean": "/api/data/clean",
                "extract_validate_qa": "/api/knowledge/extract_validate_qa",
                "generate_qa": "/api/knowledge/generate_qa",
                "finetune_action": "/api/model/finetune/action",
                "finetune_stream": "/api/model/finetune/stream",
                "semantic_chunk": "/api/data/semantic_chunk",
            },
            "TRAINING_CONFIG": {
                "default_base_model": str(llm["llm_model_name"] or llm["openai_model"]),
                "default_epochs": 3,
                "default_learning_rate": 2e-4,
                "default_batch_size": 4,
                "default_lora_rank": 16,
                "default_lora_alpha": 32,
                "default_lora_dropout": 0.05,
                "default_max_length": 2048,
                "checkpoint_dir": runtime_paths["finetune_checkpoint_dir"],
                "output_dir": runtime_paths["finetune_output_dir"],
            },
        }

    def export_env_defaults(self) -> Dict[str, str]:
        runtime_paths = self._runtime_paths()
        llm = self.data["llm"]
        embedding = self.data["embedding"]
        reranker = self.data["reranker"]
        mysql = self.data["databases"]["mysql"]
        milvus = self.data["databases"]["milvus"]
        neo4j = self.data["databases"]["neo4j"]
        embed_model_dir = _resolve_path(embedding.get("model_dir"))
        rerank_model_dir = _resolve_path(reranker.get("model_dir"))
        env: Dict[str, str] = {
            "INTERFACE_PROJECT_RUNTIME_ROOT": runtime_paths["runtime_root"],
            "INTERFACE_PROJECT_DATA_ROOT": runtime_paths["data_root"],
            "INTERFACE_PROJECT_TEMP_ROOT": runtime_paths["temp_root"],
            "INTERFACE_PROJECT_DELIVERABLES_ROOT": runtime_paths["deliverables_root"],
            "INTERFACE_PROJECTS_RUNTIME_ROOT": runtime_paths["runtime_root"],
            "INTERFACE_PROJECTS_DATA_ROOT": runtime_paths["data_root"],
            "INTERFACE_PROJECTS_TEMP_ROOT": runtime_paths["temp_root"],
            "INTERFACE_PROJECTS_DELIVERABLES_ROOT": runtime_paths["deliverables_root"],
            "MODEL_CACHE_DIR": _infer_model_cache_dir(embedding, reranker),
            "OPENAI_API_KEY": str(llm["openai_api_key"]),
            "openai_api_key": str(llm["openai_api_key"]),
            "OPENAI_BASE_URL": str(llm["openai_base_url"]),
            "openai_base_url": str(llm["openai_base_url"]),
            "OPENAI_MODEL": str(llm["openai_model"]),
            "openai_model": str(llm["openai_model"]),
            "LLM_MODEL_NAME": str(llm["llm_model_name"] or llm["openai_model"]),
            "USE_VLLM": _normalize_bool(llm["use_vllm"]),
            "VLLM_URL": str(llm["vllm_url"] or llm["openai_base_url"]),
            "EMBED_MODEL_NAME": _model_label(embedding, "qwen3-0.6b-embedding"),
            "EMBED_MODEL_DIR": embed_model_dir,
            "EMBED_MODEL_PATH": embed_model_dir,
            "RERANK_MODEL_NAME": _model_label(reranker, "Qwen3-Reranker-0.6B"),
            "RERANK_MODEL_DIR": rerank_model_dir,
            "MYSQL_HOST": str(mysql["host"]),
            "MYSQL_PORT": str(mysql["port"]),
            "MYSQL_USER": str(mysql["user"]),
            "MYSQL_PASSWORD": str(mysql["password"]),
            "MYSQL_DATABASE": str(mysql["database"]),
            "SQLITE_DB_PATH": str((Path(runtime_paths["data_root"]) / (str(mysql["database"]) + ".sqlite3")).resolve()),
            "MYSQL_USE_SQLITE": _normalize_bool(mysql["use_sqlite"]),
            "MYSQL_AUTO_FALLBACK_SQLITE": _normalize_bool(mysql["auto_fallback_sqlite"]),
            "MILVUS_HOST": str(milvus["host"]),
            "MILVUS_PORT": str(milvus["port"]),
            "MILVUS_DB": str(milvus["db"]),
            "MILVUS_URI": str(milvus["uri"]),
            "MILVUS_LITE_URI": runtime_paths["milvus_lite_uri"],
            "MILVUS_AUTO_FALLBACK_LITE": _normalize_bool(milvus["auto_fallback_lite"]),
            "PROTOCOL_CONVERSION_NEO4J_ENABLED": _normalize_bool(neo4j["enabled"]),
            "PROTOCOL_CONVERSION_NEO4J_URI": str(neo4j["uri"]),
            "PROTOCOL_CONVERSION_NEO4J_USERNAME": str(neo4j["username"]),
            "PROTOCOL_CONVERSION_NEO4J_PASSWORD": str(neo4j["password"]),
            "PROTOCOL_CONVERSION_NEO4J_DATABASE": str(neo4j["database"]),
            "PROTOCOL_CONVERSION_NEO4J_TIMEOUT_SECONDS": str(neo4j["timeout_seconds"]),
            "PROTOCOL_CONVERSION_NEO4J_AUTO_INIT": _normalize_bool(neo4j["auto_init"]),
            "PROTOCOL_CONVERSION_NEO4J_READ_STATUSES": ",".join(str(item) for item in neo4j["read_statuses"]),
            "PROTOCOL_CONVERSION_NEO4J_WRITE_STATUS": str(neo4j["write_status"]),
            "NEO4J_URI": str(neo4j["uri"]),
            "NEO4J_USERNAME": str(neo4j["username"]),
            "NEO4J_PASSWORD": str(neo4j["password"]),
            "NEO4J_DATABASE": str(neo4j["database"]),
            "FINETUNE_OUTPUT_DIR": runtime_paths["finetune_output_dir"],
            "FINETUNE_CHECKPOINT_DIR": runtime_paths["finetune_checkpoint_dir"],
            "FINETUNE_BASE_MODEL": str(llm["llm_model_name"] or llm["openai_model"]),
        }
        return {key: value for key, value in env.items() if value != ""}

    def ensure_runtime_dirs(self) -> None:
        runtime_paths = self._runtime_paths()
        dirs = [
            runtime_paths["runtime_root"],
            runtime_paths["data_root"],
            runtime_paths["temp_root"],
            runtime_paths["deliverables_root"],
            runtime_paths["finetune_output_dir"],
            runtime_paths["finetune_checkpoint_dir"],
            runtime_paths["milvus_lite_uri"],
            _infer_model_cache_dir(self.data["embedding"], self.data["reranker"]),
        ]
        for raw_path in dirs:
            resolved = _resolve_path(raw_path)
            if not resolved:
                continue
            path = Path(resolved)
            target = path if path.suffix == "" else path.parent
            target.mkdir(parents=True, exist_ok=True)

    def apply_environment(self) -> None:
        # Interface config must override inherited deploy env so restarts pick up config.yaml changes.
        for key, value in self.export_env_defaults().items():
            os.environ[key] = value
        self.ensure_runtime_dirs()


def load_runtime_config(force_reload: bool = False) -> RuntimeConfig:
    configured_path = (
        os.getenv("INTERFACE_PROJECT_CONFIG_PATH")
        or os.getenv("INTERFACE_PROJECTS_CONFIG_PATH")
        or str(DEFAULT_CONFIG_PATH)
    )
    if force_reload or configured_path not in _CACHE:
        _CACHE[configured_path] = RuntimeConfig(configured_path)
    return _CACHE[configured_path]


def apply_runtime_environment(force_reload: bool = False) -> RuntimeConfig:
    runtime = load_runtime_config(force_reload=force_reload)
    runtime.apply_environment()
    return runtime


def get_service_runner_config(project_name: str | None = None) -> Dict[str, Any]:
    return load_runtime_config().get_server()


def get_shared_project_config(project_root: str | Path | None = None) -> Dict[str, Any]:
    runtime = apply_runtime_environment()
    return runtime.get_shared_config(project_root)

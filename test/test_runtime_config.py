"""Unit tests for per-interface runtime config."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = ROOT_DIR / "07_protocol_generate_rules"
MODULE_PATH = PROJECT_DIR / "runtime_config.py"
GENERATOR_MODULE_PATH = ROOT_DIR / "scripts" / "generate_interface_configs.py"


def _load_runtime_module():
    spec = importlib.util.spec_from_file_location("interface07_runtime_config", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_generator_module():
    spec = importlib.util.spec_from_file_location("interface_config_generator", GENERATOR_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {GENERATOR_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeConfigTest(unittest.TestCase):
    """Covers one interface's self-contained config loader."""

    def setUp(self) -> None:
        self._module = _load_runtime_module()
        self._generator = _load_generator_module()
        self._old_path = os.environ.get("INTERFACE_PROJECT_CONFIG_PATH")
        self._old_legacy_path = os.environ.get("INTERFACE_PROJECTS_CONFIG_PATH")
        self._old_mysql_host = os.environ.get("MYSQL_HOST")
        self._old_openai_model = os.environ.get("OPENAI_MODEL")

    def tearDown(self) -> None:
        if self._old_path is None:
            os.environ.pop("INTERFACE_PROJECT_CONFIG_PATH", None)
        else:
            os.environ["INTERFACE_PROJECT_CONFIG_PATH"] = self._old_path
        if self._old_legacy_path is None:
            os.environ.pop("INTERFACE_PROJECTS_CONFIG_PATH", None)
        else:
            os.environ["INTERFACE_PROJECTS_CONFIG_PATH"] = self._old_legacy_path
        if self._old_mysql_host is None:
            os.environ.pop("MYSQL_HOST", None)
        else:
            os.environ["MYSQL_HOST"] = self._old_mysql_host
        if self._old_openai_model is None:
            os.environ.pop("OPENAI_MODEL", None)
        else:
            os.environ["OPENAI_MODEL"] = self._old_openai_model
        self._module.load_runtime_config(force_reload=True)

    def test_default_port_comes_from_local_config(self) -> None:
        runtime = self._module.load_runtime_config(force_reload=True)
        self.assertEqual(runtime.get_server()["port"], 6107)

    def test_runtime_dirs_resolve_under_project_root(self) -> None:
        runtime = self._module.apply_runtime_environment(force_reload=True)
        shared = runtime.get_shared_config()
        data_dir = Path(shared["DATA_DIR"])
        self.assertTrue(data_dir.exists())
        self.assertTrue(str(data_dir).startswith(str(PROJECT_DIR)))

    def test_environment_override_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "databases": {"mysql": {"host": "yaml-host"}},
                        "llm": {"openai_model": "yaml-model"},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            os.environ["INTERFACE_PROJECT_CONFIG_PATH"] = str(config_path)
            os.environ["MYSQL_HOST"] = "env-host"
            os.environ["OPENAI_MODEL"] = "env-model"
            self._module.apply_runtime_environment(force_reload=True)
            self.assertEqual(os.environ["MYSQL_HOST"], "env-host")
            self.assertEqual(os.environ["OPENAI_MODEL"], "env-model")

    def test_embedding_and_reranker_model_dirs_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "embedding": {"model_dir": "/tmp/embed-model"},
                        "reranker": {"model_dir": "/tmp/rerank-model"},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            os.environ["INTERFACE_PROJECT_CONFIG_PATH"] = str(config_path)
            self._module.apply_runtime_environment(force_reload=True)
            self.assertEqual(os.environ["EMBED_MODEL_DIR"], "/tmp/embed-model")
            self.assertEqual(os.environ["EMBED_MODEL_PATH"], "/tmp/embed-model")
            self.assertEqual(os.environ["RERANK_MODEL_DIR"], "/tmp/rerank-model")

    def test_generated_local_config_comes_from_global_plus_override(self) -> None:
        rendered = self._generator.render_interface_config("07_protocol_generate_rules")
        self.assertIn("Generated by interface_projects/scripts/generate_interface_configs.py", rendered)
        payload = yaml.safe_load(rendered)
        self.assertEqual(payload["server"]["port"], 6107)
        self.assertEqual(payload["llm"]["openai_base_url"], "http://127.0.0.1:8000/v1")
        self.assertEqual(payload["databases"]["neo4j"]["uri"], "bolt://127.0.0.1:7687")


if __name__ == "__main__":
    unittest.main()

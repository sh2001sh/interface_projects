"""Unit tests for document-oriented PageIndex registry building."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT_DIR = Path(__file__).resolve().parents[1]
INTERFACE_ROOT = ROOT_DIR / "02_upload_split"
TRAINED_DOC_INDEX_PATH = INTERFACE_ROOT / "shared" / "protocol_conversion" / "trained_doc_index.py"


def _clear_shared_modules() -> None:
    for name in list(sys.modules):
        if name == "shared" or name.startswith("shared."):
            sys.modules.pop(name, None)


def _load_trained_doc_index_module():
    _clear_shared_modules()
    shared_pkg = types.ModuleType("shared")
    shared_pkg.__path__ = [str(INTERFACE_ROOT / "shared")]
    sys.modules["shared"] = shared_pkg

    protocol_pkg = types.ModuleType("shared.protocol_conversion")
    protocol_pkg.__path__ = [str(INTERFACE_ROOT / "shared" / "protocol_conversion")]
    sys.modules["shared.protocol_conversion"] = protocol_pkg

    spec = importlib.util.spec_from_file_location(
        "shared.protocol_conversion.trained_doc_index",
        TRAINED_DOC_INDEX_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {TRAINED_DOC_INDEX_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakePageIndexClient:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def index(self, file_path: str, mode: str = "md") -> str:
        return Path(file_path).stem


class _FakeFileStore:
    def __init__(self):
        self.registries = {}
        self.doc_sets = {}
        self.dataset_meta = {}

    def load_pageindex_registry(self, project_id: str, doc_set_id: str):
        return self.registries.get((project_id, doc_set_id), {})

    def save_project_doc_set(self, project_id: str, doc_set_id: str, payload):
        self.doc_sets[(project_id, doc_set_id)] = payload
        return "doc-set.json"

    def save_pageindex_registry(self, project_id: str, doc_set_id: str, payload):
        self.registries[(project_id, doc_set_id)] = payload
        return "registry.json"

    def update_dataset_meta(self, dataset_id: str, updates):
        self.dataset_meta[dataset_id] = dict(updates)


class DocIndexBuilderTest(unittest.TestCase):
    def tearDown(self) -> None:
        _clear_shared_modules()

    def test_document_paths_are_written_into_registry(self) -> None:
        module = _load_trained_doc_index_module()
        store = _FakeFileStore()
        source_document_path = str(Path("~/samples/protocol_A.docx").expanduser())

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            module.PAGEINDEX_WORKSPACE_ROOT = temp_root / "workspace"
            module.PAGEINDEX_DOC_ROOT = temp_root / "docs"

            blocks = [
                SimpleNamespace(
                    block_id=101,
                    project_id="proj_demo",
                    file_name="protocol_A.docx",
                    page_num=1,
                    block_type="text",
                    metadata={"protocol_fields": [{"field_name": "K1.6"}]},
                    content="K1.6 := source value",
                    cleaned_content="K1.6 := source value",
                )
            ]

            registry = module.build_protocol_doc_index(
                project_id="proj_demo",
                dataset_id="dataset_demo",
                blocks=blocks,
                file_names=["protocol_A.docx"],
                document_paths=[source_document_path],
                source_block_ids=[101],
                file_store=store,
                client_factory=lambda workspace: _FakePageIndexClient(Path(workspace)),
            )

        self.assertEqual(registry["document_count"], 1)
        self.assertEqual(registry["source_documents"], [source_document_path])
        self.assertEqual(registry["documents"][0]["source_document_path"], source_document_path)

        saved_doc_set = store.doc_sets[("proj_demo", registry["doc_set_id"])]
        self.assertEqual(saved_doc_set["source_documents"], [source_document_path])
        self.assertEqual(saved_doc_set["documents"][0]["source_document_path"], source_document_path)


if __name__ == "__main__":
    unittest.main()

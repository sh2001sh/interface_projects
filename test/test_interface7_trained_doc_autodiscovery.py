from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
INTERFACE7_ROOT = ROOT_DIR / "07_protocol_generate_rules"
FILE_STORE_PATH = INTERFACE7_ROOT / "utils" / "file_store.py"
TRAINED_DOC_INDEX_PATH = INTERFACE7_ROOT / "protocol_conversion" / "trained_doc_index.py"


def _clear_interface7_modules() -> None:
    for name in list(sys.modules):
        if name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)
        if name == "protocol_conversion" or name.startswith("protocol_conversion."):
            sys.modules.pop(name, None)


def _load_file_store_module():
    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = [str(INTERFACE7_ROOT / "utils")]
    sys.modules["utils"] = utils_pkg

    spec = importlib.util.spec_from_file_location("utils.file_store", FILE_STORE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {FILE_STORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_trained_doc_index_module():
    file_store_module = _load_file_store_module()

    protocol_pkg = types.ModuleType("protocol_conversion")
    protocol_pkg.__path__ = [str(INTERFACE7_ROOT / "protocol_conversion")]
    sys.modules["protocol_conversion"] = protocol_pkg

    pageindex_module = types.ModuleType("protocol_conversion.pageindex_adapter")

    class _FakePageIndexEvidenceProvider:
        def __init__(self, workspace_dir=None, docs_dir=None, client_factory=None):
            self.workspace_dir = Path(workspace_dir) if workspace_dir else None
            self.docs_dir = Path(docs_dir) if docs_dir else None
            self.client_factory = client_factory or (lambda workspace: object())
            self._client = None

        def _get_client(self):
            if self._client is None:
                workspace = self.workspace_dir or Path(tempfile.gettempdir())
                self._client = self.client_factory(workspace)
            return self._client

        def _get_or_create_document(self, client, role, protocol):
            return f"{role}-doc"

        def _extract_source_queries(self, source_protocol, source_message=None):
            queries = []
            for value in (
                source_protocol.get("protocol_type"),
                source_protocol.get("message_code"),
            ):
                if str(value or "").strip():
                    queries.append(str(value).strip())
            if isinstance(source_message, dict):
                queries.extend(str(key).strip() for key in source_message.keys() if str(key).strip())
            return queries

        def _extract_target_queries(self, target_protocol):
            queries = target_protocol.get("field_queries") or []
            return [str(item).strip() for item in queries if str(item).strip()]

        def _collect_role_snippets(self, client, doc_id, role, protocol, queries, top_k):
            if "wrong" in str(doc_id):
                return []
            return [
                {
                    "doc_id": doc_id,
                    "role": role,
                    "query": query,
                    "title": f"{role}:{query}",
                    "content": f"{doc_id}:{query}",
                    "score": 10.0 - index,
                }
                for index, query in enumerate(list(queries or [])[:top_k])
            ]

    pageindex_module.PageIndexEvidenceProvider = _FakePageIndexEvidenceProvider
    pageindex_module._default_pageindex_client_factory = lambda workspace: object()
    sys.modules["protocol_conversion.pageindex_adapter"] = pageindex_module

    spec = importlib.util.spec_from_file_location(
        "protocol_conversion.trained_doc_index",
        TRAINED_DOC_INDEX_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {TRAINED_DOC_INDEX_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, file_store_module


def test_interface7_file_store_resolves_registry_from_global_roots():
    _clear_interface7_modules()
    file_store_module = _load_file_store_module()
    FileStore = file_store_module.FileStore

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        local_data_root = temp_root / "local_data"
        shared_registry_root = temp_root / "shared_registry_root"
        project_registry_dir = shared_registry_root / "proj_demo"
        project_registry_dir.mkdir(parents=True, exist_ok=True)

        registry_path = project_registry_dir / "docset_demo.json"
        registry_path.write_text(
            (
                '{"project_id":"proj_demo","dataset_id":"dataset_demo","doc_set_id":"docset_demo",'
                '"index_ref":"idx_demo","workspace_dir":"/tmp/ws","docs_dir":"/tmp/docs","documents":[]}'
            ),
            encoding="utf-8",
        )

        store = FileStore(base_dir=str(local_data_root))
        store._candidate_pageindex_registry_roots = lambda: [str(shared_registry_root)]  # type: ignore[method-assign]

        registry = store.resolve_pageindex_registry(
            project_id="proj_demo",
            doc_set_id="docset_demo",
            index_ref="idx_demo",
        )
        assert registry["doc_set_id"] == "docset_demo"
        assert registry["_registry_path"] == str(registry_path.resolve())

        registry_without_project = store.resolve_pageindex_registry(
            dataset_id="dataset_demo",
            doc_set_id="docset_demo",
            index_ref="idx_demo",
        )
        assert registry_without_project["project_id"] == "proj_demo"
        assert registry_without_project["_registry_path"] == str(registry_path.resolve())

    _clear_interface7_modules()


def test_interface7_trained_doc_provider_auto_discovers_relevant_registry():
    _clear_interface7_modules()
    module, _file_store_module = _load_trained_doc_index_module()

    class _FakeFileStore:
        def resolve_pageindex_registry(self, **kwargs):
            return {
                "project_id": "proj_demo",
                "dataset_id": "dataset_demo",
                "doc_set_id": "docset_wrong",
                "index_ref": "idx_wrong",
                "workspace_dir": "/tmp/ws_wrong",
                "docs_dir": "/tmp/docs_wrong",
                "document_count": 1,
                "indexed_shard_count": 1,
                "documents": [
                    {
                        "doc_id": "doc-wrong",
                        "file_name": "other.md",
                        "protocol_type": "OTHER",
                        "message_codes": ["OTHER_MSG"],
                        "field_terms": ["UNRELATED"],
                        "sample_text": "unrelated content",
                        "shard_index": 1,
                    }
                ],
            }

        def list_all_pageindex_registries(self, **kwargs):
            return [
                self.resolve_pageindex_registry(),
                {
                    "project_id": "proj_demo",
                    "dataset_id": "dataset_demo",
                    "doc_set_id": "docset_hit",
                    "index_ref": "idx_hit",
                    "workspace_dir": "/tmp/ws_hit",
                    "docs_dir": "/tmp/docs_hit",
                    "document_count": 1,
                    "indexed_shard_count": 1,
                    "documents": [
                        {
                            "doc_id": "doc-hit",
                            "file_name": "k1_6.md",
                            "protocol_type": "K1_6",
                            "message_codes": ["MISSION_ASSIGNMENT_DISCRETE"],
                            "field_terms": ["纬度", "经度", "MISSION_ASSIGNMENT_DISCRETE"],
                            "sample_text": "Page 2 / Block 2 MISSION_ASSIGNMENT_DISCRETE 纬度 经度",
                            "shard_index": 1,
                        }
                    ],
                },
            ]

    provider = module.TrainedDocEvidenceProvider(
        project_id="proj_demo",
        dataset_id="dataset_demo",
        file_store=_FakeFileStore(),
        client_factory=lambda workspace: object(),
    )

    result = provider.collect_evidence(
        source_protocol={
            "protocol_type": "K1_6",
            "message_code": "MISSION_ASSIGNMENT_DISCRETE",
            "content": "",
        },
        target_protocol={
            "protocol_type": "X0_5",
            "field_queries": ["经度"],
        },
        source_message={"纬度": 0},
    )

    assert result["status"] == "used"
    assert result["doc_set_id"] == "docset_hit"
    assert result["candidate_doc_count"] == 1
    assert provider.registry["doc_set_id"] == "docset_hit"
    assert provider.registry["index_ref"] == "idx_hit"

    _clear_interface7_modules()

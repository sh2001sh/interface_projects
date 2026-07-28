from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import protocol_conversion.trained_doc_index as trained_doc_index


def test_trained_doc_provider_normalizes_stale_registry_storage_paths(monkeypatch, tmp_path):
    project_id = "compare_proj"
    doc_set_id = "docset_demo"
    doc_id = "abc123"
    normalized_name = "demo_part_001.md"

    live_workspace_root = tmp_path / "live_workspace"
    live_docs_root = tmp_path / "live_docs"
    stale_workspace_root = tmp_path / "stale_workspace"
    stale_docs_root = tmp_path / "stale_docs"

    live_workspace = live_workspace_root / project_id / doc_set_id
    live_docs = live_docs_root / project_id / doc_set_id
    stale_workspace = stale_workspace_root / project_id / doc_set_id
    stale_docs = stale_docs_root / project_id / doc_set_id

    live_workspace.mkdir(parents=True, exist_ok=True)
    live_docs.mkdir(parents=True, exist_ok=True)
    stale_workspace.mkdir(parents=True, exist_ok=True)
    stale_docs.mkdir(parents=True, exist_ok=True)

    (live_workspace / f"{doc_id}.json").write_text("{}", encoding="utf-8")
    (live_docs / normalized_name).write_text("demo", encoding="utf-8")

    registry = {
        "project_id": project_id,
        "doc_set_id": doc_set_id,
        "workspace_dir": str(stale_workspace),
        "docs_dir": str(stale_docs),
        "documents": [
            {
                "doc_id": doc_id,
                "normalized_path": str(stale_docs / normalized_name),
            }
        ],
    }

    file_store = SimpleNamespace(
        resolve_pageindex_registry=lambda **_: dict(registry),
    )

    monkeypatch.setattr(trained_doc_index, "PAGEINDEX_WORKSPACE_ROOT", live_workspace_root)
    monkeypatch.setattr(trained_doc_index, "PAGEINDEX_DOC_ROOT", live_docs_root)

    provider = trained_doc_index.TrainedDocEvidenceProvider(
        project_id=project_id,
        doc_set_id=doc_set_id,
        file_store=file_store,
    )

    assert provider.registry["workspace_dir"] == str(live_workspace)
    assert provider.registry["docs_dir"] == str(live_docs)
    assert provider.registry["documents"][0]["normalized_path"] == str(live_docs / normalized_name)

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
API_APP_PATH = PROJECT_ROOT / "app.py"


def _load_api_module():
    project_root_text = str(PROJECT_ROOT)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)
    spec = importlib.util.spec_from_file_location("interface_project_04_api_app", API_APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {API_APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_doc_index_can_use_document_path_without_project_id(tmp_path):
    module = _load_api_module()
    document_path = tmp_path / "protocol_A.txt"
    document_path.write_text("sample block", encoding="utf-8")

    block = module.Block(
        block_id=101,
        project_id="proj_auto",
        file_name=document_path.name,
        page_num=1,
        content="sample block",
        cleaned_content="sample block",
    )

    captured = {}

    module.file_store.resolve_pageindex_registry = lambda **_: {}
    module.mysql_client.get_blocks_by_file_names = lambda file_names, project_id="": [block]

    def _fake_build_protocol_doc_index(**kwargs):
        captured["kwargs"] = kwargs
        return {
            "doc_set_id": "docset_auto",
            "index_ref": "idx_auto",
            "status": "ready",
            "document_count": 1,
        }

    module.build_protocol_doc_index = _fake_build_protocol_doc_index

    client = module.app.test_client()
    response = client.post(
        "/api/data/update_doc_index",
        json={
            "document_path": str(document_path),
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["project_id"].startswith("rag_")
    assert payload["data"]["document_paths"] == [str(document_path)]
    assert payload["data"]["file_names"] == [document_path.name]
    assert payload["data"]["source_block_count"] == 1
    assert Path(payload["data"]["storage_path"]).is_absolute()

    build_kwargs = captured["kwargs"]
    assert build_kwargs["project_id"] == payload["data"]["project_id"]
    assert build_kwargs["file_names"] == [document_path.name]
    assert build_kwargs["document_paths"] == [str(document_path)]
    assert build_kwargs["document_fingerprints"] == {document_path.name: module._compute_document_fingerprint(str(document_path))}
    assert build_kwargs["source_block_ids"] == [1]


def test_update_doc_index_auto_async_for_large_pdf_page_count(tmp_path):
    module = _load_api_module()
    document_path = tmp_path / "large_protocol.pdf"
    document_path.write_text("placeholder", encoding="utf-8")

    captured = {}

    module._file_size_from_path = lambda _path: 19_083_875
    module._inspect_document_async_profile = lambda _path: {
        "path": str(document_path),
        "file_name": document_path.name,
        "suffix": ".pdf",
        "file_size": 19_083_875,
        "page_count": 640,
        "sampled_page_count": 8,
        "estimated_total_chars": 480000,
        "estimated_shards": 12,
    }

    def _fake_submit_update_doc_index_job(payload, async_decision=None):
        captured["payload"] = payload
        captured["async_decision"] = async_decision
        return {
            "job_id": "job_auto_async",
            "status": "queued",
            "stage": "queued",
            "message": "任务已提交",
            "base_path": "/api/data/update_doc_index",
            "metadata": {
                "async_reasons": list((async_decision or {}).get("reasons") or []),
            },
        }

    module.submit_update_doc_index_job = _fake_submit_update_doc_index_job

    client = module.app.test_client()
    response = client.post(
        "/api/data/update_doc_index",
        json={
            "document_path": str(document_path),
        },
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["code"] == 202
    assert payload["data"]["job_id"] == "job_auto_async"
    assert "pdf_page_count:large_protocol.pdf:640" in payload["data"]["metadata"]["async_reasons"]
    assert "pdf_estimated_shards:large_protocol.pdf:12" in payload["data"]["metadata"]["async_reasons"]
    assert captured["payload"]["document_path"] == str(document_path)
    assert captured["async_decision"]["should_async"] is True

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "test" / "output"
REPORT_PATH = OUTPUT_DIR / "full_functional_real_report.json"

REAL_PDF = ROOT / "test" / "data" / "real_protocol_bundle" / "pdf" / "MIL-STD-6016D_J12_excerpt.pdf"
REAL_SOURCE_DIR = ROOT / "test" / "data" / "real_protocol_bundle" / "source_protocols"
REAL_TARGET_DIR = ROOT / "test" / "data" / "real_protocol_bundle" / "target_protocols"
SAMPLE_MATRIX = ROOT / "test" / "data" / "unified_test_bundle" / "config" / "conversion_matrix.json"
SAMPLE_PORT_CONFIG = ROOT / "test" / "data" / "unified_test_bundle" / "config" / "port_config.json"
SAMPLE_RULES = ROOT / "test" / "data" / "codegen" / "rules" / "07_protocol_generate_rules.json"
TRAIN_JSONL = ROOT / "test" / "output" / "train_real_e2e_from_db.jsonl"
BASE_MODEL = Path(os.environ.get("LLM_MODEL_DIR", str(ROOT / "model_cache" / "modelscope" / "Qwen" / "Qwen3-4B")))

EXISTING_DATASET_ID = "900000172741"
EXISTING_QA_IDS = ["515", "516", "517"]

INTERFACES = {
    "01": ROOT / "01_validate_protocol_files" / "app.py",
    "02": ROOT / "02_upload_split" / "app.py",
    "03": ROOT / "03_clean" / "app.py",
    "04": ROOT / "04_semantic_chunk" / "app.py",
    "05": ROOT / "05_generate_qa" / "app.py",
    "06": ROOT / "06_extract_validate_qa" / "app.py",
    "07": ROOT / "07_protocol_generate_rules" / "app.py",
    "08": ROOT / "08_code_generation" / "app.py",
    "09": ROOT / "09_finetune_runtime" / "app.py",
    "10": ROOT / "10_rule_evaluate" / "app.py",
}


def _purge_modules() -> None:
    prefixes = (
        "shared",
        "runtime_config",
        "streaming_utils",
        "api_",
        "interface_project_",
    )
    for name in list(sys.modules):
        if name.startswith(prefixes) or name == "app":
            sys.modules.pop(name, None)


def load_client(interface_id: str):
    _purge_modules()
    app_path = INTERFACES[interface_id]
    project_dir = str(app_path.parent)
    root_dir = str(ROOT)
    for path in (project_dir, root_dir):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(f"ifp_{interface_id}_app", app_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.app.test_client()


def trim(value: Any, limit: int = 1200) -> Any:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    if len(text) <= limit:
        return value
    return text[:limit] + "...<truncated>"


def response_json(response) -> Dict[str, Any]:
    try:
        return response.get_json(silent=True) or {}
    except Exception:
        return {}


def read_sse(response, max_events: int = 8) -> Dict[str, Any]:
    chunks = []
    for raw in response.response:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        chunks.append(raw)
        joined = "".join(chunks)
        if joined.count("\n\n") >= max_events:
            break
    text = "".join(chunks)
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        item: Dict[str, Any] = {"raw": block}
        for line in block.splitlines():
            if line.startswith("event:"):
                item["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                try:
                    item["data"] = json.loads(payload)
                except Exception:
                    item["data"] = payload
        events.append(item)
    return {
        "content_type": response.content_type,
        "events": events,
        "raw_preview": text[:2000],
    }


def wait_until(fn, timeout: int = 300, interval: float = 2.0) -> Tuple[Any, float]:
    started = time.time()
    while True:
        result = fn()
        if result:
            return result, time.time() - started
        if time.time() - started > timeout:
            raise TimeoutError(f"condition timeout after {timeout}s")
        time.sleep(interval)


def ensure_ok(name: str, response, expected: int = 200) -> Dict[str, Any]:
    payload = response_json(response)
    if response.status_code != expected:
        raise RuntimeError(f"{name} failed: status={response.status_code}, payload={trim(payload)}")
    return payload


def save_report(report: Dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def build_manual_writeback_payload() -> Dict[str, Any]:
    sample = json.loads(SAMPLE_RULES.read_text(encoding="utf-8"))
    conversion = sample["conversions"][0]
    return {
        "protocol_type": conversion["sources"][0]["protocol"],
        "target_protocol_type": conversion["target"]["protocol"],
        "source_message_code": "TEMP_SENSOR",
        "target_message_code": "TEMP_REPORT",
        "rules": conversion["rules"][:2],
    }


def main() -> int:
    report: Dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "real_pdf": str(REAL_PDF),
        "real_source_dir": str(REAL_SOURCE_DIR),
        "real_target_dir": str(REAL_TARGET_DIR),
        "existing_dataset_id": EXISTING_DATASET_ID,
        "tests": {},
        "errors": [],
    }

    run_id = time.strftime("%Y%m%d_%H%M%S")
    project_id = f"proj_full_func_{run_id}"
    codegen_dir = OUTPUT_DIR / f"codegen_full_func_{run_id}"
    rules_dir = OUTPUT_DIR / f"rules_full_func_{run_id}"
    codegen_dir.mkdir(parents=True, exist_ok=True)
    rules_dir.mkdir(parents=True, exist_ok=True)

    clients = {iid: load_client(iid) for iid in INTERFACES}

    try:
        health = {}
        for iid, client in clients.items():
            resp = client.get("/health")
            health[iid] = {
                "status": resp.status_code,
                "body": response_json(resp),
            }
        report["tests"]["health"] = health
        save_report(report)

        # 01 validate
        client01 = clients["01"]
        validate_payload = {
            "project_id": project_id,
            "file_paths": [
                str(REAL_PDF),
                str(REAL_SOURCE_DIR / "k1.6.xml"),
                str(REAL_TARGET_DIR / "X0.5.xml"),
            ],
            "max_size_mb": 20,
            "page_batch_size": 50,
        }
        resp = client01.post("/api/data/validate_protocol_files", json=validate_payload)
        body01 = ensure_ok("01_validate_sync", resp)
        resp = client01.post("/api/data/validate_protocol_files", json={**validate_payload, "async": True})
        body01_async = ensure_ok("01_validate_async_submit", resp, expected=202)
        job01 = body01_async["data"]["job_id"]
        status01, waited01 = wait_until(
            lambda: (
                p if (p := response_json(client01.get(f"/api/data/validate_protocol_files/status?job_id={job01}"))).get("data", {}).get("status") in {"completed", "failed"}
                else None
            ),
            timeout=240,
        )
        stream01 = read_sse(client01.get(f"/api/data/validate_protocol_files/stream?job_id={job01}", buffered=False), max_events=10)
        direct_sse01 = read_sse(
            client01.post("/api/data/validate_protocol_files", json={**validate_payload, "stream": True}, buffered=False),
            max_events=8,
        )
        report["tests"]["01_validate_protocol_files"] = {
            "sync": body01,
            "async_submit": body01_async,
            "async_final_status": status01,
            "async_wait_seconds": waited01,
            "job_stream": stream01,
            "direct_sse": direct_sse01,
        }
        save_report(report)

        # 02 upload split
        client02 = clients["02"]
        upload_payload = {
            "project_id": project_id,
            "file_path": str(REAL_PDF),
            "return_mode": "both",
            "page_batch_size": 50,
        }
        resp = client02.post("/api/data/upload_split", json=upload_payload)
        body02 = ensure_ok("02_upload_split_sync", resp)
        blocks_file_path = body02["data"]["blocks_file_path"]
        resp = client02.post("/api/data/upload_split", json={**upload_payload, "async": True})
        body02_async = ensure_ok("02_upload_split_async_submit", resp, expected=202)
        job02 = body02_async["data"]["job_id"]
        status02, waited02 = wait_until(
            lambda: (
                p if (p := response_json(client02.get(f"/api/data/upload_split/status?job_id={job02}"))).get("data", {}).get("status") in {"completed", "failed"}
                else None
            ),
            timeout=360,
        )
        stream02 = read_sse(client02.get(f"/api/data/upload_split/stream?job_id={job02}", buffered=False), max_events=12)
        direct_sse02 = read_sse(
            client02.post("/api/data/upload_split", json={**upload_payload, "stream": True}, buffered=False),
            max_events=8,
        )
        report["tests"]["02_upload_split"] = {
            "sync": body02,
            "async_submit": body02_async,
            "async_final_status": status02,
            "async_wait_seconds": waited02,
            "job_stream": stream02,
            "direct_sse": direct_sse02,
        }
        save_report(report)

        # 03 clean
        client03 = clients["03"]
        clean_payload = {
            "blocks_file_path": blocks_file_path,
            "return_mode": "both",
        }
        resp = client03.post("/api/data/clean", json=clean_payload)
        body03 = ensure_ok("03_clean_sync", resp)
        cleaned_blocks_file_path = body03["data"]["cleaned_blocks_file_path"]
        resp = client03.post("/api/data/clean", json={**clean_payload, "stream": True}, buffered=False)
        stream03 = read_sse(resp, max_events=8)
        resp = client03.post("/api/data/clean", json={"dataset_id": EXISTING_DATASET_ID, "return_mode": "content"})
        body03_dataset = ensure_ok("03_clean_dataset_mode", resp)
        report["tests"]["03_clean"] = {
            "sync": body03,
            "direct_sse": stream03,
            "dataset_mode": body03_dataset,
        }
        save_report(report)

        # 04 semantic chunk + update_doc_index
        client04 = clients["04"]
        chunk_payload = {
            "cleaned_blocks_file_path": cleaned_blocks_file_path,
            "project_id": project_id,
            "return_mode": "both",
            "config": {
                "build_doc_index": True,
                "max_token_size": 768,
            },
        }
        resp = client04.post("/api/data/semantic_chunk", json=chunk_payload)
        body04 = ensure_ok("04_semantic_chunk_sync", resp)
        chunks_file_path = body04["data"]["chunks_file_path"]
        resp = client04.post("/api/data/semantic_chunk", json={**chunk_payload, "stream": True}, buffered=False)
        stream04 = read_sse(resp, max_events=8)
        update_payload = {
            "project_id": project_id,
            "blocks_file_path": cleaned_blocks_file_path,
            "document_paths": [str(REAL_PDF)],
            "rebuild": True,
        }
        resp = client04.post("/api/data/update_doc_index", json=update_payload)
        body04_index = ensure_ok("04_update_doc_index", resp)
        doc_set_id = body04_index["data"]["doc_set_id"]
        index_ref = body04_index["data"]["index_ref"]
        registry_path = ROOT / body04_index["data"]["storage_path"] / "registry.json"
        report["tests"]["04_semantic_chunk"] = {
            "sync": body04,
            "direct_sse": stream04,
            "update_doc_index": body04_index,
            "registry_path": str(registry_path),
        }
        save_report(report)

        # 05 generate qa
        client05 = clients["05"]
        qa_payload = {
            "dataset_id": EXISTING_DATASET_ID,
            "count": 2,
            "selection_config": {"auto_select": True},
            "task_config": {"task_types": ["protocol_understanding", "protocol_conversion"]},
        }
        resp = client05.post("/api/knowledge/generate_qa", json=qa_payload)
        body05 = ensure_ok("05_generate_qa_sync", resp)
        resp = client05.post("/api/knowledge/generate_qa", json={**qa_payload, "count": 1, "stream": True}, buffered=False)
        stream05 = read_sse(resp, max_events=8)
        report["tests"]["05_generate_qa"] = {
            "sync": body05,
            "direct_sse": stream05,
        }
        save_report(report)

        # 06 extract validate
        client06 = clients["06"]
        ev_payload = {"dataset_id": EXISTING_DATASET_ID, "qa_id": EXISTING_QA_IDS[0], "protocol_type": "Link16"}
        resp = client06.post("/api/knowledge/extract_validate_qa", json=ev_payload)
        body06 = ensure_ok("06_extract_validate_single", resp)
        batch_payload = {
            "batch": True,
            "items": [{"dataset_id": EXISTING_DATASET_ID, "qa_id": qa_id, "protocol_type": "Link16"} for qa_id in EXISTING_QA_IDS],
        }
        resp = client06.post("/api/knowledge/extract_validate_qa", json=batch_payload)
        body06_batch = ensure_ok("06_extract_validate_batch", resp)
        resp = client06.post("/api/knowledge/extract_validate_qa", json={**ev_payload, "stream": True}, buffered=False)
        stream06 = read_sse(resp, max_events=8)
        report["tests"]["06_extract_validate_qa"] = {
            "single": body06,
            "batch": body06_batch,
            "direct_sse": stream06,
        }
        save_report(report)

        # 07 rule generation + manual writeback
        client07 = clients["07"]
        rules_file = rules_dir / "07_protocol_generate_rules.json"
        rule_payload = {
            "project_id": project_id,
            "dataset_id": EXISTING_DATASET_ID,
            "source_protocol_dirs": [str(REAL_SOURCE_DIR)],
            "target_protocol_dir": str(REAL_TARGET_DIR),
            "use_page_index": True,
            "use_trained_docs": True,
            "doc_set_id": doc_set_id,
            "index_ref": index_ref,
            "save_rules_file": True,
            "rules_output_dir": str(rules_dir),
            "rules_file_name": rules_file.name,
            "project_name": f"real_full_func_{run_id}",
        }
        resp = client07.post("/api/knowledge/protocol_generate_rules", json=rule_payload)
        body07 = ensure_ok("07_protocol_generate_rules_sync", resp)
        actual_rules_path = Path(body07["data"]["conversion_rules_json"])
        resp = client07.post("/api/knowledge/protocol_generate_rules", json={**rule_payload, "stream": True}, buffered=False)
        stream07 = read_sse(resp, max_events=8)
        resp = client07.post("/api/knowledge/protocol_rules/manual_writeback", json=build_manual_writeback_payload())
        body07_writeback = ensure_ok("07_protocol_rules_manual_writeback", resp)
        report["tests"]["07_protocol_generate_rules"] = {
            "sync": body07,
            "direct_sse": stream07,
            "manual_writeback": body07_writeback,
            "requested_rules_file_path": str(rules_file),
            "actual_rules_file_path": str(actual_rules_path),
            "requested_rules_file_exists": rules_file.exists(),
            "actual_rules_file_exists": actual_rules_path.exists(),
        }
        save_report(report)

        # 08 code generation
        client08 = clients["08"]
        codegen_payload = {
            "source_protocol_dirs": [str(REAL_SOURCE_DIR)],
            "target_protocol_dir": str(REAL_TARGET_DIR),
            "conversion_rules_json": str(actual_rules_path),
            "conversion_matrix_json": str(SAMPLE_MATRIX),
            "port_config_json": str(SAMPLE_PORT_CONFIG),
            "output_dir": str(codegen_dir),
            "project_name": f"generated_full_func_{run_id}",
        }
        resp = client08.post("/api/code_generation/generate", json=codegen_payload)
        body08 = ensure_ok("08_code_generation_sync", resp)
        resp = client08.post("/api/code_generation/generate", json={**codegen_payload, "stream": True}, buffered=False)
        stream08 = read_sse(resp, max_events=8)
        report["tests"]["08_code_generation"] = {
            "sync": body08,
            "direct_sse": stream08,
            "manifest_exists": (codegen_dir / "protocol_manifest.json").exists(),
        }
        save_report(report)

        # 09 finetune
        client09 = clients["09"]
        finetune_job_id = f"job_full_func_{run_id}"
        finetune_payload = {
            "action": "start",
            "job_id": finetune_job_id,
            "config": {
                "base_model_path": str(BASE_MODEL),
                "train_mode": "sft",
                "train_file_path": str(TRAIN_JSONL),
                "parameters": {
                    "epochs": 5,
                    "learning_rate": 0.0002,
                    "batch_size": 1,
                    "lora_rank": 8,
                    "save_steps": 2,
                },
            },
        }
        resp = client09.post("/api/model/finetune/action", json=finetune_payload)
        body09_start = ensure_ok("09_finetune_start", resp)
        stream09 = read_sse(client09.get(f"/api/model/finetune/stream?job_id={finetune_job_id}", buffered=False), max_events=10)
        status_route = f"/api/finetune/job/status?job_id={finetune_job_id}"

        def _fetch_status():
            payload = response_json(client09.get(status_route))
            return payload if payload.get("data") else None

        status09_started, _ = wait_until(_fetch_status, timeout=120, interval=2)

        def _can_pause():
            payload = response_json(client09.get(status_route))
            data = payload.get("data") or {}
            progress = data.get("progress") or {}
            if data.get("last_checkpoint") or int(progress.get("current_step") or 0) >= 2:
                return payload
            if data.get("status") in {"failed", "completed", "stopped"}:
                return payload
            return None

        pause_ready, _ = wait_until(_can_pause, timeout=900, interval=5)
        resp = client09.post("/api/model/finetune/action", json={"action": "pause", "job_id": finetune_job_id})
        body09_pause = ensure_ok("09_finetune_pause", resp)
        paused_status, _ = wait_until(
            lambda: (
                p if (p := response_json(client09.get(status_route))).get("data", {}).get("status") == "paused"
                else None
            ),
            timeout=120,
            interval=2,
        )
        resp = client09.post("/api/model/finetune/action", json={"action": "start", "job_id": finetune_job_id, "config": finetune_payload["config"]})
        body09_resume = ensure_ok("09_finetune_resume", resp)
        final09, waited09 = wait_until(
            lambda: (
                p if (p := response_json(client09.get(status_route))).get("data", {}).get("status") in {"completed", "failed", "stopped"}
                else None
            ),
            timeout=3600,
            interval=10,
        )
        resp = client09.get(f"/api/model/finetune/model/download?job_id={finetune_job_id}")
        report["tests"]["09_finetune_runtime"] = {
            "start": body09_start,
            "status_after_start": status09_started,
            "stream_preview": stream09,
            "pause_ready_status": pause_ready,
            "pause": body09_pause,
            "paused_status": paused_status,
            "resume": body09_resume,
            "final_status": final09,
            "final_wait_seconds": waited09,
            "download_status": resp.status_code,
            "download_content_type": resp.content_type,
        }
        save_report(report)

        # 10 rule evaluate
        client10 = clients["10"]
        eval_payload = {
            "source_protocol_dirs": [str(REAL_SOURCE_DIR)],
            "target_protocol_dir": str(REAL_TARGET_DIR),
            "conversion_rules": str(actual_rules_path),
            "trace_id": f"trace_full_func_{run_id}",
            "batch_size": 2,
            "max_workers": 1,
            "use_model_inference": True,
        }
        resp = client10.post("/api/knowledge/rule_evaluate", json=eval_payload)
        body10 = ensure_ok("10_rule_evaluate_sync", resp)
        resp = client10.post("/api/knowledge/rule_evaluate", json={**eval_payload, "stream": True}, buffered=False)
        stream10 = read_sse(resp, max_events=8)
        report["tests"]["10_rule_evaluate"] = {
            "sync": body10,
            "direct_sse": stream10,
        }
        save_report(report)

    except Exception as exc:
        report["errors"].append(
            {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        save_report(report)
        print(json.dumps({"report_path": str(REPORT_PATH), "error": report["errors"][-1]}, ensure_ascii=False, indent=2))
        return 1

    report["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_report(report)
    summary = {
        "report_path": str(REPORT_PATH),
        "completed_at": report["completed_at"],
        "test_keys": list(report["tests"].keys()),
        "errors": report["errors"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

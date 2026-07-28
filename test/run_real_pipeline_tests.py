from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import requests


ROOT_DIR = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT_DIR / "test"
DATA_DIR = TEST_DIR / "data" / "codegen"
OUTPUT_DIR = TEST_DIR / "output" / "real_pipeline"
REPORT_PATH = TEST_DIR / "output" / "real_pipeline_report.json"

PORT_OFFSET = int(os.getenv("INTERFACE_PORT_OFFSET", "0") or "0")


def _port(base_port: int) -> int:
    return base_port + PORT_OFFSET


PORTS = {
    "01": _port(6101),
    "02": _port(6102),
    "03": _port(6103),
    "04": _port(6104),
    "05": _port(6105),
    "06": _port(6106),
    "07": _port(6107),
    "08": _port(6108),
    "09": _port(6109),
    "10": _port(6110),
}


def _request(method: str, url: str, timeout: int = 300, **kwargs: Any) -> requests.Response:
    return requests.request(method=method, url=url, timeout=timeout, **kwargs)


def _record(results: List[Dict[str, Any]], name: str, passed: bool, details: Dict[str, Any]) -> None:
    results.append({"name": name, "passed": passed, "details": details})


def _short_payload(payload: Any, limit: int = 2400) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def _discover_single_xml(directory: Path, label: str) -> Path:
    xml_files = sorted(path for path in directory.glob("*.xml") if path.is_file())
    if not xml_files:
        raise FileNotFoundError(f"{label}目录中未找到 XML: {directory}")
    if len(xml_files) > 1:
        raise ValueError(f"{label}目录中存在多个 XML，当前真实链路测试要求仅保留一个: {directory}")
    return xml_files[0]


def _ensure_models(results: List[Dict[str, Any]]) -> None:
    model_paths = {
        "llm": ROOT_DIR / "model_cache" / "Qwen" / "Qwen3-4B",
        "embedding": ROOT_DIR / "model_cache" / "Qwen" / "Qwen3-Embedding-0.6B",
        "reranker": ROOT_DIR / "model_cache" / "Qwen" / "Qwen3-Reranker-0.6B",
    }
    passed = all(path.exists() for path in model_paths.values())
    _record(
        results,
        "models:downloaded",
        passed,
        {name: str(path) for name, path in model_paths.items()},
    )


def _check_vllm(host: str, results: List[Dict[str, Any]]) -> None:
    url = f"http://{host}:8000/v1/models"
    response = _request("GET", url, timeout=120)
    payload = response.json()
    model_ids = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
    passed = response.status_code == 200 and "Qwen3-4B" in model_ids
    _record(
        results,
        "vllm:models",
        passed,
        {"url": url, "status_code": response.status_code, "model_ids": model_ids},
    )


def run_real_tests(host: str, data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    session = requests.Session()

    source_dir = data_dir / "source_protocols"
    target_dir = data_dir / "target_protocols"
    source_xml = _discover_single_xml(source_dir, "source_protocols")
    conversion_matrix = data_dir / "conversion_matrix.json"
    port_config = data_dir / "port_config.json"

    project_id = f"real_proj_{int(time.time())}"
    dataset_id = f"real_ds_{int(time.time())}"
    codegen_output_dir = OUTPUT_DIR / project_id
    if codegen_output_dir.exists():
        shutil.rmtree(codegen_output_dir)
    codegen_output_dir.mkdir(parents=True, exist_ok=True)

    _ensure_models(results)
    _check_vllm(host, results)

    with source_xml.open("rb") as handle:
        response = session.post(
            f"http://{host}:{PORTS['01']}/api/data/validate_protocol_files",
            files={"file": (source_xml.name, handle, "application/xml")},
            data={"max_size_mb": "10"},
            timeout=180,
        )
    payload = response.json()
    summary = payload.get("data", {}).get("summary", {})
    _record(
        results,
        "01:validate_protocol_files",
        response.status_code == 200 and int(summary.get("passed_files", 0)) >= 1,
        {"status_code": response.status_code, "payload": _short_payload(payload)},
    )

    with source_xml.open("rb") as handle:
        response = session.post(
            f"http://{host}:{PORTS['02']}/api/data/upload_split",
            files={"file": (source_xml.name, handle, "application/xml")},
            data={"project_id": project_id, "enable_llm_postprocess": "false"},
            timeout=180,
        )
    payload = response.json()
    blocks = payload.get("data", {}).get("blocks", [])
    block_ids = [item.get("block_id") for item in blocks if item.get("block_id")]
    _record(
        results,
        "02:upload_split",
        response.status_code == 200 and len(block_ids) >= 1,
        {"status_code": response.status_code, "block_ids": block_ids, "payload": _short_payload(payload)},
    )

    response = session.post(
        f"http://{host}:{PORTS['03']}/api/data/clean",
        json={"dataset_id": dataset_id, "block_ids": block_ids},
        timeout=180,
    )
    payload = response.json()
    modified_count = int(payload.get("data", {}).get("modified_count", 0))
    _record(
        results,
        "03:clean",
        response.status_code == 200 and payload.get("data", {}).get("total_count") == len(block_ids),
        {"status_code": response.status_code, "modified_count": modified_count, "payload": _short_payload(payload)},
    )

    response = session.post(
        f"http://{host}:{PORTS['04']}/api/data/semantic_chunk",
        json={
            "project_id": project_id,
            "dataset_id": dataset_id,
            "source_block_ids": block_ids,
            "config": {
                "max_token_size": 256,
                "use_llm_boundary_fallback": False,
                "build_doc_index": True,
            },
        },
        timeout=300,
    )
    payload = response.json()
    chunks = payload.get("data", {}).get("chunks", [])
    chunk_ids = [item.get("chunk_id") for item in chunks if item.get("chunk_id")]
    _record(
        results,
        "04:semantic_chunk",
        response.status_code == 200 and len(chunk_ids) >= 1,
        {
            "status_code": response.status_code,
            "chunk_ids": chunk_ids,
            "doc_index": payload.get("data", {}).get("doc_index"),
            "payload": _short_payload(payload),
        },
    )

    response = session.post(
        f"http://{host}:{PORTS['05']}/api/knowledge/generate_qa",
        json={
            "source_block_ids": chunk_ids,
            "dataset_id": dataset_id,
            "task_config": {"task_types": ["protocol_understanding", "protocol_conversion"]},
            "prompt_config": {"user_instruction": "请基于协议真实字段生成问答，优先覆盖字段含义、默认值与转换关系。"},
            "count": 2,
        },
        timeout=300,
    )
    payload = response.json()
    qa_pairs = payload.get("data", {}).get("qa_pairs", [])
    _record(
        results,
        "05:generate_qa",
        response.status_code == 200 and len(qa_pairs) >= 1,
        {
            "status_code": response.status_code,
            "qa_count": len(qa_pairs),
            "payload": _short_payload(payload),
        },
    )

    first_qa = qa_pairs[0] if qa_pairs else {}
    response = session.post(
        f"http://{host}:{PORTS['06']}/api/knowledge/extract_validate_qa",
        json={
            "qa_id": first_qa.get("qa_id"),
            "question": first_qa.get("question"),
            "answer": first_qa.get("answer"),
            "protocol_type": first_qa.get("protocol_type") or "Link16",
        },
        timeout=300,
    )
    payload = response.json()
    _record(
        results,
        "06:extract_validate_qa",
        response.status_code == 200 and payload.get("data", {}).get("qa_id") == first_qa.get("qa_id"),
        {"status_code": response.status_code, "payload": _short_payload(payload)},
    )

    response = session.post(
        f"http://{host}:{PORTS['07']}/api/knowledge/protocol_generate_rules",
        json={
            "source_protocol_dirs": [str(source_dir)],
            "target_protocol_dir": str(target_dir),
            "project_id": project_id,
            "dataset_id": dataset_id,
            "use_trained_docs": False,
        },
        timeout=300,
    )
    payload = response.json()
    rules_path = payload.get("data", {}).get("conversion_rules_json")
    validation_result = payload.get("data", {}).get("validation_result") or {}
    _record(
        results,
        "07:protocol_generate_rules",
        response.status_code == 200 and bool(rules_path) and Path(str(rules_path)).exists(),
        {
            "status_code": response.status_code,
            "rules_path": rules_path,
            "validation_result": validation_result,
            "payload": _short_payload(payload),
        },
    )

    response = session.post(
        f"http://{host}:{PORTS['08']}/api/code_generation/generate",
        json={
            "source_protocol_dirs": [str(source_dir)],
            "target_protocol_dir": str(target_dir),
            "conversion_rules_json": rules_path,
            "conversion_matrix_json": str(conversion_matrix),
            "port_config_json": str(port_config),
            "output_dir": str(codegen_output_dir),
            "project_name": project_id,
        },
        timeout=300,
    )
    payload = response.json()
    manifest = codegen_output_dir / "protocol_manifest.json"
    conversions = payload.get("data", {}).get("manifest", {}).get("conversions", [])
    _record(
        results,
        "08:code_generation",
        response.status_code == 200 and manifest.exists() and len(conversions) >= 1,
        {
            "status_code": response.status_code,
            "manifest_path": str(manifest),
            "conversion_count": len(conversions),
            "payload": _short_payload(payload),
        },
    )

    response = session.post(
        f"http://{host}:{PORTS['09']}/api/model/finetune/action",
        json={
            "action": "start",
            "config": {
                "base_model": str(ROOT_DIR / "model_cache" / "Qwen" / "Qwen3-4B"),
                "dataset_id": dataset_id,
                "simulate": True,
                "parameters": {"epochs": 1, "save_steps": 20},
            },
        },
        timeout=180,
    )
    payload = response.json()
    job_id = payload.get("data", {}).get("job_id")
    time.sleep(2)
    status_response = session.get(
        f"http://{host}:{PORTS['09']}/api/finetune/job/status",
        params={"job_id": job_id},
        timeout=180,
    )
    status_payload = status_response.json()
    job_status = status_payload.get("data", {}).get("status")
    _record(
        results,
        "09:finetune_runtime",
        response.status_code == 200 and status_response.status_code == 200 and job_status in {"running", "completed"},
        {
            "start_status_code": response.status_code,
            "status_status_code": status_response.status_code,
            "job_id": job_id,
            "job_status": job_status,
            "payload": _short_payload(status_payload),
        },
    )

    response = session.post(
        f"http://{host}:{PORTS['10']}/api/knowledge/rule_evaluate",
        json={
            "source_protocol_dirs": [str(source_dir)],
            "target_protocol_dir": str(target_dir),
            "conversion_rules": rules_path,
            "coarse_top_k": 5,
            "coarse_similarity_threshold": 0.1,
            "fine_similarity_threshold": 0.1,
            "use_model_inference": True,
        },
        timeout=300,
    )
    payload = response.json()
    strategy = payload.get("data", {}).get("strategy", {})
    summary = payload.get("data", {}).get("summary", {})
    _record(
        results,
        "10:rule_evaluate",
        response.status_code == 200
        and strategy.get("embedding_backend", {}).get("available") is True
        and strategy.get("reranker_backend", {}).get("available") is True
        and int(summary.get("rule_count", 0)) >= 1,
        {
            "status_code": response.status_code,
            "summary": summary,
            "strategy": strategy,
            "scores": payload.get("data", {}).get("scores"),
            "payload": _short_payload(payload),
        },
    )

    e2e_names = [
        "01:validate_protocol_files",
        "02:upload_split",
        "03:clean",
        "04:semantic_chunk",
        "05:generate_qa",
        "06:extract_validate_qa",
        "07:protocol_generate_rules",
        "08:code_generation",
        "10:rule_evaluate",
    ]
    e2e_passed = all(item["passed"] for item in results if item["name"] in e2e_names)
    _record(
        results,
        "e2e:real_pipeline",
        e2e_passed,
        {
            "project_id": project_id,
            "dataset_id": dataset_id,
            "rules_path": rules_path,
            "codegen_output_dir": str(codegen_output_dir),
            "chunk_ids": chunk_ids,
            "block_ids": block_ids,
        },
    )

    report = {
        "host": host,
        "project_id": project_id,
        "dataset_id": dataset_id,
        "passed": all(item["passed"] for item in results),
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="真实数据逐接口功能测试 + 端到端联调测试")
    parser.add_argument("--host", default="127.0.0.1", help="服务主机，默认 127.0.0.1")
    parser.add_argument(
        "--data-dir",
        default=str(DATA_DIR),
        help="测试数据目录，需包含 source_protocols/、target_protocols/、conversion_matrix.json、port_config.json",
    )
    args = parser.parse_args()

    report = run_real_tests(args.host, Path(args.data_dir).resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

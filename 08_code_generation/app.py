"""Interface 08 standalone runtime entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, request

PROJECT_ROOT = Path(__file__).resolve().parent
PARENT_ROOT = PROJECT_ROOT.parent
for candidate_path in (str(PROJECT_ROOT), str(PARENT_ROOT)):
    if candidate_path in sys.path:
        sys.path.remove(candidate_path)
    sys.path.insert(0, candidate_path)

from code_generation_adapter import build_code_generation_payload
from runtime_config import apply_runtime_environment, get_service_runner_config
from shared.udp_json_gateway import normalize_udp_gateway_config, run_flask_service
from streaming_utils import is_stream_requested, stream_flask_handler


apply_runtime_environment()

app = Flask(__name__)
app.json.ensure_ascii = False
app.config["JSON_AS_ASCII"] = False

_UDP_ALLOWED_ROUTES = ["/api/code_generation/generate"]


def _normalize_code_generation_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizes legacy aliases and validates required request fields."""
    source_protocol_dirs = data.get("source_protocol_dirs")
    if source_protocol_dirs is None:
        source_protocol_dirs = data.get("source_protocol_dir")

    normalized = {
        "source_protocol_dirs": source_protocol_dirs,
        "target_protocol_dir": data.get("target_protocol_dir"),
        "conversion_rules_json": data.get("conversion_rules_json"),
        "conversion_matrix_json": data.get("conversion_matrix_json"),
        "port_config_json": data.get("port_config_json"),
        "output_dir": data.get("output_dir"),
        "target_protocol_name": str(data.get("target_protocol_name") or "").strip() or None,
        "project_name": str(data.get("project_name") or "").strip() or None,
    }

    required_fields = {
        "source_protocol_dirs": "source_protocol_dirs不能为空",
        "target_protocol_dir": "target_protocol_dir不能为空",
        "conversion_rules_json": "conversion_rules_json不能为空",
        "port_config_json": "port_config_json不能为空",
        "output_dir": "output_dir不能为空",
    }
    for field_name, message in required_fields.items():
        if normalized.get(field_name) in (None, "", []):
            raise ValueError(message)

    return normalized


def _build_code_generation_response() -> Any:
    """Executes interface 08 code generation for the current request."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({
                "code": 400,
                "message": "请求体必须是JSON对象",
                "data": None,
            }), 400

        normalized = _normalize_code_generation_payload(data)
        response_payload = build_code_generation_payload(
            source_protocol_dir=normalized["source_protocol_dirs"],
            target_protocol_dir=normalized["target_protocol_dir"],
            conversion_rules_json=normalized["conversion_rules_json"],
            conversion_matrix_json=normalized["conversion_matrix_json"],
            port_config_json=normalized["port_config_json"],
            output_dir=normalized["output_dir"],
            target_protocol_name=normalized["target_protocol_name"],
            project_name=normalized["project_name"],
        )
        return jsonify({
            "code": 200,
            "message": "success",
            "data": response_payload,
        })
    except ValueError as exc:
        return jsonify({
            "code": 400,
            "message": str(exc),
            "data": None,
        }), 400
    except Exception as exc:
        return jsonify({
            "code": 500,
            "message": f"代码生成失败: {str(exc)}",
            "data": None,
        }), 500


@app.route("/api/code_generation/generate", methods=["POST"])
def code_generation_generate() -> Any:
    """Generates a Qt/C++ protocol-conversion project."""
    if is_stream_requested():
        return stream_flask_handler(
            "code_generation_generate",
            _build_code_generation_response,
            initial_message="代码生成已开始，正在渲染协议转换工程",
        )
    return _build_code_generation_response()


@app.route("/health", methods=["GET"])
def health() -> Any:
    """Reports runtime health for interface 08."""
    return jsonify({"status": "healthy"})


def _build_udp_gateway_config() -> Dict[str, Any]:
    runner = get_service_runner_config()
    return normalize_udp_gateway_config(
        runner,
        default_route="/api/code_generation/generate",
        allowed_routes=_UDP_ALLOWED_ROUTES,
        runtime_root=PROJECT_ROOT / "runtime",
        app_name="08_code_generation",
    )


if __name__ == "__main__":
    runner = get_service_runner_config()
    run_flask_service(app, runner, _build_udp_gateway_config())

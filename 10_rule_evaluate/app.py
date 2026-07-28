from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, request


PROJECT_DIR = Path(__file__).resolve().parent
INTERFACE_ROOT = PROJECT_DIR.parent

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(INTERFACE_ROOT) not in sys.path:
    sys.path.insert(0, str(INTERFACE_ROOT))

from runtime_config import apply_runtime_environment, get_service_runner_config
from streaming_utils import is_stream_requested, stream_callable_with_progress


apply_runtime_environment()


def _purge_local_conflicts() -> None:
    for name in list(sys.modules):_purge_local_conflicts()
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from protocol_conversion import evaluate_protocol_rules


impl_evaluate_protocol_rules = evaluate_protocol_rules

app = Flask(__name__)


def _coerce_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(value)


@app.route("/api/knowledge/rule_evaluate", methods=["POST"])
def rule_evaluate():
    try:
        data = request.json
        if not isinstance(data, dict):
            return jsonify({"code": 400, "message": "请求体必须是JSON对象", "data": None}), 400

        source_protocol_dirs = data.get("source_protocol_dirs")
        if source_protocol_dirs is None:
            source_protocol_dirs = data.get("source_protocol_dir")
        target_protocol_dir = data.get("target_protocol_dir")
        conversion_rules = data.get("conversion_rules")

        if not source_protocol_dirs:
            return jsonify({"code": 400, "message": "source_protocol_dirs不能为空", "data": None}), 400
        if not target_protocol_dir:
            return jsonify({"code": 400, "message": "target_protocol_dir不能为空", "data": None}), 400
        if conversion_rules is None:
            return jsonify({"code": 400, "message": "conversion_rules不能为空", "data": None}), 400

        evaluation_kwargs = {
            "source_protocol_dirs": source_protocol_dirs,
            "target_protocol_dir": target_protocol_dir,
            "conversion_rules": conversion_rules,
            "coarse_top_k": int(data.get("coarse_top_k", 10)),
            "coarse_similarity_threshold": float(data.get("coarse_similarity_threshold", 0.55)),
            "fine_similarity_threshold": float(data.get("fine_similarity_threshold", 0.75)),
            "use_model_inference": _coerce_bool(data.get("use_model_inference"), True),
            "allow_modelscope_download": False,
            "trace_id": data.get("trace_id"),
            "batch_size": int(data.get("batch_size", 8)),
            "max_workers": int(data.get("max_workers", 1)),
            "export_payload": _coerce_bool(data.get("export_payload"), False),
            "export_name": data.get("export_name"),
        }
        if is_stream_requested():
            return stream_callable_with_progress(
                "rule_evaluate",
                lambda emit: {
                    "code": 200,
                    "message": "success",
                    "data": impl_evaluate_protocol_rules(
                        progress_callback=lambda progress: emit("progress", progress),
                        **evaluation_kwargs,
                    ),
                },
            )

        result = impl_evaluate_protocol_rules(**evaluation_kwargs)
        return jsonify({"code": 200, "message": "success", "data": result})
    except FileNotFoundError as exc:
        return jsonify({"code": 404, "message": f"评估模型文件不存在: {str(exc)}", "data": None}), 404
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc), "data": None}), 400
    except Exception as exc:
        return jsonify({"code": 500, "message": f"规则级评估失败: {str(exc)}", "data": None}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "project": "10_rule_evaluate"})


if __name__ == "__main__":
    runner = get_service_runner_config(PROJECT_DIR.name)
    app.run(
        host=runner["host"],
        port=runner["port"],
        debug=runner["debug"],
        threaded=runner["threaded"],
    )

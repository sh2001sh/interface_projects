"""接口5：模型微调启动与监控（含action语义与SSE流式日志）。"""

from __future__ import annotations

import os
import sys
import json
import time
import queue
import uuid
from typing import Any, Dict, List

from flask import Flask, request, jsonify, Response, send_file

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finetune_service import FinetuneService
from runtime_config import apply_runtime_environment, get_service_runner_config
from streaming_utils import is_stream_requested, stream_flask_handler
from shared.model_metadata import resolve_lora_finetune_metadata


apply_runtime_environment()

app = Flask(__name__)
finetune_service = FinetuneService()

# 运行时任务状态
train_tasks: Dict[str, Dict[str, Any]] = {}
task_logs: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}
task_event_buffers: Dict[str, List[Dict[str, Any]]] = {}
task_event_counters: Dict[str, int] = {}


def _new_job_id() -> str:
    return f"job_ft_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def _ensure_job_exists(job_id: str):
    if job_id not in train_tasks:
        return jsonify({"code": 404, "message": "任务不存在", "data": None}), 404
    return None


def _format_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = int(max(0.0, float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def _normalize_best_metric(raw_metric: Dict[str, Any] | None) -> Dict[str, Any]:
    metric = raw_metric or {}
    loss = metric.get("loss")
    if loss is None:
        loss = metric.get("final_loss")
    accuracy = metric.get("accuracy")
    if accuracy is None:
        accuracy = metric.get("acc")
    if accuracy is None:
        accuracy = metric.get("final_acc")
    return {"loss": loss, "accuracy": accuracy}


def _normalize_start_config(config: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(config or {})
    train_mode = str(normalized.get("train_mode") or "sft").strip().lower() or "sft"
    base_model = str(
        normalized.get("base_model_path")
        or normalized.get("base_model")
        or normalized.get("model_name")
        or ""
    ).strip()
    normalized["train_mode"] = train_mode
    if base_model:
        normalized["base_model"] = base_model
        normalized.setdefault("model_name", base_model)

    if train_mode == "orpo":
        normalized["preference_file_path"] = str(normalized.get("preference_file_path") or "").strip()
    else:
        normalized["train_file_path"] = str(normalized.get("train_file_path") or "").strip()
    return normalized


def _build_finetune_model_info(config: Dict[str, Any]) -> Dict[str, Any]:
    """Builds base-model and trainable-parameter metadata for LoRA finetuning."""
    normalized = _normalize_start_config(dict(config or {}))
    parameters = normalized.get("parameters") if isinstance(normalized.get("parameters"), dict) else {}
    base_model_name = normalized.get("base_model") or normalized.get("model_name")
    return resolve_lora_finetune_metadata(
        base_model_name=base_model_name,
        model_cache_dir=os.getenv("MODEL_CACHE_DIR"),
        lora_rank=parameters.get("lora_rank", 16),
    )


def _attach_finetune_model_info(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    normalized_payload = dict(payload)
    normalized_payload["model_info"] = _build_finetune_model_info(config)
    return normalized_payload


def _validate_start_config(config: Dict[str, Any]) -> str | None:
    if not isinstance(config, dict):
        return "config 参数必须为对象"
    normalized = _normalize_start_config(config)
    base_model = normalized.get("base_model")
    train_mode = str(normalized.get("train_mode") or "sft").strip().lower()
    if not base_model:
        return "config.base_model_path 参数必填"
    if train_mode not in {"sft", "orpo"}:
        return "config.train_mode 仅支持 sft/orpo"
    if train_mode == "orpo":
        preference_file_path = str(normalized.get("preference_file_path") or "").strip()
        if not preference_file_path:
            return "config.preference_file_path 参数必填"
        if not os.path.isfile(preference_file_path):
            return f"config.preference_file_path 文件不存在: {preference_file_path}"
    else:
        train_file_path = str(normalized.get("train_file_path") or "").strip()
        if not train_file_path:
            return "config.train_file_path 参数必填"
        if not os.path.isfile(train_file_path):
            return f"config.train_file_path 文件不存在: {train_file_path}"
    parameters = normalized.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        return "config.parameters 参数必须为对象"
    return None


def _ensure_runtime_channels(job_id: str) -> None:
    if job_id not in task_logs:
        task_logs[job_id] = queue.Queue()
    if job_id not in task_event_buffers:
        task_event_buffers[job_id] = []
    if job_id not in task_event_counters:
        task_event_counters[job_id] = 0


def _persist_task(job_id: str) -> None:
    task = train_tasks.get(job_id)
    if task is not None:
        finetune_service.persist_task_snapshot(job_id, task)


def _start_new_job(job_id: str, config: Dict[str, Any]):
    train_tasks[job_id] = {
        "job_id": job_id,
        "status": "submitted",
        "config": config,
        "progress": {"current_step": 0, "total_steps": 100, "epoch": 0, "percent": 0},
        "history_points": [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _ensure_runtime_channels(job_id)
    _persist_task(job_id)
    finetune_service.start_training(job_id, config, task_logs[job_id], train_tasks)


def _merge_resume_config(
    stored_config: Dict[str, Any] | None,
    incoming_config: Dict[str, Any] | None,
    checkpoint_path: str,
) -> Dict[str, Any]:
    merged = dict(stored_config or {})
    incoming = incoming_config or {}
    for key, value in incoming.items():
        if key == "parameters" and isinstance(value, dict):
            params = dict(merged.get("parameters", {}))
            params.update(value)
            merged["parameters"] = params
        else:
            merged[key] = value
    merged["resume_from_checkpoint"] = checkpoint_path
    return merged


def _resume_from_checkpoint(job_id: str, config: Dict[str, Any], checkpoint_path: str):
    task = train_tasks.get(job_id, {})
    task["job_id"] = job_id
    task["status"] = "resuming"
    task["config"] = config
    task["resume_from_checkpoint"] = checkpoint_path
    task["updated_at"] = time.time()
    task.setdefault("created_at", time.time())
    task.setdefault("history_points", [])
    if "progress" not in task:
        task["progress"] = {"current_step": 0, "total_steps": 100, "epoch": 0, "percent": 0}
    train_tasks[job_id] = task
    _ensure_runtime_channels(job_id)
    _persist_task(job_id)
    finetune_service.start_training(job_id, config, task_logs[job_id], train_tasks)


def _restore_tasks_from_disk() -> None:
    restored = finetune_service.load_persisted_tasks()
    if not restored:
        return

    for job_id, task in restored.items():
        if not isinstance(task, dict):
            continue
        task["job_id"] = job_id
        if task.get("status") in {"running", "resuming", "submitted"}:
            # 服务重启后不再存在原训练进程，统一标记为可恢复状态
            task["status"] = "paused"
            task["updated_at"] = time.time()
        task.setdefault("history_points", [])
        train_tasks[job_id] = task
        _ensure_runtime_channels(job_id)
        _persist_task(job_id)


_restore_tasks_from_disk()


@app.route("/api/model/finetune/action", methods=["POST"])
def finetune_action():
    """
    微调动作接口：
    - action=start: 新建/恢复
    - action=pause: 暂停
    - action=stop: 终止
    """
    data = request.json
    if not data:
        return jsonify({"code": 400, "message": "请求体不能为空", "data": None}), 400

    action = data.get("action")
    if action not in {"start", "pause", "stop"}:
        return jsonify({"code": 400, "message": "action仅支持 start/pause/stop", "data": None}), 400

    job_id = data.get("job_id") or _new_job_id()
    config = _normalize_start_config(dict(data.get("config", {}) or {}))
    config.setdefault("train_mode", "sft")

    if action == "start":
        # 恢复暂停任务
        if job_id in train_tasks and train_tasks[job_id].get("status") == "paused":
            ok, msg = finetune_service.resume_job(job_id)
            if ok:
                train_tasks[job_id]["status"] = "resuming"
                train_tasks[job_id]["updated_at"] = time.time()
                progress = train_tasks[job_id].get("progress", {})
                resume_checkpoint = finetune_service.resolve_resume_checkpoint(train_tasks[job_id])
                _persist_task(job_id)
                return jsonify(
                    {
                        "code": 200,
                        "message": "任务已恢复，继续当前训练进程",
                        "data": _attach_finetune_model_info({
                            "job_id": job_id,
                            "status": "resuming",
                            "resume_mode": "process_continue",
                            "resume_config": {
                                "resume_from_checkpoint": (resume_checkpoint or {}).get("path"),
                                "start_step": progress.get("current_step", 0) + 1,
                                "total_steps": progress.get("total_steps", 100),
                                "remaining_steps": max(0, progress.get("total_steps", 100) - progress.get("current_step", 0)),
                            },
                            "monitor_url": f"/api/model/finetune/stream?job_id={job_id}",
                        }, train_tasks[job_id].get("config", config)),
                    }
                )

            resume_checkpoint = finetune_service.resolve_resume_checkpoint(train_tasks[job_id])
            if resume_checkpoint:
                checkpoint_path = str(resume_checkpoint.get("path") or "").strip()
                stored_config = train_tasks[job_id].get("config", {})
                resume_config = _merge_resume_config(stored_config, config, checkpoint_path)
                train_tasks[job_id]["last_checkpoint"] = resume_checkpoint
                _resume_from_checkpoint(job_id, resume_config, checkpoint_path)
                progress = train_tasks[job_id].get("progress", {})
                return jsonify(
                    {
                        "code": 200,
                        "message": "任务已恢复，正在加载断点",
                        "data": _attach_finetune_model_info({
                            "job_id": job_id,
                            "status": "resuming",
                            "resume_mode": "checkpoint_restart",
                            "resume_config": {
                                "resume_from_checkpoint": checkpoint_path,
                                "start_step": progress.get("current_step", 0) + 1,
                                "total_steps": progress.get("total_steps", 100),
                                "remaining_steps": max(0, progress.get("total_steps", 100) - progress.get("current_step", 0)),
                            },
                            "monitor_url": f"/api/model/finetune/stream?job_id={job_id}",
                        }, train_tasks[job_id].get("config", resume_config)),
                    }
                )
            return jsonify({"code": 400, "message": "任务未在运行，且当前没有可恢复checkpoint；只能在原训练进程仍存活时继续", "data": None}), 400

        # 已运行任务重复启动
        if job_id in train_tasks and train_tasks[job_id].get("status") in {"running", "resuming"}:
            return jsonify(
                {
                    "code": 200,
                    "message": "任务已在运行",
                    "data": _attach_finetune_model_info({
                        "job_id": job_id,
                        "status": train_tasks[job_id]["status"],
                        "progress": train_tasks[job_id].get("progress", {}),
                        "monitor_url": f"/api/model/finetune/stream?job_id={job_id}",
                    }, train_tasks[job_id].get("config", config)),
                }
            )

        validation_error = _validate_start_config(config)
        if validation_error:
            return jsonify({"code": 400, "message": validation_error, "data": None}), 400

        _start_new_job(job_id, config)
        return jsonify(
            {
                "code": 200,
                "message": "success",
                "data": _attach_finetune_model_info({
                    "job_id": job_id,
                    "status": "running",
                    "progress": train_tasks[job_id].get("progress", {}),
                    "monitor_url": f"/api/model/finetune/stream?job_id={job_id}",
                    "output_dir": os.path.join(finetune_service.models_dir, job_id),
                }, train_tasks[job_id].get("config", config)),
            }
        )

    # pause/stop
    missing_job = _ensure_job_exists(job_id)
    if missing_job:
        return missing_job

    if action == "pause":
        ok, msg = finetune_service.pause_job(job_id)
        if not ok:
            return jsonify({"code": 400, "message": msg, "data": None}), 400
        progress = train_tasks[job_id].get("progress", {})
        checkpoint = finetune_service.resolve_resume_checkpoint(train_tasks[job_id])
        train_tasks[job_id]["status"] = "paused"
        train_tasks[job_id]["updated_at"] = time.time()
        train_tasks[job_id]["last_checkpoint"] = checkpoint
        _persist_task(job_id)
        if checkpoint:
            return jsonify(
                {
                    "code": 200,
                    "message": "任务已暂停，已发现可恢复checkpoint",
                    "data": _attach_finetune_model_info({
                        "job_id": job_id,
                        "status": "paused",
                        "pause_mode": "checkpoint_ready",
                        "progress": progress,
                        "last_checkpoint": checkpoint,
                    }, train_tasks[job_id].get("config", {})),
                }
            )
        return jsonify(
            {
                "code": 200,
                "message": "任务已暂停，但当前尚无可恢复checkpoint；仅可在当前训练进程仍存活时继续",
                "data": _attach_finetune_model_info({
                    "job_id": job_id,
                    "status": "paused",
                    "pause_mode": "process_only",
                    "progress": progress,
                    "last_checkpoint": None,
                }, train_tasks[job_id].get("config", {})),
            }
        )

    # action == stop
    ok, msg = finetune_service.stop_job(job_id)
    if not ok:
        return jsonify({"code": 400, "message": msg, "data": None}), 400
    train_tasks[job_id]["status"] = "stopped"
    train_tasks[job_id]["updated_at"] = time.time()
    _persist_task(job_id)
    progress = train_tasks[job_id].get("progress", {})
    started_at = train_tasks[job_id].get("started_at")
    total_duration = _format_duration(time.time() - started_at) if started_at else None
    last_checkpoint = train_tasks[job_id].get("last_checkpoint") or {}
    best_model_path = (
        train_tasks[job_id].get("best_checkpoint")
        or train_tasks[job_id].get("model_path")
        or last_checkpoint.get("path")
    )
    best_metric = _normalize_best_metric(
        train_tasks[job_id].get("best_metric") or train_tasks[job_id].get("metrics")
    )
    return jsonify(
        {
            "code": 200,
            "message": "任务已终止",
            "data": _attach_finetune_model_info({
                "job_id": job_id,
                "status": "stopped",
                "summary": {
                    "stopped_at_step": progress.get("current_step", 0),
                    "total_steps": progress.get("total_steps", 100),
                    "best_model_path": best_model_path,
                    "best_metric": best_metric,
                    "total_duration": total_duration,
                },
            }, train_tasks[job_id].get("config", {})),
        }
    )


@app.route("/api/model/finetune/stream", methods=["GET"])
def finetune_stream():
    """SSE监控接口"""
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    if job_id not in task_logs:
        return jsonify({"code": 404, "message": "任务不存在", "data": None}), 404
    last_event_id_header = request.headers.get("Last-Event-ID") or request.headers.get("last-event-id")

    def generate():
        event_buffer = task_event_buffers.setdefault(job_id, [])

        def next_event_id() -> int:
            task_event_counters[job_id] = task_event_counters.get(job_id, 0) + 1
            return task_event_counters[job_id]

        def append_event(event_type: str, payload: Dict[str, Any]) -> str:
            event_id = next_event_id()
            event_buffer.append({"id": event_id, "event": event_type, "data": payload})
            if len(event_buffer) > 2000:
                del event_buffer[0]
            return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def latest_state() -> Dict[str, Any]:
            task = train_tasks.get(job_id, {})
            history_points = task.get("history_points") or []
            if history_points:
                last_point = history_points[-1]
                return {
                    "step": last_point.get("step"),
                    "train_loss": last_point.get("loss"),
                    "train_acc": last_point.get("acc"),
                }
            progress = task.get("progress", {})
            metric = _normalize_best_metric(task.get("metrics"))
            return {
                "step": progress.get("current_step"),
                "train_loss": metric.get("loss"),
                "train_acc": metric.get("accuracy"),
            }

        try:
            client_last_event_id = int(last_event_id_header) if last_event_id_header else None
        except (TypeError, ValueError):
            client_last_event_id = None

        # 历史状态
        if job_id in train_tasks:
            history_points = train_tasks[job_id].get("history_points") or []
            if not history_points:
                history_points = [
                    {
                        "step": train_tasks[job_id].get("progress", {}).get("current_step", 0),
                        "loss": None,
                        "lr": None,
                        "acc": None,
                    }
                ]
            history_payload = {
                "points": history_points
            }
            yield append_event("history", history_payload)

        # 网络恢复后重放事件
        if client_last_event_id is not None:
            replay_events = [evt for evt in event_buffer if int(evt.get("id", 0)) > client_last_event_id]
            for event in replay_events:
                yield (
                    f"id: {event['id']}\n"
                    f"event: {event['event']}\n"
                    f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                )
            if replay_events:
                recovered_payload = {
                    "job_id": job_id,
                    "recovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "client_last_event_id": client_last_event_id,
                    "server_current_event_id": task_event_counters.get(job_id, 0),
                    "resume_mode": "replay",
                    "replayed_event_count": len(replay_events),
                    "latest_state": latest_state(),
                    "message": f"Connection recovered, replayed {len(replay_events)} events.",
                }
                yield append_event("connection_recovered", recovered_payload)

        log_queue = task_logs[job_id]
        while True:
            try:
                event = log_queue.get(timeout=10)
                event_type = event.get("type", "log")
                if event_type == "finish":
                    yield append_event("finish", event)
                    break
                if event_type == "checkpoint":
                    yield append_event("checkpoint", event)
                else:
                    payload = {
                        "step": event.get("progress", {}).get("current_step"),
                        "loss": event.get("loss"),
                        "lr": event.get("lr"),
                        "acc": event.get("acc"),
                        "message": event.get("message"),
                    }
                    yield append_event("update", payload)
            except queue.Empty:
                recovered_payload = {
                    "job_id": job_id,
                    "recovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "client_last_event_id": client_last_event_id,
                    "server_current_event_id": task_event_counters.get(job_id, 0),
                    "resume_mode": "heartbeat",
                    "replayed_event_count": 0,
                    "latest_state": latest_state(),
                    "message": "Connection recovered, no replay needed.",
                }
                yield append_event("connection_recovered", recovered_payload)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/model/finetune/model/download", methods=["GET"])
def download_model():
    """下载训练完成模型"""
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"code": 400, "message": "缺少job_id参数", "data": None}), 400
    if job_id not in train_tasks:
        return jsonify({"code": 404, "message": "任务不存在", "data": None}), 404
    task = train_tasks[job_id]
    model_path = task.get("model_path")
    if task.get("status") != "completed":
        return jsonify({"code": 202, "message": "训练进行中", "data": {"job_id": job_id, "status": task.get("status")}}), 202
    if not model_path or not os.path.exists(model_path):
        return jsonify({"code": 404, "message": "模型文件不存在", "data": None}), 404
    return send_file(model_path, as_attachment=True, download_name=f"{job_id}.zip", mimetype="application/zip")


# 兼容旧路径
@app.route("/api/finetune/job/submit", methods=["POST"])
def legacy_submit():
    data = request.json or {}
    payload = {
        "action": "start",
        "job_id": data.get("job_id") or _new_job_id(),
        "config": _normalize_start_config({
            "base_model_path": data.get("base_model_path") or data.get("base_model") or data.get("model_name"),
            "model_name": data.get("model_name") or data.get("base_model_path") or data.get("base_model"),
            "train_file_path": data.get("train_file_path"),
            "preference_file_path": data.get("preference_file_path"),
            "train_mode": data.get("train_mode", "sft"),
            "parameters": {
                "epochs": data.get("epochs", data.get("epoch", 3)),
                "learning_rate": data.get("learning_rate", 2e-4),
                "batch_size": data.get("batch_size", 4),
                "lora_rank": data.get("lora_rank", 16),
                "lora_alpha": data.get("lora_alpha", 32),
                "lora_dropout": data.get("lora_dropout", 0.05),
                "max_length": data.get("max_length", 2048),
                "save_steps": data.get("save_steps", 500),
            },
        }),
    }
    with app.test_request_context("/api/model/finetune/action", method="POST", json=payload):
        return finetune_action()


@app.route("/api/finetune/job/stream", methods=["GET"])
def legacy_stream():
    return finetune_stream()


@app.route("/api/finetune/model/download", methods=["GET"])
def legacy_download():
    return download_model()


@app.route("/api/finetune/job/status", methods=["GET"])
def job_status():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"code": 200, "message": "success", "data": list(train_tasks.values())})
    if job_id not in train_tasks:
        return jsonify({"code": 404, "message": "任务不存在", "data": None}), 404
    return jsonify({"code": 200, "message": "success", "data": train_tasks[job_id]})


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "service": "finetune"})

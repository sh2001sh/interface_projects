from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from flask import Response, jsonify, request, stream_with_context


_JOBS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_HEARTBEAT_SECONDS = 3.0


def _now_iso() -> str:
    return datetime.now().isoformat()


def _job_snapshot(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "job_type": job["job_type"],
        "status": job["status"],
        "stage": job.get("stage"),
        "message": job.get("message"),
        "progress": job.get("progress", 0.0),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "result": job.get("result"),
        "error": job.get("error"),
        "metadata": job.get("metadata") or {},
    }


def _append_event(job: Dict[str, Any], event: str, data: Dict[str, Any]) -> None:
    payload = dict(data or {})
    payload.setdefault("job_id", job["job_id"])
    payload.setdefault("timestamp", time.time())
    job.setdefault("events", []).append({"event": event, "data": payload})


def create_job(job_type: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    job_id = f"{job_type}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    job = {
        "job_id": job_id,
        "job_type": job_type,
        "status": "queued",
        "stage": "queued",
        "message": "任务已创建，等待处理",
        "progress": 0.0,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "result": None,
        "error": None,
        "metadata": metadata or {},
        "events": [],
    }
    _append_event(job, "progress", _job_snapshot(job))
    with _LOCK:
        _JOBS[job_id] = job
    return _job_snapshot(job)


def update_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    progress: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        if status is not None:
            job["status"] = status
        if stage is not None:
            job["stage"] = stage
        if message is not None:
            job["message"] = message
        if progress is not None:
            job["progress"] = round(float(progress), 4)
        if extra:
            cleaned_extra = {key: value for key, value in extra.items() if value is not None}
            if cleaned_extra:
                job["metadata"] = {**(job.get("metadata") or {}), **cleaned_extra}
        job["updated_at"] = _now_iso()
        snapshot = _job_snapshot(job)
        _append_event(job, "progress", snapshot)


def complete_job(job_id: str, result: Dict[str, Any]) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job["status"] = "completed"
        job["stage"] = "completed"
        job["message"] = "任务已完成"
        job["progress"] = 100.0
        job["result"] = result
        job["updated_at"] = _now_iso()
        snapshot = _job_snapshot(job)
        _append_event(job, "result", snapshot)


def fail_job(job_id: str, error: str) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job["status"] = "failed"
        job["stage"] = "failed"
        job["message"] = error
        job["error"] = error
        job["updated_at"] = _now_iso()
        snapshot = _job_snapshot(job)
        _append_event(job, "error", snapshot)


def get_job_snapshot(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        return _job_snapshot(job)


def start_job(
    job_type: str,
    worker: Callable[[str], None],
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    snapshot = create_job(job_type, metadata=metadata)

    def runner() -> None:
        try:
            worker(snapshot["job_id"])
        except Exception as exc:
            fail_job(snapshot["job_id"], str(exc))

    threading.Thread(target=runner, name=f"{job_type}_{snapshot['job_id']}", daemon=True).start()
    return snapshot


def build_submit_response(job: Dict[str, Any]) -> Response:
    base_path = request.path.rstrip("/")
    return jsonify(
        {
            "code": 200,
            "message": "accepted",
            "data": {
                "accepted": True,
                "job_id": job["job_id"],
                "status": job["status"],
                "stage": job["stage"],
                "message": job["message"],
                "result_ready": False,
                "status_url": f"{base_path}/status?job_id={job['job_id']}",
                "stream_url": f"{base_path}/stream?job_id={job['job_id']}",
                "metadata": job.get("metadata") or {},
            },
        }
    ), 200


def build_status_response(job_id: str) -> Response:
    snapshot = get_job_snapshot(job_id)
    if snapshot is None:
        return jsonify({"code": 404, "message": "job_id不存在", "data": None}), 404
    return jsonify({"code": 200, "message": "success", "data": snapshot})


def build_stream_response(job_id: str) -> Response:
    snapshot = get_job_snapshot(job_id)
    if snapshot is None:
        return jsonify({"code": 404, "message": "job_id不存在", "data": None}), 404

    @stream_with_context
    def generate():
        cursor = 0
        yield f"event: open\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
        while True:
            with _LOCK:
                job = _JOBS.get(job_id)
                events = list((job or {}).get("events", []))
                terminal = bool(job and job.get("status") in {"completed", "failed"})
            while cursor < len(events):
                item = events[cursor]
                cursor += 1
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
            if terminal:
                yield f"event: close\ndata: {json.dumps({'job_id': job_id, 'status': job.get('status')}, ensure_ascii=False)}\n\n"
                break
            yield f"event: heartbeat\ndata: {json.dumps({'job_id': job_id, 'timestamp': time.time()}, ensure_ascii=False)}\n\n"
            time.sleep(_HEARTBEAT_SECONDS)

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response

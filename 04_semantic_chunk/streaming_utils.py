"""Streaming helpers for standalone interface deployment."""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional

from flask import Response, copy_current_request_context, current_app, request, stream_with_context


_END = object()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def is_stream_requested() -> bool:
    if _as_bool(request.args.get("stream")):
        return True
    if _as_bool(request.headers.get("X-Stream-Response")):
        return True
    if request.mimetype == "application/json":
        payload = request.get_json(silent=True) or {}
        return isinstance(payload, dict) and _as_bool(payload.get("stream"))
    return _as_bool(request.form.get("stream"))


def _sse_payload(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def normalize_flask_response(response_value: Any) -> Dict[str, Any]:
    response = current_app.make_response(response_value)
    raw_text = response.get_data(as_text=True)
    try:
        result = json.loads(raw_text)
    except Exception:
        result = raw_text
    return {"status_code": response.status_code, "result": result}


def stream_flask_handler(
    task_name: str,
    handler: Callable[[], Any],
    *,
    heartbeat_seconds: float = 5.0,
    initial_message: Optional[str] = None,
) -> Response:
    events: "queue.Queue[Any]" = queue.Queue()

    @copy_current_request_context
    def run() -> None:
        try:
            events.put(("progress", {"task": task_name, "stage": "started", "message": initial_message or f"{task_name}已开始处理", "timestamp": time.time()}))
            result = normalize_flask_response(handler())
            events.put(("result", {"task": task_name, "stage": "completed", "timestamp": time.time(), **result}))
        except Exception as exc:
            events.put(("error", {"task": task_name, "stage": "failed", "message": str(exc), "timestamp": time.time()}))
        finally:
            events.put(_END)

    threading.Thread(target=run, name=f"{task_name}_stream", daemon=True).start()

    @stream_with_context
    def generate():
        yield _sse_payload("open", {"task": task_name, "message": "流式连接已建立", "timestamp": time.time()})
        while True:
            try:
                item = events.get(timeout=max(float(heartbeat_seconds), 1.0))
            except queue.Empty:
                yield _sse_payload("heartbeat", {"task": task_name, "message": "任务处理中", "timestamp": time.time()})
                continue
            if item is _END:
                yield _sse_payload("close", {"task": task_name, "message": "流式输出结束", "timestamp": time.time()})
                break
            event, payload = item
            yield _sse_payload(event, payload)

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


def stream_callable_with_progress(
    task_name: str,
    worker: Callable[[Callable[[str, Dict[str, Any]], None]], Any],
    *,
    heartbeat_seconds: float = 5.0,
) -> Response:
    events: "queue.Queue[Any]" = queue.Queue()

    def emit(event: str, payload: Dict[str, Any]) -> None:
        enriched = dict(payload or {})
        enriched.setdefault("task", task_name)
        enriched.setdefault("timestamp", time.time())
        events.put((event, enriched))

    def run() -> None:
        try:
            emit("progress", {"stage": "started", "message": f"{task_name}已开始处理"})
            result = worker(emit)
            emit("result", {"stage": "completed", "result": result})
        except Exception as exc:
            emit("error", {"stage": "failed", "message": str(exc)})
        finally:
            events.put(_END)

    threading.Thread(target=run, name=f"{task_name}_progress_stream", daemon=True).start()

    @stream_with_context
    def generate():
        yield _sse_payload("open", {"task": task_name, "message": "流式连接已建立", "timestamp": time.time()})
        while True:
            try:
                item = events.get(timeout=max(float(heartbeat_seconds), 1.0))
            except queue.Empty:
                yield _sse_payload("heartbeat", {"task": task_name, "message": "任务处理中", "timestamp": time.time()})
                continue
            if item is _END:
                yield _sse_payload("close", {"task": task_name, "message": "流式输出结束", "timestamp": time.time()})
                break
            event, payload = item
            yield _sse_payload(event, payload)

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response

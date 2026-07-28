"""Shared UDP JSON gateway for interface-project Flask services."""

from __future__ import annotations

import json
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from flask import Flask


_DEFAULT_MAX_DATAGRAM_BYTES = 65535
_DEFAULT_SAFE_RESPONSE_BYTES = 60000
_RESERVED_REQUEST_KEYS = {"path", "body", "headers", "query", "method", "request_id"}


def normalize_udp_gateway_config(
    server_config: Dict[str, Any],
    *,
    default_route: str,
    allowed_routes: Sequence[str],
    runtime_root: str | Path,
    app_name: str,
) -> Dict[str, Any]:
    """Build a normalized UDP gateway config from the service server config."""
    server = dict(server_config or {})
    udp = server.get("udp") if isinstance(server.get("udp"), dict) else {}
    port = int(server.get("port") or 0)
    host = str(server.get("host") or "0.0.0.0").strip() or "0.0.0.0"
    runtime_path = Path(runtime_root).resolve()
    response_dir = runtime_path / "deliverables" / "udp_responses"
    allowed = [str(item).strip() for item in allowed_routes if str(item).strip()]
    return {
        "enabled": bool(udp.get("enabled", True)),
        "host": str(udp.get("host") or host).strip() or host,
        "port": int(udp.get("port") or port),
        "default_route": str(udp.get("default_route") or default_route).strip() or default_route,
        "allowed_routes": allowed,
        "max_datagram_bytes": int(udp.get("max_datagram_bytes") or _DEFAULT_MAX_DATAGRAM_BYTES),
        "max_response_bytes": int(udp.get("max_response_bytes") or _DEFAULT_SAFE_RESPONSE_BYTES),
        "response_dir": response_dir,
        "app_name": app_name,
    }


def start_udp_json_gateway(app: Flask, config: Dict[str, Any]) -> Optional[socketserver.UDPServer]:
    """Start a UDP JSON gateway in a background thread for a Flask app."""
    normalized = dict(config or {})
    if not normalized.get("enabled"):
        return None

    gateway_key = "udp_json_gateway"
    extensions = getattr(app, "extensions", None)
    if not isinstance(extensions, dict):
        app.extensions = {}
        extensions = app.extensions
    existing = extensions.get(gateway_key)
    if existing is not None:
        return existing

    host = str(normalized.get("host") or "0.0.0.0").strip() or "0.0.0.0"
    port = int(normalized.get("port") or 0)
    if port <= 0:
        raise ValueError("UDP 网关端口必须大于 0")

    request_handler = _build_udp_request_handler(app, normalized)
    server = socketserver.UDPServer((host, port), request_handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"{normalized.get('app_name') or 'interface'}-udp-gateway",
        daemon=True,
    )
    thread.start()
    extensions[gateway_key] = server
    return server


def run_flask_service(app: Flask, runner: Dict[str, Any], udp_config: Optional[Dict[str, Any]] = None) -> None:
    """Run the Flask service and optionally start the UDP gateway first."""
    if udp_config:
        start_udp_json_gateway(app, udp_config)
    app.run(
        host=runner["host"],
        port=runner["port"],
        debug=False,
        threaded=bool(runner.get("threaded")),
        use_reloader=False,
    )


def _build_udp_request_handler(app: Flask, config: Dict[str, Any]):
    class UDPJsonRequestHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            packet, sock = self.request
            response_bytes = _handle_udp_packet(app, config, packet)
            sock.sendto(response_bytes, self.client_address)

    return UDPJsonRequestHandler


def _handle_udp_packet(app: Flask, config: Dict[str, Any], packet: bytes) -> bytes:
    max_datagram_bytes = int(config.get("max_datagram_bytes") or _DEFAULT_MAX_DATAGRAM_BYTES)
    if len(packet) > max_datagram_bytes:
        return _encode_udp_json(
            {
                "transport": "udp",
                "http_status": 413,
                "body": {
                    "code": 413,
                    "message": f"UDP 请求过大: {len(packet)} bytes > {max_datagram_bytes} bytes",
                    "data": None,
                },
            }
        )

    try:
        request_payload = json.loads(packet.decode("utf-8"))
    except UnicodeDecodeError:
        return _encode_udp_json(
            {
                "transport": "udp",
                "http_status": 400,
                "body": {"code": 400, "message": "UDP 请求必须是 UTF-8 JSON", "data": None},
            }
        )
    except json.JSONDecodeError as exc:
        return _encode_udp_json(
            {
                "transport": "udp",
                "http_status": 400,
                "body": {"code": 400, "message": f"UDP JSON 解析失败: {exc}", "data": None},
            }
        )

    if not isinstance(request_payload, dict):
        return _encode_udp_json(
            {
                "transport": "udp",
                "http_status": 400,
                "body": {"code": 400, "message": "UDP 请求体必须是 JSON 对象", "data": None},
            }
        )

    path = str(request_payload.get("path") or config.get("default_route") or "").strip()
    allowed_routes = list(config.get("allowed_routes") or [])
    if not path:
        return _encode_udp_json(
            {
                "transport": "udp",
                "http_status": 400,
                "body": {"code": 400, "message": "UDP 请求缺少 path，且当前服务未配置默认路由", "data": None},
            }
        )
    if allowed_routes and path not in allowed_routes:
        return _encode_udp_json(
            {
                "transport": "udp",
                "path": path,
                "http_status": 404,
                "body": {"code": 404, "message": f"UDP 路由不支持: {path}", "data": {"allowed_routes": allowed_routes}},
            }
        )

    try:
        body = _extract_udp_request_body(request_payload)
    except ValueError as exc:
        return _encode_udp_json(
            {
                "transport": "udp",
                "path": path,
                "http_status": 400,
                "body": {"code": 400, "message": str(exc), "data": None},
            }
        )
    headers = request_payload.get("headers") if isinstance(request_payload.get("headers"), dict) else {}
    query = request_payload.get("query") if isinstance(request_payload.get("query"), dict) else {}
    query = {str(key): value for key, value in query.items() if value is not None}
    query.pop("stream", None)
    request_id = str(request_payload.get("request_id") or "").strip() or None

    try:
        with app.test_request_context(path=path, method="POST", json=body, headers=headers, query_string=query):
            flask_response = app.full_dispatch_request()
    except Exception as exc:
        return _encode_udp_json(
            {
                "transport": "udp",
                "path": path,
                "request_id": request_id,
                "http_status": 500,
                "body": {"code": 500, "message": f"UDP 调用服务异常: {exc}", "data": None},
            }
        )

    response_text = flask_response.get_data(as_text=True)
    parsed_body = _parse_possible_json(response_text)
    envelope = {
        "transport": "udp",
        "path": path,
        "request_id": request_id,
        "http_status": int(flask_response.status_code),
        "body": parsed_body,
    }
    response_bytes = _encode_udp_json(envelope)
    max_response_bytes = int(config.get("max_response_bytes") or _DEFAULT_SAFE_RESPONSE_BYTES)
    if len(response_bytes) <= max_response_bytes:
        return response_bytes

    response_file = _persist_large_udp_response(config, response_bytes)
    fallback_body = _build_large_response_fallback(parsed_body, response_file, len(response_bytes))
    fallback_envelope = {
        "transport": "udp",
        "path": path,
        "request_id": request_id,
        "http_status": int(flask_response.status_code),
        "body": fallback_body,
    }
    return _encode_udp_json(fallback_envelope)


def _extract_udp_request_body(request_payload: Dict[str, Any]) -> Dict[str, Any]:
    explicit_body = request_payload.get("body")
    if isinstance(explicit_body, dict):
        return explicit_body
    if explicit_body is not None:
        raise ValueError("UDP body 字段必须是 JSON 对象")

    body = {
        key: value
        for key, value in request_payload.items()
        if key not in _RESERVED_REQUEST_KEYS
    }
    return body


def _parse_possible_json(raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text


def _encode_udp_json(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _persist_large_udp_response(config: Dict[str, Any], response_bytes: bytes) -> str:
    response_dir = Path(config["response_dir"])
    response_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{config.get('app_name') or 'interface'}_{timestamp}_{int(time.time_ns() % 1_000_000)}.json"
    response_path = response_dir / filename
    response_path.write_bytes(response_bytes)
    return str(response_path)


def _build_large_response_fallback(parsed_body: Any, response_file: str, response_bytes: int) -> Dict[str, Any]:
    if isinstance(parsed_body, dict):
        code = int(parsed_body.get("code") or 200)
        message = str(parsed_body.get("message") or "success")
    else:
        code = 200
        message = "success"
    return {
        "code": code,
        "message": message,
        "data": {
            "transport": "udp",
            "truncated": True,
            "response_file": response_file,
            "response_bytes": response_bytes,
        },
    }

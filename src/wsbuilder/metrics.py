"""Application metrics collector and JSON streaming helpers."""

import json
import threading
import time
from datetime import UTC, datetime

from .http import Response

DEFAULT_STREAM_POINTS = 5


def _iso_utc(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value, default):
    try:
        return float(value)
    except Exception:
        return default


def _is_truthy(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


class AppMetrics:
    def __init__(self, app_name="wsbuilder-app"):
        self.app_name = app_name
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._extra_snapshot_provider = None

        self.active_tcp_connections = 0
        self.total_tcp_connections = 0

        self.active_http_requests = 0
        self.total_http_requests = 0
        self.total_http_responses = 0
        self.http_methods = {}
        self.http_paths = {}
        self.http_status = {}
        self.http_total_duration_ms = 0.0
        self.http_max_duration_ms = 0.0

        self.active_ws_connections = 0
        self.total_ws_upgrades = 0
        self.ws_paths = {}
        self.ws_messages_in = 0
        self.ws_messages_out = 0
        self.ws_bytes_in = 0
        self.ws_bytes_out = 0

        self.bytes_in = 0
        self.bytes_out = 0

        self.total_errors = 0
        self.last_error = ""

    def set_extra_snapshot_provider(self, provider):
        if callable(provider):
            self._extra_snapshot_provider = provider
        else:
            self._extra_snapshot_provider = None

    def _inc_map(self, data, key, step=1):
        data[key] = data.get(key, 0) + step

    def tcp_connection_open(self):
        with self._lock:
            self.active_tcp_connections += 1
            self.total_tcp_connections += 1

    def tcp_connection_close(self):
        with self._lock:
            if self.active_tcp_connections > 0:
                self.active_tcp_connections -= 1

    def http_request_started(self, method, path, body_size=0):
        method = (method or "").upper() or "UNKNOWN"
        path = path or "/"
        body_size = max(0, int(body_size or 0))
        with self._lock:
            self.active_http_requests += 1
            self.total_http_requests += 1
            self.bytes_in += body_size
            self._inc_map(self.http_methods, method)
            self._inc_map(self.http_paths, path)

    def http_response_sent(self, method, path, status, body_size=0, duration_ms=0.0):
        status_key = str(int(status or 0))
        body_size = max(0, int(body_size or 0))
        duration_ms = max(0.0, float(duration_ms or 0.0))
        with self._lock:
            if self.active_http_requests > 0:
                self.active_http_requests -= 1
            self.total_http_responses += 1
            self.bytes_out += body_size
            self._inc_map(self.http_status, status_key)
            self.http_total_duration_ms += duration_ms
            if duration_ms > self.http_max_duration_ms:
                self.http_max_duration_ms = duration_ms

    def ws_opened(self, path):
        path = path or "/ws/"
        with self._lock:
            self.active_ws_connections += 1
            self.total_ws_upgrades += 1
            self._inc_map(self.ws_paths, path)

    def ws_closed(self, path):
        with self._lock:
            if self.active_ws_connections > 0:
                self.active_ws_connections -= 1

    def ws_message_in(self, payload_size=0):
        payload_size = max(0, int(payload_size or 0))
        with self._lock:
            self.ws_messages_in += 1
            self.ws_bytes_in += payload_size
            self.bytes_in += payload_size

    def ws_message_out(self, payload_size=0):
        payload_size = max(0, int(payload_size or 0))
        with self._lock:
            self.ws_messages_out += 1
            self.ws_bytes_out += payload_size
            self.bytes_out += payload_size

    def error(self, where, exc):
        msg = f"{where}: {exc}"
        with self._lock:
            self.total_errors += 1
            self.last_error = msg

    def snapshot(self):
        now = time.time()
        with self._lock:
            methods = dict(self.http_methods)
            paths = dict(self.http_paths)
            status = dict(self.http_status)
            ws_paths = dict(self.ws_paths)
            total_responses = self.total_http_responses
            avg_ms = 0.0
            if total_responses > 0:
                avg_ms = self.http_total_duration_ms / float(total_responses)

            data = {
                "app_name": self.app_name,
                "timestamp_unix": now,
                "timestamp_utc": _iso_utc(now),
                "started_at_unix": self.started_at,
                "started_at_utc": _iso_utc(self.started_at),
                "uptime_seconds": round(now - self.started_at, 3),
                "connections": {
                    "tcp_active": self.active_tcp_connections,
                    "tcp_total": self.total_tcp_connections,
                    "http_inflight": self.active_http_requests,
                    "ws_active": self.active_ws_connections,
                },
                "http": {
                    "requests_total": self.total_http_requests,
                    "responses_total": self.total_http_responses,
                    "methods": methods,
                    "paths": paths,
                    "status": status,
                    "duration_ms_avg": round(avg_ms, 3),
                    "duration_ms_max": round(self.http_max_duration_ms, 3),
                },
                "websocket": {
                    "upgrades_total": self.total_ws_upgrades,
                    "paths": ws_paths,
                    "messages_in_total": self.ws_messages_in,
                    "messages_out_total": self.ws_messages_out,
                    "bytes_in_total": self.ws_bytes_in,
                    "bytes_out_total": self.ws_bytes_out,
                },
                "traffic": {
                    "bytes_in_total": self.bytes_in,
                    "bytes_out_total": self.bytes_out,
                },
                "errors": {
                    "total": self.total_errors,
                    "last": self.last_error,
                },
            }
        provider = self._extra_snapshot_provider
        if provider:
            try:
                extra = provider()
            except Exception as e:
                extra = {"metrics_provider_error": str(e)}
            if isinstance(extra, dict):
                data.update(extra)
        return data

    def stream_chunks(self, interval_seconds=1.0, max_points=None):
        interval_seconds = _safe_float(interval_seconds, 1.0)
        if interval_seconds < 0.1:
            interval_seconds = 0.1
        if interval_seconds > 60.0:
            interval_seconds = 60.0
        if max_points is not None:
            max_points = _safe_int(max_points, None)
            if max_points is not None and max_points <= 0:
                max_points = None

        sent = 0
        while True:
            payload = json.dumps(
                self.snapshot(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            yield payload
            sent += 1
            if max_points is not None and sent >= max_points:
                break
            time.sleep(interval_seconds)

    def response_snapshot(self):
        return Response.json(self.snapshot())

    def response_stream(self, interval_seconds=1.0, max_points=None):
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        }
        return Response.stream(
            self.stream_chunks(interval_seconds=interval_seconds, max_points=max_points),
            content_type="application/x-ndjson; charset=utf-8",
            headers=headers,
        )


def install_metrics(
    app,
    path="/api/metrics",
    stream_path="/api/metrics/stream",
    app_name=None,
    extra_snapshot_provider=None,
):
    name = app_name or app.__class__.__name__
    metrics = AppMetrics(app_name=name)
    if extra_snapshot_provider:
        metrics.set_extra_snapshot_provider(extra_snapshot_provider)
    app.metrics = metrics

    @app.api(path, methods=("GET",))
    def _metrics_snapshot(request):
        return metrics.response_snapshot()

    @app.api(stream_path, methods=("GET",))
    def _metrics_stream(request):
        interval = request.query.get("interval", "1.0")
        limit_text = request.query.get("limit", "")
        follow = request.query.get("follow", "")
        if limit_text:
            parsed = _safe_int(limit_text, DEFAULT_STREAM_POINTS)
            if parsed <= 0:
                max_points = None
            else:
                max_points = parsed
        else:
            if _is_truthy(follow):
                max_points = None
            else:
                max_points = DEFAULT_STREAM_POINTS
        return metrics.response_stream(interval_seconds=interval, max_points=max_points)

    return metrics


__all__ = ["AppMetrics", "install_metrics"]

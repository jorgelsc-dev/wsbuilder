"""Running statistics and per-target/per-rule metric buckets for ProxyI."""

from __future__ import annotations

import math
import threading
import time

from ._common import (
    _normalize_text,
)


class RunningStats:
    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.mean = 0.0
        self.m2 = 0.0
        self.min = None
        self.max = None
        self.last = 0.0

    def observe(self, value):
        x = float(value or 0.0)
        self.count += 1
        delta = x - self.mean
        self.mean += delta / float(self.count)
        delta2 = x - self.mean
        self.m2 += delta * delta2
        self.total += x
        self.last = x
        if self.min is None or x < self.min:
            self.min = x
        if self.max is None or x > self.max:
            self.max = x

    def snapshot(self):
        count = int(self.count)
        variance = self.m2 / (count - 1) if count > 1 else 0.0
        if variance < 0.0:
            variance = 0.0
        stddev = math.sqrt(variance)
        stderr = stddev / math.sqrt(count) if count > 0 else 0.0
        uncertainty = 1.96 * stderr
        mean = self.mean if count > 0 else 0.0
        min_value = self.min if self.min is not None else 0.0
        max_value = self.max if self.max is not None else 0.0
        return {
            "count": count,
            "sum": round(self.total, 6),
            "mean": round(mean, 6),
            "min": round(min_value, 6),
            "max": round(max_value, 6),
            "variance": round(variance, 6),
            "stddev": round(stddev, 6),
            "stderr": round(stderr, 6),
            "uncertainty_95": round(uncertainty, 6),
            "lower_95": round(mean - uncertainty, 6),
            "upper_95": round(mean + uncertainty, 6),
            "last": round(self.last, 6),
        }

class ProxyMetricsBucket:
    def __init__(self, label=""):
        self.label = _normalize_text(label)
        self.started_at = time.time()
        self._lock = threading.RLock()
        self.requests_total = 0
        self.responses_total = 0
        self.errors_total = 0
        self.active_requests = 0
        self.bytes_in_total = 0
        self.bytes_out_total = 0
        self.last_status = 0
        self.last_error = ""
        self.last_request_at = 0.0
        self.last_response_at = 0.0
        self.last_selected_at = 0.0
        self.status_counts = {}
        self.request_bytes = RunningStats()
        self.response_bytes = RunningStats()
        self.latency_ms = RunningStats()

    def _inc_map(self, data, key, step=1):
        data[key] = data.get(key, 0) + int(step)

    def mark_selected(self, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            self.last_selected_at = now

    def begin_request(self, request_size=0, now=None):
        now = time.time() if now is None else float(now)
        size = max(0, int(request_size or 0))
        with self._lock:
            self.requests_total += 1
            self.active_requests += 1
            self.bytes_in_total += size
            self.request_bytes.observe(size)
            self.last_request_at = now

    def finish_request(self, status, response_size=0, latency_ms=0.0, error=None, now=None):
        now = time.time() if now is None else float(now)
        size = max(0, int(response_size or 0))
        latency_ms = max(0.0, float(latency_ms or 0.0))
        with self._lock:
            if self.active_requests > 0:
                self.active_requests -= 1
            self.responses_total += 1
            self.bytes_out_total += size
            self.response_bytes.observe(size)
            self.latency_ms.observe(latency_ms)
            self.last_status = int(status or 0)
            self.last_response_at = now
            self._inc_map(self.status_counts, str(int(status or 0)))
            if error is not None:
                self.errors_total += 1
                self.last_error = _normalize_text(error)
            elif self.last_status >= 500:
                self.last_error = f"status {self.last_status}"

    def snapshot(self):
        with self._lock:
            now = time.time()
            return {
                "label": self.label,
                "started_at_unix": self.started_at,
                "uptime_seconds": round(now - self.started_at, 3),
                "requests_total": self.requests_total,
                "responses_total": self.responses_total,
                "errors_total": self.errors_total,
                "active_requests": self.active_requests,
                "current_load": self.active_requests,
                "bytes_in_total": self.bytes_in_total,
                "bytes_out_total": self.bytes_out_total,
                "last_status": self.last_status,
                "last_error": self.last_error,
                "last_request_at": self.last_request_at,
                "last_response_at": self.last_response_at,
                "last_selected_at": self.last_selected_at,
                "status_counts": dict(self.status_counts),
                "request_bytes": self.request_bytes.snapshot(),
                "response_bytes": self.response_bytes.snapshot(),
                "latency_ms": self.latency_ms.snapshot(),
            }

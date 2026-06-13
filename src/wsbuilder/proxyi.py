"""Reverse proxy and virtual-host routing utilities."""

from __future__ import annotations

import fnmatch
import hashlib
import http.client
import json
import math
import random
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from html import escape as html_escape
from urllib.parse import urlsplit

from .http import Request, Response

DEFAULT_STREAM_POINTS = 5
DEFAULT_MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_DASHBOARD_PATH = "/__proxyi__"
DEFAULT_METRICS_PATH = "/__proxyi__/metrics"
DEFAULT_STREAM_PATH = "/__proxyi__/metrics/stream"

BALANCING_ROUND_ROBIN = "round_robin"
BALANCING_WEIGHTED_ROUND_ROBIN = "weighted_round_robin"
BALANCING_RANDOM = "random"
BALANCING_LEAST_CONNECTIONS = "least_connections"
BALANCING_LEAST_RESPONSE_TIME = "least_response_time"
BALANCING_LEAST_REQUESTS = "least_requests"
BALANCING_LEAST_BYTES_IN = "least_bytes_in"
BALANCING_LEAST_BYTES_OUT = "least_bytes_out"
BALANCING_IP_HASH = "ip_hash"
BALANCING_CONSISTENT_HASH = "consistent_hash"
BALANCING_FIRST_AVAILABLE = "first_available"
BALANCING_POWER_OF_TWO_CHOICES = "power_of_two_choices"
BALANCING_BEST = "best"

SUPPORTED_BALANCING_STRATEGIES = (
    BALANCING_ROUND_ROBIN,
    BALANCING_WEIGHTED_ROUND_ROBIN,
    BALANCING_RANDOM,
    BALANCING_LEAST_CONNECTIONS,
    BALANCING_LEAST_RESPONSE_TIME,
    BALANCING_LEAST_REQUESTS,
    BALANCING_LEAST_BYTES_IN,
    BALANCING_LEAST_BYTES_OUT,
    BALANCING_IP_HASH,
    BALANCING_CONSISTENT_HASH,
    BALANCING_FIRST_AVAILABLE,
    BALANCING_POWER_OF_TWO_CHOICES,
    BALANCING_BEST,
)

_HOP_BY_HOP_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


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
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_text(value):
    return str(value or "").strip()


def _normalize_lower(value):
    return _normalize_text(value).lower()


def _normalize_tuple(values, *, lower=False):
    if values is None:
        return ()
    if isinstance(values, str):
        rows = [values]
    else:
        rows = list(values)
    cleaned = []
    seen = set()
    for value in rows:
        text = _normalize_text(value)
        if not text:
            continue
        key = text.lower() if lower else text
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text.lower() if lower else text)
    return tuple(cleaned)


def _normalize_mapping(mapping, *, lower_keys=False, lower_values=False):
    result = {}
    for key, value in (mapping or {}).items():
        name = _normalize_text(key)
        if not name:
            continue
        if lower_keys:
            name = name.lower()
        result[name] = _normalize_lower(value) if lower_values else _normalize_text(value)
    return result


def _normalize_host_header(value):
    text = _normalize_text(value)
    if not text:
        return ""
    if text.startswith("[") and "]" in text:
        host, _, _port = text[1:].partition("]")
        return host.lower()
    host, _, _port = text.partition(":")
    return host.lower()


def _extract_request_host(request):
    headers = getattr(request, "headers", {}) or {}
    host = headers.get("host", "")
    if host:
        return _normalize_host_header(host)
    client = getattr(request, "client", None)
    if client and isinstance(client, (list, tuple)) and client:
        return _normalize_host_header(client[0])
    return ""


def _ensure_scheme(url):
    text = _normalize_text(url)
    if not text:
        raise ValueError("upstream url is required")
    if "://" not in text:
        text = f"http://{text.lstrip('/')}"
    return text


def _format_authority(host, port):
    host_text = _normalize_text(host)
    if not host_text:
        return ""
    if ":" in host_text and not host_text.startswith("["):
        host_text = f"[{host_text}]"
    if port:
        return f"{host_text}:{int(port)}"
    return host_text


def _split_url(url):
    parsed = urlsplit(_ensure_scheme(url))
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    base_path = parsed.path or "/"
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    if not base_path:
        base_path = "/"
    authority = _format_authority(host, port)
    normalized = f"{scheme}://{authority}{base_path if base_path != '/' else ''}"
    if base_path == "/":
        normalized = f"{scheme}://{authority}"
    return {
        "scheme": scheme,
        "host": host,
        "port": int(port),
        "base_path": base_path,
        "authority": authority,
        "url": normalized,
    }


def _join_paths(base_path, request_path):
    base = _normalize_text(base_path) or "/"
    path = _normalize_text(request_path) or "/"
    if not path.startswith("/"):
        path = "/" + path
    if base in {"", "/"}:
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


def _strip_prefix(path, prefix):
    raw_path = _normalize_text(path) or "/"
    raw_prefix = _normalize_text(prefix)
    if not raw_prefix:
        return raw_path
    if not raw_path.startswith(raw_prefix):
        return raw_path
    remainder = raw_path[len(raw_prefix) :]
    if not remainder:
        return "/"
    if not remainder.startswith("/"):
        remainder = "/" + remainder
    return remainder


def _combine_query(path, query_string):
    if not query_string:
        return path
    return f"{path}?{query_string}"


def _clean_response_headers(headers):
    clean = {}
    for key, value in (headers or {}).items():
        name = _normalize_text(key)
        if not name:
            continue
        if name.lower() in _HOP_BY_HOP_RESPONSE_HEADERS:
            continue
        clean[name] = _normalize_text(value)
    return clean


def _clean_request_headers(headers):
    clean = {}
    for key, value in (headers or {}).items():
        name = _normalize_text(key)
        if not name:
            continue
        if name.lower() in _HOP_BY_HOP_REQUEST_HEADERS:
            continue
        clean[name] = _normalize_text(value)
    return clean


def _request_body_bytes(request):
    body = getattr(request, "body", b"")
    if body is None:
        return b""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return _normalize_text(body).encode("utf-8")


def _request_path(request):
    path = _normalize_text(getattr(request, "path", "")) or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _request_query_string(request):
    query = getattr(request, "query_string", "")
    return _normalize_text(query)


def _header_value(request, name):
    headers = getattr(request, "headers", {}) or {}
    return _normalize_text(headers.get(name.lower(), ""))


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


@dataclass
class ProxyTarget:
    url: str
    name: str = ""
    weight: int = 1
    priority: int = 0
    timeout_seconds: float = 5.0
    preserve_host: bool = True
    enabled: bool = True
    verify_tls: bool = False
    extra_headers: dict = field(default_factory=dict)

    def __post_init__(self):
        parsed = _split_url(self.url)
        self.scheme = parsed["scheme"]
        self.host = parsed["host"]
        self.port = parsed["port"]
        self.base_path = parsed["base_path"]
        self.authority = parsed["authority"]
        self.url = parsed["url"]
        self.weight = max(1, _safe_int(self.weight, 1) or 1)
        self.priority = _safe_int(self.priority, 0) or 0
        self.timeout_seconds = max(0.1, _safe_float(self.timeout_seconds, 5.0) or 5.0)
        self.preserve_host = bool(self.preserve_host)
        self.enabled = bool(self.enabled)
        self.verify_tls = bool(self.verify_tls)
        self.name = _normalize_text(self.name) or self.authority or self.url
        self.extra_headers = _normalize_mapping(self.extra_headers)
        self.metrics = ProxyMetricsBucket(label=self.name)
        self._lock = threading.RLock()

    def _weighted_error_penalty(self):
        snap = self.metrics.snapshot()
        requests = max(1, int(snap["requests_total"]))
        error_rate = float(snap["errors_total"]) / float(requests)
        return error_rate

    def score(self):
        snap = self.metrics.snapshot()
        latency = float(snap["latency_ms"]["mean"] or 0.0)
        request_mean = float(snap["request_bytes"]["mean"] or 0.0)
        response_mean = float(snap["response_bytes"]["mean"] or 0.0)
        active = float(snap["active_requests"] or 0.0)
        errors = float(snap["errors_total"] or 0.0)
        requests = max(1.0, float(snap["requests_total"] or 0.0))
        error_rate = errors / requests
        base = latency + (active * 50.0) + (error_rate * 800.0)
        base += (request_mean / 1024.0) * 0.15
        base += (response_mean / 1024.0) * 0.10
        if self.weight > 1:
            base /= math.sqrt(float(self.weight))
        return base

    def open_connection(self):
        timeout = float(self.timeout_seconds or 5.0)
        if self.scheme == "https":
            if self.verify_tls:
                context = ssl.create_default_context()
            else:
                context = ssl._create_unverified_context()
            return http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=timeout,
                context=context,
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def build_forward_path(self, request_path, query_string="", strip_prefix=""):
        path = _normalize_text(request_path) or "/"
        if strip_prefix:
            path = _strip_prefix(path, strip_prefix)
        path = _join_paths(self.base_path, path)
        return _combine_query(path, query_string)

    def snapshot(self, include_metrics=True):
        data = {
            "name": self.name,
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "base_path": self.base_path,
            "authority": self.authority,
            "weight": self.weight,
            "priority": self.priority,
            "timeout_seconds": self.timeout_seconds,
            "preserve_host": self.preserve_host,
            "enabled": self.enabled,
            "verify_tls": self.verify_tls,
            "extra_headers": dict(self.extra_headers),
        }
        if include_metrics:
            metrics = self.metrics.snapshot()
            data["metrics"] = metrics
            data["score"] = round(self.score(), 6)
            data["current_load"] = metrics["current_load"]
        return data

    def __repr__(self):
        return f"ProxyTarget(name={self.name!r}, url={self.url!r})"


@dataclass
class ProxyRule:
    name: str = ""
    hosts: tuple = field(default_factory=tuple)
    host_contains: tuple = field(default_factory=tuple)
    host_regex: tuple = field(default_factory=tuple)
    path: str = ""
    path_prefix: str = ""
    path_contains: tuple = field(default_factory=tuple)
    path_regex: tuple = field(default_factory=tuple)
    methods: tuple = field(default_factory=tuple)
    header_equals: dict = field(default_factory=dict)
    header_contains: dict = field(default_factory=dict)
    header_regex: dict = field(default_factory=dict)
    balance: str = BALANCING_ROUND_ROBIN
    priority: int = 0
    strip_prefix: bool = False
    preserve_host: bool = True
    hash_key: str = ""
    default: bool = False
    order: int = 0
    targets: list = field(default_factory=list)

    def __post_init__(self):
        self.name = _normalize_text(self.name)
        self.hosts = _normalize_tuple(self.hosts, lower=True)
        self.host_contains = _normalize_tuple(self.host_contains, lower=True)
        self.host_regex = _normalize_tuple(self.host_regex)
        self.path = _normalize_text(self.path)
        self.path_prefix = _normalize_text(self.path_prefix)
        self.path_contains = _normalize_tuple(self.path_contains)
        self.path_regex = _normalize_tuple(self.path_regex)
        self.methods = _normalize_tuple(self.methods, lower=True)
        self.methods = tuple(method.upper() for method in self.methods)
        self.header_equals = _normalize_mapping(self.header_equals, lower_keys=True, lower_values=True)
        self.header_contains = _normalize_mapping(self.header_contains, lower_keys=True, lower_values=True)
        self.header_regex = _normalize_mapping(self.header_regex, lower_keys=True)
        self.balance = _normalize_text(self.balance) or BALANCING_ROUND_ROBIN
        self.balance = normalize_balance_mode(self.balance)
        self.priority = _safe_int(self.priority, 0) or 0
        self.strip_prefix = bool(self.strip_prefix)
        self.preserve_host = bool(self.preserve_host)
        self.hash_key = _normalize_text(self.hash_key)
        self.default = bool(self.default)
        self.order = _safe_int(self.order, 0) or 0
        self.metrics = ProxyMetricsBucket(label=self.name or "rule")
        self._lock = threading.RLock()
        self._rr_index = 0
        self._weighted_state = {}
        self._compiled_host_regex = tuple(re.compile(p, re.IGNORECASE) for p in self.host_regex if p)
        self._compiled_path_regex = tuple(re.compile(p) for p in self.path_regex if p)
        self._compiled_header_regex = {
            key: re.compile(pattern, re.IGNORECASE) for key, pattern in self.header_regex.items() if pattern
        }

    def add_target(self, target):
        row = normalize_target(target)
        with self._lock:
            self.targets.append(row)
        return row

    def _match_host(self, host):
        if not host:
            return False if (self.hosts or self.host_contains or self._compiled_host_regex) else True
        if self.hosts and not any(fnmatch.fnmatchcase(host, pattern) for pattern in self.hosts):
            return False
        if self.host_contains and not any(text in host for text in self.host_contains):
            return False
        if self._compiled_host_regex and not any(regex.search(host) for regex in self._compiled_host_regex):
            return False
        return True

    def _match_path(self, path):
        path_value = _normalize_text(path) or "/"
        if self.path and path_value != self.path:
            return False
        if self.path_prefix and not path_value.startswith(self.path_prefix):
            return False
        if self.path_contains and not any(text in path_value for text in self.path_contains):
            return False
        if self._compiled_path_regex and not any(regex.search(path_value) for regex in self._compiled_path_regex):
            return False
        return True

    def _match_headers(self, headers):
        header_map = {str(k).lower(): _normalize_lower(v) for k, v in (headers or {}).items()}
        for key, expected in self.header_equals.items():
            if header_map.get(key, "") != expected:
                return False
        for key, needle in self.header_contains.items():
            if needle not in header_map.get(key, ""):
                return False
        for key, pattern in self._compiled_header_regex.items():
            if not pattern.search(header_map.get(key, "")):
                return False
        return True

    def matches(self, request):
        if self.methods and request.method.upper() not in self.methods:
            return False
        host = _extract_request_host(request)
        if not self._match_host(host):
            return False
        if not self._match_path(_request_path(request)):
            return False
        if not self._match_headers(getattr(request, "headers", {}) or {}):
            return False
        return True

    def specificity(self):
        score = 0
        if self.default:
            score -= 10_000
        score += len(self.hosts) * 120
        score += len(self.host_contains) * 80
        score += len(self._compiled_host_regex) * 60
        if self.path:
            score += 180 + len(self.path)
        if self.path_prefix:
            score += 120 + len(self.path_prefix)
        score += len(self.path_contains) * 35
        score += len(self._compiled_path_regex) * 45
        score += len(self.header_equals) * 20
        score += len(self.header_contains) * 12
        score += len(self._compiled_header_regex) * 15
        score += len(self.methods) * 5
        return score

    def target_count(self):
        with self._lock:
            return len(self.targets)

    def enabled_targets(self):
        with self._lock:
            rows = [target for target in self.targets if getattr(target, "enabled", True)]
            return rows or list(self.targets)

    def _round_robin(self, targets):
        if not targets:
            return None
        with self._lock:
            index = self._rr_index % len(targets)
            self._rr_index = (self._rr_index + 1) % len(targets)
            return targets[index]

    def _weighted_round_robin(self, targets):
        if not targets:
            return None
        with self._lock:
            state = self._weighted_state
            total_weight = 0
            best = None
            best_score = None
            for target in targets:
                weight = max(1, int(getattr(target, "weight", 1) or 1))
                total_weight += weight
                current = int(state.get(id(target), 0)) + weight
                state[id(target)] = current
                if best is None or current > best_score:
                    best = target
                    best_score = current
            if best is None:
                return targets[0]
            state[id(best)] = int(best_score) - total_weight
            return best

    def _first_available(self, targets):
        if not targets:
            return None
        return targets[0]

    def _random(self, targets):
        if not targets:
            return None
        return random.choice(targets)

    def _least_connections(self, targets):
        if not targets:
            return None
        rows = []
        for index, target in enumerate(targets):
            snap = target.metrics.snapshot()
            rows.append((snap["active_requests"], target.score(), target.priority, index, target))
        rows.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        return rows[0][4]

    def _least_response_time(self, targets):
        if not targets:
            return None
        rows = []
        for index, target in enumerate(targets):
            snap = target.metrics.snapshot()
            rows.append((snap["latency_ms"]["mean"], snap["active_requests"], target.priority, index, target))
        rows.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        return rows[0][4]

    def _least_requests(self, targets):
        if not targets:
            return None
        rows = []
        for index, target in enumerate(targets):
            snap = target.metrics.snapshot()
            rows.append((snap["requests_total"], snap["active_requests"], target.priority, index, target))
        rows.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        return rows[0][4]

    def _least_bytes_in(self, targets):
        if not targets:
            return None
        rows = []
        for index, target in enumerate(targets):
            snap = target.metrics.snapshot()
            rows.append((snap["request_bytes"]["mean"], snap["active_requests"], target.priority, index, target))
        rows.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        return rows[0][4]

    def _least_bytes_out(self, targets):
        if not targets:
            return None
        rows = []
        for index, target in enumerate(targets):
            snap = target.metrics.snapshot()
            rows.append((snap["response_bytes"]["mean"], snap["active_requests"], target.priority, index, target))
        rows.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        return rows[0][4]

    def _hash_score(self, key, target):
        raw = f"{key}|{target.url}|{target.name}".encode("utf-8")
        digest = hashlib.sha256(raw).digest()
        value = int.from_bytes(digest[:8], byteorder="big", signed=False)
        weight = max(1, int(getattr(target, "weight", 1) or 1))
        return value / float(weight)

    def _consistent_hash(self, targets, key):
        if not targets:
            return None
        ranked = []
        for index, target in enumerate(targets):
            ranked.append((self._hash_score(key, target), target.priority, index, target))
        ranked.sort(key=lambda row: (row[0], row[1], row[2]))
        return ranked[0][3]

    def _power_of_two(self, targets):
        if not targets:
            return None
        if len(targets) == 1:
            return targets[0]
        sample = random.sample(targets, 2)
        sample.sort(key=lambda target: (target.score(), target.priority))
        return sample[0]

    def _best(self, targets):
        if not targets:
            return None
        rows = []
        for index, target in enumerate(targets):
            snap = target.metrics.snapshot()
            requests = max(1, int(snap["requests_total"]))
            error_rate = float(snap["errors_total"]) / float(requests)
            score = target.score()
            score += error_rate * 1000.0
            score += target.priority * 0.001
            rows.append((score, target.priority, index, target))
        rows.sort(key=lambda row: (row[0], row[1], row[2]))
        return rows[0][3]

    def choose_target(self, request, *, proxy=None):
        targets = self.enabled_targets()
        if not targets:
            return None
        mode = normalize_balance_mode(self.balance or BALANCING_ROUND_ROBIN)
        if mode == BALANCING_ROUND_ROBIN:
            target = self._round_robin(targets)
        elif mode == BALANCING_WEIGHTED_ROUND_ROBIN:
            target = self._weighted_round_robin(targets)
        elif mode == BALANCING_RANDOM:
            target = self._random(targets)
        elif mode == BALANCING_LEAST_CONNECTIONS:
            target = self._least_connections(targets)
        elif mode == BALANCING_LEAST_RESPONSE_TIME:
            target = self._least_response_time(targets)
        elif mode == BALANCING_LEAST_REQUESTS:
            target = self._least_requests(targets)
        elif mode == BALANCING_LEAST_BYTES_IN:
            target = self._least_bytes_in(targets)
        elif mode == BALANCING_LEAST_BYTES_OUT:
            target = self._least_bytes_out(targets)
        elif mode == BALANCING_IP_HASH:
            key = _extract_request_host(request) or getattr(request, "client", ("",))[0] or ""
            target = self._consistent_hash(targets, key)
        elif mode == BALANCING_CONSISTENT_HASH:
            header_key = self.hash_key
            if header_key:
                key = _header_value(request, header_key)
            else:
                key = _extract_request_host(request) or _request_path(request)
            target = self._consistent_hash(targets, key)
        elif mode == BALANCING_FIRST_AVAILABLE:
            target = self._first_available(targets)
        elif mode == BALANCING_POWER_OF_TWO_CHOICES:
            target = self._power_of_two(targets)
        elif mode == BALANCING_BEST:
            target = self._best(targets)
        else:
            target = self._round_robin(targets)
        if target is None and proxy is not None:
            return proxy._fallback_target(request)
        return target

    def snapshot(self, include_metrics=True):
        data = {
            "name": self.name,
            "hosts": list(self.hosts),
            "host_contains": list(self.host_contains),
            "host_regex": list(self.host_regex),
            "path": self.path,
            "path_prefix": self.path_prefix,
            "path_contains": list(self.path_contains),
            "path_regex": list(self.path_regex),
            "methods": list(self.methods),
            "header_equals": dict(self.header_equals),
            "header_contains": dict(self.header_contains),
            "header_regex": dict(self.header_regex),
            "balance": self.balance,
            "priority": self.priority,
            "strip_prefix": self.strip_prefix,
            "preserve_host": self.preserve_host,
            "hash_key": self.hash_key,
            "default": self.default,
            "order": self.order,
            "targets_total": self.target_count(),
            "specificity": self.specificity(),
        }
        if include_metrics:
            data["metrics"] = self.metrics.snapshot()
            with self._lock:
                targets = [target.snapshot(include_metrics=True) for target in self.targets]
            data["targets"] = targets
            data["best_target"] = None
            selected = self.choose_target(_dummy_request(self), proxy=None)
            if selected is not None:
                data["best_target"] = selected.snapshot(include_metrics=True)
        else:
            with self._lock:
                data["targets"] = [target.snapshot(include_metrics=False) for target in self.targets]
        return data

    def __repr__(self):
        return f"ProxyRule(name={self.name!r}, balance={self.balance!r}, targets={len(self.targets)})"


class ProxyRouteBuilder:
    def __init__(self, proxy, **kwargs):
        self.proxy = proxy
        self._config = {
            "name": kwargs.pop("name", ""),
            "hosts": tuple(kwargs.pop("hosts", ())),
            "host_contains": tuple(kwargs.pop("host_contains", ())),
            "host_regex": tuple(kwargs.pop("host_regex", ())),
            "path": kwargs.pop("path", ""),
            "path_prefix": kwargs.pop("path_prefix", ""),
            "path_contains": tuple(kwargs.pop("path_contains", ())),
            "path_regex": tuple(kwargs.pop("path_regex", ())),
            "methods": tuple(kwargs.pop("methods", ())),
            "header_equals": dict(kwargs.pop("header_equals", {})),
            "header_contains": dict(kwargs.pop("header_contains", {})),
            "header_regex": dict(kwargs.pop("header_regex", {})),
            "balance": kwargs.pop("balance", BALANCING_ROUND_ROBIN),
            "priority": kwargs.pop("priority", 0),
            "strip_prefix": kwargs.pop("strip_prefix", False),
            "preserve_host": kwargs.pop("preserve_host", True),
            "hash_key": kwargs.pop("hash_key", ""),
            "default": kwargs.pop("default", False),
        }
        self._targets = list(kwargs.pop("targets", ()) or [])
        self._built = None
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"unexpected route options: {unexpected}")

    def _rule_object(self):
        if self._built is None:
            self._built = ProxyRule(**self._config)
        return self._built

    def _apply(self):
        rule = self._rule_object()
        if rule not in self.proxy._rules:
            self.proxy.add_rule(rule)
        for target in self._targets:
            rule.add_target(target)
        self._targets = []
        return rule

    def host(self, value):
        self._config["hosts"] = (_normalize_text(value),)
        self._config["host_contains"] = ()
        self._config["host_regex"] = ()
        if self._built is not None:
            self._built.hosts = _normalize_tuple(self._config["hosts"], lower=True)
            self._built.host_contains = ()
            self._built.host_regex = ()
            self._sync_rule_from_config()
        return self

    def vhost(self, *hosts):
        self._config["hosts"] = tuple([str(host) for host in hosts])
        self._config["host_contains"] = ()
        self._config["host_regex"] = ()
        if self._built is not None:
            self._built.hosts = _normalize_tuple(self._config["hosts"], lower=True)
            self._built.host_contains = ()
            self._built.host_regex = ()
            self._sync_rule_from_config()
        return self

    def host_contains(self, value):
        self._config["host_contains"] = tuple(list(self._config.get("host_contains", ())) + [_normalize_text(value)])
        if self._built is not None:
            self._sync_rule_from_config()
        return self

    def host_regex(self, value):
        self._config["host_regex"] = tuple(list(self._config.get("host_regex", ())) + [_normalize_text(value)])
        if self._built is not None:
            self._sync_rule_from_config()
        return self

    def location(self, value, *, exact=False, contains=False, regex=False):
        text = _normalize_text(value)
        if regex:
            self._config["path"] = ""
            self._config["path_prefix"] = ""
            self._config["path_contains"] = ()
            self._config["path_regex"] = tuple(list(self._config.get("path_regex", ())) + [text])
        elif contains:
            self._config["path"] = ""
            self._config["path_prefix"] = ""
            self._config["path_regex"] = ()
            self._config["path_contains"] = tuple(list(self._config.get("path_contains", ())) + [text])
        elif exact:
            self._config["path_prefix"] = ""
            self._config["path_contains"] = ()
            self._config["path_regex"] = ()
            self._config["path"] = text
        else:
            self._config["path"] = ""
            self._config["path_contains"] = ()
            self._config["path_regex"] = ()
            self._config["path_prefix"] = text
        if self._built is not None:
            self._sync_rule_from_config()
        return self

    def path(self, value):
        return self.location(value, exact=True)

    def path_prefix(self, value):
        return self.location(value, exact=False)

    def path_contains(self, value):
        return self.location(value, contains=True)

    def path_regex(self, value):
        return self.location(value, regex=True)

    def header(self, name, *, equals=None, contains=None, regex=None):
        key = _normalize_text(name).lower()
        if equals is not None:
            self._config.setdefault("header_equals", {})[key] = _normalize_text(equals)
        if contains is not None:
            self._config.setdefault("header_contains", {})[key] = _normalize_text(contains)
        if regex is not None:
            self._config.setdefault("header_regex", {})[key] = _normalize_text(regex)
        if self._built is not None:
            self._sync_rule_from_config()
        return self

    def header_equals(self, name, value):
        return self.header(name, equals=value)

    def header_contains(self, name, value):
        return self.header(name, contains=value)

    def balance(self, value):
        self._config["balance"] = normalize_balance_mode(value)
        if self._built is not None:
            self._built.balance = normalize_balance_mode(value)
        return self

    def priority(self, value):
        self._config["priority"] = _safe_int(value, 0) or 0
        if self._built is not None:
            self._built.priority = self._config["priority"]
        return self

    def strip_prefix(self, value=True):
        self._config["strip_prefix"] = bool(value)
        if self._built is not None:
            self._built.strip_prefix = self._config["strip_prefix"]
        return self

    def preserve_host(self, value=True):
        self._config["preserve_host"] = bool(value)
        if self._built is not None:
            self._built.preserve_host = self._config["preserve_host"]
        return self

    def hash_key(self, value):
        self._config["hash_key"] = _normalize_text(value)
        if self._built is not None:
            self._built.hash_key = self._config["hash_key"]
        return self

    def default(self, value=True):
        self._config["default"] = bool(value)
        if self._built is not None:
            self._built.default = self._config["default"]
        return self

    def upstream(self, target, **kwargs):
        row = normalize_target(target, **kwargs)
        self._targets.append(row)
        rule = self._apply()
        return self

    def to(self, target, **kwargs):
        return self.upstream(target, **kwargs)

    def build(self):
        rule = self._rule_object()
        if not (self._targets or rule.targets):
            raise ValueError("a proxy rule requires at least one upstream target")
        rule = self._apply()
        return rule

    def _sync_rule_from_config(self):
        if self._built is None:
            return
        rule = self._built
        rule.name = _normalize_text(self._config["name"])
        rule.hosts = _normalize_tuple(self._config["hosts"], lower=True)
        rule.host_contains = _normalize_tuple(self._config["host_contains"], lower=True)
        rule.host_regex = _normalize_tuple(self._config["host_regex"])
        rule.path = _normalize_text(self._config["path"])
        rule.path_prefix = _normalize_text(self._config["path_prefix"])
        rule.path_contains = _normalize_tuple(self._config["path_contains"])
        rule.path_regex = _normalize_tuple(self._config["path_regex"])
        rule.methods = tuple(str(method).upper() for method in _normalize_tuple(self._config["methods"], lower=False))
        rule.header_equals = _normalize_mapping(self._config["header_equals"], lower_keys=True, lower_values=True)
        rule.header_contains = _normalize_mapping(self._config["header_contains"], lower_keys=True, lower_values=True)
        rule.header_regex = _normalize_mapping(self._config["header_regex"], lower_keys=True)
        rule.balance = normalize_balance_mode(self._config["balance"])
        rule.priority = _safe_int(self._config["priority"], 0) or 0
        rule.strip_prefix = bool(self._config["strip_prefix"])
        rule.preserve_host = bool(self._config["preserve_host"])
        rule.hash_key = _normalize_text(self._config["hash_key"])
        rule.default = bool(self._config["default"])
        rule._compiled_host_regex = tuple(re.compile(p, re.IGNORECASE) for p in rule.host_regex if p)
        rule._compiled_path_regex = tuple(re.compile(p) for p in rule.path_regex if p)
        rule._compiled_header_regex = {
            key: re.compile(pattern, re.IGNORECASE) for key, pattern in rule.header_regex.items() if pattern
        }


class ProxyI:
    def __init__(
        self,
        *,
        name="proxyi",
        dashboard_path=DEFAULT_DASHBOARD_PATH,
        metrics_path=DEFAULT_METRICS_PATH,
        metrics_stream_path=DEFAULT_STREAM_PATH,
        default_balance=BALANCING_ROUND_ROBIN,
        max_request_body_bytes=DEFAULT_MAX_REQUEST_BODY_BYTES,
    ):
        self.name = _normalize_text(name) or "proxyi"
        self.dashboard_path = _normalize_text(dashboard_path) or DEFAULT_DASHBOARD_PATH
        self.metrics_path = _normalize_text(metrics_path) or DEFAULT_METRICS_PATH
        self.metrics_stream_path = _normalize_text(metrics_stream_path) or DEFAULT_STREAM_PATH
        self.default_balance = normalize_balance_mode(default_balance)
        self.max_request_body_bytes = max(0, _safe_int(max_request_body_bytes, DEFAULT_MAX_REQUEST_BODY_BYTES))
        self.started_at = time.time()
        self.metrics = ProxyMetricsBucket(label=self.name)
        self._rules = []
        self._default_rule = None
        self._lock = threading.RLock()
        self._order = 0
        self._app = None

    def _next_order(self):
        with self._lock:
            self._order += 1
            return self._order

    def routes(self):
        with self._lock:
            return tuple(self._rules)

    def _rule_matches(self, rule, request):
        return rule.matches(request)

    def _resolve_rules(self, request):
        with self._lock:
            rows = [rule for rule in self._rules if self._rule_matches(rule, request)]
            if rows:
                rows.sort(key=lambda row: (-int(row.priority), -int(row.specificity()), int(row.order)))
                return rows
            if self._default_rule is not None:
                return [self._default_rule]
        return []

    def _fallback_target(self, request):
        with self._lock:
            if self._default_rule is None:
                return None
            return self._default_rule.choose_target(request, proxy=None)

    def add_rule(self, rule):
        if not isinstance(rule, ProxyRule):
            raise TypeError("add_rule expects a ProxyRule instance")
        with self._lock:
            if rule.order <= 0:
                rule.order = self._next_order()
            if rule.default:
                self._default_rule = rule
            if rule not in self._rules:
                self._rules.append(rule)
        return rule

    def route(self, **kwargs):
        return ProxyRouteBuilder(self, **kwargs)

    def vhost(self, *hosts, **kwargs):
        kwargs["hosts"] = tuple(hosts)
        return self.route(**kwargs)

    def location(self, path, **kwargs):
        kwargs["path_prefix"] = path
        return self.route(**kwargs)

    def default(self, *targets, **kwargs):
        kwargs["default"] = True
        builder = self.route(**kwargs)
        for target in targets:
            builder.upstream(target)
        return builder

    def install(self, app=None, *, metrics_path=None, stream_path=None, dashboard_path=None):
        return install_proxyi(
            app or self._app,
            proxy=self,
            metrics_path=metrics_path or self.metrics_path,
            stream_path=stream_path or self.metrics_stream_path,
            dashboard_path=dashboard_path or self.dashboard_path,
        )

    def _record_rule_start(self, rule, request_size):
        if rule is not None:
            rule.metrics.begin_request(request_size)

    def _record_rule_finish(self, rule, status, response_size, latency_ms, error=None):
        if rule is not None:
            rule.metrics.finish_request(status, response_size=response_size, latency_ms=latency_ms, error=error)

    def _record_target_start(self, target, request_size):
        if target is not None:
            target.metrics.begin_request(request_size)
            target.metrics.mark_selected()

    def _record_target_finish(self, target, status, response_size, latency_ms, error=None):
        if target is not None:
            target.metrics.finish_request(status, response_size=response_size, latency_ms=latency_ms, error=error)

    def _forward_request(self, request, rule, target):
        body = _request_body_bytes(request)
        query_string = _request_query_string(request)
        request_path = _request_path(request)
        strip_prefix = rule.path_prefix if (rule and rule.strip_prefix) else ""
        forward_path = target.build_forward_path(
            request_path,
            query_string=query_string,
            strip_prefix=strip_prefix,
        )
        headers = _clean_request_headers(getattr(request, "headers", {}) or {})
        headers.update(target.extra_headers)
        headers["X-Forwarded-For"] = self._forwarded_for_value(request)
        headers["X-Forwarded-Host"] = _header_value(request, "host") or _extract_request_host(request)
        headers["X-Forwarded-Proto"] = "https" if getattr(request, "tls", {}).get("enabled") else "http"
        headers["X-Forwarded-Port"] = str(target.port)
        headers["X-ProxyI-Rule"] = rule.name or rule.path_prefix or rule.path or "default"
        headers["X-ProxyI-Target"] = target.name
        if target.preserve_host and _header_value(request, "host"):
            headers["Host"] = _header_value(request, "host")
        else:
            headers["Host"] = target.authority

        conn = target.open_connection()
        started = time.time()
        try:
            conn.request(request.method, forward_path, body=body, headers=headers)
            upstream = conn.getresponse()
            raw_body = upstream.read()
            status = int(getattr(upstream, "status", 502) or 502)
            reason = _normalize_text(getattr(upstream, "reason", "") or "")
            response_headers = _clean_response_headers(dict(upstream.getheaders()))
            if request.method.upper() == "HEAD":
                raw_body = b""
            if "Content-Length" not in response_headers:
                response_headers["Content-Length"] = str(len(raw_body))
            response_headers.setdefault("X-ProxyI-Rule", rule.name or rule.path_prefix or rule.path or "default")
            response_headers.setdefault("X-ProxyI-Target", target.name)
            response_headers.setdefault("X-ProxyI-Upstream", target.url)
            elapsed_ms = (time.time() - started) * 1000.0
            return Response(status=status, body=raw_body, headers=response_headers, reason=reason)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _forwarded_for_value(self, request):
        client = getattr(request, "client", None)
        ip = ""
        if client and isinstance(client, (list, tuple)) and client:
            ip = _normalize_text(client[0])
        prior = _header_value(request, "x-forwarded-for")
        if prior and ip:
            return f"{prior}, {ip}"
        return ip or prior

    def dispatch(self, request):
        path = _request_path(request)
        method = _normalize_text(getattr(request, "method", "GET")).upper() or "GET"
        if path == self.metrics_path:
            if method != "GET":
                return Response.text("Method Not Allowed", status=405, headers={"Allow": "GET"})
            return self.response_snapshot()
        if path == self.metrics_stream_path:
            if method != "GET":
                return Response.text("Method Not Allowed", status=405, headers={"Allow": "GET"})
            interval = getattr(request, "query", {}).get("interval", "1.0")
            limit_text = getattr(request, "query", {}).get("limit", "")
            follow = getattr(request, "query", {}).get("follow", "")
            if limit_text:
                parsed = _safe_int(limit_text, DEFAULT_STREAM_POINTS)
                max_points = None if parsed <= 0 else parsed
            else:
                max_points = None if _is_truthy(follow) else DEFAULT_STREAM_POINTS
            return self.response_stream(interval_seconds=interval, max_points=max_points)
        if path == self.dashboard_path:
            if method != "GET":
                return Response.text("Method Not Allowed", status=405, headers={"Allow": "GET"})
            return self.response_dashboard()

        request_size = len(_request_body_bytes(request))
        started = time.time()
        self.metrics.begin_request(request_size)

        rule = None
        target = None
        response = None
        error = None
        try:
            matches = self._resolve_rules(request)
            rule = matches[0] if matches else None
            if rule is None:
                response = Response.text("Not Found", status=404)
                return response
            rule.metrics.mark_selected()
            self._record_rule_start(rule, request_size)
            target = rule.choose_target(request, proxy=self)
            if target is None:
                response = Response.text("Bad Gateway", status=502)
                error = "no upstream target"
                return response
            self._record_target_start(target, request_size)
            response = self._forward_request(request, rule, target)
            return response
        except Exception as exc:
            error = exc
            response = Response.text("Bad Gateway", status=502)
            return response
        finally:
            elapsed_ms = (time.time() - started) * 1000.0
            status = response.status if response is not None else 502
            body_size = len(response.body) if response is not None and not response.is_stream else 0
            self.metrics.finish_request(status, response_size=body_size, latency_ms=elapsed_ms, error=error)
            self._record_rule_finish(rule, status, body_size, elapsed_ms, error=error)
            self._record_target_finish(target, status, body_size, elapsed_ms, error=error)

    def metrics_snapshot(self):
        now = time.time()
        summary = self.metrics.snapshot()
        with self._lock:
            rules = [rule.snapshot(include_metrics=True) for rule in self._rules]
            targets = []
            for rule in self._rules:
                for target in rule.targets:
                    targets.append(target.snapshot(include_metrics=True))
            total_targets = len(targets)
        best_target = None
        if targets:
            ranked = sorted(targets, key=lambda row: (row.get("score", float("inf")), row.get("priority", 0), row.get("name", "")))
            best_target = ranked[0]
        return {
            "name": self.name,
            "started_at_unix": self.started_at,
            "uptime_seconds": round(now - self.started_at, 3),
            "dashboard_path": self.dashboard_path,
            "metrics_path": self.metrics_path,
            "metrics_stream_path": self.metrics_stream_path,
            "default_balance": self.default_balance,
            "supported_balancing": list(SUPPORTED_BALANCING_STRATEGIES),
            "rules_total": len(rules),
            "targets_total": total_targets,
            "rules": rules,
            "targets": targets,
            "best_target": best_target,
            "summary": summary,
        }

    def snapshot(self):
        return self.metrics_snapshot()

    def describe(self):
        return self.snapshot()

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
            "X-Content-Type-Options": "nosniff",
        }
        return Response.stream(
            self.stream_chunks(interval_seconds=interval_seconds, max_points=max_points),
            content_type="application/x-ndjson; charset=utf-8",
            headers=headers,
        )

    def render_dashboard_html(self, snapshot=None):
        data = snapshot or self.snapshot()
        summary = data.get("summary") or {}
        rules = list(data.get("rules") or [])
        targets = list(data.get("targets") or [])
        best_target = data.get("best_target") or {}

        def render_card(label, value, hint=""):
            return f"""
            <article class="card">
              <div class="k">{html_escape(str(label))}</div>
              <div class="v">{html_escape(str(value))}</div>
              <div class="hint">{html_escape(str(hint))}</div>
            </article>
            """

        def fmt_stat(stat):
            stat = stat or {}
            return (
                f"n={stat.get('count', 0)} mean={stat.get('mean', 0)} "
                f"std={stat.get('stddev', 0)} ci95=±{stat.get('uncertainty_95', 0)}"
            )

        rule_rows = []
        for row in rules:
            match_bits = []
            if row.get("hosts"):
                match_bits.append(f"hosts={', '.join(row.get('hosts') or [])}")
            if row.get("path"):
                match_bits.append(f"path={row.get('path')}")
            if row.get("path_prefix"):
                match_bits.append(f"prefix={row.get('path_prefix')}")
            if row.get("header_equals"):
                match_bits.append(f"headers={json.dumps(row.get('header_equals'), ensure_ascii=False)}")
            targets_preview = ", ".join(target.get("name", "") for target in row.get("targets", [])) or "-"
            rule_rows.append(
                "<tr>"
                f"<td data-label=\"Name\"><code>{html_escape(str(row.get('name') or '-'))}</code></td>"
                f"<td data-label=\"Match\">{html_escape('; '.join(match_bits) or '-')}</td>"
                f"<td data-label=\"Balance\">{html_escape(str(row.get('balance') or '-'))}</td>"
                f"<td data-label=\"Targets\">{html_escape(targets_preview)}</td>"
                f"<td data-label=\"Metrics\">{html_escape(str(row.get('metrics', {}).get('requests_total', 0)))}</td>"
                "</tr>"
            )

        target_rows = []
        for row in targets:
            metrics = row.get("metrics") or {}
            target_rows.append(
                "<tr>"
                f"<td data-label=\"Target\"><code>{html_escape(str(row.get('name') or '-'))}</code></td>"
                f"<td data-label=\"URL\"><code>{html_escape(str(row.get('url') or '-'))}</code></td>"
                f"<td data-label=\"Load\">{html_escape(str(metrics.get('current_load', 0)))}</td>"
                f"<td data-label=\"Latency\">{html_escape(str(metrics.get('latency_ms', {}).get('mean', 0)))}</td>"
                f"<td data-label=\"Req size\">{html_escape(str(metrics.get('request_bytes', {}).get('mean', 0)))}</td>"
                f"<td data-label=\"Resp size\">{html_escape(str(metrics.get('response_bytes', {}).get('mean', 0)))}</td>"
                f"<td data-label=\"Errors\">{html_escape(str(metrics.get('errors_total', 0)))}</td>"
                f"<td data-label=\"Score\">{html_escape(str(row.get('score', 0)))}</td>"
                "</tr>"
            )

        stats_rows = [
            ("requests_total", summary.get("requests_total", 0)),
            ("responses_total", summary.get("responses_total", 0)),
            ("errors_total", summary.get("errors_total", 0)),
            ("active_requests", summary.get("active_requests", 0)),
            ("bytes_in_total", summary.get("bytes_in_total", 0)),
            ("bytes_out_total", summary.get("bytes_out_total", 0)),
        ]

        return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html_escape(self.name)} metrics</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f2;
      --panel: #ffffff;
      --ink: #111827;
      --muted: #5b6472;
      --line: #d7d9df;
      --brand: #0f766e;
      --accent: #ca8a04;
      --shadow: 0 18px 40px rgba(17, 24, 39, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 20% 20%, rgba(15,118,110,0.12), transparent 28%),
        radial-gradient(circle at 80% 0%, rgba(202,138,4,0.12), transparent 22%),
        linear-gradient(180deg, #fff, var(--bg));
    }}
    a {{ color: var(--brand); text-decoration: none; }}
    .wrap {{ max-width: 1320px; margin: 0 auto; padding: 28px 18px 56px; }}
    .hero {{
      background: linear-gradient(135deg, rgba(15,118,110,0.12), rgba(202,138,4,0.08));
      border: 1px solid var(--line);
      border-radius: 26px;
      box-shadow: var(--shadow);
      padding: 24px;
      margin-bottom: 20px;
    }}
    .hero h1 {{
      margin: 0;
      font-size: clamp(2rem, 4vw, 3.3rem);
      letter-spacing: -0.04em;
      line-height: 1.03;
    }}
    .hero p {{ margin: 10px 0 0; color: var(--muted); max-width: 72ch; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 18px 0 24px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 8px 18px rgba(17, 24, 39, 0.05);
    }}
    .k {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }}
    .v {{ margin-top: 8px; font-size: 1.7rem; font-weight: 800; letter-spacing: -0.03em; }}
    .hint {{ margin-top: 8px; color: var(--muted); font-size: 0.88rem; line-height: 1.4; }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      overflow: hidden;
      margin-bottom: 18px;
    }}
    .section h2 {{ margin: 0; padding: 18px 20px 8px; font-size: 1.15rem; }}
    .section p.lead {{ margin: 0; padding: 0 20px 16px; color: var(--muted); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; vertical-align: top; padding: 12px 20px; border-top: 1px solid var(--line); word-break: break-word; }}
    th {{
      background: rgba(15, 118, 110, 0.06);
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background: rgba(17, 24, 39, 0.06);
      padding: 0.12em 0.35em;
      border-radius: 0.35rem;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(202, 138, 4, 0.12);
      color: #7c4a00;
      font-weight: 700;
      font-size: 0.88rem;
    }}
    @media (max-width: 960px) {{
      .grid {{ grid-template-columns: 1fr; }}
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-top: 1px solid var(--line); }}
      td {{ border-top: 0; }}
      td::before {{
        content: attr(data-label);
        display: block;
        margin-bottom: 4px;
        color: var(--muted);
        font-size: 0.76rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="badge">ProxyI metrics area</div>
      <h1>{html_escape(self.name)}</h1>
      <p>Reglas tipo vhost/location, balanceo adaptativo y estadisticas por destino. Los datos que aparecen aqui salen del motor activo y pueden usarse para elegir el mejor upstream por latencia, carga o estabilidad.</p>
    </header>

    <section class="grid" aria-label="Resumen">
      {render_card("requests", summary.get("requests_total", 0), "Requests procesadas por el proxy")}
      {render_card("responses", summary.get("responses_total", 0), "Respuestas emitidas al cliente")}
      {render_card("errors", summary.get("errors_total", 0), "Errores de transporte o upstream")}
    </section>

    <section class="grid" aria-label="Cargas">
      {render_card("active", summary.get("active_requests", 0), "Carga concurrente actual")}
      {render_card("bytes in", summary.get("bytes_in_total", 0), "Tamano acumulado de peticiones")}
      {render_card("bytes out", summary.get("bytes_out_total", 0), "Tamano acumulado de respuestas")}
    </section>

    <section class="section">
      <h2>Estado operacional</h2>
      <p class="lead">El panel muestra la distribucion actual y una recomendacion de destino basada en la metrica agregada.</p>
      <table>
        <tbody>
          <tr><th>Campo</th><th>Valor</th></tr>
          <tr><td data-label="Campo">Balance por defecto</td><td data-label="Valor"><code>{html_escape(str(data.get("default_balance") or "-"))}</code></td></tr>
          <tr><td data-label="Campo">Rutas</td><td data-label="Valor">{html_escape(str(data.get("rules_total", 0)))}</td></tr>
          <tr><td data-label="Campo">Destinos</td><td data-label="Valor">{html_escape(str(data.get("targets_total", 0)))}</td></tr>
          <tr><td data-label="Campo">Mejor destino</td><td data-label="Valor"><code>{html_escape(str((best_target or {}).get("name") or "-"))}</code></td></tr>
        </tbody>
      </table>
    </section>

    <section class="section">
      <h2>Metricas base</h2>
      <p class="lead">Media, desviacion estandar, incertidumbre y rango de confianza al 95% por destino.</p>
      <table>
        <thead>
          <tr>
            <th>Metrica</th>
            <th>Valor</th>
          </tr>
        </thead>
        <tbody>
          {''.join(
            f'<tr><td data-label="Metrica">{html_escape(str(label))}</td><td data-label="Valor">{html_escape(str(value))}</td></tr>'
            for label, value in stats_rows
          )}
        </tbody>
      </table>
      <details style="border-top: 1px solid var(--line); padding: 0 20px 20px;">
        <summary style="cursor: pointer; padding: 14px 0; color: var(--brand); font-weight: 700;">Ver estadisticas detalladas del mejor destino</summary>
        <pre style="margin: 0; padding: 16px; border-radius: 16px; background: #0b1020; color: #e2e8f0; overflow: auto;">{html_escape(json.dumps(best_target, ensure_ascii=False, indent=2))}</pre>
      </details>
    </section>

    <section class="section">
      <h2>Reglas</h2>
      <p class="lead">Las reglas se ordenan por prioridad y especificidad para comportarse como un vhost de proxy.</p>
      <table>
        <thead>
          <tr>
            <th>Nombre</th>
            <th>Match</th>
            <th>Balance</th>
            <th>Targets</th>
            <th>Requests</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rule_rows) or '<tr><td colspan="5" class="muted" style="padding:16px 20px;">No hay reglas registradas.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section class="section">
      <h2>Destinos</h2>
      <p class="lead">Cada target conserva sus propias medias, desviacion, incertidumbre y score para permitir balanceo adaptativo.</p>
      <table>
        <thead>
          <tr>
            <th>Target</th>
            <th>URL</th>
            <th>Load</th>
            <th>Latency</th>
            <th>Request size</th>
            <th>Response size</th>
            <th>Errors</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {''.join(target_rows) or '<tr><td colspan="8" class="muted" style="padding:16px 20px;">No hay destinos registrados.</td></tr>'}
        </tbody>
      </table>
    </section>
  </div>
</body>
</html>"""

    def response_dashboard(self):
        return Response.html(self.render_dashboard_html())

    def close(self):
        self._rules = []
        self._default_rule = None


def normalize_balance_mode(value):
    text = _normalize_text(value).lower().replace("-", "_")
    mapping = {
        "rr": BALANCING_ROUND_ROBIN,
        "roundrobin": BALANCING_ROUND_ROBIN,
        "weightedrr": BALANCING_WEIGHTED_ROUND_ROBIN,
        "wrr": BALANCING_WEIGHTED_ROUND_ROBIN,
        "least_conn": BALANCING_LEAST_CONNECTIONS,
        "leastconnections": BALANCING_LEAST_CONNECTIONS,
        "least_response": BALANCING_LEAST_RESPONSE_TIME,
        "least_resptime": BALANCING_LEAST_RESPONSE_TIME,
        "leastreq": BALANCING_LEAST_REQUESTS,
        "least_bytes_in": BALANCING_LEAST_BYTES_IN,
        "least_bytes_out": BALANCING_LEAST_BYTES_OUT,
        "consistenthash": BALANCING_CONSISTENT_HASH,
        "first": BALANCING_FIRST_AVAILABLE,
        "power_of_two": BALANCING_POWER_OF_TWO_CHOICES,
        "best_effort": BALANCING_BEST,
    }
    if text in SUPPORTED_BALANCING_STRATEGIES:
        return text
    if text in mapping:
        return mapping[text]
    if text.replace("_", "") in mapping:
        return mapping[text.replace("_", "")]
    return BALANCING_ROUND_ROBIN


def normalize_target(target, **kwargs):
    if isinstance(target, ProxyTarget):
        row = target
    elif isinstance(target, dict):
        data = dict(target)
        data.update(kwargs)
        if "url" not in data:
            raise ValueError("target mapping requires a url")
        row = ProxyTarget(**data)
    else:
        url = target
        if "url" in kwargs:
            url = kwargs.pop("url")
        row = ProxyTarget(url=url, **kwargs)
    return row


def _dummy_request(rule):
    class _Req:
        method = "GET"
        path = rule.path_prefix or rule.path or "/"
        query_string = ""
        headers = {}
        client = ("127.0.0.1", 0)
        tls = {}

    return _Req()


def install_proxyi(
    app,
    *,
    proxy=None,
    metrics_path=DEFAULT_METRICS_PATH,
    stream_path=DEFAULT_STREAM_PATH,
    dashboard_path=DEFAULT_DASHBOARD_PATH,
):
    proxy = proxy or ProxyI(
        metrics_path=metrics_path,
        metrics_stream_path=stream_path,
        dashboard_path=dashboard_path,
    )
    if app is None:
        return proxy

    app.proxyi = proxy
    proxy._app = app

    @app.api(metrics_path, methods=("GET",))
    def _proxy_metrics(_request):
        return proxy.response_snapshot()

    @app.api(stream_path, methods=("GET",))
    def _proxy_metrics_stream(request):
        interval = request.query.get("interval", "1.0")
        limit_text = request.query.get("limit", "")
        follow = request.query.get("follow", "")
        if limit_text:
            parsed = _safe_int(limit_text, DEFAULT_STREAM_POINTS)
            max_points = None if parsed <= 0 else parsed
        else:
            max_points = None if _is_truthy(follow) else DEFAULT_STREAM_POINTS
        return proxy.response_stream(interval_seconds=interval, max_points=max_points)

    @app.view(dashboard_path, methods=("GET",))
    def _proxy_dashboard(_request):
        return proxy.response_dashboard()

    return proxy


proxyi = ProxyI


__all__ = [
    "ProxyI",
    "proxyi",
    "ProxyRule",
    "ProxyRouteBuilder",
    "ProxyTarget",
    "ProxyMetricsBucket",
    "RunningStats",
    "install_proxyi",
    "normalize_balance_mode",
    "normalize_target",
    "SUPPORTED_BALANCING_STRATEGIES",
    "BALANCING_ROUND_ROBIN",
    "BALANCING_WEIGHTED_ROUND_ROBIN",
    "BALANCING_RANDOM",
    "BALANCING_LEAST_CONNECTIONS",
    "BALANCING_LEAST_RESPONSE_TIME",
    "BALANCING_LEAST_REQUESTS",
    "BALANCING_LEAST_BYTES_IN",
    "BALANCING_LEAST_BYTES_OUT",
    "BALANCING_IP_HASH",
    "BALANCING_CONSISTENT_HASH",
    "BALANCING_FIRST_AVAILABLE",
    "BALANCING_POWER_OF_TWO_CHOICES",
    "BALANCING_BEST",
]

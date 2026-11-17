"""Constants, header helpers and upstream I/O shared by the ProxyI modules."""

from __future__ import annotations

import http.client
import re
import time
from urllib.parse import urlsplit

from ..headers import header_value_text

DEFAULT_STREAM_POINTS = 5

DEFAULT_MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024

#: Largest buffered upstream body; bigger ones are answered 502.
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: Total time allowed to read a buffered upstream body.
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 30.0

_RESPONSE_READ_CHUNK = 64 * 1024

_ENCODED_DOT_SEGMENT_RE = re.compile(r"%2e", re.IGNORECASE)

_ENCODED_SEPARATOR_RE = re.compile(r"%2f|%5c", re.IGNORECASE)

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
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

_HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

_FORWARDED_REQUEST_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
}

_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}

_REDACTED_HEADER_VALUE = "[REDACTED]"

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

def _redact_headers(headers):
    redacted = {}
    for key, value in (headers or {}).items():
        name = _normalize_text(key)
        normalized = name.lower().replace("_", "-")
        is_sensitive = normalized in _SENSITIVE_HEADER_NAMES or any(
            marker in normalized
            for marker in ("api-key", "password", "secret", "token")
        )
        redacted[name] = _REDACTED_HEADER_VALUE if is_sensitive else value
    return redacted

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
    host = _header_value(request, "host")
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
    if not _path_has_prefix(raw_path, raw_prefix):
        return raw_path
    if raw_prefix != "/":
        raw_prefix = raw_prefix.rstrip("/")
    remainder = raw_path[len(raw_prefix) :]
    if not remainder:
        return "/"
    if not remainder.startswith("/"):
        remainder = "/" + remainder
    return remainder

def _path_has_prefix(path, prefix):
    raw_path = _normalize_text(path) or "/"
    raw_prefix = _normalize_text(prefix)
    if not raw_prefix:
        return True
    if raw_prefix == "/":
        return raw_path.startswith("/")
    raw_prefix = raw_prefix.rstrip("/")
    return raw_path == raw_prefix or raw_path.startswith(raw_prefix + "/")

def _has_dot_segments(path):
    """True when ``path`` has a ``.`` or ``..`` segment, literal or percent-encoded.

    Segment parameters are ignored (``..;x`` is ``..``).

    Encoded dots (``%2e``) and separators (``%2f``, ``%5c``) are decoded first,
    so ``/a/%2e%2e%2fadmin`` counts as traversal too.
    """
    text = _normalize_text(path)
    decoded = _ENCODED_SEPARATOR_RE.sub("/", _ENCODED_DOT_SEGMENT_RE.sub(".", text))
    decoded = decoded.replace("\\", "/")
    # Some servers drop ";params" from a segment, so "..;x" counts as "..".
    return any(segment.split(";", 1)[0] in {".", ".."} for segment in decoded.split("/"))

class _UpstreamResponseTooLarge(Exception):
    pass

class _UpstreamResponseTimeout(Exception):
    pass

def _collect_upstream_headers(pairs):
    """Header pairs as a dict; a repeated name keeps every value in a list."""
    headers = {}
    lowered = {}
    for name, value in pairs or ():
        key = lowered.setdefault(str(name).lower(), str(name))
        if key in headers:
            existing = headers[key]
            headers[key] = (existing if isinstance(existing, list) else [existing]) + [value]
        else:
            headers[key] = value
    return headers

def _should_stream_upstream(upstream, headers):
    lowered = {str(name).lower(): header_value_text(value) for name, value in headers.items()}
    if "text/event-stream" in lowered.get("content-type", "").lower():
        return True
    if "chunked" in lowered.get("transfer-encoding", "").lower():
        return True
    return getattr(upstream, "length", None) is None and "content-length" not in lowered

def _iter_upstream_body(upstream, conn):
    """Yield the upstream body as it arrives, closing the connection at the end."""
    try:
        reader = getattr(upstream, "read1", None) or upstream.read
        while True:
            chunk = reader(_RESPONSE_READ_CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            conn.close()
        except OSError:
            pass  # Closing an upstream that already dropped is harmless.

def _read_upstream_body(upstream, *, max_bytes, deadline):
    declared = getattr(upstream, "length", None)
    if declared is not None and declared > max_bytes:
        raise _UpstreamResponseTooLarge(f"upstream declared {declared} bytes")
    chunks = []
    size = 0
    reader = getattr(upstream, "read1", None) or upstream.read
    while True:
        if time.monotonic() > deadline:
            raise _UpstreamResponseTimeout("upstream response deadline exceeded")
        chunk = reader(_RESPONSE_READ_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise _UpstreamResponseTooLarge(f"upstream body over {max_bytes} bytes")
        chunks.append(chunk)
    if declared is not None and size != declared:
        raise http.client.IncompleteRead(b"", max(0, declared - size))
    return b"".join(chunks)

def _combine_query(path, query_string):
    if not query_string:
        return path
    return f"{path}?{query_string}"

def _clean_response_headers(headers):
    connection_tokens = _connection_header_tokens(headers)
    clean = {}
    for key, value in (headers or {}).items():
        name = _normalize_text(key)
        if not name:
            continue
        normalized = name.lower()
        if (
            normalized in _HOP_BY_HOP_RESPONSE_HEADERS
            or normalized in connection_tokens
        ):
            continue
        if isinstance(value, (list, tuple)):
            clean[name] = [_normalize_text(item) for item in value]
        else:
            clean[name] = _normalize_text(value)
    return clean

def _clean_request_headers(headers):
    connection_tokens = _connection_header_tokens(headers)
    clean = {}
    for key, value in (headers or {}).items():
        name = _normalize_text(key)
        if not name:
            continue
        normalized = name.lower()
        if (
            normalized in _HOP_BY_HOP_REQUEST_HEADERS
            or normalized in _FORWARDED_REQUEST_HEADERS
            or normalized in connection_tokens
        ):
            continue
        clean[name] = _normalize_text(value)
    return clean

def _connection_header_tokens(headers):
    tokens = set()
    for key, value in (headers or {}).items():
        if str(key).strip().lower() != "connection":
            continue
        tokens.update(
            token.strip().lower()
            for token in header_value_text(value or "").split(",")
            if token.strip()
        )
    return tokens

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
    wanted = str(name or "").lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return _normalize_text(value)
    return ""

def _forwarded_port_value(request):
    host = _header_value(request, "host")
    port_text = ""
    if host.startswith("["):
        closing = host.find("]")
        if closing >= 0 and host[closing + 1 :].startswith(":"):
            port_text = host[closing + 2 :]
    elif host.count(":") == 1:
        _hostname, _separator, port_text = host.rpartition(":")
    if port_text.isascii() and port_text.isdigit():
        port = int(port_text)
        if 1 <= port <= 65535:
            return str(port)
    tls = getattr(request, "tls", {}) or {}
    return "443" if tls.get("enabled") else "80"

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

def _dummy_request(rule):
    class _Req:
        method = "GET"
        path = rule.path_prefix or rule.path or "/"
        query_string = ""
        headers = {}
        client = ("127.0.0.1", 0)
        tls = {}

    return _Req()

import json
import re
from urllib.parse import parse_qsl

from .constants import STATUS_MESSAGES
from .headers import validate_header_name, validate_header_value


MAX_QUERY_FIELDS = 1024
_HTTP_METHOD_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def parse_query_string(qs):
    params = {}
    if not qs:
        return params
    pairs = parse_qsl(
        str(qs),
        keep_blank_values=True,
        strict_parsing=False,
        max_num_fields=MAX_QUERY_FIELDS,
    )
    for key, value in pairs:
        params[key] = value
    return params


class Request:
    def __init__(self, method, path, query_string, headers, body, client, tls=None):
        self.method = (method or "").upper()
        self.path = path or "/"
        self.query_string = query_string or ""
        self.query = parse_query_string(self.query_string)
        self.headers = headers or {}
        self.body = body or b""
        self.client = client
        self.tls = tls or {}
        self.app = None

    def text(self, encoding="utf-8"):
        return self.body.decode(encoding, errors="ignore")

    def json(self):
        try:
            return json.loads(self.text())
        except Exception:
            return None


class Response:
    def __init__(self, status=200, body=b"", headers=None, reason=None, stream=None):
        self.status = int(status)
        if not 100 <= self.status <= 599:
            raise ValueError("HTTP response status must be between 100 and 599")
        self.reason = None if reason is None else validate_header_value(reason)
        self.stream = stream
        self.is_stream = stream is not None
        if self.is_stream:
            self.body = b""
        elif isinstance(body, (bytes, bytearray, memoryview)):
            self.body = bytes(body)
        else:
            self.body = str(body).encode("utf-8")
        self.headers = {}
        for name, value in (headers or {}).items():
            header_name = validate_header_name(name)
            self.headers[header_name] = validate_header_value(value)

    @classmethod
    def json(cls, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        hdrs = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            hdrs.update(headers)
        return cls(status=status, body=body, headers=hdrs)

    @classmethod
    def text(cls, text, status=200, headers=None):
        hdrs = {"Content-Type": "text/plain; charset=utf-8"}
        if headers:
            hdrs.update(headers)
        return cls(status=status, body=text, headers=hdrs)

    @classmethod
    def html(cls, html, status=200, headers=None):
        hdrs = {"Content-Type": "text/html; charset=utf-8"}
        if headers:
            hdrs.update(headers)
        return cls(status=status, body=html, headers=hdrs)

    @classmethod
    def stream(cls, chunks, status=200, headers=None, content_type=None):
        hdrs = {}
        if content_type:
            hdrs["Content-Type"] = content_type
        if headers:
            hdrs.update(headers)
        return cls(status=status, headers=hdrs, stream=chunks)


def parse_http_request(conn, max_header_bytes=65536):
    max_header_bytes = int(max_header_bytes)
    if max_header_bytes <= 0:
        raise ValueError("max_header_bytes must be positive")

    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(1024)
        if not chunk:
            break
        data += chunk
        delimiter_at = data.find(b"\r\n\r\n")
        if delimiter_at > max_header_bytes or (
            delimiter_at < 0 and len(data) > max_header_bytes
        ):
            raise ValueError("Request headers too large")

    if not data:
        return None
    delimiter_at = data.find(b"\r\n\r\n")
    if delimiter_at < 0:
        raise ValueError("Incomplete request headers")

    header_bytes = data[:delimiter_at]
    remainder = data[delimiter_at + 4 :]
    if b"\x00" in header_bytes:
        raise ValueError("Request headers contain NUL")
    lines = header_bytes.decode("iso-8859-1").split("\r\n")
    if not lines or not lines[0]:
        raise ValueError("Missing HTTP request line")

    request_line = lines[0]
    parts = request_line.split()
    if len(parts) != 3:
        raise ValueError("Invalid HTTP request line")
    method, path, version = parts
    if not _HTTP_METHOD_RE.fullmatch(method):
        raise ValueError("Invalid HTTP method")
    if not path or any(ord(char) < 32 for char in path):
        raise ValueError("Invalid HTTP request target")
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise ValueError("Unsupported HTTP version")

    headers = {}
    for line in lines[1:]:
        if not line or line[:1] in {" ", "\t"} or ":" not in line:
            raise ValueError("Invalid HTTP header line")
        key, value = line.split(":", 1)
        header_name = validate_header_name(key)
        header_value = validate_header_value(value.strip(" \t"))
        normalized = header_name.lower()
        if normalized in {"content-length", "host"} and normalized in headers:
            raise ValueError(f"Duplicate {header_name} header")
        if normalized == "cookie" and normalized in headers:
            headers[normalized] = f"{headers[normalized]}; {header_value}"
        elif normalized in headers:
            headers[normalized] = f"{headers[normalized]}, {header_value}"
        else:
            headers[normalized] = header_value

    if version == "HTTP/1.1" and not headers.get("host"):
        raise ValueError("Missing Host header")

    return {
        "method": method,
        "path": path,
        "version": version,
        "headers": headers,
        "remainder": remainder,
    }


def send_http_response(conn, response, *, send_body=True):
    status_code = int(response.status)
    if not 100 <= status_code <= 599:
        raise ValueError("HTTP response status must be between 100 and 599")
    reason = validate_header_value(
        response.reason or STATUS_MESSAGES.get(status_code, "Unknown")
    )
    headers = {}
    seen_headers = set()
    for name, value in (response.headers or {}).items():
        header_name = validate_header_name(name)
        normalized = header_name.lower()
        if normalized in seen_headers:
            raise ValueError(f"Duplicate response header: {header_name}")
        seen_headers.add(normalized)
        headers[header_name] = validate_header_value(value)

    lowermap = {k.lower(): v for k, v in headers.items()}
    status_allows_body = not (100 <= status_code < 200 or status_code in {204, 304})
    should_send_body = bool(send_body and status_allows_body)
    if not status_allows_body:
        for name in list(headers):
            if name.lower() in {"content-length", "transfer-encoding"}:
                headers.pop(name)
        lowermap = {k.lower(): v for k, v in headers.items()}
    elif getattr(response, "is_stream", False):
        if "transfer-encoding" not in lowermap and "content-length" not in lowermap:
            headers["Transfer-Encoding"] = "chunked"
        lowermap = {k.lower(): v for k, v in headers.items()}
    elif "content-length" not in lowermap:
        headers["Content-Length"] = str(len(response.body))
        lowermap = {k.lower(): v for k, v in headers.items()}
    if "connection" not in lowermap:
        headers["Connection"] = "close"
        lowermap = {k.lower(): v for k, v in headers.items()}
    status_line = f"HTTP/1.1 {status_code} {reason}\r\n"
    hdrs = ""
    for k, v in headers.items():
        hdrs += f"{k}: {v}\r\n"
    resp = status_line + hdrs + "\r\n"
    try:
        conn.sendall(resp.encode("utf-8"))
        if not should_send_body:
            return
        if getattr(response, "is_stream", False):
            use_chunked = "chunked" in lowermap.get("transfer-encoding", "").lower()
            for chunk in _iter_stream_chunks(response.stream):
                if use_chunked:
                    conn.sendall(f"{len(chunk):X}\r\n".encode("utf-8"))
                    conn.sendall(chunk)
                    conn.sendall(b"\r\n")
                else:
                    conn.sendall(chunk)
            if use_chunked:
                conn.sendall(b"0\r\n\r\n")
        else:
            conn.sendall(response.body)
    except Exception as e:
        print(f"[http] send error {status_code}: {e}")


def _iter_stream_chunks(source):
    if source is None:
        return
    if isinstance(source, (bytes, bytearray, str)):
        normalized = _normalize_chunk(source)
        if normalized:
            yield normalized
        return
    if hasattr(source, "read") and callable(source.read):
        while True:
            chunk = source.read(8192)
            if not chunk:
                break
            normalized = _normalize_chunk(chunk)
            if normalized:
                yield normalized
        return
    for chunk in source:
        normalized = _normalize_chunk(chunk)
        if normalized:
            yield normalized


def _normalize_chunk(chunk):
    if chunk is None:
        return b""
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        return bytes(chunk)
    return str(chunk).encode("utf-8")

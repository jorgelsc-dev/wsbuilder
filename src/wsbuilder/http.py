import json

from .constants import STATUS_MESSAGES


def parse_query_string(qs):
    params = {}
    if not qs:
        return params
    parts = qs.split("&")
    for part in parts:
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        params[k] = v
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

    def text(self, encoding="utf-8"):
        return self.body.decode(encoding, errors="ignore")

    def json(self):
        try:
            return json.loads(self.text())
        except Exception:
            return None


class Response:
    def __init__(self, status=200, body=b"", headers=None, reason=None, stream=None):
        self.status = status
        self.reason = reason
        self.stream = stream
        self.is_stream = stream is not None
        if self.is_stream:
            self.body = b""
        elif isinstance(body, (bytes, bytearray)):
            self.body = bytes(body)
        else:
            self.body = str(body).encode("utf-8")
        self.headers = headers or {}

    @classmethod
    def json(cls, data, status=200, headers=None):
        body = json.dumps(data).encode("utf-8")
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
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(1024)
        if not chunk:
            break
        data += chunk
        if len(data) > int(max_header_bytes):
            raise ValueError("Request headers too large")
    header_text, _, remainder = data.partition(b"\r\n\r\n")
    lines = header_text.decode("utf-8", errors="ignore").split("\r\n")
    if not lines:
        return None
    request_line = lines[0]
    parts = request_line.split()
    if len(parts) != 3:
        return None
    method, path, version = parts
    if not version.startswith("HTTP/"):
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return {
        "method": method,
        "path": path,
        "version": version,
        "headers": headers,
        "remainder": remainder,
    }


def send_http_response(conn, response):
    status_code = response.status
    reason = response.reason or STATUS_MESSAGES.get(status_code, "OK")
    headers = dict(response.headers or {})
    lowermap = {k.lower(): v for k, v in headers.items()}
    if getattr(response, "is_stream", False):
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
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk)
    return str(chunk).encode("utf-8")

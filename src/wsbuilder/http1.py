"""HTTP/1.x framing: version negotiation, chunked coding and connection reuse.

Covers HTTP/0.9 (RFC 1945 appendix), HTTP/1.0 (RFC 1945) and HTTP/1.1
(RFC 9112). The pieces here are deliberately transport agnostic: they take a
reader rather than a socket so the same code paths are exercised by tests
without a network.
"""

from .headers import validate_header_name, validate_header_value

HTTP_0_9 = "HTTP/0.9"
HTTP_1_0 = "HTTP/1.0"
HTTP_1_1 = "HTTP/1.1"

#: Versions the HTTP/1.x code path understands, oldest first.
SUPPORTED_HTTP1_VERSIONS = (HTTP_0_9, HTTP_1_0, HTTP_1_1)

DEFAULT_MAX_TRAILER_BYTES = 8 * 1024
MAX_CHUNK_LINE_BYTES = 1024

#: Fields that change how a message is framed or routed, so a trailer section
#: must not carry them (RFC 9112 section 7.1.2).
FORBIDDEN_TRAILERS = frozenset(
    {
        "authorization",
        "cache-control",
        "content-encoding",
        "content-length",
        "content-range",
        "content-type",
        "expect",
        "host",
        "max-forwards",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
    }
)

_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


class BufferedReader:
    """A socket plus the bytes a previous read already pulled off it.

    ``parse_http_request`` reads ahead past the header block, so body framing
    has to start from that leftover before touching the socket again.
    """

    def __init__(self, conn, buffered=b""):
        self._conn = conn
        self._buffer = bytearray(buffered)

    @property
    def buffered(self):
        return bytes(self._buffer)

    def _fill(self, size=65536):
        chunk = self._conn.recv(size)
        if not chunk:
            raise ConnectionError("connection closed")
        self._buffer.extend(chunk)

    def read_exactly(self, count):
        count = int(count)
        if count < 0:
            raise ValueError("count must not be negative")
        while len(self._buffer) < count:
            self._fill(max(count - len(self._buffer), 4096))
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def read_line(self, max_bytes):
        """Read one CRLF-terminated line and return it without the CRLF."""
        max_bytes = int(max_bytes)
        while True:
            end = self._buffer.find(b"\r\n")
            if end >= 0:
                break
            if len(self._buffer) > max_bytes:
                raise ValueError("HTTP line exceeds limit")
            self._fill()
        if end > max_bytes:
            raise ValueError("HTTP line exceeds limit")
        line = bytes(self._buffer[:end])
        del self._buffer[: end + 2]
        return line


def parse_request_line(raw_line):
    """Split a request line, tolerating the version-less HTTP/0.9 form."""
    text = raw_line.decode("iso-8859-1") if isinstance(raw_line, bytes) else str(raw_line)
    parts = text.split()
    if len(parts) == 2:
        # HTTP/0.9 predates the version token and only ever defined GET.
        method, target = parts
        if method != "GET":
            raise ValueError("HTTP/0.9 only supports GET")
        return method, target, HTTP_0_9
    if len(parts) != 3:
        raise ValueError("Invalid HTTP request line")
    method, target, version = parts
    if version not in (HTTP_1_0, HTTP_1_1):
        raise ValueError("Unsupported HTTP version")
    return method, target, version


def parse_chunk_size(line):
    """Parse a chunk header, ignoring chunk extensions (RFC 9112 7.1.1)."""
    raw = bytes(line)
    size_field = raw.split(b";", 1)[0].strip()
    if not size_field:
        raise ValueError("Missing chunk size")
    if len(size_field) > 16 or any(byte not in _HEX_DIGITS for byte in size_field):
        raise ValueError("Invalid chunk size")
    return int(size_field, 16)


def read_trailer_section(reader, max_trailer_bytes=DEFAULT_MAX_TRAILER_BYTES):
    """Read the optional trailer section that follows the last chunk."""
    trailers = {}
    consumed = 0
    while True:
        line = reader.read_line(MAX_CHUNK_LINE_BYTES)
        if not line:
            return trailers
        consumed += len(line) + 2
        if consumed > max_trailer_bytes:
            raise ValueError("Trailer section too large")
        text = line.decode("iso-8859-1")
        if text[:1] in (" ", "\t") or ":" not in text:
            raise ValueError("Invalid trailer line")
        name, value = text.split(":", 1)
        header_name = validate_header_name(name)
        header_value = validate_header_value(value.strip(" \t"))
        normalized = header_name.lower()
        if normalized in FORBIDDEN_TRAILERS:
            raise ValueError(f"Header not allowed in trailers: {header_name}")
        if normalized in trailers:
            trailers[normalized] = f"{trailers[normalized]}, {header_value}"
        else:
            trailers[normalized] = header_value


def read_chunked_body(reader, *, max_body_bytes, max_trailer_bytes=DEFAULT_MAX_TRAILER_BYTES):
    """Decode a chunked request body, returning ``(body, trailers)``."""
    max_body_bytes = int(max_body_bytes)
    body = bytearray()
    while True:
        size = parse_chunk_size(reader.read_line(MAX_CHUNK_LINE_BYTES))
        if size == 0:
            break
        if len(body) + size > max_body_bytes:
            raise ValueError("Payload Too Large")
        body.extend(reader.read_exactly(size))
        if reader.read_exactly(2) != b"\r\n":
            raise ValueError("Malformed chunk terminator")
    trailers = read_trailer_section(reader, max_trailer_bytes=max_trailer_bytes)
    return bytes(body), trailers


def encode_chunk(payload):
    """Encode one chunk of a chunked response body."""
    data = bytes(payload)
    if not data:
        return b""
    return b"%X\r\n%s\r\n" % (len(data), data)


def encode_last_chunk():
    return b"0\r\n\r\n"


def parse_connection_tokens(headers):
    raw = ""
    for key, value in (headers or {}).items():
        if str(key).lower() == "connection":
            raw = str(value)
            break
    return {token.strip().lower() for token in raw.split(",") if token.strip()}


def client_wants_keep_alive(version, headers):
    """Apply the default-persistence rules of RFC 9112 section 9.3."""
    tokens = parse_connection_tokens(headers)
    if version == HTTP_1_1:
        return "close" not in tokens
    if version == HTTP_1_0:
        return "keep-alive" in tokens and "close" not in tokens
    return False


def response_is_self_delimiting(response, *, send_body=True):
    """True when the client can tell where the response ends without EOF."""
    status = int(getattr(response, "status", 200))
    if 100 <= status < 200 or status in (204, 304) or not send_body:
        return True
    headers = getattr(response, "headers", None) or {}
    lowered = {str(name).lower() for name in headers}
    if "content-length" in lowered:
        return True
    if not getattr(response, "is_stream", False):
        # A buffered body always gets a Content-Length when serialized.
        return True
    for name, value in headers.items():
        if str(name).lower() == "transfer-encoding":
            return "chunked" in str(value).lower()
    return False


def should_keep_alive(version, request_headers, response, *, send_body=True, server_allows=True):
    """Decide whether this connection can carry another request."""
    if not server_allows:
        return False
    if version not in (HTTP_1_0, HTTP_1_1):
        return False
    if not client_wants_keep_alive(version, request_headers):
        return False
    if "close" in parse_connection_tokens(getattr(response, "headers", None)):
        return False
    return response_is_self_delimiting(response, send_body=send_body)


def connection_header_value(version, keep_alive):
    """The Connection header a response should carry for this outcome."""
    if not keep_alive:
        return "close"
    return "keep-alive" if version == HTTP_1_0 else None


__all__ = [
    "BufferedReader",
    "DEFAULT_MAX_TRAILER_BYTES",
    "FORBIDDEN_TRAILERS",
    "HTTP_0_9",
    "HTTP_1_0",
    "HTTP_1_1",
    "SUPPORTED_HTTP1_VERSIONS",
    "client_wants_keep_alive",
    "connection_header_value",
    "encode_chunk",
    "encode_last_chunk",
    "parse_chunk_size",
    "parse_connection_tokens",
    "parse_request_line",
    "read_chunked_body",
    "read_trailer_section",
    "response_is_self_delimiting",
    "should_keep_alive",
]

"""HTTP/3 framing and request handling (RFC 9114).

With QUIC underneath, HTTP/3 is the smallest of the three versions. QUIC
already provides streams, ordering within a stream, flow control and
encryption, so HTTP/3 only has to say what goes on those streams.

Each request takes one bidirectional stream: HEADERS, optional DATA, then
the stream ends. There is no stream multiplexing to invent and no
connection-level header compression state to keep in step -- the things that
make HTTP/2 intricate are either handled by QUIC or, in QPACK's case, moved
onto separate ordered streams.
"""

from .qpack import QPACKError, decode_field_section, encode_field_section
from .quic.varint import decode_varint, encode_varint

FRAME_DATA = 0x00
FRAME_HEADERS = 0x01
FRAME_CANCEL_PUSH = 0x03
FRAME_SETTINGS = 0x04
FRAME_PUSH_PROMISE = 0x05
FRAME_GOAWAY = 0x07
FRAME_MAX_PUSH_ID = 0x0D

FRAME_NAMES = {
    FRAME_DATA: "DATA",
    FRAME_HEADERS: "HEADERS",
    FRAME_CANCEL_PUSH: "CANCEL_PUSH",
    FRAME_SETTINGS: "SETTINGS",
    FRAME_PUSH_PROMISE: "PUSH_PROMISE",
    FRAME_GOAWAY: "GOAWAY",
    FRAME_MAX_PUSH_ID: "MAX_PUSH_ID",
}

STREAM_CONTROL = 0x00
STREAM_PUSH = 0x01
STREAM_QPACK_ENCODER = 0x02
STREAM_QPACK_DECODER = 0x03

SETTINGS_QPACK_MAX_TABLE_CAPACITY = 0x01
SETTINGS_MAX_FIELD_SECTION_SIZE = 0x06
SETTINGS_QPACK_BLOCKED_STREAMS = 0x07

H3_NO_ERROR = 0x0100
H3_GENERAL_PROTOCOL_ERROR = 0x0101
H3_INTERNAL_ERROR = 0x0102
H3_STREAM_CREATION_ERROR = 0x0103
H3_CLOSED_CRITICAL_STREAM = 0x0104
H3_FRAME_UNEXPECTED = 0x0105
H3_FRAME_ERROR = 0x0106
H3_EXCESSIVE_LOAD = 0x0107
H3_ID_ERROR = 0x0108
H3_SETTINGS_ERROR = 0x0109
H3_MISSING_SETTINGS = 0x010A
H3_REQUEST_REJECTED = 0x010B
H3_MESSAGE_ERROR = 0x010E

#: Fields HTTP/3 forbids for the same reason HTTP/2 does.
FORBIDDEN_HEADERS = frozenset(
    {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "upgrade"}
)

#: Frame types reserved to exercise the "ignore unknown" rule (section 7.2.8).
GREASE_MASK = 0x1F * 0x1F


class H3Error(Exception):
    def __init__(self, message, code=H3_GENERAL_PROTOCOL_ERROR):
        super().__init__(message)
        self.code = int(code)


def encode_frame(frame_type, payload=b""):
    payload = bytes(payload)
    return encode_varint(frame_type) + encode_varint(len(payload)) + payload


def parse_frames(data, *, allow_partial=True):
    """Decode whole frames, returning them plus any unconsumed tail."""
    data = bytes(data)
    offset = 0
    frames = []
    while offset < len(data):
        try:
            frame_type, cursor = decode_varint(data, offset)
            length, cursor = decode_varint(data, cursor)
        except ValueError:
            if allow_partial:
                break
            raise H3Error("truncated frame header", H3_FRAME_ERROR) from None
        end = cursor + length
        if end > len(data):
            if allow_partial:
                break
            raise H3Error("frame runs past the stream", H3_FRAME_ERROR)
        frames.append((frame_type, data[cursor:end]))
        offset = end
    return frames, data[offset:]


def encode_settings(values):
    return b"".join(encode_varint(key) + encode_varint(value) for key, value in values.items())


def decode_settings(payload):
    data = bytes(payload)
    offset = 0
    settings = {}
    while offset < len(data):
        key, offset = decode_varint(data, offset)
        value, offset = decode_varint(data, offset)
        if key in settings:
            raise H3Error(f"duplicate setting 0x{key:x}", H3_SETTINGS_ERROR)
        settings[key] = value
    return settings


def is_reserved_frame_type(frame_type):
    """Reserved types exist so peers prove they ignore what they do not know."""
    return frame_type >= 0x21 and (frame_type - 0x21) % 0x1F == 0


def validate_request_headers(headers):
    """The pseudo-header rules of RFC 9114 section 4.3.1."""
    pseudo = {}
    regular = []
    seen_regular = False
    for name, value in headers:
        if name.startswith(":"):
            if seen_regular:
                raise H3Error("pseudo-header after a regular field", H3_MESSAGE_ERROR)
            if name not in (":method", ":scheme", ":authority", ":path"):
                raise H3Error(f"unknown request pseudo-header {name}", H3_MESSAGE_ERROR)
            if name in pseudo:
                raise H3Error(f"duplicate pseudo-header {name}", H3_MESSAGE_ERROR)
            pseudo[name] = value
            continue
        seen_regular = True
        lowered = name.lower()
        if lowered != name:
            raise H3Error(f"header name {name!r} is not lowercase", H3_MESSAGE_ERROR)
        if lowered in FORBIDDEN_HEADERS:
            raise H3Error(f"connection-specific header {name!r}", H3_MESSAGE_ERROR)
        regular.append((lowered, value))

    method = pseudo.get(":method")
    if method == "CONNECT":
        if ":authority" not in pseudo:
            raise H3Error("CONNECT requires :authority", H3_MESSAGE_ERROR)
    else:
        for required in (":method", ":scheme", ":path"):
            if required not in pseudo:
                raise H3Error(f"missing {required}", H3_MESSAGE_ERROR)
        if not pseudo[":path"]:
            raise H3Error(":path must not be empty", H3_MESSAGE_ERROR)
    return pseudo, regular


class RequestStream:
    """One request/response exchange on a bidirectional QUIC stream."""

    def __init__(self, stream_id):
        self.id = int(stream_id)
        self.buffer = bytearray()
        self.headers = None
        self.body = bytearray()
        self.trailers = None
        self.finished = False
        self.answered = False

    def feed(self, data, fin=False):
        """Add stream bytes; returns True once the request is complete."""
        self.buffer.extend(data)
        frames, tail = parse_frames(self.buffer)
        self.buffer = bytearray(tail)
        for frame_type, payload in frames:
            self._on_frame(frame_type, payload)
        if fin:
            self.finished = True
            if self.buffer:
                raise H3Error("stream ended mid-frame", H3_FRAME_ERROR)
        return self.finished and self.headers is not None

    def _on_frame(self, frame_type, payload):
        if frame_type == FRAME_HEADERS:
            try:
                fields = decode_field_section(payload)
            except QPACKError as exc:
                raise H3Error(str(exc), H3_GENERAL_PROTOCOL_ERROR) from exc
            if self.headers is None:
                self.headers = fields
            else:
                self.trailers = fields
            return
        if frame_type == FRAME_DATA:
            if self.headers is None:
                # Section 4.1: a request opens with HEADERS, always.
                raise H3Error("DATA before HEADERS", H3_FRAME_UNEXPECTED)
            self.body.extend(payload)
            return
        if frame_type in (FRAME_SETTINGS, FRAME_GOAWAY, FRAME_MAX_PUSH_ID, FRAME_CANCEL_PUSH):
            raise H3Error(
                f"{FRAME_NAMES.get(frame_type)} does not belong on a request stream",
                H3_FRAME_UNEXPECTED,
            )
        if frame_type == FRAME_PUSH_PROMISE:
            raise H3Error("a client must not push", H3_FRAME_UNEXPECTED)
        # Unknown and reserved types are discarded (section 9).

    def describe(self):
        return {
            "id": self.id,
            "has_headers": self.headers is not None,
            "body_bytes": len(self.body),
            "finished": self.finished,
        }


def build_request(stream, client_address, tls=None):
    """Turn a completed request stream into a wsbuilder Request."""
    from .http import Request

    pseudo, regular = validate_request_headers(stream.headers)
    target = pseudo.get(":path", "*")
    path, _, query = target.partition("?")
    headers = dict(regular)
    if ":authority" in pseudo:
        headers.setdefault("host", pseudo[":authority"])
    return Request(
        method=pseudo.get(":method", "GET"),
        path=path,
        query_string=query,
        headers=headers,
        body=bytes(stream.body),
        client=client_address,
        tls=tls or {},
        version="HTTP/3",
        trailers=dict(stream.trailers or []),
    )


def build_response_frames(response, *, send_body=True):
    """Serialize a Response as the HEADERS and DATA frames of a stream."""
    fields = [(":status", str(int(response.status)))]
    for name, value in (response.headers or {}).items():
        lowered = str(name).lower()
        if lowered in FORBIDDEN_HEADERS:
            continue
        fields.append((lowered, str(value)))

    body = b""
    if send_body:
        if response.is_stream:
            from .http import _iter_stream_chunks

            body = b"".join(_iter_stream_chunks(response.stream))
        else:
            body = bytes(response.body)
        fields.append(("content-length", str(len(body))))

    out = encode_frame(FRAME_HEADERS, encode_field_section(fields))
    if body:
        out += encode_frame(FRAME_DATA, body)
    return out


def build_control_stream(settings=None):
    """The opening of a server's control stream: its type, then SETTINGS."""
    values = {
        SETTINGS_QPACK_MAX_TABLE_CAPACITY: 0,
        SETTINGS_QPACK_BLOCKED_STREAMS: 0,
        SETTINGS_MAX_FIELD_SECTION_SIZE: 65536,
    }
    if settings:
        values.update(settings)
    return encode_varint(STREAM_CONTROL) + encode_frame(FRAME_SETTINGS, encode_settings(values))


__all__ = [
    "FORBIDDEN_HEADERS",
    "FRAME_DATA",
    "FRAME_GOAWAY",
    "FRAME_HEADERS",
    "FRAME_SETTINGS",
    "H3Error",
    "H3_FRAME_UNEXPECTED",
    "H3_MESSAGE_ERROR",
    "H3_NO_ERROR",
    "RequestStream",
    "SETTINGS_MAX_FIELD_SECTION_SIZE",
    "SETTINGS_QPACK_BLOCKED_STREAMS",
    "SETTINGS_QPACK_MAX_TABLE_CAPACITY",
    "STREAM_CONTROL",
    "STREAM_QPACK_DECODER",
    "STREAM_QPACK_ENCODER",
    "build_control_stream",
    "build_request",
    "build_response_frames",
    "decode_settings",
    "encode_frame",
    "encode_settings",
    "is_reserved_frame_type",
    "parse_frames",
    "validate_request_headers",
]

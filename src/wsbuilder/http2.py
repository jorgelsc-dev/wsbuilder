"""HTTP/2 framing and connection handling (RFC 9113).

HTTP/2 replaces the text framing of HTTP/1 with binary frames multiplexed
over one connection. Every frame carries a stream identifier, so requests no
longer queue behind each other, and headers travel compressed with HPACK.

Two consequences shape this module. Flow control is mandatory and per stream
as well as per connection, so a sender that ignores a window can stall a peer
rather than merely be rude. And the HPACK context spans the whole connection,
so a header block must be decoded even on a stream being discarded -- skipping
it would desynchronise the decoder for every later stream.
"""

import struct

from .hpack import Decoder, Encoder, HPACKError

#: RFC 9113 section 3.4. A client opens with this exact octet sequence.
CONNECTION_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

FRAME_HEADER_SIZE = 9

FRAME_DATA = 0x0
FRAME_HEADERS = 0x1
FRAME_PRIORITY = 0x2
FRAME_RST_STREAM = 0x3
FRAME_SETTINGS = 0x4
FRAME_PUSH_PROMISE = 0x5
FRAME_PING = 0x6
FRAME_GOAWAY = 0x7
FRAME_WINDOW_UPDATE = 0x8
FRAME_CONTINUATION = 0x9

FRAME_NAMES = {
    FRAME_DATA: "DATA",
    FRAME_HEADERS: "HEADERS",
    FRAME_PRIORITY: "PRIORITY",
    FRAME_RST_STREAM: "RST_STREAM",
    FRAME_SETTINGS: "SETTINGS",
    FRAME_PUSH_PROMISE: "PUSH_PROMISE",
    FRAME_PING: "PING",
    FRAME_GOAWAY: "GOAWAY",
    FRAME_WINDOW_UPDATE: "WINDOW_UPDATE",
    FRAME_CONTINUATION: "CONTINUATION",
}

FLAG_END_STREAM = 0x1
FLAG_ACK = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20

SETTINGS_HEADER_TABLE_SIZE = 0x1
SETTINGS_ENABLE_PUSH = 0x2
SETTINGS_MAX_CONCURRENT_STREAMS = 0x3
SETTINGS_INITIAL_WINDOW_SIZE = 0x4
SETTINGS_MAX_FRAME_SIZE = 0x5
SETTINGS_MAX_HEADER_LIST_SIZE = 0x6

ERROR_NO_ERROR = 0x0
ERROR_PROTOCOL_ERROR = 0x1
ERROR_INTERNAL_ERROR = 0x2
ERROR_FLOW_CONTROL_ERROR = 0x3
ERROR_SETTINGS_TIMEOUT = 0x4
ERROR_STREAM_CLOSED = 0x5
ERROR_FRAME_SIZE_ERROR = 0x6
ERROR_REFUSED_STREAM = 0x7
ERROR_CANCEL = 0x8
ERROR_COMPRESSION_ERROR = 0x9
ERROR_CONNECT_ERROR = 0xA
ERROR_ENHANCE_YOUR_CALM = 0xB
ERROR_INADEQUATE_SECURITY = 0xC
ERROR_HTTP_1_1_REQUIRED = 0xD

DEFAULT_MAX_FRAME_SIZE = 16384
MAX_ALLOWED_FRAME_SIZE = 16777215
DEFAULT_INITIAL_WINDOW_SIZE = 65535
MAX_WINDOW_SIZE = 2**31 - 1

#: Connection-specific fields HTTP/2 forbids (RFC 9113 section 8.2.2).
FORBIDDEN_HEADERS = frozenset(
    {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "upgrade"}
)
PSEUDO_REQUEST_HEADERS = frozenset({":method", ":scheme", ":authority", ":path"})


class ConnectionError_(Exception):
    """A fault that takes the whole connection down, with a GOAWAY."""

    def __init__(self, message, code=ERROR_PROTOCOL_ERROR):
        super().__init__(message)
        self.code = code


class StreamError(Exception):
    """A fault confined to one stream, answered with RST_STREAM."""

    def __init__(self, message, stream_id, code=ERROR_PROTOCOL_ERROR):
        super().__init__(message)
        self.stream_id = stream_id
        self.code = code


class Frame:
    __slots__ = ("type", "flags", "stream_id", "payload")

    def __init__(self, frame_type, flags, stream_id, payload=b""):
        self.type = int(frame_type)
        self.flags = int(flags)
        self.stream_id = int(stream_id)
        self.payload = bytes(payload)

    @property
    def name(self):
        return FRAME_NAMES.get(self.type, f"UNKNOWN(0x{self.type:x})")

    def has(self, flag):
        return bool(self.flags & flag)

    def serialize(self):
        length = len(self.payload)
        if length > MAX_ALLOWED_FRAME_SIZE:
            raise ValueError("frame payload exceeds the 24-bit length field")
        header = struct.pack(
            ">BHBBI",
            (length >> 16) & 0xFF,
            length & 0xFFFF,
            self.type & 0xFF,
            self.flags & 0xFF,
            self.stream_id & 0x7FFFFFFF,
        )
        return header + self.payload

    def __repr__(self):
        return f"<Frame {self.name} flags=0x{self.flags:02x} stream={self.stream_id} len={len(self.payload)}>"


def parse_frame_header(data):
    """Split a 9-octet frame header into ``(length, type, flags, stream_id)``."""
    if len(data) < FRAME_HEADER_SIZE:
        raise ValueError("frame header is 9 octets")
    high, low, frame_type, flags, stream = struct.unpack(">BHBBI", bytes(data[:FRAME_HEADER_SIZE]))
    length = (high << 16) | low
    # The top bit is reserved and must be ignored on receipt.
    return length, frame_type, flags, stream & 0x7FFFFFFF


def strip_padding(payload, flags, stream_id):
    """Remove the pad length and padding an octet-padded frame carries."""
    if not flags & FLAG_PADDED:
        return payload
    if not payload:
        raise ConnectionError_("padded frame without a pad length", ERROR_PROTOCOL_ERROR)
    pad_length = payload[0]
    body = payload[1:]
    if pad_length > len(body):
        raise ConnectionError_("padding longer than the frame payload", ERROR_PROTOCOL_ERROR)
    return body[: len(body) - pad_length]


def encode_settings(values):
    return b"".join(struct.pack(">HI", key, value) for key, value in values.items())


def decode_settings(payload):
    payload = bytes(payload)
    if len(payload) % 6:
        raise ConnectionError_("SETTINGS payload is not a multiple of 6", ERROR_FRAME_SIZE_ERROR)
    settings = {}
    for offset in range(0, len(payload), 6):
        key, value = struct.unpack(">HI", payload[offset : offset + 6])
        settings[key] = value
    return settings


def validate_settings(settings):
    """Reject the values RFC 9113 section 6.5.2 declares out of range."""
    push = settings.get(SETTINGS_ENABLE_PUSH)
    if push is not None and push not in (0, 1):
        raise ConnectionError_("ENABLE_PUSH must be 0 or 1", ERROR_PROTOCOL_ERROR)
    window = settings.get(SETTINGS_INITIAL_WINDOW_SIZE)
    if window is not None and window > MAX_WINDOW_SIZE:
        raise ConnectionError_("INITIAL_WINDOW_SIZE above 2^31-1", ERROR_FLOW_CONTROL_ERROR)
    frame_size = settings.get(SETTINGS_MAX_FRAME_SIZE)
    if frame_size is not None and not DEFAULT_MAX_FRAME_SIZE <= frame_size <= MAX_ALLOWED_FRAME_SIZE:
        raise ConnectionError_("MAX_FRAME_SIZE out of range", ERROR_PROTOCOL_ERROR)
    return settings


class FlowControlWindow:
    """One direction of one window, connection-wide or per stream."""

    def __init__(self, initial=DEFAULT_INITIAL_WINDOW_SIZE):
        self.available = int(initial)

    def consume(self, amount):
        amount = int(amount)
        if amount > self.available:
            raise ConnectionError_(
                f"peer sent {amount} octets with {self.available} of window",
                ERROR_FLOW_CONTROL_ERROR,
            )
        self.available -= amount

    def credit(self, amount):
        amount = int(amount)
        if amount == 0:
            raise ConnectionError_("WINDOW_UPDATE of 0", ERROR_PROTOCOL_ERROR)
        if self.available + amount > MAX_WINDOW_SIZE:
            raise ConnectionError_("window grew past 2^31-1", ERROR_FLOW_CONTROL_ERROR)
        self.available += amount

    def adjust_initial(self, delta):
        """Apply a change of SETTINGS_INITIAL_WINDOW_SIZE to a live window."""
        self.available += int(delta)
        if self.available > MAX_WINDOW_SIZE:
            raise ConnectionError_("window grew past 2^31-1", ERROR_FLOW_CONTROL_ERROR)


IDLE = "idle"
OPEN = "open"
HALF_CLOSED_REMOTE = "half-closed(remote)"
HALF_CLOSED_LOCAL = "half-closed(local)"
CLOSED = "closed"


class Stream:
    """One request/response exchange inside the connection."""

    def __init__(self, stream_id, initial_window=DEFAULT_INITIAL_WINDOW_SIZE):
        self.id = int(stream_id)
        self.state = IDLE
        self.headers = []
        self.body = bytearray()
        self.trailers = []
        self.inbound = FlowControlWindow(initial_window)
        self.outbound = FlowControlWindow(initial_window)
        self.reset_code = None

    @property
    def closed(self):
        return self.state == CLOSED

    def open_remote(self, end_stream):
        self.state = HALF_CLOSED_REMOTE if end_stream else OPEN

    def end_remote(self):
        if self.state == OPEN:
            self.state = HALF_CLOSED_REMOTE
        elif self.state == HALF_CLOSED_LOCAL:
            self.state = CLOSED

    def end_local(self):
        if self.state in (OPEN, IDLE):
            self.state = HALF_CLOSED_LOCAL
        elif self.state == HALF_CLOSED_REMOTE:
            self.state = CLOSED

    def describe(self):
        return {
            "id": self.id,
            "state": self.state,
            "body_bytes": len(self.body),
            "inbound_window": self.inbound.available,
            "outbound_window": self.outbound.available,
        }


def validate_request_headers(headers, stream_id):
    """Check the pseudo-header rules of RFC 9113 section 8.3.1."""
    seen_regular = False
    pseudo = {}
    regular = []
    for name, value in headers:
        if name.startswith(":"):
            if seen_regular:
                raise StreamError("pseudo-header after a regular field", stream_id)
            if name not in PSEUDO_REQUEST_HEADERS:
                raise StreamError(f"unknown request pseudo-header {name}", stream_id)
            if name in pseudo:
                raise StreamError(f"duplicate pseudo-header {name}", stream_id)
            pseudo[name] = value
            continue
        seen_regular = True
        lowered = name.lower()
        if lowered != name:
            # Field names travel lowercase; anything else is malformed.
            raise StreamError(f"header name {name!r} is not lowercase", stream_id)
        if lowered in FORBIDDEN_HEADERS:
            raise StreamError(f"connection-specific header {name!r}", stream_id)
        if lowered == "te" and value.lower() != "trailers":
            raise StreamError("te may only carry 'trailers'", stream_id)
        regular.append((lowered, value))

    method = pseudo.get(":method")
    if method == "CONNECT":
        if ":scheme" in pseudo or ":path" in pseudo:
            raise StreamError("CONNECT takes neither :scheme nor :path", stream_id)
        if ":authority" not in pseudo:
            raise StreamError("CONNECT requires :authority", stream_id)
    else:
        for required in (":method", ":scheme", ":path"):
            if required not in pseudo:
                raise StreamError(f"missing {required}", stream_id)
        if pseudo[":path"] == "":
            raise StreamError(":path must not be empty", stream_id)
    return pseudo, regular


class Http2Connection:
    """Drives one HTTP/2 connection: frames in, responses out.

    Takes anything with ``recv`` and ``sendall``, so the frame logic is
    exercised in tests without a socket.
    """

    def __init__(self, conn, app, *, tls=None, client_address=("", 0), max_streams=100):
        self.conn = conn
        self.app = app
        self.tls = tls or {}
        self.client_address = client_address
        self.streams = {}
        self.decoder = Decoder()
        self.encoder = Encoder()
        self.inbound = FlowControlWindow()
        self.outbound = FlowControlWindow()
        self.local_settings = {
            SETTINGS_ENABLE_PUSH: 0,
            SETTINGS_MAX_CONCURRENT_STREAMS: int(max_streams),
            SETTINGS_INITIAL_WINDOW_SIZE: DEFAULT_INITIAL_WINDOW_SIZE,
            SETTINGS_MAX_FRAME_SIZE: DEFAULT_MAX_FRAME_SIZE,
        }
        self.peer_settings = {
            SETTINGS_INITIAL_WINDOW_SIZE: DEFAULT_INITIAL_WINDOW_SIZE,
            SETTINGS_MAX_FRAME_SIZE: DEFAULT_MAX_FRAME_SIZE,
        }
        self.last_stream_id = 0
        self.goaway_sent = False
        self._buffer = bytearray()
        self._pending_headers = None
        self._closed = False

    # -- transport -----------------------------------------------------

    def _read(self, count):
        while len(self._buffer) < count:
            chunk = self.conn.recv(max(count - len(self._buffer), 8192))
            if not chunk:
                raise ConnectionError("peer closed the connection")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def _send(self, frame):
        self.conn.sendall(frame.serialize())

    def send_settings(self, ack=False):
        if ack:
            self._send(Frame(FRAME_SETTINGS, FLAG_ACK, 0))
        else:
            self._send(Frame(FRAME_SETTINGS, 0, 0, encode_settings(self.local_settings)))

    def send_goaway(self, code=ERROR_NO_ERROR, debug=b""):
        if self.goaway_sent:
            return
        self.goaway_sent = True
        payload = struct.pack(">II", self.last_stream_id & 0x7FFFFFFF, code) + bytes(debug)
        try:
            self._send(Frame(FRAME_GOAWAY, 0, 0, payload))
        except OSError:
            pass

    def send_rst_stream(self, stream_id, code):
        try:
            self._send(Frame(FRAME_RST_STREAM, 0, stream_id, struct.pack(">I", code)))
        except OSError:
            pass

    # -- lifecycle -----------------------------------------------------

    def read_preface(self):
        received = self._read(len(CONNECTION_PREFACE))
        if received != CONNECTION_PREFACE:
            raise ConnectionError_("client preface missing or malformed")

    def next_frame(self):
        length, frame_type, flags, stream_id = parse_frame_header(self._read(FRAME_HEADER_SIZE))
        limit = self.local_settings[SETTINGS_MAX_FRAME_SIZE]
        if length > limit:
            raise ConnectionError_(
                f"frame of {length} octets over the {limit} limit", ERROR_FRAME_SIZE_ERROR
            )
        return Frame(frame_type, flags, stream_id, self._read(length) if length else b"")

    def serve(self):
        """Run the connection until the peer goes away or a fault ends it."""
        try:
            self.read_preface()
            self.send_settings()
            while not self._closed:
                frame = self.next_frame()
                self.handle_frame(frame)
        except ConnectionError_ as exc:
            self.send_goaway(exc.code, str(exc).encode("utf-8", "replace")[:128])
        except (ConnectionError, OSError):
            pass
        except HPACKError as exc:
            # A broken header block leaves the shared table unusable.
            self.send_goaway(ERROR_COMPRESSION_ERROR, str(exc).encode("utf-8", "replace")[:128])
        else:
            self.send_goaway(ERROR_NO_ERROR)

    # -- frame handling ------------------------------------------------

    def handle_frame(self, frame):
        if self._pending_headers is not None and frame.type != FRAME_CONTINUATION:
            raise ConnectionError_("expected CONTINUATION after an unfinished header block")

        handler = {
            FRAME_DATA: self._on_data,
            FRAME_HEADERS: self._on_headers,
            FRAME_PRIORITY: self._on_priority,
            FRAME_RST_STREAM: self._on_rst_stream,
            FRAME_SETTINGS: self._on_settings,
            FRAME_PUSH_PROMISE: self._on_push_promise,
            FRAME_PING: self._on_ping,
            FRAME_GOAWAY: self._on_goaway,
            FRAME_WINDOW_UPDATE: self._on_window_update,
            FRAME_CONTINUATION: self._on_continuation,
        }.get(frame.type)
        if handler is None:
            # Section 4.1: unknown frame types are discarded, not an error.
            return
        try:
            handler(frame)
        except StreamError as exc:
            stream = self.streams.get(exc.stream_id)
            if stream is not None:
                stream.state = CLOSED
                stream.reset_code = exc.code
            self.send_rst_stream(exc.stream_id, exc.code)

    def _require_stream_id(self, frame, expected_zero):
        if expected_zero and frame.stream_id != 0:
            raise ConnectionError_(f"{frame.name} must use stream 0")
        if not expected_zero and frame.stream_id == 0:
            raise ConnectionError_(f"{frame.name} must not use stream 0")

    def _on_settings(self, frame):
        self._require_stream_id(frame, True)
        if frame.has(FLAG_ACK):
            if frame.payload:
                raise ConnectionError_("SETTINGS ack carries a payload", ERROR_FRAME_SIZE_ERROR)
            return
        settings = validate_settings(decode_settings(frame.payload))
        if SETTINGS_INITIAL_WINDOW_SIZE in settings:
            previous = self.peer_settings.get(
                SETTINGS_INITIAL_WINDOW_SIZE, DEFAULT_INITIAL_WINDOW_SIZE
            )
            delta = settings[SETTINGS_INITIAL_WINDOW_SIZE] - previous
            # Section 6.9.2: the change applies to every live stream at once.
            for stream in self.streams.values():
                stream.outbound.adjust_initial(delta)
        if SETTINGS_HEADER_TABLE_SIZE in settings:
            self.encoder.set_max_size(settings[SETTINGS_HEADER_TABLE_SIZE])
        self.peer_settings.update(settings)
        self.send_settings(ack=True)

    def _on_ping(self, frame):
        self._require_stream_id(frame, True)
        if len(frame.payload) != 8:
            raise ConnectionError_("PING payload must be 8 octets", ERROR_FRAME_SIZE_ERROR)
        if not frame.has(FLAG_ACK):
            self._send(Frame(FRAME_PING, FLAG_ACK, 0, frame.payload))

    def _on_goaway(self, frame):
        self._require_stream_id(frame, True)
        self._closed = True

    def _on_priority(self, frame):
        self._require_stream_id(frame, False)
        if len(frame.payload) != 5:
            raise StreamError("PRIORITY payload must be 5 octets", frame.stream_id,
                              ERROR_FRAME_SIZE_ERROR)
        # Prioritisation is advisory and deprecated in RFC 9113; parsed, not used.

    def _on_push_promise(self, frame):
        raise ConnectionError_("server push is disabled by our SETTINGS")

    def _on_rst_stream(self, frame):
        self._require_stream_id(frame, False)
        if len(frame.payload) != 4:
            raise ConnectionError_("RST_STREAM payload must be 4 octets", ERROR_FRAME_SIZE_ERROR)
        stream = self.streams.get(frame.stream_id)
        if stream is None:
            if frame.stream_id > self.last_stream_id:
                raise ConnectionError_("RST_STREAM on an idle stream")
            return
        stream.state = CLOSED
        stream.reset_code = struct.unpack(">I", frame.payload)[0]

    def _on_window_update(self, frame):
        if len(frame.payload) != 4:
            raise ConnectionError_("WINDOW_UPDATE payload must be 4 octets", ERROR_FRAME_SIZE_ERROR)
        increment = struct.unpack(">I", frame.payload)[0] & 0x7FFFFFFF
        if frame.stream_id == 0:
            self.outbound.credit(increment)
            return
        stream = self.streams.get(frame.stream_id)
        if stream is None:
            return
        try:
            stream.outbound.credit(increment)
        except ConnectionError_ as exc:
            raise StreamError(str(exc), frame.stream_id, ERROR_FLOW_CONTROL_ERROR) from exc

    def _on_headers(self, frame):
        self._require_stream_id(frame, False)
        if frame.stream_id % 2 == 0:
            raise ConnectionError_("clients open odd-numbered streams only")
        if frame.stream_id <= self.last_stream_id and frame.stream_id not in self.streams:
            raise ConnectionError_("stream identifiers must increase")

        payload = strip_padding(frame.payload, frame.flags, frame.stream_id)
        if frame.has(FLAG_PRIORITY):
            if len(payload) < 5:
                raise ConnectionError_("HEADERS priority block truncated", ERROR_FRAME_SIZE_ERROR)
            payload = payload[5:]

        stream = self.streams.get(frame.stream_id)
        if stream is None:
            limit = self.local_settings[SETTINGS_MAX_CONCURRENT_STREAMS]
            live = sum(1 for s in self.streams.values() if not s.closed)
            if live >= limit:
                raise StreamError("too many concurrent streams", frame.stream_id,
                                  ERROR_REFUSED_STREAM)
            stream = Stream(frame.stream_id, self.peer_settings[SETTINGS_INITIAL_WINDOW_SIZE])
            self.streams[frame.stream_id] = stream
            self.last_stream_id = max(self.last_stream_id, frame.stream_id)

        self._pending_headers = (stream, bytearray(payload), frame.has(FLAG_END_STREAM))
        if frame.has(FLAG_END_HEADERS):
            self._finish_header_block()

    def _on_continuation(self, frame):
        if self._pending_headers is None:
            raise ConnectionError_("CONTINUATION without an open header block")
        stream, buffer, end_stream = self._pending_headers
        if frame.stream_id != stream.id:
            raise ConnectionError_("CONTINUATION on a different stream")
        buffer.extend(frame.payload)
        if frame.has(FLAG_END_HEADERS):
            self._finish_header_block()

    def _finish_header_block(self):
        stream, buffer, end_stream = self._pending_headers
        self._pending_headers = None
        # Decoded even when the stream is doomed: the HPACK table is shared,
        # so skipping a block would desync every stream that follows.
        headers = self.decoder.decode(bytes(buffer))
        if stream.closed:
            return
        if stream.headers:
            stream.trailers = headers
            stream.end_remote()
            self._dispatch(stream)
            return
        stream.headers = headers
        stream.open_remote(end_stream)
        if end_stream:
            self._dispatch(stream)

    def _on_data(self, frame):
        self._require_stream_id(frame, False)
        stream = self.streams.get(frame.stream_id)
        payload = strip_padding(frame.payload, frame.flags, frame.stream_id)

        # The connection window is consumed by the whole frame, padding included.
        received = len(frame.payload)
        self.inbound.consume(received)
        if received:
            # An increment of 0 is itself a protocol error, and an empty DATA
            # frame is the ordinary way to close a stream after the headers.
            self._send(Frame(FRAME_WINDOW_UPDATE, 0, 0, struct.pack(">I", received)))
            self.inbound.credit(received)

        if stream is None or stream.closed:
            return
        if stream.state not in (OPEN, HALF_CLOSED_LOCAL):
            raise StreamError("DATA on a stream that is not open", frame.stream_id,
                              ERROR_STREAM_CLOSED)
        stream.inbound.consume(received)
        stream.body.extend(payload)
        if received:
            self._send(Frame(FRAME_WINDOW_UPDATE, 0, stream.id, struct.pack(">I", received)))
            stream.inbound.credit(received)
        if frame.has(FLAG_END_STREAM):
            stream.end_remote()
            self._dispatch(stream)

    # -- request handling ----------------------------------------------

    def _dispatch(self, stream):
        from .http import Request

        pseudo, regular = validate_request_headers(stream.headers, stream.id)
        target = pseudo.get(":path", "*")
        path, _, query = target.partition("?")
        headers = {name: value for name, value in regular}
        if ":authority" in pseudo:
            headers.setdefault("host", pseudo[":authority"])

        request = Request(
            method=pseudo.get(":method", "GET"),
            path=path,
            query_string=query,
            headers=headers,
            body=bytes(stream.body),
            client=self.client_address,
            tls=self.tls,
            version="HTTP/2",
            trailers={name: value for name, value in stream.trailers},
        )
        try:
            response = self.app.dispatch(request)
        except Exception as exc:  # noqa: BLE001 - reported on the stream
            print(f"[http2] handler error on stream {stream.id}: {exc}")
            raise StreamError("handler failed", stream.id, ERROR_INTERNAL_ERROR) from exc
        self.send_response(stream, response, send_body=request.method != "HEAD")

    def send_response(self, stream, response, *, send_body=True):
        fields = [(":status", str(int(response.status)))]
        for name, value in (response.headers or {}).items():
            lowered = str(name).lower()
            if lowered in FORBIDDEN_HEADERS:
                continue  # Section 8.2.2: these have no meaning in HTTP/2.
            fields.append((lowered, str(value)))

        body = b"" if not send_body else self._response_body(response)
        if send_body and not response.is_stream:
            fields.append(("content-length", str(len(body))))

        block = self.encoder.encode(fields)
        end_stream = not body
        self._send_header_block(stream.id, block, end_stream)
        if body:
            self._send_data(stream, body)
        stream.end_local()

    @staticmethod
    def _response_body(response):
        if not response.is_stream:
            return bytes(response.body)
        from .http import _iter_stream_chunks

        return b"".join(_iter_stream_chunks(response.stream))

    def _send_header_block(self, stream_id, block, end_stream):
        limit = self.peer_settings[SETTINGS_MAX_FRAME_SIZE]
        first, rest = block[:limit], block[limit:]
        flags = FLAG_END_HEADERS if not rest else 0
        if end_stream:
            flags |= FLAG_END_STREAM
        self._send(Frame(FRAME_HEADERS, flags, stream_id, first))
        while rest:
            piece, rest = rest[:limit], rest[limit:]
            self._send(
                Frame(FRAME_CONTINUATION, FLAG_END_HEADERS if not rest else 0, stream_id, piece)
            )

    def _send_data(self, stream, body):
        limit = self.peer_settings[SETTINGS_MAX_FRAME_SIZE]
        offset = 0
        while offset < len(body):
            allowed = min(limit, stream.outbound.available, self.outbound.available)
            if allowed <= 0:
                # No window left and no way to wait for one in this loop.
                raise StreamError("peer window exhausted", stream.id, ERROR_FLOW_CONTROL_ERROR)
            chunk = body[offset : offset + allowed]
            offset += len(chunk)
            stream.outbound.consume(len(chunk))
            self.outbound.consume(len(chunk))
            flags = FLAG_END_STREAM if offset >= len(body) else 0
            self._send(Frame(FRAME_DATA, flags, stream.id, chunk))

    def describe(self):
        return {
            "protocol": "HTTP/2",
            "streams": [stream.describe() for stream in self.streams.values()],
            "peer_settings": dict(self.peer_settings),
            "inbound_window": self.inbound.available,
            "outbound_window": self.outbound.available,
        }


__all__ = [
    "CONNECTION_PREFACE",
    "DEFAULT_INITIAL_WINDOW_SIZE",
    "DEFAULT_MAX_FRAME_SIZE",
    "ERROR_COMPRESSION_ERROR",
    "ERROR_FLOW_CONTROL_ERROR",
    "ERROR_FRAME_SIZE_ERROR",
    "ERROR_INTERNAL_ERROR",
    "ERROR_NO_ERROR",
    "ERROR_PROTOCOL_ERROR",
    "ERROR_REFUSED_STREAM",
    "ERROR_STREAM_CLOSED",
    "FLAG_ACK",
    "FLAG_END_HEADERS",
    "FLAG_END_STREAM",
    "FLAG_PADDED",
    "FLAG_PRIORITY",
    "FORBIDDEN_HEADERS",
    "FRAME_CONTINUATION",
    "FRAME_DATA",
    "FRAME_GOAWAY",
    "FRAME_HEADERS",
    "FRAME_HEADER_SIZE",
    "FRAME_PING",
    "FRAME_PRIORITY",
    "FRAME_PUSH_PROMISE",
    "FRAME_RST_STREAM",
    "FRAME_SETTINGS",
    "FRAME_WINDOW_UPDATE",
    "FlowControlWindow",
    "Http2Connection",
    "Frame",
    "MAX_ALLOWED_FRAME_SIZE",
    "MAX_WINDOW_SIZE",
    "SETTINGS_ENABLE_PUSH",
    "SETTINGS_HEADER_TABLE_SIZE",
    "SETTINGS_INITIAL_WINDOW_SIZE",
    "SETTINGS_MAX_CONCURRENT_STREAMS",
    "SETTINGS_MAX_FRAME_SIZE",
    "SETTINGS_MAX_HEADER_LIST_SIZE",
    "Stream",
    "StreamError",
    "decode_settings",
    "encode_settings",
    "parse_frame_header",
    "strip_padding",
    "validate_request_headers",
    "validate_settings",
]

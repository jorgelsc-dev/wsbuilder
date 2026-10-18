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
import threading
from concurrent.futures import ThreadPoolExecutor, wait

from .headers import (
    combine_field_values,
    field_name_problem,
    field_value_problem,
    iter_header_fields,
)
from .hpack import Decoder, Encoder, HeaderListTooLarge, HPACKError
from .http import Response, _status_allows_body
from .logs import get_log

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
        #: Body the peer's window has no room for yet.
        self.pending_output = bytearray()
        self.pending_end_stream = False
        #: Set once the body outgrew the server limit and a 413 went out.
        self.body_rejected = False

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
        value_problem = field_value_problem(value)
        if value_problem:
            raise StreamError(f"{value_problem} in {name!r}", stream_id)
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
        name_problem = field_name_problem(name)
        if name_problem:
            raise StreamError(name_problem, stream_id)
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


def _validated_trailers(fields, stream_id):
    """Trailer fields checked like header fields; pseudo-headers are not allowed."""
    for name, value in fields:
        problem = field_name_problem(name) or field_value_problem(value)
        if problem:
            raise StreamError(f"trailer: {problem}", stream_id)
    try:
        return combine_field_values(fields)
    except ValueError as exc:
        raise StreamError(f"trailer: {exc}", stream_id) from exc


class Http2Connection:
    """Drives one HTTP/2 connection: frames in, responses out.

    Takes anything with ``recv`` and ``sendall``, so the frame logic is
    exercised in tests without a socket.

    Handlers run on a per-connection pool (bounded by the advertised
    ``max_concurrent_streams``) and streamed bodies on their own thread, so
    the frame loop keeps reading (PING, WINDOW_UPDATE, other streams) while a
    handler works. A lock serialises frame handling, HPACK encoding and
    sending; no handler runs while holding it.
    """

    #: Ids of streams closed by RST_STREAM remembered so that a HEADERS frame
    #: still in flight on one is decoded and ignored, not a protocol error.
    RECENTLY_RESET_STREAMS = 256
    #: Body bytes a streaming producer may queue ahead of the peer's windows.
    MAX_PENDING_STREAM_BYTES = 64 * 1024
    #: How long a closing connection waits for producers to finish their bodies.
    PRODUCER_GRACE_SECONDS = 5.0
    #: Handler threads per connection; streams beyond it wait in the pool's
    #: queue, so many connections cannot multiply into thousands of threads.
    MAX_HANDLER_THREADS = 16

    def __init__(
        self,
        conn,
        app,
        *,
        tls=None,
        client_address=("", 0),
        max_streams=100,
        max_body_bytes=None,
        max_header_bytes=None,
    ):
        self.conn = conn
        #: Largest request body buffered per stream; None leaves it unbounded.
        self.max_body_bytes = None if max_body_bytes is None else int(max_body_bytes)
        #: Ceiling for a header block and its decoded list; None is unbounded.
        self.max_header_bytes = None if max_header_bytes is None else int(max_header_bytes)
        self.app = app
        self.tls = tls or {}
        self.client_address = client_address
        self.streams = {}
        self._recently_reset = {}
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
        if self.max_header_bytes is not None:
            self.local_settings[SETTINGS_MAX_HEADER_LIST_SIZE] = self.max_header_bytes
        self.peer_settings = {
            SETTINGS_INITIAL_WINDOW_SIZE: DEFAULT_INITIAL_WINDOW_SIZE,
            SETTINGS_MAX_FRAME_SIZE: DEFAULT_MAX_FRAME_SIZE,
        }
        self.last_stream_id = 0
        self.goaway_sent = False
        self._buffer = bytearray()
        self._pending_headers = None
        self._closed = False
        self._lock = threading.RLock()
        self._output_ready = threading.Condition(self._lock)
        self._producers = []
        #: False once the frame loop stops: no more window credit can arrive.
        self._reading = True
        #: Set when producers must stop at once (connection error or timeout).
        self._abandoned = False
        self._max_handlers = max(1, min(int(max_streams), int(self.MAX_HANDLER_THREADS)))
        self._handler_pool = None
        self._handlers = set()

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
            with self._lock:
                self.send_settings()
            while not self._closed:
                try:
                    frame = self.next_frame()
                except TimeoutError:
                    # A client reading a long streamed body, or waiting on a
                    # slow handler, may send nothing; _read keeps any partial
                    # frame, so waiting again is safe.
                    if self._streaming() or self._handling():
                        continue
                    raise
                with self._lock:
                    self.handle_frame(frame)
                    if self._producers:
                        self._output_ready.notify_all()
        except ConnectionError_ as exc:
            self._abandoned = True
            with self._lock:
                self.send_goaway(exc.code, str(exc).encode("utf-8", "replace")[:128])
        except (ConnectionError, OSError):
            pass
        except HPACKError as exc:
            self._abandoned = True
            # A broken header block leaves the shared table unusable.
            with self._lock:
                self.send_goaway(
                    ERROR_COMPRESSION_ERROR, str(exc).encode("utf-8", "replace")[:128]
                )
        else:
            with self._lock:
                self.send_goaway(ERROR_NO_ERROR)
        finally:
            self._finish_handlers()
            self._finish_producers()

    def _handling(self):
        with self._lock:
            self._handlers = {future for future in self._handlers if not future.done()}
            return bool(self._handlers)

    def _finish_handlers(self):
        """Wait for running handlers so their responses can still go out."""
        with self._lock:
            pending = set(self._handlers)
            pool, self._handler_pool = self._handler_pool, None
        if pending:
            wait(pending, timeout=self.PRODUCER_GRACE_SECONDS)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _streaming(self):
        self._producers = [thread for thread in self._producers if thread.is_alive()]
        return bool(self._producers)

    def _finish_producers(self):
        """Let streamed bodies finish, then stop any that cannot."""
        with self._lock:
            self._reading = False
            self._output_ready.notify_all()
        for thread in list(self._producers):
            thread.join(self.PRODUCER_GRACE_SECONDS)
        self._abandoned = True
        with self._lock:
            self._output_ready.notify_all()

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
        self._release_closed_streams()

    def _release_closed_streams(self):
        """Forget closed streams so their bodies and headers can be freed.

        Nothing more is sent on a closed stream, and frames that may still
        arrive for one are handled without its state.
        """
        for stream_id, stream in list(self.streams.items()):
            if not stream.closed:
                continue
            del self.streams[stream_id]
            if stream.reset_code is not None:
                self._recently_reset[stream_id] = None
                while len(self._recently_reset) > self.RECENTLY_RESET_STREAMS:
                    del self._recently_reset[next(iter(self._recently_reset))]

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
            if delta > 0:
                self._flush_all()
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
            self._flush_all()
            return
        stream = self.streams.get(frame.stream_id)
        if stream is None:
            return
        try:
            stream.outbound.credit(increment)
        except ConnectionError_ as exc:
            raise StreamError(str(exc), frame.stream_id, ERROR_FLOW_CONTROL_ERROR) from exc
        self._flush_stream(stream)

    def _on_headers(self, frame):
        self._require_stream_id(frame, False)
        if frame.stream_id % 2 == 0:
            raise ConnectionError_("clients open odd-numbered streams only")
        stream = self.streams.get(frame.stream_id)
        if stream is None and frame.stream_id <= self.last_stream_id:
            if frame.stream_id not in self._recently_reset:
                raise ConnectionError_("stream identifiers must increase")
            # Section 5.4.2: frames on a stream we reset are ignored, but the
            # block is still decoded to keep the shared HPACK table in step.
            stream = Stream(frame.stream_id)
            stream.state = CLOSED

        payload = strip_padding(frame.payload, frame.flags, frame.stream_id)
        if frame.has(FLAG_PRIORITY):
            if len(payload) < 5:
                raise ConnectionError_("HEADERS priority block truncated", ERROR_FRAME_SIZE_ERROR)
            payload = payload[5:]

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
        self._check_header_block_size(self._pending_headers[1])
        if frame.has(FLAG_END_HEADERS):
            self._finish_header_block()

    def _on_continuation(self, frame):
        if self._pending_headers is None:
            raise ConnectionError_("CONTINUATION without an open header block")
        stream, buffer, end_stream = self._pending_headers
        if frame.stream_id != stream.id:
            raise ConnectionError_("CONTINUATION on a different stream")
        buffer.extend(frame.payload)
        self._check_header_block_size(buffer)
        if frame.has(FLAG_END_HEADERS):
            self._finish_header_block()

    def _check_header_block_size(self, buffer):
        # A block cut short cannot be decoded, and the HPACK table is shared,
        # so an oversized block can only end the connection.
        if self.max_header_bytes is not None and len(buffer) > self.max_header_bytes:
            raise ConnectionError_(
                f"header block over the {self.max_header_bytes} octet limit",
                ERROR_ENHANCE_YOUR_CALM,
            )

    def _finish_header_block(self):
        stream, buffer, end_stream = self._pending_headers
        self._pending_headers = None
        # Decoded even when the stream is doomed: the HPACK table is shared,
        # so skipping a block would desync every stream that follows.
        try:
            headers = self.decoder.decode(bytes(buffer), max_list_size=self.max_header_bytes)
        except HeaderListTooLarge:
            if stream.closed:
                return
            if not stream.headers:
                stream.open_remote(end_stream)
            self._refuse_early(
                stream, Response.text("Request Header Fields Too Large", status=431)
            )
            return
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
        elif self._declared_length_exceeds_limit(headers):
            self._reject_body(stream)

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
        if stream.body_rejected:
            # The 413 is already on its way; the rest of the body is dropped
            # and its window is not handed back.
            if frame.has(FLAG_END_STREAM):
                stream.end_remote()
            return
        if self.max_body_bytes is not None and len(stream.body) + len(payload) > self.max_body_bytes:
            self._reject_body(stream)
            return
        stream.body.extend(payload)
        if received:
            self._send(Frame(FRAME_WINDOW_UPDATE, 0, stream.id, struct.pack(">I", received)))
            stream.inbound.credit(received)
        if frame.has(FLAG_END_STREAM):
            stream.end_remote()
            self._dispatch(stream)

    def _declared_length_exceeds_limit(self, headers):
        if self.max_body_bytes is None:
            return False
        for name, value in headers:
            if name == "content-length":
                try:
                    return int(value) > self.max_body_bytes
                except ValueError:
                    return False
        return False

    def _reject_body(self, stream):
        """Answer 413 before the body is complete, then stop the upload."""
        self._refuse_early(stream, Response.text("Payload Too Large", status=413))

    def _refuse_early(self, stream, response):
        """Answer ``response`` without dispatching, then stop the upload.

        RFC 9113 section 8.1 lets a server respond early and reset the
        request stream with NO_ERROR so the client stops sending.
        """
        stream.body_rejected = True
        stream.body = bytearray()
        self.send_response(stream, response)
        if stream.closed:
            return  # The request had already ended; nothing left to stop.
        if not stream.pending_output:
            stream.state = CLOSED
            stream.reset_code = ERROR_NO_ERROR
            self.send_rst_stream(stream.id, ERROR_NO_ERROR)

    # -- request handling ----------------------------------------------

    def _dispatch(self, stream):
        from .http import Request

        pseudo, regular = validate_request_headers(stream.headers, stream.id)
        target = pseudo.get(":path", "*")
        path, _, query = target.partition("?")
        try:
            headers = combine_field_values(regular)
        except ValueError as exc:
            raise StreamError(str(exc), stream.id) from exc
        if ":authority" in pseudo:
            headers.setdefault("host", pseudo[":authority"])

        declared = headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) != len(stream.body)):
            # RFC 9113 section 8.1.1: the DATA must match the declared length.
            raise StreamError("content-length does not match the DATA received", stream.id)

        request = Request(
            method=pseudo.get(":method", "GET"),
            path=path,
            query_string=query,
            headers=headers,
            body=bytes(stream.body),
            client=self.client_address,
            tls=self.tls,
            version="HTTP/2",
            trailers=_validated_trailers(stream.trailers, stream.id),
        )
        if self._handler_pool is None:
            self._handler_pool = ThreadPoolExecutor(
                max_workers=self._max_handlers, thread_name_prefix="http2-handler"
            )
        future = self._handler_pool.submit(self._run_handler, stream, request)
        self._handlers.add(future)

    def _run_handler(self, stream, request):
        """Run the app off the frame loop, then send the answer under the lock."""
        try:
            response = self.app.dispatch(request)
        except Exception as exc:  # noqa: BLE001 - reported on the stream
            get_log(self.app).exception("http2", f"handler error on stream {stream.id}: {exc}", exc)
            with self._lock:
                if not stream.closed and not self._abandoned:
                    stream.state = CLOSED
                    stream.reset_code = ERROR_INTERNAL_ERROR
                    self.send_rst_stream(stream.id, ERROR_INTERNAL_ERROR)
                self._release_closed_streams()
            return
        try:
            with self._lock:
                if self._stream_abandoned(stream):
                    # Reset by the peer, or the connection is gone.
                    self._release_closed_streams()
                    self._close_unsent(response)
                    return
                self.send_response(stream, response, send_body=request.method != "HEAD")
                self._release_closed_streams()
        except OSError:
            pass  # send_response owns the response's producer from here on.
        except Exception as exc:  # noqa: BLE001 - reported on the stream
            # An unencodable response must not leave the stream open for ever.
            get_log(self.app).exception(
                "http2", f"response failed on stream {stream.id}: {exc}", exc
            )
            with self._lock:
                if not stream.closed and not self._abandoned:
                    stream.state = CLOSED
                    stream.reset_code = ERROR_INTERNAL_ERROR
                    self.send_rst_stream(stream.id, ERROR_INTERNAL_ERROR)
                self._release_closed_streams()

    @staticmethod
    def _close_unsent(response):
        if getattr(response, "is_stream", False):
            close = getattr(response.stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - cleanup only
                    pass

    def send_response(self, stream, response, *, send_body=True):
        producer_started = False
        try:
            producer_started = self._send_response(stream, response, send_body=send_body)
        finally:
            if not producer_started:
                self._close_unsent(response)

    def _send_response(self, stream, response, *, send_body):
        status = int(response.status)
        status_allows_body = _status_allows_body(status)
        send_body = bool(send_body and status_allows_body)
        fields = [(":status", str(int(response.status)))]
        has_content_length = False
        for name, value in iter_header_fields(response.headers):
            lowered = str(name).lower()
            if lowered in FORBIDDEN_HEADERS:
                continue  # Section 8.2.2: these have no meaning in HTTP/2.
            if lowered == "content-length":
                if 100 <= status < 200 or status == 204:
                    continue
                if send_body and not response.is_stream:
                    continue  # Buffered DATA determines its actual length.
                has_content_length = True
            fields.append((lowered, value))

        if send_body and response.is_stream:
            # Sent as it is produced: joining it first would hold an endless
            # stream forever and a large one entirely in memory.
            self._send_header_block(stream.id, self.encoder.encode(fields), False)
            producer = threading.Thread(
                target=self._produce,
                args=(stream, response),
                name=f"http2-stream-{stream.id}",
                daemon=True,
            )
            producer.start()
            self._producers.append(producer)
            return True

        body = bytes(response.body) if send_body else b""
        if status_allows_body and not response.is_stream and not has_content_length:
            # HEAD describes the representation even though DATA is omitted.
            fields.append(("content-length", str(len(response.body))))

        block = self.encoder.encode(fields)
        self._send_header_block(stream.id, block, not body)
        if body:
            self._send_data(stream, body, end_stream=True)
        else:
            stream.end_local()

    def _stream_abandoned(self, stream):
        return self._abandoned or stream.closed

    def _produce(self, stream, response):
        """Send a streamed body chunk by chunk, within the peer's windows."""
        from .http import _iter_stream_chunks

        try:
            for chunk in _iter_stream_chunks(response.stream):
                with self._lock:
                    if self._stream_abandoned(stream):
                        return
                    stream.pending_output.extend(chunk)
                    self._flush_stream(stream)
                    while len(stream.pending_output) >= self.MAX_PENDING_STREAM_BYTES:
                        if self._stream_abandoned(stream) or not self._reading:
                            return  # No window credit can arrive any more.
                        self._output_ready.wait(0.5)
            with self._lock:
                if self._stream_abandoned(stream):
                    return
                if stream.pending_output:
                    stream.pending_end_stream = True
                    self._flush_stream(stream)
                else:
                    self._send(Frame(FRAME_DATA, FLAG_END_STREAM, stream.id, b""))
                    stream.end_local()
        except OSError:
            pass  # The peer is gone; nothing more can be sent.
        except Exception as exc:  # noqa: BLE001 - reported on the stream
            get_log(self.app).exception("http2", f"stream {stream.id} body failed: {exc}", exc)
            with self._lock:
                if not stream.closed:
                    stream.state = CLOSED
                    stream.reset_code = ERROR_INTERNAL_ERROR
                    self.send_rst_stream(stream.id, ERROR_INTERNAL_ERROR)
        finally:
            close = getattr(response.stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 - cleanup only
                    get_log(self.app).warning("http2", f"stream {stream.id} cleanup failed: {exc}")
            with self._lock:
                self._release_closed_streams()

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

    def _send_data(self, stream, body, end_stream=True):
        """Send what the windows allow and queue the rest.

        Blocking here would stall every other stream on the connection, so
        the remainder waits for a WINDOW_UPDATE and is flushed from the frame
        loop instead.
        """
        stream.pending_output.extend(body)
        stream.pending_end_stream = end_stream
        self._flush_stream(stream)

    def _flush_stream(self, stream):
        limit = self.peer_settings[SETTINGS_MAX_FRAME_SIZE]
        while stream.pending_output:
            allowed = min(limit, stream.outbound.available, self.outbound.available)
            if allowed <= 0:
                return False
            chunk = bytes(stream.pending_output[:allowed])
            del stream.pending_output[:allowed]
            stream.outbound.consume(len(chunk))
            self.outbound.consume(len(chunk))
            last = not stream.pending_output and stream.pending_end_stream
            self._send(Frame(FRAME_DATA, FLAG_END_STREAM if last else 0, stream.id, chunk))
        if stream.pending_end_stream:
            stream.pending_end_stream = False
            stream.end_local()
        return True

    def _flush_all(self):
        for stream in list(self.streams.values()):
            if stream.pending_output and not stream.closed:
                self._flush_stream(stream)

    @property
    def pending_output_bytes(self):
        return sum(len(s.pending_output) for s in self.streams.values())

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

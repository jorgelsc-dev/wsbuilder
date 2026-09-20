"""QUIC frames (RFC 9000 section 19).

A packet's payload is a sequence of frames with no separators: each one's
type tells the parser how much to read. Frames, not packets, are what gets
retransmitted -- a lost packet's frames are re-sent in a new packet with a
new number, which is why acknowledgements talk about packets while delivery
guarantees talk about frames.
"""

from .varint import decode_varint, encode_varint

FRAME_PADDING = 0x00
FRAME_PING = 0x01
FRAME_ACK = 0x02
FRAME_ACK_ECN = 0x03
FRAME_RESET_STREAM = 0x04
FRAME_STOP_SENDING = 0x05
FRAME_CRYPTO = 0x06
FRAME_NEW_TOKEN = 0x07
FRAME_STREAM_BASE = 0x08  # 0x08 to 0x0f, the low bits are flags
FRAME_MAX_DATA = 0x10
FRAME_MAX_STREAM_DATA = 0x11
FRAME_MAX_STREAMS_BIDI = 0x12
FRAME_MAX_STREAMS_UNI = 0x13
FRAME_DATA_BLOCKED = 0x14
FRAME_STREAM_DATA_BLOCKED = 0x15
FRAME_STREAMS_BLOCKED_BIDI = 0x16
FRAME_STREAMS_BLOCKED_UNI = 0x17
FRAME_NEW_CONNECTION_ID = 0x18
FRAME_RETIRE_CONNECTION_ID = 0x19
FRAME_PATH_CHALLENGE = 0x1A
FRAME_PATH_RESPONSE = 0x1B
FRAME_CONNECTION_CLOSE = 0x1C
FRAME_CONNECTION_CLOSE_APP = 0x1D
FRAME_HANDSHAKE_DONE = 0x1E

STREAM_FIN = 0x01
STREAM_LEN = 0x02
STREAM_OFF = 0x04

#: Transport error codes (RFC 9000 section 20.1).
NO_ERROR = 0x00
INTERNAL_ERROR = 0x01
CONNECTION_REFUSED = 0x02
FLOW_CONTROL_ERROR = 0x03
STREAM_LIMIT_ERROR = 0x04
STREAM_STATE_ERROR = 0x05
FINAL_SIZE_ERROR = 0x06
FRAME_ENCODING_ERROR = 0x07
TRANSPORT_PARAMETER_ERROR = 0x08
PROTOCOL_VIOLATION = 0x0A
CRYPTO_BUFFER_EXCEEDED = 0x0D


class QuicFrameError(ValueError):
    """A frame that cannot be parsed."""


class CryptoFrame:
    __slots__ = ("offset", "data")

    def __init__(self, offset, data):
        self.offset = int(offset)
        self.data = bytes(data)

    def serialize(self):
        return (
            encode_varint(FRAME_CRYPTO)
            + encode_varint(self.offset)
            + encode_varint(len(self.data))
            + self.data
        )

    def __repr__(self):
        return f"<CRYPTO offset={self.offset} len={len(self.data)}>"


class StreamFrame:
    __slots__ = ("stream_id", "offset", "data", "fin")

    def __init__(self, stream_id, offset, data, fin=False):
        self.stream_id = int(stream_id)
        self.offset = int(offset)
        self.data = bytes(data)
        self.fin = bool(fin)

    def serialize(self, *, with_length=True):
        frame_type = FRAME_STREAM_BASE | STREAM_LEN if with_length else FRAME_STREAM_BASE
        if self.offset:
            frame_type |= STREAM_OFF
        if self.fin:
            frame_type |= STREAM_FIN
        out = encode_varint(frame_type) + encode_varint(self.stream_id)
        if self.offset:
            out += encode_varint(self.offset)
        if with_length:
            out += encode_varint(len(self.data))
        return out + self.data

    def __repr__(self):
        return (
            f"<STREAM id={self.stream_id} offset={self.offset} "
            f"len={len(self.data)} fin={self.fin}>"
        )


class AckFrame:
    __slots__ = ("largest", "delay", "ranges", "ecn")

    def __init__(self, largest, delay=0, ranges=None, ecn=None):
        self.largest = int(largest)
        self.delay = int(delay)
        #: Additional (gap, length) pairs after the first range.
        self.ranges = list(ranges or [])
        self.ecn = ecn

    def serialize(self):
        first_range = self.ranges[0] if self.ranges else 0
        extra = self.ranges[1:] if self.ranges else []
        out = (
            encode_varint(FRAME_ACK)
            + encode_varint(self.largest)
            + encode_varint(self.delay)
            + encode_varint(len(extra))
            + encode_varint(first_range)
        )
        for gap, length in extra:
            out += encode_varint(gap) + encode_varint(length)
        return out

    def __repr__(self):
        return f"<ACK largest={self.largest} ranges={len(self.ranges)}>"


class ConnectionCloseFrame:
    __slots__ = ("error_code", "frame_type", "reason", "application")

    def __init__(self, error_code, reason="", frame_type=0, application=False):
        self.error_code = int(error_code)
        self.reason = reason if isinstance(reason, bytes) else str(reason).encode("utf-8")
        self.frame_type = int(frame_type)
        self.application = bool(application)

    def serialize(self):
        kind = FRAME_CONNECTION_CLOSE_APP if self.application else FRAME_CONNECTION_CLOSE
        out = encode_varint(kind) + encode_varint(self.error_code)
        if not self.application:
            out += encode_varint(self.frame_type)
        return out + encode_varint(len(self.reason)) + self.reason

    def __repr__(self):
        return f"<CONNECTION_CLOSE code=0x{self.error_code:x} reason={self.reason!r}>"


class MaxDataFrame:
    __slots__ = ("maximum",)

    def __init__(self, maximum):
        self.maximum = int(maximum)

    def serialize(self):
        return encode_varint(FRAME_MAX_DATA) + encode_varint(self.maximum)


class MaxStreamDataFrame:
    __slots__ = ("stream_id", "maximum")

    def __init__(self, stream_id, maximum):
        self.stream_id = int(stream_id)
        self.maximum = int(maximum)

    def serialize(self):
        return (
            encode_varint(FRAME_MAX_STREAM_DATA)
            + encode_varint(self.stream_id)
            + encode_varint(self.maximum)
        )


class MaxStreamsFrame:
    __slots__ = ("maximum", "bidirectional")

    def __init__(self, maximum, bidirectional=True):
        self.maximum = int(maximum)
        self.bidirectional = bool(bidirectional)

    def serialize(self):
        kind = FRAME_MAX_STREAMS_BIDI if self.bidirectional else FRAME_MAX_STREAMS_UNI
        return encode_varint(kind) + encode_varint(self.maximum)


class NewConnectionIdFrame:
    __slots__ = ("sequence_number", "retire_prior_to", "connection_id", "stateless_reset_token")

    def __init__(self, sequence_number, connection_id, stateless_reset_token, retire_prior_to=0):
        self.sequence_number = int(sequence_number)
        self.retire_prior_to = int(retire_prior_to)
        self.connection_id = bytes(connection_id)
        self.stateless_reset_token = bytes(stateless_reset_token)

    def serialize(self):
        return (
            encode_varint(FRAME_NEW_CONNECTION_ID)
            + encode_varint(self.sequence_number)
            + encode_varint(self.retire_prior_to)
            + bytes([len(self.connection_id)])
            + self.connection_id
            + self.stateless_reset_token
        )


class SimpleFrame:
    """PING, HANDSHAKE_DONE and the other frames that carry nothing."""

    __slots__ = ("frame_type",)

    def __init__(self, frame_type):
        self.frame_type = int(frame_type)

    def serialize(self):
        return encode_varint(self.frame_type)

    def __repr__(self):
        return f"<frame 0x{self.frame_type:x}>"


class PathFrame:
    __slots__ = ("frame_type", "data")

    def __init__(self, frame_type, data):
        self.frame_type = int(frame_type)
        self.data = bytes(data)
        if len(self.data) != 8:
            raise ValueError("path challenge data is 8 octets")

    def serialize(self):
        return encode_varint(self.frame_type) + self.data


def parse_frames(payload):
    """Decode every frame in a packet payload, in order."""
    data = bytes(payload)
    offset = 0
    frames = []
    while offset < len(data):
        frame_type, offset = decode_varint(data, offset)

        if frame_type == FRAME_PADDING:
            # Runs of padding are common; swallow them in one step.
            while offset < len(data) and data[offset] == 0:
                offset += 1
            continue
        if frame_type in (FRAME_PING, FRAME_HANDSHAKE_DONE):
            frames.append(SimpleFrame(frame_type))
            continue
        if frame_type in (FRAME_ACK, FRAME_ACK_ECN):
            frame, offset = _parse_ack(data, offset, frame_type)
            frames.append(frame)
            continue
        if frame_type == FRAME_CRYPTO:
            crypto_offset, offset = decode_varint(data, offset)
            length, offset = decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise QuicFrameError("CRYPTO frame runs past the packet")
            frames.append(CryptoFrame(crypto_offset, data[offset:end]))
            offset = end
            continue
        if FRAME_STREAM_BASE <= frame_type <= FRAME_STREAM_BASE | 0x07:
            frame, offset = _parse_stream(data, offset, frame_type)
            frames.append(frame)
            continue
        if frame_type in (FRAME_CONNECTION_CLOSE, FRAME_CONNECTION_CLOSE_APP):
            frame, offset = _parse_connection_close(data, offset, frame_type)
            frames.append(frame)
            continue
        if frame_type == FRAME_MAX_DATA:
            maximum, offset = decode_varint(data, offset)
            frames.append(MaxDataFrame(maximum))
            continue
        if frame_type == FRAME_MAX_STREAM_DATA:
            stream_id, offset = decode_varint(data, offset)
            maximum, offset = decode_varint(data, offset)
            frames.append(MaxStreamDataFrame(stream_id, maximum))
            continue
        if frame_type in (FRAME_MAX_STREAMS_BIDI, FRAME_MAX_STREAMS_UNI):
            maximum, offset = decode_varint(data, offset)
            frames.append(MaxStreamsFrame(maximum, frame_type == FRAME_MAX_STREAMS_BIDI))
            continue
        if frame_type in (FRAME_PATH_CHALLENGE, FRAME_PATH_RESPONSE):
            end = offset + 8
            if end > len(data):
                raise QuicFrameError("path frame runs past the packet")
            frames.append(PathFrame(frame_type, data[offset:end]))
            offset = end
            continue
        if frame_type == FRAME_NEW_TOKEN:
            length, offset = decode_varint(data, offset)
            offset += length
            continue
        if frame_type == FRAME_NEW_CONNECTION_ID:
            offset = _skip_new_connection_id(data, offset)
            continue
        if frame_type == FRAME_RETIRE_CONNECTION_ID:
            _value, offset = decode_varint(data, offset)
            continue
        if frame_type in (FRAME_RESET_STREAM, FRAME_STOP_SENDING):
            offset = _skip_stream_control(data, offset, frame_type)
            continue
        if frame_type in (
            FRAME_DATA_BLOCKED,
            FRAME_STREAMS_BLOCKED_BIDI,
            FRAME_STREAMS_BLOCKED_UNI,
        ):
            _value, offset = decode_varint(data, offset)
            continue
        if frame_type == FRAME_STREAM_DATA_BLOCKED:
            _stream, offset = decode_varint(data, offset)
            _limit, offset = decode_varint(data, offset)
            continue
        raise QuicFrameError(f"unknown frame type 0x{frame_type:x}")
    return frames


def _parse_ack(data, offset, frame_type):
    largest, offset = decode_varint(data, offset)
    delay, offset = decode_varint(data, offset)
    range_count, offset = decode_varint(data, offset)
    first_range, offset = decode_varint(data, offset)
    ranges = [first_range]
    for _ in range(range_count):
        gap, offset = decode_varint(data, offset)
        length, offset = decode_varint(data, offset)
        ranges.append((gap, length))
    ecn = None
    if frame_type == FRAME_ACK_ECN:
        ect0, offset = decode_varint(data, offset)
        ect1, offset = decode_varint(data, offset)
        ce, offset = decode_varint(data, offset)
        ecn = (ect0, ect1, ce)
    frame = AckFrame(largest, delay, ranges, ecn)
    return frame, offset


def _parse_stream(data, offset, frame_type):
    stream_id, offset = decode_varint(data, offset)
    stream_offset = 0
    if frame_type & STREAM_OFF:
        stream_offset, offset = decode_varint(data, offset)
    if frame_type & STREAM_LEN:
        length, offset = decode_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise QuicFrameError("STREAM frame runs past the packet")
    else:
        # Without a length the frame extends to the end of the packet.
        end = len(data)
    frame = StreamFrame(stream_id, stream_offset, data[offset:end], bool(frame_type & STREAM_FIN))
    return frame, end


def _parse_connection_close(data, offset, frame_type):
    error_code, offset = decode_varint(data, offset)
    inner_type = 0
    if frame_type == FRAME_CONNECTION_CLOSE:
        inner_type, offset = decode_varint(data, offset)
    length, offset = decode_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise QuicFrameError("CONNECTION_CLOSE reason runs past the packet")
    frame = ConnectionCloseFrame(
        error_code,
        data[offset:end],
        inner_type,
        application=frame_type == FRAME_CONNECTION_CLOSE_APP,
    )
    return frame, end


def _skip_new_connection_id(data, offset):
    _sequence, offset = decode_varint(data, offset)
    _retire, offset = decode_varint(data, offset)
    if offset >= len(data):
        raise QuicFrameError("truncated NEW_CONNECTION_ID")
    length = data[offset]
    offset += 1 + length + 16
    if offset > len(data):
        raise QuicFrameError("NEW_CONNECTION_ID runs past the packet")
    return offset


def _skip_stream_control(data, offset, frame_type):
    _stream, offset = decode_varint(data, offset)
    _code, offset = decode_varint(data, offset)
    if frame_type == FRAME_RESET_STREAM:
        _final_size, offset = decode_varint(data, offset)
    return offset


def serialize_frames(frames):
    return b"".join(frame.serialize() for frame in frames)


def pad_to(payload, size):
    """PADDING out to ``size``, as a client's first flight must."""
    payload = bytes(payload)
    if len(payload) >= size:
        return payload
    return payload + bytes(size - len(payload))


__all__ = [
    "AckFrame",
    "ConnectionCloseFrame",
    "CryptoFrame",
    "FRAME_ACK",
    "FRAME_CONNECTION_CLOSE",
    "FRAME_CRYPTO",
    "FRAME_HANDSHAKE_DONE",
    "FRAME_PADDING",
    "FRAME_PING",
    "FRAME_STREAM_BASE",
    "MaxDataFrame",
    "MaxStreamDataFrame",
    "MaxStreamsFrame",
    "NewConnectionIdFrame",
    "NO_ERROR",
    "PROTOCOL_VIOLATION",
    "PathFrame",
    "QuicFrameError",
    "STREAM_FIN",
    "SimpleFrame",
    "StreamFrame",
    "TRANSPORT_PARAMETER_ERROR",
    "pad_to",
    "parse_frames",
    "serialize_frames",
]

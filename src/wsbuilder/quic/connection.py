"""A QUIC server connection: packets in, packets out (RFC 9000, RFC 9001).

Encryption happens at three levels, each with its own keys and its own
packet number space: Initial, Handshake and 1-RTT. A datagram may carry one
packet from several levels at once, which is how a server sends its whole
first flight without waiting.

Streams are reassembled here rather than in HTTP/3, because ordering is a
transport guarantee: bytes arrive with offsets and may repeat or arrive out
of order, and only once a stream is contiguous does the layer above see it.
"""

import threading
import time

from . import frames as qframes
from .address import AmplificationLimit, PathValidator
from .crypto import (
    AEAD_TAG_SIZE,
    PacketKeys,
    apply_header_protection,
    decode_packet_number,
    initial_secrets,
    remove_header_protection,
)
from .packet import (
    MIN_INITIAL_DATAGRAM,
    PACKET_HANDSHAKE,
    PACKET_INITIAL,
    QuicPacketError,
    build_long_header,
    build_short_header,
    is_long_header,
    new_connection_id,
    parse_long_header,
)
from .recovery import LossRecovery, SentPacket
from .tls import ServerHandshake, TLSError
from .varint import decode_varint, encode_varint

LEVEL_INITIAL = "initial"
LEVEL_HANDSHAKE = "handshake"
LEVEL_APPLICATION = "application"

VERSION_1 = 0x00000001
MAX_DATAGRAM_SIZE = 1350
#: Largest stream payload put in one frame, leaving room for the packet header.
STREAM_CHUNK_SIZE = 1000

#: Receive windows we advertise and keep topping up as data is delivered
#: (RFC 9000 section 4). A peer that writes past them is closed.
STREAM_RECEIVE_WINDOW = 262144
CONNECTION_RECEIVE_WINDOW = 1048576
#: Peer-initiated streams of each kind (bidirectional, unidirectional).
MAX_PEER_STREAMS = 100
#: CRYPTO bytes buffered per level before the handshake is refused.
MAX_CRYPTO_BUFFER = 65536
#: Bytes a 1-RTT packet adds around its STREAM data (header, frame, AEAD tag),
#: used to ask the congestion controller before building the packet.
PACKET_OVERHEAD_ESTIMATE = 64

# Transport error codes (RFC 9000 section 20.1).
ERROR_FLOW_CONTROL = 0x03
ERROR_STREAM_LIMIT = 0x04
ERROR_CRYPTO_BUFFER_EXCEEDED = 0x0D

# Transport parameter identifiers (RFC 9000 section 18.2).
TP_ORIGINAL_DESTINATION_CONNECTION_ID = 0x00
TP_MAX_IDLE_TIMEOUT = 0x01
TP_MAX_UDP_PAYLOAD_SIZE = 0x03
TP_INITIAL_MAX_DATA = 0x04
TP_INITIAL_MAX_STREAM_DATA_BIDI_LOCAL = 0x05
TP_INITIAL_MAX_STREAM_DATA_BIDI_REMOTE = 0x06
TP_INITIAL_MAX_STREAM_DATA_UNI = 0x07
TP_INITIAL_MAX_STREAMS_BIDI = 0x08
TP_INITIAL_MAX_STREAMS_UNI = 0x09
TP_INITIAL_SOURCE_CONNECTION_ID = 0x0F


def encode_transport_parameters(values):
    out = bytearray()
    for key, value in values.items():
        payload = value if isinstance(value, bytes) else encode_varint(value)
        out += encode_varint(key) + encode_varint(len(payload)) + payload
    return bytes(out)


def decode_transport_parameters(data):
    data = bytes(data or b"")
    offset = 0
    values = {}
    while offset < len(data):
        key, offset = decode_varint(data, offset)
        length, offset = decode_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise QuicPacketError("transport parameter runs past the extension")
        values[key] = data[offset:end]
        offset = end
    return values


class StreamBuffer:
    """Reassembles one direction of a stream from offset-tagged pieces.

    With ``retain=False`` delivered bytes are only counted, not kept: the
    caller consumes them from ``add``'s return value, so a long-lived stream
    does not hold its whole history in memory.
    """

    def __init__(self, *, retain=True, limit=None):
        self.retain = bool(retain)
        self.data = bytearray()
        #: Contiguous bytes delivered so far.
        self.received = 0
        #: One past the highest byte the peer has sent.
        self.highest = 0
        #: Receive limit currently advertised for this stream, if any.
        self.limit = limit
        self.pending = {}
        self.pending_bytes = 0
        self.final_size = None

    @property
    def complete(self):
        return self.final_size is not None and self.received >= self.final_size

    def add(self, offset, payload, fin=False):
        """Returns the newly contiguous bytes, which may be empty."""
        offset = int(offset)
        self.highest = max(self.highest, offset + len(payload))
        if fin:
            self.final_size = offset + len(payload)
        if offset > self.received:
            # Arrived early: hold it until the gap in front is filled.
            previous = self.pending.get(offset)
            self.pending_bytes += len(payload) - (len(previous) if previous else 0)
            self.pending[offset] = bytes(payload)
            return b""
        start = self.received - offset
        fresh = bytes(payload[start:]) if start < len(payload) else b""
        grew = bytearray(fresh)
        self.received += len(fresh)
        # Held pieces may overlap what just arrived; trim instead of
        # requiring an exact offset match.
        while self.pending:
            ready = [held for held in self.pending if held <= self.received]
            if not ready:
                break
            for held in sorted(ready):
                queued = self.pending.pop(held)
                self.pending_bytes -= len(queued)
                skip = self.received - held
                if skip < len(queued):
                    tail = queued[skip:]
                    grew.extend(tail)
                    self.received += len(tail)
        if self.retain:
            self.data.extend(grew)
        return bytes(grew)


class QuicConnection:
    """One QUIC connection, driven by whole datagrams."""

    def __init__(
        self,
        certificate_chain_der,
        private_key,
        *,
        alpn_protocols=("h3",),
        client_address=("", 0),
        on_stream_data=None,
    ):
        self.client_address = client_address
        self.on_stream_data = on_stream_data
        self.host_cid = new_connection_id(8)
        self.peer_cid = b""
        self.original_dcid = None
        self.keys = {}
        self.packet_numbers = {LEVEL_INITIAL: 0, LEVEL_HANDSHAKE: 0, LEVEL_APPLICATION: 0}
        self.largest_received = {LEVEL_INITIAL: -1, LEVEL_HANDSHAKE: -1, LEVEL_APPLICATION: -1}
        self.crypto_buffers = {level: StreamBuffer() for level in
                               (LEVEL_INITIAL, LEVEL_HANDSHAKE, LEVEL_APPLICATION)}
        self.streams = {}
        self._recv_max_data = CONNECTION_RECEIVE_WINDOW
        self._recv_highest_total = 0
        self._recv_consumed = 0
        # Stream bytes queued from other threads (a WebSocket handler, a
        # streaming response) and sent by the thread that owns the socket.
        self._outbound_lock = threading.Lock()
        #: Notified whenever queued bytes leave, so producers can wait for room.
        self._outbound_room = threading.Condition(self._outbound_lock)
        self._outbound = []
        self._queued_bytes = {}
        self._send_offsets = {}
        # Send-side flow control (RFC 9000 section 4): what the peer allows.
        # None means unlimited, used only when the peer sent no transport
        # parameters at all.
        self._peer_max_data = None
        self._peer_stream_max = {}
        self._sent_data_total = 0
        self._blocked_reported = {}
        self._reset_streams = set()
        self.handshake_complete = False
        self.closed = False
        self.alpn = None
        self.peer_transport_parameters = {}
        self._pending_acks = {level: [] for level in self.packet_numbers}
        self.recovery = LossRecovery(
            levels=(LEVEL_INITIAL, LEVEL_HANDSHAKE, LEVEL_APPLICATION)
        )
        self.amplification = AmplificationLimit()
        self.paths = PathValidator()
        self._tls = ServerHandshake(
            certificate_chain_der,
            private_key,
            alpn_protocols=alpn_protocols,
            transport_parameters=b"",
        )

    # -- keys ----------------------------------------------------------

    def _install_initial_keys(self, destination_cid):
        client_secret, server_secret = initial_secrets(destination_cid)
        self.keys[LEVEL_INITIAL] = {
            "recv": PacketKeys(client_secret),
            "send": PacketKeys(server_secret),
        }

    def _install(self, level, client_secret, server_secret):
        self.keys[level] = {
            "recv": PacketKeys(client_secret),
            "send": PacketKeys(server_secret),
        }

    def _transport_parameters(self):
        values = {
            TP_INITIAL_SOURCE_CONNECTION_ID: self.host_cid,
            TP_INITIAL_MAX_DATA: CONNECTION_RECEIVE_WINDOW,
            TP_INITIAL_MAX_STREAM_DATA_BIDI_LOCAL: STREAM_RECEIVE_WINDOW,
            TP_INITIAL_MAX_STREAM_DATA_BIDI_REMOTE: STREAM_RECEIVE_WINDOW,
            TP_INITIAL_MAX_STREAM_DATA_UNI: STREAM_RECEIVE_WINDOW,
            TP_INITIAL_MAX_STREAMS_BIDI: MAX_PEER_STREAMS,
            TP_INITIAL_MAX_STREAMS_UNI: MAX_PEER_STREAMS,
            TP_MAX_IDLE_TIMEOUT: 30000,
            TP_MAX_UDP_PAYLOAD_SIZE: MAX_DATAGRAM_SIZE,
        }
        if self.original_dcid:
            values[TP_ORIGINAL_DESTINATION_CONNECTION_ID] = self.original_dcid
        return encode_transport_parameters(values)

    # -- receiving -----------------------------------------------------

    def receive_datagram(self, datagram, address=None):
        """Process one UDP datagram, returning the datagrams to send back."""
        data = bytes(datagram)
        self.amplification.on_received(len(data))
        offset = 0
        outgoing = []
        if address is not None and tuple(address) != tuple(self.client_address):
            outgoing.extend(self._on_address_change(address))
        while offset < len(data) and not self.closed:
            if not is_long_header(data[offset]):
                consumed = self._receive_short(data[offset:], outgoing)
                offset += consumed if consumed else len(data) - offset
                continue
            try:
                header = parse_long_header(data, offset)
            except QuicPacketError:
                break
            self._receive_long(data, header, outgoing)
            if header.packet_length <= 0:
                break
            offset += header.packet_length
        return self._flush(outgoing)

    def _receive_long(self, data, header, outgoing):
        if header.packet_type == PACKET_INITIAL and LEVEL_INITIAL not in self.keys:
            self.original_dcid = header.destination_cid
            self.peer_cid = header.source_cid
            self._install_initial_keys(header.destination_cid)

        level = {PACKET_INITIAL: LEVEL_INITIAL, PACKET_HANDSHAKE: LEVEL_HANDSHAKE}.get(
            header.packet_type
        )
        if level is None or level not in self.keys:
            return  # 0-RTT and Retry are not served.

        start = header.payload_offset - (header.packet_length - header.length)
        whole = data[start : start + header.packet_length]
        pn_offset = header.payload_offset - start
        self._open_packet(level, whole, pn_offset, outgoing)

    def _receive_short(self, packet, outgoing):
        if LEVEL_APPLICATION not in self.keys:
            return 0
        pn_offset = 1 + len(self.host_cid)
        self._open_packet(LEVEL_APPLICATION, packet, pn_offset, outgoing)
        return 0

    def _open_packet(self, level, packet, pn_offset, outgoing):
        keys = self.keys[level]["recv"]
        try:
            cleaned, truncated, pn_length = remove_header_protection(keys, packet, pn_offset)
        except ValueError:
            return
        number = decode_packet_number(truncated, pn_length, self.largest_received[level])
        header = cleaned[: pn_offset + pn_length]
        body = cleaned[pn_offset + pn_length :]
        try:
            payload = keys.open(number, header, body)
        except Exception:
            # An un-openable packet is dropped, not fatal: it may be an
            # injection, or a packet for keys we have since replaced.
            return
        self.largest_received[level] = max(self.largest_received[level], number)
        self._pending_acks[level].append(number)
        self._handle_frames(level, qframes.parse_frames(payload), outgoing)

    def _handle_frames(self, level, parsed, outgoing):
        for frame in parsed:
            if self.closed:
                return
            if isinstance(frame, qframes.CryptoFrame):
                if frame.offset + len(frame.data) > MAX_CRYPTO_BUFFER:
                    self._close_with_error(
                        level, ERROR_CRYPTO_BUFFER_EXCEEDED, "CRYPTO data exceeds buffer", outgoing
                    )
                    return
                fresh = self.crypto_buffers[level].add(frame.offset, frame.data)
                if fresh:
                    self._advance_handshake(level, outgoing)
            elif isinstance(frame, qframes.StreamFrame):
                self._on_stream_frame(frame, outgoing)
            elif isinstance(frame, qframes.PathFrame):
                self._on_path_frame(frame, outgoing)
            elif isinstance(frame, qframes.AckFrame):
                self._on_ack(level, frame, outgoing)
            elif isinstance(frame, qframes.MaxDataFrame):
                if self._peer_max_data is not None:
                    self._peer_max_data = max(self._peer_max_data, frame.maximum)
            elif isinstance(frame, qframes.MaxStreamDataFrame):
                current = self._stream_send_limit(frame.stream_id)
                if current is not None:
                    self._peer_stream_max[frame.stream_id] = max(current, frame.maximum)
            elif isinstance(frame, qframes.ConnectionCloseFrame):
                self.closed = True
            elif isinstance(frame, qframes.SimpleFrame):
                if frame.frame_type == qframes.FRAME_HANDSHAKE_DONE:
                    self.handshake_complete = True

    def _on_address_change(self, address):
        """A packet from somewhere new: probe the path before trusting it.

        The connection is not moved yet. Until the peer echoes the challenge
        from that address, treating it as the peer's would let anyone
        redirect the traffic by spoofing one packet.
        """
        if self.paths.is_validated(address):
            self.client_address = tuple(address)
            return []
        data = self.paths.challenge(address)
        self._probe_address = tuple(address)
        return [(LEVEL_APPLICATION, [qframes.PathFrame(qframes.FRAME_PATH_CHALLENGE, data)])]

    def _on_path_frame(self, frame, outgoing):
        if frame.frame_type == qframes.FRAME_PATH_CHALLENGE:
            # Echoing it is how we let the peer validate its own new path.
            outgoing.append(
                (LEVEL_APPLICATION, [qframes.PathFrame(qframes.FRAME_PATH_RESPONSE, frame.data)])
            )
            return
        candidate = getattr(self, "_probe_address", None)
        if candidate and self.paths.on_response(frame.data, candidate):
            self.client_address = candidate
            self.amplification.validate()
            self._probe_address = None

    def _on_ack(self, level, frame, outgoing):
        _acked, lost = self.recovery.on_ack_received(
            level, frame.largest, frame.ranges, ack_delay=frame.delay / 1_000_000
        )
        self._retransmit(level, lost, outgoing)

    def _retransmit(self, level, lost, outgoing):
        """Resend what a lost packet carried, not the packet itself.

        A packet number is used once, so recovery means putting the frames
        into a new packet. Frames that only described the past -- ACKs,
        padding -- are dropped: repeating them would say nothing new.
        """
        frames = []
        for packet in lost:
            frames.extend(
                frame
                for frame in packet.frames
                if not isinstance(frame, qframes.AckFrame)
            )
        if frames:
            outgoing.append((level, frames))
        return frames

    def on_timeout(self, now=None):
        """Let a caller drive the loss timer. Returns datagrams to send."""
        if self.closed:
            return []
        probes = self.recovery.on_timeout(now=now)
        outgoing = []
        by_level = {}
        for packet in probes:
            frames = [f for f in packet.frames if not isinstance(f, qframes.AckFrame)]
            if frames:
                by_level.setdefault(packet.level, []).extend(frames)
        for level, frames in by_level.items():
            outgoing.append((level, frames))
        return self._flush(outgoing) if outgoing else []

    def loss_timer(self):
        """When on_timeout should next be called, or None if nothing is due."""
        return self.recovery.loss_detection_timer()

    def _advance_handshake(self, level, outgoing):
        buffer = self.crypto_buffers[level]
        if level == LEVEL_INITIAL and LEVEL_HANDSHAKE not in self.keys:
            message = bytes(buffer.data)
            if len(message) < 4:
                return
            body_length = int.from_bytes(message[1:4], "big")
            if len(message) < 4 + body_length:
                return  # The ClientHello spans more packets.
            self._tls.transport_parameters = self._transport_parameters()
            server_hello, flight, secrets = self._tls.handle_client_hello(
                message[: 4 + body_length]
            )
            self.alpn = self._tls.selected_alpn
            self.peer_transport_parameters = decode_transport_parameters(
                self._tls.peer_transport_parameters
            )
            self._peer_max_data = self._peer_parameter(TP_INITIAL_MAX_DATA)
            self._install(
                LEVEL_HANDSHAKE, secrets["client_handshake"], secrets["server_handshake"]
            )
            self._install(
                LEVEL_APPLICATION, secrets["client_application"], secrets["server_application"]
            )
            outgoing.append((LEVEL_INITIAL, [qframes.CryptoFrame(0, server_hello)]))
            outgoing.append((LEVEL_HANDSHAKE, [qframes.CryptoFrame(0, flight)]))
        elif level == LEVEL_HANDSHAKE:
            message = bytes(buffer.data)
            if len(message) >= 4 and len(message) >= 4 + int.from_bytes(message[1:4], "big"):
                try:
                    self._tls.verify_client_finished(message)
                except TLSError:
                    self.closed = True
                    return
                self.handshake_complete = True
                # Finishing the handshake proves the peer is where it claims.
                self.amplification.validate()
                outgoing.append(
                    (LEVEL_APPLICATION, [qframes.SimpleFrame(qframes.FRAME_HANDSHAKE_DONE)])
                )

    def _on_stream_frame(self, frame, outgoing):
        buffer = self.streams.get(frame.stream_id)
        if buffer is None:
            if (frame.stream_id >> 2) >= MAX_PEER_STREAMS:
                self._close_with_error(
                    LEVEL_APPLICATION, ERROR_STREAM_LIMIT, "stream limit exceeded", outgoing
                )
                return
            buffer = StreamBuffer(retain=False, limit=STREAM_RECEIVE_WINDOW)
            self.streams[frame.stream_id] = buffer
        end = frame.offset + len(frame.data)
        grown = max(0, end - buffer.highest)
        if end > buffer.limit or self._recv_highest_total + grown > self._recv_max_data:
            self._close_with_error(
                LEVEL_APPLICATION, ERROR_FLOW_CONTROL, "data exceeds the receive window", outgoing
            )
            return
        self._recv_highest_total += grown
        fresh = buffer.add(frame.offset, frame.data, frame.fin)
        self._recv_consumed += len(fresh)
        self._replenish_windows(frame.stream_id, buffer, outgoing)
        if self.on_stream_data is None:
            return
        reply = self.on_stream_data(self, frame.stream_id, fresh, buffer.complete)
        if reply:
            outgoing.append((LEVEL_APPLICATION, reply))

    def _replenish_windows(self, stream_id, buffer, outgoing):
        """Advertise more room once half of a window has been delivered."""
        frames = []
        if buffer.final_size is None and buffer.limit - buffer.received < STREAM_RECEIVE_WINDOW // 2:
            buffer.limit = buffer.received + STREAM_RECEIVE_WINDOW
            frames.append(qframes.MaxStreamDataFrame(stream_id, buffer.limit))
        if self._recv_max_data - self._recv_consumed < CONNECTION_RECEIVE_WINDOW // 2:
            self._recv_max_data = self._recv_consumed + CONNECTION_RECEIVE_WINDOW
            frames.append(qframes.MaxDataFrame(self._recv_max_data))
        if frames:
            outgoing.append((LEVEL_APPLICATION, frames))

    def _close_with_error(self, level, code, reason, outgoing):
        """Send CONNECTION_CLOSE for a transport error and stop processing."""
        outgoing.append((level, [qframes.ConnectionCloseFrame(code, reason)]))
        self.closed = True

    # -- send-side flow control ----------------------------------------

    def _peer_parameter(self, key):
        """A peer transport parameter as an int; absent means 0 (RFC 9000 18.2).

        Returns None (no limit) when the peer sent no parameters at all.
        """
        if not self.peer_transport_parameters:
            return None
        raw = self.peer_transport_parameters.get(key)
        if raw is None:
            return 0
        try:
            value, _offset = decode_varint(bytes(raw), 0)
        except Exception:
            return 0
        return int(value)

    def _stream_send_limit(self, stream_id):
        """Highest offset the peer lets us send on ``stream_id``, or None."""
        if stream_id in self._peer_stream_max:
            return self._peer_stream_max[stream_id]
        if not self.peer_transport_parameters:
            return None
        kind = int(stream_id) & 0x03
        if kind == 0:
            # Client-initiated bidirectional: the peer's "local" stream.
            return self._peer_parameter(TP_INITIAL_MAX_STREAM_DATA_BIDI_LOCAL)
        if kind == 1:
            return self._peer_parameter(TP_INITIAL_MAX_STREAM_DATA_BIDI_REMOTE)
        return self._peer_parameter(TP_INITIAL_MAX_STREAM_DATA_UNI)

    def _flow_allowance(self, stream_id, offset, size):
        """How many of ``size`` bytes at ``offset`` the peer's limits admit."""
        allowed = size
        stream_limit = self._stream_send_limit(stream_id)
        if stream_limit is not None:
            allowed = min(allowed, stream_limit - offset)
        if self._peer_max_data is not None:
            allowed = min(allowed, self._peer_max_data - self._sent_data_total)
        return max(0, allowed), stream_limit

    def _blocked_frames(self, stream_id, offset, size, stream_limit):
        """DATA_BLOCKED / STREAM_DATA_BLOCKED, once per limit value."""
        frames = []
        if stream_limit is not None and offset + size > stream_limit:
            key = ("stream", stream_id)
            if self._blocked_reported.get(key) != stream_limit:
                self._blocked_reported[key] = stream_limit
                frames.append(qframes.StreamDataBlockedFrame(stream_id, stream_limit))
        if (
            self._peer_max_data is not None
            and self._sent_data_total + size > self._peer_max_data
            and self._blocked_reported.get("connection") != self._peer_max_data
        ):
            self._blocked_reported["connection"] = self._peer_max_data
            frames.append(qframes.DataBlockedFrame(self._peer_max_data))
        return frames

    def queued_stream_bytes(self, stream_id=None):
        """Bytes queued and not yet packetized, for one stream or all of them."""
        with self._outbound_lock:
            if stream_id is None:
                return sum(self._queued_bytes.values())
            return self._queued_bytes.get(int(stream_id), 0)

    def wait_for_send_room(self, stream_id, max_queued, timeout=None, poll=0.1):
        """Block until ``stream_id`` has at most ``max_queued`` bytes queued.

        Returns False on timeout or when the connection closes, so a producer
        can stop instead of queueing without bound.
        """
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        stream_id = int(stream_id)
        with self._outbound_room:
            while not self.closed and self._queued_bytes.get(stream_id, 0) > max_queued:
                wait_for = poll
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait_for = min(poll, remaining)
                self._outbound_room.wait(wait_for)
            return not self.closed

    def reset_stream(self, stream_id, error_code):
        """Abandon our side of ``stream_id``: drop what is queued, send RESET_STREAM.

        Lets a producer that fails or stalls tell the peer the response is
        incomplete instead of ending it with a FIN. Safe from any thread.
        """
        stream_id = int(stream_id)
        with self._outbound_room:
            self._outbound = [entry for entry in self._outbound if entry[0] != stream_id]
            self._outbound.append((stream_id, None, int(error_code)))
            self._queued_bytes.pop(stream_id, None)
            self._outbound_room.notify_all()

    # -- sending -------------------------------------------------------

    def send_stream_data(self, stream_id, payload, fin=True, offset=0):
        return qframes.StreamFrame(stream_id, offset, payload, fin)

    def queue_stream_data(self, stream_id, payload, fin=False):
        """Queue bytes for a stream from any thread; :meth:`drain_outbound` sends them.

        Data is cut into frames small enough for one packet, and each stream
        keeps its own send offset, so successive calls append to the stream.
        """
        data = bytes(payload)
        pieces = [data[i:i + STREAM_CHUNK_SIZE] for i in range(0, len(data), STREAM_CHUNK_SIZE)]
        if not pieces:
            pieces = [b""]
        stream_id = int(stream_id)
        with self._outbound_lock:
            for index, piece in enumerate(pieces):
                last = index == len(pieces) - 1
                self._outbound.append((stream_id, piece, bool(fin) and last))
            self._queued_bytes[stream_id] = self._queued_bytes.get(stream_id, 0) + len(data)

    def drain_outbound(self):
        """Packetize queued stream bytes. Call from the thread that owns the socket.

        Returns the datagrams to send. Bytes stay queued until the 1-RTT send
        keys exist, and while the peer's flow control limits (MAX_DATA,
        MAX_STREAM_DATA), the congestion window or the anti-amplification
        limit hold them back; a blocked stream does not hold up the others.
        """
        if self.closed or self.keys.get(LEVEL_APPLICATION, {}).get("send") is None:
            return []
        with self._outbound_lock:
            pending, self._outbound = self._outbound, []
        datagrams = []
        keep = []
        blocked_streams = set()
        control = []
        stopped = False
        for stream_id, piece, fin in pending:
            if piece is None:
                # A reset: its final size is what the peer was already sent.
                final_size = self._send_offsets.get(stream_id, 0)
                control.append(qframes.ResetStreamFrame(stream_id, fin, final_size))
                self._reset_streams.add(stream_id)
                keep = [entry for entry in keep if entry[0] != stream_id]
                continue
            if stream_id in self._reset_streams:
                continue  # Nothing more goes out on a stream we reset.
            if stopped or stream_id in blocked_streams:
                keep.append((stream_id, piece, fin))
                continue
            offset = self._send_offsets.get(stream_id, 0)
            allowed, stream_limit = self._flow_allowance(stream_id, offset, len(piece))
            rest = None
            if allowed < len(piece):
                control.extend(self._blocked_frames(stream_id, offset, len(piece), stream_limit))
                blocked_streams.add(stream_id)
                if allowed == 0:
                    keep.append((stream_id, piece, fin))
                    continue
                piece, rest = piece[:allowed], (stream_id, piece[allowed:], fin)
                fin = False
            if not self.recovery.congestion.can_send(len(piece) + PACKET_OVERHEAD_ESTIMATE):
                # The congestion window is full: wait for acknowledgements.
                stopped = True
                keep.append((stream_id, piece, fin))
                if rest is not None:
                    keep.append(rest)
                continue
            frame = qframes.StreamFrame(stream_id, offset, piece, fin)
            sent = self._flush([(LEVEL_APPLICATION, [frame])])
            if not sent:
                # The anti-amplification limit refused this packet. Keep it and
                # everything after it for the next call, so no bytes are lost.
                stopped = True
                keep.append((stream_id, piece, fin))
                if rest is not None:
                    keep.append(rest)
                continue
            self._send_offsets[stream_id] = offset + len(piece)
            self._sent_data_total += len(piece)
            datagrams.extend(sent)
            if rest is not None:
                keep.append(rest)
        if control:
            datagrams.extend(self._flush([(LEVEL_APPLICATION, control)]))
        with self._outbound_room:
            self._outbound[:0] = keep
            queued = {}
            for stream_id, piece, _fin in self._outbound:
                if piece is not None:
                    queued[stream_id] = queued.get(stream_id, 0) + len(piece)
            self._queued_bytes = queued
            self._outbound_room.notify_all()
        return datagrams

    @staticmethod
    def ack_ranges(numbers):
        """Describe received packet numbers the way an ACK frame does.

        Returns ``(largest, ranges)`` where ``ranges[0]`` counts the packets
        contiguous below the largest and the rest are ``(gap, length)`` pairs,
        which is the shape RFC 9000 section 19.3 puts on the wire.
        """
        ordered = sorted(set(numbers), reverse=True)
        if not ordered:
            return None, []
        largest = ordered[0]
        index = 0
        run = 0
        while index + 1 < len(ordered) and ordered[index + 1] == ordered[index] - 1:
            index += 1
            run += 1
        ranges = [run]
        while index + 1 < len(ordered):
            smallest = ordered[index]
            index += 1
            next_largest = ordered[index]
            gap = smallest - next_largest - 2
            run = 0
            while index + 1 < len(ordered) and ordered[index + 1] == ordered[index] - 1:
                index += 1
                run += 1
            ranges.append((gap, run))
        return largest, ranges

    def _take_ack_frame(self, level):
        """An ACK for what arrived at this level, or None if nothing has."""
        pending = self._pending_acks.get(level)
        if not pending:
            return None
        largest, ranges = self.ack_ranges(pending)
        self._pending_acks[level] = []
        return qframes.AckFrame(largest, 0, ranges)

    def _flush(self, outgoing):
        datagrams = []
        carries_initial = False
        # Acknowledge at every level something arrived on, even where we have
        # nothing else to send: without this a peer sees no packet confirmed,
        # retransmits for ever and eventually gives up on the connection.
        levels_sent = {level for level, frames in outgoing if frames}
        for level in (LEVEL_INITIAL, LEVEL_HANDSHAKE, LEVEL_APPLICATION):
            if level not in levels_sent and self._pending_acks.get(level):
                outgoing.append((level, []))
        for level, frame_list in outgoing:
            ack = self._take_ack_frame(level)
            if ack is not None:
                frame_list = [ack] + list(frame_list)
            if not frame_list:
                continue
            packet = self._build_packet(level, frame_list)
            if packet:
                datagrams.append(packet)
                carries_initial = carries_initial or level == LEVEL_INITIAL
        # A datagram carrying an Initial must reach the minimum size, so a
        # server's reply cannot amplify traffic at a spoofed address. It must
        # NOT be applied to anything else: a short header packet runs to the
        # end of its datagram, so trailing padding lands inside the AEAD
        # ciphertext and the peer cannot open it.
        if carries_initial and datagrams and len(datagrams[0]) < MIN_INITIAL_DATAGRAM:
            datagrams[0] = qframes.pad_to(datagrams[0], MIN_INITIAL_DATAGRAM)
        allowed = []
        for datagram in datagrams:
            if not self.amplification.may_send(len(datagram)):
                # Section 8: answering a possibly spoofed address with more
                # than three times what arrived turns us into an amplifier.
                break
            self.amplification.on_sent(len(datagram))
            allowed.append(datagram)
        return allowed

    def _build_packet(self, level, frame_list):
        keys = self.keys.get(level, {}).get("send")
        if keys is None:
            return b""
        number = self.packet_numbers[level]
        self.packet_numbers[level] = number + 1
        payload = qframes.serialize_frames(frame_list)
        if len(payload) < 4:
            payload = qframes.pad_to(payload, 4)

        if level == LEVEL_APPLICATION:
            header = build_short_header(self.peer_cid, packet_number=number)
        else:
            packet_type = PACKET_INITIAL if level == LEVEL_INITIAL else PACKET_HANDSHAKE
            header = build_long_header(
                packet_type,
                VERSION_1,
                self.peer_cid,
                self.host_cid,
                packet_number=number,
                payload_length=len(payload) + AEAD_TAG_SIZE,
            )
        sealed = keys.seal(number, header, payload)
        pn_offset = len(header) - 4
        packet = apply_header_protection(keys, header + sealed, pn_offset, 4)

        # An ACK-only packet is never retransmitted: the peer will tell us
        # again, and probing on it would never end.
        ack_eliciting = any(not isinstance(f, qframes.AckFrame) for f in frame_list)
        record = SentPacket(
            number,
            self.recovery._now(),
            frames=list(frame_list),
            ack_eliciting=ack_eliciting,
            in_flight=ack_eliciting,
            size=len(packet),
            level=level,
        )
        self.recovery.on_packet_sent(level, record)
        return packet

    def describe(self):
        return {
            "protocol": "QUIC",
            "alpn": self.alpn,
            "handshake_complete": self.handshake_complete,
            "host_connection_id": self.host_cid.hex(),
            "peer_connection_id": self.peer_cid.hex(),
            "levels": sorted(self.keys),
            "streams": len(self.streams),
            "recovery": self.recovery.describe(),
            "amplification": self.amplification.describe(),
            "paths": self.paths.describe(),
        }


__all__ = [
    "LEVEL_APPLICATION",
    "LEVEL_HANDSHAKE",
    "LEVEL_INITIAL",
    "MAX_DATAGRAM_SIZE",
    "QuicConnection",
    "StreamBuffer",
    "decode_transport_parameters",
    "encode_transport_parameters",
]

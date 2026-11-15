"""The client half of a QUIC connection, for calling other services.

The server's :class:`~wsbuilder.quic.connection.QuicConnection` already holds
everything both halves share: packet protection, ACKs, loss recovery,
congestion and flow control, stream reassembly. What differs is small and
lives here: which secret protects which direction, who speaks first, which
stream ids are ours, and the client side of the TLS handshake.
"""

from . import frames as qframes
from .connection import (
    LEVEL_APPLICATION,
    LEVEL_HANDSHAKE,
    LEVEL_INITIAL,
    MAX_DATAGRAM_SIZE,
    TP_INITIAL_MAX_DATA,
    TP_INITIAL_MAX_STREAMS_BIDI,
    TP_INITIAL_SOURCE_CONNECTION_ID,
    TP_ORIGINAL_DESTINATION_CONNECTION_ID,
    TP_RETRY_SOURCE_CONNECTION_ID,
    QuicConnection,
    decode_transport_parameters,
)
from .crypto import PacketKeys, initial_secrets, retry_integrity_tag
from .packet import (
    PACKET_HANDSHAKE,
    PACKET_INITIAL,
    PACKET_RETRY,
    QuicPacketError,
    new_connection_id,
)
from .tls import ClientHandshake, TLSError

#: RFC 9000 section 20.1: CONNECTION_CLOSE codes. 0x100 + alert is CRYPTO_ERROR.
ERROR_PROTOCOL_VIOLATION = 0x0A
ERROR_TRANSPORT_PARAMETER = 0x08


class QuicClientConnection(QuicConnection):
    """One client connection: call :meth:`start`, then feed it datagrams."""

    def __init__(
        self,
        server_name,
        trust,
        *,
        alpn_protocols=("h3",),
        server_address=("", 0),
        on_stream_data=None,
        certificate_chain_der=None,
        private_key=None,
    ):
        super().__init__(
            [b"unused"],
            None,
            alpn_protocols=alpn_protocols,
            client_address=server_address,
            on_stream_data=on_stream_data,
        )
        # The peer here is the server; the base class calls it client_address
        # because it was written for the server side.
        self.server_address = tuple(server_address)
        # A client is never an amplifier: it only answers what it asked for.
        self.amplification.validate()
        self.original_dcid = new_connection_id(8)
        self.peer_cid = self.original_dcid
        self._retry_seen = False
        self._retry_source_cid = None
        self._next_bidi = 0
        self._next_uni = 2
        #: Raised by MAX_STREAMS frames past the initial transport parameter.
        self._max_bidi_streams = None
        self._tls = ClientHandshake(
            server_name,
            trust,
            alpn_protocols=alpn_protocols,
            transport_parameters=b"",
            certificate_chain_der=certificate_chain_der,
            private_key=private_key,
        )
        self._install_initial_keys(self.original_dcid)
        #: Set when the connection is closed by a TLS or transport error.
        self.error = None

    # -- keys: the client sends with the client secrets ----------------

    def _install_initial_keys(self, destination_cid):
        client_secret, server_secret = initial_secrets(destination_cid)
        self.keys[LEVEL_INITIAL] = {
            "recv": PacketKeys(server_secret),
            "send": PacketKeys(client_secret),
        }

    def _install(self, level, client_secret, server_secret):
        self.keys[level] = {
            "recv": PacketKeys(server_secret),
            "send": PacketKeys(client_secret),
        }

    def _transport_parameters(self):
        values = decode_transport_parameters(super()._transport_parameters())
        # Only a server sends original_destination_connection_id.
        values.pop(TP_ORIGINAL_DESTINATION_CONNECTION_ID, None)
        values[TP_INITIAL_SOURCE_CONNECTION_ID] = self.host_cid
        from .connection import encode_transport_parameters

        return encode_transport_parameters(values)

    def _defers_early_application_data(self):
        # Only a server must hold 1-RTT data until the peer has proved itself;
        # a client has verified the server before it holds 1-RTT keys at all.
        return False

    # -- the first flight ----------------------------------------------

    def start(self):
        """The datagrams that open the connection (the ClientHello)."""
        self._tls.transport_parameters = self._transport_parameters()
        hello = self._tls.client_hello()
        self._client_hello = hello
        return self._flush([(LEVEL_INITIAL, [qframes.CryptoFrame(0, hello)])])

    # -- receiving -----------------------------------------------------

    def _receive_long(self, data, header, outgoing):
        if header.packet_type == PACKET_RETRY:
            self._on_retry(data, header, outgoing)
            return
        if header.destination_cid != self.host_cid:
            return  # Not for this connection.
        if header.packet_type == PACKET_INITIAL and self.largest_received[LEVEL_INITIAL] < 0:
            # The server's first Initial names the connection id it wants.
            self.peer_cid = header.source_cid
        level = {PACKET_INITIAL: LEVEL_INITIAL, PACKET_HANDSHAKE: LEVEL_HANDSHAKE}.get(
            header.packet_type
        )
        if level is None or level not in self.keys:
            return
        start = header.payload_offset - (header.packet_length - header.length)
        whole = data[start : start + header.packet_length]
        pn_offset = header.payload_offset - start
        self._open_packet(level, whole, pn_offset, outgoing)

    def _on_retry(self, data, header, outgoing):
        """RFC 9000 section 17.2.5: start again with the server's token."""
        if self._retry_seen or self.largest_received[LEVEL_INITIAL] >= 0:
            return  # Only one Retry, and only before any Initial arrived.
        packet = data[: header.packet_length] if header.packet_length else data
        if len(packet) < 16 or header.source_cid == self.peer_cid:
            return
        body, tag = packet[:-16], packet[-16:]
        if retry_integrity_tag(self.original_dcid, body) != tag:
            return  # Not from someone who saw our Initial.
        token = header.token[:-16]
        if not token:
            return
        self._retry_seen = True
        self._retry_source_cid = header.source_cid
        self._initial_token = token
        self.peer_cid = header.source_cid
        self._install_initial_keys(header.source_cid)
        # The first Initial is gone for good: forget it and send the hello
        # again, under the new keys, from offset zero.
        self.recovery = type(self.recovery)(
            levels=(LEVEL_INITIAL, LEVEL_HANDSHAKE, LEVEL_APPLICATION)
        )
        self.packet_numbers[LEVEL_INITIAL] = 0
        outgoing.append((LEVEL_INITIAL, [qframes.CryptoFrame(0, self._client_hello)]))

    def _fail(self, level, code, reason, outgoing):
        self.error = reason
        self._close_with_error(level, code, reason, outgoing)

    def _advance_handshake(self, level, outgoing):
        buffer = self.crypto_buffers[level]
        try:
            if level == LEVEL_INITIAL and LEVEL_HANDSHAKE not in self.keys:
                message = bytes(buffer.data)
                if len(message) < 4 or len(message) < 4 + int.from_bytes(message[1:4], "big"):
                    return
                length = 4 + int.from_bytes(message[1:4], "big")
                client_hs, server_hs = self._tls.handle_server_hello(message[:length])
                self._install(LEVEL_HANDSHAKE, client_hs, server_hs)
            elif level == LEVEL_HANDSHAKE and LEVEL_APPLICATION not in self.keys:
                result = self._tls.handle_server_flight(bytes(buffer.data))
                if result is None:
                    return
                flight, secrets = result
                self._on_server_authenticated(outgoing)
                if self.closed:
                    return
                self._install(
                    LEVEL_APPLICATION, secrets["client_application"], secrets["server_application"]
                )
                outgoing.append((LEVEL_HANDSHAKE, [qframes.CryptoFrame(0, flight)]))
                # The client is done once its Finished is sent; HANDSHAKE_DONE
                # from the server only confirms it.
                self.handshake_complete = True
        except TLSError as exc:
            self._fail(level, 0x100 + int(getattr(exc, "alert", 40)), str(exc), outgoing)

    def _on_server_authenticated(self, outgoing):
        """Check the server's transport parameters (RFC 9000 section 7.3)."""
        self.alpn = self._tls.selected_alpn
        try:
            parameters = decode_transport_parameters(self._tls.peer_transport_parameters)
        except QuicPacketError as exc:
            self._fail(LEVEL_HANDSHAKE, ERROR_TRANSPORT_PARAMETER, str(exc), outgoing)
            return
        if parameters.get(TP_ORIGINAL_DESTINATION_CONNECTION_ID) != self.original_dcid:
            self._fail(
                LEVEL_HANDSHAKE,
                ERROR_TRANSPORT_PARAMETER,
                "original_destination_connection_id does not match",
                outgoing,
            )
            return
        if self._retry_seen and parameters.get(TP_RETRY_SOURCE_CONNECTION_ID) != self._retry_source_cid:
            self._fail(
                LEVEL_HANDSHAKE,
                ERROR_TRANSPORT_PARAMETER,
                "retry_source_connection_id does not match",
                outgoing,
            )
            return
        if parameters.get(TP_INITIAL_SOURCE_CONNECTION_ID) != self.peer_cid:
            self._fail(
                LEVEL_HANDSHAKE,
                ERROR_TRANSPORT_PARAMETER,
                "initial_source_connection_id does not match",
                outgoing,
            )
            return
        self.peer_transport_parameters = parameters
        self._peer_max_data = self._peer_parameter(TP_INITIAL_MAX_DATA)

    def _handle_frames(self, level, parsed, outgoing):
        for frame in parsed:
            if isinstance(frame, qframes.ConnectionCloseFrame) and self.error is None:
                reason = frame.reason.decode("utf-8", "replace") if frame.reason else ""
                self.error = f"closed by peer (0x{frame.error_code:x}) {reason}".strip()
            elif isinstance(frame, qframes.ResetStreamFrame):
                callback = getattr(self, "on_stream_reset", None)
                if callback is not None:
                    callback(frame.stream_id, frame.error_code)
            elif isinstance(frame, qframes.MaxStreamsFrame) and frame.bidirectional:
                self._max_bidi_streams = max(self._max_bidi_streams or 0, frame.maximum)
        super()._handle_frames(level, parsed, outgoing)

    # -- streams -------------------------------------------------------

    @property
    def peer_max_bidi_streams(self):
        """How many request streams the server lets us open, or None."""
        initial = self._peer_parameter(TP_INITIAL_MAX_STREAMS_BIDI)
        if self._max_bidi_streams is None:
            return initial
        return max(initial or 0, self._max_bidi_streams)

    def streams_left(self):
        """Request streams still available on this connection."""
        limit = self.peer_max_bidi_streams
        if limit is None:
            return 0 if not self.handshake_complete else 1 << 30
        return max(0, limit - self._next_bidi // 4)

    def open_bidi_stream(self):
        stream_id = self._next_bidi
        self._next_bidi += 4
        return stream_id

    def open_uni_stream(self):
        stream_id = self._next_uni
        self._next_uni += 4
        return stream_id

    def close(self, error_code=0, reason=""):
        """Datagrams carrying CONNECTION_CLOSE; the connection is then done."""
        if self.closed:
            return []
        level = LEVEL_APPLICATION if LEVEL_APPLICATION in self.keys else (
            LEVEL_HANDSHAKE if LEVEL_HANDSHAKE in self.keys else LEVEL_INITIAL
        )
        frame = qframes.ConnectionCloseFrame(int(error_code), reason)
        datagrams = self._flush([(level, [frame])])
        self.closed = True
        return datagrams


__all__ = ["MAX_DATAGRAM_SIZE", "QuicClientConnection"]

"""A UDP listener that serves HTTP/3 over QUIC.

HTTP/3 does not share the TCP listener: QUIC runs on UDP, so this is a
separate socket on the same port number. A client finds it through the
Alt-Svc header an HTTP/1 or HTTP/2 response advertises, which is why
:func:`alt_svc_header` exists here rather than being left to the caller.

Connections are keyed by the destination connection id the client picks,
which is what lets a connection survive a change of address -- the property
QUIC has and TCP does not.
"""

import socket
import threading

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from . import http3
from .http3_websocket import Http3WebSocketStream
from .quic.address import AddressValidator
from .quic.connection import MAX_DATAGRAM_SIZE, QuicConnection
from .quic.crypto import retry_integrity_tag
from .quic.packet import (
    PACKET_INITIAL,
    build_retry,
    is_long_header,
    new_connection_id,
    parse_long_header,
)
from .quic.varint import decode_varint

DEFAULT_MAX_CONNECTIONS = 256


def alt_svc_header(port, max_age=86400):
    """The Alt-Svc value that points a client at this listener."""
    return f'h3=":{int(port)}"; ma={int(max_age)}'


def certificate_chain_der(material):
    """DER for the leaf and any chain, which is what TLS puts on the wire."""
    chain = [x509.load_pem_x509_certificate(material.certificate_pem)]
    remaining = material.chain_pem
    while remaining and b"BEGIN CERTIFICATE" in remaining:
        certificate = x509.load_pem_x509_certificate(remaining)
        chain.append(certificate)
        marker = remaining.find(b"-----END CERTIFICATE-----")
        if marker < 0:
            break
        remaining = remaining[marker + len(b"-----END CERTIFICATE-----") :].lstrip()
    return [c.public_bytes(serialization.Encoding.DER) for c in chain]


#: Server-initiated unidirectional stream id 3 carries the control stream.
CONTROL_STREAM_ID = 3


class Http3Server:
    """Serves an app over QUIC on a UDP socket."""

    #: Short enough that bytes queued by a handler thread leave promptly.
    ACCEPT_TIMEOUT_SECONDS = 0.02

    def __init__(self, host, port, app, tls, *, max_connections=DEFAULT_MAX_CONNECTIONS,
                 require_address_validation=False):
        self.host = host
        self.port = port
        self.app = app
        self.tls = tls
        self.max_connections = int(max_connections)
        #: Answer a first Initial with a Retry, so the client proves it can
        #: receive at the address it claims before we commit any work.
        self.require_address_validation = bool(require_address_validation)
        self.addresses = AddressValidator()
        self.retries_sent = 0
        self.server_address = (host, port)
        self.connections = {}
        self._sock = None
        self._stop = threading.Event()
        self._serving = threading.Event()

    # -- lifecycle -----------------------------------------------------

    def stop(self):
        self._stop.set()

    def wait_until_serving(self, timeout=None):
        return self._serving.wait(timeout)

    def _material(self):
        provider = getattr(self.tls, "current", None)
        return provider() if callable(provider) else self.tls

    def serve_forever(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(self.ACCEPT_TIMEOUT_SECONDS)
        self._sock = sock
        self.server_address = sock.getsockname()[:2]
        host, port = self.server_address
        print(f"HTTP/3 listening on udp://{host}:{port}/")
        self._serving.set()
        try:
            while not self._stop.is_set():
                try:
                    datagram, address = sock.recvfrom(65535)
                except socket.timeout:
                    # The idle gap is where loss timers get a chance to fire;
                    # without this a probe would wait for the next datagram,
                    # which on a lossy path may never come.
                    self._tick()
                    continue
                except OSError as e:
                    if self._stop.is_set():
                        break
                    print(f"[http3] receive error: {e}")
                    continue
                try:
                    for reply in self.handle_datagram(datagram, address):
                        sock.sendto(reply, address)
                except Exception as e:
                    print(f"[http3] error from {address}: {e}")
                self._send_outbound()
        finally:
            self._serving.clear()
            sock.close()

    def _tick(self, now=None):
        """Fire any due loss timer, send queued stream bytes, and send what both ask for."""
        import time as _time

        moment = _time.monotonic() if now is None else now
        sent = 0
        for connection in {id(c): c for c in self.connections.values()}.values():
            deadline = connection.loss_timer()
            if deadline is None or deadline > moment:
                continue
            for datagram in connection.on_timeout(now=moment):
                try:
                    self._sock.sendto(datagram, connection.client_address)
                    sent += 1
                except OSError:
                    break
        return sent + self._send_outbound()

    def _send_outbound(self):
        """Send stream bytes that handler threads queued. Runs on the socket thread."""
        sent = 0
        for connection in {id(c): c for c in self.connections.values()}.values():
            for datagram in connection.drain_outbound():
                try:
                    self._sock.sendto(datagram, connection.client_address)
                    sent += 1
                except OSError:
                    break
        return sent

    # -- routing -------------------------------------------------------

    @staticmethod
    def _destination_cid(datagram):
        if not datagram:
            return None
        if is_long_header(datagram[0]):
            return parse_long_header(datagram).destination_cid
        # A short header gives no length, and we always issue 8-octet ids.
        return datagram[1:9] if len(datagram) >= 9 else None

    def handle_datagram(self, datagram, address):
        key = self._destination_cid(datagram)
        if key is None:
            return []
        connection = self.connections.get(key)
        if connection is None:
            connection = self.connections.get(bytes(key))
        if connection is None:
            if len(self.connections) >= self.max_connections:
                return []
            retry = self._maybe_retry(datagram, address)
            if retry is not None:
                return [retry]
            connection = self._new_connection(address)
            # The client's chosen id routes its first flight; ours routes
            # everything after the handshake tells it which to use.
            self.connections[bytes(key)] = connection
            self.connections[connection.host_cid] = connection
        replies = list(connection.receive_datagram(datagram, address=address))
        # Answers queued while handling this datagram leave in the same reply.
        replies.extend(connection.drain_outbound())
        if connection.closed:
            self._drop(connection)
        return replies

    def _maybe_retry(self, datagram, address):
        """Answer an unvalidated Initial with a Retry, or None to proceed."""
        if not self.require_address_validation or not is_long_header(datagram[0]):
            return None
        try:
            header = parse_long_header(datagram)
        except Exception:
            return None
        if header.packet_type != PACKET_INITIAL:
            return None
        if header.token and self.addresses.validate(header.token, address) is not None:
            return None  # Already proved; let the handshake run.

        token = self.addresses.issue(address, header.destination_cid)
        new_cid = new_connection_id(8)
        body = build_retry(header.version, header.source_cid, new_cid, token, b"")
        packet = body + retry_integrity_tag(header.destination_cid, body)
        self.retries_sent += 1
        return packet

    def _new_connection(self, address):
        material = self._material()
        private_key = serialization.load_pem_private_key(
            material.private_key_pem, password=material.key_password
        )
        connection = QuicConnection(
            certificate_chain_der(material),
            private_key,
            alpn_protocols=("h3",),
            client_address=address,
            on_stream_data=self._on_stream_data,
        )
        # RFC 9114 section 6.2.1: the server opens a control stream carrying
        # SETTINGS, which is where a client learns CONNECT is allowed.
        connection.queue_stream_data(CONTROL_STREAM_ID, http3.build_control_stream(), fin=False)
        return connection

    def _drop(self, connection):
        for key in [k for k, value in self.connections.items() if value is connection]:
            self.connections.pop(key, None)

    # -- application ---------------------------------------------------

    def _on_stream_data(self, connection, stream_id, data, complete):
        """Feed request bytes into HTTP/3 and answer when the stream ends."""
        if stream_id % 4 == 2 or stream_id % 4 == 3:
            # Unidirectional: control and QPACK streams. We only need to read
            # far enough to ignore them, since our QPACK uses no dynamic table.
            return []
        streams = getattr(connection, "h3_streams", None)
        if streams is None:
            streams = {}
            connection.h3_streams = streams
        stream = streams.get(stream_id)
        if stream is None:
            stream = http3.RequestStream(stream_id)
            streams[stream_id] = stream

        try:
            ready = stream.feed(data, fin=complete)
        except http3.H3Error as e:
            print(f"[http3] stream {stream_id}: {e}")
            if stream.websocket is not None:
                stream.websocket.push_eof()
            return []

        if stream.websocket is not None:
            self._pump_websocket(stream)
            return []

        if not stream.answered and stream.headers is not None:
            try:
                extended = http3.is_extended_connect(stream)
            except http3.H3Error as e:
                print(f"[http3] malformed request on stream {stream_id}: {e}")
                return []
            if extended:
                # RFC 9220: a WebSocket request is answered at once and its
                # stream stays open, so the body is never waited for.
                stream.answered = True
                self._open_websocket(connection, stream)
                return []

        if not ready or stream.answered:
            return []
        stream.answered = True

        tls_meta = {"enabled": True, "version": "TLSv1.3", "alpn": connection.alpn}
        try:
            request = http3.build_request(stream, connection.client_address, tls_meta)
            response = self.app.dispatch(request)
        except http3.H3Error as e:
            print(f"[http3] malformed request on stream {stream_id}: {e}")
            return []
        self._send_response(connection, stream_id, response, send_body=request.method != "HEAD")
        return []

    def _send_response(self, connection, stream_id, response, *, send_body):
        """Queue a response. A streamed body is sent chunk by chunk as it is produced."""
        if not getattr(response, "is_stream", False) or not send_body:
            payload = http3.build_response_frames(response, send_body=send_body)
            connection.queue_stream_data(stream_id, payload, fin=True)
            return

        from .http import _iter_stream_chunks

        connection.queue_stream_data(
            stream_id,
            http3.encode_headers_frame(http3.build_response_head(response.status, response.headers)),
            fin=False,
        )

        def pump():
            try:
                for chunk in _iter_stream_chunks(response.stream):
                    if connection.closed:
                        break
                    connection.queue_stream_data(stream_id, http3.encode_frame(http3.FRAME_DATA, chunk))
            except Exception as e:
                print(f"[http3] streaming response on stream {stream_id} failed: {e}")
            finally:
                # Closing the producer lets its own cleanup run (for example a
                # realtime client deregistering itself) once the peer is gone.
                close = getattr(response.stream, "close", None)
                if callable(close):
                    close()
                connection.queue_stream_data(stream_id, b"", fin=True)

        threading.Thread(target=pump, name=f"http3-stream-{stream_id}", daemon=True).start()

    def _open_websocket(self, connection, stream):
        """Answer an extended CONNECT: accept it onto a route, or refuse it."""
        tls_meta = {"enabled": True, "version": "TLSv1.3", "alpn": connection.alpn}
        try:
            request = http3.build_request(stream, connection.client_address, tls_meta)
        except http3.H3Error as e:
            print(f"[http3] malformed WebSocket request on stream {stream.id}: {e}")
            connection.queue_stream_data(stream.id, self._refusal(400, "Bad Request"), fin=True)
            return

        route = self.app.ws_routes.get(request.path)
        if route is None:
            connection.queue_stream_data(stream.id, self._refusal(404, "Not Found"), fin=True)
            return
        if request.headers.get("sec-websocket-version", "") != "13":
            connection.queue_stream_data(
                stream.id,
                http3.encode_headers_frame(
                    http3.build_response_head(426, {"sec-websocket-version": "13"})
                ),
                fin=True,
            )
            return

        security = getattr(self.app, "security", None)
        if security is not None:
            decision = security.evaluate(request)
            if not decision.allowed:
                response = decision.to_response()
                security.observe_response(request, response.status)
                self._send_response(connection, stream.id, response, send_body=True)
                return

        subprotocol = self._choose_subprotocol(request, route)
        headers = {"sec-websocket-protocol": subprotocol} if subprotocol else {}
        connection.queue_stream_data(
            stream.id,
            http3.encode_headers_frame(http3.build_response_head(200, headers)),
            fin=False,
        )
        transport = Http3WebSocketStream(connection, stream.id)
        stream.websocket = transport
        self._pump_websocket(stream)

        threading.Thread(
            target=self._run_websocket,
            args=(route, transport, request, subprotocol),
            name=f"http3-ws-{stream.id}",
            daemon=True,
        ).start()

    @staticmethod
    def _refusal(status, text):
        """A complete small response, as the frames for a stream."""
        from .http import Response

        return http3.build_response_frames(Response.text(text, status=status))

    @staticmethod
    def _choose_subprotocol(request, route):
        offered = [item.strip() for item in request.headers.get("sec-websocket-protocol", "").split(",")]
        supported = tuple(route.get("subprotocols", ()) or ())
        for candidate in offered:
            if candidate and candidate in supported:
                return candidate
        return ""

    def _pump_websocket(self, stream):
        """Hand the WebSocket bytes that just arrived to the transport."""
        transport = stream.websocket
        payload = bytes(stream.body)
        stream.body.clear()
        transport.push(payload)
        if stream.finished:
            transport.push_eof()

    @staticmethod
    def _run_websocket(route, transport, request, subprotocol):
        """Run the route's handler over the stream, the way the HTTP/1 path does."""
        from .ws import WebSocket

        ws = WebSocket(
            transport,
            request.client,
            subprotocol,
            request.headers,
            supported_subprotocols=route.get("subprotocols", ()),
            idle_timeout=route.get("idle_timeout", 0.0),
            keepalive_interval=route.get("keepalive_interval", 0.0),
            pong_timeout=route.get("pong_timeout", 0.0),
            auto_pong=route.get("auto_pong", True),
            on_close=route.get("on_close"),
            on_error=route.get("on_error"),
            on_timeout=route.get("on_timeout"),
            io_poll_interval=route.get("io_poll_interval", 1.0),
            ping_payload=route.get("ping_payload", b""),
        )
        try:
            route["handler"](ws, request)
        except Exception as e:
            print(f"[ws] error over HTTP/3: {e}")
        finally:
            transport.close()

    def describe(self):
        return {
            "protocol": "HTTP/3",
            "address": f"{self.server_address[0]}:{self.server_address[1]}",
            "connections": len({id(c) for c in self.connections.values()}),
            "alt_svc": alt_svc_header(self.server_address[1]),
            "address_validation": self.require_address_validation,
            "retries_sent": self.retries_sent,
            "recovery": [
                c.recovery.describe()
                for c in {id(c): c for c in self.connections.values()}.values()
            ],
        }


def install_http3(app, tls, host="0.0.0.0", port=0, attr_name="http3"):
    """Attach an :class:`Http3Server` to an app without starting it."""
    server = Http3Server(host, port, app, tls)
    setattr(app, str(attr_name or "http3"), server)
    return server


__all__ = [
    "Http3Server",
    "alt_svc_header",
    "certificate_chain_der",
    "install_http3",
]

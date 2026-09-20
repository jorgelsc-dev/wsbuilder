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
from .quic.connection import MAX_DATAGRAM_SIZE, QuicConnection
from .quic.packet import is_long_header, parse_long_header
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


class Http3Server:
    """Serves an app over QUIC on a UDP socket."""

    ACCEPT_TIMEOUT_SECONDS = 0.5

    def __init__(self, host, port, app, tls, *, max_connections=DEFAULT_MAX_CONNECTIONS):
        self.host = host
        self.port = port
        self.app = app
        self.tls = tls
        self.max_connections = int(max_connections)
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
        finally:
            self._serving.clear()
            sock.close()

    def _tick(self, now=None):
        """Fire any due loss timer and send what it asks for."""
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
            connection = self._new_connection(address)
            # The client's chosen id routes its first flight; ours routes
            # everything after the handshake tells it which to use.
            self.connections[bytes(key)] = connection
            self.connections[connection.host_cid] = connection
        replies = connection.receive_datagram(datagram)
        if connection.closed:
            self._drop(connection)
        return replies

    def _new_connection(self, address):
        material = self._material()
        private_key = serialization.load_pem_private_key(
            material.private_key_pem, password=material.key_password
        )
        return QuicConnection(
            certificate_chain_der(material),
            private_key,
            alpn_protocols=("h3",),
            client_address=address,
            on_stream_data=self._on_stream_data,
        )

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
        payload = http3.build_response_frames(response, send_body=request.method != "HEAD")
        return [connection.send_stream_data(stream_id, payload, fin=True)]

    def describe(self):
        return {
            "protocol": "HTTP/3",
            "address": f"{self.server_address[0]}:{self.server_address[1]}",
            "connections": len({id(c) for c in self.connections.values()}),
            "alt_svc": alt_svc_header(self.server_address[1]),
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

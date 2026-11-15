"""An HTTP/3 client for calling other services over QUIC.

One :class:`Http3Client` keeps a QUIC connection open and sends every
request on a new stream of it, so calls after the first pay neither a TCP
nor a TLS handshake, and a slow response never holds up the others (there is
no head-of-line blocking between streams). Packets are encrypted with TLS
1.3, the server's certificate is always verified, and a client certificate
can be presented for mutual TLS.

A background thread owns the UDP socket and the connection state; callers on
any thread queue requests and wait for their own stream. When the server's
stream allowance runs out, the connection goes idle or it fails, the next
request opens a fresh connection transparently.

Needs the optional ``tls`` extra (``cryptography``), like the server.
"""

import os
import select
import socket
import threading
import time

from . import http3
from .qpack import decode_field_section, encode_field_section
from .quic.client import QuicClientConnection
from .quic.tls import CertificateTrust
from .quic.varint import encode_varint

#: Default ceiling on one response body, so a misbehaving server cannot make
#: the client buffer without limit.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
#: The server drops connections idle for 30 s; reconnect before that.
DEFAULT_IDLE_RECONNECT_SECONDS = 20.0

H3_REQUEST_CANCELLED = 0x010C

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "Http3Client",
    "Http3Error",
    "Http3Response",
    "build_trust",
]


class Http3Error(ConnectionError):
    """A request that could not be completed over HTTP/3."""


class Http3Response:
    """A complete response: status, lower-cased headers and body."""

    __slots__ = ("status", "headers", "body", "trailers")

    def __init__(self, status, headers, body, trailers=None):
        self.status = int(status)
        self.headers = dict(headers)
        self.body = bytes(body)
        self.trailers = dict(trailers or {})

    def header(self, name, default=None):
        return self.headers.get(str(name).lower(), default)

    def __repr__(self):
        return f"<Http3Response {self.status} {len(self.body)} bytes>"


def build_trust(trust=None, ca_pem=None):
    """A :class:`CertificateTrust` from whichever form the caller has.

    ``trust`` may already be one, or anything with ``certificate_pem`` (a
    :class:`~wsbuilder.CertificateAuthority`); ``ca_pem`` is a PEM bundle.
    With neither, the platform's default CA store is used.
    """
    if isinstance(trust, CertificateTrust):
        return trust
    if trust is not None:
        pem = getattr(trust, "certificate_pem", None)
        if pem is None:
            raise TypeError("trust must be a CertificateTrust or have certificate_pem")
        return CertificateTrust.from_pem(pem)
    if ca_pem is not None:
        return CertificateTrust.from_pem(ca_pem)
    return CertificateTrust.system()


def _client_identity(material):
    """``(chain_der, private_key)`` from TLS material, or ``(None, None)``."""
    if material is None:
        return None, None
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    pem = bytes(material.certificate_pem) + b"\n" + bytes(getattr(material, "chain_pem", b"") or b"")
    certificates = x509.load_pem_x509_certificates(pem)
    chain = [cert.public_bytes(serialization.Encoding.DER) for cert in certificates]
    password = getattr(material, "key_password", None)
    if isinstance(password, str):
        password = password.encode("utf-8")
    key = serialization.load_pem_private_key(bytes(material.private_key_pem), password=password)
    return chain, key


def _client_control_stream():
    """A client's control stream: its type, then SETTINGS (RFC 9114 6.2.1)."""
    settings = {
        http3.SETTINGS_QPACK_MAX_TABLE_CAPACITY: 0,
        http3.SETTINGS_QPACK_BLOCKED_STREAMS: 0,
        http3.SETTINGS_MAX_FIELD_SECTION_SIZE: http3.MAX_FIELD_SECTION_SIZE,
    }
    return encode_varint(http3.STREAM_CONTROL) + http3.encode_frame(
        http3.FRAME_SETTINGS, http3.encode_settings(settings)
    )


class _Pending:
    __slots__ = ("event", "data", "error", "response", "limit")

    def __init__(self, limit):
        self.event = threading.Event()
        self.data = bytearray()
        self.error = None
        self.response = None
        self.limit = limit


class _Session:
    """One QUIC connection and the thread that drives it."""

    #: Upper bound on how long the I/O thread sleeps without checking timers.
    POLL_SECONDS = 0.05

    def __init__(self, address, family, server_name, trust, identity, max_response_bytes):
        chain, key = identity
        self.max_response_bytes = int(max_response_bytes)
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        # A connected UDP socket only receives from the server it names.
        self.sock.connect(address)
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._sockets = (self.sock, self._wake_r, self._wake_w)
        self.connection = QuicClientConnection(
            server_name,
            trust,
            server_address=address,
            on_stream_data=self._on_stream_data,
            certificate_chain_der=chain,
            private_key=key,
        )
        self.connection.on_stream_reset = self._on_stream_reset
        self._lock = threading.Lock()
        self._pending = {}
        self._handshake = threading.Event()
        self._stop = threading.Event()
        self.draining = False
        self.last_used = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name=f"http3-client-{server_name}", daemon=True
        )

    # -- lifecycle -----------------------------------------------------

    def connect(self, timeout):
        self._send(self.connection.start())
        self.connection.queue_stream_data(
            self.connection.open_uni_stream(), _client_control_stream(), fin=False
        )
        self._thread.start()
        if not self._handshake.wait(timeout):
            self.close()
            raise Http3Error("QUIC handshake timed out")
        if self.connection.closed or not self.connection.handshake_complete:
            self.close()
            raise Http3Error(self.connection.error or "QUIC handshake failed")

    def close(self):
        """Stop the I/O thread after sending CONNECTION_CLOSE."""
        self._stop.set()
        self._wake()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
        if not self._thread.is_alive():
            self._shutdown_socket()

    def _shutdown_socket(self):
        with self._lock:
            sockets, self._sockets = self._sockets, ()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    @property
    def usable(self):
        connection = self.connection
        return (
            not self.draining
            and not self._stop.is_set()
            and not connection.closed
            and connection.handshake_complete
            and connection.streams_left() > 0
        )

    def idle(self):
        with self._lock:
            return not self._pending

    # -- requests ------------------------------------------------------

    def submit(self, payload):
        """Queue one request; returns the pending entry to wait on."""
        pending = _Pending(self.max_response_bytes)
        with self._lock:
            stream_id = self.connection.open_bidi_stream()
            self._pending[stream_id] = pending
            if self.connection.streams_left() <= 0:
                self.draining = True
        self.connection.queue_stream_data(stream_id, payload, fin=True)
        self.last_used = time.monotonic()
        self._wake()
        return stream_id, pending

    def forget(self, stream_id):
        with self._lock:
            self._pending.pop(stream_id, None)

    def cancel(self, stream_id):
        """Give up on a stream: tell the server and forget it."""
        self.forget(stream_id)
        if not self.connection.closed:
            self.connection.reset_stream(stream_id, H3_REQUEST_CANCELLED)
            self._wake()

    # -- I/O thread ----------------------------------------------------

    def _wake(self):
        try:
            self._wake_w.send(b"\x00")
        except (OSError, ValueError):
            pass  # Full (a wakeup is already pending) or already closed.

    def _send(self, datagrams):
        for datagram in datagrams:
            try:
                self.sock.send(datagram)
            except BlockingIOError:
                # The kernel buffer is full; loss recovery resends it.
                break
            except OSError:
                break

    def _run(self):
        connection = self.connection
        try:
            while not self._stop.is_set():
                timeout = self.POLL_SECONDS
                deadline = connection.loss_timer()
                if deadline is not None:
                    timeout = max(0.0, min(timeout, deadline - time.monotonic()))
                try:
                    readable, _, _ = select.select([self.sock, self._wake_r], [], [], timeout)
                except (OSError, ValueError):
                    break
                if self._wake_r in readable:
                    try:
                        while self._wake_r.recv(4096):
                            pass
                    except (BlockingIOError, OSError):
                        pass
                if self.sock in readable:
                    self._receive_all()
                deadline = connection.loss_timer()
                if deadline is not None and deadline <= time.monotonic():
                    self._send(connection.on_timeout())
                self._send(connection.drain_outbound())
                if connection.handshake_complete or connection.closed:
                    self._handshake.set()
                if connection.closed:
                    break
                if self.draining and self.idle():
                    break
        finally:
            if not connection.closed:
                try:
                    self._send(connection.close(http3.H3_NO_ERROR, ""))
                except Exception:
                    pass
            self._handshake.set()
            self._fail_all(connection.error or "connection closed")
            self._stop.set()
            # The thread owns the sockets; nothing uses them once it ends.
            self._shutdown_socket()

    def _receive_all(self):
        while True:
            try:
                datagram = self.sock.recv(65535)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                # ICMP port unreachable and friends surface here.
                self.connection.error = self.connection.error or f"socket error: {exc}"
                self.connection.closed = True
                return
            self._send(self.connection.receive_datagram(datagram))
            if self.connection.closed:
                return

    def _fail_all(self, reason):
        with self._lock:
            pending, self._pending = self._pending, {}
        for entry in pending.values():
            if not entry.event.is_set():
                entry.error = reason
                entry.event.set()

    # -- stream callbacks (I/O thread) ---------------------------------

    def _on_stream_data(self, connection, stream_id, data, complete):
        with self._lock:
            entry = self._pending.get(stream_id)
        if entry is None:
            # Server control and QPACK streams, or a request we gave up on.
            return []
        if data:
            entry.data += data
            if len(entry.data) > entry.limit:
                self.forget(stream_id)
                connection.reset_stream(stream_id, H3_REQUEST_CANCELLED)
                entry.error = "response exceeds max_response_bytes"
                entry.event.set()
                return []
        if complete:
            self.forget(stream_id)
            try:
                entry.response = _parse_response(bytes(entry.data))
            except (http3.H3Error, ValueError) as exc:
                entry.error = f"malformed response: {exc}"
            entry.event.set()
        return []

    def _on_stream_reset(self, stream_id, error_code):
        with self._lock:
            entry = self._pending.pop(stream_id, None)
        if entry is not None:
            entry.error = f"stream reset by the server (0x{error_code:x})"
            entry.event.set()


def _parse_response(data):
    frames, tail = http3.parse_frames(data, allow_partial=False)
    if tail:
        raise http3.H3Error("trailing bytes after the last frame")
    headers = None
    trailers = {}
    body = bytearray()
    for frame_type, payload in frames:
        if frame_type == http3.FRAME_HEADERS:
            fields = decode_field_section(payload)
            if headers is None:
                section = {}
                for name, value in fields:
                    if name in section and not name.startswith(":"):
                        section[name] = f"{section[name]}, {value}"
                    else:
                        section[name] = value
                status = section.get(":status", "")
                if status.isdigit() and status.startswith("1"):
                    continue  # An interim response; the real one follows.
                headers = section
            else:
                trailers.update(dict(fields))
        elif frame_type == http3.FRAME_DATA:
            if headers is None:
                raise http3.H3Error("DATA before HEADERS")
            body += payload
        # Unknown and reserved frame types are ignored (RFC 9114 section 9).
    if headers is None:
        raise http3.H3Error("no response HEADERS")
    status = headers.pop(":status", "")
    if not status.isdigit():
        raise http3.H3Error("response without a valid :status")
    return Http3Response(int(status), headers, body, trailers)


class Http3Client:
    """Requests to one service over a reused QUIC connection.

    ``trust`` (a :class:`CertificateTrust`, or a
    :class:`~wsbuilder.CertificateAuthority`) or ``ca_pem`` names the
    authorities the server's certificate must chain to; the platform store
    is used otherwise. ``client_material`` (a :class:`~wsbuilder.TLSMaterial`)
    is presented when the server asks for a client certificate.
    """

    def __init__(
        self,
        host,
        port=443,
        *,
        server_name=None,
        trust=None,
        ca_pem=None,
        client_material=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
        idle_reconnect_seconds=DEFAULT_IDLE_RECONNECT_SECONDS,
    ):
        self.host = str(host)
        self.port = int(port)
        self.server_name = str(server_name or host)
        self.authority = self.server_name if self.port == 443 else f"{self.server_name}:{self.port}"
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.idle_reconnect_seconds = float(idle_reconnect_seconds)
        self._trust = build_trust(trust, ca_pem)
        self._identity = _client_identity(client_material)
        self._lock = threading.Lock()
        self._session = None
        self._retired = []
        self._closed = False
        self.connections_opened = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _resolve(self):
        infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_DGRAM)
        if not infos:
            raise Http3Error(f"cannot resolve {self.host}")
        family, _type, _proto, _name, address = infos[0]
        return family, address

    def _current_session(self, timeout):
        with self._lock:
            if self._closed:
                raise Http3Error("client is closed")
            session = self._session
            stale = (
                session is not None
                and session.idle()
                and time.monotonic() - session.last_used > self.idle_reconnect_seconds
            )
            if session is not None and session.usable and not stale:
                return session
            if session is not None:
                self._retire(session)
            family, address = self._resolve()
            session = _Session(
                address,
                family,
                self.server_name,
                self._trust,
                self._identity,
                self.max_response_bytes,
            )
            # Connect under the lock: concurrent callers wait for this
            # handshake instead of each opening a connection of their own.
            session.connect(timeout)
            self.connections_opened += 1
            self._session = session
            return session

    def _retire(self, session):
        """Let a session finish its in-flight requests, then close."""
        session.draining = True
        session._wake()
        self._retired = [old for old in self._retired if not old._stop.is_set()]
        self._retired.append(session)

    def request(self, method, path, *, headers=None, body=b"", timeout=None):
        """Send one request and wait for the whole response."""
        wait = self.timeout if timeout is None else float(timeout)
        started = time.monotonic()
        payload = self._encode_request(method, path, headers, body)
        session = self._current_session(wait)
        stream_id, pending = session.submit(payload)
        remaining = max(0.0, wait - (time.monotonic() - started))
        if not pending.event.wait(remaining):
            session.cancel(stream_id)
            raise Http3Error(f"{method} {path} timed out after {wait:.3g}s")
        if pending.error:
            raise Http3Error(pending.error)
        return pending.response

    def _encode_request(self, method, path, headers, body):
        body = bytes(body or b"")
        path = str(path or "/")
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        fields = [
            (":method", str(method).upper()),
            (":scheme", "https"),
            (":authority", self.authority),
            (":path", path),
        ]
        names = set()
        for name, value in (headers or {}).items():
            lowered = str(name).lower()
            if lowered in ("host", "connection", "transfer-encoding", "keep-alive", "upgrade"):
                continue  # Connection-specific fields are illegal in HTTP/3.
            names.add(lowered)
            fields.append((lowered, str(value)))
        if body and "content-length" not in names:
            fields.append(("content-length", str(len(body))))
        payload = http3.encode_frame(http3.FRAME_HEADERS, encode_field_section(fields))
        if body:
            payload += http3.encode_frame(http3.FRAME_DATA, body)
        return payload

    def close(self):
        with self._lock:
            self._closed = True
            sessions = [s for s in [self._session, *self._retired] if s is not None]
            self._session = None
            self._retired = []
        for session in sessions:
            session.close()

    def describe(self):
        session = self._session
        return {
            "protocol": "HTTP/3",
            "authority": self.authority,
            "connections_opened": self.connections_opened,
            "connection": session.connection.describe() if session is not None else None,
            "pid": os.getpid(),
        }

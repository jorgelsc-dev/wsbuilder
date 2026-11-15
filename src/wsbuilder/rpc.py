"""Service-to-service calls with a compact binary body.

Between services nobody reads the payload, so there is no reason to spend
bytes writing it as text. Here a call carries its payload as CBOR (RFC 8949):
the caller hands over a Python object, it is encoded straight to bytes, sent,
and the receiving handler gets the decoded object back. JSON text never
appears in the middle; a service that must answer a browser in JSON can still
do so at its own edge, and JSON stays available as a codec for debugging.

The transport is HTTP/3 by default for ``https://`` addresses: one QUIC
connection is kept open and each call is a new stream on it, so repeated
calls pay no handshake and never queue behind each other, and everything on
the wire is encrypted with TLS 1.3. ``http://`` addresses, or
``transport="http1"``, use HTTP/1.1 keep-alive connections (with TLS for
``https://``) through the standard library.

Server side::

    app = App()

    @app.rpc("/users.get")
    def get_user(request, payload):
        return {"id": payload["id"], "name": "Ada"}

Client side::

    with RpcClient("https://users.internal:8443", trust=ca) as users:
        user = users.call("/users.get", {"id": 7})

A handler raises :class:`RpcError` to answer with a structured error, which
the client raises again on its side with the same code and details.
"""

import http.client
import json
import ssl
import threading
from urllib.parse import urlsplit

from .codec import DEFAULT_MAX_DEPTH, CBORDecodeError, CBOREncodeError, cbor_dumps, cbor_loads
from .headers import get_header
from .http import Response
from .logs import get_log

CBOR_MEDIA_TYPE = "application/cbor"
JSON_MEDIA_TYPE = "application/json"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

TRANSPORT_HTTP1 = "http1"
TRANSPORT_HTTP3 = "http3"

__all__ = [
    "CBOR_MEDIA_TYPE",
    "JSON_MEDIA_TYPE",
    "RpcClient",
    "RpcCodec",
    "RpcError",
    "RpcTransportError",
    "TRANSPORT_HTTP1",
    "TRANSPORT_HTTP3",
    "get_codec",
    "rpc_endpoint",
]


class RpcCodec:
    """How a payload becomes bytes and back, and the media type naming it."""

    __slots__ = ("name", "media_type", "_dumps", "_loads")

    def __init__(self, name, media_type, dumps, loads):
        self.name = name
        self.media_type = media_type
        self._dumps = dumps
        self._loads = loads

    @property
    def content_type(self):
        if self.media_type == JSON_MEDIA_TYPE:
            return f"{JSON_MEDIA_TYPE}; charset=utf-8"
        return self.media_type

    def dumps(self, value):
        return self._dumps(value)

    def loads(self, data):
        return self._loads(data)

    def __repr__(self):
        return f"<RpcCodec {self.name}>"


def _json_dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise CBOREncodeError(str(exc)) from exc


def _json_loads(data):
    try:
        return json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CBORDecodeError(f"invalid JSON: {exc}") from exc


def _cbor_loads(data):
    return cbor_loads(data, max_depth=DEFAULT_MAX_DEPTH)


_CODECS = {
    "cbor": RpcCodec("cbor", CBOR_MEDIA_TYPE, cbor_dumps, _cbor_loads),
    "json": RpcCodec("json", JSON_MEDIA_TYPE, _json_dumps, _json_loads),
}
_BY_MEDIA_TYPE = {codec.media_type: codec for codec in _CODECS.values()}


def get_codec(name):
    """A codec by name (``"cbor"``, ``"json"``) or by media type."""
    if isinstance(name, RpcCodec):
        return name
    key = str(name or "").split(";", 1)[0].strip().lower()
    codec = _CODECS.get(key) or _BY_MEDIA_TYPE.get(key)
    if codec is None:
        raise ValueError(f"unknown RPC codec {name!r}; use 'cbor' or 'json'")
    return codec


def _codec_for_media_type(value):
    key = str(value or "").split(";", 1)[0].strip().lower()
    return _BY_MEDIA_TYPE.get(key)


class RpcError(Exception):
    """A call that failed in a way the caller should know about.

    Raised by a handler, it becomes an error response with ``status`` and a
    body of ``{"error": {"code", "message", "details"}}``; on the client the
    same exception is raised again from that body.
    """

    def __init__(self, message, *, code="error", status=400, details=None):
        super().__init__(str(message))
        self.message = str(message)
        self.code = str(code)
        self.status = int(status)
        if not 400 <= self.status <= 599:
            raise ValueError("an RPC error status must be between 400 and 599")
        self.details = details

    def to_payload(self):
        error = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}

    def __repr__(self):
        return f"RpcError({self.message!r}, code={self.code!r}, status={self.status})"


class RpcTransportError(ConnectionError):
    """The call did not complete: no connection, timeout, or a broken reply."""


# -- server -----------------------------------------------------------------


def _response_codec(request, request_codec, default):
    accept = str(get_header(request.headers, "accept", default="") or "").lower()
    if CBOR_MEDIA_TYPE in accept:
        return _CODECS["cbor"]
    if JSON_MEDIA_TYPE in accept:
        return _CODECS["json"]
    return request_codec or default


def _encoded(codec, payload, status=200):
    return Response(status=status, body=codec.dumps(payload), headers={"Content-Type": codec.content_type})


def _error_response(codec, error):
    return _encoded(codec, error.to_payload(), status=error.status)


def rpc_endpoint(handler, *, default_codec="cbor"):
    """Wrap ``handler(request, payload)`` as an app route handler.

    The request body is decoded with the codec its ``Content-Type`` names
    (CBOR or JSON); an empty body is ``None``. The reply uses the codec the
    ``Accept`` header asks for, else the request's, else ``default_codec``.
    A returned :class:`~wsbuilder.Response` is passed through untouched.
    """
    default = get_codec(default_codec)

    def endpoint(request):
        content_type = get_header(request.headers, "content-type", default="")
        request_codec = _codec_for_media_type(content_type)
        reply_codec = _response_codec(request, request_codec, default)
        body = request.body or b""
        if body and request_codec is None:
            return _error_response(
                reply_codec,
                RpcError(
                    f"unsupported Content-Type {content_type or '(none)'!r}; "
                    f"send {CBOR_MEDIA_TYPE} or {JSON_MEDIA_TYPE}",
                    code="unsupported_media_type",
                    status=415,
                ),
            )
        try:
            payload = request_codec.loads(body) if body else None
        except (CBORDecodeError, ValueError) as exc:
            return _error_response(
                reply_codec, RpcError(f"undecodable payload: {exc}", code="bad_payload", status=400)
            )
        try:
            result = handler(request, payload)
        except RpcError as exc:
            return _error_response(reply_codec, exc)
        except Exception as exc:
            get_log(getattr(request, "app", None)).exception(
                "rpc", f"handler error {request.path}: {exc}", exc
            )
            return _error_response(
                reply_codec, RpcError("Internal Server Error", code="internal", status=500)
            )
        if isinstance(result, Response):
            return result
        try:
            return _encoded(reply_codec, result)
        except (CBOREncodeError, ValueError, RecursionError) as exc:
            get_log(getattr(request, "app", None)).exception(
                "rpc", f"cannot encode the result of {request.path}: {exc}", exc
            )
            return _error_response(
                reply_codec,
                RpcError("the result could not be encoded", code="internal", status=500),
            )

    endpoint.__name__ = getattr(handler, "__name__", "rpc_endpoint")
    endpoint.__doc__ = getattr(handler, "__doc__", None)
    endpoint.rpc_handler = handler
    return endpoint


# -- client transports ------------------------------------------------------


class _Http1Transport:
    """HTTP/1.1 keep-alive connections, one per calling thread."""

    name = TRANSPORT_HTTP1

    def __init__(self, scheme, host, port, *, ssl_context, server_name, max_response_bytes):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.server_name = server_name
        self.max_response_bytes = int(max_response_bytes)
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()
        self._closed = False

    def _connection(self, timeout):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            return connection, True
        if self.scheme == "https":
            connection = _ServerNameHTTPSConnection(
                self.host,
                self.port,
                timeout=timeout,
                context=self.ssl_context,
                server_name=self.server_name,
            )
        else:
            connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        self._local.connection = connection
        with self._lock:
            self._all.append(connection)
        return connection, False

    def _discard(self):
        connection = getattr(self._local, "connection", None)
        self._local.connection = None
        if connection is not None:
            connection.close()
            with self._lock:
                if connection in self._all:
                    self._all.remove(connection)

    def request(self, method, path, headers, body, timeout):
        if self._closed:
            raise RpcTransportError("client is closed")
        for attempt in (1, 2):
            connection, reused = self._connection(timeout)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as exc:
                self._discard()
                # A kept-alive connection the server closed while idle fails
                # before the request reached it; one fresh attempt is safe.
                if reused and attempt == 1:
                    continue
                raise RpcTransportError(f"connection lost: {exc}") from exc
            except TimeoutError as exc:
                self._discard()
                raise RpcTransportError(f"{method} {path} timed out after {timeout:.3g}s") from exc
            except (OSError, http.client.HTTPException) as exc:
                self._discard()
                raise RpcTransportError(str(exc) or exc.__class__.__name__) from exc
            try:
                data = response.read(self.max_response_bytes + 1)
                if len(data) > self.max_response_bytes:
                    self._discard()
                    raise RpcTransportError("response exceeds max_response_bytes")
                if not response.isclosed():
                    response.read()  # Leave the connection ready for the next call.
            except (OSError, http.client.HTTPException) as exc:
                self._discard()
                raise RpcTransportError(f"broken response: {exc}") from exc
            if response.will_close:
                self._discard()
            headers_out = {name.lower(): value for name, value in response.getheaders()}
            return response.status, headers_out, data
        raise RpcTransportError("unreachable")  # pragma: no cover

    def close(self):
        self._closed = True
        with self._lock:
            connections, self._all = self._all, []
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass


class _ServerNameHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection whose certificate name can differ from its address."""

    def __init__(self, host, port, *, server_name, **kwargs):
        super().__init__(host, port, **kwargs)
        self._server_name = server_name

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_name)


class _Http3Transport:
    name = TRANSPORT_HTTP3

    def __init__(self, host, port, *, trust, ca_pem, client_material, server_name, timeout,
                 max_response_bytes):
        try:
            from .http3_client import Http3Client
        except ImportError as exc:
            raise ImportError(
                "RPC over HTTP/3 needs the 'cryptography' package. "
                "Install it with: pip install wsbuilder[tls] (or use transport='http1')"
            ) from exc

        self.client = Http3Client(
            host,
            port,
            server_name=server_name,
            trust=trust,
            ca_pem=ca_pem,
            client_material=client_material,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )

    def request(self, method, path, headers, body, timeout):
        from .http3_client import Http3Error

        try:
            response = self.client.request(method, path, headers=headers, body=body, timeout=timeout)
        except Http3Error as exc:
            raise RpcTransportError(str(exc)) from exc
        except OSError as exc:
            raise RpcTransportError(str(exc) or exc.__class__.__name__) from exc
        return response.status, response.headers, response.body

    def close(self):
        self.client.close()


def _ssl_context(trust, ca_pem, client_material):
    """A verifying client context, presenting ``client_material`` if given."""
    if trust is not None and not isinstance(trust, (bytes, str)) and ca_pem is None:
        ca_pem = getattr(trust, "certificate_pem", None)
        if ca_pem is None:
            raise TypeError("for HTTP/1.1, trust must be a CertificateAuthority or PEM")
    elif isinstance(trust, (bytes, str)) and ca_pem is None:
        ca_pem = trust
    if ca_pem is not None:
        cadata = ca_pem.decode("ascii") if isinstance(ca_pem, (bytes, bytearray)) else str(ca_pem)
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cadata=cadata)
    else:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if client_material is not None:
        with client_material.materialize() as files:
            context.load_cert_chain(
                certfile=files.certificate,
                keyfile=files.private_key,
                password=getattr(client_material, "key_password", None),
            )
    return context


# -- client -----------------------------------------------------------------


class RpcClient:
    """Calls to the RPC endpoints of one service.

    ``base_url`` is ``https://host:port`` (HTTP/3 by default) or
    ``http://host:port`` (plain HTTP/1.1, for local development).
    ``transport`` forces ``"http3"`` or ``"http1"``. ``trust`` (a
    :class:`~wsbuilder.CertificateAuthority` or PEM) or ``ca_pem`` names the
    authorities the server certificate must chain to; ``client_material`` (a
    :class:`~wsbuilder.TLSMaterial`) is the client certificate for mutual TLS.
    ``server_name`` is the name to verify when it differs from the host
    (calling ``127.0.0.1`` for a certificate issued to ``users.internal``).
    """

    def __init__(
        self,
        base_url,
        *,
        transport=None,
        codec="cbor",
        trust=None,
        ca_pem=None,
        client_material=None,
        server_name=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        headers=None,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
    ):
        parts = urlsplit(str(base_url))
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError("base_url must start with http:// or https://")
        if not parts.hostname:
            raise ValueError("base_url needs a host")
        self.base_url = str(base_url).rstrip("/")
        self.scheme = scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.codec = get_codec(codec)
        self.timeout = float(timeout)
        self.headers = dict(headers or {})
        self.server_name = server_name or self.host
        if transport is None:
            transport = TRANSPORT_HTTP3 if scheme == "https" else TRANSPORT_HTTP1
        transport = str(transport).lower()
        if transport not in (TRANSPORT_HTTP1, TRANSPORT_HTTP3):
            raise ValueError("transport must be 'http1' or 'http3'")
        if transport == TRANSPORT_HTTP3:
            if scheme != "https":
                raise ValueError("HTTP/3 is always encrypted: use an https:// base_url")
            self._transport = _Http3Transport(
                self.host,
                self.port,
                trust=trust,
                ca_pem=ca_pem,
                client_material=client_material,
                server_name=self.server_name,
                timeout=self.timeout,
                max_response_bytes=max_response_bytes,
            )
        else:
            context = None
            if scheme == "https":
                context = _ssl_context(trust, ca_pem, client_material)
            elif client_material is not None:
                raise ValueError("a client certificate needs an https:// base_url")
            self._transport = _Http1Transport(
                scheme,
                self.host,
                self.port,
                ssl_context=context,
                server_name=self.server_name,
                max_response_bytes=max_response_bytes,
            )
        self.transport = self._transport.name
        self._stats_lock = threading.Lock()
        self.calls = 0
        self.bytes_sent = 0
        self.bytes_received = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, path, payload=None, *, headers=None, timeout=None):
        """Call ``path`` with ``payload`` and return the decoded result.

        Raises :class:`RpcError` for an error answer and
        :class:`RpcTransportError` when no answer arrived.
        """
        target = str(path or "/")
        if not target.startswith("/"):
            target = "/" + target
        target = self.prefix + target
        body = b"" if payload is None else self.codec.dumps(payload)
        request_headers = {
            "Accept": self.codec.media_type,
            "User-Agent": "wsbuilder-rpc",
        }
        if body:
            request_headers["Content-Type"] = self.codec.content_type
        request_headers.update(self.headers)
        if headers:
            request_headers.update(headers)
        wait = self.timeout if timeout is None else float(timeout)
        status, response_headers, data = self._transport.request(
            "POST", target, request_headers, body, wait
        )
        with self._stats_lock:
            self.calls += 1
            self.bytes_sent += len(body)
            self.bytes_received += len(data)
        return self._decode(status, response_headers, data, target)

    def _decode(self, status, headers, data, target):
        codec = _codec_for_media_type(headers.get("content-type"))
        result = None
        if data:
            if codec is None:
                if status >= 400:
                    raise RpcError(
                        data.decode("utf-8", "replace")[:500] or f"HTTP {status}",
                        code="http_error",
                        status=status if 400 <= status <= 599 else 502,
                    )
                raise RpcTransportError(
                    f"{target}: unexpected Content-Type {headers.get('content-type')!r}"
                )
            try:
                result = codec.loads(data)
            except (CBORDecodeError, ValueError) as exc:
                raise RpcTransportError(f"{target}: undecodable response: {exc}") from exc
        if status >= 400:
            error = result.get("error") if isinstance(result, dict) else None
            if isinstance(error, dict):
                raise RpcError(
                    error.get("message", f"HTTP {status}"),
                    code=error.get("code", "error"),
                    status=status if 400 <= status <= 599 else 502,
                    details=error.get("details"),
                )
            raise RpcError(
                f"HTTP {status}", code="http_error", status=status if 400 <= status <= 599 else 502
            )
        if not 200 <= status < 300:
            raise RpcTransportError(f"{target}: unexpected status {status}")
        return result

    def close(self):
        self._transport.close()

    def describe(self):
        return {
            "base_url": self.base_url,
            "transport": self.transport,
            "codec": self.codec.name,
            "calls": self.calls,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
        }

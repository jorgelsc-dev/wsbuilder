"""Access control and stream limits for built-in operational endpoints.

Metrics snapshots, NDJSON metric streams and the ProxyI dashboard describe
routes, errors and upstream addresses, and a ``follow`` stream holds a
connection thread for as long as the client keeps it open. ``EndpointGuard``
keeps them away from the public Internet by default and bounds the streams.
"""

import hmac
import ipaddress
import threading
import time

from .http import Response

#: Loopback, RFC 1918, link-local, CGNAT and IPv6 unique-local addresses.
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
LOOPBACK_NETWORKS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))

ACCESS_ANY = "*"
ACCESS_PRIVATE = "private"
ACCESS_LOOPBACK = "loopback"

DEFAULT_MAX_STREAMS = 4
DEFAULT_MAX_STREAM_SECONDS = 300.0


def _networks(access):
    if access == ACCESS_PRIVATE:
        return PRIVATE_NETWORKS
    if access == ACCESS_LOOPBACK:
        return LOOPBACK_NETWORKS
    if isinstance(access, str):
        access = (access,)
    networks = []
    for item in access:
        try:
            networks.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid address or network in access: {item!r}") from exc
    return tuple(networks)


def client_ip(request, app=None):
    """The client address, through trusted proxies when security is enabled."""
    security = getattr(app, "security", None) if app is not None else None
    resolve = getattr(security, "resolve_client_ip", None)
    if callable(resolve):
        resolved = resolve(request)
        if resolved:
            return resolved
    client = getattr(request, "client", None)
    if isinstance(client, (tuple, list)) and client:
        return str(client[0] or "")
    return ""


class EndpointGuard:
    """Who may read an operational endpoint, and how many streams may run.

    ``access``: ``"private"`` (default: loopback and private networks),
    ``"loopback"``, ``"*"`` (anyone) or an iterable of addresses/CIDRs.
    ``token``: when set, requests must also send ``Authorization: Bearer <token>``.
    ``authorize(request) -> bool``: when set, it alone decides.
    ``max_streams``: concurrent NDJSON streams; more are answered 429.
    ``max_stream_seconds``: a stream ends after this long, even with ``follow``.
    """

    def __init__(
        self,
        access=ACCESS_PRIVATE,
        *,
        token=None,
        authorize=None,
        max_streams=DEFAULT_MAX_STREAMS,
        max_stream_seconds=DEFAULT_MAX_STREAM_SECONDS,
    ):
        if authorize is not None and not callable(authorize):
            raise TypeError("authorize must be callable")
        self.access = access if access is not None else ACCESS_PRIVATE
        self._networks = None if self.access == ACCESS_ANY else _networks(self.access)
        self.token = str(token) if token else None
        self.authorize = authorize
        self.max_streams = max(1, int(max_streams))
        self.max_stream_seconds = max(1.0, float(max_stream_seconds))
        self._lock = threading.Lock()
        self._active_streams = 0

    @property
    def active_streams(self):
        with self._lock:
            return self._active_streams

    def check(self, request, app=None):
        """``None`` when the request may proceed, else the refusal to send."""
        if self.authorize is not None:
            if self.authorize(request):
                return None
            return Response.text("Forbidden", status=403)
        if self._networks is not None:
            text = client_ip(request, app)
            try:
                address = ipaddress.ip_address(text.split("%", 1)[0])
            except ValueError:
                return Response.text("Forbidden", status=403)
            if not any(address in net for net in self._networks if net.version == address.version):
                return Response.text(
                    "Forbidden: this endpoint only answers allowed networks "
                    "(see the access= option)",
                    status=403,
                )
        if self.token is not None:
            headers = getattr(request, "headers", {}) or {}
            supplied = str(headers.get("authorization", "") or "")
            scheme, _, value = supplied.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip(), self.token):
                return Response.text(
                    "Unauthorized", status=401, headers={"WWW-Authenticate": 'Bearer realm="wsbuilder"'}
                )
        return None

    def acquire_stream(self):
        """Reserve a stream slot; ``False`` when all are taken."""
        with self._lock:
            if self._active_streams >= self.max_streams:
                return False
            self._active_streams += 1
            return True

    def release_stream(self):
        with self._lock:
            self._active_streams = max(0, self._active_streams - 1)

    def bounded(self, chunks):
        """Wrap a reserved stream: stop at ``max_stream_seconds``, then release."""
        deadline = time.monotonic() + self.max_stream_seconds
        try:
            for chunk in chunks:
                yield chunk
                if time.monotonic() >= deadline:
                    break
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
            self.release_stream()

    def too_many_streams(self):
        return Response.text(
            "Too Many Requests: metrics stream limit reached",
            status=429,
            headers={"Retry-After": "5"},
        )


def coerce_guard(guard=None, **options):
    """An ``EndpointGuard`` from an existing one or from keyword options."""
    if guard is not None:
        if not isinstance(guard, EndpointGuard):
            raise TypeError("guard must be an EndpointGuard")
        return guard
    return EndpointGuard(**{key: value for key, value in options.items() if value is not None})


__all__ = [
    "ACCESS_ANY",
    "ACCESS_LOOPBACK",
    "ACCESS_PRIVATE",
    "EndpointGuard",
    "client_ip",
    "coerce_guard",
]

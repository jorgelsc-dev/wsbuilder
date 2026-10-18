"""Optional mDNS/DNS-SD advertising and discovery using python-zeroconf."""

import ipaddress
import math
import queue
import re
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

DEFAULT_SERVICE_TYPE = "_http._tcp.local."


def _backend():
    try:
        import zeroconf
    except ImportError as exc:
        raise ImportError(
            "mDNS needs the 'zeroconf' package. "
            "Install it with: pip install wsbuilder[mdns]"
        ) from exc
    return zeroconf


def _service_type(value):
    value = str(value).strip().lower().rstrip(".")
    if value.count(".") == 1:
        value += ".local"
    if not re.fullmatch(r"_[a-z0-9-]+\._(?:tcp|udp)\.local", value):
        raise ValueError("service_type must be _service._tcp.local. or _service._udp.local.")
    label = value.split(".")[0][1:]
    if (
        len(label) > 15 or not re.search(r"[a-z]", label)
        or label.startswith("-") or label.endswith("-") or "--" in label
    ):
        raise ValueError("invalid DNS-SD service label")
    return value + "."


def _service_name(name, service_type):
    name = str(name).strip()
    suffix = "." + service_type
    if name.lower().endswith(suffix):
        name = name[:-len(suffix)]
    if not name or len(name.encode("utf-8")) > 63:
        raise ValueError("service instance name must contain 1 to 63 UTF-8 bytes")
    return name + suffix


def _hostname(value):
    if value is None:
        value = socket.gethostname().split(".")[0]
    value = str(value).strip().rstrip(".").lower()
    if "." not in value:
        value += ".local"
    if not value.endswith(".local"):
        raise ValueError("hostname must belong to .local")
    labels = value.split(".")
    encoded = [label.encode("idna").decode("ascii") for label in labels]
    if any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in encoded
    ) or sum(len(label) + 1 for label in encoded) + 1 > 255:
        raise ValueError("invalid mDNS hostname")
    return ".".join(encoded) + "."


def _addresses(values):
    if isinstance(values, str):
        values = [values]
    packed = []
    for value in values:
        address = ipaddress.ip_address(value)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("advertised addresses must be concrete unicast IP addresses")
        if address.packed not in packed:
            packed.append(address.packed)
    if not packed:
        raise ValueError("at least one advertised IP address is required")
    return packed


def _properties(values):
    result = {}
    for key, value in (values or {}).items():
        key = key.encode("ascii") if isinstance(key, str) else key
        if (
            not isinstance(key, bytes) or not key or b"=" in key
            or any(byte < 0x20 or byte > 0x7E for byte in key)
        ):
            raise ValueError("TXT keys must be nonempty printable ASCII without '='")
        if isinstance(value, str):
            value = value.encode("utf-8")
        if value is not None and not isinstance(value, bytes):
            raise ValueError("TXT values must be strings, bytes or None")
        if len(key) + (1 + len(value) if value is not None else 0) > 255:
            raise ValueError("TXT segment too long (>255 bytes)")
        if key.lower() in {existing.lower() for existing in result}:
            raise ValueError("TXT keys must be unique ignoring case")
        result[key] = value
    return result


def _timeout(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    return value


@dataclass(frozen=True)
class MDNSService:
    """Snapshot of a published or discovered service; TXT data stays as bytes."""

    name: str
    service_type: str
    hostname: str
    port: int
    addresses: tuple[str, ...]
    properties: Mapping[bytes, bytes | None]
    interface_index: int | None = None


def _snapshot(info):
    return MDNSService(
        name=info.name,
        service_type=info.type,
        hostname=info.server,
        port=info.port,
        addresses=tuple(info.parsed_scoped_addresses()),
        properties=MappingProxyType(dict(info.properties)),
        interface_index=info.interface_index,
    )


class LocalMDNSServer:
    """Publish and resolve DNS-SD services on the local link.

    Install ``wsbuilder[mdns]`` before starting. ``interfaces`` accepts local
    interface IP addresses (or IPv6 interface indexes, as in Zeroconf).
    ``ip_version`` is ``"v4"`` (default), ``"v6"`` or ``"all"``. Published
    A/AAAA records are independent of the multicast transport version.
    """

    def __init__(self, *, interfaces=None, ip_version="v4"):
        if ip_version not in ("v4", "v6", "all"):
            raise ValueError("ip_version must be 'v4', 'v6' or 'all'")
        if isinstance(interfaces, str):
            interfaces = [interfaces]
        self.interfaces = None if interfaces is None else list(interfaces)
        if self.interfaces == []:
            raise ValueError("interfaces cannot be empty")
        self.ip_version = ip_version
        self._zeroconf = None
        self._services = {}
        self._lock = threading.RLock()

    @property
    def services(self):
        """Return snapshots of the services registered by this instance."""
        with self._lock:
            return tuple(_snapshot(info) for info in self._services.values())

    def start(self):
        """Open multicast sockets once, returning this server."""
        with self._lock:
            if self._zeroconf is None:
                backend = _backend()
                versions = {
                    "v4": backend.IPVersion.V4Only,
                    "v6": backend.IPVersion.V6Only,
                    "all": backend.IPVersion.All,
                }
                self._zeroconf = backend.Zeroconf(
                    interfaces=(
                        backend.InterfaceChoice.All
                        if self.interfaces is None else self.interfaces
                    ),
                    ip_version=versions[self.ip_version],
                )
        return self

    def register_service(
        self, name, port, addresses, *, service_type=DEFAULT_SERVICE_TYPE,
        hostname=None, properties=None, allow_name_change=False,
    ):
        """Probe and announce PTR, SRV, TXT and A/AAAA records.

        A conflicting instance name raises ``zeroconf.NonUniqueNameException``
        unless ``allow_name_change=True``. The returned snapshot contains the
        actual registered name, including any automatic rename.
        """
        service_type = _service_type(service_type)
        name = _service_name(name, service_type)
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        hostname = _hostname(hostname)
        addresses = _addresses(addresses)
        properties = _properties(properties)
        backend = _backend()
        info = backend.ServiceInfo(
            service_type, name, port=port, server=hostname,
            addresses=addresses, properties=properties,
        )
        with self._lock:
            if name.lower() in self._services:
                raise ValueError(f"service already registered: {name}")
            self.start()
            self._zeroconf.register_service(info, allow_name_change=allow_name_change)
            self._services[info.name.lower()] = info
            return _snapshot(info)

    def unregister_service(self, service, *, service_type=DEFAULT_SERVICE_TYPE):
        """Withdraw an instance with goodbye records; return whether it existed."""
        if isinstance(service, MDNSService):
            name = service.name
        else:
            name = _service_name(service, _service_type(service_type))
        with self._lock:
            info = self._services.get(name.lower())
            if info is None:
                return False
            self._zeroconf.unregister_service(info)
            del self._services[name.lower()]
            return True

    def resolve_service(
        self, name, *, service_type=DEFAULT_SERVICE_TYPE, timeout=3.0,
    ):
        """Resolve an instance name or full service name; return None on timeout."""
        service_type = _service_type(service_type)
        name = _service_name(name, service_type)
        timeout = _timeout(timeout)
        with self._lock:
            self.start()
            info = self._zeroconf.get_service_info(
                service_type, name, timeout=max(1, int(timeout * 1000)),
            )
            return None if info is None else _snapshot(info)

    def close(self):
        """Send goodbyes for all services and release sockets and threads."""
        with self._lock:
            if self._zeroconf is not None:
                try:
                    self._zeroconf.close()
                finally:
                    self._zeroconf = None
                    self._services.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


class _ServiceCollector:
    """Browser callbacks enqueue names; network resolution runs in the caller."""

    def __init__(self):
        self.events = queue.Queue()
        self.names = {}
        self.revision = 0
        self.lock = threading.Lock()

    def add_service(self, zeroconf, service_type, name):
        with self.lock:
            self.revision += 1
            self.names[name] = self.revision
            self.events.put((name, self.revision))

    update_service = add_service

    def remove_service(self, zeroconf, service_type, name):
        with self.lock:
            self.names.pop(name, None)
            self.events.put((name, None))

    def is_current(self, name, revision):
        with self.lock:
            return self.names.get(name) == revision


def discover_mdns_services(
    service_type=DEFAULT_SERVICE_TYPE, *, timeout=3.0,
    interfaces=None, ip_version="v4",
):
    """Browse for up to timeout seconds and return resolved MDNSService snapshots.

    An empty tuple means no services were resolved. Temporary browser threads
    and multicast sockets are closed on both success and failure.
    """
    service_type = _service_type(service_type)
    timeout = _timeout(timeout)
    server = LocalMDNSServer(interfaces=interfaces, ip_version=ip_version)
    collector = _ServiceCollector()
    browser = None
    results = {}
    try:
        server.start()
        deadline = time.monotonic() + timeout
        browser = _backend().ServiceBrowser(
            server._zeroconf, service_type, listener=collector,
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                name, revision = collector.events.get(timeout=remaining)
            except queue.Empty:
                break
            results.pop(name, None)
            if revision is None or not collector.is_current(name, revision):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            info = server._zeroconf.get_service_info(
                service_type, name,
                timeout=max(1, int(min(1.0, remaining) * 1000)),
            )
            if info is not None and collector.is_current(name, revision):
                results[name] = (revision, _snapshot(info))
        with collector.lock:
            return tuple(
                service for name, (revision, service) in sorted(results.items())
                if collector.names.get(name) == revision
            )
    finally:
        try:
            if browser is not None:
                browser.cancel()
        finally:
            server.close()

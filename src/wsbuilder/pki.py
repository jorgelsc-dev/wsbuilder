"""Certificate authority, issuance and rotation for TLS material.

``ssl.SSLContext.load_cert_chain`` only accepts file paths: OpenSSL will not
read a certificate or a private key out of memory. Material that lives
anywhere else -- a database row, a secrets manager, a value built at start-up
-- therefore has to reach the filesystem before TLS can use it.

This module keeps that window as small as it can be. PEM is written into a
private temporary directory, handed to OpenSSL, and wiped immediately: the
``SSLContext`` holds its own copy once loaded, so the files are not needed
past that call.

On wiping: the bytes are overwritten before the file is unlinked, which
defeats a later read of the same path. It is not an erasure guarantee. A
copy-on-write filesystem, a journal or an SSD's wear levelling can retain the
old blocks, so treat a host that runs this as a host that has seen the key.
"""

from __future__ import annotations

import datetime as _datetime
import ipaddress
import os
import shutil
import ssl
import tempfile
import threading
import weakref

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

UTC = _datetime.timezone.utc

DEFAULT_CA_VALID_DAYS = 3650
DEFAULT_LEAF_VALID_DAYS = 397
DEFAULT_ROTATING_VALID_DAYS = 1
#: Renew this long before expiry so a rotating certificate never serves stale.
DEFAULT_RENEW_BEFORE_SECONDS = 3600.0

KEY_EC = "ec"
KEY_RSA = "rsa"
SUPPORTED_KEY_TYPES = (KEY_EC, KEY_RSA)

_TEMP_DIR_PREFIX = "wsbuilder-tls-"


def _utcnow():
    return _datetime.datetime.now(tz=UTC)


def _generate_private_key(key_type=KEY_EC, rsa_key_size=2048):
    key_type = str(key_type or KEY_EC).strip().lower()
    if key_type == KEY_EC:
        return ec.generate_private_key(ec.SECP256R1())
    if key_type == KEY_RSA:
        size = int(rsa_key_size)
        if size < 2048:
            raise ValueError("RSA keys must be at least 2048 bits")
        return rsa.generate_private_key(public_exponent=65537, key_size=size)
    raise ValueError(f"Unsupported key type: {key_type!r}; use one of {SUPPORTED_KEY_TYPES}")


def _signature_hash(private_key):
    # Ed25519 and Ed448 carry their own hash; everything here uses SHA-256.
    return hashes.SHA256()


def _build_name(common_name, organization=None, country=None):
    attributes = [x509.NameAttribute(NameOID.COMMON_NAME, str(common_name))]
    if organization:
        attributes.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, str(organization)))
    if country:
        country_text = str(country).strip().upper()
        if len(country_text) != 2:
            raise ValueError("country must be a two-letter code")
        attributes.append(x509.NameAttribute(NameOID.COUNTRY_NAME, country_text))
    return x509.Name(attributes)


def _san_entries(dns_names=None, ip_addresses=None):
    entries = []
    for name in dns_names or ():
        text = str(name).strip()
        if text:
            entries.append(x509.DNSName(text))
    for address in ip_addresses or ():
        entries.append(x509.IPAddress(ipaddress.ip_address(str(address).strip())))
    return entries


def _private_key_pem(private_key, password=None):
    if password:
        encryption = serialization.BestAvailableEncryption(
            password if isinstance(password, bytes) else str(password).encode("utf-8")
        )
    else:
        encryption = serialization.NoEncryption()
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )


def _certificate_pem(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM)


def _wipe_file(path):
    """Overwrite a file's bytes, then remove it. Best effort by nature."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size:
        try:
            with open(path, "r+b", buffering=0) as handle:
                handle.write(b"\x00" * size)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        os.remove(path)
    except OSError:
        pass


class MaterializedFiles:
    """Paths to TLS material that exists on disk only inside a ``with`` block."""

    def __init__(self, directory, certificate, private_key, ca=None):
        self.directory = directory
        self.certificate = certificate
        self.private_key = private_key
        self.ca = ca
        # A caller who forgets to close would otherwise strand a private key
        # on disk for the life of the machine. The finalizer runs on garbage
        # collection and again at interpreter exit, and is idempotent, so
        # close() simply invokes it.
        self._finalizer = weakref.finalize(
            self, self._erase, directory, (certificate, private_key, ca)
        )

    @staticmethod
    def _erase(directory, paths):
        # Deliberately free of any reference to the instance: holding one
        # would keep it alive and the finalizer would never run.
        for path in paths:
            if path:
                _wipe_file(path)
        shutil.rmtree(directory, ignore_errors=True)

    @property
    def closed(self):
        return not self._finalizer.alive

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        self._finalizer()


class TLSMaterial:
    """A certificate, its private key and any intermediate chain, as PEM.

    This is the value you store and reload: ``certificate_pem`` and
    ``private_key_pem`` are plain bytes, so they go into a database column, a
    secrets manager or a file without further conversion.
    """

    def __init__(self, certificate_pem, private_key_pem, chain_pem=b"", key_password=None):
        self.certificate_pem = bytes(certificate_pem)
        self.private_key_pem = bytes(private_key_pem)
        self.chain_pem = bytes(chain_pem or b"")
        self.key_password = key_password
        self._certificate = x509.load_pem_x509_certificate(self.certificate_pem)
        self._check_key_matches_certificate()

    def _check_key_matches_certificate(self):
        """Reject a key that does not belong to the certificate.

        Stored material can drift apart -- a half-written row, two rotations
        interleaved. Without this the mismatch only surfaces inside OpenSSL as
        `[X509: KEY_VALUES_MISMATCH]`, far from the cause.
        """
        secret = self.key_password
        if secret is not None and not isinstance(secret, bytes):
            secret = str(secret).encode("utf-8")
        try:
            private_key = serialization.load_pem_private_key(
                self.private_key_pem, password=secret
            )
        except (ValueError, TypeError):
            # Unreadable here (wrong or absent passphrase) is not the same as
            # mismatched; leave that to whoever supplies the password.
            return
        public_format = dict(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if private_key.public_key().public_bytes(**public_format) != self._certificate.public_key(
        ).public_bytes(**public_format):
            raise ValueError(
                "the private key does not match the certificate "
                f"(subject {self._certificate.subject.rfc4514_string()})"
            )

    @property
    def certificate(self):
        return self._certificate

    @property
    def subject(self):
        return self._certificate.subject.rfc4514_string()

    @property
    def serial_number(self):
        return self._certificate.serial_number

    @property
    def not_valid_before(self):
        return self._certificate.not_valid_before_utc

    @property
    def not_valid_after(self):
        return self._certificate.not_valid_after_utc

    def seconds_until_expiry(self, now=None):
        moment = now or _utcnow()
        return (self.not_valid_after - moment).total_seconds()

    def is_expired(self, now=None):
        return self.seconds_until_expiry(now=now) <= 0

    def dns_names(self):
        try:
            san = self._certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
        except x509.ExtensionNotFound:
            return []
        return list(san.get_values_for_type(x509.DNSName))

    def describe(self):
        return {
            "subject": self.subject,
            "serial_number": self.serial_number,
            "not_valid_before": self.not_valid_before.isoformat(),
            "not_valid_after": self.not_valid_after.isoformat(),
            "seconds_until_expiry": round(self.seconds_until_expiry(), 3),
            "dns_names": self.dns_names(),
            "has_chain": bool(self.chain_pem),
        }

    def materialize(self, *, include_chain=True):
        """Write the material to a private temporary directory.

        Returns a :class:`MaterializedFiles` usable as a context manager. The
        directory is created 0700 and each file 0600, both by ``tempfile``.
        """
        directory = tempfile.mkdtemp(prefix=_TEMP_DIR_PREFIX)
        try:
            cert_payload = self.certificate_pem
            if include_chain and self.chain_pem:
                cert_payload = cert_payload + self.chain_pem
            certificate_path = _write_private(directory, "certificate.pem", cert_payload)
            key_path = _write_private(directory, "private-key.pem", self.private_key_pem)
            ca_path = None
            if self.chain_pem:
                ca_path = _write_private(directory, "chain.pem", self.chain_pem)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return MaterializedFiles(directory, certificate_path, key_path, ca_path)

    def ssl_context(
        self,
        *,
        purpose=ssl.Purpose.CLIENT_AUTH,
        client_ca_pem=None,
        require_client_cert=False,
        alpn_protocols=None,
        minimum_version=ssl.TLSVersion.TLSv1_2,
    ):
        """Build an ``SSLContext``, touching disk only for the load itself."""
        context = ssl.create_default_context(purpose)
        context.minimum_version = minimum_version
        with self.materialize() as files:
            context.load_cert_chain(
                certfile=files.certificate,
                keyfile=files.private_key,
                password=self.key_password,
            )
        if client_ca_pem:
            directory = tempfile.mkdtemp(prefix=_TEMP_DIR_PREFIX)
            ca_path = None
            try:
                ca_path = _write_private(directory, "client-ca.pem", bytes(client_ca_pem))
                context.load_verify_locations(cafile=ca_path)
            finally:
                if ca_path:
                    _wipe_file(ca_path)
                shutil.rmtree(directory, ignore_errors=True)
            context.verify_mode = (
                ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_OPTIONAL
            )
        if alpn_protocols:
            context.set_alpn_protocols([str(p) for p in alpn_protocols])
        return context


def _write_private(directory, name, payload):
    """Create a 0600 file inside ``directory`` and write ``payload`` to it."""
    path = os.path.join(directory, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return path


class CertificateAuthority:
    """A self-signed root able to issue leaf certificates."""

    def __init__(self, certificate, private_key, certificate_pem=None, private_key_pem=None):
        self._certificate = certificate
        self._private_key = private_key
        self._certificate_pem = certificate_pem or _certificate_pem(certificate)
        self._private_key_pem = private_key_pem or _private_key_pem(private_key)

    @classmethod
    def create(
        cls,
        common_name="wsbuilder Local CA",
        *,
        organization=None,
        country=None,
        valid_days=DEFAULT_CA_VALID_DAYS,
        key_type=KEY_EC,
        rsa_key_size=2048,
        not_before=None,
    ):
        private_key = _generate_private_key(key_type, rsa_key_size)
        subject = _build_name(common_name, organization, country)
        span = float(valid_days)
        if span <= 0:
            raise ValueError("valid_days must be greater than zero")
        start = not_before or (_utcnow() - _datetime.timedelta(minutes=5))
        end = start + _datetime.timedelta(days=span)
        public_key = private_key.public_key()
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(end)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
            .sign(private_key, _signature_hash(private_key))
        )
        return cls(certificate, private_key)

    @classmethod
    def load(cls, certificate_pem, private_key_pem, password=None):
        """Rebuild a CA from stored PEM, e.g. two columns of a database row."""
        certificate = x509.load_pem_x509_certificate(bytes(certificate_pem))
        secret = None
        if password is not None:
            secret = password if isinstance(password, bytes) else str(password).encode("utf-8")
        private_key = serialization.load_pem_private_key(bytes(private_key_pem), password=secret)
        return cls(certificate, private_key, bytes(certificate_pem), bytes(private_key_pem))

    @property
    def certificate_pem(self):
        return self._certificate_pem

    @property
    def private_key_pem(self):
        return self._private_key_pem

    def private_key_pem_encrypted(self, password):
        return _private_key_pem(self._private_key, password=password)

    @property
    def certificate(self):
        return self._certificate

    @property
    def subject(self):
        return self._certificate.subject.rfc4514_string()

    @property
    def not_valid_after(self):
        return self._certificate.not_valid_after_utc

    def describe(self):
        return {
            "subject": self.subject,
            "serial_number": self._certificate.serial_number,
            "not_valid_after": self.not_valid_after.isoformat(),
        }

    def issue(
        self,
        common_name,
        *,
        dns_names=None,
        ip_addresses=None,
        valid_days=DEFAULT_LEAF_VALID_DAYS,
        key_type=KEY_EC,
        rsa_key_size=2048,
        client_auth=False,
        server_auth=True,
        key_password=None,
        not_before=None,
    ):
        """Issue a leaf certificate signed by this CA."""
        if self._certificate.not_valid_after_utc <= _utcnow():
            raise ValueError("the certificate authority has expired")

        span = float(valid_days)
        if span <= 0:
            raise ValueError("valid_days must be greater than zero")
        private_key = _generate_private_key(key_type, rsa_key_size)
        public_key = private_key.public_key()
        start = not_before or (_utcnow() - _datetime.timedelta(minutes=5))
        end = start + _datetime.timedelta(days=span)
        if end > self._certificate.not_valid_after_utc:
            # A leaf outliving its issuer is rejected by every verifier.
            end = self._certificate.not_valid_after_utc

        entries = _san_entries(dns_names, ip_addresses)
        if not entries:
            # Name checking has ignored the common name since RFC 2818 was
            # replaced; without a SAN the certificate matches nothing.
            entries = _san_entries([common_name])

        usages = []
        if server_auth:
            usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
        if client_auth:
            usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
        if not usages:
            raise ValueError("a certificate needs server_auth, client_auth or both")

        builder = (
            x509.CertificateBuilder()
            .subject_name(_build_name(common_name))
            .issuer_name(self._certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(end)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(entries), critical=False)
            .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=key_type == KEY_RSA,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self._certificate.public_key()
                ),
                critical=False,
            )
        )
        certificate = builder.sign(self._private_key, _signature_hash(self._private_key))
        return TLSMaterial(
            certificate_pem=_certificate_pem(certificate),
            private_key_pem=_private_key_pem(private_key, password=key_password),
            chain_pem=self._certificate_pem,
            key_password=key_password,
        )




class CertificateManager:
    """Holds the TLS material a server is currently using.

    Two shapes, chosen by ``rotate``:

    * **static** -- material is loaded once and used until it expires.
    * **rotating** -- material is reissued whenever it is within
      ``renew_before_seconds`` of expiry.

    Where material comes from is separate from that choice. ``provider`` is
    consulted first, which is how stored material is reused: point it at the
    query that reads the PEM back. When it returns nothing, or returns
    something too close to expiry, the manager issues a fresh certificate from
    ``ca`` and hands it to ``on_rotate`` -- the hook to write it back.
    """

    def __init__(
        self,
        *,
        ca=None,
        material=None,
        provider=None,
        common_name="localhost",
        dns_names=None,
        ip_addresses=None,
        rotate=False,
        valid_days=None,
        renew_before_seconds=DEFAULT_RENEW_BEFORE_SECONDS,
        on_rotate=None,
        key_type=KEY_EC,
        alpn_protocols=None,
        client_ca_pem=None,
        require_client_cert=False,
        minimum_version=ssl.TLSVersion.TLSv1_2,
        time_source=None,
    ):
        if material is None and provider is None and ca is None:
            raise ValueError("a manager needs material, a provider or a ca to issue from")
        if provider is not None and not callable(provider):
            raise TypeError("provider must be callable")
        if on_rotate is not None and not callable(on_rotate):
            raise TypeError("on_rotate must be callable")

        self.ca = ca
        self.provider = provider
        self.common_name = str(common_name)
        self.dns_names = tuple(dns_names or ())
        self.ip_addresses = tuple(ip_addresses or ())
        self.rotating = bool(rotate)
        if valid_days is None:
            valid_days = DEFAULT_ROTATING_VALID_DAYS if self.rotating else DEFAULT_LEAF_VALID_DAYS
        self.valid_days = float(valid_days)
        self.renew_before_seconds = max(0.0, float(renew_before_seconds))
        self.on_rotate = on_rotate
        self.key_type = key_type
        self.alpn_protocols = tuple(alpn_protocols or ())
        self.client_ca_pem = bytes(client_ca_pem) if client_ca_pem else None
        self.require_client_cert = bool(require_client_cert)
        self.minimum_version = minimum_version
        self._now = time_source or _utcnow

        self._lock = threading.RLock()
        self._material = material
        self._context = None
        self._rotations = 0
        self._closed = False

    # -- state ---------------------------------------------------------

    def needs_rotation(self, material=None):
        candidate = material if material is not None else self._material
        if candidate is None:
            return True
        remaining = candidate.seconds_until_expiry(now=self._now())
        if remaining <= 0:
            return True
        if not self.rotating:
            return False
        return remaining <= self.renew_before_seconds

    @property
    def rotations(self):
        return self._rotations

    def describe(self):
        with self._lock:
            material = self._material
            return {
                "mode": "rotating" if self.rotating else "static",
                "rotations": self._rotations,
                "valid_days": self.valid_days,
                "renew_before_seconds": self.renew_before_seconds,
                "has_ca": self.ca is not None,
                "has_provider": self.provider is not None,
                "alpn_protocols": list(self.alpn_protocols),
                "requires_client_certificate": self.require_client_cert,
                "certificate": material.describe() if material else None,
            }

    # -- material ------------------------------------------------------

    def _coerce(self, value):
        if value is None:
            return None
        if isinstance(value, TLSMaterial):
            return value
        if isinstance(value, dict):
            return TLSMaterial(
                certificate_pem=value["certificate_pem"],
                private_key_pem=value["private_key_pem"],
                chain_pem=value.get("chain_pem", b""),
                key_password=value.get("key_password"),
            )
        if isinstance(value, (tuple, list)):
            if len(value) == 2:
                return TLSMaterial(value[0], value[1])
            if len(value) == 3:
                return TLSMaterial(value[0], value[1], value[2])
            raise ValueError("a material tuple must hold 2 or 3 PEM blobs")
        raise TypeError(f"cannot read TLS material from {type(value).__name__}")

    def _issue(self):
        if self.ca is None:
            raise RuntimeError(
                "no certificate authority available to issue new material; "
                "pass ca= or keep the provider returning valid material"
            )
        return self.ca.issue(
            self.common_name,
            dns_names=self.dns_names or (self.common_name,),
            ip_addresses=self.ip_addresses,
            valid_days=self.valid_days,
            key_type=self.key_type,
        )

    def _refresh_locked(self, force=False):
        """Return True when the held material was replaced."""
        if not force and not self.needs_rotation():
            return False

        if self.provider is not None:
            supplied = self._coerce(self.provider())
            # Material someone else already stored wins, as long as it lasts.
            if supplied is not None and not self.needs_rotation(supplied):
                self._material = supplied
                self._context = None
                return True

        self._material = self._issue()
        self._context = None
        self._rotations += 1
        if self.on_rotate is not None:
            self.on_rotate(self._material)
        return True

    def current(self):
        """The material in force, reissuing first when it is due."""
        with self._lock:
            if self._closed:
                raise RuntimeError("certificate manager is closed")
            self._refresh_locked()
            return self._material

    def rotate(self, force=True):
        """Reissue now. Returns True when the material changed."""
        with self._lock:
            if self._closed:
                raise RuntimeError("certificate manager is closed")
            return self._refresh_locked(force=force)

    # -- use -----------------------------------------------------------

    def ssl_context(self):
        """The context for a new connection, rebuilt only when material changes."""
        with self._lock:
            if self._closed:
                raise RuntimeError("certificate manager is closed")
            self._refresh_locked()
            if self._context is None:
                self._context = self._material.ssl_context(
                    client_ca_pem=self.client_ca_pem,
                    require_client_cert=self.require_client_cert,
                    alpn_protocols=self.alpn_protocols,
                    minimum_version=self.minimum_version,
                )
            return self._context

    def materialize(self):
        """Current material on disk, for tools that only take paths."""
        return self.current().materialize()

    def close(self):
        with self._lock:
            self._closed = True
            self._material = None
            self._context = None


def install_tls(
    app,
    *,
    manager=None,
    ca=None,
    common_name="localhost",
    dns_names=None,
    ip_addresses=("127.0.0.1",),
    rotate=False,
    attr_name="tls",
    **kwargs,
):
    """Attach a :class:`CertificateManager` to an app as ``app.tls``.

    With no ``manager`` and no ``ca`` a throwaway local authority is created,
    which is enough to serve HTTPS in development.
    """
    if manager is not None:
        ignored = {
            "ca": ca,
            "dns_names": dns_names,
            "rotate": rotate or None,
            "ip_addresses": None if ip_addresses == ("127.0.0.1",) else ip_addresses,
            **kwargs,
        }
        supplied = sorted(name for name, value in ignored.items() if value)
        if supplied or common_name != "localhost":
            raise TypeError(
                "install_tls takes either manager= or the arguments used to "
                f"build one, not both; drop {', '.join(supplied) or 'common_name'}"
            )
    if manager is None:
        if ca is None:
            ca = CertificateAuthority.create(f"{common_name} Local CA")
        manager = CertificateManager(
            ca=ca,
            common_name=common_name,
            dns_names=dns_names,
            ip_addresses=ip_addresses,
            rotate=rotate,
            **kwargs,
        )
    setattr(app, str(attr_name or "tls"), manager)
    return manager


__all__ = [
    "CertificateAuthority",
    "CertificateManager",
    "DEFAULT_CA_VALID_DAYS",
    "DEFAULT_LEAF_VALID_DAYS",
    "DEFAULT_RENEW_BEFORE_SECONDS",
    "DEFAULT_ROTATING_VALID_DAYS",
    "KEY_EC",
    "KEY_RSA",
    "MaterializedFiles",
    "SUPPORTED_KEY_TYPES",
    "TLSMaterial",
    "install_tls",
]

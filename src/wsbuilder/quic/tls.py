"""A TLS 1.3 server handshake, written out because ssl cannot drive QUIC.

QUIC does not carry TLS records. Handshake messages travel inside CRYPTO
frames and the transport derives its own packet protection keys from the TLS
key schedule, so an implementation needs to feed handshake bytes in and pull
traffic secrets out at each stage. CPython's ``ssl`` exposes neither, which
leaves writing the handshake on top of the primitives ``cryptography``
provides.

Scope is deliberately narrow, and narrow here is safer than general:

* one cipher suite, TLS_AES_128_GCM_SHA256
* one key exchange group, X25519
* server side only, no client certificates, no session resumption, no 0-RTT

Everything else in a ClientHello is parsed far enough to answer or refuse.
Refusing an unsupported parameter is the correct outcome; quietly picking
something weaker is not.
"""

import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa, x25519

from .crypto import hkdf_expand_label, hkdf_extract

# Handshake message types (RFC 8446 section 4).
CLIENT_HELLO = 1
SERVER_HELLO = 2
NEW_SESSION_TICKET = 4
ENCRYPTED_EXTENSIONS = 8
CERTIFICATE = 11
CERTIFICATE_VERIFY = 15
FINISHED = 20

# Extensions.
EXT_SERVER_NAME = 0x0000
EXT_SUPPORTED_GROUPS = 0x000A
EXT_SIGNATURE_ALGORITHMS = 0x000D
EXT_ALPN = 0x0010
EXT_SUPPORTED_VERSIONS = 0x002B
EXT_KEY_SHARE = 0x0033
#: RFC 9001 section 8.2 carries QUIC's transport parameters in the handshake.
EXT_QUIC_TRANSPORT_PARAMETERS = 0x0039

TLS_AES_128_GCM_SHA256 = 0x1301
GROUP_X25519 = 0x001D
TLS_1_3_VERSION = 0x0304
TLS_1_2_LEGACY = 0x0303

# Signature schemes we can produce, in the order we prefer them.
RSA_PSS_RSAE_SHA256 = 0x0804
ECDSA_SECP256R1_SHA256 = 0x0403
ED25519 = 0x0807

ALERT_HANDSHAKE_FAILURE = 40
ALERT_ILLEGAL_PARAMETER = 47
ALERT_PROTOCOL_VERSION = 70
ALERT_MISSING_EXTENSION = 109
ALERT_NO_APPLICATION_PROTOCOL = 120


class TLSError(Exception):
    """A handshake that cannot continue, carrying the alert to send."""

    def __init__(self, message, alert=ALERT_HANDSHAKE_FAILURE):
        super().__init__(message)
        self.alert = int(alert)


def _u8(value):
    return bytes([value & 0xFF])


def _u16(value):
    return int(value).to_bytes(2, "big")


def _u24(value):
    return int(value).to_bytes(3, "big")


def _block8(payload):
    return _u8(len(payload)) + bytes(payload)


def _block16(payload):
    return _u16(len(payload)) + bytes(payload)


def _block24(payload):
    return _u24(len(payload)) + bytes(payload)


class _Reader:
    """Bounds-checked cursor, so a malformed hello cannot walk off the end."""

    def __init__(self, data):
        self.data = bytes(data)
        self.offset = 0

    def remaining(self):
        return len(self.data) - self.offset

    def read(self, count):
        end = self.offset + int(count)
        if end > len(self.data):
            raise TLSError("handshake message truncated", ALERT_ILLEGAL_PARAMETER)
        chunk = self.data[self.offset : end]
        self.offset = end
        return chunk

    def u8(self):
        return self.read(1)[0]

    def u16(self):
        return int.from_bytes(self.read(2), "big")

    def u24(self):
        return int.from_bytes(self.read(3), "big")

    def block8(self):
        return self.read(self.u8())

    def block16(self):
        return self.read(self.u16())


class ClientHello:
    """The parts of a ClientHello a server needs to answer it."""

    __slots__ = (
        "random",
        "legacy_session_id",
        "cipher_suites",
        "server_name",
        "alpn",
        "key_shares",
        "supported_versions",
        "signature_algorithms",
        "transport_parameters",
        "raw",
    )

    def __init__(self, **fields):
        for name in self.__slots__:
            setattr(self, name, fields.get(name))

    def describe(self):
        return {
            "server_name": self.server_name,
            "alpn": list(self.alpn or ()),
            "cipher_suites": [f"0x{suite:04x}" for suite in self.cipher_suites or ()],
            "groups": [f"0x{group:04x}" for group, _ in (self.key_shares or ())],
        }


def parse_client_hello(body):
    """Parse a ClientHello body, without the handshake header."""
    reader = _Reader(body)
    version = reader.u16()
    if version != TLS_1_2_LEGACY:
        # TLS 1.3 freezes this field; the real version is in an extension.
        raise TLSError("legacy_version must be 0x0303", ALERT_PROTOCOL_VERSION)
    random = reader.read(32)
    session_id = reader.block8()

    suites_blob = reader.block16()
    cipher_suites = [
        int.from_bytes(suites_blob[i : i + 2], "big") for i in range(0, len(suites_blob), 2)
    ]

    compression = reader.block8()
    if compression != b"\x00":
        # Compression was removed in TLS 1.3 and its presence is an attack.
        raise TLSError("only null compression is allowed", ALERT_ILLEGAL_PARAMETER)

    hello = ClientHello(
        random=random,
        legacy_session_id=session_id,
        cipher_suites=cipher_suites,
        alpn=[],
        key_shares=[],
        supported_versions=[],
        signature_algorithms=[],
        raw=bytes(body),
    )
    if reader.remaining():
        _parse_extensions(reader.block16(), hello)
    return hello


def _parse_extensions(blob, hello):
    reader = _Reader(blob)
    while reader.remaining():
        kind = reader.u16()
        body = reader.block16()
        if kind == EXT_SERVER_NAME:
            hello.server_name = _parse_server_name(body)
        elif kind == EXT_ALPN:
            hello.alpn = _parse_alpn(body)
        elif kind == EXT_SUPPORTED_VERSIONS:
            inner = _Reader(body).block8()
            hello.supported_versions = [
                int.from_bytes(inner[i : i + 2], "big") for i in range(0, len(inner), 2)
            ]
        elif kind == EXT_KEY_SHARE:
            hello.key_shares = _parse_key_shares(body)
        elif kind == EXT_SIGNATURE_ALGORITHMS:
            inner = _Reader(body).block16()
            hello.signature_algorithms = [
                int.from_bytes(inner[i : i + 2], "big") for i in range(0, len(inner), 2)
            ]
        elif kind == EXT_QUIC_TRANSPORT_PARAMETERS:
            hello.transport_parameters = bytes(body)


def _parse_server_name(body):
    reader = _Reader(body)
    names = _Reader(reader.block16())
    while names.remaining():
        name_type = names.u8()
        name = names.block16()
        if name_type == 0:
            return name.decode("ascii", errors="replace")
    return None


def _parse_alpn(body):
    reader = _Reader(body)
    inner = _Reader(reader.block16())
    protocols = []
    while inner.remaining():
        protocols.append(inner.block8().decode("ascii", errors="replace"))
    return protocols


def _parse_key_shares(body):
    reader = _Reader(body)
    inner = _Reader(reader.block16())
    shares = []
    while inner.remaining():
        group = inner.u16()
        shares.append((group, inner.block16()))
    return shares


def build_handshake(message_type, body):
    return _u8(message_type) + _block24(body)


def build_server_hello(client_random_session_id, public_key):
    """A ServerHello answering with X25519 and TLS_AES_128_GCM_SHA256."""
    extensions = (
        _u16(EXT_SUPPORTED_VERSIONS) + _block16(_u16(TLS_1_3_VERSION))
        + _u16(EXT_KEY_SHARE) + _block16(_u16(GROUP_X25519) + _block16(public_key))
    )
    body = (
        _u16(TLS_1_2_LEGACY)
        + os.urandom(32)
        + _block8(client_random_session_id)
        + _u16(TLS_AES_128_GCM_SHA256)
        + _u8(0)  # null compression
        + _block16(extensions)
    )
    return build_handshake(SERVER_HELLO, body)


def build_encrypted_extensions(alpn=None, transport_parameters=None):
    extensions = b""
    if alpn:
        protocol = alpn.encode("ascii") if isinstance(alpn, str) else bytes(alpn)
        extensions += _u16(EXT_ALPN) + _block16(_block16(_block8(protocol)))
    if transport_parameters is not None:
        extensions += _u16(EXT_QUIC_TRANSPORT_PARAMETERS) + _block16(transport_parameters)
    return build_handshake(ENCRYPTED_EXTENSIONS, _block16(extensions))


def build_certificate(certificate_der_chain):
    entries = b""
    for der in certificate_der_chain:
        entries += _block24(der) + _block16(b"")  # no per-certificate extensions
    body = _block8(b"") + _block24(entries)  # empty certificate_request_context
    return build_handshake(CERTIFICATE, body)


def _signature_scheme_for(private_key, offered):
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        scheme = ED25519
    elif isinstance(private_key, ec.EllipticCurvePrivateKey):
        scheme = ECDSA_SECP256R1_SHA256
    elif isinstance(private_key, rsa.RSAPrivateKey):
        scheme = RSA_PSS_RSAE_SHA256
    else:
        raise TLSError("unsupported certificate key type")
    if offered and scheme not in offered:
        raise TLSError(
            f"client does not accept signature scheme 0x{scheme:04x}", ALERT_HANDSHAKE_FAILURE
        )
    return scheme


def build_certificate_verify(private_key, transcript_hash, offered_schemes=()):
    """Sign the transcript, binding the certificate to this handshake."""
    scheme = _signature_scheme_for(private_key, offered_schemes)
    # RFC 8446 section 4.4.3: 64 spaces, a context string, a zero, the hash.
    content = b" " * 64 + b"TLS 1.3, server CertificateVerify" + b"\x00" + transcript_hash
    if scheme == ED25519:
        signature = private_key.sign(content)
    elif scheme == ECDSA_SECP256R1_SHA256:
        signature = private_key.sign(content, ec.ECDSA(hashes.SHA256()))
    else:
        signature = private_key.sign(
            content,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
            hashes.SHA256(),
        )
    return build_handshake(CERTIFICATE_VERIFY, _u16(scheme) + _block16(signature))


def build_finished(traffic_secret, transcript_hash):
    key = hkdf_expand_label(traffic_secret, "finished", b"", 32)
    from cryptography.hazmat.primitives.hmac import HMAC

    mac = HMAC(key, hashes.SHA256())
    mac.update(transcript_hash)
    return build_handshake(FINISHED, mac.finalize())


class Transcript:
    """Running hash of every handshake message, in order."""

    def __init__(self):
        self._messages = bytearray()

    def add(self, message):
        self._messages.extend(bytes(message))

    def hash(self):
        digest = hashes.Hash(hashes.SHA256())
        digest.update(bytes(self._messages))
        return digest.finalize()

    def __len__(self):
        return len(self._messages)


class KeySchedule:
    """The TLS 1.3 secret derivation, one stage at a time (RFC 8446 7.1)."""

    EMPTY_HASH = None

    def __init__(self):
        digest = hashes.Hash(hashes.SHA256())
        self.empty_hash = digest.finalize()
        self.early_secret = hkdf_extract(b"\x00" * 32, b"\x00" * 32)
        self.handshake_secret = None
        self.master_secret = None
        self.client_handshake_secret = None
        self.server_handshake_secret = None
        self.client_application_secret = None
        self.server_application_secret = None

    def enter_handshake(self, shared_secret, transcript_hash):
        derived = hkdf_expand_label(self.early_secret, "derived", self.empty_hash, 32)
        self.handshake_secret = hkdf_extract(derived, shared_secret)
        self.client_handshake_secret = hkdf_expand_label(
            self.handshake_secret, "c hs traffic", transcript_hash, 32
        )
        self.server_handshake_secret = hkdf_expand_label(
            self.handshake_secret, "s hs traffic", transcript_hash, 32
        )
        return self.client_handshake_secret, self.server_handshake_secret

    def enter_application(self, transcript_hash):
        derived = hkdf_expand_label(self.handshake_secret, "derived", self.empty_hash, 32)
        self.master_secret = hkdf_extract(derived, b"\x00" * 32)
        self.client_application_secret = hkdf_expand_label(
            self.master_secret, "c ap traffic", transcript_hash, 32
        )
        self.server_application_secret = hkdf_expand_label(
            self.master_secret, "s ap traffic", transcript_hash, 32
        )
        return self.client_application_secret, self.server_application_secret


class ServerHandshake:
    """Drives the server half of a TLS 1.3 handshake for QUIC."""

    def __init__(self, certificate_chain_der, private_key, *, alpn_protocols=("h3",),
                 transport_parameters=b""):
        self.certificate_chain = [bytes(der) for der in certificate_chain_der]
        self.private_key = private_key
        self.alpn_protocols = tuple(alpn_protocols)
        self.transport_parameters = bytes(transport_parameters)
        self.transcript = Transcript()
        self.schedule = KeySchedule()
        self.selected_alpn = None
        self.client_hello = None
        self.peer_transport_parameters = None
        self._private = x25519.X25519PrivateKey.generate()

    def _choose_alpn(self, offered):
        if not offered:
            return None
        for protocol in self.alpn_protocols:
            if protocol in offered:
                return protocol
        raise TLSError("no overlapping ALPN protocol", ALERT_NO_APPLICATION_PROTOCOL)

    def handle_client_hello(self, message):
        """Consume a ClientHello and produce the server's whole flight.

        Returns ``(handshake_bytes, secrets)`` where secrets holds the
        handshake and application traffic secrets QUIC needs for its own
        packet protection.
        """
        reader = _Reader(message)
        if reader.u8() != CLIENT_HELLO:
            raise TLSError("expected a ClientHello", ALERT_ILLEGAL_PARAMETER)
        body = reader.read(reader.u24())
        hello = parse_client_hello(body)
        self.client_hello = hello

        if TLS_1_3_VERSION not in (hello.supported_versions or ()):
            raise TLSError("client does not offer TLS 1.3", ALERT_PROTOCOL_VERSION)
        if TLS_AES_128_GCM_SHA256 not in (hello.cipher_suites or ()):
            raise TLSError("no supported cipher suite", ALERT_HANDSHAKE_FAILURE)
        peer_share = next(
            (share for group, share in (hello.key_shares or ()) if group == GROUP_X25519), None
        )
        if peer_share is None:
            # A HelloRetryRequest would ask for one; refusing is correct too.
            raise TLSError("no X25519 key share offered", ALERT_MISSING_EXTENSION)

        self.selected_alpn = self._choose_alpn(hello.alpn)
        self.peer_transport_parameters = hello.transport_parameters

        self.transcript.add(message)
        public_bytes = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        server_hello = build_server_hello(hello.legacy_session_id, public_bytes)
        self.transcript.add(server_hello)

        shared = self._private.exchange(x25519.X25519PublicKey.from_public_bytes(peer_share))
        client_hs, server_hs = self.schedule.enter_handshake(shared, self.transcript.hash())

        flight = bytearray()
        encrypted_extensions = build_encrypted_extensions(
            self.selected_alpn, self.transport_parameters
        )
        flight += encrypted_extensions
        self.transcript.add(encrypted_extensions)

        certificate = build_certificate(self.certificate_chain)
        flight += certificate
        self.transcript.add(certificate)

        verify = build_certificate_verify(
            self.private_key, self.transcript.hash(), hello.signature_algorithms or ()
        )
        flight += verify
        self.transcript.add(verify)

        finished = build_finished(server_hs, self.transcript.hash())
        flight += finished
        self.transcript.add(finished)

        client_app, server_app = self.schedule.enter_application(self.transcript.hash())
        return (
            server_hello,
            bytes(flight),
            {
                "client_handshake": client_hs,
                "server_handshake": server_hs,
                "client_application": client_app,
                "server_application": server_app,
            },
        )

    def verify_client_finished(self, message):
        """Check the client's Finished against our own transcript."""
        reader = _Reader(message)
        if reader.u8() != FINISHED:
            raise TLSError("expected a Finished", ALERT_ILLEGAL_PARAMETER)
        received = reader.read(reader.u24())
        expected_message = build_finished(
            self.schedule.client_handshake_secret, self.transcript.hash()
        )
        expected = expected_message[4:]
        import hmac as _hmac

        if not _hmac.compare_digest(received, expected):
            raise TLSError("client Finished does not verify", ALERT_HANDSHAKE_FAILURE)
        self.transcript.add(message)
        return True


__all__ = [
    "ALERT_HANDSHAKE_FAILURE",
    "ALERT_NO_APPLICATION_PROTOCOL",
    "CLIENT_HELLO",
    "ClientHello",
    "EXT_QUIC_TRANSPORT_PARAMETERS",
    "GROUP_X25519",
    "KeySchedule",
    "ServerHandshake",
    "TLSError",
    "TLS_AES_128_GCM_SHA256",
    "TLS_1_3_VERSION",
    "EXT_ALPN",
    "EXT_KEY_SHARE",
    "EXT_SUPPORTED_VERSIONS",
    "FINISHED",
    "SERVER_HELLO",
    "Transcript",
    "build_certificate",
    "build_certificate_verify",
    "build_encrypted_extensions",
    "build_finished",
    "build_handshake",
    "build_server_hello",
    "parse_client_hello",
]

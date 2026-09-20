"""A TLS 1.3 server handshake, written out because ssl cannot drive QUIC.

QUIC does not carry TLS records. Handshake messages travel inside CRYPTO
frames and the transport derives its own packet protection keys from the TLS
key schedule, so an implementation needs to feed handshake bytes in and pull
traffic secrets out at each stage. CPython's ``ssl`` exposes neither, which
leaves writing the handshake on top of the primitives ``cryptography``
provides.

Scope stays deliberately small, because narrow is safer than general here:

* three cipher suites, all AEAD and all TLS 1.3 only
* two key exchange groups, X25519 and secp256r1
* server side only, no client certificates, no session resumption, no 0-RTT

Everything else in a ClientHello is parsed far enough to answer or refuse.
Refusing an unsupported parameter is the correct outcome; quietly picking
something weaker is not, which is why there is no fallback path at all.
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
TLS_AES_256_GCM_SHA384 = 0x1302
TLS_CHACHA20_POLY1305_SHA256 = 0x1303

#: In preference order. All three are AEAD and exist only in TLS 1.3.
SUPPORTED_CIPHER_SUITES = (
    TLS_AES_128_GCM_SHA256,
    TLS_CHACHA20_POLY1305_SHA256,
    TLS_AES_256_GCM_SHA384,
)

#: What each suite means to the QUIC packet protection and the key schedule.
CIPHER_SUITE_PARAMETERS = {
    TLS_AES_128_GCM_SHA256: ("aes-128-gcm", "sha256", 32),
    TLS_AES_256_GCM_SHA384: ("aes-256-gcm", "sha384", 48),
    TLS_CHACHA20_POLY1305_SHA256: ("chacha20-poly1305", "sha256", 32),
}

GROUP_X25519 = 0x001D
GROUP_SECP256R1 = 0x0017
SUPPORTED_GROUPS = (GROUP_X25519, GROUP_SECP256R1)
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


def generate_key_share(group):
    """A fresh ephemeral key pair for the negotiated group."""
    if group == GROUP_X25519:
        private = x25519.X25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return private, public
    if group == GROUP_SECP256R1:
        private = ec.generate_private_key(ec.SECP256R1())
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        return private, public
    raise TLSError(f"unsupported group 0x{group:04x}", ALERT_ILLEGAL_PARAMETER)


def derive_shared_secret(group, private_key, peer_share):
    if group == GROUP_X25519:
        return private_key.exchange(x25519.X25519PublicKey.from_public_bytes(peer_share))
    if group == GROUP_SECP256R1:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes(peer_share))
        return private_key.exchange(ec.ECDH(), peer)
    raise TLSError(f"unsupported group 0x{group:04x}", ALERT_ILLEGAL_PARAMETER)


def build_handshake(message_type, body):
    return _u8(message_type) + _block24(body)


def build_server_hello(client_random_session_id, public_key, *,
                       cipher_suite=TLS_AES_128_GCM_SHA256, group=GROUP_X25519):
    """A ServerHello naming the suite and group the server picked."""
    extensions = (
        _u16(EXT_SUPPORTED_VERSIONS) + _block16(_u16(TLS_1_3_VERSION))
        + _u16(EXT_KEY_SHARE) + _block16(_u16(group) + _block16(public_key))
    )
    body = (
        _u16(TLS_1_2_LEGACY)
        + os.urandom(32)
        + _block8(client_random_session_id)
        + _u16(cipher_suite)
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


def hash_for(cipher_suite):
    """The hash the suite's key schedule and transcript use."""
    _aead, digest, _length = CIPHER_SUITE_PARAMETERS[cipher_suite]
    return hashes.SHA384() if digest == "sha384" else hashes.SHA256()


def secret_length(cipher_suite):
    return CIPHER_SUITE_PARAMETERS[cipher_suite][2]


def build_finished(traffic_secret, transcript_hash, cipher_suite=TLS_AES_128_GCM_SHA256):
    algorithm = hash_for(cipher_suite)
    key = hkdf_expand_label(
        traffic_secret, "finished", b"", secret_length(cipher_suite), algorithm
    )
    from cryptography.hazmat.primitives.hmac import HMAC

    mac = HMAC(key, algorithm)
    mac.update(transcript_hash)
    return build_handshake(FINISHED, mac.finalize())


class Transcript:
    """Running hash of every handshake message, in order."""

    def __init__(self, algorithm=None):
        self._messages = bytearray()
        self.algorithm = algorithm or hashes.SHA256()

    def add(self, message):
        self._messages.extend(bytes(message))

    def hash(self):
        digest = hashes.Hash(self.algorithm)
        digest.update(bytes(self._messages))
        return digest.finalize()

    def __len__(self):
        return len(self._messages)


class KeySchedule:
    """The TLS 1.3 secret derivation, one stage at a time (RFC 8446 7.1)."""

    EMPTY_HASH = None

    def __init__(self, algorithm=None, length=32):
        self.algorithm = algorithm or hashes.SHA256()
        self.length = int(length)
        digest = hashes.Hash(self.algorithm)
        self.empty_hash = digest.finalize()
        zeros = b"\x00" * self.length
        self.early_secret = hkdf_extract(zeros, zeros, self.algorithm)
        self.handshake_secret = None
        self.master_secret = None
        self.client_handshake_secret = None
        self.server_handshake_secret = None
        self.client_application_secret = None
        self.server_application_secret = None

    def _expand(self, secret, label, context):
        return hkdf_expand_label(secret, label, context, self.length, self.algorithm)

    def enter_handshake(self, shared_secret, transcript_hash):
        derived = self._expand(self.early_secret, "derived", self.empty_hash)
        self.handshake_secret = hkdf_extract(derived, shared_secret, self.algorithm)
        self.client_handshake_secret = self._expand(
            self.handshake_secret, "c hs traffic", transcript_hash
        )
        self.server_handshake_secret = self._expand(
            self.handshake_secret, "s hs traffic", transcript_hash
        )
        return self.client_handshake_secret, self.server_handshake_secret

    def enter_application(self, transcript_hash):
        derived = self._expand(self.handshake_secret, "derived", self.empty_hash)
        self.master_secret = hkdf_extract(derived, b"\x00" * self.length, self.algorithm)
        self.client_application_secret = self._expand(
            self.master_secret, "c ap traffic", transcript_hash
        )
        self.server_application_secret = self._expand(
            self.master_secret, "s ap traffic", transcript_hash
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
        self.cipher_suite = None
        self.group = None
        self.client_hello = None
        self.peer_transport_parameters = None
        self._private = None

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
        offered = hello.cipher_suites or ()
        self.cipher_suite = next(
            (suite for suite in SUPPORTED_CIPHER_SUITES if suite in offered), None
        )
        if self.cipher_suite is None:
            raise TLSError("no supported cipher suite", ALERT_HANDSHAKE_FAILURE)

        shares = dict(hello.key_shares or ())
        self.group = next((g for g in SUPPORTED_GROUPS if g in shares), None)
        if self.group is None:
            # A HelloRetryRequest would ask for a group we do accept;
            # refusing outright is also a correct answer, and simpler.
            raise TLSError("no supported key share offered", ALERT_MISSING_EXTENSION)
        peer_share = shares[self.group]

        algorithm = hash_for(self.cipher_suite)
        length = secret_length(self.cipher_suite)
        self.transcript = Transcript(algorithm)
        self.schedule = KeySchedule(algorithm, length)

        self.selected_alpn = self._choose_alpn(hello.alpn)
        self.peer_transport_parameters = hello.transport_parameters

        self.transcript.add(message)
        self._private, public_bytes = generate_key_share(self.group)
        server_hello = build_server_hello(
            hello.legacy_session_id,
            public_bytes,
            cipher_suite=self.cipher_suite,
            group=self.group,
        )
        self.transcript.add(server_hello)
        shared = derive_shared_secret(self.group, self._private, peer_share)
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

        finished = build_finished(server_hs, self.transcript.hash(), self.cipher_suite)
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
            self.schedule.client_handshake_secret, self.transcript.hash(), self.cipher_suite
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
    "TLS_AES_256_GCM_SHA384",
    "TLS_CHACHA20_POLY1305_SHA256",
    "SUPPORTED_CIPHER_SUITES",
    "SUPPORTED_GROUPS",
    "GROUP_SECP256R1",
    "CIPHER_SUITE_PARAMETERS",
    "derive_shared_secret",
    "generate_key_share",
    "hash_for",
    "secret_length",
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

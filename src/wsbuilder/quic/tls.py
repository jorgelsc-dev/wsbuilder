"""A TLS 1.3 handshake for QUIC, written out because ssl cannot drive QUIC.

QUIC does not carry TLS records. Handshake messages travel inside CRYPTO
frames and the transport derives its own packet protection keys from the TLS
key schedule, so an implementation needs to feed handshake bytes in and pull
traffic secrets out at each stage. CPython's ``ssl`` exposes neither, which
leaves writing the handshake on top of the primitives ``cryptography``
provides.

Scope stays deliberately small, because narrow is safer than general here:

* three cipher suites, all AEAD and all TLS 1.3 only
* two key exchange groups, X25519 and secp256r1
* both halves: the server, and the client used for service-to-service
  calls, with optional client certificates (mutual TLS) in both
* no session resumption, no 0-RTT, no HelloRetryRequest

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
CERTIFICATE_REQUEST = 13
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
RSA_PSS_RSAE_SHA384 = 0x0805
RSA_PSS_RSAE_SHA512 = 0x0806
ECDSA_SECP256R1_SHA256 = 0x0403
ECDSA_SECP384R1_SHA384 = 0x0503
ED25519 = 0x0807

#: What a peer's CertificateVerify may use, as offered in our hellos.
VERIFIABLE_SIGNATURE_SCHEMES = (
    ECDSA_SECP256R1_SHA256,
    ECDSA_SECP384R1_SHA384,
    ED25519,
    RSA_PSS_RSAE_SHA256,
    RSA_PSS_RSAE_SHA384,
    RSA_PSS_RSAE_SHA512,
)

#: RFC 8446 section 4.1.3: a ServerHello with this random is a HelloRetryRequest.
HELLO_RETRY_REQUEST_RANDOM = bytes.fromhex(
    "cf21ad74e59a6111be1d8c021e65b891c2a211167abb8c5e079e09e2c8a8339c"
)

ALERT_HANDSHAKE_FAILURE = 40
ALERT_BAD_CERTIFICATE = 42
ALERT_ILLEGAL_PARAMETER = 47
ALERT_DECODE_ERROR = 50
ALERT_DECRYPT_ERROR = 51
ALERT_PROTOCOL_VERSION = 70
ALERT_MISSING_EXTENSION = 109
ALERT_CERTIFICATE_REQUIRED = 116
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

    def block24(self):
        return self.read(self.u24())

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


def build_certificate(certificate_der_chain, request_context=b""):
    entries = b""
    for der in certificate_der_chain:
        entries += _block24(der) + _block16(b"")  # no per-certificate extensions
    body = _block8(bytes(request_context)) + _block24(entries)
    return build_handshake(CERTIFICATE, body)


def parse_certificate(body):
    """Return ``(request_context, [der, ...])`` from a Certificate body."""
    reader = _Reader(body)
    context = reader.block8()
    entries = _Reader(reader.block24())
    if reader.remaining():
        raise TLSError("trailing bytes in Certificate", ALERT_DECODE_ERROR)
    chain = []
    while entries.remaining():
        der = entries.block24()
        entries.block16()  # per-certificate extensions: none are used
        if not der:
            raise TLSError("empty certificate entry", ALERT_DECODE_ERROR)
        chain.append(der)
    return context, chain


def build_certificate_request(request_context=b""):
    """Ask the client for a certificate (RFC 8446 section 4.3.2)."""
    schemes = b"".join(_u16(scheme) for scheme in VERIFIABLE_SIGNATURE_SCHEMES)
    extensions = _u16(EXT_SIGNATURE_ALGORITHMS) + _block16(_block16(schemes))
    return build_handshake(
        CERTIFICATE_REQUEST, _block8(bytes(request_context)) + _block16(extensions)
    )


def parse_certificate_request(body):
    """Return ``(request_context, offered_signature_schemes)``."""
    reader = _Reader(body)
    context = reader.block8()
    extensions = _Reader(reader.block16())
    schemes = []
    while extensions.remaining():
        kind = extensions.u16()
        data = extensions.block16()
        if kind == EXT_SIGNATURE_ALGORITHMS:
            inner = _Reader(data).block16()
            schemes = [int.from_bytes(inner[i : i + 2], "big") for i in range(0, len(inner), 2)]
    if not schemes:
        # The extension is mandatory in a CertificateRequest.
        raise TLSError("CertificateRequest without signature_algorithms", ALERT_MISSING_EXTENSION)
    return context, schemes


def _split_messages(data, offset):
    """Whole handshake messages in ``data`` from ``offset``, plus the new offset."""
    messages = []
    while len(data) - offset >= 4:
        length = int.from_bytes(data[offset + 1 : offset + 4], "big")
        end = offset + 4 + length
        if end > len(data):
            break
        messages.append((data[offset], data[offset:end], data[offset + 4 : end]))
        offset = end
    return messages, offset


class CertificateTrust:
    """The certificate authorities a peer's chain must lead to.

    Path building and checks (validity, key usage, names) are done by
    ``cryptography``'s WebPKI verifier rather than by hand.
    """

    def __init__(self, ca_certificates_der):
        from cryptography import x509

        roots = []
        for der in ca_certificates_der:
            roots.append(x509.load_der_x509_certificate(bytes(der)))
        if not roots:
            raise ValueError("a trust store needs at least one CA certificate")
        self.roots = roots

    @classmethod
    def from_pem(cls, pem):
        """Trust every certificate in a PEM bundle (bytes or str)."""
        from cryptography import x509

        data = pem.encode("ascii") if isinstance(pem, str) else bytes(pem)
        certificates = x509.load_pem_x509_certificates(data)
        return cls(
            [cert.public_bytes(serialization.Encoding.DER) for cert in certificates]
        )

    @classmethod
    def system(cls):
        """The CA certificates the platform's OpenSSL trusts by default."""
        import ssl

        context = ssl.create_default_context()
        return cls(context.get_ca_certs(binary_form=True))

    def _load_chain(self, chain_der):
        from cryptography import x509

        if not chain_der:
            raise TLSError("the peer sent no certificate", ALERT_BAD_CERTIFICATE)
        try:
            certificates = [x509.load_der_x509_certificate(bytes(der)) for der in chain_der]
        except ValueError as exc:
            raise TLSError(f"unparseable certificate: {exc}", ALERT_BAD_CERTIFICATE) from None
        return certificates[0], certificates[1:]

    def _builder(self):
        from cryptography.x509.verification import PolicyBuilder, Store

        return PolicyBuilder().store(Store(self.roots))

    def verify_server(self, chain_der, server_name):
        """Check a server chain for ``server_name``; return the leaf certificate."""
        import ipaddress

        from cryptography import x509
        from cryptography.x509.verification import VerificationError

        leaf, intermediates = self._load_chain(chain_der)
        try:
            subject = x509.IPAddress(ipaddress.ip_address(server_name))
        except ValueError:
            subject = x509.DNSName(str(server_name))
        try:
            self._builder().build_server_verifier(subject).verify(leaf, intermediates)
        except VerificationError as exc:
            raise TLSError(f"server certificate rejected: {exc}", ALERT_BAD_CERTIFICATE) from None
        return leaf

    def verify_client(self, chain_der):
        """Check a client chain; return the leaf certificate."""
        from cryptography.x509.verification import VerificationError

        leaf, intermediates = self._load_chain(chain_der)
        builder = self._builder()
        if not hasattr(builder, "build_client_verifier"):
            raise TLSError(
                "client certificates need cryptography>=43", ALERT_HANDSHAKE_FAILURE
            )
        try:
            builder.build_client_verifier().verify(leaf, intermediates)
        except VerificationError as exc:
            raise TLSError(f"client certificate rejected: {exc}", ALERT_BAD_CERTIFICATE) from None
        return leaf


def describe_certificate(certificate):
    """The parts of a verified peer certificate an application acts on."""
    from cryptography import x509

    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns_names = san.get_values_for_type(x509.DNSName)
        ip_addresses = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    except x509.ExtensionNotFound:
        dns_names, ip_addresses, uris = [], [], []
    common_names = [
        attribute.value
        for attribute in certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    ]
    return {
        "subject": certificate.subject.rfc4514_string(),
        "issuer": certificate.issuer.rfc4514_string(),
        "common_name": common_names[0] if common_names else None,
        "dns_names": list(dns_names),
        "ip_addresses": ip_addresses,
        "uris": list(uris),
        "serial_number": certificate.serial_number,
        "not_valid_after": certificate.not_valid_after_utc.isoformat(),
        "fingerprint_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
    }


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


_VERIFY_CONTEXTS = {
    "server": b"TLS 1.3, server CertificateVerify",
    "client": b"TLS 1.3, client CertificateVerify",
}


def _certificate_verify_content(transcript_hash, side):
    # RFC 8446 section 4.4.3: 64 spaces, a context string, a zero, the hash.
    return b" " * 64 + _VERIFY_CONTEXTS[side] + b"\x00" + transcript_hash


def build_certificate_verify(private_key, transcript_hash, offered_schemes=(), side="server"):
    """Sign the transcript, binding the certificate to this handshake."""
    scheme = _signature_scheme_for(private_key, offered_schemes)
    content = _certificate_verify_content(transcript_hash, side)
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


_SCHEME_HASHES = {
    ECDSA_SECP256R1_SHA256: hashes.SHA256,
    ECDSA_SECP384R1_SHA384: hashes.SHA384,
    RSA_PSS_RSAE_SHA256: hashes.SHA256,
    RSA_PSS_RSAE_SHA384: hashes.SHA384,
    RSA_PSS_RSAE_SHA512: hashes.SHA512,
}


def verify_certificate_verify(public_key, body, transcript_hash, side="server"):
    """Check a peer's CertificateVerify body against the transcript.

    Raises TLSError when the scheme does not fit the key or the signature
    does not verify: either means the peer does not hold the private key.
    """
    from cryptography.exceptions import InvalidSignature

    reader = _Reader(body)
    scheme = reader.u16()
    signature = reader.block16()
    if reader.remaining():
        raise TLSError("trailing bytes in CertificateVerify", ALERT_DECODE_ERROR)
    content = _certificate_verify_content(transcript_hash, side)
    try:
        if scheme == ED25519:
            if not isinstance(public_key, ed25519.Ed25519PublicKey):
                raise TLSError("signature scheme does not match the key", ALERT_ILLEGAL_PARAMETER)
            public_key.verify(signature, content)
        elif scheme in (ECDSA_SECP256R1_SHA256, ECDSA_SECP384R1_SHA384):
            expected_curve = ec.SECP256R1 if scheme == ECDSA_SECP256R1_SHA256 else ec.SECP384R1
            if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
                public_key.curve, expected_curve
            ):
                raise TLSError("signature scheme does not match the key", ALERT_ILLEGAL_PARAMETER)
            public_key.verify(signature, content, ec.ECDSA(_SCHEME_HASHES[scheme]()))
        elif scheme in (RSA_PSS_RSAE_SHA256, RSA_PSS_RSAE_SHA384, RSA_PSS_RSAE_SHA512):
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise TLSError("signature scheme does not match the key", ALERT_ILLEGAL_PARAMETER)
            algorithm = _SCHEME_HASHES[scheme]()
            public_key.verify(
                signature,
                content,
                padding.PSS(mgf=padding.MGF1(algorithm), salt_length=algorithm.digest_size),
                algorithm,
            )
        else:
            raise TLSError(f"unsupported signature scheme 0x{scheme:04x}", ALERT_ILLEGAL_PARAMETER)
    except InvalidSignature:
        raise TLSError("CertificateVerify does not verify", ALERT_DECRYPT_ERROR) from None
    return scheme


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
                 transport_parameters=b"", client_trust=None,
                 require_client_certificate=False):
        #: When set, the server asks for a client certificate and checks it
        #: against these authorities (mutual TLS).
        self.client_trust = client_trust
        self.require_client_certificate = bool(require_client_certificate)
        if self.require_client_certificate and client_trust is None:
            raise ValueError("requiring a client certificate needs a client_trust store")
        self.peer_certificate = None
        self._client_offset = 0
        self._client_chain = None
        self._client_verified = False
        self._client_finished = False
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

        if self.client_trust is not None:
            request = build_certificate_request()
            flight += request
            self.transcript.add(request)

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

    def handle_client_flight(self, data):
        """Consume the client's handshake-level bytes as they accumulate.

        ``data`` is everything received at that level so far. Returns True
        once the client's Finished verifies, None while more is needed, and
        raises TLSError for anything wrong. With mutual TLS the flight is
        Certificate, CertificateVerify (when a certificate was sent) and
        Finished; otherwise just Finished.
        """
        import hmac as _hmac

        if self._client_finished:
            return True
        messages, self._client_offset = _split_messages(bytes(data), self._client_offset)
        for message_type, message, body in messages:
            if self.client_trust is not None and self._client_chain is None:
                if message_type != CERTIFICATE:
                    raise TLSError("expected the client Certificate", ALERT_ILLEGAL_PARAMETER)
                context, chain = parse_certificate(body)
                if context:
                    raise TLSError("unexpected certificate_request_context", ALERT_ILLEGAL_PARAMETER)
                if not chain and self.require_client_certificate:
                    raise TLSError("a client certificate is required", ALERT_CERTIFICATE_REQUIRED)
                if chain:
                    leaf = self.client_trust.verify_client(chain)
                    self.peer_certificate = leaf
                self._client_chain = chain
                self.transcript.add(message)
                continue
            if message_type == CERTIFICATE_VERIFY:
                if not self._client_chain or self._client_verified:
                    raise TLSError("unexpected CertificateVerify", ALERT_ILLEGAL_PARAMETER)
                verify_certificate_verify(
                    self.peer_certificate.public_key(), body, self.transcript.hash(), side="client"
                )
                self._client_verified = True
                self.transcript.add(message)
                continue
            if message_type != FINISHED:
                raise TLSError("expected a Finished", ALERT_ILLEGAL_PARAMETER)
            if self._client_chain and not self._client_verified:
                raise TLSError("client certificate without CertificateVerify", ALERT_ILLEGAL_PARAMETER)
            expected = build_finished(
                self.schedule.client_handshake_secret, self.transcript.hash(), self.cipher_suite
            )[4:]
            if not _hmac.compare_digest(body, expected):
                raise TLSError("client Finished does not verify", ALERT_DECRYPT_ERROR)
            self.transcript.add(message)
            self._client_finished = True
            return True
        return None

    def verify_client_finished(self, message):
        """Check the client's Finished against our own transcript."""
        result = self.handle_client_flight(message)
        if result is None:
            raise TLSError("incomplete client Finished", ALERT_DECODE_ERROR)
        return result


class ClientHandshake:
    """Drives the client half of a TLS 1.3 handshake for QUIC.

    ``trust`` checks the server's chain for ``server_name``; it is not
    optional, because a client that accepts any certificate gets the
    encryption without knowing who it is talking to. ``certificate_chain_der``
    and ``private_key`` answer a server that asks for a client certificate.
    """

    def __init__(self, server_name, trust, *, alpn_protocols=("h3",), transport_parameters=b"",
                 certificate_chain_der=None, private_key=None):
        if trust is None:
            raise ValueError("a client handshake needs a trust store to check the server")
        if bool(certificate_chain_der) != (private_key is not None):
            raise ValueError("a client certificate needs both its chain and its private key")
        self.server_name = str(server_name or "")
        self.trust = trust
        self.alpn_protocols = tuple(alpn_protocols)
        self.transport_parameters = bytes(transport_parameters)
        self.certificate_chain = [bytes(der) for der in certificate_chain_der or ()]
        self.private_key = private_key
        self.cipher_suite = TLS_AES_128_GCM_SHA256
        self.transcript = Transcript(hash_for(self.cipher_suite))
        self.schedule = KeySchedule(hash_for(self.cipher_suite), secret_length(self.cipher_suite))
        self.selected_alpn = None
        self.peer_transport_parameters = None
        self.peer_certificate = None
        self.group = None
        self._shares = {}
        self._offset = 0
        self._state = "start"
        self._certificate_request = None

    def client_hello(self):
        """The ClientHello message, ready for a CRYPTO frame."""
        if self._state != "start":
            raise TLSError("the ClientHello was already built")
        shares = b""
        # One share is enough: every server we talk to accepts X25519, and a
        # second share would only cost bytes in the first datagram.
        private, public = generate_key_share(GROUP_X25519)
        self._shares[GROUP_X25519] = private
        shares += _u16(GROUP_X25519) + _block16(public)

        extensions = b""
        name = self.server_name
        if name and not _is_ip_literal(name):
            entry = _u8(0) + _block16(name.encode("idna"))
            extensions += _u16(EXT_SERVER_NAME) + _block16(_block16(entry))
        groups = b"".join(_u16(group) for group in SUPPORTED_GROUPS)
        extensions += _u16(EXT_SUPPORTED_GROUPS) + _block16(_block16(groups))
        schemes = b"".join(_u16(scheme) for scheme in VERIFIABLE_SIGNATURE_SCHEMES)
        extensions += _u16(EXT_SIGNATURE_ALGORITHMS) + _block16(_block16(schemes))
        if self.alpn_protocols:
            protocols = b"".join(_block8(p.encode("ascii")) for p in self.alpn_protocols)
            extensions += _u16(EXT_ALPN) + _block16(_block16(protocols))
        extensions += _u16(EXT_SUPPORTED_VERSIONS) + _block16(_block8(_u16(TLS_1_3_VERSION)))
        extensions += _u16(EXT_KEY_SHARE) + _block16(_block16(shares))
        extensions += _u16(EXT_QUIC_TRANSPORT_PARAMETERS) + _block16(self.transport_parameters)

        body = (
            _u16(TLS_1_2_LEGACY)
            + os.urandom(32)
            + _block8(b"")  # RFC 9001 section 8.4: no legacy session id in QUIC
            + _block16(_u16(self.cipher_suite))
            + _block8(b"\x00")
            + _block16(extensions)
        )
        message = build_handshake(CLIENT_HELLO, body)
        self.transcript.add(message)
        self._state = "wait_server_hello"
        return message

    def handle_server_hello(self, message):
        """Consume the ServerHello; return the handshake traffic secrets."""
        if self._state != "wait_server_hello":
            raise TLSError("unexpected ServerHello", ALERT_ILLEGAL_PARAMETER)
        reader = _Reader(message)
        if reader.u8() != SERVER_HELLO:
            raise TLSError("expected a ServerHello", ALERT_ILLEGAL_PARAMETER)
        body = _Reader(reader.read(reader.u24()))
        if body.u16() != TLS_1_2_LEGACY:
            raise TLSError("legacy_version must be 0x0303", ALERT_PROTOCOL_VERSION)
        random = body.read(32)
        if random == HELLO_RETRY_REQUEST_RANDOM:
            raise TLSError("HelloRetryRequest is not supported", ALERT_HANDSHAKE_FAILURE)
        if body.block8():
            raise TLSError("the server echoed a session id we never sent", ALERT_ILLEGAL_PARAMETER)
        suite = body.u16()
        if suite != self.cipher_suite:
            raise TLSError("the server chose a suite we did not offer", ALERT_ILLEGAL_PARAMETER)
        if body.u8() != 0:
            raise TLSError("only null compression is allowed", ALERT_ILLEGAL_PARAMETER)
        version = None
        share = None
        extensions = _Reader(body.block16())
        while extensions.remaining():
            kind = extensions.u16()
            data = extensions.block16()
            if kind == EXT_SUPPORTED_VERSIONS:
                version = int.from_bytes(data, "big")
            elif kind == EXT_KEY_SHARE:
                inner = _Reader(data)
                share = (inner.u16(), inner.block16())
        if version != TLS_1_3_VERSION:
            raise TLSError("the server did not negotiate TLS 1.3", ALERT_PROTOCOL_VERSION)
        if share is None or share[0] not in self._shares:
            raise TLSError("the server's key share is for a group we did not send", ALERT_ILLEGAL_PARAMETER)
        self.group = share[0]
        self.transcript.add(message)
        shared = derive_shared_secret(self.group, self._shares.pop(self.group), share[1])
        self._shares.clear()
        self._state = "wait_encrypted_extensions"
        return self.schedule.enter_handshake(shared, self.transcript.hash())

    def handle_server_flight(self, data):
        """Consume the server's handshake-level bytes as they accumulate.

        ``data`` is everything received at that level so far. Returns None
        while more is needed; once the server's Finished verifies, returns
        ``(client_flight, secrets)`` where ``client_flight`` is what to send
        back at the handshake level and ``secrets`` holds the application
        traffic secrets.
        """
        import hmac as _hmac

        if self._state == "done":
            return None
        messages, self._offset = _split_messages(bytes(data), self._offset)
        for message_type, message, body in messages:
            state = self._state
            if state == "wait_encrypted_extensions":
                if message_type != ENCRYPTED_EXTENSIONS:
                    raise TLSError("expected EncryptedExtensions", ALERT_ILLEGAL_PARAMETER)
                self._read_encrypted_extensions(body)
                self._state = "wait_certificate"
            elif state == "wait_certificate" and message_type == CERTIFICATE_REQUEST:
                if self._certificate_request is not None:
                    raise TLSError("a second CertificateRequest", ALERT_ILLEGAL_PARAMETER)
                self._certificate_request = parse_certificate_request(body)
            elif state == "wait_certificate":
                if message_type != CERTIFICATE:
                    raise TLSError("expected the server Certificate", ALERT_ILLEGAL_PARAMETER)
                context, chain = parse_certificate(body)
                if context:
                    raise TLSError("unexpected certificate_request_context", ALERT_ILLEGAL_PARAMETER)
                self.peer_certificate = self.trust.verify_server(chain, self.server_name)
                self._state = "wait_certificate_verify"
            elif state == "wait_certificate_verify":
                if message_type != CERTIFICATE_VERIFY:
                    raise TLSError("expected CertificateVerify", ALERT_ILLEGAL_PARAMETER)
                verify_certificate_verify(
                    self.peer_certificate.public_key(), body, self.transcript.hash(), side="server"
                )
                self._state = "wait_finished"
            elif state == "wait_finished":
                if message_type != FINISHED:
                    raise TLSError("expected the server Finished", ALERT_ILLEGAL_PARAMETER)
                expected = build_finished(
                    self.schedule.server_handshake_secret, self.transcript.hash(), self.cipher_suite
                )[4:]
                if not _hmac.compare_digest(body, expected):
                    raise TLSError("server Finished does not verify", ALERT_DECRYPT_ERROR)
                self.transcript.add(message)
                return self._finish()
            else:
                raise TLSError("unexpected handshake message", ALERT_ILLEGAL_PARAMETER)
            self.transcript.add(message)
        return None

    def _read_encrypted_extensions(self, body):
        reader = _Reader(_Reader(body).block16())
        while reader.remaining():
            kind = reader.u16()
            data = reader.block16()
            if kind == EXT_ALPN:
                protocols = _parse_alpn(data)
                if len(protocols) != 1 or protocols[0] not in self.alpn_protocols:
                    raise TLSError("the server chose an ALPN we did not offer", ALERT_ILLEGAL_PARAMETER)
                self.selected_alpn = protocols[0]
            elif kind == EXT_QUIC_TRANSPORT_PARAMETERS:
                self.peer_transport_parameters = bytes(data)
        if self.alpn_protocols and self.selected_alpn is None:
            raise TLSError("the server chose no ALPN", ALERT_NO_APPLICATION_PROTOCOL)
        if self.peer_transport_parameters is None:
            raise TLSError("the server sent no QUIC transport parameters", ALERT_MISSING_EXTENSION)

    def _finish(self):
        # Application secrets cover the transcript up to the server Finished;
        # the client's own certificate messages come after (RFC 8446 7.1).
        client_app, server_app = self.schedule.enter_application(self.transcript.hash())
        flight = bytearray()
        if self._certificate_request is not None:
            context, offered = self._certificate_request
            certificate = build_certificate(self.certificate_chain, context)
            flight += certificate
            self.transcript.add(certificate)
            if self.certificate_chain:
                verify = build_certificate_verify(
                    self.private_key, self.transcript.hash(), offered, side="client"
                )
                flight += verify
                self.transcript.add(verify)
        finished = build_finished(
            self.schedule.client_handshake_secret, self.transcript.hash(), self.cipher_suite
        )
        flight += finished
        self.transcript.add(finished)
        self._state = "done"
        return bytes(flight), {
            "client_handshake": self.schedule.client_handshake_secret,
            "server_handshake": self.schedule.server_handshake_secret,
            "client_application": client_app,
            "server_application": server_app,
        }


def _is_ip_literal(name):
    import ipaddress

    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


__all__ = [
    "CERTIFICATE_REQUEST",
    "CertificateTrust",
    "ClientHandshake",
    "VERIFIABLE_SIGNATURE_SCHEMES",
    "build_certificate_request",
    "describe_certificate",
    "parse_certificate",
    "parse_certificate_request",
    "verify_certificate_verify",
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

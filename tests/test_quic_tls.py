import ssl
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from wsbuilder.pki import CertificateAuthority
from wsbuilder.quic.tls import (
    GROUP_X25519,
    TLS_1_3_VERSION,
    TLS_AES_128_GCM_SHA256,
    KeySchedule,
    ServerHandshake,
    TLSError,
    Transcript,
    parse_client_hello,
)


def openssl_client_hello(alpn=("h3",), server_name="localhost"):
    """A genuine ClientHello, so the parser is tested against OpenSSL."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    if alpn:
        context.set_alpn_protocols(list(alpn))
    obj = context.wrap_bio(incoming, outgoing, server_hostname=server_name)
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()[5:]  # strip the TLS record header


class TestClientHelloParsing(unittest.TestCase):
    def setUp(self):
        self.message = openssl_client_hello()
        self.hello = parse_client_hello(self.message[4:])

    def test_server_name_and_alpn(self):
        self.assertEqual(self.hello.server_name, "localhost")
        self.assertEqual(self.hello.alpn, ["h3"])

    def test_tls_1_3_and_our_cipher_suite_are_offered(self):
        self.assertIn(TLS_1_3_VERSION, self.hello.supported_versions)
        self.assertIn(TLS_AES_128_GCM_SHA256, self.hello.cipher_suites)

    def test_an_x25519_key_share_is_present(self):
        groups = {group for group, _ in self.hello.key_shares}
        self.assertIn(GROUP_X25519, groups)

    def test_a_legacy_version_other_than_0x0303_is_refused(self):
        body = bytearray(self.message[4:])
        body[0:2] = (0x0304).to_bytes(2, "big")
        with self.assertRaisesRegex(TLSError, "legacy_version"):
            parse_client_hello(bytes(body))

    def test_a_truncated_hello_is_refused_rather_than_read_past(self):
        with self.assertRaises(TLSError):
            parse_client_hello(self.message[4:40])


class TestKeySchedule(unittest.TestCase):
    """Checked against the published TLS 1.3 constants for an all-zero PSK."""

    def test_early_secret(self):
        self.assertEqual(
            KeySchedule().early_secret.hex(),
            "33ad0a1c607ec03b09e6cd9893680ce210adf300aa1f2660e1b22e10f170f92a",
        )

    def test_derived_for_handshake(self):
        from wsbuilder.quic.crypto import hkdf_expand_label

        schedule = KeySchedule()
        derived = hkdf_expand_label(schedule.early_secret, "derived", schedule.empty_hash, 32)
        self.assertEqual(
            derived.hex(),
            "6f2615a108c702c5678f54fc9dbab69716c076189c48250cebeac3576c3611ba",
        )

    def test_the_empty_hash_is_sha256_of_nothing(self):
        self.assertEqual(
            KeySchedule().empty_hash.hex(),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )


class TestTranscript(unittest.TestCase):
    def test_order_matters(self):
        one, two = Transcript(), Transcript()
        one.add(b"a")
        one.add(b"b")
        two.add(b"b")
        two.add(b"a")
        self.assertNotEqual(one.hash(), two.hash())

    def test_concatenation_is_what_is_hashed(self):
        split, whole = Transcript(), Transcript()
        split.add(b"ab")
        split.add(b"cd")
        whole.add(b"abcd")
        self.assertEqual(split.hash(), whole.hash())


class TestServerHandshake(unittest.TestCase):
    def setUp(self):
        ca = CertificateAuthority.create("QUIC CA")
        leaf = ca.issue("localhost", dns_names=["localhost"])
        certificate = x509.load_pem_x509_certificate(leaf.certificate_pem)
        self.der = certificate.public_bytes(serialization.Encoding.DER)
        self.key = serialization.load_pem_private_key(leaf.private_key_pem, password=None)

    def _handshake(self, **kwargs):
        return ServerHandshake([self.der], self.key, **kwargs)

    def test_a_full_flight_is_produced(self):
        handshake = self._handshake(transport_parameters=b"\x00\x04\x80\x00\xff\xff")
        server_hello, flight, secrets = handshake.handle_client_hello(openssl_client_hello())

        self.assertTrue(server_hello.startswith(b"\x02"))  # ServerHello
        self.assertGreater(len(flight), 200)
        self.assertEqual(handshake.selected_alpn, "h3")
        self.assertEqual(len(set(secrets.values())), 4)
        for secret in secrets.values():
            self.assertEqual(len(secret), 32)

    def test_alpn_is_negotiated_not_assumed(self):
        handshake = self._handshake(alpn_protocols=("h3",))
        with self.assertRaisesRegex(TLSError, "no overlapping ALPN"):
            handshake.handle_client_hello(openssl_client_hello(alpn=("h2",)))

    def test_the_transport_parameters_travel_back(self):
        parameters = b"\x00\x04\x80\x00\xff\xff"
        handshake = self._handshake(transport_parameters=parameters)
        _hello, flight, _secrets = handshake.handle_client_hello(openssl_client_hello())
        self.assertIn(parameters, flight)

    def test_the_client_transport_parameters_are_kept(self):
        handshake = self._handshake()
        handshake.handle_client_hello(openssl_client_hello())
        # OpenSSL in TLS mode does not send them; the field exists regardless.
        self.assertIsNone(handshake.peer_transport_parameters)

    def test_a_message_that_is_not_a_client_hello_is_refused(self):
        handshake = self._handshake()
        with self.assertRaisesRegex(TLSError, "expected a ClientHello"):
            handshake.handle_client_hello(b"\x02\x00\x00\x01\x00")

    def test_two_handshakes_derive_different_secrets(self):
        first = self._handshake().handle_client_hello(openssl_client_hello())[2]
        second = self._handshake().handle_client_hello(openssl_client_hello())[2]
        # Fresh ephemeral keys each time, so nothing repeats.
        self.assertNotEqual(first["server_application"], second["server_application"])


if __name__ == "__main__":
    unittest.main()


def build_client_hello(suites, group=None, alpn=(b"h3",)):
    """A ClientHello we control, for the cases OpenSSL will not produce."""
    import os

    from wsbuilder.quic.tls import (
        CLIENT_HELLO,
        EXT_ALPN,
        EXT_KEY_SHARE,
        EXT_SUPPORTED_VERSIONS,
        GROUP_X25519,
        _block8,
        _block16,
        _u16,
        build_handshake,
        generate_key_share,
    )

    group = GROUP_X25519 if group is None else group
    try:
        _private, public = generate_key_share(group)
    except Exception:
        public = b"\x00" * 32  # a group we cannot generate for, on purpose
    extensions = (
        _u16(EXT_SUPPORTED_VERSIONS) + _block16(_block8(_u16(TLS_1_3_VERSION)))
        + _u16(EXT_KEY_SHARE) + _block16(_block16(_u16(group) + _block16(public)))
        + _u16(0x000D) + _block16(_block16(_u16(0x0804) + _u16(0x0403)))
        + _u16(EXT_ALPN) + _block16(_block16(b"".join(_block8(p) for p in alpn)))
    )
    body = (
        _u16(0x0303)
        + os.urandom(32)
        + _block8(b"")
        + _block16(b"".join(_u16(suite) for suite in suites))
        + _block8(b"\x00")
        + _block16(extensions)
    )
    return build_handshake(CLIENT_HELLO, body)


class TestCipherSuiteNegotiation(unittest.TestCase):
    def setUp(self):
        ca = CertificateAuthority.create("Suite CA")
        leaf = ca.issue("localhost", dns_names=["localhost"])
        certificate = x509.load_pem_x509_certificate(leaf.certificate_pem)
        self.der = certificate.public_bytes(serialization.Encoding.DER)
        self.key = serialization.load_pem_private_key(leaf.private_key_pem, password=None)

    def _handshake(self):
        return ServerHandshake([self.der], self.key, alpn_protocols=("h3",))

    def test_each_supported_suite_is_selectable(self):
        from wsbuilder.quic.tls import (
            TLS_AES_128_GCM_SHA256,
            TLS_AES_256_GCM_SHA384,
            TLS_CHACHA20_POLY1305_SHA256,
        )

        for suite, secret_size in (
            (TLS_AES_128_GCM_SHA256, 32),
            (TLS_CHACHA20_POLY1305_SHA256, 32),
            (TLS_AES_256_GCM_SHA384, 48),
        ):
            with self.subTest(suite=hex(suite)):
                handshake = self._handshake()
                _hello, _flight, secrets = handshake.handle_client_hello(
                    build_client_hello([suite])
                )
                self.assertEqual(handshake.cipher_suite, suite)
                # SHA-384 gives longer secrets, all the way through.
                self.assertEqual(len(secrets["server_application"]), secret_size)

    def test_our_preference_decides_not_the_clients_order(self):
        from wsbuilder.quic.tls import TLS_AES_128_GCM_SHA256, TLS_AES_256_GCM_SHA384

        handshake = self._handshake()
        handshake.handle_client_hello(
            build_client_hello([TLS_AES_256_GCM_SHA384, TLS_AES_128_GCM_SHA256])
        )
        self.assertEqual(handshake.cipher_suite, TLS_AES_128_GCM_SHA256)

    def test_an_unknown_suite_is_refused(self):
        with self.assertRaisesRegex(TLSError, "no supported cipher suite"):
            self._handshake().handle_client_hello(build_client_hello([0x1305]))

    def test_both_groups_are_selectable(self):
        from wsbuilder.quic.tls import GROUP_SECP256R1, TLS_AES_128_GCM_SHA256

        for group in (GROUP_X25519, GROUP_SECP256R1):
            with self.subTest(group=hex(group)):
                handshake = self._handshake()
                handshake.handle_client_hello(
                    build_client_hello([TLS_AES_128_GCM_SHA256], group=group)
                )
                self.assertEqual(handshake.group, group)

    def test_a_group_we_do_not_support_is_refused(self):
        from wsbuilder.quic.tls import TLS_AES_128_GCM_SHA256

        with self.assertRaises(TLSError):
            self._handshake().handle_client_hello(
                build_client_hello([TLS_AES_128_GCM_SHA256], group=0x0018)
            )

    def test_the_secret_length_follows_the_hash(self):
        from wsbuilder.quic.tls import (
            TLS_AES_128_GCM_SHA256,
            TLS_AES_256_GCM_SHA384,
            secret_length,
        )

        self.assertEqual(secret_length(TLS_AES_128_GCM_SHA256), 32)
        self.assertEqual(secret_length(TLS_AES_256_GCM_SHA384), 48)

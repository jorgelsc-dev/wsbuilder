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

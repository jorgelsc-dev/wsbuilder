"""The QUIC client half, driven against the server half in memory and over UDP."""

import threading
import unittest

from cryptography.hazmat.primitives import serialization

from wsbuilder import App, CertificateAuthority, CertificateManager, Http3Client, Http3Error
from wsbuilder.http3_server import Http3Server, certificate_chain_der
from wsbuilder.quic import frames as qf
from wsbuilder.quic.client import QuicClientConnection
from wsbuilder.quic.connection import LEVEL_APPLICATION, QuicConnection
from wsbuilder.quic.packet import MIN_INITIAL_DATAGRAM
from wsbuilder.quic.tls import (
    ALERT_BAD_CERTIFICATE,
    HELLO_RETRY_REQUEST_RANDOM,
    CertificateTrust,
    ClientHandshake,
    TLSError,
)


def _server_identity(ca, name="localhost"):
    manager = CertificateManager(ca=ca, common_name=name, dns_names=[name])
    material = manager.current()
    key = serialization.load_pem_private_key(
        material.private_key_pem, password=material.key_password
    )
    return certificate_chain_der(material), key


def _client_identity(material):
    from wsbuilder.http3_client import _client_identity as load

    return load(material)


def _pump(client, server, first, rounds=10):
    """Exchange datagrams until both sides fall silent; returns the round count."""
    to_server = list(first)
    for count in range(rounds):
        to_client = []
        for datagram in to_server:
            to_client.extend(server.receive_datagram(datagram))
        to_server = []
        for datagram in to_client:
            to_server.extend(client.receive_datagram(datagram))
        if not to_server and not to_client:
            return count
    return rounds


class TestInMemoryHandshake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca = CertificateAuthority.create("Client Test CA")
        cls.chain, cls.key = _server_identity(cls.ca)
        cls.trust = CertificateTrust.from_pem(cls.ca.certificate_pem)

    def _pair(self, server_name="localhost", trust=None, **server_options):
        server = QuicConnection(self.chain, self.key, client_address=("10.0.0.1", 4433), **server_options)
        client = QuicClientConnection(server_name, trust or self.trust)
        return client, server

    def test_both_sides_finish_and_agree_on_alpn(self):
        client, server = self._pair()
        first = client.start()
        self.assertGreaterEqual(len(first[0]), MIN_INITIAL_DATAGRAM)
        _pump(client, server, first)
        self.assertTrue(client.handshake_complete)
        self.assertTrue(server.handshake_complete)
        self.assertEqual(client.alpn, "h3")
        self.assertEqual(server.alpn, "h3")
        self.assertIsNone(client.error)
        self.assertEqual(client.peer_certificate.subject.rfc4514_string(), "CN=localhost")

    def test_the_exchange_settles_instead_of_trading_acks(self):
        # Neither side may acknowledge an ACK-only packet, or the two would
        # answer each other for ever.
        client, server = self._pair()
        rounds = _pump(client, server, client.start(), rounds=20)
        self.assertLess(rounds, 6)

    def test_stream_data_flows_both_ways(self):
        received = {}

        def on_server_data(connection, stream_id, data, complete):
            received.setdefault(stream_id, bytearray()).extend(data)
            if complete:
                return [qf.StreamFrame(stream_id, 0, b"pong:" + bytes(received[stream_id]), True)]
            return []

        client, server = self._pair()
        server.on_stream_data = on_server_data
        answers = {}
        client.on_stream_data = lambda c, s, d, done: answers.setdefault(s, bytearray()).extend(d) or []
        _pump(client, server, client.start())
        stream_id = client.open_bidi_stream()
        client.queue_stream_data(stream_id, b"ping", fin=True)
        _pump(client, server, client.drain_outbound())
        self.assertEqual(bytes(answers[stream_id]), b"pong:ping")

    def test_a_server_for_another_name_is_refused(self):
        client, server = self._pair(server_name="billing.internal")
        _pump(client, server, client.start())
        self.assertTrue(client.closed)
        self.assertFalse(client.handshake_complete)
        self.assertIn("server certificate rejected", client.error)

    def test_a_server_from_an_untrusted_authority_is_refused(self):
        other = CertificateTrust.from_pem(CertificateAuthority.create("Other CA").certificate_pem)
        client, server = self._pair(trust=other)
        _pump(client, server, client.start())
        self.assertTrue(client.closed)
        self.assertIsNotNone(client.error)
        self.assertNotIn(LEVEL_APPLICATION, client.keys)

    def test_mutual_tls_identifies_the_client(self):
        client_material = self.ca.issue("orders", client_auth=True, server_auth=False)
        chain, key = _client_identity(client_material)
        server = QuicConnection(
            self.chain, self.key, client_address=("10.0.0.1", 4433),
            client_trust=self.trust, require_client_certificate=True,
        )
        client = QuicClientConnection(
            "localhost", self.trust, certificate_chain_der=chain, private_key=key
        )
        _pump(client, server, client.start())
        self.assertTrue(server.handshake_complete)
        self.assertEqual(server.peer_certificate.subject.rfc4514_string(), "CN=orders")

    def test_mutual_tls_refuses_a_client_without_a_certificate(self):
        server = QuicConnection(
            self.chain, self.key, client_address=("10.0.0.1", 4433),
            client_trust=self.trust, require_client_certificate=True,
        )
        client = QuicClientConnection("localhost", self.trust)
        _pump(client, server, client.start())
        self.assertFalse(server.handshake_complete)
        self.assertTrue(server.closed)
        self.assertIn("client certificate is required", client.error)

    def test_mutual_tls_refuses_a_certificate_from_another_authority(self):
        stranger = CertificateAuthority.create("Stranger CA").issue(
            "intruder", client_auth=True, server_auth=False
        )
        chain, key = _client_identity(stranger)
        server = QuicConnection(
            self.chain, self.key, client_address=("10.0.0.1", 4433),
            client_trust=self.trust, require_client_certificate=True,
        )
        client = QuicClientConnection(
            "localhost", self.trust, certificate_chain_der=chain, private_key=key
        )
        _pump(client, server, client.start())
        self.assertFalse(server.handshake_complete)
        self.assertIsNone(server.peer_certificate)

    def test_optional_client_certificates_may_be_omitted(self):
        server = QuicConnection(
            self.chain, self.key, client_address=("10.0.0.1", 4433), client_trust=self.trust
        )
        client = QuicClientConnection("localhost", self.trust)
        _pump(client, server, client.start())
        self.assertTrue(server.handshake_complete)
        self.assertIsNone(server.peer_certificate)


class TestEarlyApplicationData(unittest.TestCase):
    """RFC 9001 section 5.7: no 1-RTT data is acted on before the client is verified."""

    def test_requests_wait_for_the_client_finished(self):
        ca = CertificateAuthority.create("Early CA")
        chain, key = _server_identity(ca)
        trust = CertificateTrust.from_pem(ca.certificate_pem)
        seen = []
        server = QuicConnection(
            chain, key, client_address=("10.0.0.1", 4433),
            on_stream_data=lambda c, s, d, done: seen.append((s, bytes(d))) or [],
        )
        client = QuicClientConnection("localhost", trust)
        to_client = []
        for datagram in client.start():
            to_client.extend(server.receive_datagram(datagram))
        to_server = []
        for datagram in to_client:
            to_server.extend(client.receive_datagram(datagram))
        self.assertTrue(client.handshake_complete)
        # Queue a request and deliver it *before* the Finished datagram.
        stream_id = client.open_bidi_stream()
        client.queue_stream_data(stream_id, b"too early", fin=True)
        early = client.drain_outbound()
        for datagram in early:
            server.receive_datagram(datagram)
        self.assertEqual(seen, [])
        self.assertFalse(server.handshake_complete)
        for datagram in to_server:
            server.receive_datagram(datagram)
        self.assertTrue(server.handshake_complete)
        self.assertEqual(seen, [(stream_id, b"too early")])


class TestClientHandshakeChecks(unittest.TestCase):
    def setUp(self):
        self.trust = CertificateTrust.from_pem(
            CertificateAuthority.create("Check CA").certificate_pem
        )

    def test_a_trust_store_is_mandatory(self):
        with self.assertRaises(ValueError):
            ClientHandshake("localhost", None)

    def test_a_hello_retry_request_is_refused(self):
        handshake = ClientHandshake("localhost", self.trust)
        handshake.client_hello()
        body = b"\x03\x03" + HELLO_RETRY_REQUEST_RANDOM + b"\x00" + b"\x13\x01" + b"\x00" + b"\x00\x00"
        message = b"\x02" + len(body).to_bytes(3, "big") + body
        with self.assertRaises(TLSError):
            handshake.handle_server_hello(message)

    def test_an_empty_server_chain_is_a_bad_certificate(self):
        with self.assertRaises(TLSError) as caught:
            self.trust.verify_server([], "localhost")
        self.assertEqual(caught.exception.alert, ALERT_BAD_CERTIFICATE)

    def test_the_client_hello_names_the_server_and_offers_h3(self):
        from wsbuilder.quic.tls import parse_client_hello

        message = ClientHandshake("users.internal", self.trust).client_hello()
        hello = parse_client_hello(message[4:])
        self.assertEqual(hello.server_name, "users.internal")
        self.assertEqual(hello.alpn, ["h3"])
        self.assertEqual(hello.legacy_session_id, b"")
        self.assertIsNotNone(hello.transport_parameters)

    def test_an_ip_address_is_not_sent_as_a_server_name(self):
        from wsbuilder.quic.tls import parse_client_hello

        message = ClientHandshake("127.0.0.1", self.trust).client_hello()
        self.assertIsNone(parse_client_hello(message[4:]).server_name)


class Http3ServerFixture(unittest.TestCase):
    require_address_validation = False
    client_ca = False

    def setUp(self):
        self.ca = CertificateAuthority.create("H3 Client CA")
        options = {}
        if self.client_ca:
            options = {"client_ca_pem": self.ca.certificate_pem, "require_client_cert": True}
        self.manager = CertificateManager(
            ca=self.ca, common_name="localhost", dns_names=["localhost"], **options
        )
        self.app = App()
        self.addCleanup(self.app.close)

        @self.app.api("/hi", methods=("GET", "POST"))
        def hi(request):
            peer = (request.tls or {}).get("peer_cert") or {}
            return {
                "version": request.version,
                "length": len(request.body),
                "peer": peer.get("common_name"),
            }

        @self.app.api("/big", methods=("GET",))
        def big(_request):
            return {"data": "x" * 200000}

        @self.app.api("/slow", methods=("GET",))
        def slow(_request):
            self.release.wait(5)
            return {"slow": True}

        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.server = Http3Server(
            "127.0.0.1", 0, self.app, self.manager,
            require_address_validation=self.require_address_validation,
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(self.server.stop)
        self.assertTrue(self.server.wait_until_serving(timeout=5.0))
        self.port = self.server.server_address[1]

    def client(self, **options):
        options.setdefault("trust", self.ca)
        options.setdefault("timeout", 5.0)
        client = Http3Client("127.0.0.1", self.port, server_name="localhost", **options)
        self.addCleanup(client.close)
        return client


class TestHttp3ClientOverUdp(Http3ServerFixture):
    def test_get_and_post(self):
        client = self.client()
        response = client.request("GET", "/hi")
        self.assertEqual(response.status, 200)
        self.assertIn(b'"version":"HTTP/3"', response.body)
        self.assertEqual(response.header("Content-Type"), "application/json; charset=utf-8")
        response = client.request("POST", "/hi", body=b"z" * 50000)
        self.assertIn(b'"length":50000', response.body)

    def test_one_connection_serves_many_requests(self):
        client = self.client()
        for _ in range(20):
            self.assertEqual(client.request("GET", "/hi").status, 200)
        self.assertEqual(client.connections_opened, 1)

    def test_a_new_connection_opens_when_the_stream_allowance_runs_out(self):
        client = self.client()
        for _ in range(105):
            client.request("GET", "/hi")
        self.assertEqual(client.connections_opened, 2)

    def test_requests_from_many_threads_share_the_connection(self):
        client = self.client()
        results, errors = [], []

        def worker():
            try:
                for _ in range(5):
                    results.append(client.request("GET", "/hi").status)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(results, [200] * 40)

    def test_a_slow_response_does_not_hold_up_another(self):
        client = self.client()
        outcome = {}
        slow = threading.Thread(target=lambda: outcome.setdefault("slow", client.request("GET", "/slow")))
        slow.start()
        self.assertEqual(client.request("GET", "/hi").status, 200)
        self.assertNotIn("slow", outcome)
        self.release.set()
        slow.join(5)
        self.assertEqual(outcome["slow"].status, 200)

    def test_a_large_response_arrives_whole(self):
        response = self.client().request("GET", "/big")
        self.assertEqual(len(response.body), len(b'{"data":""}') + 200000)

    def test_a_response_over_the_limit_is_refused(self):
        with self.assertRaises(Http3Error):
            self.client(max_response_bytes=1000).request("GET", "/big")

    def test_a_request_that_takes_too_long_times_out(self):
        with self.assertRaises(Http3Error):
            self.client().request("GET", "/slow", timeout=0.3)

    def test_an_untrusted_server_is_refused(self):
        with self.assertRaises(Http3Error):
            self.client(trust=CertificateAuthority.create("Wrong CA")).request("GET", "/hi")

    def test_nothing_listening_times_out(self):
        self.server.stop()
        client = Http3Client(
            "127.0.0.1", self.port, server_name="localhost", trust=self.ca, timeout=0.5
        )
        self.addCleanup(client.close)
        with self.assertRaises(Http3Error):
            client.request("GET", "/hi")


class TestHttp3ClientAfterRetry(Http3ServerFixture):
    require_address_validation = True

    def test_the_client_follows_a_retry(self):
        response = self.client().request("GET", "/hi")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.server.retries_sent, 1)


class TestHttp3MutualTls(Http3ServerFixture):
    client_ca = True

    def test_the_handler_sees_the_verified_client(self):
        material = self.ca.issue("orders-service", client_auth=True, server_auth=False)
        response = self.client(client_material=material).request("GET", "/hi")
        self.assertIn(b'"peer":"orders-service"', response.body)

    def test_a_client_without_a_certificate_is_refused(self):
        with self.assertRaises(Http3Error) as caught:
            self.client().request("GET", "/hi")
        self.assertIn("certificate", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

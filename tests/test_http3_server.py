import socket
import ssl
import threading
import unittest

from wsbuilder import App, CertificateAuthority, CertificateManager
from wsbuilder import http3
from wsbuilder.http3_server import Http3Server, alt_svc_header, certificate_chain_der
from wsbuilder.qpack import decode_field_section, encode_field_section
from wsbuilder.quic import frames as qf
from wsbuilder.quic.connection import (
    QuicConnection,
    StreamBuffer,
    decode_transport_parameters,
    encode_transport_parameters,
)
from wsbuilder.quic.crypto import (
    AEAD_TAG_SIZE,
    PacketKeys,
    apply_header_protection,
    initial_secrets,
    remove_header_protection,
)
from wsbuilder.quic.packet import (
    MIN_INITIAL_DATAGRAM,
    PACKET_INITIAL,
    build_long_header,
    build_short_header,
    is_long_header,
    new_connection_id,
    parse_long_header,
)


def openssl_client_hello(alpn=("h3",)):
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(list(alpn))
    obj = context.wrap_bio(incoming, outgoing, server_hostname="localhost")
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()[5:]


class TestStreamReassembly(unittest.TestCase):
    def test_pieces_in_order(self):
        buffer = StreamBuffer()
        self.assertEqual(buffer.add(0, b"hello "), b"hello ")
        self.assertEqual(buffer.add(6, b"world", fin=True), b"world")
        self.assertTrue(buffer.complete)

    def test_a_piece_that_arrives_early_waits(self):
        buffer = StreamBuffer()
        self.assertEqual(buffer.add(6, b"world"), b"")
        # Only once the gap in front is filled does anything become readable.
        self.assertEqual(buffer.add(0, b"hello "), b"hello world")

    def test_overlapping_pieces_are_not_duplicated(self):
        buffer = StreamBuffer()
        buffer.add(0, b"hello")
        self.assertEqual(buffer.add(2, b"llo world"), b" world")
        self.assertEqual(bytes(buffer.data), b"hello world")

    def test_a_repeat_adds_nothing(self):
        buffer = StreamBuffer()
        buffer.add(0, b"abc")
        self.assertEqual(buffer.add(0, b"abc"), b"")
        self.assertEqual(bytes(buffer.data), b"abc")


class TestTransportParameters(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_transport_parameters({0x04: 1048576, 0x0F: b"\x01\x02"})
        decoded = decode_transport_parameters(encoded)
        self.assertEqual(decoded[0x0F], b"\x01\x02")
        self.assertIn(0x04, decoded)

    def test_a_parameter_past_the_end_is_refused(self):
        from wsbuilder.quic.packet import QuicPacketError

        with self.assertRaises(QuicPacketError):
            decode_transport_parameters(b"\x04\x40\x10short")


class QuicClientHarness(unittest.TestCase):
    """A minimal QUIC client, enough to drive the server over real UDP."""

    def setUp(self):
        self.app = App()
        self.addCleanup(self.app.close)

        @self.app.api("/hi", methods=("GET",))
        def hi(request):
            return {
                "version": request.version,
                "alpn": (request.tls or {}).get("alpn"),
                "n": request.query.get("n"),
            }

        @self.app.api("/echo", methods=("POST",))
        def echo(request):
            return {"length": len(request.body)}

        ca = CertificateAuthority.create("H3 CA")
        self.manager = CertificateManager(
            ca=ca, common_name="localhost", dns_names=["localhost"]
        )
        self.server = Http3Server("127.0.0.1", 0, self.app, self.manager)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(self.server.stop)
        self.assertTrue(self.server.wait_until_serving(timeout=5.0))

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5.0)
        self.addCleanup(self.sock.close)
        self.dcid = new_connection_id(8)
        self.scid = new_connection_id(8)

    def _handshake(self):
        client_secret, _server = initial_secrets(self.dcid)
        keys = PacketKeys(client_secret)
        payload = qf.pad_to(
            qf.serialize_frames([qf.CryptoFrame(0, openssl_client_hello())]), 1162
        )
        header = build_long_header(
            PACKET_INITIAL,
            1,
            self.dcid,
            self.scid,
            packet_number=0,
            payload_length=len(payload) + AEAD_TAG_SIZE,
        )
        packet = apply_header_protection(
            keys, header + keys.seal(0, header, payload), len(header) - 4, 4
        )
        self.sock.sendto(packet, self.server.server_address)

        replies = []
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                break
            replies.append(data)
            # The flight is already in flight; drain the rest impatiently so
            # the suite does not wait out a full timeout each handshake.
            self.sock.settimeout(0.3)
        self.sock.settimeout(5.0)
        return replies

    def _connection(self):
        return next(iter({id(c): c for c in self.server.connections.values()}.values()))

    def _request(self, fields, body=b"", stream_id=0):
        connection = self._connection()
        recv = connection.keys["application"]["recv"]
        send = connection.keys["application"]["send"]

        payload = http3.encode_frame(http3.FRAME_HEADERS, encode_field_section(fields))
        if body:
            payload += http3.encode_frame(http3.FRAME_DATA, body)
        frames = qf.pad_to(
            qf.serialize_frames([qf.StreamFrame(stream_id, 0, payload, fin=True)]), 60
        )
        header = build_short_header(connection.host_cid, packet_number=0)
        packet = apply_header_protection(
            recv, header + recv.seal(0, header, frames), len(header) - 4, 4
        )
        self.sock.sendto(packet, self.server.server_address)

        data, _ = self.sock.recvfrom(65535)
        pn_offset = 1 + len(self.scid)
        cleaned, number, pn_length = remove_header_protection(send, data, pn_offset)
        opened = send.open(
            number, cleaned[: pn_offset + pn_length], cleaned[pn_offset + pn_length :]
        )
        for frame in qf.parse_frames(opened):
            if isinstance(frame, qf.StreamFrame) and frame.data:
                parsed, _tail = http3.parse_frames(frame.data)
                headers = dict(decode_field_section(parsed[0][1]))
                content = parsed[1][1] if len(parsed) > 1 else b""
                return headers, content
        raise AssertionError("no response stream frame")


class TestHandshakeOverUdp(QuicClientHarness):
    def test_the_server_answers_an_initial(self):
        replies = self._handshake()
        self.assertGreaterEqual(len(replies), 1)
        self.assertTrue(all(is_long_header(reply[0]) for reply in replies))

    def test_the_first_datagram_is_padded_against_amplification(self):
        replies = self._handshake()
        self.assertGreaterEqual(len(replies[0]), MIN_INITIAL_DATAGRAM)

    def test_alpn_is_negotiated(self):
        self._handshake()
        self.assertEqual(self._connection().alpn, "h3")

    def test_all_three_key_levels_are_installed(self):
        self._handshake()
        self.assertEqual(
            sorted(self._connection().keys), ["application", "handshake", "initial"]
        )

    def test_the_server_hello_is_readable_by_the_client(self):
        replies = self._handshake()
        _client, server_secret = initial_secrets(self.dcid)
        keys = PacketKeys(server_secret)
        header = parse_long_header(replies[0])
        cleaned, number, pn_length = remove_header_protection(
            keys, replies[0][: header.packet_length], header.payload_offset
        )
        split = header.payload_offset + pn_length
        opened = keys.open(number, cleaned[:split], cleaned[split:])
        frames = qf.parse_frames(opened)
        self.assertIsInstance(frames[0], qf.CryptoFrame)
        self.assertEqual(frames[0].data[0], 0x02)  # a ServerHello


class TestRequestsOverUdp(QuicClientHarness):
    def test_a_get_is_answered(self):
        self._handshake()
        headers, body = self._request(
            [
                (":method", "GET"),
                (":scheme", "https"),
                (":path", "/hi?n=42"),
                (":authority", "localhost"),
            ]
        )
        self.assertEqual(headers[":status"], "200")
        self.assertIn(b'"version":"HTTP/3"', body)
        self.assertIn(b'"alpn":"h3"', body)
        self.assertIn(b'"n":"42"', body)

    def test_a_post_body_arrives(self):
        self._handshake()
        headers, body = self._request(
            [
                (":method", "POST"),
                (":scheme", "https"),
                (":path", "/echo"),
                (":authority", "localhost"),
            ],
            body=b"hola mundo",
        )
        self.assertEqual(headers[":status"], "200")
        self.assertIn(b'"length":10', body)

    def test_a_missing_route_answers_404(self):
        self._handshake()
        headers, _body = self._request(
            [
                (":method", "GET"),
                (":scheme", "https"),
                (":path", "/nope"),
                (":authority", "localhost"),
            ]
        )
        self.assertEqual(headers[":status"], "404")

    def test_a_one_rtt_reply_is_not_padded(self):
        # Padding a short header packet puts the pad inside the AEAD
        # ciphertext, because such a packet runs to the end of its datagram.
        self._handshake()
        connection = self._connection()
        recv = connection.keys["application"]["recv"]
        payload = http3.encode_frame(
            http3.FRAME_HEADERS,
            encode_field_section(
                [
                    (":method", "GET"),
                    (":scheme", "https"),
                    (":path", "/hi"),
                    (":authority", "localhost"),
                ]
            ),
        )
        frames = qf.pad_to(qf.serialize_frames([qf.StreamFrame(0, 0, payload, fin=True)]), 60)
        header = build_short_header(connection.host_cid, packet_number=0)
        self.sock.sendto(
            apply_header_protection(
                recv, header + recv.seal(0, header, frames), len(header) - 4, 4
            ),
            self.server.server_address,
        )
        data, _ = self.sock.recvfrom(65535)
        self.assertLess(len(data), MIN_INITIAL_DATAGRAM)


class TestServerHelpers(unittest.TestCase):
    def test_alt_svc_points_at_the_udp_port(self):
        self.assertEqual(alt_svc_header(8443), 'h3=":8443"; ma=86400')

    def test_the_certificate_chain_reaches_der(self):
        ca = CertificateAuthority.create("Chain CA")
        manager = CertificateManager(ca=ca, common_name="localhost")
        chain = certificate_chain_der(manager.current())
        self.assertEqual(len(chain), 2)  # leaf plus the issuing authority
        self.assertTrue(all(isinstance(der, bytes) for der in chain))

    def test_unknown_datagrams_are_ignored(self):
        app = App()
        self.addCleanup(app.close)
        ca = CertificateAuthority.create("CA")
        manager = CertificateManager(ca=ca, common_name="localhost")
        server = Http3Server("127.0.0.1", 0, app, manager)
        self.assertEqual(server.handle_datagram(b"", ("127.0.0.1", 1)), [])


if __name__ == "__main__":
    unittest.main()

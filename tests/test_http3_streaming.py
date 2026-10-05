"""Unit coverage for HTTP/3 streaming, RFC 9220 extended CONNECT and the WebSocket adapter."""

import socket
import threading
import unittest

from wsbuilder import http3
from wsbuilder.http3_websocket import Http3WebSocketStream
from wsbuilder.quic.connection import STREAM_CHUNK_SIZE, QuicConnection


def _connect_headers():
    return [
        (":method", "CONNECT"),
        (":protocol", "websocket"),
        (":scheme", "https"),
        (":authority", "localhost"),
        (":path", "/echo"),
        ("sec-websocket-version", "13"),
    ]


class TestExtendedConnectValidation(unittest.TestCase):
    def test_a_well_formed_websocket_connect_is_accepted(self):
        pseudo, regular = http3.validate_request_headers(_connect_headers())
        self.assertEqual(pseudo[":protocol"], "websocket")
        self.assertEqual(pseudo[":path"], "/echo")

    def test_an_unknown_protocol_is_refused(self):
        headers = _connect_headers()
        headers[1] = (":protocol", "webtransport")
        with self.assertRaises(http3.H3Error):
            http3.validate_request_headers(headers)

    def test_extended_connect_needs_a_path(self):
        headers = [h for h in _connect_headers() if h[0] != ":path"]
        with self.assertRaises(http3.H3Error):
            http3.validate_request_headers(headers)

    def test_protocol_is_refused_on_other_methods(self):
        headers = _connect_headers()
        headers[0] = (":method", "GET")
        with self.assertRaises(http3.H3Error):
            http3.validate_request_headers(headers)

    def test_a_plain_connect_still_needs_only_authority(self):
        pseudo, _ = http3.validate_request_headers(
            [(":method", "CONNECT"), (":authority", "example.test:443")]
        )
        self.assertEqual(pseudo[":method"], "CONNECT")

    def test_is_extended_connect_reads_the_request_stream(self):
        stream = http3.RequestStream(0)
        stream.headers = _connect_headers()
        self.assertTrue(http3.is_extended_connect(stream))

        stream.headers = [(":method", "GET"), (":scheme", "https"), (":path", "/"), (":authority", "x")]
        self.assertFalse(http3.is_extended_connect(stream))


class TestControlStream(unittest.TestCase):
    def test_settings_advertise_extended_connect(self):
        stream = http3.build_control_stream()
        frames, _tail = http3.parse_frames(stream[1:])  # skip the stream type byte
        frame_type, payload = frames[0]
        self.assertEqual(frame_type, http3.FRAME_SETTINGS)
        settings = http3.decode_settings(payload)
        self.assertEqual(settings[http3.SETTINGS_ENABLE_CONNECT_PROTOCOL], 1)


class TestResponseHead(unittest.TestCase):
    def test_head_carries_status_and_no_body(self):
        fields = http3.build_response_head(200, {"sec-websocket-protocol": "chat", "connection": "close"})
        self.assertEqual(fields[0], (":status", "200"))
        self.assertIn(("sec-websocket-protocol", "chat"), fields)
        self.assertFalse(any(name == "connection" for name, _ in fields))

    def test_the_head_frame_is_a_headers_frame(self):
        frame = http3.encode_headers_frame(http3.build_response_head(404))
        frames, tail = http3.parse_frames(frame)
        self.assertEqual(tail, b"")
        self.assertEqual(frames[0][0], http3.FRAME_HEADERS)


class TestWebSocketStream(unittest.TestCase):
    class _Connection:
        def __init__(self):
            self.queued = []

        def queue_stream_data(self, stream_id, payload, fin=False):
            self.queued.append((stream_id, bytes(payload), fin))

    def setUp(self):
        self.connection = self._Connection()
        self.stream = Http3WebSocketStream(self.connection, 4)

    def test_pushed_bytes_are_read_back_in_order(self):
        self.stream.push(b"abc")
        self.stream.push(b"def")
        self.assertEqual(self.stream.recv(4), b"abcd")
        self.assertEqual(self.stream.recv(10), b"ef")

    def test_end_of_stream_reads_as_empty(self):
        self.stream.push_eof()
        self.assertEqual(self.stream.recv(10), b"")

    def test_an_idle_read_times_out(self):
        self.stream.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            self.stream.recv(1)

    def test_a_blocked_read_wakes_when_bytes_arrive(self):
        self.stream.settimeout(2.0)
        result = []
        reader = threading.Thread(target=lambda: result.append(self.stream.recv(3)))
        reader.start()
        self.stream.push(b"hey")
        reader.join(2.0)
        self.assertEqual(result, [b"hey"])

    def test_sendall_queues_a_data_frame(self):
        self.stream.sendall(b"payload")
        stream_id, payload, fin = self.connection.queued[0]
        self.assertEqual(stream_id, 4)
        self.assertFalse(fin)
        frames, _tail = http3.parse_frames(payload)
        self.assertEqual(frames, [(http3.FRAME_DATA, b"payload")])

    def test_close_queues_end_of_stream_once(self):
        self.stream.close()
        self.stream.close()
        self.assertEqual(self.connection.queued, [(4, b"", True)])
        with self.assertRaises(ConnectionError):
            self.stream.sendall(b"late")


class TestOutboundQueue(unittest.TestCase):
    def _bare_connection(self):
        # The queue needs no keys to be filled; only draining does.
        connection = QuicConnection.__new__(QuicConnection)
        connection._outbound_lock = threading.Lock()
        connection._outbound = []
        connection._send_offsets = {}
        return connection

    def test_long_payloads_are_split_into_packet_sized_pieces(self):
        connection = self._bare_connection()
        connection.queue_stream_data(0, b"x" * (STREAM_CHUNK_SIZE * 2 + 5), fin=True)
        sizes = [len(piece) for _sid, piece, _fin in connection._outbound]
        self.assertEqual(sizes, [STREAM_CHUNK_SIZE, STREAM_CHUNK_SIZE, 5])

    def test_only_the_last_piece_carries_fin(self):
        connection = self._bare_connection()
        connection.queue_stream_data(0, b"x" * (STREAM_CHUNK_SIZE + 1), fin=True)
        fins = [fin for _sid, _piece, fin in connection._outbound]
        self.assertEqual(fins, [False, True])

    def test_an_empty_payload_with_fin_is_still_queued(self):
        connection = self._bare_connection()
        connection.queue_stream_data(8, b"", fin=True)
        self.assertEqual(connection._outbound, [(8, b"", True)])


if __name__ == "__main__":
    unittest.main()

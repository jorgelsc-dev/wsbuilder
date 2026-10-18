"""Streamed responses and RFC 9220 WebSockets over real UDP, using only wsbuilder.

These reuse the minimal QUIC client from ``test_http3_server`` and add a
streaming route and a WebSocket route to the same app.
"""

import threading
import time
import unittest

from test_http3_server import QuicClientHarness, _server_frames
from wsbuilder import Response, http3
from wsbuilder.qpack import decode_field_section, encode_field_section
from wsbuilder.quic import frames as qf
from wsbuilder.quic.crypto import apply_header_protection
from wsbuilder.quic.packet import build_short_header
from wsbuilder.ws import WebSocketConnectionClosedError


def _masked_text_frame(text):
    """A client-to-server WebSocket text frame, which RFC 6455 requires masked."""
    payload = text.encode("utf-8")
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes([0x81, 0x80 | len(payload)]) + mask + masked


def _unmasked_text(frame_bytes):
    assert frame_bytes[0] & 0x0F == 0x1, "expected a text frame"
    length = frame_bytes[1] & 0x7F
    return frame_bytes[2:2 + length].decode("utf-8")


class TestStreamingAndWebSocketOverUdp(QuicClientHarness):
    def setUp(self):
        super().setUp()
        self._packet_number = 0
        self._offsets = {}

        @self.app.api("/sse", methods=("GET",))
        def sse(request):
            def chunks():
                for index in range(3):
                    yield f"event {index}\n\n"
                    time.sleep(0.1)

            return Response(200, stream=chunks(), headers={"content-type": "text/event-stream"})

        @self.app.ws("/echo")
        def echo(ws, request=None):
            while True:
                frame = ws.recv_frame()
                if frame.opcode == 0x8:
                    break
                if frame.opcode == 0x1:
                    ws.send_text("echo:" + frame.payload.decode("utf-8"))

    # -- client plumbing -----------------------------------------------

    def _send(self, stream_id, data, fin=False):
        offset = self._offsets.get(stream_id, 0)
        self._offsets[stream_id] = offset + len(data)
        self._send_frames([qf.StreamFrame(stream_id, offset, data, fin=fin)])

    def _send_frames(self, frames):
        connection = self._connection()
        recv = connection.keys["application"]["recv"]
        number = self._packet_number
        self._packet_number += 1
        frames = qf.pad_to(qf.serialize_frames(frames), 60)
        header = build_short_header(connection.host_cid, packet_number=number)
        packet = apply_header_protection(
            recv, header + recv.seal(number, header, frames), len(header) - 4, 4
        )
        self.sock.sendto(packet, self.server.server_address)

    def _replies(self, stream_id):
        """The server's bytes for one stream, read until the socket goes quiet."""
        send = self._connection().keys["application"]["send"]
        frames = _server_frames(self.sock, send, 1 + len(self.scid))
        data = b""
        status = None
        ended = False
        for frame in frames:
            if isinstance(frame, qf.StreamFrame) and frame.stream_id == stream_id:
                data += frame.data
                ended = ended or frame.fin
        parsed, tail = http3.parse_frames(data)
        bodies = []
        for frame_type, payload in parsed:
            if frame_type == http3.FRAME_HEADERS and status is None:
                fields = dict(decode_field_section(payload))
                status = fields.get(":status")
            elif frame_type == http3.FRAME_DATA:
                bodies.append(payload)
        return status, bodies, ended

    @staticmethod
    def _request_fields(method, path, *extra):
        fields = [
            (":method", method),
            (":scheme", "https"),
            (":authority", "localhost"),
            (":path", path),
        ]
        return fields + list(extra)

    # -- tests ---------------------------------------------------------

    def _open_waiting_websocket(self):
        entered = threading.Event()
        exited = threading.Event()
        workers = []

        @self.app.ws("/waiting")
        def waiting(ws, request):
            workers.append(threading.current_thread())
            entered.set()
            try:
                ws.recv_frame()
            except WebSocketConnectionClosedError:
                pass
            finally:
                exited.set()

        self._handshake()
        fields = self._request_fields(
            "CONNECT", "/waiting", (":protocol", "websocket"),
            ("sec-websocket-version", "13"),
        )
        self._send(0, http3.encode_headers_frame(fields))
        status, _bodies, ended = self._replies(0)
        self.assertEqual(status, "200")
        self.assertFalse(ended)
        self.assertTrue(entered.wait(2.0))
        connection = self._connection()
        # Wake the handler even if a regression makes the assertion fail.
        self.addCleanup(workers[0].join, 2.0)
        self.addCleanup(connection.h3_streams[0].websocket.push_eof)
        return connection, workers[0], exited

    def test_connection_close_releases_a_blocked_websocket_handler(self):
        connection, worker, exited = self._open_waiting_websocket()
        self._send_frames([qf.ConnectionCloseFrame(http3.H3_NO_ERROR, application=True)])
        self.assertTrue(exited.wait(2.0), "WebSocket handler did not observe connection close")
        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(connection.closed)
        self.assertEqual(self.server.connections, {})

    def test_server_stop_releases_a_blocked_websocket_handler(self):
        connection, worker, exited = self._open_waiting_websocket()
        self.server.stop()
        self.assertTrue(exited.wait(2.0), "WebSocket handler did not observe server stop")
        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(connection.closed)
        self.assertEqual(self.server.connections, {})

    def test_a_streamed_response_is_sent_as_separate_data_frames(self):
        self._handshake()
        headers = http3.encode_frame(
            http3.FRAME_HEADERS, encode_field_section(self._request_fields("GET", "/sse"))
        )
        self._send(0, headers, fin=True)
        status, bodies, ended = self._replies(0)

        self.assertEqual(status, "200")
        self.assertTrue(ended)
        # One DATA frame per chunk the generator yields, not one joined body.
        self.assertGreaterEqual(len(bodies), 3)
        self.assertEqual(b"".join(bodies), b"event 0\n\nevent 1\n\nevent 2\n\n")

    def test_a_slow_handler_does_not_hold_up_other_requests(self):
        release = threading.Event()
        self.addCleanup(release.set)

        @self.app.api("/slow", methods=("GET",))
        def slow(_request):
            release.wait(5.0)
            return {"slow": True}

        @self.app.api("/quick", methods=("GET",))
        def quick(_request):
            return {"quick": True}

        self._handshake()
        self._send(0, http3.encode_headers_frame(self._request_fields("GET", "/slow")), fin=True)
        self._send(4, http3.encode_headers_frame(self._request_fields("GET", "/quick")), fin=True)
        status, bodies, ended = self._replies(4)
        self.assertEqual(status, "200")
        self.assertTrue(ended)
        self.assertIn(b'"quick":true', b"".join(bodies))

        release.set()
        status, bodies, _ended = self._replies(0)
        self.assertEqual(status, "200")
        self.assertIn(b'"slow":true', b"".join(bodies))

    def test_a_websocket_runs_over_an_extended_connect(self):
        self._handshake()
        connect = http3.encode_frame(
            http3.FRAME_HEADERS,
            encode_field_section(
                [
                    (":method", "CONNECT"),
                    (":protocol", "websocket"),
                    (":scheme", "https"),
                    (":authority", "localhost"),
                    (":path", "/echo"),
                    ("sec-websocket-version", "13"),
                ]
            ),
        )
        self._send(0, connect, fin=False)
        status, _bodies, ended = self._replies(0)
        self.assertEqual(status, "200")
        self.assertFalse(ended)  # the stream stays open for WebSocket frames

        self._send(0, http3.encode_frame(http3.FRAME_DATA, _masked_text_frame("hello")))
        _status, bodies, _ended = self._replies(0)
        self.assertEqual(_unmasked_text(b"".join(bodies)), "echo:hello")

        self._send(0, http3.encode_frame(http3.FRAME_DATA, _masked_text_frame("again")))
        _status, bodies, _ended = self._replies(0)
        self.assertIn(b"echo:again", b"".join(bodies))

    def test_a_websocket_to_an_unknown_route_is_refused(self):
        self._handshake()
        connect = http3.encode_frame(
            http3.FRAME_HEADERS,
            encode_field_section(
                [
                    (":method", "CONNECT"),
                    (":protocol", "websocket"),
                    (":scheme", "https"),
                    (":authority", "localhost"),
                    (":path", "/nope"),
                    ("sec-websocket-version", "13"),
                ]
            ),
        )
        self._send(0, connect, fin=True)
        status, _bodies, ended = self._replies(0)
        self.assertEqual(status, "404")
        self.assertTrue(ended)


if __name__ == "__main__":
    unittest.main()

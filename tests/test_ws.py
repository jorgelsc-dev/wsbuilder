import socket
import time
import unittest

from wsbuilder import App
from wsbuilder.ws import (
    WebSocket,
    WebSocketConnectionClosedError,
    WebSocketFrame,
    WebSocketReadTimeoutError,
    handshake_websocket_with_options,
    make_ws_frame_bytes,
    read_ws_frame_raw,
)


def _masked_client_frame(opcode, payload=b"", mask=b"\x01\x02\x03\x04"):
    payload = bytes(payload or b"")
    fin_opcode = 0x80 | (opcode & 0x0F)
    length = len(payload)
    header = bytes([fin_opcode])
    if length <= 125:
        header += bytes([0x80 | length])
    elif length <= 65535:
        header += bytes([0x80 | 126]) + length.to_bytes(2, "big")
    else:
        header += bytes([0x80 | 127]) + length.to_bytes(8, "big")
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


class DummySocket:
    def __init__(self, *, data=b"", recv_events=None):
        self.buffer = bytearray(data)
        self.recv_events = list(recv_events or [])
        self.sent = []
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def recv(self, n):
        if self.recv_events:
            event = self.recv_events.pop(0)
            if isinstance(event, BaseException):
                raise event
            self.buffer.extend(event)
        if not self.buffer:
            return b""
        chunk = bytes(self.buffer[:n])
        del self.buffer[:n]
        return chunk

    def sendall(self, data):
        self.sent.append(bytes(data))


class DummyHandshakeSocket:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(bytes(data))


class TestWebSocketCore(unittest.TestCase):
    def test_read_ws_frame_raw_returns_named_frame_and_supports_unpacking(self):
        raw = _masked_client_frame(0x1, b"hi")
        sock = DummySocket(data=raw)

        frame = read_ws_frame_raw(sock)
        self.assertIsInstance(frame, WebSocketFrame)
        self.assertEqual(frame.fin, 1)
        self.assertEqual(frame.opcode, 0x1)
        self.assertEqual(frame.payload, b"hi")
        self.assertTrue(frame.masked)
        self.assertEqual(len(frame.mask or b""), 4)

        fin, opcode, payload, masked, mask = frame
        self.assertEqual((fin, opcode, payload, masked), (1, 0x1, b"hi", True))
        self.assertEqual(frame[2], b"hi")
        self.assertEqual(mask, frame.mask)

    def test_recv_frame_distinguishes_timeout_and_connection_close(self):
        timeout_events = []
        timeout_ws = WebSocket(
            DummySocket(recv_events=[socket.timeout("timed out")]),
            ("127.0.0.1", 1234),
            "",
            {},
            on_timeout=lambda _ws, reason: timeout_events.append(reason),
        )

        with self.assertRaises(WebSocketReadTimeoutError):
            timeout_ws.recv_frame()
        self.assertEqual(timeout_events, ["read timeout"])

        close_events = []
        error_ws = WebSocket(
            DummySocket(recv_events=[ConnectionResetError("reset")]),
            ("127.0.0.1", 1234),
            "",
            {},
            on_close=lambda _ws, code, reason: close_events.append((code, reason)),
        )

        with self.assertRaises(WebSocketConnectionClosedError):
            error_ws.recv_frame()
        self.assertEqual(close_events, [(1006, "Connection closed")])

    def test_keepalive_pings_before_next_frame(self):
        pong_frame = _masked_client_frame(0xA, b"")
        sock = DummySocket(data=pong_frame, recv_events=[socket.timeout("idle")])
        ws = WebSocket(
            sock,
            ("127.0.0.1", 1234),
            "",
            {},
            keepalive_interval=0.01,
            pong_timeout=0.05,
            io_poll_interval=0.01,
        )

        time.sleep(0.02)
        frame = ws.recv_frame()
        self.assertEqual(frame.opcode, 0xA)
        self.assertGreaterEqual(len(sock.sent), 1)
        self.assertEqual(sock.sent[0][:2], make_ws_frame_bytes(0x9, b"")[:2])

    def test_handshake_negotiates_supported_subprotocol(self):
        conn = DummyHandshakeSocket()
        headers = {
            "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
            "connection": "Upgrade",
            "upgrade": "websocket",
            "sec-websocket-version": "13",
            "sec-websocket-protocol": "chat, superchat, json",
        }

        ws = handshake_websocket_with_options(
            conn,
            ("127.0.0.1", 1234),
            headers,
            supported_subprotocols=("json", "superchat"),
        )

        self.assertIsNotNone(ws)
        self.assertEqual(ws.subprotocol, "superchat")
        response = b"".join(conn.sent).decode("utf-8", errors="ignore")
        self.assertIn("Sec-WebSocket-Protocol: superchat", response)

    def test_app_ws_can_declare_ws_behaviour(self):
        app = App()

        @app.ws(
            "/ws/",
            subprotocols=("json", "msgpack"),
            idle_timeout=15.0,
            keepalive_interval=5.0,
            pong_timeout=3.0,
            auto_pong=False,
        )
        def handler(_ws, _request):
            return None

        route = app.ws_routes["/ws/"]
        self.assertIs(route["handler"], handler)
        self.assertEqual(route["subprotocols"], ("json", "msgpack"))
        self.assertEqual(route["idle_timeout"], 15.0)
        self.assertEqual(route["keepalive_interval"], 5.0)
        self.assertEqual(route["pong_timeout"], 3.0)
        self.assertFalse(route["auto_pong"])


if __name__ == "__main__":
    unittest.main()

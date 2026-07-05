import unittest

from wsbuilder import App
from wsbuilder.metrics import AppMetrics
from wsbuilder.security import SecurityDecision
from wsbuilder.server import HTTPServer


class DummyConn:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.timeout = None
        self.closed = False

    def recv(self, n):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) <= n:
            return chunk
        self.chunks.insert(0, chunk[n:])
        return chunk[:n]

    def sendall(self, data):
        self.sent.append(bytes(data))

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class AllowAllSecurity:
    def __init__(self):
        self.observed = []

    def evaluate(self, _request):
        return SecurityDecision(allowed=True)

    def observe_response(self, _request, status_code):
        self.observed.append(int(status_code))


class TestHTTPServer(unittest.TestCase):
    def test_invalid_content_length_returns_400(self):
        app = App()
        called = {"value": False}

        @app.api("/echo", methods=("POST",))
        def echo(_request):
            called["value"] = True
            return {"ok": True}

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [
                (
                    b"POST /echo HTTP/1.1\r\n"
                    b"Host: example.test\r\n"
                    b"Content-Length: abc\r\n"
                    b"\r\n"
                    b"hello"
                )
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertFalse(called["value"])
        response = b"".join(conn.sent).decode("utf-8", errors="ignore")
        self.assertIn("HTTP/1.1 400 Bad Request", response)
        self.assertIn("Invalid Content-Length", response)

    def test_ws_handshake_failure_records_real_status(self):
        app = App()
        app.metrics = AppMetrics("test-app")
        app.security = AllowAllSecurity()

        @app.ws("/ws")
        def handler(_ws, _request):
            raise AssertionError("handler must not be called")

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [
                (
                    b"GET /ws HTTP/1.1\r\n"
                    b"Host: example.test\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    b"Sec-WebSocket-Version: 12\r\n"
                    b"\r\n"
                )
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 4321))

        response = b"".join(conn.sent).decode("utf-8", errors="ignore")
        self.assertIn("HTTP/1.1 426 Upgrade Required", response)
        self.assertEqual(app.metrics.http_status.get("426"), 1)
        self.assertEqual(app.security.observed, [426])


if __name__ == "__main__":
    unittest.main()

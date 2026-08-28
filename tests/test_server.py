import socket
import threading
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

    def test_content_length_requires_decimal_digits(self):
        app = App()

        @app.api("/echo", methods=("POST",))
        def echo(_request):
            raise AssertionError("handler must not be called")

        server = HTTPServer("127.0.0.1", 0, app)
        for value in ("+5", "1_0", "5.0"):
            with self.subTest(value=value):
                conn = DummyConn(
                    [
                        (
                            b"POST /echo HTTP/1.1\r\n"
                            b"Host: example.test\r\n"
                            + f"Content-Length: {value}\r\n\r\n".encode("ascii")
                            + b"hello"
                        )
                    ]
                )

                server.handle_conn(conn, ("127.0.0.1", 1234))

                self.assertIn(
                    b"HTTP/1.1 400 Bad Request",
                    b"".join(conn.sent),
                )

    def test_oversized_query_returns_400(self):
        app = App()

        @app.api("/query")
        def query(_request):
            raise AssertionError("handler must not be called")

        raw_query = "&".join(f"k{index}=v" for index in range(1025))
        conn = DummyConn(
            [
                (
                    f"GET /query?{raw_query} HTTP/1.1\r\n"
                    "Host: example.test\r\n\r\n"
                ).encode("ascii")
            ]
        )

        HTTPServer("127.0.0.1", 0, app).handle_conn(
            conn,
            ("127.0.0.1", 1234),
        )

        wire = b"".join(conn.sent)
        self.assertIn(b"HTTP/1.1 400 Bad Request", wire)
        self.assertIn(b"Invalid request target", wire)

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

    def test_rejects_unsupported_or_ambiguous_request_framing(self):
        app = App()
        called = {"count": 0}

        @app.api("/echo", methods=("POST",))
        def echo(_request):
            called["count"] += 1
            return {"ok": True}

        requests = (
            (
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"5\r\nhello\r\n0\r\n\r\n",
                b"HTTP/1.1 501 Not Implemented",
            ),
            (
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Content-Length: 5\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"hello",
                b"HTTP/1.1 400 Bad Request",
            ),
        )

        server = HTTPServer("127.0.0.1", 0, app)
        for raw, expected_status in requests:
            with self.subTest(status=expected_status):
                conn = DummyConn([raw])
                server.handle_conn(conn, ("127.0.0.1", 1234))
                self.assertIn(expected_status, b"".join(conn.sent))
        self.assertEqual(called["count"], 0)

    def test_incomplete_request_body_returns_400(self):
        app = App()

        @app.api("/echo", methods=("POST",))
        def echo(_request):
            raise AssertionError("handler must not be called")

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Content-Length: 5\r\n\r\n"
                b"hi"
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertIn(b"HTTP/1.1 400 Bad Request", b"".join(conn.sent))
        self.assertIn(b"Incomplete Request Body", b"".join(conn.sent))

    def test_head_uses_get_route_without_sending_body(self):
        app = App()

        @app.view("/resource")
        def resource(_request):
            return "payload"

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [b"HEAD /resource HTTP/1.1\r\nHost: example.test\r\n\r\n"]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        headers, body = b"".join(conn.sent).split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 200 OK", headers)
        self.assertIn(b"Content-Length: 7", headers)
        self.assertEqual(body, b"")

    def test_method_not_allowed_and_options_report_all_methods(self):
        app = App()

        @app.view("/resource", methods=("GET",))
        def get_resource(_request):
            return "get"

        @app.view("/resource", methods=("POST",))
        def post_resource(_request):
            return "post"

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [b"DELETE /resource HTTP/1.1\r\nHost: example.test\r\n\r\n"]
        )
        server.handle_conn(conn, ("127.0.0.1", 1234))
        wire = b"".join(conn.sent)
        self.assertIn(b"HTTP/1.1 405 Method Not Allowed", wire)
        self.assertIn(b"Allow: GET, HEAD, OPTIONS, POST", wire)

        options_conn = DummyConn(
            [b"OPTIONS /resource HTTP/1.1\r\nHost: example.test\r\n\r\n"]
        )
        server.handle_conn(options_conn, ("127.0.0.1", 1234))
        options_wire = b"".join(options_conn.sent)
        self.assertIn(
            b"Access-Control-Allow-Methods: GET, HEAD, OPTIONS, POST",
            options_wire,
        )

    def test_websocket_upgrade_requires_get(self):
        app = App()
        called = {"value": False}

        @app.ws("/ws")
        def handler(_ws, _request):
            called["value"] = True

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [
                b"POST /ws HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"Sec-WebSocket-Version: 13\r\n"
                b"Content-Length: 0\r\n\r\n"
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertFalse(called["value"])
        self.assertIn(
            b"HTTP/1.1 405 Method Not Allowed",
            b"".join(conn.sent),
        )


if __name__ == "__main__":
    unittest.main()


class _FlakyListener:
    """Listening socket whose first ``accept`` fails like a real transient error."""

    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.accept_failures = 0

    def accept(self):
        if self.accept_failures == 0:
            self.accept_failures += 1
            raise OSError(24, "Too many open files")
        return self._wrapped.accept()

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


class _FlakyServer(HTTPServer):
    def _create_listening_socket(self):
        self.listener = _FlakyListener(super()._create_listening_socket())
        return self.listener


class TestHTTPServerLifecycle(unittest.TestCase):
    def _serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.assertTrue(server.wait_until_serving(timeout=5.0))
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(server.stop)
        return thread

    def _get(self, address, path="/ping"):
        with socket.create_connection(address, timeout=5.0) as sock:
            sock.sendall(
                f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
            )
            received = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        return received

    def _ping_app(self):
        app = App()

        @app.api("/ping", methods=("GET",))
        def ping(_request):
            return {"pong": True}

        return app

    def test_serve_forever_answers_requests_and_stops_on_demand(self):
        server = HTTPServer("127.0.0.1", 0, self._ping_app())
        thread = self._serve(server)

        received = self._get(server.server_address)
        self.assertIn(b"200 OK", received)
        self.assertIn(b'{"pong":true}', received)

        server.stop()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())

    def test_transient_accept_error_does_not_stop_the_server(self):
        server = _FlakyServer("127.0.0.1", 0, self._ping_app())
        thread = self._serve(server)

        received = self._get(server.server_address)
        self.assertEqual(server.listener.accept_failures, 1)
        self.assertIn(b"200 OK", received)

        server.stop()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())

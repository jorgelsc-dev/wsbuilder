import io
import os
import signal
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch

from wsbuilder import App, Response
from wsbuilder.__main__ import main as cli_main
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

    def test_content_length_with_too_many_digits_returns_413(self):
        app = App()

        @app.api("/echo", methods=("POST",))
        def echo(_request):
            raise AssertionError("handler must not be called")

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                + b"Content-Length: " + b"9" * 5000 + b"\r\n\r\n"
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertIn(b"HTTP/1.1 413 Payload Too Large", b"".join(conn.sent))

    def test_content_length_leading_zeros_do_not_count_toward_the_limit(self):
        app = App()

        @app.api("/echo", methods=("POST",))
        def echo(request):
            return Response.text(request.body.decode())

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Connection: close\r\n"
                + b"Content-Length: " + b"0" * 5000 + b"5\r\n\r\nhello"
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        sent = b"".join(conn.sent)
        self.assertIn(b"HTTP/1.1 200 OK", sent)
        self.assertTrue(sent.endswith(b"hello"))

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
                # chunked is decoded now, but a coding the server cannot apply
                # still has to be refused.
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Transfer-Encoding: gzip, chunked\r\n\r\n"
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

    def test_websocket_limits_resolve_route_then_server_then_default(self):
        app = App()
        seen = {}

        @app.ws("/server-limit")
        def server_limit(ws, _request):
            seen["server"] = (ws.max_frame_bytes, ws.max_message_bytes)

        @app.ws("/route-limit", max_message_bytes=32)
        def route_limit(ws, _request):
            seen["route"] = (ws.max_frame_bytes, ws.max_message_bytes)

        server = HTTPServer("127.0.0.1", 0, app, max_ws_frame_bytes=128, max_ws_message_bytes=256)
        for path in ("/server-limit", "/route-limit"):
            conn = DummyConn(
                [
                    f"GET {path} HTTP/1.1\r\n".encode()
                    + b"Host: example.test\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    b"Sec-WebSocket-Version: 13\r\n\r\n"
                ]
            )
            server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertEqual(seen["server"], (128, 256))
        self.assertEqual(seen["route"], (32, 32))


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

    def _blocking_app(self, release):
        app = self._ping_app()
        app.started = threading.Event()
        app.events = []

        @app.api("/slow", methods=("GET",))
        def slow(_request):
            app.started.set()
            release.wait(5.0)
            app.events.append("handler done")
            return {"slow": True}

        original_close = app.close

        def close():
            app.events.append("app closed")
            original_close()

        app.close = close
        return app

    def test_stop_lets_in_flight_requests_finish_before_closing_the_app(self):
        release = threading.Event()
        app = self._blocking_app(release)
        server = HTTPServer("127.0.0.1", 0, app)
        thread = self._serve(server)

        responses = []
        client = threading.Thread(
            target=lambda: responses.append(self._get(server.server_address, "/slow"))
        )
        client.start()
        self.assertTrue(app.started.wait(5.0))
        self.assertEqual(server.active_connections(), 1)

        server.stop()
        thread.join(0.3)
        self.assertTrue(thread.is_alive(), "shutdown did not wait for the in-flight request")
        release.set()
        client.join(5.0)
        thread.join(5.0)

        self.assertFalse(thread.is_alive())
        self.assertIn(b"200 OK", responses[0])
        self.assertIn(b"Connection: close", responses[0])
        self.assertEqual(app.events, ["handler done", "app closed"])
        self.assertEqual(server.active_connections(), 0)

    def test_draining_gives_up_after_the_shutdown_timeout(self):
        release = threading.Event()
        app = self._blocking_app(release)
        server = HTTPServer("127.0.0.1", 0, app)
        server.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 0.2
        thread = self._serve(server)

        client = threading.Thread(target=self._get, args=(server.server_address, "/slow"))
        client.start()
        # Cleanups run last-in first-out: release the handler, then join.
        self.addCleanup(client.join, 5.0)
        self.addCleanup(release.set)
        self.assertTrue(app.started.wait(5.0))

        server.stop()
        thread.join(3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(app.events, ["app closed"])

    @unittest.skipUnless(hasattr(signal, "SIGTERM") and os.name == "posix", "POSIX signals only")
    def test_sigterm_stops_a_server_running_in_the_main_thread(self):
        server = HTTPServer("127.0.0.1", 0, self._ping_app())
        before = signal.getsignal(signal.SIGTERM)

        def terminate():
            if server.wait_until_serving(timeout=5.0):
                os.kill(os.getpid(), signal.SIGTERM)

        killer = threading.Thread(target=terminate)
        killer.start()
        server.serve_forever()
        killer.join(5.0)

        self.assertTrue(server._stop.is_set())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_serving_from_a_worker_thread_leaves_sigterm_alone(self):
        before = signal.getsignal(signal.SIGTERM)
        server = HTTPServer("127.0.0.1", 0, self._ping_app())
        self._serve(server)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_transient_accept_error_does_not_stop_the_server(self):
        server = _FlakyServer("127.0.0.1", 0, self._ping_app())
        thread = self._serve(server)

        received = self._get(server.server_address)
        self.assertEqual(server.listener.accept_failures, 1)
        self.assertIn(b"200 OK", received)

        server.stop()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())


    def test_silent_client_is_dropped_after_the_read_timeout(self):
        server = HTTPServer("127.0.0.1", 0, self._ping_app(), request_read_timeout=0.2)
        self._serve(server)

        for opening in (b"", b"P"):
            with self.subTest(opening=opening):
                with socket.create_connection(server.server_address, timeout=5.0) as sock:
                    if opening:
                        sock.sendall(opening)
                    received = b""
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        received += chunk
                self.assertNotIn(b"200 OK", received)

    def test_tls_handshake_runs_under_the_read_timeout(self):
        seen = []

        class RecordingContext:
            def wrap_socket(self, conn, server_side):
                seen.append(conn.gettimeout())
                raise ssl.SSLError("stop after recording")

        server = HTTPServer(
            "127.0.0.1", 0, self._ping_app(), ssl_context=RecordingContext(), request_read_timeout=0.2
        )
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        left.setblocking(True)
        server.handle_conn(left, ("127.0.0.1", 0))

        self.assertEqual(seen, [0.2])

    def test_idle_keepalive_connection_closes_after_the_keepalive_timeout(self):
        server = HTTPServer(
            "127.0.0.1", 0, self._ping_app(), request_read_timeout=5.0, keepalive_timeout=0.2
        )
        self._serve(server)

        with socket.create_connection(server.server_address, timeout=3.0) as sock:
            sock.sendall(b"GET /ping HTTP/1.1\r\nHost: localhost\r\n\r\n")
            started = time.monotonic()
            received = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
            elapsed = time.monotonic() - started

        self.assertEqual(received.count(b"HTTP/1.1 "), 1)
        self.assertIn(b"200 OK", received)
        self.assertNotIn(b"408", received)
        self.assertLess(elapsed, 2.0)


class TestHTTPServerLimits(unittest.TestCase):
    def test_constructor_limits_override_only_this_instance(self):
        server = HTTPServer(
            "127.0.0.1",
            0,
            App(),
            max_connection_workers=8,
            keepalive_timeout=1.5,
            max_ws_message_bytes=1024,
            request_read_timeout=None,
        )

        self.assertEqual(server.MAX_CONNECTION_WORKERS, 8)
        self.assertEqual(server.KEEPALIVE_TIMEOUT_SECONDS, 1.5)
        self.assertEqual(server.MAX_WS_MESSAGE_BYTES, 1024)
        self.assertEqual(server.REQUEST_READ_TIMEOUT_SECONDS, HTTPServer.REQUEST_READ_TIMEOUT_SECONDS)
        self.assertEqual(HTTPServer.MAX_CONNECTION_WORKERS, 64)

    def test_invalid_limits_are_rejected(self):
        cases = (
            ({"max_conections": 4}, TypeError),
            ({"max_connection_workers": True}, TypeError),
            ({"max_connection_workers": 0}, ValueError),
            ({"keepalive_timeout": 0}, ValueError),
            ({"max_request_body_bytes": -1}, ValueError),
            ({"max_request_body_bytes": "big"}, ValueError),
        )
        for limits, error in cases:
            with self.subTest(limits=limits), self.assertRaises(error):
                HTTPServer("127.0.0.1", 0, App(), **limits)

    def test_app_run_forwards_server_limits(self):
        app = App()
        captured = []

        def fake_serve_forever(server):
            captured.append(server)

        with patch.object(HTTPServer, "serve_forever", fake_serve_forever):
            app.run("127.0.0.1", 0, max_request_body_bytes=1024)

        self.assertEqual(captured[0].MAX_REQUEST_BODY_BYTES, 1024)

    def test_cli_flags_become_server_limits(self):
        with (
            patch("sys.argv", ["wsbuilder", "--port", "0", "--max-workers", "4", "--shutdown-timeout", "2.5"]),
            patch.object(App, "run") as run,
        ):
            cli_main()

        _args, kwargs = run.call_args
        self.assertEqual(kwargs["max_connection_workers"], 4)
        self.assertEqual(kwargs["graceful_shutdown_timeout"], 2.5)
        self.assertIsNone(kwargs["max_request_body_bytes"])

    def test_cli_reports_invalid_limits_by_flag(self):
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["wsbuilder", "--port", "0", "--max-workers", "0"]),
            patch("sys.stderr", stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli_main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--max-workers must be at least 1", stderr.getvalue())


class TestChunkedRequests(unittest.TestCase):
    def _app(self, seen):
        app = App()

        @app.api("/echo", methods=("POST",))
        def echo(request):
            seen.append(request)
            return {"body": request.body.decode(), "trailers": request.trailers}

        return app

    def test_chunked_body_reaches_the_handler_reassembled(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"5\r\nhello\r\n",
                b"6\r\n world\r\n0\r\n\r\n",
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        sent = b"".join(conn.sent)
        self.assertIn(b"HTTP/1.1 200 OK", sent)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].body, b"hello world")
        self.assertEqual(seen[0].version, "HTTP/1.1")

    def test_trailers_are_exposed_on_the_request(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"4\r\nbody\r\n0\r\nX-Checksum: abc\r\n\r\n"
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertEqual(seen[0].body, b"body")
        self.assertEqual(seen[0].trailers, {"x-checksum": "abc"})

    def test_expect_100_continue_works_without_content_length(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Expect: 100-continue\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n",
                b"2\r\nhi\r\n0\r\n\r\n",
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        sent = b"".join(conn.sent)
        self.assertIn(b"HTTP/1.1 100 Continue", sent)
        self.assertIn(b"HTTP/1.1 200 OK", sent)
        self.assertEqual(seen[0].body, b"hi")

    def test_chunked_body_over_the_limit_returns_413(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        server.MAX_REQUEST_BODY_BYTES = 8
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"10\r\n" + b"x" * 16 + b"\r\n0\r\n\r\n"
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertIn(b"HTTP/1.1 413 Payload Too Large", b"".join(conn.sent))
        self.assertEqual(seen, [])

    def test_limits_can_be_passed_to_the_constructor(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen), max_request_body_bytes=8)
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Content-Length: 16\r\n\r\n" + b"x" * 16
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertIn(b"HTTP/1.1 413 Payload Too Large", b"".join(conn.sent))
        self.assertEqual(seen, [])

    def test_truncated_chunked_body_returns_400(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"9\r\nshort"
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertIn(b"HTTP/1.1 400 Bad Request", b"".join(conn.sent))
        self.assertEqual(seen, [])


class TestPersistentConnections(unittest.TestCase):
    def _app(self, seen):
        app = App()

        @app.api("/n", methods=("GET",))
        def counter(request):
            seen.append(request.path)
            return {"n": len(seen)}

        return app

    @staticmethod
    def _get(path="/n", version="HTTP/1.1", extra=b""):
        return (
            f"GET {path} {version}\r\nHost: example.test\r\n".encode() + extra + b"\r\n"
        )

    def test_two_requests_share_one_connection(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        conn = DummyConn([self._get(), self._get()])

        server.handle_conn(conn, ("127.0.0.1", 1234))

        sent = b"".join(conn.sent)
        self.assertEqual(seen, ["/n", "/n"])
        self.assertEqual(sent.count(b"HTTP/1.1 200 OK"), 2)
        self.assertNotIn(b"Connection: close", sent)

    def test_pipelined_requests_arriving_together_are_both_served(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        # Both request lines land in a single read, so the second one is only
        # served if the leftover bytes survive the first response.
        conn = DummyConn([self._get() + self._get()])

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertEqual(seen, ["/n", "/n"])

    def test_bodies_do_not_bleed_into_the_next_request(self):
        seen = []
        app = App()

        @app.api("/echo", methods=("POST",))
        def echo(request):
            seen.append(request.body)
            return {"ok": True}

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn(
            [
                b"POST /echo HTTP/1.1\r\nHost: t\r\nContent-Length: 5\r\n\r\nhello"
                b"POST /echo HTTP/1.1\r\nHost: t\r\nContent-Length: 5\r\n\r\nworld"
            ]
        )

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertEqual(seen, [b"hello", b"world"])

    def test_connection_close_ends_the_conversation(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        conn = DummyConn([self._get(extra=b"Connection: close\r\n"), self._get()])

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertEqual(seen, ["/n"])
        self.assertIn(b"Connection: close", b"".join(conn.sent))

    def test_http_1_0_closes_unless_it_asks_to_stay(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        conn = DummyConn([self._get(version="HTTP/1.0"), self._get()])
        server.handle_conn(conn, ("127.0.0.1", 1234))
        self.assertEqual(seen, ["/n"])

        seen.clear()
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        conn = DummyConn(
            [
                self._get(version="HTTP/1.0", extra=b"Connection: keep-alive\r\n"),
                self._get(version="HTTP/1.0", extra=b"Connection: keep-alive\r\n"),
            ]
        )
        server.handle_conn(conn, ("127.0.0.1", 1234))
        self.assertEqual(seen, ["/n", "/n"])
        self.assertIn(b"Connection: keep-alive", b"".join(conn.sent))

    def test_max_requests_per_connection_is_enforced(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        server.MAX_KEEPALIVE_REQUESTS = 2
        conn = DummyConn([self._get(), self._get(), self._get()])

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertEqual(len(seen), 2)

    def test_reuse_can_be_turned_off_entirely(self):
        seen = []
        server = HTTPServer("127.0.0.1", 0, self._app(seen))
        server.MAX_KEEPALIVE_REQUESTS = 1
        conn = DummyConn([self._get(), self._get()])

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertEqual(seen, ["/n"])
        self.assertIn(b"Connection: close", b"".join(conn.sent))

    def test_an_unframed_streamed_response_closes_the_connection(self):
        app = App()

        @app.route("/stream", methods=("GET",))
        def stream(_request):
            response = Response.stream(iter([b"a", b"b"]))
            response.headers.pop("Transfer-Encoding", None)
            response.headers["Content-Type"] = "text/plain"
            return response

        server = HTTPServer("127.0.0.1", 0, app)
        conn = DummyConn([self._get("/stream"), self._get("/stream")])

        server.handle_conn(conn, ("127.0.0.1", 1234))

        self.assertEqual(b"".join(conn.sent).count(b"HTTP/1.1 200 OK"), 1)

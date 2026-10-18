"""Failure paths of HTTPServer: malformed input, timeouts, TLS and shutdown."""

import io
import socket
import ssl
import threading
import unittest
from unittest.mock import patch

from wsbuilder import App, Response
from wsbuilder.logs import console
from wsbuilder.metrics import AppMetrics
from wsbuilder.security import SecurityDecision
from wsbuilder.server import HTTPServer

WS_HEADERS = (
    b"Host: example.test\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"Sec-WebSocket-Version: 13\r\n"
)


class ScriptedConn:
    """A socket whose reads follow a script of byte chunks and exceptions."""

    def __init__(self, script, *, fail_send=None):
        self.script = list(script)
        self.sent = []
        self.closed = False
        self.timeouts = []
        self.fail_send = fail_send

    def recv(self, n):
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if len(item) > n:
            self.script.insert(0, item[n:])
            item = item[:n]
        return item

    def sendall(self, data):
        if self.fail_send is not None and self.fail_send(bytes(data)):
            raise BrokenPipeError("peer went away")
        self.sent.append(bytes(data))

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    @property
    def wire(self):
        return b"".join(self.sent)


class QuietConsole(unittest.TestCase):
    """Capture framework log lines instead of printing them."""

    def setUp(self):
        self.log = io.StringIO()
        patcher = patch.object(console, "stream", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _app(self):
        app = App()
        self.addCleanup(app.close)
        app.metrics = AppMetrics("errors-test")
        app.calls = []

        @app.api("/echo", methods=("GET", "POST"))
        def echo(request):
            app.calls.append(request.body)
            return {"ok": True}

        return app

    def _serve(self, app, script, **kwargs):
        server = HTTPServer("127.0.0.1", 0, app, **kwargs)
        conn = ScriptedConn(script)
        server.handle_conn(conn, ("127.0.0.1", 1234))
        return conn


class TestMalformedRequests(QuietConsole):
    def test_status_codes_for_broken_request_heads(self):
        cases = (
            ("slow headers", [b"GET /echo HTTP/1.1\r\n", TimeoutError()], {}, b"408 Request Timeout"),
            (
                "oversized headers",
                [b"GET /echo HTTP/1.1\r\nX-Big: " + b"a" * 200 + b"\r\n\r\n"],
                {"max_request_header_bytes": 64},
                b"431 ",
            ),
            ("unknown version", [b"GET /echo HTTP/7.0\r\nHost: x\r\n\r\n"], {}, b"505 "),
            ("garbage line", [b"NONSENSE\r\n\r\n"], {}, b"400 Bad Request"),
        )
        for name, script, limits, status in cases:
            with self.subTest(name):
                app = self._app()
                conn = self._serve(app, script, **limits)
                self.assertIn(status, conn.wire)
                self.assertEqual(app.calls, [])

    def test_expectations_other_than_100_continue_are_refused(self):
        app = self._app()
        conn = self._serve(
            app,
            [b"POST /echo HTTP/1.1\r\nHost: x\r\nExpect: magic\r\nContent-Length: 2\r\n\r\nhi"],
        )
        self.assertIn(b"417 Expectation Failed", conn.wire)

    def test_100_continue_needs_a_declared_body(self):
        app = self._app()
        conn = self._serve(app, [b"POST /echo HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n\r\n"])
        self.assertIn(b"100-continue requires Content-Length", conn.wire)
        self.assertNotIn(b"100 Continue\r\n", conn.wire)

    def test_100_continue_stops_quietly_when_the_client_is_gone(self):
        for framing in (b"Content-Length: 2\r\n", b"Transfer-Encoding: chunked\r\n"):
            with self.subTest(framing=framing):
                app = self._app()
                server = HTTPServer("127.0.0.1", 0, app)
                conn = ScriptedConn(
                    [b"POST /echo HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n" + framing + b"\r\n"],
                    fail_send=lambda data: data.startswith(b"HTTP/1.1 100"),
                )
                server.handle_conn(conn, ("127.0.0.1", 1234))
                self.assertEqual(conn.sent, [])
                self.assertEqual(app.calls, [])

    def test_bodies_that_stall_or_stop_short(self):
        head = b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\n"
        chunked = b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        cases = (
            ("fixed length timeout", [head, b"abc", TimeoutError()], b"408 Request Timeout"),
            ("fixed length cut off", [head, b"abc"], b"Incomplete Request Body"),
            ("chunked timeout", [chunked, b"5\r\nab", TimeoutError()], b"408 Request Timeout"),
            ("chunked cut off", [chunked, b"5\r\nab"], b"Incomplete Request Body"),
            ("bad chunk size", [chunked, b"zz\r\nab\r\n0\r\n\r\n"], b"400 Bad Request"),
        )
        for name, script, status in cases:
            with self.subTest(name):
                app = self._app()
                conn = self._serve(app, script)
                self.assertIn(status, conn.wire)
                self.assertEqual(app.calls, [])


class TestDispatchFailures(QuietConsole):
    def test_failed_chunked_stream_does_not_send_the_next_pipelined_response(self):
        app = self._app()
        closed = []

        def chunks():
            try:
                yield b"first"
                raise ValueError("producer failed")
            finally:
                closed.append(True)

        @app.view("/broken")
        def broken(_request):
            return Response.stream(chunks(), headers={"Transfer-Encoding": "chunked"})

        conn = self._serve(app, [
            b"GET /broken HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /echo HTTP/1.1\r\nHost: x\r\n\r\n"
        ])
        self.assertEqual(conn.wire.count(b"HTTP/1.1 200"), 1)
        self.assertIn(b"5\r\nfirst\r\n", conn.wire)
        self.assertEqual(app.calls, [])
        self.assertEqual(closed, [True])
        self.assertTrue(conn.closed)
        self.assertEqual(app.metrics.active_http_requests, 0)
        self.assertEqual(app.metrics.total_http_responses, 0)

    def test_failed_buffered_write_does_not_dispatch_the_next_request(self):
        app = self._app()
        conn = ScriptedConn([
            b"GET /echo HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /echo HTTP/1.1\r\nHost: x\r\n\r\n"
        ], fail_send=lambda data: data.startswith(b'{"ok"'))
        HTTPServer("127.0.0.1", 0, app).handle_conn(conn, ("127.0.0.1", 1))
        self.assertEqual(len(app.calls), 1)
        self.assertTrue(conn.closed)
        self.assertIn("http_response", app.metrics.last_error)
        self.assertEqual(app.metrics.active_http_requests, 0)
        self.assertEqual(app.metrics.total_http_responses, 0)

    def test_a_failing_dispatch_returns_500_and_is_counted(self):
        app = self._app()

        def explode(_request):
            raise RuntimeError("router broke")

        app.dispatch = explode
        conn = self._serve(app, [b"GET /echo HTTP/1.1\r\nHost: x\r\n\r\n"])

        self.assertIn(b"500 Internal Server Error", conn.wire)
        self.assertEqual(app.metrics.total_errors, 1)
        self.assertIn("http_dispatch: router broke", app.metrics.last_error)

    def test_an_unsendable_response_falls_back_to_500(self):
        app = self._app()

        @app.view("/bad-header")
        def bad_header(_request):
            response = Response.text("x")
            # Set after construction so only the wire encoder can catch it.
            response.headers["X-Split"] = "a\r\nInjected: yes"
            return response

        conn = self._serve(app, [b"GET /bad-header HTTP/1.1\r\nHost: x\r\n\r\n"])

        self.assertIn(b"500 Internal Server Error", conn.wire)
        self.assertNotIn(b"Injected: yes", conn.wire)
        self.assertIn("http_response", app.metrics.last_error)

    def test_keep_alive_stops_at_the_request_ceiling(self):
        app = self._app()
        request = b"GET /echo HTTP/1.1\r\nHost: x\r\n\r\n"
        conn = self._serve(app, [request * 3], max_keepalive_requests=2)
        self.assertEqual(len(app.calls), 2)
        self.assertEqual(conn.wire.count(b"200 OK"), 2)


class DenyAll:
    def __init__(self):
        self.observed = []

    def evaluate(self, _request):
        return SecurityDecision(allowed=False, status=403, reason="acl_deny", message="Forbidden")

    def observe_response(self, _request, status):
        self.observed.append(int(status))


class TestWebSocketFailures(QuietConsole):
    def _upgrade(self, app, path, headers=WS_HEADERS):
        return self._serve(app, [f"GET {path} HTTP/1.1\r\n".encode() + headers + b"\r\n"])

    def test_an_unknown_websocket_route_is_404(self):
        app = self._app()
        conn = self._upgrade(app, "/nowhere")
        self.assertIn(b"404 Not Found", conn.wire)
        self.assertEqual(app.metrics.http_status.get("404"), 1)

    def test_security_can_refuse_an_upgrade(self):
        app = self._app()
        app.security = DenyAll()
        opened = []

        @app.ws("/ws")
        def handler(_ws, _request):
            opened.append(True)

        conn = self._upgrade(app, "/ws")
        self.assertIn(b"403 Forbidden", conn.wire)
        self.assertEqual(opened, [])
        self.assertEqual(app.security.observed, [403])

    def test_a_handshake_without_a_key_is_rejected(self):
        app = self._app()

        @app.ws("/ws")
        def handler(_ws, _request):
            raise AssertionError("must not run")

        headers = WS_HEADERS.replace(b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n", b"")
        conn = self._upgrade(app, "/ws", headers)
        self.assertIn(b"400 Bad Request", conn.wire)
        self.assertNotIn(b"101 Switching Protocols", conn.wire)

    def test_a_crashing_handler_is_logged_and_the_socket_released(self):
        app = self._app()

        @app.ws("/ws")
        def handler(_ws, _request):
            raise RuntimeError("handler crashed")

        conn = self._upgrade(app, "/ws")

        self.assertIn(b"101 Switching Protocols", conn.wire)
        self.assertTrue(conn.closed)
        self.assertIn("ws_handler: handler crashed", app.metrics.last_error)
        self.assertIn("[ws] handler error: handler crashed\nTraceback", self.log.getvalue())
        self.assertEqual(app.metrics.active_ws_connections, 0)


class ExplodingContext:
    def __init__(self, error):
        self.error = error

    def wrap_socket(self, _conn, server_side):
        raise self.error


class TestTLSFailures(QuietConsole):
    def test_handshake_and_wrap_errors_close_the_connection(self):
        for error, line in (
            (ssl.SSLError("bad handshake"), "[tls] handshake error"),
            (OSError("reset during wrap"), "[tls] wrap error"),
        ):
            with self.subTest(error=type(error).__name__):
                app = self._app()
                server = HTTPServer("127.0.0.1", 0, app, ssl_context=ExplodingContext(error))
                conn = ScriptedConn([])
                server.handle_conn(conn, ("127.0.0.1", 1234))
                self.assertTrue(conn.closed)
                self.assertEqual(conn.sent, [])
                self.assertIn(line, self.log.getvalue())

    def test_a_context_that_cannot_be_resolved_is_counted(self):
        app = self._app()

        def provider():
            raise RuntimeError("authority expired")

        server = HTTPServer("127.0.0.1", 0, app, ssl_context=provider)
        conn = ScriptedConn([])
        server.handle_conn(conn, ("127.0.0.1", 1234))
        self.assertTrue(conn.closed)
        self.assertIn("tls_context: authority expired", app.metrics.last_error)

    def test_an_alpn_probe_that_fails_falls_back_to_http1(self):
        app = self._app()
        server = HTTPServer("127.0.0.1", 0, app)
        conn = ScriptedConn([b"GET /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"])

        def broken_alpn():
            raise ssl.SSLError("no ALPN state")

        conn.selected_alpn_protocol = broken_alpn
        server.handle_conn(conn, ("127.0.0.1", 1234))
        self.assertIn(b"200 OK", conn.wire)

    def test_a_crashing_http2_connection_is_logged(self):
        app = self._app()
        server = HTTPServer("127.0.0.1", 0, app)
        conn = ScriptedConn([b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"])
        with patch("wsbuilder.http2.Http2Connection.serve", side_effect=RuntimeError("h2 broke")):
            server.handle_conn(conn, ("127.0.0.1", 1234))
        self.assertIn("[http2] connection error", self.log.getvalue())
        self.assertIn("http2: h2 broke", app.metrics.last_error)


class TestServeForeverFailures(QuietConsole):
    def _run(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(server.stop)
        self.assertTrue(server.wait_until_serving(timeout=5.0))
        return thread

    def _get(self, address):
        with socket.create_connection(address, timeout=5.0) as sock:
            sock.sendall(b"GET /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            received = b""
            while chunk := sock.recv(4096):
                received += chunk
        return received

    def test_failing_startup_hooks_and_close_do_not_stop_the_server(self):
        app = self._app()

        def broken_hook():
            raise RuntimeError("hook failed")

        def broken_close():
            raise RuntimeError("close failed")

        app.add_startup(broken_hook)
        app.close = broken_close
        server = HTTPServer("127.0.0.1", 0, app)
        thread = self._run(server)

        self.assertIn(b"200 OK", self._get(server.server_address))
        server.stop()
        thread.join(5.0)

        self.assertFalse(thread.is_alive())
        output = self.log.getvalue()
        self.assertIn("[startup] error: hook failed", output)
        self.assertIn("[shutdown] app.close() error: close failed", output)

    def test_connections_beyond_the_worker_limit_are_closed(self):
        app = self._app()
        release = threading.Event()
        started = threading.Event()

        @app.api("/hold", methods=("GET",))
        def hold(_request):
            started.set()
            release.wait(5.0)
            return {"held": True}

        server = HTTPServer("127.0.0.1", 0, app, max_connection_workers=1)
        server.ACQUIRE_WORKER_TIMEOUT_SECONDS = 0.05
        self._run(server)

        holder = socket.create_connection(server.server_address, timeout=5.0)
        self.addCleanup(holder.close)
        holder.sendall(b"GET /hold HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertTrue(started.wait(5.0))

        with socket.create_connection(server.server_address, timeout=5.0) as extra:
            extra.sendall(b"GET /echo HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                refused = extra.recv(4096)
            except ConnectionResetError:
                refused = b""
            self.assertEqual(refused, b"")
        release.set()
        self.assertIn(b"200 OK", holder.recv(4096))

    def test_ctrl_c_stops_the_loop_and_closes_the_app(self):
        app = self._app()
        closed = []
        app.close = lambda: closed.append(True)
        server = HTTPServer("127.0.0.1", 0, app)

        class InterruptingListener:
            def __init__(self, real):
                self.real = real

            def accept(self):
                raise KeyboardInterrupt

            def __getattr__(self, name):
                return getattr(self.real, name)

        original = server._create_listening_socket
        server._create_listening_socket = lambda: InterruptingListener(original())
        server.serve_forever()

        self.assertEqual(closed, [True])
        self.assertIn("[shutdown] server stopped.", self.log.getvalue())

    def test_connections_still_open_after_the_timeout_are_reported(self):
        server = HTTPServer("127.0.0.1", 0, self._app(), graceful_shutdown_timeout=0)
        stuck = threading.Thread(target=threading.Event().wait, args=(0.5,), daemon=True)
        stuck.start()
        server._workers.add(stuck)
        self.assertEqual(server._drain_workers(), 1)


if __name__ == "__main__":
    unittest.main()

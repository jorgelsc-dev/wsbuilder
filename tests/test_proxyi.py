import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wsbuilder import ProxyI, ProxyTarget, Request
from wsbuilder.proxyi import (
    BALANCING_BEST,
    BALANCING_IP_HASH,
    BALANCING_LEAST_RESPONSE_TIME,
    BALANCING_ROUND_ROBIN,
    _clean_response_headers,
    _has_dot_segments,
)


class _BackendHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {
            "path": self.path,
            "host": self.headers.get("Host", ""),
            "x-env": self.headers.get("x-env", ""),
            "forwarded": self.headers.get("forwarded", ""),
            "x-forwarded-for": self.headers.get("x-forwarded-for", ""),
            "x-forwarded-port": self.headers.get("x-forwarded-port", ""),
            "x-hop": self.headers.get("x-hop", ""),
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        _ = format
        _ = args


class TestProxyI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), _BackendHandler)
        cls.backend_thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.backend_thread.start()
        cls.backend_port = cls.backend.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.backend.shutdown()
        cls.backend.server_close()
        cls.backend_thread.join(timeout=1.0)

    def _request(
        self,
        path="/api/users",
        *,
        host="api.test.local",
        headers=None,
        body=b"",
        method="GET",
        client=("127.0.0.1", 12345),
    ):
        request_headers = {"host": host}
        if headers:
            request_headers.update(headers)
        return Request(
            method=method,
            path=path,
            query_string="debug=1",
            headers=request_headers,
            body=body,
            client=client,
            tls={},
        )

    def test_vhost_location_and_header_contains_match(self):
        proxy = ProxyI(name="edge")
        rule = (
            proxy.vhost("api.test.local", name="api-vhost")
            .location("/api")
            .header("x-role", contains="admin")
            .upstream(f"http://127.0.0.1:{self.backend_port}")
            .build()
        )

        request = self._request(headers={"x-role": "superadmin"})
        self.assertTrue(rule.matches(request))
        resolved = proxy.routes()[0]
        self.assertEqual(resolved.name, "api-vhost")
        self.assertEqual(proxy._resolve_rules(request)[0].name, "api-vhost")

    def test_round_robin_and_least_response_time_balance_modes(self):
        proxy = ProxyI(name="edge")
        rule = (
            proxy.route(name="lb", path_prefix="/", balance=BALANCING_ROUND_ROBIN)
            .upstream("http://127.0.0.1:9001", name="slow")
            .upstream("http://127.0.0.1:9002", name="fast")
            .build()
        )
        request = self._request(path="/anything")

        first = rule.choose_target(request)
        second = rule.choose_target(request)
        self.assertNotEqual(first.name, second.name)

        slow, fast = rule.targets
        slow.metrics.begin_request(32)
        slow.metrics.finish_request(200, response_size=128, latency_ms=120.0)
        fast.metrics.begin_request(32)
        fast.metrics.finish_request(200, response_size=128, latency_ms=4.5)

        rule.balance = BALANCING_LEAST_RESPONSE_TIME
        self.assertIs(rule.choose_target(request), fast)

        rule.balance = BALANCING_BEST
        self.assertIs(rule.choose_target(request), fast)

    def test_dispatch_proxies_request_and_records_metrics(self):
        proxy = ProxyI(name="edge")
        proxy.vhost("api.test.local", name="api-vhost").location("/api").header("x-env", equals="prod").upstream(
            f"http://127.0.0.1:{self.backend_port}",
            name="backend-1",
        ).build()

        request = self._request(headers={"x-env": "prod"})
        response = proxy.dispatch(request)
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["path"], "/api/users?debug=1")
        self.assertEqual(payload["host"], "api.test.local")
        self.assertEqual(payload["x-env"], "prod")

        snapshot = proxy.snapshot()
        self.assertEqual(snapshot["summary"]["requests_total"], 1)
        self.assertEqual(snapshot["summary"]["responses_total"], 1)
        self.assertEqual(snapshot["rules_total"], 1)
        self.assertEqual(snapshot["targets_total"], 1)
        self.assertEqual(snapshot["rules"][0]["metrics"]["requests_total"], 1)
        self.assertEqual(snapshot["targets"][0]["metrics"]["requests_total"], 1)
        self.assertEqual(snapshot["targets"][0]["metrics"]["latency_ms"]["count"], 1)
        self.assertIn("ProxyI metrics area", proxy.response_dashboard().body.decode("utf-8"))

    def test_https_targets_verify_tls_and_snapshot_redacts_sensitive_headers(self):
        target = ProxyTarget(
            "https://upstream.test",
            extra_headers={
                "Authorization": "Bearer top-secret",
                "X-API-Key": "api-secret",
                "X-Trace": "visible",
            },
        )

        snapshot = target.snapshot(include_metrics=False)

        self.assertTrue(target.verify_tls)
        self.assertEqual(snapshot["extra_headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(snapshot["extra_headers"]["X-API-Key"], "[REDACTED]")
        self.assertEqual(snapshot["extra_headers"]["X-Trace"], "visible")
        self.assertEqual(target.extra_headers["Authorization"], "Bearer top-secret")

    def test_rule_snapshot_redacts_sensitive_match_values(self):
        proxy = ProxyI(name="edge")
        rule = (
            proxy.route(
                name="private",
                path_prefix="/",
                header_equals={
                    "Authorization": "Bearer top-secret",
                    "X-Trace": "visible",
                },
                header_contains={"X-API-Key": "secret-fragment"},
                header_regex={"X-Auth-Token": "^private-token$"},
            )
            .upstream("http://127.0.0.1:9001")
            .build()
        )

        snapshot = rule.snapshot(include_metrics=False)

        self.assertEqual(
            snapshot["header_equals"]["authorization"],
            "[REDACTED]",
        )
        self.assertEqual(snapshot["header_equals"]["x-trace"], "visible")
        self.assertEqual(
            snapshot["header_contains"]["x-api-key"],
            "[REDACTED]",
        )
        self.assertEqual(
            snapshot["header_regex"]["x-auth-token"],
            "[REDACTED]",
        )

    def test_disabled_targets_are_not_reused_when_none_are_enabled(self):
        proxy = ProxyI(name="edge")
        rule = (
            proxy.route(name="disabled", path_prefix="/")
            .upstream("http://127.0.0.1:9001", enabled=False)
            .build()
        )
        request = self._request(path="/anything")

        self.assertIsNone(rule.choose_target(request))
        self.assertEqual(proxy.dispatch(request).status, 502)

    def test_a_rule_with_only_disabled_targets_falls_back_to_the_default_rule(self):
        proxy = ProxyI(name="edge")
        proxy.default({"url": f"http://127.0.0.1:{self.backend_port}", "name": "fallback"})
        rule = (
            proxy.route(name="api", path_prefix="/api")
            .upstream("http://127.0.0.1:9001", enabled=False)
            .build()
        )
        request = self._request(path="/api/users")

        self.assertEqual(rule.choose_target(request, proxy=proxy).name, "fallback")
        self.assertIsNone(rule.choose_target(request))
        response = proxy.dispatch(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-ProxyI-Target"], "fallback")

    def test_snapshot_does_not_advance_round_robin(self):
        proxy = ProxyI(name="edge")
        rule = (
            proxy.route(name="rr", path_prefix="/")
            .upstream("http://127.0.0.1:9001", name="one")
            .upstream("http://127.0.0.1:9002", name="two")
            .build()
        )

        self.assertEqual(rule._rr_index, 0)
        proxy.snapshot()
        self.assertEqual(rule._rr_index, 0)
        selected = rule.choose_target(self._request(path="/anything"))
        self.assertEqual(selected.name, "one")

    def test_path_prefix_requires_segment_boundary_for_matching_and_stripping(self):
        proxy = ProxyI(name="edge")
        rule = (
            proxy.route(name="api", path_prefix="/api", strip_prefix=True)
            .upstream("http://127.0.0.1:9001")
            .build()
        )
        target = rule.targets[0]

        self.assertTrue(rule.matches(self._request(path="/api/users")))
        self.assertFalse(rule.matches(self._request(path="/apix")))
        self.assertEqual(target.build_forward_path("/api/users", strip_prefix="/api"), "/users")
        self.assertEqual(target.build_forward_path("/apix", strip_prefix="/api"), "/apix")

    def test_ip_hash_uses_client_ip_instead_of_host(self):
        proxy = ProxyI(name="edge")
        rule = (
            proxy.route(name="ip", path_prefix="/", balance=BALANCING_IP_HASH)
            .upstream("http://127.0.0.1:9001")
            .build()
        )
        observed_keys = []
        original = rule._consistent_hash

        def capture_key(targets, key):
            observed_keys.append(key)
            return targets[0]

        rule._consistent_hash = capture_key
        try:
            rule.choose_target(self._request(client=("192.0.2.10", 1000)))
            rule.choose_target(self._request(client=("198.51.100.20", 2000)))
        finally:
            rule._consistent_hash = original

        self.assertEqual(observed_keys, ["192.0.2.10", "198.51.100.20"])

    def test_default_balance_and_rule_preserve_host_are_applied(self):
        proxy = ProxyI(name="edge", default_balance=BALANCING_LEAST_RESPONSE_TIME)
        rule = (
            proxy.route(name="configured", path_prefix="/", preserve_host=False)
            .upstream(f"http://127.0.0.1:{self.backend_port}")
            .build()
        )

        response = proxy.dispatch(self._request(path="/host"))
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(rule.balance, BALANCING_LEAST_RESPONSE_TIME)
        self.assertEqual(payload["host"], f"127.0.0.1:{self.backend_port}")

    def test_forwarded_port_uses_the_public_host_not_the_upstream(self):
        proxy = ProxyI(name="edge")
        (
            proxy.route(name="public-port", path_prefix="/")
            .upstream(f"http://127.0.0.1:{self.backend_port}")
            .build()
        )

        response = proxy.dispatch(
            self._request(path="/port", host="api.test.local:8443")
        )
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(payload["x-forwarded-port"], "8443")

    def test_forwarding_headers_cannot_be_spoofed_by_the_client(self):
        proxy = ProxyI(name="edge")
        (
            proxy.route(name="sanitized", path_prefix="/")
            .upstream(f"http://127.0.0.1:{self.backend_port}")
            .build()
        )
        request = self._request(
            path="/headers",
            client=("198.51.100.20", 1234),
            headers={
                "Forwarded": "for=192.0.2.99",
                "X-Forwarded-For": "192.0.2.99",
                "Connection": "X-Hop",
                "X-Hop": "client-controlled",
            },
        )

        payload = json.loads(proxy.dispatch(request).body.decode("utf-8"))

        self.assertEqual(payload["forwarded"], "")
        self.assertEqual(payload["x-forwarded-for"], "198.51.100.20")
        self.assertEqual(payload["x-hop"], "")

    def test_response_connection_tokens_and_trailer_are_removed(self):
        cleaned = _clean_response_headers(
            {
                "Connection": "X-Internal",
                "X-Internal": "secret",
                "Trailer": "X-Checksum",
                "Content-Type": "text/plain",
            }
        )

        self.assertEqual(cleaned, {"Content-Type": "text/plain"})

    def test_programmatic_dispatch_enforces_body_limit(self):
        proxy = ProxyI(name="edge", max_request_body_bytes=3)
        (
            proxy.route(name="limited", path_prefix="/")
            .upstream("http://127.0.0.1:1")
            .build()
        )

        response = proxy.dispatch(
            self._request(path="/upload", method="POST", body=b"four")
        )

        self.assertEqual(response.status, 413)


class _RawUpstream:
    """One-shot upstream that answers every connection with ``script(conn)``."""

    def __init__(self, script):
        self.script = script
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                if self.stop.is_set():
                    return
                conn.recv(65536)
                try:
                    self.script(conn, self.stop)
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        try:
            # Closing a listening socket does not wake accept(); a connection does.
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self.thread.join(timeout=2)
        self.sock.close()


class TestProxyHardening(unittest.TestCase):
    def _proxy(self, script, **options):
        upstream = _RawUpstream(script)
        self.addCleanup(upstream.close)
        proxy = ProxyI(name="edge", **options)
        proxy.route(name="r", path_prefix="/").upstream(
            f"http://127.0.0.1:{upstream.port}", name="u", timeout_seconds=2
        ).build()
        return proxy

    def _get(self, proxy, path="/x"):
        return proxy.dispatch(
            Request("GET", path, "", {"host": "edge.test"}, b"", ("127.0.0.1", 1), tls={})
        )

    def test_dot_segment_detection(self):
        for path in (
            "/public/../admin",
            "/public/..",
            "/a/./b",
            "/public/%2e%2e/admin",
            "/public/%2E%2e/admin",
            "/public/.%2E/admin",
            "/public/%2e%2e%2fadmin",
            "/public%2f..%2fadmin",
            "/public/..%5cadmin",
        ):
            with self.subTest(path=path):
                self.assertTrue(_has_dot_segments(path))
        for path in ("/", "/a/..b/c", "/.well-known/x", "/a/b.", "/a/%2e%2ex"):
            with self.subTest(path=path):
                self.assertFalse(_has_dot_segments(path))

    def test_dot_segments_are_rejected_before_rules(self):
        calls = []

        def script(conn, _stop):
            calls.append(1)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        proxy = self._proxy(script)
        for path in ("/public/../../admin", "/public/%2e%2e/%2E%2E/admin"):
            with self.subTest(path=path):
                self.assertEqual(self._get(proxy, path).status, 400)
        self.assertEqual(calls, [])
        self.assertEqual(self._get(proxy, "/public/ok").status, 200)

    def test_all_upstream_set_cookie_headers_are_kept(self):
        def script(conn, _stop):
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2; HttpOnly\r\n"
                b"Content-Length: 2\r\n\r\nok"
            )

        response = self._get(self._proxy(script))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Set-Cookie"], ["a=1", "b=2; HttpOnly"])

    def test_upstream_url_header_is_opt_in(self):
        def script(conn, _stop):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        response = self._get(self._proxy(script))
        self.assertNotIn("X-ProxyI-Upstream", response.headers)
        self.assertEqual(response.headers["X-ProxyI-Target"], "u")
        response = self._get(self._proxy(script, expose_upstream_header=True))
        self.assertTrue(response.headers["X-ProxyI-Upstream"].startswith("http://127.0.0.1:"))

    def test_buffered_body_over_the_limit_is_502(self):
        def declared(conn, _stop):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 100)

        response = self._get(self._proxy(declared, max_response_bytes=10))
        self.assertEqual(response.status, 502)

        def within(conn, _stop):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n" + b"y" * 10)

        response = self._get(self._proxy(within, max_response_bytes=10))
        self.assertEqual((response.status, response.body), (200, b"y" * 10))

    def test_buffered_body_has_a_total_deadline(self):
        def trickle(conn, stop):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
            while not stop.wait(0.02):
                conn.sendall(b"x")

        proxy = self._proxy(trickle, response_timeout_seconds=0.2)
        self.assertEqual(self._get(proxy).status, 504)

    def test_event_stream_is_relayed_without_buffering(self):
        release = threading.Event()

        def sse(conn, stop):
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n7\r\ndata:1\n\r\n"
            )
            release.wait(timeout=5)
            conn.sendall(b"7\r\ndata:2\n\r\n0\r\n\r\n")

        response = self._get(self._proxy(sse))
        self.assertTrue(response.is_stream)
        self.assertNotIn("Content-Length", response.headers)
        chunks = iter(response.stream)
        self.assertEqual(next(chunks), b"data:1\n")
        release.set()
        self.assertEqual(b"".join(chunks), b"data:2\n")

    def test_close_delimited_body_is_streamed(self):
        def script(conn, _stop):
            conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nabc")

        response = self._get(self._proxy(script))
        self.assertTrue(response.is_stream)
        self.assertEqual(b"".join(response.stream), b"abc")

if __name__ == "__main__":
    unittest.main()

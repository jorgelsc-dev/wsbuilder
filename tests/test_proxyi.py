import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wsbuilder import ProxyI, Request
from wsbuilder.proxyi import BALANCING_BEST, BALANCING_LEAST_RESPONSE_TIME, BALANCING_ROUND_ROBIN


class _BackendHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {
            "path": self.path,
            "host": self.headers.get("Host", ""),
            "x-env": self.headers.get("x-env", ""),
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

    def _request(self, path="/api/users", *, host="api.test.local", headers=None, body=b"", method="GET"):
        request_headers = {"host": host}
        if headers:
            request_headers.update(headers)
        return Request(
            method=method,
            path=path,
            query_string="debug=1",
            headers=request_headers,
            body=body,
            client=("127.0.0.1", 12345),
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


if __name__ == "__main__":
    unittest.main()

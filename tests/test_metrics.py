import json
import unittest

from wsbuilder import App, ProxyI, Request
from wsbuilder.metrics import OVERFLOW_LABEL, AppMetrics, install_metrics


class TestMetrics(unittest.TestCase):
    def test_aborted_request_does_not_count_as_a_delivered_response(self):
        metrics = AppMetrics()
        metrics.http_request_started("GET", "/interrupted")
        metrics.http_request_started("GET", "/other")
        metrics.http_request_aborted()
        snap = metrics.snapshot()
        self.assertEqual(snap["connections"]["http_inflight"], 1)
        self.assertEqual(snap["http"]["responses_total"], 0)
        self.assertEqual(snap["traffic"]["bytes_out_total"], 0)
        metrics.http_response_sent("GET", "/other", 200, body_size=2)
        self.assertEqual(metrics.snapshot()["connections"]["http_inflight"], 0)
        self.assertEqual(metrics.total_http_responses, 1)

    def test_snapshot_and_stream(self):
        m = AppMetrics(app_name="test-app")
        m.tcp_connection_open()
        m.http_request_started("GET", "/hello", body_size=10)
        m.http_response_sent("GET", "/hello", 200, body_size=20, duration_ms=5.5)
        m.ws_opened("/ws/")
        m.ws_message_in(4)
        m.ws_message_out(8)
        m.ws_closed("/ws/")
        m.tcp_connection_close()

        snap = m.snapshot()
        self.assertEqual(snap["app_name"], "test-app")
        self.assertEqual(snap["http"]["requests_total"], 1)
        self.assertEqual(snap["http"]["responses_total"], 1)
        self.assertEqual(snap["http"]["status"]["200"], 1)
        self.assertEqual(snap["websocket"]["upgrades_total"], 1)
        self.assertEqual(snap["websocket"]["messages_in_total"], 1)
        self.assertEqual(snap["websocket"]["messages_out_total"], 1)

        chunks = list(m.stream_chunks(interval_seconds=0.01, max_points=2))
        self.assertEqual(len(chunks), 2)
        payload = json.loads(chunks[0].decode("utf-8"))
        self.assertEqual(payload["app_name"], "test-app")

    def test_install_metrics_routes(self):
        app = App()
        metrics = install_metrics(app, app_name="my-app")

        req_snapshot = Request(
            method="GET",
            path="/api/metrics",
            query_string="",
            headers={},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )
        resp_snapshot = app.dispatch(req_snapshot)
        data = json.loads(resp_snapshot.body.decode("utf-8"))
        self.assertEqual(resp_snapshot.status, 200)
        self.assertEqual(data["app_name"], "my-app")

        req_stream = Request(
            method="GET",
            path="/api/metrics/stream",
            query_string="interval=0.01&limit=1",
            headers={},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )
        resp_stream = app.dispatch(req_stream)
        self.assertEqual(resp_stream.status, 200)
        self.assertTrue(resp_stream.is_stream)
        self.assertEqual(resp_stream.headers.get("X-Content-Type-Options"), "nosniff")
        first = next(iter(resp_stream.stream))
        data_stream = json.loads(first.decode("utf-8"))
        self.assertEqual(data_stream["app_name"], "my-app")
        self.assertIs(app.metrics, metrics)

    def test_stream_default_is_finite(self):
        app = App()
        install_metrics(app, app_name="finite-default")
        req_stream = Request(
            method="GET",
            path="/api/metrics/stream",
            query_string="interval=0.1",
            headers={},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )
        resp_stream = app.dispatch(req_stream)
        chunks = list(resp_stream.stream)
        self.assertEqual(len(chunks), 5)

    def test_app_enable_metrics_exposes_thread_metrics_in_snapshot_and_stream(self):
        app = App()

        @app.view("/jobs", min_threads=1, max_threads=2, requests_per_thread=0)
        def jobs(_request):
            return "ok"

        app.enable_metrics(app_name="thread-metrics")

        req_snapshot = Request(
            method="GET",
            path="/api/metrics",
            query_string="",
            headers={},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )
        resp_snapshot = app.dispatch(req_snapshot)
        data = json.loads(resp_snapshot.body.decode("utf-8"))
        self.assertEqual(resp_snapshot.status, 200)
        self.assertIn("threads", data)
        self.assertEqual(data["threads"]["distribution"], "least_busy")
        route_row = next(row for row in data["threads"]["routes"] if row["path"] == "/jobs")
        self.assertEqual(route_row["min_threads"], 1)
        self.assertEqual(route_row["max_threads"], 2)
        self.assertEqual(route_row["requests_per_thread"], 0)

        req_stream = Request(
            method="GET",
            path="/api/metrics/stream",
            query_string="interval=0.01&limit=1",
            headers={},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )
        resp_stream = app.dispatch(req_stream)
        self.assertEqual(resp_stream.status, 200)
        first = next(iter(resp_stream.stream))
        stream_data = json.loads(first.decode("utf-8"))
        self.assertIn("threads", stream_data)
        self.assertEqual(stream_data["threads"]["distribution"], "least_busy")

    def test_app_enable_metrics_includes_proxyi_snapshot(self):
        app = App()
        app.proxyi = ProxyI(name="proxy-metrics")
        app.enable_metrics(app_name="with-proxy")

        req_snapshot = Request(
            method="GET",
            path="/api/metrics",
            query_string="",
            headers={},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )
        resp_snapshot = app.dispatch(req_snapshot)
        data = json.loads(resp_snapshot.body.decode("utf-8"))
        self.assertIn("proxyi", data)
        self.assertEqual(data["proxyi"]["name"], "proxy-metrics")


class TestMetricsCardinality(unittest.TestCase):
    def test_unknown_paths_and_methods_fold_into_an_overflow_bucket(self):
        metrics = AppMetrics(app_name="capped", max_tracked_labels=3)

        for index in range(50):
            metrics.http_request_started(f"M{index}", f"/scan/{index}")
            metrics.ws_opened(f"/ws/{index}")

        snapshot = metrics.snapshot()
        paths = snapshot["http"]["paths"]
        methods = snapshot["http"]["methods"]
        ws_paths = snapshot["websocket"]["paths"]

        # 3 tracked labels plus the shared overflow bucket.
        self.assertEqual(len(paths), 4)
        self.assertEqual(len(methods), 4)
        self.assertEqual(len(ws_paths), 4)
        self.assertEqual(paths[OVERFLOW_LABEL], 47)
        self.assertEqual(ws_paths[OVERFLOW_LABEL], 47)
        self.assertEqual(sum(paths.values()), 50)
        self.assertEqual(snapshot["http"]["requests_total"], 50)

    def test_known_labels_keep_their_own_counters(self):
        metrics = AppMetrics(app_name="capped", max_tracked_labels=2)

        for _ in range(5):
            metrics.http_request_started("GET", "/health")
        metrics.http_request_started("GET", "/other")
        metrics.http_request_started("GET", "/overflow")

        paths = metrics.snapshot()["http"]["paths"]
        self.assertEqual(paths["/health"], 5)
        self.assertEqual(paths["/other"], 1)
        self.assertEqual(paths[OVERFLOW_LABEL], 1)

    def test_enable_metrics_accepts_a_custom_label_budget(self):
        app = App()
        self.addCleanup(app.close)
        metrics = app.enable_metrics(max_tracked_labels=7)

        self.assertEqual(metrics.max_tracked_labels, 7)


class TestOperationalEndpointGuard(unittest.TestCase):
    def request(self, path, client="127.0.0.1", headers=None, query=""):
        return Request(
            method="GET",
            path=path,
            query_string=query,
            headers=headers or {},
            body=b"",
            client=(client, 4000),
        )

    def app(self, **options):
        app = App()
        self.addCleanup(app.close)
        app.enable_metrics(**options)
        return app

    def test_metrics_answer_private_networks_only_by_default(self):
        app = self.app()
        self.assertEqual(app.dispatch(self.request("/api/metrics")).status, 200)
        self.assertEqual(app.dispatch(self.request("/api/metrics", "10.1.2.3")).status, 200)
        self.assertEqual(app.dispatch(self.request("/api/metrics", "203.0.113.9")).status, 403)
        self.assertEqual(app.dispatch(self.request("/api/metrics/stream", "203.0.113.9")).status, 403)

    def test_access_any_and_explicit_networks(self):
        public = self.app(access="*")
        self.assertEqual(public.dispatch(self.request("/api/metrics", "203.0.113.9")).status, 200)
        listed = self.app(access=["203.0.113.0/24"])
        self.assertEqual(listed.dispatch(self.request("/api/metrics", "203.0.113.9")).status, 200)
        self.assertEqual(listed.dispatch(self.request("/api/metrics", "127.0.0.1")).status, 403)
        with self.assertRaises(ValueError):
            self.app(access=["not-a-network"])

    def test_bearer_token_is_required_when_configured(self):
        app = self.app(access="*", token="s3cret")
        self.assertEqual(app.dispatch(self.request("/api/metrics")).status, 401)
        wrong = {"authorization": "Bearer nope"}
        self.assertEqual(app.dispatch(self.request("/api/metrics", headers=wrong)).status, 401)
        right = {"authorization": "Bearer s3cret"}
        self.assertEqual(app.dispatch(self.request("/api/metrics", headers=right)).status, 200)

    def test_authorize_callback_decides_alone(self):
        app = self.app(authorize=lambda request: request.headers.get("x-ops") == "1")
        self.assertEqual(app.dispatch(self.request("/api/metrics", "203.0.113.9", {"x-ops": "1"})).status, 200)
        self.assertEqual(app.dispatch(self.request("/api/metrics")).status, 403)

    def test_streams_are_limited_in_number_and_duration(self):
        app = self.app(max_streams=1, max_stream_seconds=1)
        first = app.dispatch(self.request("/api/metrics/stream", query="follow=1&interval=0.1"))
        self.assertEqual(first.status, 200)
        busy = app.dispatch(self.request("/api/metrics/stream"))
        self.assertEqual(busy.status, 429)
        self.assertEqual(busy.headers["Retry-After"], "5")
        chunks = list(first.stream)  # Ends at max_stream_seconds despite follow=1.
        self.assertGreaterEqual(len(chunks), 1)
        self.assertEqual(app.metrics.guard.active_streams, 0)
        self.assertEqual(app.dispatch(self.request("/api/metrics/stream", query="limit=1")).status, 200)

    def test_proxyi_dashboard_and_metrics_are_guarded(self):
        app = App()
        self.addCleanup(app.close)
        proxy = ProxyI()
        proxy.install(app)
        for path in (proxy.dashboard_path, proxy.metrics_path, proxy.metrics_stream_path):
            with self.subTest(path=path):
                self.assertEqual(app.dispatch(self.request(path, "198.51.100.7")).status, 403)
        self.assertEqual(app.dispatch(self.request(proxy.metrics_path)).status, 200)
        # ProxyI used directly as the dispatcher applies the same guard.
        self.assertEqual(proxy.dispatch(self.request(proxy.dashboard_path, "198.51.100.7")).status, 403)
        open_proxy = ProxyI(access="*")
        self.assertEqual(open_proxy.dispatch(self.request(open_proxy.metrics_path, "198.51.100.7")).status, 200)


if __name__ == "__main__":
    unittest.main()

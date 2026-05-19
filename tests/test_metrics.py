import json
import unittest

from wsbuilder import App, Request
from wsbuilder.metrics import AppMetrics, install_metrics


class TestMetrics(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

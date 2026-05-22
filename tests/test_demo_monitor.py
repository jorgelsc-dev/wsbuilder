import unittest

from wsbuilder import Request
from wsbuilder.__main__ import build_demo_app


class TestDemoMonitor(unittest.TestCase):
    def setUp(self):
        self.app = build_demo_app()

    def tearDown(self):
        self.app.close()

    def test_monitor_route_streams_metrics_in_browser(self):
        req = Request(
            method="GET",
            path="/monitor",
            query_string="",
            headers={},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )
        resp = self.app.dispatch(req)
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))

        html = resp.body.decode("utf-8", errors="ignore")
        self.assertIn("/api/metrics/stream", html)
        self.assertIn("TextDecoderStream", html)
        self.assertIn("follow=1", html)


if __name__ == "__main__":
    unittest.main()

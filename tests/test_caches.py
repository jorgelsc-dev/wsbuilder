import json
import time
import unittest

from wsbuilder import App, Request, Response


def _req(path, method="GET"):
    return Request(
        method=method,
        path=path,
        query_string="",
        headers={},
        body=b"",
        client=("127.0.0.1", 1234),
        tls={},
    )


class TestViewResponseCaches(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def tearDown(self):
        self.app.close()

    def test_view_cache_ttl_from_route_config(self):
        caches = self.app.enable_caches()
        self.assertIs(caches, self.app.caches)
        state = {"n": 0}

        @self.app.view("/ttl", cache={"ttl": 0.08})
        def ttl_view(_request):
            state["n"] += 1
            return f"n={state['n']}"

        first = self.app.dispatch(_req("/ttl"))
        second = self.app.dispatch(_req("/ttl"))
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(first.body, second.body)
        self.assertEqual(second.headers.get("X-WSBuilder-Cache"), "HIT")

        time.sleep(0.1)
        third = self.app.dispatch(_req("/ttl"))
        self.assertNotEqual(third.body, first.body)

    def test_global_wildcard_rule(self):
        caches = self.app.enable_caches()
        caches.set_global_wildcard(0.2)
        state = {"n": 0}

        @self.app.view("/wild")
        def wild_view(_request):
            state["n"] += 1
            return f"wild={state['n']}"

        first = self.app.dispatch(_req("/wild"))
        second = self.app.dispatch(_req("/wild"))
        self.assertEqual(first.body, second.body)
        self.assertEqual(second.headers.get("X-WSBuilder-Cache"), "HIT")

    def test_global_mimetype_rule(self):
        caches = self.app.enable_caches()
        caches.set_global_mimetype("text/plain", 0.2)
        plain_state = {"n": 0}
        html_state = {"n": 0}

        @self.app.view("/plain")
        def plain_view(_request):
            plain_state["n"] += 1
            return Response.text(f"plain={plain_state['n']}")

        @self.app.view("/html")
        def html_view(_request):
            html_state["n"] += 1
            return Response.html(f"<p>{html_state['n']}</p>")

        p1 = self.app.dispatch(_req("/plain"))
        p2 = self.app.dispatch(_req("/plain"))
        self.assertEqual(p1.body, p2.body)
        self.assertEqual(p2.headers.get("X-WSBuilder-Cache"), "HIT")

        h1 = self.app.dispatch(_req("/html"))
        h2 = self.app.dispatch(_req("/html"))
        self.assertNotEqual(h1.body, h2.body)
        self.assertIsNone(h2.headers.get("X-WSBuilder-Cache"))

    def test_route_can_opt_out_from_global_cache(self):
        caches = self.app.enable_caches()
        caches.set_global_wildcard(0.2)
        state = {"n": 0}

        @self.app.view("/no-cache", cache=False)
        def no_cache_view(_request):
            state["n"] += 1
            return f"v={state['n']}"

        first = self.app.dispatch(_req("/no-cache"))
        second = self.app.dispatch(_req("/no-cache"))
        self.assertNotEqual(first.body, second.body)
        self.assertIsNone(second.headers.get("X-WSBuilder-Cache"))

    def test_metrics_include_http_cache_snapshot(self):
        caches = self.app.enable_caches()
        caches.set_global_wildcard(0.2)
        self.app.enable_metrics(app_name="cache-metrics")

        @self.app.view("/cache-me")
        def cache_me(_request):
            return "ok"

        self.app.dispatch(_req("/cache-me"))
        self.app.dispatch(_req("/cache-me"))

        metrics_resp = self.app.dispatch(_req("/api/metrics"))
        payload = json.loads(metrics_resp.body.decode("utf-8"))
        self.assertIn("http_cache", payload)
        self.assertIn("counters", payload["http_cache"])
        self.assertGreaterEqual(payload["http_cache"]["counters"]["lookups"], 1)


if __name__ == "__main__":
    unittest.main()

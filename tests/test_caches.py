import json
import threading
import time
import unittest

from wsbuilder import App, Request, Response
from wsbuilder.caches import ViewResponseCache


def _req(path, method="GET", headers=None):
    return Request(
        method=method,
        path=path,
        query_string="",
        headers=headers or {},
        body=b"",
        client=("127.0.0.1", 1234),
        tls={},
    )


def _request_header(request, name):
    wanted = name.lower()
    for key, value in request.headers.items():
        if str(key).lower() == wanted:
            return value
    return ""


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

    def test_private_request_headers_bypass_cache_by_default(self):
        self.app.enable_caches(ViewResponseCache(default_ttl=1.0))

        @self.app.view("/account")
        def account(request):
            return _request_header(request, "authorization")

        alice = self.app.dispatch(
            _req("/account", headers={"Authorization": "Bearer alice"})
        )
        bob = self.app.dispatch(
            _req("/account", headers={"authorization": "Bearer bob"})
        )
        self.assertEqual(alice.body, b"Bearer alice")
        self.assertEqual(bob.body, b"Bearer bob")
        self.assertIsNone(bob.headers.get("X-WSBuilder-Cache"))

    def test_private_cache_opt_in_varies_credentials_automatically(self):
        self.app.enable_caches()
        calls = {"alice": 0, "bob": 0}

        @self.app.view(
            "/private-opt-in",
            cache={"ttl": 1.0, "allow_private": True},
        )
        def private_opt_in(request):
            user = _request_header(request, "authorization").split()[-1]
            calls[user] += 1
            return f"{user}:{calls[user]}"

        alice_1 = self.app.dispatch(
            _req("/private-opt-in", headers={"Authorization": "Bearer alice"})
        )
        alice_2 = self.app.dispatch(
            _req("/private-opt-in", headers={"authorization": "Bearer alice"})
        )
        bob = self.app.dispatch(
            _req("/private-opt-in", headers={"authorization": "Bearer bob"})
        )
        self.assertEqual(alice_1.body, alice_2.body)
        self.assertEqual(alice_2.headers.get("X-WSBuilder-Cache"), "HIT")
        self.assertEqual(bob.body, b"bob:1")
        self.assertIsNone(bob.headers.get("X-WSBuilder-Cache"))

    def test_cache_control_and_content_type_are_case_insensitive(self):
        caches = self.app.enable_caches()
        caches.set_global_mimetype("text/plain", 1.0)
        state = {"private": 0, "plain": 0}

        @self.app.view("/lower-no-store")
        def lower_no_store(_request):
            state["private"] += 1
            return Response.text(
                str(state["private"]),
                headers={"cache-control": "no-store"},
            )

        @self.app.view("/lower-content-type")
        def lower_content_type(_request):
            state["plain"] += 1
            return Response(
                body=str(state["plain"]),
                headers={"content-type": "text/plain; charset=utf-8"},
            )

        private_1 = self.app.dispatch(_req("/lower-no-store"))
        private_2 = self.app.dispatch(_req("/lower-no-store"))
        plain_1 = self.app.dispatch(_req("/lower-content-type"))
        plain_2 = self.app.dispatch(_req("/lower-content-type"))
        self.assertNotEqual(private_1.body, private_2.body)
        self.assertEqual(plain_1.body, plain_2.body)
        self.assertEqual(plain_2.headers.get("X-WSBuilder-Cache"), "HIT")

    def test_uncovered_vary_header_prevents_storage(self):
        self.app.enable_caches(ViewResponseCache(default_ttl=1.0))

        @self.app.view("/localized")
        def localized(request):
            return Response.text(
                request.headers.get("accept-language", ""),
                headers={"Vary": "Accept-Language"},
            )

        spanish = self.app.dispatch(
            _req("/localized", headers={"accept-language": "es"})
        )
        english = self.app.dispatch(
            _req("/localized", headers={"accept-language": "en"})
        )
        self.assertEqual(spanish.body, b"es")
        self.assertEqual(english.body, b"en")
        self.assertIsNone(english.headers.get("X-WSBuilder-Cache"))

    def test_cache_stats_are_exact_under_concurrency(self):
        caches = ViewResponseCache()
        try:
            def increment_hits():
                for _ in range(1000):
                    caches._inc_stat("hits")

            threads = [
                threading.Thread(target=increment_hits)
                for _ in range(10)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(caches.snapshot()["counters"]["hits"], 10_000)
        finally:
            caches.close()


if __name__ == "__main__":
    unittest.main()

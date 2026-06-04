import json
import unittest

from wsbuilder import App, Request, Response, SecurityPolicy, SQLiteMemoryCache, install_cache, install_metrics, install_security


def _req(path, method="GET", client=("127.0.0.1", 1234)):
    return Request(
        method=method,
        path=path,
        query_string="",
        headers={},
        body=b"",
        client=client,
        tls={},
    )


class TestNativeDocs(unittest.TestCase):
    def setUp(self):
        self.app = App()
        install_metrics(self.app, app_name="docs-app")
        install_cache(self.app, SQLiteMemoryCache(default_ttl=5))
        install_security(
            self.app,
            SecurityPolicy(
                rate_limit_requests=10,
                rate_limit_window_seconds=60,
            ),
        )

        @self.app.view("/hello")
        def hello(_request):
            return Response.text("ok")

        @self.app.api("/api/health")
        def health(_request):
            return {"ok": True}

        @self.app.ws("/ws/")
        def ws_handler(_ws, _request):
            return None

        self.app.enable_docs(path="/docs", json_path="/docs.json", title="Docs")

    def tearDown(self):
        self.app.close()

    def test_docs_json_reflects_registered_surface(self):
        response = self.app.dispatch(_req("/docs.json"))
        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.headers.get("Content-Type", ""))

        data = json.loads(response.body.decode("utf-8"))
        self.assertTrue(data["metrics_enabled"])
        self.assertTrue(data["security_enabled"])
        self.assertTrue(data["cache_enabled"])
        self.assertEqual(data["ws_routes_total"], 1)

        paths = {row["path"] for row in data["routes"]}
        self.assertIn("/docs", paths)
        self.assertIn("/docs.json", paths)
        self.assertIn("/hello", paths)
        self.assertIn("/api/health", paths)

        ws_paths = {row["path"] for row in data["ws_routes"]}
        self.assertIn("/ws/", ws_paths)

    def test_docs_html_contains_automatic_sections(self):
        response = self.app.dispatch(_req("/docs"))
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers.get("Content-Type", ""))

        html = response.body.decode("utf-8")
        self.assertIn("Docs", html)
        self.assertIn("/docs.json", html)
        self.assertIn("/hello", html)
        self.assertIn("/api/health", html)
        self.assertIn("/ws/", html)


if __name__ == "__main__":
    unittest.main()

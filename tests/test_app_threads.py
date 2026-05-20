import threading
import unittest

from wsbuilder import App, Request


class TestViewThreadRouting(unittest.TestCase):
    def setUp(self):
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.close()

    def _new_app(self):
        app = App()
        self.apps.append(app)
        return app

    def _request(self, path, headers=None):
        return Request(
            method="GET",
            path=path,
            query_string="",
            headers=headers or {},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )

    def test_view_without_threads_runs_in_parent(self):
        app = self._new_app()

        @app.view("/plain")
        def plain(_request):
            return threading.current_thread().name

        response = app.dispatch(self._request("/plain"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body.decode("utf-8"), threading.current_thread().name)
        self.assertNotIn("WSBuilder-Thread", response.headers)

    def test_first_request_assigns_cookie_and_parent_handles(self):
        app = self._new_app()

        @app.view("/work", thread_count=3, max_clients=2)
        def work(_request):
            return threading.current_thread().name

        response = app.dispatch(self._request("/work"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body.decode("utf-8"), threading.current_thread().name)
        self.assertEqual(response.headers.get("WSBuilder-Thread-Mode"), "parent-assigned")
        self.assertIn("Set-Cookie", response.headers)
        self.assertIn("wsbuilder-thread=", response.headers["Set-Cookie"])

    def test_cookie_routes_to_worker_thread(self):
        app = self._new_app()

        @app.view("/work", thread_count=2, max_clients=3)
        def work(_request):
            return threading.current_thread().name

        first = app.dispatch(self._request("/work"))
        set_cookie = first.headers["Set-Cookie"]
        cookie_value = set_cookie.split(";", 1)[0].split("=", 1)[1]

        second = app.dispatch(self._request("/work", headers={"Cookie": f"wsbuilder-thread={cookie_value}"}))
        self.assertEqual(second.status, 200)
        self.assertNotEqual(second.body.decode("utf-8"), threading.current_thread().name)
        self.assertEqual(second.headers.get("WSBuilder-Thread-Mode"), "worker")
        self.assertEqual(second.headers.get("WSBuilder-Thread"), cookie_value)

    def test_invalid_or_unknown_cookie_falls_back_to_parent_without_error(self):
        app = self._new_app()

        @app.view("/v", thread_count=1, max_clients=1)
        def view(_request):
            return "ok"

        bad = app.dispatch(self._request("/v", headers={"Cookie": "wsbuilder-thread=not-a-uuid"}))
        self.assertEqual(bad.status, 200)
        self.assertEqual(bad.body.decode("utf-8"), "ok")
        self.assertEqual(bad.headers.get("WSBuilder-Thread-Mode"), "parent-assigned")

        unknown = app.dispatch(
            self._request(
                "/v",
                headers={"Cookie": "wsbuilder-thread=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            )
        )
        self.assertEqual(unknown.status, 200)
        self.assertEqual(unknown.body.decode("utf-8"), "ok")
        self.assertEqual(unknown.headers.get("WSBuilder-Thread-Mode"), "parent-assigned")

    def test_api_routes_keep_existing_dispatch(self):
        app = self._new_app()

        @app.api("/api/health")
        def health(_request):
            return {"ok": True}

        response = app.dispatch(self._request("/api/health"))
        self.assertEqual(response.status, 200)
        self.assertNotIn("WSBuilder-Thread", response.headers)


if __name__ == "__main__":
    unittest.main()

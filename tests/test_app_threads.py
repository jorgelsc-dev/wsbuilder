import threading
import time
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

    def _request(self, path, headers=None, client=("127.0.0.1", 1234)):
        return Request(
            method="GET",
            path=path,
            query_string="",
            headers=headers or {},
            body=b"",
            client=client,
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

    def test_threaded_view_runs_in_worker_and_sets_headers(self):
        app = self._new_app()

        @app.view("/work", min_threads=1, max_threads=2, requests_per_thread=0)
        def work(_request):
            return threading.current_thread().name

        response = app.dispatch(self._request("/work"))
        self.assertEqual(response.status, 200)
        self.assertNotEqual(response.body.decode("utf-8"), threading.current_thread().name)
        self.assertEqual(response.headers.get("WSBuilder-Thread-Mode"), "worker")
        self.assertIn("WSBuilder-Thread", response.headers)
        self.assertIn("Set-Cookie", response.headers)

    def test_requests_per_thread_zero_allows_queue_growth(self):
        app = self._new_app()
        release = threading.Event()
        first_started = threading.Event()
        responses = {}

        @app.view("/queue", min_threads=1, max_threads=1, requests_per_thread=0)
        def queue(_request):
            first_started.set()
            release.wait(0.5)
            return "ok"

        def call(idx):
            responses[idx] = app.dispatch(self._request("/queue"))

        workers = [threading.Thread(target=call, args=(i,)) for i in range(3)]
        for w in workers:
            w.start()

        self.assertTrue(first_started.wait(1.0))
        route = app.router.resolve("/queue", "GET")
        self.assertIsNotNone(route)
        # wait a bit for enqueued requests to accumulate behind the first active one
        pending = 0
        for _ in range(20):
            pending = route.thread_pool.workers[0].pending_jobs()
            if pending >= 2:
                break
            time.sleep(0.01)
        self.assertGreaterEqual(pending, 2)
        release.set()
        for w in workers:
            w.join(timeout=1.0)

        self.assertEqual(len(responses), 3)
        for i in range(3):
            self.assertEqual(responses[i].status, 200)

    def test_requests_per_thread_limit_returns_503_when_worker_full(self):
        app = self._new_app()
        release = threading.Event()
        started = threading.Event()
        first_response = {}

        @app.view("/limited", min_threads=1, max_threads=1, requests_per_thread=1)
        def limited(_request):
            started.set()
            release.wait(0.5)
            return "ok"

        def run_first():
            first_response["resp"] = app.dispatch(self._request("/limited"))

        t = threading.Thread(target=run_first)
        t.start()
        self.assertTrue(started.wait(1.0))

        second = app.dispatch(self._request("/limited"))
        self.assertEqual(second.status, 503)

        release.set()
        t.join(timeout=1.0)
        self.assertIn("resp", first_response)
        self.assertEqual(first_response["resp"].status, 200)

    def test_min_max_threads_scale_up_under_load(self):
        app = self._new_app()
        release = threading.Event()
        entered = threading.Event()
        lock = threading.Lock()
        started_count = 0
        responses = {}

        @app.view("/scale", min_threads=1, max_threads=3, requests_per_thread=1)
        def scale(_request):
            nonlocal started_count
            with lock:
                started_count += 1
                if started_count >= 3:
                    entered.set()
            release.wait(0.5)
            return "ok"

        def call(idx):
            responses[idx] = app.dispatch(self._request("/scale"))

        workers = [threading.Thread(target=call, args=(i,)) for i in range(3)]
        for w in workers:
            w.start()

        self.assertTrue(entered.wait(1.0))
        release.set()
        for w in workers:
            w.join(timeout=1.0)

        self.assertEqual(len(responses), 3)
        thread_ids = {responses[i].headers.get("WSBuilder-Thread") for i in range(3)}
        self.assertEqual(len(thread_ids), 3)

        route = app.router.resolve("/scale", "GET")
        self.assertIsNotNone(route)
        self.assertEqual(len(route.thread_pool.workers), 3)

    def test_worker_execution_timeout_returns_504(self):
        app = self._new_app()

        @app.view("/slow", min_threads=1, max_threads=1, worker_timeout_seconds=0.05, requests_per_thread=0)
        def slow(_request):
            time.sleep(0.2)
            return "done"

        response = app.dispatch(self._request("/slow"))
        self.assertEqual(response.status, 504)

    def test_thread_count_backwards_compatibility(self):
        app = self._new_app()

        @app.view("/legacy", thread_count=2, requests_per_thread=0)
        def legacy(_request):
            return "ok"

        route = app.router.resolve("/legacy", "GET")
        self.assertIsNotNone(route)
        self.assertEqual(route.min_threads, 2)
        self.assertEqual(route.max_threads, 2)
        self.assertEqual(route.thread_count, 2)

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

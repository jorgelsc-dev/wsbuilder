import json
import unittest

from wsbuilder import App, Request, SecurityPolicy


def _req(method, path, query_string="", client=("127.0.0.1", 1234), headers=None, tls=None):
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=b"",
        client=client,
        tls=tls or {},
    )


class TestSecurityPolicy(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def tearDown(self):
        self.app.close()

    def test_acl_deny_rule_blocks_api_route(self):
        policy = self.app.enable_security()
        policy.deny(name="deny-admin-post", methods=("POST",), path="/api/admin")

        @self.app.api("/api/admin", methods=("POST",))
        def admin(_request):
            return {"ok": True}

        response = self.app.dispatch(_req("POST", "/api/admin"))
        self.assertEqual(response.status, 403)
        self.assertIn("application/json", response.headers.get("Content-Type", ""))
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload.get("reason"), "acl_deny")
        self.assertIn("X-WSBuilder-Security-Reason", response.headers)

        snap = policy.snapshot()
        self.assertEqual(snap["counters"]["acl_deny_total"], 1)
        self.assertGreaterEqual(snap["counters"]["blocked_total"], 1)

    def test_whitelist_overrides_blacklist(self):
        policy = self.app.enable_security()
        policy.add_blacklist("127.0.0.1")
        policy.add_whitelist("127.0.0.1")

        @self.app.view("/hello")
        def hello(_request):
            return "ok"

        allowed = self.app.dispatch(_req("GET", "/hello"))
        self.assertEqual(allowed.status, 200)

        policy.whitelist_overrides_blacklist = False
        denied = self.app.dispatch(_req("GET", "/hello"))
        self.assertEqual(denied.status, 403)
        self.assertIn(b"Forbidden", denied.body)

    def test_behavior_rate_limit_triggers_temporary_block(self):
        policy = SecurityPolicy(
            rate_limit_requests=1,
            rate_limit_window_seconds=60.0,
            block_duration_seconds=5.0,
        )
        self.app.enable_security(policy=policy)

        @self.app.view("/limited")
        def limited(_request):
            return "ok"

        first = self.app.dispatch(_req("GET", "/limited"))
        second = self.app.dispatch(_req("GET", "/limited"))
        third = self.app.dispatch(_req("GET", "/limited"))

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 429)
        self.assertEqual(third.status, 429)
        self.assertIn("Retry-After", second.headers)

        snap = policy.snapshot()
        self.assertGreaterEqual(snap["counters"]["temporary_blocks_active"], 1)
        self.assertIn("127.0.0.1", snap["active_blocks"])

    def test_metrics_snapshot_includes_security_block(self):
        self.app.enable_security()
        self.app.enable_metrics(app_name="secure-app")

        response = self.app.dispatch(_req("GET", "/api/metrics"))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertIn("security", payload)
        self.assertTrue(payload["security"]["enabled"])
        self.assertIn("counters", payload["security"])


if __name__ == "__main__":
    unittest.main()

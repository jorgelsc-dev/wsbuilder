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

    def test_forwarded_for_requires_a_trusted_direct_proxy(self):
        untrusted = SecurityPolicy(trust_x_forwarded_for=True)
        spoofed = _req(
            "GET",
            "/",
            client=("203.0.113.10", 1234),
            headers={"X-Forwarded-For": "198.51.100.20"},
        )
        self.assertEqual(
            untrusted.resolve_client_ip(spoofed),
            "203.0.113.10",
        )

        trusted = SecurityPolicy(
            trust_x_forwarded_for=True,
            trusted_proxy_cidrs=("10.0.0.0/8",),
        )
        proxied = _req(
            "GET",
            "/",
            client=("10.0.0.5", 1234),
            headers={
                "X-Forwarded-For": "spoofed, 198.51.100.20, 10.0.0.4"
            },
        )
        self.assertEqual(
            trusted.resolve_client_ip(proxied),
            "198.51.100.20",
        )

    def test_whitelist_bypasses_existing_behavior_block(self):
        policy = SecurityPolicy()
        self.assertTrue(policy.block_ip("127.0.0.1"))
        policy.add_whitelist("127.0.0.1")

        decision = policy.evaluate(_req("GET", "/"))

        self.assertTrue(decision.allowed)

    def test_acl_rejects_invalid_network_instead_of_matching_everyone(self):
        policy = SecurityPolicy()
        with self.assertRaises(ValueError):
            policy.deny(path="/admin", ip_cidrs=("not-a-network",))

    def test_acl_path_prefix_respects_segment_boundaries(self):
        policy = SecurityPolicy(acl_default="deny")
        policy.allow(path_prefix="/api")

        self.assertTrue(policy.evaluate(_req("GET", "/api")).allowed)
        self.assertTrue(policy.evaluate(_req("GET", "/api/users")).allowed)
        self.assertFalse(policy.evaluate(_req("GET", "/api-private")).allowed)

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


class TestSecurityTrackingLimits(unittest.TestCase):
    def test_client_tracking_maps_stop_growing_at_the_configured_ceiling(self):
        policy = SecurityPolicy(max_tracked_clients=16)

        for index in range(4000):
            decision = policy.evaluate(
                _req("GET", "/", client=(f"10.0.{index // 256}.{index % 256}", 1234))
            )
            self.assertTrue(decision.allowed)

        counters = policy.snapshot()["counters"]
        self.assertLessEqual(counters["request_clients_tracked"], 16)
        self.assertEqual(counters["tracked_clients_evicted_total"], 4000 - 16)
        self.assertEqual(counters["requests_total"], 4000)

    def test_recently_seen_client_survives_eviction_and_keeps_rate_limiting(self):
        policy = SecurityPolicy(
            max_tracked_clients=4,
            rate_limit_requests=3,
            rate_limit_window_seconds=60.0,
        )

        for index in range(3):
            self.assertTrue(policy.evaluate(_req("GET", "/", client=("203.0.113.9", 1))).allowed)
            policy.evaluate(_req("GET", "/", client=(f"198.51.100.{index}", 1)))

        decision = policy.evaluate(_req("GET", "/", client=("203.0.113.9", 1)))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.status, 429)
        self.assertEqual(decision.reason, "rate_limit")

    def test_temporary_blocks_are_capped(self):
        policy = SecurityPolicy(max_temporary_blocks=8)

        for index in range(200):
            policy.block_ip(f"192.0.2.{index % 200}", duration_seconds=600)

        counters = policy.snapshot()["counters"]
        self.assertEqual(counters["temporary_blocks_active"], 8)
        self.assertEqual(counters["temporary_blocks_evicted_total"], 192)

    def test_reblocking_a_known_ip_extends_it_without_evicting_others(self):
        policy = SecurityPolicy(max_temporary_blocks=2)
        policy.block_ip("192.0.2.1", duration_seconds=600)
        policy.block_ip("192.0.2.2", duration_seconds=600)

        policy.block_ip("192.0.2.1", duration_seconds=900)

        active = policy.snapshot()["active_blocks"]
        self.assertEqual(set(active), {"192.0.2.1", "192.0.2.2"})
        self.assertGreater(active["192.0.2.1"]["remaining_seconds"], 600)

    def test_snapshot_reports_the_configured_limits(self):
        policy = SecurityPolicy(max_tracked_clients=11, max_temporary_blocks=13)

        reported = policy.snapshot()["policy"]

        self.assertEqual(reported["max_tracked_clients"], 11)
        self.assertEqual(reported["max_temporary_blocks"], 13)

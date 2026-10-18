"""ProxyI routing, balancing, builder and dispatch paths that need no backend."""

import json
import socket
import unittest

from wsbuilder import App, ProxyI, ProxyTarget, Request
from wsbuilder.proxyi import (
    BALANCING_BEST,
    BALANCING_CONSISTENT_HASH,
    BALANCING_FIRST_AVAILABLE,
    BALANCING_LEAST_BYTES_IN,
    BALANCING_LEAST_BYTES_OUT,
    BALANCING_LEAST_CONNECTIONS,
    BALANCING_LEAST_REQUESTS,
    BALANCING_LEAST_RESPONSE_TIME,
    BALANCING_POWER_OF_TWO_CHOICES,
    BALANCING_RANDOM,
    BALANCING_ROUND_ROBIN,
    BALANCING_WEIGHTED_ROUND_ROBIN,
    SUPPORTED_BALANCING_STRATEGIES,
    ProxyRule,
    install_proxyi,
    normalize_balance_mode,
    normalize_target,
)


def _request(path="/", *, method="GET", host="example.test", headers=None, body=b"", query="", client=None):
    request_headers = {"host": host} if host else {}
    request_headers.update(headers or {})
    return Request(
        method=method,
        path=path,
        query_string=query,
        headers=request_headers,
        body=body,
        client=client or ("127.0.0.1", 40000),
        tls={},
    )


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _served(target, *, request_bytes=0, response_bytes=0, latency_ms=1.0, error=None, status=200):
    target.metrics.begin_request(request_bytes)
    target.metrics.finish_request(status, response_size=response_bytes, latency_ms=latency_ms, error=error)


class TestNormalization(unittest.TestCase):
    def test_balance_aliases_resolve_to_supported_modes(self):
        cases = {
            "rr": BALANCING_ROUND_ROBIN,
            "WRR": BALANCING_WEIGHTED_ROUND_ROBIN,
            "least-conn": BALANCING_LEAST_CONNECTIONS,
            "Least_Response": BALANCING_LEAST_RESPONSE_TIME,
            "leastreq": BALANCING_LEAST_REQUESTS,
            "consistent-hash": BALANCING_CONSISTENT_HASH,
            "first": BALANCING_FIRST_AVAILABLE,
            "power_of_two": BALANCING_POWER_OF_TWO_CHOICES,
            "best_effort": BALANCING_BEST,
            "no-such-mode": BALANCING_ROUND_ROBIN,
        }
        for alias, expected in cases.items():
            with self.subTest(alias=alias):
                self.assertEqual(normalize_balance_mode(alias), expected)
        for mode in SUPPORTED_BALANCING_STRATEGIES:
            self.assertEqual(normalize_balance_mode(mode), mode)

    def test_targets_accept_instances_mappings_and_urls(self):
        target = ProxyTarget("http://127.0.0.1:9000", name="a")
        self.assertIs(normalize_target(target), target)

        mapped = normalize_target({"url": "http://127.0.0.1:9001"}, name="b", weight=3)
        self.assertEqual((mapped.name, mapped.weight, mapped.port), ("b", 3, 9001))

        replaced = normalize_target("ignored", url="https://upstream.test/base")
        self.assertEqual((replaced.scheme, replaced.host, replaced.base_path), ("https", "upstream.test", "/base"))

        with self.assertRaisesRegex(ValueError, "requires a url"):
            normalize_target({"name": "missing"})


class TestBalancing(unittest.TestCase):
    def _rule(self, balance, *names, **rule_options):
        rule = ProxyRule(name="lb", balance=balance, **rule_options)
        for index, name in enumerate(names):
            rule.add_target({"url": f"http://127.0.0.1:{9100 + index}", "name": name})
        return rule

    def _pick(self, rule, request=None):
        return rule.choose_target(request or _request()).name

    def test_weighted_round_robin_follows_the_weights_smoothly(self):
        rule = ProxyRule(name="wrr", balance=BALANCING_WEIGHTED_ROUND_ROBIN)
        rule.add_target({"url": "http://127.0.0.1:9100", "name": "heavy", "weight": 3})
        rule.add_target({"url": "http://127.0.0.1:9101", "name": "light", "weight": 1})

        picks = [self._pick(rule) for _ in range(8)]

        self.assertEqual(picks[:4], ["heavy", "heavy", "light", "heavy"])
        self.assertEqual(picks.count("heavy"), 6)

    def test_least_modes_prefer_the_idlest_target(self):
        # Both targets served one plain request; "busy" then carries more of
        # whatever the mode measures.
        cases = (
            (BALANCING_LEAST_CONNECTIONS, lambda busy: busy.metrics.begin_request(0)),
            (BALANCING_LEAST_REQUESTS, lambda busy: _served(busy)),
            (BALANCING_LEAST_BYTES_IN, lambda busy: _served(busy, request_bytes=4096)),
            (BALANCING_LEAST_BYTES_OUT, lambda busy: _served(busy, response_bytes=4096)),
        )
        for mode, load in cases:
            with self.subTest(mode=mode):
                rule = self._rule(mode, "busy", "idle")
                busy, idle = rule.targets
                _served(busy)
                _served(idle)
                load(busy)
                self.assertEqual(self._pick(rule), "idle")

    def test_best_avoids_a_target_that_fails(self):
        rule = self._rule(BALANCING_BEST, "flaky", "steady")
        flaky, steady = rule.targets
        _served(flaky, error="refused", status=502)
        _served(steady)
        self.assertEqual(self._pick(rule), "steady")

    def test_first_available_skips_disabled_targets(self):
        rule = self._rule(BALANCING_FIRST_AVAILABLE, "off", "on", "spare")
        rule.targets[0].enabled = False
        self.assertEqual({self._pick(rule) for _ in range(3)}, {"on"})

    def test_consistent_hash_pins_a_key_to_one_target(self):
        rule = self._rule(BALANCING_CONSISTENT_HASH, "a", "b", "c", hash_key="x-tenant")

        pinned = {self._pick(rule, _request(headers={"x-tenant": "acme"})) for _ in range(5)}
        spread = {self._pick(rule, _request(headers={"x-tenant": f"tenant-{n}"})) for n in range(40)}

        self.assertEqual(len(pinned), 1)
        self.assertGreater(len(spread), 1)

    def test_consistent_hash_without_a_key_uses_the_host(self):
        rule = self._rule(BALANCING_CONSISTENT_HASH, "a", "b", "c")
        picks = {self._pick(rule, _request(host="tenant.example.test")) for _ in range(5)}
        self.assertEqual(len(picks), 1)

    def test_random_choices_stay_within_the_enabled_targets(self):
        for mode in (BALANCING_RANDOM, BALANCING_POWER_OF_TWO_CHOICES):
            with self.subTest(mode=mode):
                rule = self._rule(mode, "a", "b", "off")
                rule.targets[2].enabled = False
                self.assertLessEqual({self._pick(rule) for _ in range(30)}, {"a", "b"})
                single = self._rule(mode, "only")
                self.assertEqual(self._pick(single), "only")


class TestMatching(unittest.TestCase):
    def test_host_patterns_contains_and_regex(self):
        wildcard = ProxyRule(hosts=("*.example.test",))
        contains = ProxyRule(host_contains=("tenant",))
        regex = ProxyRule(host_regex=(r"^api-\d+\.",))

        self.assertTrue(wildcard.matches(_request(host="api.example.test")))
        self.assertFalse(wildcard.matches(_request(host="example.org")))
        self.assertTrue(contains.matches(_request(host="tenant7.example.test")))
        self.assertTrue(regex.matches(_request(host="API-12.example.test")))
        self.assertFalse(regex.matches(_request(host="api-x.example.test")))
        # A rule that needs a host never matches a request without one.
        self.assertFalse(wildcard.matches(_request(host="")))
        self.assertTrue(ProxyRule().matches(_request(host="")))

    def test_path_methods_and_headers(self):
        rule = ProxyRule(
            path_contains=("/v2/",),
            path_regex=(r"/items/\d+$",),
            methods=("get", "post"),
            header_regex={"x-client": r"^mobile-"},
        )
        good = _request("/api/v2/items/42", headers={"x-client": "Mobile-iOS"})

        self.assertTrue(rule.matches(good))
        self.assertFalse(rule.matches(_request("/api/v2/items/42", method="DELETE", headers={"x-client": "mobile-a"})))
        self.assertFalse(rule.matches(_request("/api/v1/items/42", headers={"x-client": "mobile-a"})))
        self.assertFalse(rule.matches(_request("/api/v2/items/42", headers={"x-client": "desktop"})))
        self.assertTrue(ProxyRule(path="/exact").matches(_request("/exact")))
        self.assertFalse(ProxyRule(path="/exact").matches(_request("/exact/more")))

    def test_priority_then_specificity_decide_between_matching_rules(self):
        proxy = ProxyI()
        proxy.location("/api", name="prefix").upstream("http://127.0.0.1:9300").build()
        proxy.route(name="exact", path="/api/users").upstream("http://127.0.0.1:9301").build()
        proxy.default("http://127.0.0.1:9302", name="catch-all")

        self.assertEqual(proxy._resolve_rules(_request("/api/users"))[0].name, "exact")
        self.assertEqual(proxy._resolve_rules(_request("/api/other"))[0].name, "prefix")
        self.assertEqual(proxy._resolve_rules(_request("/elsewhere"))[0].default, True)

        proxy.location("/api", name="urgent", priority=10).upstream("http://127.0.0.1:9303").build()
        self.assertEqual(proxy._resolve_rules(_request("/api/users"))[0].name, "urgent")


class TestRouteBuilder(unittest.TestCase):
    def test_changes_after_the_first_upstream_reach_the_built_rule(self):
        proxy = ProxyI(default_balance="least_conn")
        builder = proxy.route(name="api").upstream("http://127.0.0.1:9400")
        rule = proxy.routes()[0]

        (
            builder.host("API.Example.Test")
            .host_contains("example")
            .host_regex(r"\.test$")
            .location(r"^/v\d+/", regex=True)
            .header_equals("X-Env", "Prod")
            .header_contains("x-role", "admin")
            .balance("wrr")
            .priority("5")
            .strip_prefix()
            .preserve_host(False)
            .hash_key("x-user")
            .to("http://127.0.0.1:9401")
        )

        self.assertEqual(rule.hosts, ("api.example.test",))
        self.assertEqual(rule.host_contains, ("example",))
        self.assertEqual(rule.path_regex, (r"^/v\d+/",))
        self.assertEqual(rule.header_equals, {"x-env": "prod"})
        self.assertEqual(rule.balance, BALANCING_WEIGHTED_ROUND_ROBIN)
        self.assertEqual(rule.priority, 5)
        self.assertTrue(rule.strip_prefix)
        self.assertFalse(rule.preserve_host)
        self.assertEqual(rule.hash_key, "x-user")
        self.assertEqual(rule.target_count(), 2)
        self.assertTrue(
            rule.matches(
                _request(
                    "/v2/users",
                    host="api.example.test",
                    headers={"x-env": "PROD", "x-role": "superadmin"},
                )
            )
        )

    def test_location_kinds_replace_each_other(self):
        builder = ProxyI().route().location("/a").location("/b", exact=True).upstream("http://127.0.0.1:9402")
        rule = builder.build()
        self.assertEqual((rule.path, rule.path_prefix), ("/b", ""))
        builder.path_contains("/c")
        self.assertEqual((rule.path, rule.path_contains), ("", ("/c",)))
        builder.path_prefix("/d")
        self.assertEqual((rule.path_prefix, rule.path_contains), ("/d", ()))
        builder.vhost("one.test", "two.test").default()
        self.assertEqual(rule.hosts, ("one.test", "two.test"))
        self.assertTrue(rule.default)

    def test_invalid_builders_and_rules_are_rejected(self):
        proxy = ProxyI()
        with self.assertRaisesRegex(ValueError, "at least one upstream"):
            proxy.route(name="empty").build()
        with self.assertRaisesRegex(TypeError, "unexpected route options: colour"):
            proxy.route(colour="blue")
        with self.assertRaises(TypeError):
            proxy.add_rule({"name": "not a rule"})


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.proxy = ProxyI(name="edge")
        self.addCleanup(self.proxy.close)

    def test_unmatched_requests_are_404_and_counted(self):
        self.proxy.route(hosts=("only.test",)).upstream("http://127.0.0.1:9500").build()

        response = self.proxy.dispatch(_request("/x", host="other.test"))

        self.assertEqual(response.status, 404)
        self.assertEqual(self.proxy.metrics.snapshot()["status_counts"], {"404": 1})

    def test_an_unreachable_upstream_is_502_and_marks_the_target(self):
        rule = self.proxy.location("/").upstream(
            f"http://127.0.0.1:{_closed_port()}", name="down", timeout_seconds=0.5
        ).build()

        response = self.proxy.dispatch(_request("/x"))

        self.assertEqual(response.status, 502)
        snapshot = rule.targets[0].metrics.snapshot()
        self.assertEqual(snapshot["errors_total"], 1)
        self.assertEqual(snapshot["active_requests"], 0)
        self.assertEqual(rule.metrics.snapshot()["errors_total"], 1)

    def test_a_rule_whose_targets_are_all_disabled_is_502(self):
        rule = self.proxy.location("/").upstream("http://127.0.0.1:9501").build()
        rule.targets[0].enabled = False
        response = self.proxy.dispatch(_request("/x"))
        self.assertEqual(response.status, 502)
        self.assertEqual(rule.metrics.snapshot()["last_error"], "no upstream target")

    def test_built_in_endpoints_answer_get_only(self):
        self.proxy.location("/").upstream("http://127.0.0.1:9502").build()
        for path in (self.proxy.metrics_path, self.proxy.metrics_stream_path, self.proxy.dashboard_path):
            with self.subTest(path=path):
                response = self.proxy.dispatch(_request(path, method="POST"))
                self.assertEqual(response.status, 405)
                self.assertEqual(response.headers["Allow"], "GET")

        snapshot = json.loads(self.proxy.dispatch(_request(self.proxy.metrics_path)).body)
        self.assertEqual(snapshot["rules_total"], 1)
        self.assertEqual(snapshot["targets_total"], 1)
        self.assertIn(BALANCING_BEST, snapshot["supported_balancing"])

        dashboard = self.proxy.dispatch(_request(self.proxy.dashboard_path))
        self.assertIn(b"<html", dashboard.body.lower())

    def test_the_metrics_stream_honours_its_point_limit(self):
        response = self.proxy.dispatch(_request(self.proxy.metrics_stream_path, query="limit=2&interval=0"))
        lines = list(response.stream)
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["name"], "edge")
        self.assertIn("ndjson", response.headers["Content-Type"])


class TestInstall(unittest.TestCase):
    def test_install_registers_the_endpoints_on_the_app(self):
        app = App()
        self.addCleanup(app.close)
        proxy = ProxyI(name="mounted")
        proxy.install(app)

        self.assertIs(app.proxyi, proxy)
        metrics = app.dispatch(_request(proxy.metrics_path))
        self.assertEqual(json.loads(metrics.body)["name"], "mounted")
        stream = app.dispatch(_request(proxy.metrics_stream_path, query="limit=1&interval=0"))
        self.assertEqual(len(list(stream.stream)), 1)
        dashboard = app.dispatch(_request(proxy.dashboard_path))
        self.assertEqual(dashboard.status, 200)

    def test_install_without_an_app_just_returns_a_proxy(self):
        proxy = install_proxyi(None, metrics_path="/m")
        self.assertIsInstance(proxy, ProxyI)
        self.assertEqual(proxy.metrics_path, "/m")


if __name__ == "__main__":
    unittest.main()

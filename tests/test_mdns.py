import ipaddress
import itertools
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from wsbuilder import LocalMDNSServer, MDNSService, discover_mdns_services

try:
    import zeroconf
except ImportError:
    zeroconf = None


SERVICE_TYPE = "_http._tcp.local."
SERVICE_NAME = "Demo." + SERVICE_TYPE


class TestMDNSOptionalDependency(unittest.TestCase):
    def test_core_and_mdns_exports_import_without_zeroconf(self):
        script = """
import sys
sys.modules['zeroconf'] = None
from wsbuilder import LocalDNSServer, LocalMDNSServer, MDNSService, discover_mdns_services
assert LocalMDNSServer().services == ()
assert 'zeroconf' not in sys.modules or sys.modules['zeroconf'] is None
try:
    LocalMDNSServer().start()
except ImportError as exc:
    assert 'pip install wsbuilder[mdns]' in str(exc)
else:
    raise AssertionError('missing zeroconf was not reported')
"""
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_dependency_during_registration(self):
        with patch.dict(sys.modules, {"zeroconf": None}):
            with self.assertRaisesRegex(ImportError, r"wsbuilder\[mdns\]"):
                LocalMDNSServer().register_service("Demo", 8765, "192.0.2.1")

    def test_invalid_transport_configuration(self):
        for kwargs in ({"ip_version": "v5"}, {"interfaces": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LocalMDNSServer(**kwargs)

    def test_registration_validates_before_opening_sockets(self):
        cases = [
            {"name": ""},
            {"name": "á" * 32},
            {"port": 0},
            {"port": 65536},
            {"addresses": []},
            {"addresses": ["0.0.0.0"]},
            {"addresses": ["::"]},
            {"addresses": ["224.0.0.251"]},
            {"addresses": ["bad-ip"]},
            {"hostname": "example.com"},
            {"hostname": "-bad.local"},
            {"service_type": "_http._sctp.local."},
            {"service_type": "_http._tcp.example."},
            {"service_type": "_123._tcp.local."},
            {"service_type": "_a--b._tcp.local."},
            {"service_type": "_abcdefghijklmnop._tcp.local."},
            {"properties": {"": "value"}},
            {"properties": {"key=value": "value"}},
            {"properties": {"clé": "value"}},
            {"properties": {"key": "v" * 252}},
            {"properties": {"key": 42}},
            {"properties": {"Key": "a", "key": "b"}},
        ]
        with patch("wsbuilder.mdns._backend") as backend:
            for overrides in cases:
                kwargs = dict(name="Demo", port=8765, addresses=["192.0.2.1"])
                kwargs.update(overrides)
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    LocalMDNSServer().register_service(**kwargs)
            backend.assert_not_called()

    def test_invalid_timeouts_and_types_do_not_open_sockets(self):
        with patch("wsbuilder.mdns._backend") as backend:
            for timeout in (0, -1, float("nan"), float("inf")):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(ValueError):
                        discover_mdns_services(timeout=timeout)
                    with self.assertRaises(ValueError):
                        LocalMDNSServer().resolve_service("Demo", timeout=timeout)
            with self.assertRaises(ValueError):
                discover_mdns_services("_http._tcp.example.")
            backend.assert_not_called()


@unittest.skipIf(zeroconf is None, "install wsbuilder[mdns] for backend tests")
class TestLocalMDNSServer(unittest.TestCase):
    def setUp(self):
        self.engine = Mock(spec=zeroconf.Zeroconf)
        self.engine.get_service_info.return_value = None
        patcher = patch("zeroconf.Zeroconf", return_value=self.engine)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.server = LocalMDNSServer()
        self.addCleanup(self.server.close)

    def register(self, **kwargs):
        options = dict(
            name="Demo", port=8765, addresses=["192.0.2.1"],
            hostname="demo.local.",
        )
        options.update(kwargs)
        return self.server.register_service(**options)

    def test_registration_publishes_ipv4_ipv6_and_binary_txt(self):
        properties = {"path": "/", "binary": b"\xff", "flag": None, "empty": ""}
        service = self.register(
            name="Café", service_type="_HTTP._TCP",
            addresses=["192.0.2.1", "2001:db8::1", "192.0.2.1"],
            hostname="Demo", properties=properties,
        )
        self.assertIsInstance(service, MDNSService)
        self.assertEqual(service.name, "Café._http._tcp.local.")
        self.assertEqual(service.hostname, "demo.local.")
        self.assertEqual(set(service.addresses), {"192.0.2.1", "2001:db8::1"})
        self.assertEqual(service.properties, {
            b"path": b"/", b"binary": b"\xff", b"flag": None, b"empty": b"",
        })
        info = self.engine.register_service.call_args.args[0]
        self.assertEqual(info.dns_pointer().alias, service.name)
        self.assertEqual(info.dns_service().port, 8765)
        self.assertEqual(info.dns_service().server, "demo.local.")
        self.assertEqual(len(info.dns_addresses()), 2)
        properties["path"] = "/changed"
        self.assertEqual(service.properties[b"path"], b"/")
        with self.assertRaises(TypeError):
            service.properties[b"path"] = b"/changed"
        self.assertEqual(self.server.services, (service,))

    def test_start_is_idempotent_and_supports_selected_transports(self):
        self.assertIs(self.server.start(), self.server)
        self.server.start()
        self.factory.assert_called_once_with(
            interfaces=zeroconf.InterfaceChoice.All,
            ip_version=zeroconf.IPVersion.V4Only,
        )
        for version, expected in (
            ("v6", zeroconf.IPVersion.V6Only), ("all", zeroconf.IPVersion.All),
        ):
            with self.subTest(version=version):
                interfaces = ["192.0.2.1"]
                server = LocalMDNSServer(interfaces=interfaces, ip_version=version)
                interfaces.append("192.0.2.2")
                with server:
                    self.factory.assert_called_with(
                        interfaces=["192.0.2.1"], ip_version=expected,
                    )

    def test_close_withdraws_services_and_allows_restart(self):
        self.register()
        self.server.close()
        self.server.close()
        self.engine.close.assert_called_once()
        self.assertEqual(self.server.services, ())
        self.register()
        self.assertEqual(self.factory.call_count, 2)

    def test_context_closes_when_user_code_fails(self):
        with self.assertRaisesRegex(RuntimeError, "user failure"):
            with self.server:
                self.register()
                raise RuntimeError("user failure")
        self.engine.close.assert_called_once()
        self.assertEqual(self.server.services, ())

    def test_failed_start_can_be_retried(self):
        self.factory.side_effect = [OSError("no interface"), self.engine]
        with self.assertRaisesRegex(OSError, "no interface"):
            self.server.start()
        self.server.start()
        self.register()
        self.assertEqual(len(self.server.services), 1)

    def test_close_failure_clears_local_state(self):
        self.register()
        self.engine.close.side_effect = OSError("close failed")
        with self.assertRaises(OSError):
            self.server.close()
        self.assertEqual(self.server.services, ())
        self.server.close()
        self.engine.close.assert_called_once()

    def test_conflict_propagates_without_registering_locally(self):
        self.engine.register_service.side_effect = zeroconf.NonUniqueNameException()
        with self.assertRaises(zeroconf.NonUniqueNameException):
            self.register()
        self.assertEqual(self.server.services, ())
        self.server.close()
        self.engine.close.assert_called_once()

    def test_automatic_rename_returns_actual_name_and_can_be_withdrawn(self):
        def rename(info, **kwargs):
            self.assertTrue(kwargs["allow_name_change"])
            info.name = "Demo-2." + SERVICE_TYPE

        self.engine.register_service.side_effect = rename
        service = self.register(allow_name_change=True)
        self.assertEqual(service.name, "Demo-2." + SERVICE_TYPE)
        self.assertTrue(self.server.unregister_service(service))
        self.engine.unregister_service.assert_called_once()
        self.assertEqual(self.server.services, ())

    def test_duplicate_registration_is_case_insensitive_and_serialized(self):
        def register(name):
            try:
                return self.register(name=name)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(register, ["Demo", "demo"]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.engine.register_service.assert_called_once()

    def test_unregister_accepts_full_name_and_unknown_service_is_noop(self):
        self.assertFalse(self.server.unregister_service("Unknown"))
        self.factory.assert_not_called()
        self.register()
        self.assertTrue(self.server.unregister_service(SERVICE_NAME.upper()))
        self.assertFalse(self.server.unregister_service("Demo"))
        self.engine.unregister_service.assert_called_once()

    def test_unregister_failure_retains_service_for_retry(self):
        service = self.register()
        self.engine.unregister_service.side_effect = [OSError("send failed"), None]
        with self.assertRaises(OSError):
            self.server.unregister_service(service)
        self.assertEqual(self.server.services, (service,))
        self.assertTrue(self.server.unregister_service(service))

    def test_resolve_service_preserves_ipv6_scope_and_timeout_units(self):
        info = zeroconf.ServiceInfo(
            SERVICE_TYPE, SERVICE_NAME, server="demo.local.", port=8765,
            addresses=[ipaddress.ip_address("fe80::1").packed],
            properties={b"path": b"/"}, interface_index=3,
        )
        self.engine.get_service_info.return_value = info
        service = self.server.resolve_service("Demo", timeout=0.25)
        self.engine.get_service_info.assert_called_once_with(
            SERVICE_TYPE, SERVICE_NAME, timeout=250,
        )
        self.assertEqual(service.addresses, ("fe80::1%3",))
        self.assertEqual(service.interface_index, 3)

    def test_resolve_service_returns_none_on_timeout(self):
        self.assertIsNone(self.server.resolve_service(SERVICE_NAME, timeout=0.01))
        self.engine.get_service_info.assert_called_once_with(
            SERVICE_TYPE, SERVICE_NAME, timeout=10,
        )


@unittest.skipIf(zeroconf is None, "install wsbuilder[mdns] for backend tests")
class TestMDNSDiscovery(unittest.TestCase):
    def setUp(self):
        self.engine = Mock(spec=zeroconf.Zeroconf)
        self.browser = Mock(spec=zeroconf.ServiceBrowser)
        self.info = zeroconf.ServiceInfo(
            SERVICE_TYPE, SERVICE_NAME, server="demo.local.", port=8765,
            parsed_addresses=["192.0.2.1", "2001:db8::1"], properties={"path": "/"},
        )
        self.engine.get_service_info.return_value = self.info
        engine_patcher = patch("zeroconf.Zeroconf", return_value=self.engine)
        browser_patcher = patch("zeroconf.ServiceBrowser", side_effect=self.browse)
        clock_patcher = patch(
            "wsbuilder.mdns.time.monotonic", side_effect=itertools.count(0, 0.001),
        )
        engine_patcher.start()
        self.browser_factory = browser_patcher.start()
        clock_patcher.start()
        self.addCleanup(engine_patcher.stop)
        self.addCleanup(browser_patcher.stop)
        self.addCleanup(clock_patcher.stop)

    def browse(self, engine, service_type, listener):
        self.listener = listener
        listener.add_service(engine, service_type, SERVICE_NAME)
        return self.browser

    def test_discovery_returns_resolved_snapshots_and_closes_resources(self):
        services = discover_mdns_services("_http._tcp", timeout=0.02)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].name, SERVICE_NAME)
        self.assertEqual(set(services[0].addresses), {"192.0.2.1", "2001:db8::1"})
        self.assertLessEqual(self.engine.get_service_info.call_args.kwargs["timeout"], 20)
        self.browser.cancel.assert_called_once()
        self.engine.close.assert_called_once()

    def test_no_services_or_unresolved_service_returns_empty_tuple(self):
        self.engine.get_service_info.return_value = None
        self.assertEqual(discover_mdns_services(timeout=0.01), ())
        self.browser_factory.side_effect = lambda *args, **kwargs: self.browser
        self.assertEqual(discover_mdns_services(timeout=0.01), ())
        self.assertEqual(self.browser.cancel.call_count, 2)
        self.assertEqual(self.engine.close.call_count, 2)

    def test_removed_service_during_resolution_is_not_returned(self):
        def resolve(*args, **kwargs):
            self.listener.remove_service(self.engine, SERVICE_TYPE, SERVICE_NAME)
            return self.info

        self.engine.get_service_info.side_effect = resolve
        self.assertEqual(discover_mdns_services(timeout=0.02), ())

    def test_update_during_resolution_discards_old_result(self):
        updated = zeroconf.ServiceInfo(
            SERVICE_TYPE, SERVICE_NAME, server="demo.local.", port=9999,
            parsed_addresses=["192.0.2.2"],
        )

        def resolve(*args, **kwargs):
            if self.engine.get_service_info.call_count == 1:
                self.listener.update_service(self.engine, SERVICE_TYPE, SERVICE_NAME)
                return self.info
            return updated

        self.engine.get_service_info.side_effect = resolve
        services = discover_mdns_services(timeout=0.02)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].port, 9999)
        self.assertEqual(services[0].addresses, ("192.0.2.2",))

    def test_service_removed_after_resolution_is_not_returned(self):
        other_name = "Other." + SERVICE_TYPE
        other = zeroconf.ServiceInfo(
            SERVICE_TYPE, other_name, server="other.local.", port=9999,
            parsed_addresses=["192.0.2.2"],
        )

        def browse(engine, service_type, listener):
            self.listener = listener
            listener.add_service(engine, service_type, SERVICE_NAME)
            listener.add_service(engine, service_type, other_name)
            return self.browser

        def resolve(service_type, name, **kwargs):
            if name == SERVICE_NAME:
                return self.info
            self.listener.remove_service(self.engine, SERVICE_TYPE, SERVICE_NAME)
            return other

        self.browser_factory.side_effect = browse
        self.engine.get_service_info.side_effect = resolve
        services = discover_mdns_services(timeout=0.02)
        self.assertEqual([service.name for service in services], [other_name])

    def test_browser_failure_closes_engine(self):
        self.browser_factory.side_effect = OSError("browser failed")
        with self.assertRaisesRegex(OSError, "browser failed"):
            discover_mdns_services()
        self.engine.close.assert_called_once()

    def test_resolution_failure_cancels_browser_and_closes_engine(self):
        self.engine.get_service_info.side_effect = OSError("resolution failed")
        with self.assertRaisesRegex(OSError, "resolution failed"):
            discover_mdns_services()
        self.browser.cancel.assert_called_once()
        self.engine.close.assert_called_once()

    def test_browser_cleanup_failure_still_closes_engine(self):
        self.browser.cancel.side_effect = OSError("cancel failed")
        with self.assertRaisesRegex(OSError, "cancel failed"):
            discover_mdns_services(timeout=0.01)
        self.engine.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

import datetime
import os
import socket
import sqlite3
import ssl
import threading
import unittest

from wsbuilder import App
from wsbuilder.pki import (
    CertificateAuthority,
    CertificateManager,
    TLSMaterial,
    install_tls,
)
from wsbuilder.server import HTTPServer

UTC = datetime.timezone.utc


def _at(offset_seconds):
    """A clock frozen at now + offset, for driving rotation deterministically."""
    moment = datetime.datetime.now(tz=UTC) + datetime.timedelta(seconds=offset_seconds)
    return lambda: moment


class TestCertificateAuthority(unittest.TestCase):
    def setUp(self):
        self.ca = CertificateAuthority.create("Test CA", organization="wsbuilder")

    def test_authority_is_a_usable_ca(self):
        from cryptography import x509

        basic = self.ca.certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = self.ca.certificate.extensions.get_extension_for_class(x509.KeyUsage)
        self.assertTrue(basic.value.ca)
        self.assertTrue(basic.critical)
        self.assertTrue(usage.value.key_cert_sign)

    def test_pem_round_trips_through_storage(self):
        restored = CertificateAuthority.load(self.ca.certificate_pem, self.ca.private_key_pem)
        self.assertEqual(restored.certificate_pem, self.ca.certificate_pem)
        # The reloaded key must still sign: that is what makes storage useful.
        leaf = restored.issue("localhost", dns_names=["localhost"])
        self.assertEqual(leaf.subject, "CN=localhost")

    def test_private_key_can_be_stored_encrypted(self):
        encrypted = self.ca.private_key_pem_encrypted("s3cret")
        self.assertIn(b"ENCRYPTED", encrypted)
        with self.assertRaises(Exception):
            CertificateAuthority.load(self.ca.certificate_pem, encrypted)
        reopened = CertificateAuthority.load(self.ca.certificate_pem, encrypted, password="s3cret")
        self.assertEqual(reopened.subject, self.ca.subject)

    def test_issued_certificate_carries_the_names_it_was_asked_for(self):
        leaf = self.ca.issue(
            "api.example.test",
            dns_names=["api.example.test", "alt.example.test"],
            ip_addresses=["127.0.0.1", "::1"],
        )
        self.assertEqual(leaf.dns_names(), ["api.example.test", "alt.example.test"])
        self.assertEqual(leaf.chain_pem, self.ca.certificate_pem)

    def test_common_name_becomes_a_san_when_none_is_given(self):
        # Verifiers stopped honouring the common name, so a SAN is mandatory.
        leaf = self.ca.issue("solo.example.test")
        self.assertEqual(leaf.dns_names(), ["solo.example.test"])

    def test_leaf_never_outlives_its_issuer(self):
        short = CertificateAuthority.create("Short CA", valid_days=1)
        leaf = short.issue("localhost", valid_days=3650)
        self.assertLessEqual(leaf.not_valid_after, short.not_valid_after)

    def test_expired_authority_refuses_to_sign(self):
        past = datetime.datetime.now(tz=UTC) - datetime.timedelta(days=30)
        stale = CertificateAuthority.create("Old CA", valid_days=1, not_before=past)
        with self.assertRaisesRegex(ValueError, "expired"):
            stale.issue("localhost")

    def test_unsupported_key_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported key type"):
            CertificateAuthority.create("X", key_type="magic")

    def test_rsa_keys_are_supported_and_size_checked(self):
        rsa_ca = CertificateAuthority.create("RSA CA", key_type="rsa", rsa_key_size=2048)
        self.assertIn(b"BEGIN CERTIFICATE", rsa_ca.certificate_pem)
        with self.assertRaisesRegex(ValueError, "at least 2048"):
            CertificateAuthority.create("Weak", key_type="rsa", rsa_key_size=512)


class TestMaterialization(unittest.TestCase):
    def setUp(self):
        self.ca = CertificateAuthority.create("Test CA")
        self.leaf = self.ca.issue("localhost", dns_names=["localhost"])

    def test_files_exist_inside_the_block_and_are_gone_after(self):
        with self.leaf.materialize() as files:
            self.assertTrue(os.path.exists(files.certificate))
            self.assertTrue(os.path.exists(files.private_key))
            directory = files.directory
            key_path = files.private_key
        self.assertFalse(os.path.exists(key_path))
        self.assertFalse(os.path.exists(directory))

    def test_private_key_file_is_not_readable_by_others(self):
        with self.leaf.materialize() as files:
            mode = os.stat(files.private_key).st_mode & 0o777
            dir_mode = os.stat(files.directory).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(dir_mode, 0o700)

    def test_certificate_file_carries_the_chain(self):
        with self.leaf.materialize() as files:
            payload = open(files.certificate, "rb").read()
        self.assertEqual(payload.count(b"BEGIN CERTIFICATE"), 2)

    def test_closing_twice_is_harmless(self):
        files = self.leaf.materialize()
        files.close()
        files.close()

    def test_ssl_context_loads_and_leaves_nothing_behind(self):
        before = set(os.listdir("/tmp")) if os.path.isdir("/tmp") else set()
        context = self.leaf.ssl_context()
        self.assertIsInstance(context, ssl.SSLContext)
        if before:
            leftovers = {n for n in os.listdir("/tmp") if n.startswith("wsbuilder-tls-")}
            self.assertEqual(leftovers, set())


class TestCertificateManager(unittest.TestCase):
    def setUp(self):
        self.ca = CertificateAuthority.create("Test CA")

    def test_static_manager_issues_once_and_keeps_it(self):
        manager = CertificateManager(ca=self.ca, common_name="localhost")
        first = manager.current()
        self.assertIs(manager.current(), first)
        self.assertEqual(manager.rotations, 1)

    def test_static_manager_reuses_the_same_context(self):
        manager = CertificateManager(ca=self.ca, common_name="localhost")
        self.assertIs(manager.ssl_context(), manager.ssl_context())

    def test_rotating_manager_reissues_near_expiry(self):
        manager = CertificateManager(
            ca=self.ca,
            common_name="localhost",
            rotate=True,
            valid_days=1,
            renew_before_seconds=3600,
        )
        first = manager.current()
        self.assertEqual(manager.rotations, 1)

        # Still fresh: nothing happens.
        manager._now = _at(600)
        self.assertIs(manager.current(), first)

        # Inside the renewal window: new material, new serial.
        manager._now = _at(86400 - 60)
        second = manager.current()
        self.assertIsNot(second, first)
        self.assertNotEqual(second.serial_number, first.serial_number)
        self.assertEqual(manager.rotations, 2)

    def test_rotation_rebuilds_the_ssl_context(self):
        manager = CertificateManager(ca=self.ca, rotate=True, valid_days=1)
        first_context = manager.ssl_context()
        manager.rotate()
        self.assertIsNot(manager.ssl_context(), first_context)

    def test_static_manager_still_replaces_expired_material(self):
        manager = CertificateManager(ca=self.ca, common_name="localhost", valid_days=1)
        first = manager.current()
        manager._now = _at(86400 * 2)
        self.assertIsNot(manager.current(), first)

    def test_provider_material_is_preferred_over_issuing(self):
        stored = self.ca.issue("localhost", dns_names=["localhost"], valid_days=30)
        calls = []

        def provider():
            calls.append(1)
            return stored

        manager = CertificateManager(ca=self.ca, provider=provider)
        self.assertEqual(manager.current().serial_number, stored.serial_number)
        self.assertEqual(len(calls), 1)
        # Nothing was issued, so nothing counts as a rotation.
        self.assertEqual(manager.rotations, 0)

    def test_stale_provider_material_triggers_a_fresh_issue(self):
        expired = self.ca.issue("localhost", valid_days=1)
        manager = CertificateManager(ca=self.ca, provider=lambda: expired)
        manager._now = _at(86400 * 3)
        self.assertNotEqual(manager.current().serial_number, expired.serial_number)
        self.assertEqual(manager.rotations, 1)

    def test_provider_may_return_plain_pem_pairs(self):
        stored = self.ca.issue("localhost", valid_days=30)
        manager = CertificateManager(
            ca=self.ca,
            provider=lambda: (stored.certificate_pem, stored.private_key_pem, stored.chain_pem),
        )
        self.assertEqual(manager.current().serial_number, stored.serial_number)

    def test_provider_may_return_a_mapping(self):
        stored = self.ca.issue("localhost", valid_days=30)
        manager = CertificateManager(
            ca=self.ca,
            provider=lambda: {
                "certificate_pem": stored.certificate_pem,
                "private_key_pem": stored.private_key_pem,
            },
        )
        self.assertEqual(manager.current().serial_number, stored.serial_number)

    def test_on_rotate_receives_every_new_certificate(self):
        received = []
        manager = CertificateManager(
            ca=self.ca, rotate=True, valid_days=1, on_rotate=received.append
        )
        manager.current()
        manager.rotate()
        self.assertEqual(len(received), 2)
        self.assertTrue(all(isinstance(m, TLSMaterial) for m in received))

    def test_manager_without_any_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "needs material, a provider or a ca"):
            CertificateManager()

    def test_provider_only_manager_cannot_issue_when_material_goes_stale(self):
        expired = self.ca.issue("localhost", valid_days=1)
        manager = CertificateManager(provider=lambda: expired)
        manager._now = _at(86400 * 3)
        with self.assertRaisesRegex(RuntimeError, "no certificate authority"):
            manager.current()

    def test_non_callable_hooks_are_rejected(self):
        with self.assertRaises(TypeError):
            CertificateManager(ca=self.ca, provider="not callable")
        with self.assertRaises(TypeError):
            CertificateManager(ca=self.ca, on_rotate="not callable")

    def test_closed_manager_refuses_to_serve(self):
        manager = CertificateManager(ca=self.ca)
        manager.current()
        manager.close()
        for call in (manager.current, manager.ssl_context, manager.rotate):
            with self.subTest(call=call.__name__):
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    call()

    def test_concurrent_use_issues_exactly_one_certificate(self):
        manager = CertificateManager(ca=self.ca, common_name="localhost")
        seen = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            seen.append(manager.current().serial_number)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(manager.rotations, 1)


class TestDatabaseBackedRotation(unittest.TestCase):
    """The shape the module is built for: PEM lives in a database, reaches
    disk only as a temporary file, and rotation writes the new value back."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE tls (name TEXT PRIMARY KEY, certificate BLOB, key BLOB, chain BLOB)"
        )
        self.ca = CertificateAuthority.create("DB CA")

    def tearDown(self):
        self.db.close()

    def _load(self):
        row = self.db.execute(
            "SELECT certificate, key, chain FROM tls WHERE name = ?", ("localhost",)
        ).fetchone()
        return None if row is None else TLSMaterial(row[0], row[1], row[2])

    def _store(self, material):
        self.db.execute(
            "INSERT OR REPLACE INTO tls VALUES (?, ?, ?, ?)",
            (
                "localhost",
                material.certificate_pem,
                material.private_key_pem,
                material.chain_pem,
            ),
        )
        self.db.commit()

    def test_first_start_issues_and_persists(self):
        manager = CertificateManager(
            ca=self.ca,
            common_name="localhost",
            provider=self._load,
            on_rotate=self._store,
        )
        serial = manager.current().serial_number

        stored = self._load()
        self.assertIsNotNone(stored)
        self.assertEqual(stored.serial_number, serial)

    def test_a_restart_reuses_what_the_database_holds(self):
        first = CertificateManager(
            ca=self.ca, common_name="localhost", provider=self._load, on_rotate=self._store
        )
        serial = first.current().serial_number

        # A second process, same database: no new certificate.
        second = CertificateManager(
            ca=self.ca, common_name="localhost", provider=self._load, on_rotate=self._store
        )
        self.assertEqual(second.current().serial_number, serial)
        self.assertEqual(second.rotations, 0)

    def test_rotation_replaces_the_stored_row(self):
        manager = CertificateManager(
            ca=self.ca,
            common_name="localhost",
            provider=self._load,
            on_rotate=self._store,
            rotate=True,
            valid_days=1,
            renew_before_seconds=3600,
        )
        first = manager.current().serial_number
        manager._now = _at(86400 - 60)
        second = manager.current().serial_number

        self.assertNotEqual(first, second)
        self.assertEqual(self._load().serial_number, second)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM tls").fetchone()[0], 1
        )

    def test_material_from_the_database_encrypts_a_real_connection(self):
        manager = CertificateManager(
            ca=self.ca,
            common_name="localhost",
            dns_names=["localhost"],
            ip_addresses=["127.0.0.1"],
            provider=self._load,
            on_rotate=self._store,
        )
        manager.current()

        app = App()

        @app.api("/secure", methods=("GET",))
        def secure(request):
            return {"tls": bool(request.tls.get("enabled")), "version": request.tls.get("version")}

        server = HTTPServer("127.0.0.1", 0, app, ssl_context=manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(server.stop)
        self.assertTrue(server.wait_until_serving(timeout=5.0))

        client = ssl.create_default_context(cadata=self.ca.certificate_pem.decode())
        client.minimum_version = ssl.TLSVersion.TLSv1_2
        client.minimum_version = ssl.TLSVersion.TLSv1_2
        with socket.create_connection(server.server_address, timeout=5.0) as raw:
            with client.wrap_socket(raw, server_hostname="localhost") as tls:
                tls.sendall(b"GET /secure HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                received = b""
                while True:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    received += chunk

        self.assertIn(b"200 OK", received)
        self.assertIn(b'"tls":true', received)


class TestInstallTLS(unittest.TestCase):
    def test_install_creates_a_throwaway_ca_when_none_is_given(self):
        app = App()
        self.addCleanup(app.close)
        manager = install_tls(app, common_name="localhost")
        self.assertIs(app.tls, manager)
        self.assertIsNotNone(manager.ca)
        self.assertEqual(manager.current().dns_names(), ["localhost"])

    def test_enable_tls_marks_the_app(self):
        app = App()
        self.addCleanup(app.close)
        manager = app.enable_tls(common_name="localhost", rotate=True)
        self.assertTrue(manager.rotating)
        self.assertEqual(manager.describe()["mode"], "rotating")

    def test_server_asks_the_manager_per_connection(self):
        app = App()
        self.addCleanup(app.close)
        manager = app.enable_tls(common_name="localhost")
        server = HTTPServer("127.0.0.1", 0, app, ssl_context=manager)

        first = server._resolve_ssl_context()
        self.assertIsInstance(first, ssl.SSLContext)
        manager.rotate()
        self.assertIsNot(server._resolve_ssl_context(), first)

    def test_a_plain_context_still_works(self):
        app = App()
        self.addCleanup(app.close)
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        server = HTTPServer("127.0.0.1", 0, app, ssl_context=context)
        self.assertIs(server._resolve_ssl_context(), context)
        self.assertIsNone(HTTPServer("127.0.0.1", 0, app)._resolve_ssl_context())


if __name__ == "__main__":
    unittest.main()


class TestOptionalDependency(unittest.TestCase):
    def test_pki_names_resolve_lazily_from_the_package_root(self):
        import wsbuilder

        self.assertIs(wsbuilder.CertificateAuthority, CertificateAuthority)
        self.assertIn("CertificateManager", dir(wsbuilder))

    def test_unknown_attributes_still_raise_attribute_error(self):
        import wsbuilder

        with self.assertRaises(AttributeError):
            wsbuilder.ThisDoesNotExist


class TestMaterialValidation(unittest.TestCase):
    """Regressions from review cycle 1."""

    def setUp(self):
        self.ca = CertificateAuthority.create("Test CA")

    def test_key_that_belongs_to_another_certificate_is_caught_early(self):
        one = self.ca.issue("one.test")
        other = self.ca.issue("other.test")
        # Without the check this only failed inside OpenSSL as
        # [X509: KEY_VALUES_MISMATCH], far from where the pairing broke.
        with self.assertRaisesRegex(ValueError, "does not match the certificate"):
            TLSMaterial(one.certificate_pem, other.private_key_pem)

    def test_matching_material_is_accepted(self):
        good = self.ca.issue("good.test")
        rebuilt = TLSMaterial(good.certificate_pem, good.private_key_pem, good.chain_pem)
        self.assertEqual(rebuilt.serial_number, good.serial_number)

    def test_an_unreadable_key_is_not_reported_as_a_mismatch(self):
        good = self.ca.issue("good.test", key_password="secret")
        # No password supplied here: unverifiable, but not proof of mismatch.
        material = TLSMaterial(good.certificate_pem, good.private_key_pem)
        self.assertEqual(material.subject, "CN=good.test")

    def test_encrypted_key_is_verified_when_the_password_is_known(self):
        good = self.ca.issue("good.test", key_password="secret")
        other = self.ca.issue("other.test", key_password="secret")
        self.assertEqual(
            TLSMaterial(
                good.certificate_pem, good.private_key_pem, key_password="secret"
            ).subject,
            "CN=good.test",
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            TLSMaterial(good.certificate_pem, other.private_key_pem, key_password="secret")

    def test_fractional_validity_is_honoured_not_truncated(self):
        # int(0.5) == 0 produced an authority that was already expired.
        half_day = CertificateAuthority.create("Half CA", valid_days=0.5)
        span = half_day.not_valid_after - half_day.certificate.not_valid_before_utc
        self.assertAlmostEqual(span.total_seconds(), 43200, delta=1)

    def test_non_positive_validity_is_rejected(self):
        for days in (0, -1):
            with self.subTest(days=days):
                with self.assertRaisesRegex(ValueError, "greater than zero"):
                    CertificateAuthority.create("X", valid_days=days)
                with self.assertRaisesRegex(ValueError, "greater than zero"):
                    self.ca.issue("x.test", valid_days=days)

    def test_install_tls_refuses_a_manager_plus_build_arguments(self):
        app = App()
        self.addCleanup(app.close)
        manager = CertificateManager(ca=self.ca, common_name="fixed")
        with self.assertRaisesRegex(TypeError, "not both"):
            install_tls(app, manager=manager, rotate=True)
        with self.assertRaisesRegex(TypeError, "not both"):
            install_tls(app, manager=manager, common_name="other")
        # A manager on its own is still fine.
        self.assertIs(install_tls(app, manager=manager), manager)


class TestMaterializedFileLifetime(unittest.TestCase):
    """Regressions from review cycle 2."""

    def setUp(self):
        self.ca = CertificateAuthority.create("Test CA")
        self.leaf = self.ca.issue("localhost")

    @staticmethod
    def _leftovers():
        import glob
        import tempfile

        return glob.glob(os.path.join(tempfile.gettempdir(), "wsbuilder-tls-*"))

    def test_abandoned_material_is_erased_without_an_explicit_close(self):
        import gc

        before = len(self._leftovers())
        for _ in range(5):
            self.leaf.materialize()  # no close, no surviving reference
        gc.collect()
        self.assertEqual(len(self._leftovers()), before)

    def test_closed_reports_the_state(self):
        files = self.leaf.materialize()
        self.assertFalse(files.closed)
        key_path = files.private_key
        files.close()
        self.assertTrue(files.closed)
        self.assertFalse(os.path.exists(key_path))

    def test_repeated_close_is_idempotent(self):
        files = self.leaf.materialize()
        files.close()
        files.close()
        self.assertTrue(files.closed)

    def test_building_many_contexts_leaves_nothing_behind(self):
        before = len(self._leftovers())
        for _ in range(5):
            self.leaf.ssl_context()
        self.assertEqual(len(self._leftovers()), before)

    def test_concurrent_rotation_and_use_stay_consistent(self):
        manager = CertificateManager(
            ca=self.ca, common_name="localhost", rotate=True, valid_days=1
        )
        errors = []

        def hammer(call):
            for _ in range(15):
                try:
                    call()
                except Exception as exc:  # noqa: BLE001 - recorded, then asserted
                    errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(manager.ssl_context,)) for _ in range(3)]
        threads += [threading.Thread(target=hammer, args=(manager.rotate,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(manager.rotations, 31)


class TestFailureAndIntegration(unittest.TestCase):
    """Regressions from review cycle 3."""

    class _Exploding:
        def ssl_context(self):
            raise RuntimeError("rotation failed")

    def test_a_failed_context_refuses_the_connection_without_leaking_it(self):
        app = App()
        self.addCleanup(app.close)
        app.enable_metrics()
        server = HTTPServer("127.0.0.1", 0, app, ssl_context=self._Exploding())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(server.stop)
        self.assertTrue(server.wait_until_serving(timeout=5.0))

        before = len(os.listdir("/proc/self/fd"))
        for _ in range(4):
            try:
                with socket.create_connection(server.server_address, timeout=2.0) as client:
                    client.recv(16)
            except OSError:
                pass

        deadline = __import__("time").monotonic() + 5.0
        while __import__("time").monotonic() < deadline:
            if app.metrics.total_errors >= 4:
                break
            __import__("time").sleep(0.05)

        # The worker used to die with a traceback and strand the socket.
        self.assertTrue(thread.is_alive())
        self.assertEqual(app.metrics.total_errors, 4)
        self.assertLessEqual(len(os.listdir("/proc/self/fd")) - before, 1)

    def test_metrics_snapshot_carries_the_certificate_state(self):
        app = App()
        self.addCleanup(app.close)
        metrics = app.enable_metrics()
        manager = app.enable_tls(common_name="localhost", rotate=True)
        manager.current()

        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["tls"]["mode"], "rotating")
        self.assertEqual(snapshot["tls"]["certificate"]["dns_names"], ["localhost"])
        self.assertEqual(snapshot["tls"]["rotations"], 1)

    def test_describe_before_first_use_reports_no_certificate_yet(self):
        manager = CertificateManager(ca=CertificateAuthority.create("CA"))
        self.assertIsNone(manager.describe()["certificate"])
        manager.current()
        self.assertIsNotNone(manager.describe()["certificate"])


class TestInstallArgumentHygiene(unittest.TestCase):
    """Regression from review cycle 4."""

    def test_ip_addresses_beside_a_manager_is_refused_too(self):
        app = App()
        self.addCleanup(app.close)
        manager = CertificateManager(ca=CertificateAuthority.create("CA"))
        with self.assertRaisesRegex(TypeError, "ip_addresses"):
            install_tls(app, manager=manager, ip_addresses=["10.0.0.1"])
        # The default value must not trip the check.
        self.assertIs(install_tls(app, manager=manager), manager)


class TestMutualTLS(unittest.TestCase):
    def setUp(self):
        self.ca = CertificateAuthority.create("mTLS CA")
        self.manager = CertificateManager(
            ca=self.ca,
            common_name="localhost",
            dns_names=["localhost"],
            ip_addresses=["127.0.0.1"],
            client_ca_pem=self.ca.certificate_pem,
            require_client_cert=True,
        )
        self.client_cert = self.ca.issue("client", client_auth=True, server_auth=False)

        app = App()
        self.addCleanup(app.close)

        @app.api("/who", methods=("GET",))
        def who(request):
            peer = (request.tls or {}).get("peer_cert") or {}
            return {"subject": str(peer.get("subject", ""))}

        self.server = HTTPServer("127.0.0.1", 0, app, ssl_context=self.manager)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(self.server.stop)
        self.assertTrue(self.server.wait_until_serving(timeout=5.0))

    def _request(self, present_certificate):
        context = ssl.create_default_context(cadata=self.ca.certificate_pem.decode())
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if present_certificate:
            with self.client_cert.materialize() as files:
                context.load_cert_chain(files.certificate, files.private_key)
        with socket.create_connection(self.server.server_address, timeout=5.0) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as tls:
                tls.sendall(b"GET /who HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                received = b""
                while True:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    received += chunk
        return received

    def test_a_client_certificate_reaches_the_handler(self):
        received = self._request(present_certificate=True)
        self.assertIn(b"200 OK", received)
        self.assertIn(b"commonName", received)
        self.assertIn(b"client", received)

    def test_a_client_without_a_certificate_is_refused(self):
        # The refusal reaches the client either as a TLS alert or as a reset,
        # depending on which side tears the socket down first. What matters is
        # that the request does not succeed, not how it fails.
        with self.assertRaises((ssl.SSLError, OSError)):
            self._request(present_certificate=False)

    def test_client_auth_certificates_declare_the_right_usage(self):
        from cryptography import x509

        usage = self.client_cert.certificate.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
        oids = {u.dotted_string for u in usage}
        self.assertIn("1.3.6.1.5.5.7.3.2", oids)  # clientAuth
        self.assertNotIn("1.3.6.1.5.5.7.3.1", oids)  # serverAuth

    def test_a_certificate_needs_at_least_one_usage(self):
        with self.assertRaisesRegex(ValueError, "server_auth, client_auth or both"):
            self.ca.issue("nobody", client_auth=False, server_auth=False)

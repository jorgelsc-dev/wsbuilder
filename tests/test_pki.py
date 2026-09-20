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

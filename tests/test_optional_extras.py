import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

# Makes ``cryptography`` unimportable before wsbuilder is imported, as on an
# install without the ``tls`` extra.
_WITHOUT_CRYPTOGRAPHY = textwrap.dedent(
    """
    import sys

    class _Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None

    sys.meta_path.insert(0, _Blocker())
    """
)


def _run(code):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    return subprocess.run(
        [sys.executable, "-c", _WITHOUT_CRYPTOGRAPHY + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


class TestCoreWithoutTLSExtra(unittest.TestCase):
    def test_star_import_hasattr_and_getattr_do_not_raise(self):
        result = _run(
            """
            import inspect
            import wsbuilder

            namespace = {}
            exec("from wsbuilder import *", namespace)
            assert "App" in namespace
            assert "TLSMaterial" not in namespace
            assert "TLSMaterial" not in wsbuilder.__all__
            assert "TLSMaterial" not in dir(wsbuilder)
            assert hasattr(wsbuilder, "TLSMaterial") is False
            assert getattr(wsbuilder, "CertificateAuthority", None) is None
            inspect.getmembers(wsbuilder)
            try:
                wsbuilder.install_tls
            except AttributeError as exc:
                assert "pip install wsbuilder[tls]" in str(exc), exc
            else:
                raise AssertionError("install_tls resolved without cryptography")
            print("ok")
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_an_unimportable_installed_extra_stops_being_advertised(self):
        result = _run(
            """
            import importlib.util

            real_find_spec = importlib.util.find_spec

            def find_spec(name, *args, **kwargs):
                if name == "cryptography":
                    return object()  # Looks installed, but cannot be imported.
                return real_find_spec(name, *args, **kwargs)

            importlib.util.find_spec = find_spec
            import wsbuilder

            importlib.util.find_spec = real_find_spec
            assert "TLSMaterial" in wsbuilder.__all__
            assert getattr(wsbuilder, "TLSMaterial", None) is None
            assert "TLSMaterial" not in wsbuilder.__all__
            assert "TLSMaterial" not in dir(wsbuilder)
            exec("from wsbuilder import *", {})
            print("ok")
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_rpc_works_over_plain_http_without_the_extra(self):
        result = _run(
            """
            import threading
            from wsbuilder import App, HTTPServer, RpcClient, cbor_dumps, cbor_loads

            assert cbor_loads(cbor_dumps({"a": [1, b"x"]})) == {"a": [1, b"x"]}
            app = App()

            @app.rpc("/echo")
            def echo(_request, payload):
                return payload

            server = HTTPServer("127.0.0.1", 0, app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            assert server._serving.wait(5)
            host, port = server.server_address
            with RpcClient(f"http://{host}:{port}") as client:
                assert client.call("/echo", {"n": 1}) == {"n": 1}
            try:
                RpcClient("https://localhost:1")
            except ImportError as exc:
                assert "pip install wsbuilder[tls]" in str(exc), exc
            else:
                raise AssertionError("HTTP/3 resolved without cryptography")
            server.stop()
            print("ok")
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_tls_names_are_exported_when_the_extra_is_installed(self):
        import wsbuilder

        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography is not installed")
        for name in ("CertificateAuthority", "TLSMaterial", "install_tls", "Http3Client"):
            self.assertIn(name, wsbuilder.__all__)
            self.assertTrue(hasattr(wsbuilder, name))


if __name__ == "__main__":
    unittest.main()

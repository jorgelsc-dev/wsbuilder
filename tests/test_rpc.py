import json
import threading
import time
import unittest

from wsbuilder import (
    App,
    CertificateAuthority,
    CertificateManager,
    HTTPServer,
    Request,
    Response,
    RpcClient,
    RpcError,
    RpcTransportError,
    cbor_dumps,
    cbor_loads,
    rpc_endpoint,
)
from wsbuilder.http3_server import Http3Server
from wsbuilder.rpc import CBOR_MEDIA_TYPE, JSON_MEDIA_TYPE, get_codec


def _request(body=b"", content_type=None, accept=None, path="/rpc"):
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if accept:
        headers["Accept"] = accept
    return Request("POST", path, "", headers, body, ("127.0.0.1", 1))


def _build_app():
    app = App()

    @app.rpc("/math.add")
    def add(_request, payload):
        return {"sum": payload["a"] + payload["b"]}

    @app.rpc("/echo")
    def echo(_request, payload):
        return payload

    @app.rpc("/fail")
    def fail(_request, payload):
        raise RpcError("no such order", code="not_found", status=404, details={"id": payload})

    @app.rpc("/crash")
    def crash(_request, _payload):
        raise RuntimeError("boom")

    @app.rpc("/whoami")
    def whoami(request, _payload):
        peer = (request.tls or {}).get("peer_cert") or {}
        name = peer.get("common_name") if isinstance(peer, dict) else None
        if name is None and peer:
            # The TCP server reports ssl.getpeercert()'s shape.
            name = dict(item[0] for item in peer.get("subject", ()))["commonName"]
        return {"version": request.version, "peer": name}

    @app.rpc("/slow")
    def slow(_request, _payload):
        time.sleep(1.0)
        return True

    return app


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        self.endpoint = rpc_endpoint(lambda _request, payload: {"got": payload})

    def test_cbor_in_cbor_out(self):
        response = self.endpoint(_request(cbor_dumps({"x": b"\x00\x01"}), CBOR_MEDIA_TYPE))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], CBOR_MEDIA_TYPE)
        self.assertEqual(cbor_loads(response.body), {"got": {"x": b"\x00\x01"}})

    def test_json_in_json_out(self):
        response = self.endpoint(_request(b'{"x":1}', "application/json; charset=utf-8"))
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(response.body), {"got": {"x": 1}})

    def test_accept_chooses_the_reply_codec(self):
        response = self.endpoint(_request(b'{"x":1}', JSON_MEDIA_TYPE, accept=CBOR_MEDIA_TYPE))
        self.assertEqual(cbor_loads(response.body), {"got": {"x": 1}})

    def test_an_empty_body_is_none_and_the_default_codec_answers(self):
        response = self.endpoint(_request())
        self.assertEqual(response.headers["Content-Type"], CBOR_MEDIA_TYPE)
        self.assertEqual(cbor_loads(response.body), {"got": None})
        json_default = rpc_endpoint(lambda _r, p: p, default_codec="json")
        self.assertEqual(json_default(_request()).body, b"null")

    def test_an_unknown_content_type_is_415(self):
        response = self.endpoint(_request(b"x=1", "application/x-www-form-urlencoded"))
        self.assertEqual(response.status, 415)
        self.assertEqual(cbor_loads(response.body)["error"]["code"], "unsupported_media_type")

    def test_an_undecodable_body_is_400(self):
        for body, media_type in ((b"\x82\x01", CBOR_MEDIA_TYPE), (b"{nope", JSON_MEDIA_TYPE)):
            with self.subTest(media_type=media_type):
                response = self.endpoint(_request(body, media_type))
                self.assertEqual(response.status, 400)
                error = get_codec(media_type).loads(response.body)["error"]
                self.assertEqual(error["code"], "bad_payload")

    def test_rpc_error_keeps_its_status_code_and_details(self):
        def handler(_request, _payload):
            raise RpcError("over quota", code="quota", status=429, details={"retry_after": 3})

        response = rpc_endpoint(handler)(_request(cbor_dumps(1), CBOR_MEDIA_TYPE))
        self.assertEqual(response.status, 429)
        self.assertEqual(
            cbor_loads(response.body),
            {"error": {"code": "quota", "message": "over quota", "details": {"retry_after": 3}}},
        )

    def test_an_unexpected_exception_is_500_without_internals(self):
        def handler(_request, _payload):
            raise RuntimeError("database password is hunter2")

        response = rpc_endpoint(handler)(_request(cbor_dumps(1), CBOR_MEDIA_TYPE))
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"hunter2", response.body)
        self.assertEqual(cbor_loads(response.body)["error"]["code"], "internal")

    def test_an_unencodable_result_is_500(self):
        response = rpc_endpoint(lambda _r, _p: object())(_request())
        self.assertEqual(response.status, 500)

    def test_a_response_is_passed_through(self):
        response = rpc_endpoint(lambda _r, _p: Response.text("raw", status=202))(_request())
        self.assertEqual((response.status, response.body), (202, b"raw"))

    def test_rpc_error_status_must_be_an_error(self):
        with self.assertRaises(ValueError):
            RpcError("fine", status=200)

    def test_app_rpc_registers_a_post_api_route(self):
        app = _build_app()
        response = app.dispatch(_request(cbor_dumps({"a": 2, "b": 3}), CBOR_MEDIA_TYPE, path="/math.add"))
        self.assertEqual(cbor_loads(response.body), {"sum": 5})
        get = Request("GET", "/math.add", "", {}, b"", ("127.0.0.1", 1))
        self.assertEqual(app.dispatch(get).status, 405)


class TestCodecs(unittest.TestCase):
    def test_lookup_by_name_and_media_type(self):
        self.assertEqual(get_codec("cbor").name, "cbor")
        self.assertEqual(get_codec("application/json; charset=utf-8").name, "json")
        with self.assertRaises(ValueError):
            get_codec("xml")

    def test_json_refuses_bytes_and_nan(self):
        codec = get_codec("json")
        for value in (b"x", float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                codec.dumps(value)


class _ClientContract:
    """Behaviour every transport must show; subclasses provide self.client()."""

    def test_a_call_returns_the_decoded_result(self):
        with self.client() as client:
            self.assertEqual(client.call("/math.add", {"a": 2, "b": 40}), {"sum": 42})

    def test_bytes_and_nesting_survive_the_trip(self):
        payload = {"blob": bytes(range(256)), "nested": [{"k": [1.5, None, True]}], "big": 2**80}
        with self.client() as client:
            self.assertEqual(client.call("/echo", payload), payload)

    def test_an_rpc_error_is_raised_again_on_the_client(self):
        with self.client() as client, self.assertRaises(RpcError) as caught:
            client.call("/fail", 17)
        error = caught.exception
        self.assertEqual((error.status, error.code, error.message), (404, "not_found", "no such order"))
        self.assertEqual(error.details, {"id": 17})

    def test_a_handler_crash_is_a_500_rpc_error(self):
        with self.client() as client, self.assertRaises(RpcError) as caught:
            client.call("/crash")
        self.assertEqual((caught.exception.status, caught.exception.code), (500, "internal"))

    def test_an_unknown_path_is_a_404_rpc_error(self):
        with self.client() as client, self.assertRaises(RpcError) as caught:
            client.call("/does-not-exist", 1)
        self.assertEqual(caught.exception.status, 404)

    def test_json_codec(self):
        with self.client(codec="json") as client:
            self.assertEqual(client.call("/math.add", {"a": 1, "b": 1}), {"sum": 2})

    def test_calls_reuse_the_connection_and_count_bytes(self):
        with self.client() as client:
            for i in range(30):
                self.assertEqual(client.call("/echo", i), i)
            stats = client.describe()
        self.assertEqual(stats["calls"], 30)
        self.assertGreater(stats["bytes_received"], 0)

    def test_concurrent_calls(self):
        errors = []
        with self.client() as client:

            def worker(base):
                try:
                    for i in range(10):
                        if client.call("/math.add", {"a": base, "b": i}) != {"sum": base + i}:
                            errors.append("wrong answer")
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(n * 100,)) for n in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
        self.assertEqual(errors, [])

    def test_a_timeout_is_a_transport_error(self):
        with self.client() as client, self.assertRaises(RpcTransportError):
            client.call("/slow", timeout=0.2)


class TestRpcOverPlainHttp(_ClientContract, unittest.TestCase):
    def setUp(self):
        self.app = _build_app()
        self.addCleanup(self.app.close)
        self.server = HTTPServer("127.0.0.1", 0, self.app)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(self.server.stop)
        self.assertTrue(self.server.wait_until_serving(5.0))

    def client(self, **options):
        host, port = self.server.server_address
        options.setdefault("timeout", 5.0)
        return RpcClient(f"http://{host}:{port}", **options)

    def test_plain_http_defaults_to_http1(self):
        with self.client() as client:
            self.assertEqual(client.transport, "http1")
            self.assertEqual(client.call("/whoami")["version"], "HTTP/1.1")

    def test_a_path_prefix_in_the_base_url_is_kept(self):
        host, port = self.server.server_address
        with RpcClient(f"http://{host}:{port}/", timeout=5) as client:
            self.assertEqual(client.call("math.add", {"a": 1, "b": 2}), {"sum": 3})

    def test_a_server_that_closes_idle_connections_is_reconnected(self):
        server = HTTPServer("127.0.0.1", 0, self.app, keepalive_timeout=0.2)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(server.stop)
        self.assertTrue(server.wait_until_serving(5.0))
        host, port = server.server_address
        with RpcClient(f"http://{host}:{port}", timeout=5.0) as client:
            self.assertEqual(client.call("/echo", 1), 1)
            time.sleep(0.6)  # The server drops the idle kept-alive socket.
            self.assertEqual(client.call("/echo", 2), 2)

    def test_nothing_listening_is_a_transport_error(self):
        with RpcClient("http://127.0.0.1:9", timeout=1.0) as client, self.assertRaises(RpcTransportError):
            client.call("/echo", 1)


class TestRpcClientArguments(unittest.TestCase):
    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            RpcClient("ftp://host")
        with self.assertRaises(ValueError):
            RpcClient("http://host", transport="http3")
        with self.assertRaises(ValueError):
            RpcClient("http://host", transport="carrier-pigeon")
        with self.assertRaises(ValueError):
            RpcClient("https://")


class _TlsFixture:
    def start_servers(self, *, mutual):
        self.ca = CertificateAuthority.create("RPC Test CA")
        options = {}
        if mutual:
            options = {"client_ca_pem": self.ca.certificate_pem, "require_client_cert": True}
        self.manager = CertificateManager(
            ca=self.ca, common_name="localhost", dns_names=["localhost"], **options
        )
        self.app = _build_app()
        self.addCleanup(self.app.close)
        self.h3 = Http3Server("127.0.0.1", 0, self.app, self.manager)
        self.tcp = HTTPServer("127.0.0.1", 0, self.app, ssl_context=self.manager.ssl_context())
        for server in (self.h3, self.tcp):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5.0)
            self.addCleanup(server.stop)
            self.assertTrue(server.wait_until_serving(5.0))
        self.client_material = self.ca.issue("orders-service", client_auth=True, server_auth=False)

    def make_client(self, transport, **options):
        port = (self.h3 if transport == "http3" else self.tcp).server_address[1]
        options.setdefault("trust", self.ca)
        options.setdefault("timeout", 5.0)
        return RpcClient(
            f"https://127.0.0.1:{port}", transport=transport, server_name="localhost", **options
        )


class TestRpcOverHttp3(_TlsFixture, _ClientContract, unittest.TestCase):
    def setUp(self):
        self.start_servers(mutual=False)

    def client(self, **options):
        return self.make_client("http3", **options)

    def test_https_defaults_to_http3(self):
        port = self.h3.server_address[1]
        with RpcClient(f"https://127.0.0.1:{port}", server_name="localhost", trust=self.ca) as client:
            self.assertEqual(client.transport, "http3")
            self.assertEqual(client.call("/whoami")["version"], "HTTP/3")

    def test_an_untrusted_server_is_refused(self):
        other = CertificateAuthority.create("Not Our CA")
        with self.client(trust=other) as client, self.assertRaises(RpcTransportError):
            client.call("/echo", 1)

    def test_ca_pem_is_accepted_as_trust(self):
        with self.client(trust=None, ca_pem=self.ca.certificate_pem) as client:
            self.assertEqual(client.call("/echo", "ok"), "ok")


class TestRpcOverHttpsHttp1(_TlsFixture, _ClientContract, unittest.TestCase):
    def setUp(self):
        self.start_servers(mutual=False)

    def client(self, **options):
        return self.make_client("http1", **options)

    def test_an_untrusted_server_is_refused(self):
        other = CertificateAuthority.create("Not Our CA")
        with self.client(trust=other) as client, self.assertRaises(RpcTransportError):
            client.call("/echo", 1)


class TestMutualTls(_TlsFixture, unittest.TestCase):
    def setUp(self):
        self.start_servers(mutual=True)

    def test_both_transports_identify_the_calling_service(self):
        for transport, version in (("http3", "HTTP/3"), ("http1", "HTTP/1.1")):
            with self.subTest(transport=transport):
                with self.make_client(transport, client_material=self.client_material) as client:
                    self.assertEqual(
                        client.call("/whoami"), {"version": version, "peer": "orders-service"}
                    )

    def test_a_caller_without_a_certificate_is_refused(self):
        for transport in ("http3", "http1"):
            with self.subTest(transport=transport):
                with self.make_client(transport) as client, self.assertRaises(RpcTransportError):
                    client.call("/whoami")

    def test_a_certificate_from_another_authority_is_refused(self):
        stranger = CertificateAuthority.create("Stranger").issue(
            "intruder", client_auth=True, server_auth=False
        )
        for transport in ("http3", "http1"):
            with self.subTest(transport=transport):
                with self.make_client(transport, client_material=stranger) as client:
                    with self.assertRaises(RpcTransportError):
                        client.call("/whoami")


if __name__ == "__main__":
    unittest.main()

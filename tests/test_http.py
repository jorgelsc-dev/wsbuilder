import unittest

from wsbuilder import App, Request, Response, parse_query_string
from wsbuilder.http import parse_http_request, send_http_response


class BufferSocket:
    def __init__(self, data=b""):
        self.buffer = bytearray(data)
        self.sent = []

    def recv(self, size):
        if not self.buffer:
            return b""
        chunk = bytes(self.buffer[:size])
        del self.buffer[:size]
        return chunk

    def sendall(self, data):
        self.sent.append(bytes(data))


class TestHTTPCore(unittest.TestCase):
    def test_query_string_decodes_standard_url_encoding(self):
        parsed = parse_query_string(
            "name=Ana+Mar%C3%ADa&empty=&flag&name=final"
        )

        self.assertEqual(
            parsed,
            {
                "name": "final",
                "empty": "",
                "flag": "",
            },
        )

    def test_request_parser_rejects_incomplete_and_ambiguous_headers(self):
        with self.assertRaisesRegex(ValueError, "Incomplete request headers"):
            parse_http_request(
                BufferSocket(b"GET / HTTP/1.1\r\nHost: example.test\r\n")
            )

        with self.assertRaisesRegex(ValueError, "Duplicate Content-Length"):
            parse_http_request(
                BufferSocket(
                    b"POST / HTTP/1.1\r\n"
                    b"Host: example.test\r\n"
                    b"Content-Length: 1\r\n"
                    b"Content-Length: 2\r\n\r\n"
                )
            )

    def test_request_parser_requires_host_for_http_11(self):
        with self.assertRaisesRegex(ValueError, "Missing Host"):
            parse_http_request(BufferSocket(b"GET / HTTP/1.1\r\n\r\n"))

    def test_response_rejects_header_injection(self):
        with self.assertRaises(ValueError):
            Response.text(
                "body",
                headers={"X-Test": "safe\r\nX-Injected: true"},
            )
        with self.assertRaises(ValueError):
            Response(status=200, reason="OK\r\nX-Injected: true")

        response = Response.text("body")
        response.headers["X-Test"] = "safe\r\nX-Injected: true"
        with self.assertRaises(ValueError):
            send_http_response(BufferSocket(), response)

    def test_head_transport_keeps_length_but_suppresses_body(self):
        conn = BufferSocket()
        send_http_response(
            conn,
            Response.text("payload"),
            send_body=False,
        )

        wire = b"".join(conn.sent)
        headers, body = wire.split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Length: 7", headers)
        self.assertEqual(body, b"")

    def test_status_without_payload_never_emits_body_framing(self):
        conn = BufferSocket()
        send_http_response(
            conn,
            Response(status=204, body=b"not-allowed"),
        )

        wire = b"".join(conn.sent)
        headers, body = wire.split(b"\r\n\r\n", 1)
        self.assertNotIn(b"Content-Length", headers)
        self.assertNotIn(b"Transfer-Encoding", headers)
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()


class TestRouterDispatch(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def tearDown(self):
        self.app.close()

    def _request(self, method, path):
        return Request(
            method=method,
            path=path,
            query_string="",
            headers={},
            body=b"",
            client=("127.0.0.1", 1234),
            tls={},
        )

    def test_explicit_options_route_receives_the_request(self):
        seen = []

        @self.app.api("/api/items", methods=("OPTIONS",))
        def options_handler(_request):
            seen.append("handler")
            return Response.text("custom", status=204, headers={"Allow": "GET, OPTIONS"})

        response = self.app.dispatch(self._request("OPTIONS", "/api/items"))

        self.assertEqual(seen, ["handler"])
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers.get("Allow"), "GET, OPTIONS")

    def test_options_without_explicit_route_still_answers_automatically(self):
        @self.app.api("/api/items", methods=("GET", "POST"))
        def items(_request):
            return {"ok": True}

        response = self.app.dispatch(self._request("OPTIONS", "/api/items"))

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.headers["Access-Control-Allow-Methods"],
            "GET, HEAD, OPTIONS, POST",
        )

    def test_router_index_keeps_registration_order_and_head_fallback(self):
        @self.app.api("/dup", methods=("GET",))
        def first(_request):
            return {"which": "first"}

        @self.app.api("/dup", methods=("GET", "POST"))
        def second(_request):
            return {"which": "second"}

        self.assertIs(self.app.router.resolve("/dup", "GET"), self.app.router.routes[0])
        self.assertIs(self.app.router.resolve("/dup", "POST"), self.app.router.routes[1])
        self.assertIs(self.app.router.resolve("/dup", "HEAD"), self.app.router.routes[0])
        self.assertIsNone(self.app.router.resolve("/missing", "GET"))
        self.assertEqual(
            self.app.router.allowed_methods("/dup"),
            {"GET", "HEAD", "POST", "OPTIONS"},
        )
        self.assertEqual(self.app.router.allowed_methods("/missing"), set())

    def test_unknown_method_on_known_path_returns_405_with_allow(self):
        @self.app.api("/only-get", methods=("GET",))
        def only_get(_request):
            return {"ok": True}

        response = self.app.dispatch(self._request("DELETE", "/only-get"))

        self.assertEqual(response.status, 405)
        self.assertEqual(response.headers["Allow"], "GET, HEAD, OPTIONS")

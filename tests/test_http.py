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
    def test_a_whole_response_leaves_in_one_write(self):
        # Head and body in separate small writes meet Nagle's algorithm here
        # and delayed ACKs at the client: ~40 ms per kept-alive response.
        sock = BufferSocket()
        send_http_response(sock, Response.json({"ok": True}), keep_alive=True)
        self.assertEqual(len(sock.sent), 1)
        self.assertTrue(sock.sent[0].endswith(b'{"ok":true}'))

    def test_a_head_response_is_one_write_without_the_body(self):
        sock = BufferSocket()
        send_http_response(sock, Response.text("hidden"), send_body=False)
        self.assertEqual(len(sock.sent), 1)
        self.assertNotIn(b"hidden", sock.sent[0])

    def _closing_stream(self, flags):
        def chunks():
            try:
                yield b"one"
                yield b"two"
            finally:
                flags.append("closed")

        return chunks()

    def test_stream_is_closed_when_the_client_goes_away(self):
        class Broken:
            def __init__(self):
                self.calls = 0

            def sendall(self, _data):
                self.calls += 1
                if self.calls > 1:
                    raise BrokenPipeError("gone")

        flags = []
        stream = self._closing_stream(flags)
        self.assertFalse(send_http_response(Broken(), Response.stream(stream)))
        self.assertEqual(flags, ["closed"])

    def test_stream_is_closed_when_the_body_is_skipped(self):
        flags = []
        stream = self._closing_stream(flags)
        next(stream)  # started, so close() runs its finally block
        send_http_response(BufferSocket(), Response.stream(stream), send_body=False)
        self.assertEqual(flags, ["closed"])

    def test_list_header_values_are_sent_as_separate_lines(self):
        sock = BufferSocket()
        response = Response.text("ok", headers={"Set-Cookie": ["a=1; Path=/", "b=2"]})
        send_http_response(sock, response)
        head = b"".join(sock.sent).split(b"\r\n\r\n", 1)[0].split(b"\r\n")
        self.assertIn(b"Set-Cookie: a=1; Path=/", head)
        self.assertIn(b"Set-Cookie: b=2", head)
        self.assertEqual(sum(1 for line in head if line.startswith(b"Set-Cookie:")), 2)

    def test_list_connection_header_value_is_still_understood(self):
        sock = BufferSocket()
        response = Response.text("ok", headers={"Connection": ["close"]})
        send_http_response(sock, response, keep_alive=True)
        head = b"".join(sock.sent).split(b"\r\n\r\n", 1)[0]
        self.assertEqual(head.count(b"Connection:"), 1)

    def test_request_json_returns_none_for_unparseable_bodies(self):
        def request(body):
            return Request(
                method="POST",
                path="/",
                query_string="",
                headers={},
                body=body,
                client=("127.0.0.1", 1234),
                tls={},
            )

        self.assertEqual(request(b'{"ok": true}').json(), {"ok": True})
        for body in (b"", b"{not json", b"[" * 100_000):
            with self.subTest(body=body[:16]):
                self.assertIsNone(request(body).json())

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

    def test_request_parser_accepts_absolute_form(self):
        parsed = parse_http_request(
            BufferSocket(b"GET http://origin.test:8080/items?x=1 HTTP/1.1\r\nHost: other\r\n\r\n")
        )
        self.assertEqual(parsed["path"], "/items?x=1")
        # RFC 9112 section 3.2.2: the target's authority replaces Host.
        self.assertEqual(parsed["headers"]["host"], "origin.test:8080")
        bare = parse_http_request(BufferSocket(b"GET https://origin.test HTTP/1.1\r\nHost: x\r\n\r\n"))
        self.assertEqual(bare["path"], "/")

    def test_request_parser_rejects_bare_lf_at_once(self):
        for raw in (
            b"GET / HTTP/1.1\nHost: x\n\n",
            b"GET / HTTP/1.1\r\nHost: x\nX: y\r\n\r\n",
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "CRLF"):
                parse_http_request(BufferSocket(raw))

    def test_request_parser_keeps_methods_case_sensitive(self):
        with self.assertRaisesRegex(ValueError, "case-sensitive"):
            parse_http_request(BufferSocket(b"get / HTTP/1.1\r\nHost: x\r\n\r\n"))

    def test_request_parser_validates_the_request_target(self):
        star = parse_http_request(BufferSocket(b"OPTIONS * HTTP/1.1\r\nHost: x\r\n\r\n"))
        self.assertEqual(star["path"], "*")
        for raw in (
            b"GET * HTTP/1.1\r\nHost: x\r\n\r\n",
            b"GET items HTTP/1.1\r\nHost: x\r\n\r\n",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_http_request(BufferSocket(raw))

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

    def test_not_modified_keeps_explicit_representation_length_without_a_body(self):
        conn = BufferSocket()
        self.assertTrue(send_http_response(
            conn, Response(status=304, body=b"ignored", headers={"Content-Length": "100"}),
        ))
        headers, body = b"".join(conn.sent).split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Length: 100", headers)
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

        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers["Allow"], "GET, HEAD, OPTIONS, POST")
        # CORS is off by default, so no Access-Control-* headers are sent.
        self.assertNotIn("Access-Control-Allow-Methods", response.headers)

    def test_options_preflight_carries_cors_headers_when_enabled(self):
        app = App(cors_allow_origin="https://app.example")
        self.addCleanup(app.close)

        @app.api("/api/items", methods=("GET", "POST"))
        def items(_request):
            return {"ok": True}

        response = app.dispatch(self._request("OPTIONS", "/api/items"))
        self.assertEqual(response.status, 204)
        self.assertEqual(
            response.headers["Access-Control-Allow-Methods"], "GET, HEAD, OPTIONS, POST"
        )
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://app.example")
        self.assertEqual(response.headers["Vary"], "Origin")

    def test_options_asterisk_describes_the_server(self):
        response = self.app.dispatch(self._request("OPTIONS", "*"))
        self.assertEqual(response.status, 204)
        self.assertIn("GET", response.headers["Allow"])

    def test_router_captures_and_converts_path_parameters(self):
        @self.app.api("/users/{user_id:int}")
        def user(request):
            return {"id": request.path_params["user_id"]}

        @self.app.api("/files/{rest:path}")
        def files(request):
            return {"rest": request.path_params["rest"]}

        @self.app.api("/tags/{name}", methods=("GET", "DELETE"))
        def tag(request):
            return {"name": request.path_params["name"]}

        response = self.app.dispatch(self._request("GET", "/users/42"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b'{"id":42}')
        self.assertEqual(self.app.dispatch(self._request("GET", "/users/abc")).status, 404)
        self.assertEqual(
            self.app.dispatch(self._request("GET", "/files/a/b.txt")).body, b'{"rest":"a/b.txt"}'
        )
        self.assertEqual(self.app.dispatch(self._request("GET", "/tags/x")).body, b'{"name":"x"}')
        self.assertEqual(self.app.dispatch(self._request("GET", "/tags/x/y")).status, 404)
        not_allowed = self.app.dispatch(self._request("POST", "/tags/x"))
        self.assertEqual(not_allowed.status, 405)
        self.assertEqual(not_allowed.headers["Allow"], "DELETE, GET, HEAD, OPTIONS")

    def test_static_routes_win_over_patterns(self):
        @self.app.api("/users/{name}")
        def by_name(request):
            return {"who": request.path_params["name"]}

        @self.app.api("/users/me")
        def me(_request):
            return {"who": "me"}

        self.assertEqual(self.app.dispatch(self._request("GET", "/users/me")).body, b'{"who":"me"}')
        self.assertEqual(self.app.dispatch(self._request("GET", "/users/ana")).body, b'{"who":"ana"}')

    def test_invalid_route_patterns_are_refused(self):
        for path in ("/a/{", "/a/{1x}", "/a/{x}/{x}", "/a/{x:float}"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.app.api(path)(lambda _r: {})

    def test_trailing_slash_redirects_to_the_registered_spelling(self):
        @self.app.api("/items")
        def items(_request):
            return {"ok": True}

        request = self._request("GET", "/items/")
        request.query_string = "page=2"
        response = self.app.dispatch(request)
        self.assertEqual(response.status, 308)
        self.assertEqual(response.headers["Location"], "/items?page=2")
        # A double slash would become a scheme-relative (open) redirect.
        self.assertEqual(self.app.dispatch(self._request("GET", "//items/")).status, 404)

        strict = App(redirect_slashes=False)
        self.addCleanup(strict.close)
        strict.api("/items")(items)
        self.assertEqual(strict.dispatch(self._request("GET", "/items/")).status, 404)

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

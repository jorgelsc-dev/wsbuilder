import unittest

from wsbuilder import Response, parse_query_string
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

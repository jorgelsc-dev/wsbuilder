import unittest

from wsbuilder import Response
from wsbuilder.http1 import (
    HTTP_0_9,
    HTTP_1_0,
    HTTP_1_1,
    BufferedReader,
    client_wants_keep_alive,
    connection_header_value,
    encode_chunk,
    encode_last_chunk,
    parse_chunk_size,
    parse_request_line,
    read_chunked_body,
    read_trailer_section,
    response_is_self_delimiting,
    should_keep_alive,
)


class FakeSocket:
    """Hands out the payload in fixed slices to exercise partial reads."""

    def __init__(self, payload, slice_size=7):
        self.remaining = bytes(payload)
        self.slice_size = slice_size

    def recv(self, size):
        if not self.remaining:
            return b""
        take = min(size, self.slice_size, len(self.remaining))
        chunk, self.remaining = self.remaining[:take], self.remaining[take:]
        return chunk


def _reader(payload, buffered=b"", slice_size=7):
    return BufferedReader(FakeSocket(payload, slice_size), buffered)


class TestBufferedReader(unittest.TestCase):
    def test_read_exactly_spans_several_socket_reads(self):
        reader = _reader(b"abcdefghijklmnop", slice_size=3)
        self.assertEqual(reader.read_exactly(10), b"abcdefghij")
        self.assertEqual(reader.read_exactly(6), b"klmnop")

    def test_read_exactly_consumes_the_pushback_buffer_first(self):
        reader = _reader(b"world", buffered=b"hello ")
        self.assertEqual(reader.read_exactly(11), b"hello world")

    def test_read_exactly_raises_when_the_peer_stops_sending(self):
        reader = _reader(b"abc")
        with self.assertRaises(ConnectionError):
            reader.read_exactly(8)

    def test_read_line_splits_on_crlf_and_drops_it(self):
        reader = _reader(b"first\r\nsecond\r\n")
        self.assertEqual(reader.read_line(64), b"first")
        self.assertEqual(reader.read_line(64), b"second")

    def test_read_line_rejects_a_line_over_the_limit(self):
        reader = _reader(b"x" * 200 + b"\r\n")
        with self.assertRaisesRegex(ValueError, "exceeds limit"):
            reader.read_line(16)


class TestRequestLine(unittest.TestCase):
    def test_versioned_request_lines(self):
        self.assertEqual(parse_request_line(b"GET / HTTP/1.1"), ("GET", "/", HTTP_1_1))
        self.assertEqual(parse_request_line(b"POST /x HTTP/1.0"), ("POST", "/x", HTTP_1_0))

    def test_version_less_line_is_http_0_9(self):
        self.assertEqual(parse_request_line(b"GET /index.html"), ("GET", "/index.html", HTTP_0_9))

    def test_http_0_9_only_defines_get(self):
        with self.assertRaisesRegex(ValueError, "only supports GET"):
            parse_request_line(b"POST /x")

    def test_unknown_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported HTTP version"):
            parse_request_line(b"GET / HTTP/1.2")


class TestChunkedDecoding(unittest.TestCase):
    def test_single_chunk(self):
        body, trailers = read_chunked_body(_reader(b"5\r\nhello\r\n0\r\n\r\n"), max_body_bytes=1024)
        self.assertEqual(body, b"hello")
        self.assertEqual(trailers, {})

    def test_several_chunks_are_concatenated(self):
        payload = b"3\r\nabc\r\n4\r\ndefg\r\n1\r\nh\r\n0\r\n\r\n"
        body, _ = read_chunked_body(_reader(payload, slice_size=2), max_body_bytes=1024)
        self.assertEqual(body, b"abcdefgh")

    def test_chunk_extensions_are_ignored(self):
        self.assertEqual(parse_chunk_size(b"1a;name=value;flag"), 26)
        body, _ = read_chunked_body(
            _reader(b"5;foo=bar\r\nhello\r\n0\r\n\r\n"), max_body_bytes=1024
        )
        self.assertEqual(body, b"hello")

    def test_trailers_are_collected(self):
        payload = b"4\r\nbody\r\n0\r\nX-Checksum: abc123\r\nX-Note: ok\r\n\r\n"
        body, trailers = read_chunked_body(_reader(payload), max_body_bytes=1024)
        self.assertEqual(body, b"body")
        self.assertEqual(trailers, {"x-checksum": "abc123", "x-note": "ok"})

    def test_framing_headers_are_refused_in_trailers(self):
        payload = b"0\r\nContent-Length: 5\r\n\r\n"
        with self.assertRaisesRegex(ValueError, "not allowed in trailers"):
            read_chunked_body(_reader(payload), max_body_bytes=1024)

    def test_body_over_the_limit_is_rejected(self):
        payload = b"10\r\n" + b"x" * 16 + b"\r\n0\r\n\r\n"
        with self.assertRaisesRegex(ValueError, "Payload Too Large"):
            read_chunked_body(_reader(payload), max_body_bytes=8)

    def test_limit_counts_the_whole_body_not_one_chunk(self):
        payload = b"8\r\n" + b"x" * 8 + b"\r\n8\r\n" + b"y" * 8 + b"\r\n0\r\n\r\n"
        with self.assertRaisesRegex(ValueError, "Payload Too Large"):
            read_chunked_body(_reader(payload), max_body_bytes=12)

    def test_missing_chunk_terminator_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Malformed chunk terminator"):
            read_chunked_body(_reader(b"2\r\nabXX0\r\n\r\n"), max_body_bytes=1024)

    def test_non_hex_chunk_size_is_rejected(self):
        for bad in (b"zz", b"", b"-1", b"1" * 20):
            with self.subTest(size=bad):
                with self.assertRaises(ValueError):
                    parse_chunk_size(bad)

    def test_oversized_trailer_section_is_rejected(self):
        trailer = b"".join(b"X-Pad-%d: %s\r\n" % (i, b"v" * 100) for i in range(200))
        with self.assertRaisesRegex(ValueError, "Trailer section too large"):
            read_trailer_section(_reader(trailer + b"\r\n"), max_trailer_bytes=1024)


class TestChunkedEncoding(unittest.TestCase):
    def test_chunk_carries_a_hex_length(self):
        self.assertEqual(encode_chunk(b"hello"), b"5\r\nhello\r\n")
        self.assertEqual(encode_chunk(b"x" * 26), b"1A\r\n" + b"x" * 26 + b"\r\n")

    def test_empty_payload_produces_nothing(self):
        # An empty chunk would terminate the body early.
        self.assertEqual(encode_chunk(b""), b"")

    def test_terminator(self):
        self.assertEqual(encode_last_chunk(), b"0\r\n\r\n")


class TestPersistence(unittest.TestCase):
    def test_http_1_1_keeps_the_connection_by_default(self):
        self.assertTrue(client_wants_keep_alive(HTTP_1_1, {}))
        self.assertFalse(client_wants_keep_alive(HTTP_1_1, {"connection": "close"}))

    def test_http_1_0_needs_an_explicit_opt_in(self):
        self.assertFalse(client_wants_keep_alive(HTTP_1_0, {}))
        self.assertTrue(client_wants_keep_alive(HTTP_1_0, {"connection": "keep-alive"}))
        self.assertFalse(client_wants_keep_alive(HTTP_1_0, {"connection": "keep-alive, close"}))

    def test_http_0_9_has_no_persistence(self):
        self.assertFalse(client_wants_keep_alive(HTTP_0_9, {}))

    def test_connection_token_matching_ignores_case_and_spacing(self):
        self.assertFalse(client_wants_keep_alive(HTTP_1_1, {"Connection": "  CLOSE  "}))

    def test_a_buffered_response_is_self_delimiting(self):
        self.assertTrue(response_is_self_delimiting(Response.text("body")))

    def test_an_unframed_stream_forces_the_connection_shut(self):
        streamed = Response.stream(iter([b"a", b"b"]))
        streamed.headers.pop("Transfer-Encoding", None)
        self.assertFalse(response_is_self_delimiting(streamed))
        self.assertTrue(
            response_is_self_delimiting(
                Response.stream(iter([b"a"]), headers={"Transfer-Encoding": "chunked"})
            )
        )

    def test_should_keep_alive_requires_client_server_and_framing_to_agree(self):
        ok = Response.text("ok")
        self.assertTrue(should_keep_alive(HTTP_1_1, {}, ok))
        self.assertFalse(should_keep_alive(HTTP_1_1, {}, ok, server_allows=False))
        self.assertFalse(should_keep_alive(HTTP_1_1, {"connection": "close"}, ok))
        self.assertFalse(
            should_keep_alive(HTTP_1_1, {}, Response.text("x", headers={"Connection": "close"}))
        )

    def test_a_head_response_stays_framed_even_when_streamed(self):
        streamed = Response.stream(iter([b"a"]))
        streamed.headers.pop("Transfer-Encoding", None)
        self.assertTrue(should_keep_alive(HTTP_1_1, {}, streamed, send_body=False))

    def test_connection_header_spells_keep_alive_only_for_http_1_0(self):
        self.assertIsNone(connection_header_value(HTTP_1_1, True))
        self.assertEqual(connection_header_value(HTTP_1_0, True), "keep-alive")
        self.assertEqual(connection_header_value(HTTP_1_1, False), "close")


if __name__ == "__main__":
    unittest.main()

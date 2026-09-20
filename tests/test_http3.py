import unittest

from wsbuilder import App, Response
from wsbuilder.http3 import (
    FRAME_DATA,
    FRAME_GOAWAY,
    FRAME_HEADERS,
    FRAME_SETTINGS,
    H3Error,
    RequestStream,
    build_control_stream,
    build_request,
    build_response_frames,
    decode_settings,
    encode_frame,
    is_reserved_frame_type,
    parse_frames,
    validate_request_headers,
)
from wsbuilder.qpack import (
    STATIC_TABLE_SIZE,
    QPACKError,
    decode_field_section,
    encode_field_section,
)
from wsbuilder.quic.varint import decode_varint


def request_fields(path="/hi", method="GET"):
    return [
        (":method", method),
        (":scheme", "https"),
        (":path", path),
        (":authority", "example.test"),
    ]


class TestQPACK(unittest.TestCase):
    def test_the_static_table_has_the_published_size(self):
        self.assertEqual(STATIC_TABLE_SIZE, 99)

    def test_round_trip(self):
        for fields in (
            request_fields(),
            [(":status", "200"), ("content-type", "application/json")],
            [(":status", "404")],
            [("x-custom", "value"), ("another", "one")],
        ):
            with self.subTest(first=fields[0]):
                self.assertEqual(decode_field_section(encode_field_section(fields)), fields)

    def test_static_entries_compress_hard(self):
        # ":status 404" is one static index plus the two prefix octets.
        self.assertEqual(len(encode_field_section([(":status", "404")])), 3)

    def test_names_are_lowercased(self):
        self.assertEqual(
            decode_field_section(encode_field_section([("Content-Type", "text/plain")])),
            [("content-type", "text/plain")],
        )

    def test_a_dynamic_table_reference_is_refused(self):
        # We advertise a capacity of zero, so a peer must not ask for one.
        with self.assertRaisesRegex(QPACKError, "dynamic table"):
            decode_field_section(b"\x00\x00\x80")

    def test_a_blocking_prefix_is_refused(self):
        with self.assertRaisesRegex(QPACKError, "requires dynamic table"):
            decode_field_section(b"\x05\x00")

    def test_a_string_past_the_end_is_refused(self):
        with self.assertRaisesRegex(QPACKError, "past the field section"):
            decode_field_section(b"\x00\x00" + b"\x2f" + b"short")

    def test_huffman_is_used_only_when_it_helps(self):
        long_value = "a" * 100
        encoded = encode_field_section([("x", long_value)])
        self.assertEqual(decode_field_section(encoded), [("x", long_value)])
        self.assertLess(len(encoded), len(long_value) + 10)


class TestFrameLayer(unittest.TestCase):
    def test_round_trip(self):
        payload = encode_frame(FRAME_DATA, b"body") + encode_frame(FRAME_HEADERS, b"block")
        frames, tail = parse_frames(payload)
        self.assertEqual(frames, [(FRAME_DATA, b"body"), (FRAME_HEADERS, b"block")])
        self.assertEqual(tail, b"")

    def test_a_partial_frame_is_kept_for_later(self):
        whole = encode_frame(FRAME_DATA, b"body")
        frames, tail = parse_frames(whole[:-2])
        self.assertEqual(frames, [])
        self.assertEqual(tail, whole[:-2])

    def test_settings_round_trip(self):
        from wsbuilder.http3 import encode_settings

        values = {0x01: 0, 0x06: 65536}
        self.assertEqual(decode_settings(encode_settings(values)), values)

    def test_a_duplicate_setting_is_refused(self):
        with self.assertRaisesRegex(H3Error, "duplicate setting"):
            decode_settings(b"\x06\x40\x00\x06\x40\x01")

    def test_reserved_frame_types_are_recognised(self):
        # They exist so peers prove they ignore what they do not know.
        self.assertTrue(is_reserved_frame_type(0x21))
        self.assertTrue(is_reserved_frame_type(0x21 + 0x1F))
        self.assertFalse(is_reserved_frame_type(FRAME_DATA))

    def test_the_control_stream_opens_with_its_type_then_settings(self):
        stream = build_control_stream()
        stream_type, offset = decode_varint(stream)
        self.assertEqual(stream_type, 0x00)
        frames, _tail = parse_frames(stream[offset:])
        self.assertEqual(frames[0][0], FRAME_SETTINGS)
        settings = decode_settings(frames[0][1])
        # A capacity of zero is what lets us decode without blocking.
        self.assertEqual(settings[0x01], 0)
        self.assertEqual(settings[0x07], 0)


class TestRequestStream(unittest.TestCase):
    def test_a_complete_get(self):
        stream = RequestStream(0)
        done = stream.feed(
            encode_frame(FRAME_HEADERS, encode_field_section(request_fields())), fin=True
        )
        self.assertTrue(done)
        self.assertEqual(dict(stream.headers)[":path"], "/hi")

    def test_a_body_split_across_frames_and_feeds(self):
        stream = RequestStream(4)
        stream.feed(encode_frame(FRAME_HEADERS, encode_field_section(request_fields("/e", "POST"))))
        stream.feed(encode_frame(FRAME_DATA, b"hello "))
        done = stream.feed(encode_frame(FRAME_DATA, b"world"), fin=True)
        self.assertTrue(done)
        self.assertEqual(bytes(stream.body), b"hello world")

    def test_a_frame_split_mid_octets_is_reassembled(self):
        whole = encode_frame(FRAME_HEADERS, encode_field_section(request_fields()))
        stream = RequestStream(0)
        stream.feed(whole[:4])
        stream.feed(whole[4:], fin=True)
        self.assertEqual(dict(stream.headers)[":method"], "GET")

    def test_data_before_headers_is_refused(self):
        with self.assertRaisesRegex(H3Error, "DATA before HEADERS"):
            RequestStream(0).feed(encode_frame(FRAME_DATA, b"body"))

    def test_connection_level_frames_are_refused_on_a_request_stream(self):
        stream = RequestStream(0)
        stream.feed(encode_frame(FRAME_HEADERS, encode_field_section(request_fields())))
        with self.assertRaisesRegex(H3Error, "does not belong"):
            stream.feed(encode_frame(FRAME_SETTINGS, b""))

    def test_a_stream_ending_mid_frame_is_refused(self):
        whole = encode_frame(FRAME_HEADERS, encode_field_section(request_fields()))
        with self.assertRaisesRegex(H3Error, "ended mid-frame"):
            RequestStream(0).feed(whole[:-3], fin=True)

    def test_unknown_frame_types_are_ignored(self):
        stream = RequestStream(0)
        stream.feed(encode_frame(0x21, b"reserved"))
        done = stream.feed(
            encode_frame(FRAME_HEADERS, encode_field_section(request_fields())), fin=True
        )
        self.assertTrue(done)

    def test_trailers_land_in_their_own_field(self):
        stream = RequestStream(0)
        stream.feed(encode_frame(FRAME_HEADERS, encode_field_section(request_fields())))
        stream.feed(encode_frame(FRAME_DATA, b"x"))
        stream.feed(encode_frame(FRAME_HEADERS, encode_field_section([("x-sum", "abc")])), fin=True)
        self.assertEqual(stream.trailers, [("x-sum", "abc")])


class TestHeaderValidation(unittest.TestCase):
    def test_a_well_formed_request(self):
        pseudo, regular = validate_request_headers(request_fields() + [("accept", "*/*")])
        self.assertEqual(pseudo[":method"], "GET")
        self.assertEqual(regular, [("accept", "*/*")])

    def test_uppercase_names_are_refused(self):
        with self.assertRaisesRegex(H3Error, "not lowercase"):
            validate_request_headers(request_fields() + [("Accept", "x")])

    def test_connection_specific_headers_are_refused(self):
        for name in ("connection", "transfer-encoding", "upgrade"):
            with self.subTest(header=name):
                with self.assertRaisesRegex(H3Error, "connection-specific"):
                    validate_request_headers(request_fields() + [(name, "x")])

    def test_missing_pseudo_headers_are_refused(self):
        with self.assertRaisesRegex(H3Error, "missing :path"):
            validate_request_headers([(":method", "GET"), (":scheme", "https")])

    def test_a_pseudo_header_after_a_regular_one_is_refused(self):
        with self.assertRaisesRegex(H3Error, "pseudo-header after"):
            validate_request_headers([("accept", "*/*"), (":method", "GET")])


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.addCleanup(self.app.close)

        @self.app.api("/hi", methods=("GET",))
        def hi(request):
            return {"version": request.version, "path": request.path, "n": request.query.get("n")}

        @self.app.api("/echo", methods=("POST",))
        def echo(request):
            return {"length": len(request.body), "body": request.body.decode()}

        @self.app.route("/legacy", methods=("GET",))
        def legacy(_request):
            return Response.text("x", headers={"Connection": "close", "X-Keep": "yes"})

    def _exchange(self, stream):
        request = build_request(stream, ("127.0.0.1", 1234))
        response = self.app.dispatch(request)
        frames, _tail = parse_frames(build_response_frames(response))
        headers = dict(decode_field_section(frames[0][1]))
        body = frames[1][1] if len(frames) > 1 else b""
        return headers, body

    def test_a_get_round_trip(self):
        stream = RequestStream(0)
        stream.feed(
            encode_frame(FRAME_HEADERS, encode_field_section(request_fields("/hi?n=7"))), fin=True
        )
        headers, body = self._exchange(stream)
        self.assertEqual(headers[":status"], "200")
        self.assertIn(b'"version":"HTTP/3"', body)
        self.assertIn(b'"n":"7"', body)

    def test_a_post_round_trip(self):
        stream = RequestStream(4)
        stream.feed(
            encode_frame(FRAME_HEADERS, encode_field_section(request_fields("/echo", "POST")))
        )
        stream.feed(encode_frame(FRAME_DATA, b"hola mundo"), fin=True)
        headers, body = self._exchange(stream)
        self.assertEqual(headers[":status"], "200")
        self.assertIn(b'"length":10', body)

    def test_connection_specific_response_headers_are_dropped(self):
        stream = RequestStream(0)
        stream.feed(
            encode_frame(FRAME_HEADERS, encode_field_section(request_fields("/legacy"))), fin=True
        )
        headers, _body = self._exchange(stream)
        self.assertNotIn("connection", headers)
        self.assertEqual(headers["x-keep"], "yes")

    def test_a_missing_route_answers_404(self):
        stream = RequestStream(0)
        stream.feed(
            encode_frame(FRAME_HEADERS, encode_field_section(request_fields("/nope"))), fin=True
        )
        headers, _body = self._exchange(stream)
        self.assertEqual(headers[":status"], "404")


if __name__ == "__main__":
    unittest.main()

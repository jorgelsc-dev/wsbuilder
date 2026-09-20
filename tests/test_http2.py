import struct
import unittest

from wsbuilder import App, Response
from wsbuilder.hpack import Decoder, Encoder
from wsbuilder.http2 import (
    CONNECTION_PREFACE,
    DEFAULT_INITIAL_WINDOW_SIZE,
    ERROR_FLOW_CONTROL_ERROR,
    ERROR_FRAME_SIZE_ERROR,
    ERROR_PROTOCOL_ERROR,
    FLAG_ACK,
    FLAG_END_HEADERS,
    FLAG_END_STREAM,
    FLAG_PADDED,
    FLAG_PRIORITY,
    FRAME_CONTINUATION,
    FRAME_DATA,
    FRAME_GOAWAY,
    FRAME_HEADERS,
    FRAME_PING,
    FRAME_RST_STREAM,
    FRAME_SETTINGS,
    FRAME_WINDOW_UPDATE,
    MAX_WINDOW_SIZE,
    SETTINGS_INITIAL_WINDOW_SIZE,
    SETTINGS_MAX_FRAME_SIZE,
    FlowControlWindow,
    Frame,
    Http2Connection,
    StreamError,
    decode_settings,
    encode_settings,
    parse_frame_header,
    strip_padding,
    validate_request_headers,
    validate_settings,
)
from wsbuilder.http2 import ConnectionError_ as H2ConnectionError


class Pipe:
    """Stands in for a socket: fixed input, captured output."""

    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.sent = bytearray()

    def recv(self, size):
        if not self.data:
            return b""
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk

    def sendall(self, payload):
        self.sent.extend(payload)


def frames_in(payload):
    """Split a captured byte stream back into frames."""
    out = []
    offset = 0
    payload = bytes(payload)
    while offset < len(payload):
        length, frame_type, flags, stream_id = parse_frame_header(payload[offset : offset + 9])
        body = payload[offset + 9 : offset + 9 + length]
        offset += 9 + length
        out.append(Frame(frame_type, flags, stream_id, body))
    return out


class TestFrameSerialization(unittest.TestCase):
    def test_header_layout(self):
        raw = Frame(FRAME_HEADERS, FLAG_END_HEADERS | FLAG_END_STREAM, 1, b"abc").serialize()
        self.assertEqual(raw.hex(), "000003010500000001616263")
        self.assertEqual(parse_frame_header(raw), (3, FRAME_HEADERS, 0x05, 1))

    def test_the_reserved_bit_is_ignored_on_receipt(self):
        raw = bytearray(Frame(FRAME_DATA, 0, 1, b"").serialize())
        raw[5] |= 0x80  # set the reserved bit
        self.assertEqual(parse_frame_header(bytes(raw))[3], 1)

    def test_a_short_header_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "9 octets"):
            parse_frame_header(b"\x00" * 8)

    def test_a_payload_beyond_the_length_field_is_refused(self):
        with self.assertRaisesRegex(ValueError, "24-bit"):
            Frame(FRAME_DATA, 0, 1, b"x" * (2**24)).serialize()

    def test_padding_is_removed(self):
        payload = bytes([3]) + b"body" + b"\x00\x00\x00"
        self.assertEqual(strip_padding(payload, FLAG_PADDED, 1), b"body")

    def test_padding_longer_than_the_payload_is_a_connection_error(self):
        with self.assertRaises(H2ConnectionError):
            strip_padding(bytes([9]) + b"ab", FLAG_PADDED, 1)

    def test_a_padded_frame_needs_a_pad_length(self):
        with self.assertRaises(H2ConnectionError):
            strip_padding(b"", FLAG_PADDED, 1)


class TestSettings(unittest.TestCase):
    def test_round_trip(self):
        values = {SETTINGS_MAX_FRAME_SIZE: 16384, SETTINGS_INITIAL_WINDOW_SIZE: 65535}
        self.assertEqual(decode_settings(encode_settings(values)), values)

    def test_a_ragged_payload_is_a_frame_size_error(self):
        with self.assertRaises(H2ConnectionError) as caught:
            decode_settings(b"\x00" * 7)
        self.assertEqual(caught.exception.code, ERROR_FRAME_SIZE_ERROR)

    def test_out_of_range_values_are_refused(self):
        with self.assertRaisesRegex(H2ConnectionError, "ENABLE_PUSH"):
            validate_settings({0x2: 2})
        with self.assertRaisesRegex(H2ConnectionError, "INITIAL_WINDOW_SIZE"):
            validate_settings({SETTINGS_INITIAL_WINDOW_SIZE: MAX_WINDOW_SIZE + 1})
        with self.assertRaisesRegex(H2ConnectionError, "MAX_FRAME_SIZE"):
            validate_settings({SETTINGS_MAX_FRAME_SIZE: 1024})

    def test_unknown_settings_are_accepted(self):
        # Section 6.5.3: a receiver ignores identifiers it does not know.
        self.assertEqual(validate_settings({0xFF: 1}), {0xFF: 1})


class TestFlowControl(unittest.TestCase):
    def test_consuming_beyond_the_window_is_an_error(self):
        window = FlowControlWindow(10)
        window.consume(10)
        with self.assertRaises(H2ConnectionError) as caught:
            window.consume(1)
        self.assertEqual(caught.exception.code, ERROR_FLOW_CONTROL_ERROR)

    def test_a_zero_increment_is_a_protocol_error(self):
        with self.assertRaises(H2ConnectionError):
            FlowControlWindow().credit(0)

    def test_the_window_cannot_pass_two_to_the_thirty_first(self):
        window = FlowControlWindow(MAX_WINDOW_SIZE)
        with self.assertRaisesRegex(H2ConnectionError, "2\\^31-1"):
            window.credit(1)

    def test_changing_the_initial_size_moves_a_live_window(self):
        window = FlowControlWindow(100)
        window.consume(40)
        window.adjust_initial(50)
        self.assertEqual(window.available, 110)


class TestRequestHeaderValidation(unittest.TestCase):
    def test_a_well_formed_request(self):
        pseudo, regular = validate_request_headers(
            [(":method", "GET"), (":scheme", "https"), (":path", "/"), ("accept", "*/*")], 1
        )
        self.assertEqual(pseudo[":method"], "GET")
        self.assertEqual(regular, [("accept", "*/*")])

    def test_a_pseudo_header_after_a_regular_one_is_rejected(self):
        with self.assertRaisesRegex(StreamError, "pseudo-header after"):
            validate_request_headers([("accept", "*/*"), (":method", "GET")], 1)

    def test_uppercase_field_names_are_rejected(self):
        with self.assertRaisesRegex(StreamError, "not lowercase"):
            validate_request_headers(
                [(":method", "GET"), (":scheme", "h"), (":path", "/"), ("Accept", "x")], 1
            )

    def test_connection_specific_headers_are_rejected(self):
        for name in ("connection", "keep-alive", "transfer-encoding", "upgrade"):
            with self.subTest(header=name):
                with self.assertRaisesRegex(StreamError, "connection-specific"):
                    validate_request_headers(
                        [(":method", "GET"), (":scheme", "h"), (":path", "/"), (name, "x")], 1
                    )

    def test_te_may_only_say_trailers(self):
        base = [(":method", "GET"), (":scheme", "h"), (":path", "/")]
        validate_request_headers(base + [("te", "trailers")], 1)
        with self.assertRaisesRegex(StreamError, "te may only"):
            validate_request_headers(base + [("te", "gzip")], 1)

    def test_missing_pseudo_headers_are_rejected(self):
        with self.assertRaisesRegex(StreamError, "missing :path"):
            validate_request_headers([(":method", "GET"), (":scheme", "https")], 1)

    def test_an_empty_path_is_rejected(self):
        with self.assertRaisesRegex(StreamError, ":path must not be empty"):
            validate_request_headers(
                [(":method", "GET"), (":scheme", "https"), (":path", "")], 1
            )

    def test_duplicate_pseudo_headers_are_rejected(self):
        with self.assertRaisesRegex(StreamError, "duplicate"):
            validate_request_headers([(":method", "GET"), (":method", "POST")], 1)

    def test_connect_takes_authority_only(self):
        validate_request_headers([(":method", "CONNECT"), (":authority", "h:443")], 1)
        with self.assertRaisesRegex(StreamError, "neither"):
            validate_request_headers(
                [(":method", "CONNECT"), (":authority", "h"), (":path", "/")], 1
            )


class ConnectionHarness(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.addCleanup(self.app.close)

        @self.app.api("/hello", methods=("GET",))
        def hello(request):
            return {"version": request.version, "n": request.query.get("n", "")}

        @self.app.api("/echo", methods=("POST",))
        def echo(request):
            return {"length": len(request.body), "body": request.body.decode()}

        @self.app.api("/boom", methods=("GET",))
        def boom(_request):
            raise RuntimeError("handler exploded")

        self.encoder = Encoder()
        self.decoder = Decoder()

    def headers_frame(self, stream_id, fields, end_stream=True, flags=0):
        combined = FLAG_END_HEADERS | flags | (FLAG_END_STREAM if end_stream else 0)
        return Frame(FRAME_HEADERS, combined, stream_id, self.encoder.encode(fields)).serialize()

    def request(self, path, method="GET", stream_id=1):
        return [
            (":method", method),
            (":scheme", "https"),
            (":path", path),
            (":authority", "example.test"),
        ]

    def run_connection(self, *chunks, preface=True):
        payload = bytearray()
        if preface:
            payload += CONNECTION_PREFACE + Frame(FRAME_SETTINGS, 0, 0, b"").serialize()
        for chunk in chunks:
            payload += chunk
        pipe = Pipe(bytes(payload))
        Http2Connection(pipe, self.app, client_address=("127.0.0.1", 1234)).serve()
        return frames_in(pipe.sent)

    def responses(self, frames):
        out = {}
        for frame in frames:
            if frame.type == FRAME_HEADERS:
                out.setdefault(frame.stream_id, {})["headers"] = dict(
                    self.decoder.decode(frame.payload)
                )
            elif frame.type == FRAME_DATA:
                out.setdefault(frame.stream_id, {}).setdefault("body", b"")
                out[frame.stream_id]["body"] += frame.payload
        return out


class TestConnectionFlow(ConnectionHarness):
    def test_settings_are_exchanged_and_acknowledged(self):
        frames = self.run_connection()
        kinds = [(f.type, f.flags) for f in frames]
        self.assertIn((FRAME_SETTINGS, 0), kinds)
        self.assertIn((FRAME_SETTINGS, FLAG_ACK), kinds)

    def test_a_bad_preface_ends_the_connection(self):
        pipe = Pipe(b"NOT-HTTP2-AT-ALL\r\n\r\n" + b"\x00" * 16)
        Http2Connection(pipe, self.app).serve()
        goaway = [f for f in frames_in(pipe.sent) if f.type == FRAME_GOAWAY]
        self.assertEqual(len(goaway), 1)
        self.assertEqual(struct.unpack(">II", goaway[0].payload[:8])[1], ERROR_PROTOCOL_ERROR)

    def test_a_get_is_answered(self):
        frames = self.run_connection(self.headers_frame(1, self.request("/hello?n=7")))
        answer = self.responses(frames)[1]
        self.assertEqual(answer["headers"][":status"], "200")
        self.assertIn(b'"version":"HTTP/2"', answer["body"])
        self.assertIn(b'"n":"7"', answer["body"])

    def test_a_post_body_arrives_reassembled(self):
        frames = self.run_connection(
            self.headers_frame(3, self.request("/echo", "POST"), end_stream=False),
            Frame(FRAME_DATA, 0, 3, b"hello ").serialize(),
            Frame(FRAME_DATA, FLAG_END_STREAM, 3, b"world").serialize(),
        )
        answer = self.responses(frames)[3]
        self.assertIn(b'"length":11', answer["body"])
        self.assertIn(b'"body":"hello world"', answer["body"])

    def test_streams_are_multiplexed(self):
        frames = self.run_connection(
            self.headers_frame(1, self.request("/hello?n=1")),
            self.headers_frame(3, self.request("/hello?n=3")),
        )
        answers = self.responses(frames)
        self.assertIn(b'"n":"1"', answers[1]["body"])
        self.assertIn(b'"n":"3"', answers[3]["body"])

    def test_data_frames_are_credited_back(self):
        frames = self.run_connection(
            self.headers_frame(3, self.request("/echo", "POST"), end_stream=False),
            Frame(FRAME_DATA, FLAG_END_STREAM, 3, b"payload").serialize(),
        )
        updates = [f for f in frames if f.type == FRAME_WINDOW_UPDATE]
        # One for the connection window, one for the stream.
        self.assertEqual({f.stream_id for f in updates}, {0, 3})
        for frame in updates:
            self.assertEqual(struct.unpack(">I", frame.payload)[0], len(b"payload"))

    def test_a_header_block_split_across_continuation_is_reassembled(self):
        block = self.encoder.encode(self.request("/hello?n=9"))
        first, rest = block[:3], block[3:]
        frames = self.run_connection(
            Frame(FRAME_HEADERS, FLAG_END_STREAM, 1, first).serialize(),
            Frame(FRAME_CONTINUATION, FLAG_END_HEADERS, 1, rest).serialize(),
        )
        self.assertIn(b'"n":"9"', self.responses(frames)[1]["body"])

    def test_a_frame_between_headers_and_continuation_is_fatal(self):
        block = self.encoder.encode(self.request("/hello"))
        frames = self.run_connection(
            Frame(FRAME_HEADERS, FLAG_END_STREAM, 1, block[:3]).serialize(),
            Frame(FRAME_PING, 0, 0, b"12345678").serialize(),
        )
        self.assertTrue(any(f.type == FRAME_GOAWAY for f in frames))

    def test_ping_is_echoed(self):
        frames = self.run_connection(Frame(FRAME_PING, 0, 0, b"abcdefgh").serialize())
        pongs = [f for f in frames if f.type == FRAME_PING and f.flags & FLAG_ACK]
        self.assertEqual(len(pongs), 1)
        self.assertEqual(pongs[0].payload, b"abcdefgh")

    def test_a_ping_of_the_wrong_size_is_fatal(self):
        frames = self.run_connection(Frame(FRAME_PING, 0, 0, b"short").serialize())
        goaway = [f for f in frames if f.type == FRAME_GOAWAY][0]
        self.assertEqual(struct.unpack(">II", goaway.payload[:8])[1], ERROR_FRAME_SIZE_ERROR)

    def test_an_even_numbered_stream_from_a_client_is_fatal(self):
        frames = self.run_connection(self.headers_frame(2, self.request("/hello")))
        self.assertTrue(any(f.type == FRAME_GOAWAY for f in frames))

    def test_stream_identifiers_must_increase(self):
        frames = self.run_connection(
            self.headers_frame(5, self.request("/hello")),
            self.headers_frame(3, self.request("/hello")),
        )
        self.assertTrue(any(f.type == FRAME_GOAWAY for f in frames))

    def test_a_failing_handler_answers_500_without_hurting_other_streams(self):
        frames = self.run_connection(
            self.headers_frame(1, self.request("/boom")),
            self.headers_frame(3, self.request("/hello?n=2")),
        )
        answers = self.responses(frames)
        # App.dispatch already turns a handler fault into a 500, as it does
        # over HTTP/1, so the stream is answered rather than reset.
        self.assertEqual(answers[1]["headers"][":status"], "500")
        self.assertIn(b'"n":"2"', answers[3]["body"])
        self.assertFalse(any(f.type == FRAME_GOAWAY and f.payload[7] != 0 for f in frames))

    def test_headers_with_priority_are_accepted(self):
        block = self.encoder.encode(self.request("/hello?n=4"))
        payload = struct.pack(">IB", 0, 15) + block
        frames = self.run_connection(
            Frame(
                FRAME_HEADERS, FLAG_END_HEADERS | FLAG_END_STREAM | FLAG_PRIORITY, 1, payload
            ).serialize()
        )
        self.assertIn(b'"n":"4"', self.responses(frames)[1]["body"])

    def test_padded_headers_are_accepted(self):
        block = self.encoder.encode(self.request("/hello?n=5"))
        payload = bytes([4]) + block + b"\x00" * 4
        frames = self.run_connection(
            Frame(
                FRAME_HEADERS, FLAG_END_HEADERS | FLAG_END_STREAM | FLAG_PADDED, 1, payload
            ).serialize()
        )
        self.assertIn(b'"n":"5"', self.responses(frames)[1]["body"])

    def test_connection_specific_response_headers_are_dropped(self):
        @self.app.route("/legacy", methods=("GET",))
        def legacy(_request):
            return Response.text("x", headers={"Connection": "close", "X-Keep": "yes"})

        frames = self.run_connection(self.headers_frame(1, self.request("/legacy")))
        headers = self.responses(frames)[1]["headers"]
        self.assertNotIn("connection", headers)
        self.assertEqual(headers["x-keep"], "yes")

    def test_an_oversized_frame_is_fatal(self):
        raw = bytearray(Frame(FRAME_DATA, 0, 1, b"").serialize())
        raw[0:3] = (20000).to_bytes(3, "big")  # beyond our MAX_FRAME_SIZE
        frames = self.run_connection(bytes(raw))
        goaway = [f for f in frames if f.type == FRAME_GOAWAY][0]
        self.assertEqual(struct.unpack(">II", goaway.payload[:8])[1], ERROR_FRAME_SIZE_ERROR)

    def test_unknown_frame_types_are_ignored(self):
        frames = self.run_connection(
            Frame(0x63, 0, 0, b"whatever").serialize(),
            self.headers_frame(1, self.request("/hello?n=8")),
        )
        self.assertIn(b'"n":"8"', self.responses(frames)[1]["body"])

    def test_rst_stream_stops_a_stream_without_killing_the_connection(self):
        frames = self.run_connection(
            self.headers_frame(1, self.request("/hello"), end_stream=False),
            Frame(FRAME_RST_STREAM, 0, 1, struct.pack(">I", 8)).serialize(),
            self.headers_frame(3, self.request("/hello?n=6")),
        )
        self.assertIn(b'"n":"6"', self.responses(frames)[3]["body"])

    def test_a_settings_change_moves_live_stream_windows(self):
        frames = self.run_connection(
            self.headers_frame(1, self.request("/hello"), end_stream=False),
            Frame(
                FRAME_SETTINGS, 0, 0, encode_settings({SETTINGS_INITIAL_WINDOW_SIZE: 1024})
            ).serialize(),
            Frame(FRAME_DATA, FLAG_END_STREAM, 1, b"").serialize(),
        )
        # An empty DATA frame with END_STREAM is the ordinary way to finish a
        # stream; it once made the server emit a WINDOW_UPDATE of 0 and kill
        # the connection over its own illegal frame.
        self.assertFalse(any(f.type == FRAME_GOAWAY and f.payload[7] != 0 for f in frames))
        self.assertFalse(
            any(
                f.type == FRAME_WINDOW_UPDATE and struct.unpack(">I", f.payload)[0] == 0
                for f in frames
            )
        )
        self.assertEqual(self.responses(frames)[1]["headers"][":status"], "200")


if __name__ == "__main__":
    unittest.main()


class TestServerIntegration(unittest.TestCase):
    """HTTP/2 reached over a real socket, both in the clear and over TLS."""

    def setUp(self):
        import socket
        import threading

        from wsbuilder.server import HTTPServer

        self.socket = socket
        self.app = App()
        self.addCleanup(self.app.close)

        @self.app.api("/ping", methods=("GET",))
        def ping(request):
            return {"version": request.version, "path": request.path}

        self.HTTPServer = HTTPServer
        self.threading = threading

    def _start(self, **kwargs):
        server = self.HTTPServer("127.0.0.1", 0, self.app, **kwargs)
        thread = self.threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(server.stop)
        self.assertTrue(server.wait_until_serving(timeout=5.0))
        return server

    def _speak_http2(self, sock):
        encoder, decoder = Encoder(), Decoder()
        sock.sendall(CONNECTION_PREFACE + Frame(FRAME_SETTINGS, 0, 0, b"").serialize())
        fields = [
            (":method", "GET"),
            (":scheme", "https"),
            (":path", "/ping"),
            (":authority", "localhost"),
        ]
        sock.sendall(
            Frame(
                FRAME_HEADERS, FLAG_END_HEADERS | FLAG_END_STREAM, 1, encoder.encode(fields)
            ).serialize()
        )
        sock.settimeout(5.0)
        buffer = b""
        result = {}
        while "body" not in result:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= 9:
                length, frame_type, _flags, _stream = parse_frame_header(buffer[:9])
                if len(buffer) < 9 + length:
                    break
                payload, buffer = buffer[9 : 9 + length], buffer[9 + length :]
                if frame_type == FRAME_HEADERS:
                    result["headers"] = dict(decoder.decode(payload))
                elif frame_type == FRAME_DATA:
                    result["body"] = payload
        return result

    def test_cleartext_prior_knowledge(self):
        # RFC 9113 section 3.3: no negotiation, the client just opens with the
        # preface and the server recognises it.
        server = self._start()
        with self.socket.create_connection(server.server_address, timeout=5.0) as sock:
            result = self._speak_http2(sock)
        self.assertEqual(result["headers"][":status"], "200")
        self.assertIn(b'"version":"HTTP/2"', result["body"])

    def test_http1_still_works_on_the_same_port(self):
        server = self._start()
        with self.socket.create_connection(server.server_address, timeout=5.0) as sock:
            sock.sendall(b"GET /ping HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
            received = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        self.assertIn(b"HTTP/1.1 200 OK", received)
        self.assertIn(b'"version":"HTTP/1.1"', received)

    def test_alpn_selects_h2_over_tls(self):
        import ssl as ssl_module

        from wsbuilder import CertificateAuthority, CertificateManager

        ca = CertificateAuthority.create("H2 CA")
        manager = CertificateManager(
            ca=ca,
            common_name="localhost",
            dns_names=["localhost"],
            ip_addresses=["127.0.0.1"],
            alpn_protocols=["h2", "http/1.1"],
        )
        server = self._start(ssl_context=manager)

        context = ssl_module.create_default_context(cadata=ca.certificate_pem.decode())
        context.set_alpn_protocols(["h2", "http/1.1"])
        raw = self.socket.create_connection(server.server_address, timeout=5.0)
        with context.wrap_socket(raw, server_hostname="localhost") as tls:
            self.assertEqual(tls.selected_alpn_protocol(), "h2")
            result = self._speak_http2(tls)
        self.assertEqual(result["headers"][":status"], "200")
        self.assertIn(b'"version":"HTTP/2"', result["body"])

    def test_http2_can_be_switched_off(self):
        server = self._start()
        server.ENABLE_HTTP2 = False
        with self.socket.create_connection(server.server_address, timeout=5.0) as sock:
            sock.sendall(CONNECTION_PREFACE)
            sock.settimeout(5.0)
            received = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    received += chunk
            except OSError:
                pass
        # Falls through to HTTP/1, which cannot parse the preface.
        self.assertNotIn(b"\x00\x00\x00\x04", received[:9])


class TestOutputFlowControl(ConnectionHarness):
    """A body larger than the peer's window waits for credit."""

    def setUp(self):
        super().setUp()

        @self.app.route("/big", methods=("GET",))
        def big(_request):
            return Response.text("x" * 300)

    def test_a_body_beyond_the_window_is_sent_in_instalments(self):
        frames = self.run_connection(
            Frame(
                FRAME_SETTINGS, 0, 0, encode_settings({SETTINGS_INITIAL_WINDOW_SIZE: 100})
            ).serialize(),
            self.headers_frame(1, self.request("/big")),
            Frame(FRAME_WINDOW_UPDATE, 0, 1, struct.pack(">I", 250)).serialize(),
        )
        data = [f for f in frames if f.type == FRAME_DATA]
        self.assertEqual(b"".join(f.payload for f in data), b"x" * 300)
        # The first 100 octets fit the window; the rest waited for credit.
        self.assertGreater(len(data), 1)
        self.assertEqual(len(data[0].payload), 100)
        self.assertTrue(data[-1].flags & FLAG_END_STREAM)

    def test_an_exhausted_window_no_longer_kills_the_stream(self):
        frames = self.run_connection(
            Frame(
                FRAME_SETTINGS, 0, 0, encode_settings({SETTINGS_INITIAL_WINDOW_SIZE: 50})
            ).serialize(),
            self.headers_frame(1, self.request("/big")),
        )
        # It used to raise a flow control error and reset the stream.
        self.assertFalse(any(f.type == FRAME_RST_STREAM for f in frames))
        sent = b"".join(f.payload for f in frames if f.type == FRAME_DATA)
        self.assertEqual(len(sent), 50)
        self.assertFalse(
            any(f.type == FRAME_DATA and f.flags & FLAG_END_STREAM for f in frames)
        )

    def test_raising_the_initial_window_releases_queued_output(self):
        frames = self.run_connection(
            Frame(
                FRAME_SETTINGS, 0, 0, encode_settings({SETTINGS_INITIAL_WINDOW_SIZE: 60})
            ).serialize(),
            self.headers_frame(1, self.request("/big")),
            Frame(
                FRAME_SETTINGS, 0, 0, encode_settings({SETTINGS_INITIAL_WINDOW_SIZE: 400})
            ).serialize(),
        )
        sent = b"".join(f.payload for f in frames if f.type == FRAME_DATA)
        self.assertEqual(sent, b"x" * 300)

    def test_a_connection_level_update_flushes_every_stream(self):
        frames = self.run_connection(
            Frame(
                FRAME_SETTINGS, 0, 0, encode_settings({SETTINGS_INITIAL_WINDOW_SIZE: 400})
            ).serialize(),
            self.headers_frame(1, self.request("/big")),
            self.headers_frame(3, self.request("/big")),
            Frame(FRAME_WINDOW_UPDATE, 0, 0, struct.pack(">I", 1000)).serialize(),
        )
        by_stream = {}
        for frame in frames:
            if frame.type == FRAME_DATA:
                by_stream.setdefault(frame.stream_id, b"")
                by_stream[frame.stream_id] += frame.payload
        self.assertEqual(by_stream[1], b"x" * 300)
        self.assertEqual(by_stream[3], b"x" * 300)

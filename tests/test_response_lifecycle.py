"""Response semantics shared by the multiplexed HTTP transports."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wsbuilder import Response, http3
from wsbuilder.hpack import Decoder
from wsbuilder.http2 import FRAME_DATA, FRAME_HEADERS, Http2Connection, Stream, parse_frame_header
from wsbuilder.http3_server import Http3Server
from wsbuilder.qpack import decode_field_section


class Producer:
    def __init__(self):
        self.iterations = 0
        self.close_calls = 0

    def __iter__(self):
        self.iterations += 1
        return iter([b"payload"])

    def close(self):
        self.close_calls += 1


class TestResponseLifecycle(unittest.TestCase):
    def _send(self, protocol, response, *, send_body=True):
        queued = []
        if protocol == "h2":
            connection = Http2Connection(SimpleNamespace(sendall=queued.append), None)
            connection.send_response(Stream(1), response, send_body=send_body)
            headers = []
            body = b""
            decoder = Decoder()
            for raw in queued:
                length, kind, _flags, _stream_id = parse_frame_header(raw[:9])
                payload = raw[9:9 + length]
                if kind == FRAME_HEADERS:
                    headers.extend(decoder.decode(payload))
                elif kind == FRAME_DATA:
                    body += payload
            return headers, body
        server = Http3Server("127.0.0.1", 0, None, None)
        self.addCleanup(server._dispatch_pool.shutdown, wait=True)
        connection = SimpleNamespace(queue_stream_data=lambda _sid, data, fin=False: queued.append(data))
        server._send_response(connection, 0, response, send_body=send_body)
        frames, tail = http3.parse_frames(b"".join(queued))
        self.assertEqual(tail, b"")
        headers = decode_field_section(frames[0][1])
        return headers, b"".join(data for kind, data in frames if kind == http3.FRAME_DATA)

    def test_head_closes_the_unused_stream_exactly_once(self):
        for protocol in ("h2", "h3"):
            with self.subTest(protocol=protocol):
                producer = Producer()
                fields, body = self._send(
                    protocol, Response.stream(producer, headers={"Content-Length": "7"}),
                    send_body=False,
                )
                self.assertEqual((producer.iterations, producer.close_calls), (0, 1))
                self.assertEqual(body, b"")
                self.assertIn(("content-length", "7"), fields)

    def test_head_buffered_response_describes_its_get_length(self):
        for protocol in ("h2", "h3"):
            with self.subTest(protocol=protocol):
                fields, body = self._send(protocol, Response.text("payload"), send_body=False)
                self.assertEqual(body, b"")
                self.assertIn(("content-length", "7"), fields)

    def test_statuses_without_content_do_not_consume_or_send_bodies(self):
        for protocol in ("h2", "h3"):
            for status in (103, 204, 304):
                for streaming in (False, True):
                    with self.subTest(protocol=protocol, status=status, streaming=streaming):
                        producer = Producer()
                        response = Response(
                            status=status, body=b"payload", headers={"Content-Length": "7"},
                            stream=producer if streaming else None,
                        )
                        fields, body = self._send(protocol, response)
                        self.assertEqual(body, b"")
                        self.assertEqual(producer.iterations, 0)
                        self.assertEqual(producer.close_calls, int(streaming))
                        if status == 304:
                            self.assertIn(("content-length", "7"), fields)
                        else:
                            self.assertFalse(any(name == "content-length" for name, _value in fields))

    def test_buffered_body_has_one_accurate_content_length(self):
        for protocol in ("h2", "h3"):
            with self.subTest(protocol=protocol):
                fields, body = self._send(protocol, Response.text("payload", headers={"Content-Length": "99"}))
                self.assertEqual(body, b"payload")
                self.assertEqual([value for name, value in fields if name == "content-length"], ["7"])

    def test_failure_writing_headers_closes_the_stream_once_through_dispatch(self):
        for protocol in ("h2", "h3"):
            for method in ("GET", "HEAD"):
                with self.subTest(protocol=protocol, method=method):
                    producer = Producer()
                    app = SimpleNamespace(dispatch=lambda _request: Response.stream(producer))

                    def broken(*_args, **_kwargs):
                        raise OSError("peer gone")

                    if protocol == "h2":
                        connection = Http2Connection(SimpleNamespace(sendall=broken), app)
                        connection._run_handler(Stream(1), SimpleNamespace(method=method))
                    else:
                        server = Http3Server("127.0.0.1", 0, app, None)
                        self.addCleanup(server._dispatch_pool.shutdown, wait=True)
                        connection = SimpleNamespace(closed=False, queue_stream_data=broken)
                        with patch("wsbuilder.http3_server.get_log"):
                            server._dispatch(connection, 0, SimpleNamespace(method=method))
                    self.assertEqual((producer.iterations, producer.close_calls), (0, 1))

    def test_h3_serialization_failure_closes_unused_stream(self):
        producer = Producer()
        with patch("wsbuilder.http3.encode_headers_frame", side_effect=ValueError("bad header")):
            with self.assertRaises(ValueError):
                http3.build_response_frames(Response.stream(producer), send_body=False)
        self.assertEqual((producer.iterations, producer.close_calls), (0, 1))

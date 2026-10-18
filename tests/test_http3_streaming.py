"""Unit coverage for HTTP/3 streaming, RFC 9220 extended CONNECT and the WebSocket adapter."""

import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wsbuilder import App, Response, http3
from wsbuilder.http3_server import Http3Server
from wsbuilder.http3_websocket import Http3WebSocketStream
from wsbuilder.qpack import decode_field_section, encode_field_section
from wsbuilder.quic.connection import STREAM_CHUNK_SIZE, QuicConnection


def _connect_headers():
    return [
        (":method", "CONNECT"),
        (":protocol", "websocket"),
        (":scheme", "https"),
        (":authority", "localhost"),
        (":path", "/echo"),
        ("sec-websocket-version", "13"),
    ]


class TestExtendedConnectValidation(unittest.TestCase):
    def test_a_well_formed_websocket_connect_is_accepted(self):
        pseudo, regular = http3.validate_request_headers(_connect_headers())
        self.assertEqual(pseudo[":protocol"], "websocket")
        self.assertEqual(pseudo[":path"], "/echo")

    def test_an_unknown_protocol_is_refused(self):
        headers = _connect_headers()
        headers[1] = (":protocol", "webtransport")
        with self.assertRaises(http3.H3Error):
            http3.validate_request_headers(headers)

    def test_extended_connect_needs_a_path(self):
        headers = [h for h in _connect_headers() if h[0] != ":path"]
        with self.assertRaises(http3.H3Error):
            http3.validate_request_headers(headers)

    def test_protocol_is_refused_on_other_methods(self):
        headers = _connect_headers()
        headers[0] = (":method", "GET")
        with self.assertRaises(http3.H3Error):
            http3.validate_request_headers(headers)

    def test_a_plain_connect_still_needs_only_authority(self):
        pseudo, _ = http3.validate_request_headers(
            [(":method", "CONNECT"), (":authority", "example.test:443")]
        )
        self.assertEqual(pseudo[":method"], "CONNECT")

    def test_is_extended_connect_reads_the_request_stream(self):
        stream = http3.RequestStream(0)
        stream.headers = _connect_headers()
        self.assertTrue(http3.is_extended_connect(stream))

        stream.headers = [(":method", "GET"), (":scheme", "https"), (":path", "/"), (":authority", "x")]
        self.assertFalse(http3.is_extended_connect(stream))


class TestControlStream(unittest.TestCase):
    def test_settings_advertise_extended_connect(self):
        stream = http3.build_control_stream()
        frames, _tail = http3.parse_frames(stream[1:])  # skip the stream type byte
        frame_type, payload = frames[0]
        self.assertEqual(frame_type, http3.FRAME_SETTINGS)
        settings = http3.decode_settings(payload)
        self.assertEqual(settings[http3.SETTINGS_ENABLE_CONNECT_PROTOCOL], 1)


class TestResponseHead(unittest.TestCase):
    def test_head_carries_status_and_no_body(self):
        fields = http3.build_response_head(200, {"sec-websocket-protocol": "chat", "connection": "close"})
        self.assertEqual(fields[0], (":status", "200"))
        self.assertIn(("sec-websocket-protocol", "chat"), fields)
        self.assertFalse(any(name == "connection" for name, _ in fields))

    def test_the_head_frame_is_a_headers_frame(self):
        frame = http3.encode_headers_frame(http3.build_response_head(404))
        frames, tail = http3.parse_frames(frame)
        self.assertEqual(tail, b"")
        self.assertEqual(frames[0][0], http3.FRAME_HEADERS)


class TestWebSocketStream(unittest.TestCase):
    class _Connection:
        def __init__(self):
            self.queued = []

        def queue_stream_data(self, stream_id, payload, fin=False):
            self.queued.append((stream_id, bytes(payload), fin))

    def setUp(self):
        self.connection = self._Connection()
        self.stream = Http3WebSocketStream(self.connection, 4)

    def test_pushed_bytes_are_read_back_in_order(self):
        self.stream.push(b"abc")
        self.stream.push(b"def")
        self.assertEqual(self.stream.recv(4), b"abcd")
        self.assertEqual(self.stream.recv(10), b"ef")

    def test_end_of_stream_reads_as_empty(self):
        self.stream.push_eof()
        self.assertEqual(self.stream.recv(10), b"")

    def test_an_idle_read_times_out(self):
        self.stream.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            self.stream.recv(1)

    def test_a_blocked_read_wakes_when_bytes_arrive(self):
        self.stream.settimeout(2.0)
        result = []
        reader = threading.Thread(target=lambda: result.append(self.stream.recv(3)))
        reader.start()
        self.stream.push(b"hey")
        reader.join(2.0)
        self.assertEqual(result, [b"hey"])

    def test_sendall_queues_a_data_frame(self):
        self.stream.sendall(b"payload")
        stream_id, payload, fin = self.connection.queued[0]
        self.assertEqual(stream_id, 4)
        self.assertFalse(fin)
        frames, _tail = http3.parse_frames(payload)
        self.assertEqual(frames, [(http3.FRAME_DATA, b"payload")])

    def test_close_queues_end_of_stream_once(self):
        self.stream.close()
        self.stream.close()
        self.assertEqual(self.connection.queued, [(4, b"", True)])
        with self.assertRaises(ConnectionError):
            self.stream.sendall(b"late")


class TestOutboundQueue(unittest.TestCase):
    def _bare_connection(self):
        # The queue needs no keys to be filled; only draining does.
        connection = QuicConnection.__new__(QuicConnection)
        connection._outbound_lock = threading.Lock()
        connection._outbound = []
        connection._queued_bytes = {}
        connection._send_offsets = {}
        connection._reset_streams = set()
        return connection

    def test_long_payloads_are_split_into_packet_sized_pieces(self):
        connection = self._bare_connection()
        connection.queue_stream_data(0, b"x" * (STREAM_CHUNK_SIZE * 2 + 5), fin=True)
        sizes = [len(piece) for _sid, piece, _fin in connection._outbound]
        self.assertEqual(sizes, [STREAM_CHUNK_SIZE, STREAM_CHUNK_SIZE, 5])

    def test_only_the_last_piece_carries_fin(self):
        connection = self._bare_connection()
        connection.queue_stream_data(0, b"x" * (STREAM_CHUNK_SIZE + 1), fin=True)
        fins = [fin for _sid, _piece, fin in connection._outbound]
        self.assertEqual(fins, [False, True])

    def test_an_empty_payload_with_fin_is_still_queued(self):
        connection = self._bare_connection()
        connection.queue_stream_data(8, b"", fin=True)
        self.assertEqual(connection._outbound, [(8, b"", True)])


class TestStreamingBackpressure(unittest.TestCase):
    def test_the_pump_waits_for_room_and_stops_when_it_never_comes(self):
        app = App()
        self.addCleanup(app.close)
        server = Http3Server("127.0.0.1", 0, app, None)
        server.STREAM_STALL_TIMEOUT_SECONDS = 0.01
        queued = []
        finished = threading.Event()
        closed = threading.Event()
        room = iter([True, True, False])
        waits = []

        def queue_stream_data(stream_id, data, fin=False):
            queued.append((stream_id, data, fin))
            if fin:
                finished.set()

        def wait_for_send_room(stream_id, max_queued, timeout=None):
            waits.append((stream_id, max_queued, timeout))
            return next(room)

        def endless():
            try:
                while True:
                    yield b"chunk"
            finally:
                closed.set()

        resets = []

        def reset_stream(stream_id, code):
            resets.append((stream_id, code))
            finished.set()

        connection = SimpleNamespace(
            closed=False,
            queue_stream_data=queue_stream_data,
            wait_for_send_room=wait_for_send_room,
            reset_stream=reset_stream,
        )
        server._send_response(connection, 0, Response.stream(endless()), send_body=True)

        self.assertTrue(finished.wait(timeout=5))
        self.assertTrue(closed.wait(timeout=5))
        data_frames = [entry for entry in queued[1:] if entry[1]]
        self.assertEqual(len(data_frames), 2)
        # A stalled body is reset, not ended with a FIN that would look complete.
        self.assertEqual(resets, [(0, http3.H3_INTERNAL_ERROR)])
        self.assertFalse(any(fin for _sid, _data, fin in queued))
        self.assertEqual(waits[0], (0, server.MAX_PENDING_STREAM_BYTES, 0.01))


class TestStreamingCompletion(unittest.TestCase):
    def test_a_finished_body_ends_with_fin_and_a_failing_one_is_reset(self):
        app = App()
        self.addCleanup(app.close)
        server = Http3Server("127.0.0.1", 0, app, None)

        def run(chunks):
            queued, resets, done = [], [], threading.Event()

            def queue_stream_data(stream_id, data, fin=False):
                queued.append((stream_id, data, fin))
                if fin:
                    done.set()

            def reset_stream(stream_id, code):
                resets.append((stream_id, code))
                done.set()

            connection = SimpleNamespace(
                closed=False,
                queue_stream_data=queue_stream_data,
                wait_for_send_room=lambda *_args, **_kwargs: True,
                reset_stream=reset_stream,
            )
            server._send_response(connection, 4, Response.stream(chunks), send_body=True)
            self.assertTrue(done.wait(timeout=5))
            return queued, resets

        queued, resets = run(iter([b"a", b"b"]))
        self.assertEqual(queued[-1], (4, b"", True))
        self.assertEqual(resets, [])

        def failing():
            yield b"a"
            raise RuntimeError("producer broke")

        queued, resets = run(failing())
        self.assertEqual(resets, [(4, http3.H3_INTERNAL_ERROR)])
        self.assertFalse(any(fin for _sid, _data, fin in queued))


class TestHttp3BodyLimit(unittest.TestCase):
    def test_an_oversized_body_is_answered_413_without_dispatch(self):
        app = App()
        self.addCleanup(app.close)
        calls = []

        @app.api("/upload", methods=("POST",))
        def upload(request):
            calls.append(len(request.body))
            return {"ok": True}

        server = Http3Server("127.0.0.1", 0, app, None, max_request_body_bytes=8)
        queued = []
        connection = SimpleNamespace(
            closed=False,
            client_address=("127.0.0.1", 1234),
            alpn="h3",
            queue_stream_data=lambda stream_id, data, fin=False: queued.append((stream_id, data, fin)),
        )
        headers = [
            (":method", "POST"),
            (":scheme", "https"),
            (":path", "/upload"),
            (":authority", "example.test"),
        ]
        request = http3.encode_frame(http3.FRAME_HEADERS, encode_field_section(headers))
        server._on_stream_data(connection, 0, request, False)
        server._on_stream_data(connection, 0, http3.encode_frame(http3.FRAME_DATA, b"x" * 9), False)
        server._on_stream_data(connection, 0, http3.encode_frame(http3.FRAME_DATA, b"y"), True)

        self.assertEqual(calls, [])
        self.assertEqual(len(queued), 1)
        stream_id, payload, fin = queued[0]
        self.assertEqual((stream_id, fin), (0, True))
        frames, _tail = http3.parse_frames(payload)
        self.assertEqual(dict(decode_field_section(frames[0][1]))[":status"], "413")

    def test_the_body_limit_must_not_be_negative(self):
        with self.assertRaises(ValueError):
            Http3Server("127.0.0.1", 0, None, None, max_request_body_bytes=-1)


class TestHttp3StreamCleanup(unittest.TestCase):
    def test_closing_a_connection_during_dispatch_closes_the_unused_producer(self):
        class Producer:
            iterations = 0
            close_calls = 0

            def __iter__(self):
                self.iterations += 1
                return iter([b"chunk"])

        class ClosingProducer(Producer):
            def __init__(self, fail_close=False):
                self.error = OSError("cleanup failed") if fail_close else None

            def close(self):
                self.close_calls += 1
                if self.error is not None:
                    raise self.error

        for producer in (ClosingProducer(), ClosingProducer(fail_close=True), Producer()):
            with self.subTest(producer=type(producer).__name__, error=getattr(producer, "error", None)):
                entered = threading.Event()
                release = threading.Event()
                queued = []

                def dispatch(request):
                    entered.set()
                    if not release.wait(2.0):
                        raise TimeoutError("dispatch was never released")
                    return Response.stream(producer)

                app = SimpleNamespace(dispatch=dispatch)
                server = Http3Server("127.0.0.1", 0, app, None)
                self.addCleanup(server._dispatch_pool.shutdown, wait=True)
                connection = SimpleNamespace(
                    closed=False,
                    queue_stream_data=lambda *args, **kwargs: queued.append((args, kwargs)),
                )
                request = SimpleNamespace(method="GET")
                with patch("wsbuilder.http3_server.get_log") as get_log:
                    future = server._dispatch_pool.submit(server._dispatch, connection, 12, request)
                    try:
                        self.assertTrue(entered.wait(2.0), "dispatch never started")
                        server._drop(connection)
                    finally:
                        release.set()
                    future.result(timeout=2.0)

                    self.assertEqual(producer.iterations, 0)
                    self.assertEqual(queued, [])
                    self.assertEqual(producer.close_calls, int(isinstance(producer, ClosingProducer)))
                    if getattr(producer, "error", None) is not None:
                        get_log.assert_called_once_with(app)
                        get_log.return_value.exception.assert_called_once_with(
                            "http3", "streaming cleanup on stream 12 failed: cleanup failed", producer.error,
                        )
                    else:
                        get_log.assert_not_called()

    def test_dropping_a_connection_wakes_all_websocket_readers(self):
        server = Http3Server("127.0.0.1", 0, None, None)
        connection = SimpleNamespace(
            closed=True, h3_streams={}, queue_stream_data=lambda *args, **kwargs: None,
        )
        other = SimpleNamespace(closed=False)
        server.connections = {b"client": connection, b"server": connection, b"other": other}
        readers = []
        results = {}
        for stream_id in (0, 4):
            stream = http3.RequestStream(stream_id)
            transport = Http3WebSocketStream(connection, stream_id)
            stream.websocket = transport
            connection.h3_streams[stream_id] = stream

            def read(transport=transport, stream_id=stream_id):
                results[stream_id] = transport.recv(1)

            reader = threading.Thread(target=read, daemon=True)
            self.addCleanup(reader.join, 2.0)
            self.addCleanup(transport.push_eof)
            reader.start()
            readers.append(reader)
        # Ordinary HTTP streams need no WebSocket notification.
        connection.h3_streams[8] = http3.RequestStream(8)

        server._drop(connection)
        for reader in readers:
            reader.join(2.0)
            self.assertFalse(reader.is_alive(), "WebSocket reader stayed blocked")
        self.assertEqual(results, {0: b"", 4: b""})
        self.assertEqual(server.connections, {b"other": other})
        self.assertFalse(other.closed)

    def test_streaming_always_finishes_after_producer_cleanup(self):
        server = Http3Server("127.0.0.1", 0, None, None)
        for fail_iteration, fail_close in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(fail_iteration=fail_iteration, fail_close=fail_close):
                class Producer:
                    close_calls = 0

                    def __iter__(self):
                        yield b"chunk"
                        if fail_iteration:
                            raise OSError("iteration failed")

                    def close(self):
                        self.close_calls += 1
                        if fail_close:
                            raise OSError("cleanup failed")

                queued = []
                finished = threading.Event()
                workers = []

                def queue_data(stream_id, data, fin=False):
                    queued.append((stream_id, data, fin))
                    if fin:
                        workers.append(threading.current_thread())
                        finished.set()

                connection = SimpleNamespace(closed=False, queue_stream_data=queue_data)
                producer = Producer()
                server._send_response(connection, 12, Response.stream(producer), send_body=True)
                self.assertTrue(finished.wait(2.0), "streaming response never queued FIN")
                workers[0].join(2.0)
                self.assertFalse(workers[0].is_alive())
                self.assertEqual(producer.close_calls, 1)
                self.assertEqual(queued[-1], (12, b"", True))
                self.assertEqual(sum(fin for _, _, fin in queued), 1)
                frames, tail = http3.parse_frames(b"".join(data for _, data, _ in queued))
                self.assertEqual(tail, b"")
                self.assertEqual(frames[-1], (http3.FRAME_DATA, b"chunk"))


if __name__ == "__main__":
    unittest.main()

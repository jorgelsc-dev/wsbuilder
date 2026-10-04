"""A socket-shaped view of one RFC 9220 WebSocket stream over HTTP/3.

After a ``CONNECT`` with ``:protocol: websocket`` is answered 200, the request
stream stays open in both directions and carries WebSocket bytes inside HTTP/3
DATA frames. :class:`wsbuilder.ws.WebSocket` only needs ``recv``, ``sendall``
and ``settimeout`` from the socket it wraps, so this class provides exactly
those. Bytes the peer sends are pushed in by the thread that owns the UDP
socket; bytes this side sends are queued for that same thread to packetize.
"""

import socket
import threading
import time

from .http3 import FRAME_DATA, encode_frame


class Http3WebSocketStream:
    """Blocking, timeout-aware byte stream backed by a QUIC stream."""

    def __init__(self, connection, stream_id):
        self._connection = connection
        self._stream_id = int(stream_id)
        self._cond = threading.Condition()
        self._buffer = bytearray()
        self._eof = False
        self._closed = False
        self._timeout = None

    # -- inbound: called by the socket-owning thread -------------------

    def push(self, data):
        """Add WebSocket bytes that arrived in DATA frames."""
        if not data:
            return
        with self._cond:
            self._buffer.extend(data)
            self._cond.notify_all()

    def push_eof(self):
        """The peer finished its side of the stream."""
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    # -- socket surface used by wsbuilder.ws ---------------------------

    def settimeout(self, timeout):
        self._timeout = None if timeout is None else float(timeout)

    def gettimeout(self):
        return self._timeout

    def recv(self, n):
        """Up to ``n`` bytes; ``b""`` at end of stream; ``socket.timeout`` when idle."""
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        with self._cond:
            while not self._buffer and not self._eof and not self._closed:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout("timed out")
                self._cond.wait(remaining)
            if self._buffer:
                chunk = bytes(self._buffer[:n])
                del self._buffer[:n]
                return chunk
            return b""

    def sendall(self, data):
        if self._closed:
            raise ConnectionError("WebSocket stream is closed")
        frame = encode_frame(FRAME_DATA, data)
        self._connection.queue_stream_data(self._stream_id, frame, fin=False)

    def close(self):
        """Finish the stream; the peer sees end of stream after queued bytes."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        self._connection.queue_stream_data(self._stream_id, b"", fin=True)


__all__ = ["Http3WebSocketStream"]

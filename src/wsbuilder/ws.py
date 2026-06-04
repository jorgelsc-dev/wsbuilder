import base64
import socket
import threading
import time
from dataclasses import dataclass

from .constants import MAGIC_WS
from .http import Response, send_http_response

MAX_WS_FRAME_PAYLOAD_BYTES = 2 * 1024 * 1024


class WebSocketProtocolError(Exception):
    pass


class WebSocketReadError(Exception):
    pass


class WebSocketReadTimeoutError(WebSocketReadError):
    pass


class WebSocketConnectionClosedError(WebSocketReadError):
    pass


@dataclass(slots=True)
class WebSocketFrame:
    fin: int
    opcode: int
    payload: bytes
    masked: bool
    mask: bytes | None

    def __iter__(self):
        yield self.fin
        yield self.opcode
        yield self.payload
        yield self.masked
        yield self.mask

    def __len__(self):
        return 5

    def __getitem__(self, index):
        values = (self.fin, self.opcode, self.payload, self.masked, self.mask)
        return values[index]


def _header_token_contains(raw_value, token):
    wanted = str(token or "").strip().lower()
    if not wanted:
        return False
    values = [part.strip().lower() for part in str(raw_value or "").split(",")]
    return wanted in values


def _is_valid_websocket_key(value):
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        decoded = base64.b64decode(raw.encode("ascii"), validate=True)
    except Exception:
        return False
    return len(decoded) == 16


def is_ws_request(headers):
    upgrade_ok = str(headers.get("upgrade", "")).strip().lower() == "websocket"
    connection_ok = _header_token_contains(headers.get("connection", ""), "upgrade")
    return upgrade_ok and connection_ok


def _normalize_protocols(value):
    if not value:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(str(part).strip() for part in value if str(part).strip())


def handshake_websocket_with_options(
    conn,
    addr,
    headers,
    *,
    supported_subprotocols=(),
    idle_timeout=0.0,
    keepalive_interval=0.0,
    pong_timeout=0.0,
    auto_pong=True,
    on_close=None,
    on_error=None,
    on_timeout=None,
    io_poll_interval=1.0,
    ping_payload=b"",
):
    key = headers.get("sec-websocket-key", "")
    if not key:
        send_http_response(conn, Response.text("Missing Sec-WebSocket-Key", status=400))
        return None
    if not _is_valid_websocket_key(key):
        send_http_response(conn, Response.text("Invalid Sec-WebSocket-Key", status=400))
        return None

    if not _header_token_contains(headers.get("connection", ""), "upgrade"):
        send_http_response(conn, Response.text("Missing/invalid Connection: Upgrade", status=400))
        return None
    if str(headers.get("upgrade", "")).strip().lower() != "websocket":
        send_http_response(conn, Response.text("Missing/invalid Upgrade: websocket", status=400))
        return None

    version = str(headers.get("sec-websocket-version", "")).strip()
    if version != "13":
        send_http_response(
            conn,
            Response(
                status=426,
                body=b"Unsupported WebSocket Version",
                headers={"Sec-WebSocket-Version": "13"},
            ),
        )
        return None

    supported = _normalize_protocols(supported_subprotocols)
    subprotocol = ""
    offered = headers.get("sec-websocket-protocol", "")
    if offered:
        parts = [p.strip() for p in offered.split(",") if p.strip()]
        if parts:
            if supported:
                for part in parts:
                    if part in supported:
                        subprotocol = part
                        break
            else:
                subprotocol = parts[0]

    accept_src = (key + MAGIC_WS).encode("utf-8")
    digest = sha1(accept_src)
    accept = base64_encode(digest)

    resp_headers = {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Accept": accept,
    }
    if subprotocol:
        resp_headers["Sec-WebSocket-Protocol"] = subprotocol

    send_http_response(conn, Response(status=101, body=b"", headers=resp_headers))
    return WebSocket(
        conn,
        addr,
        subprotocol,
        headers,
        idle_timeout=idle_timeout,
        keepalive_interval=keepalive_interval,
        pong_timeout=pong_timeout,
        auto_pong=auto_pong,
        on_close=on_close,
        on_error=on_error,
        on_timeout=on_timeout,
        io_poll_interval=io_poll_interval,
        ping_payload=ping_payload.encode("utf-8") if isinstance(ping_payload, str) else ping_payload,
        supported_subprotocols=supported,
    )


def handshake_websocket(conn, addr, headers):
    return handshake_websocket_with_options(conn, addr, headers)


class WebSocket:
    def __init__(
        self,
        sock,
        addr,
        subprotocol,
        headers,
        *,
        idle_timeout=0.0,
        keepalive_interval=0.0,
        pong_timeout=0.0,
        auto_pong=True,
        on_close=None,
        on_error=None,
        on_timeout=None,
        io_poll_interval=1.0,
        ping_payload=b"",
        supported_subprotocols=(),
    ):
        self.sock = sock
        self.addr = addr
        self.subprotocol = subprotocol or ""
        self.headers = headers or {}
        self.supported_subprotocols = tuple(supported_subprotocols or ())
        self.idle_timeout = max(0.0, float(idle_timeout))
        self.keepalive_interval = max(0.0, float(keepalive_interval))
        self.pong_timeout = max(0.0, float(pong_timeout))
        self.auto_pong = bool(auto_pong)
        self.on_close = on_close
        self.on_error = on_error
        self.on_timeout = on_timeout
        self.io_poll_interval = max(0.1, float(io_poll_interval or 1.0))
        self.ping_payload = (
            ping_payload.encode("utf-8")
            if isinstance(ping_payload, str)
            else bytes(ping_payload or b"")
        )
        self._send_lock = threading.Lock()
        self._closed = False
        self._close_reported = False
        self._peer_close_received = False
        self._awaiting_pong = False
        self._last_rx_at = time.monotonic()
        self._last_tx_at = self._last_rx_at
        self._last_ping_at = 0.0

        if self.idle_timeout > 0 or self.keepalive_interval > 0 or self.pong_timeout > 0:
            try:
                self.sock.settimeout(self.io_poll_interval)
            except Exception:
                pass

    def _now(self):
        return time.monotonic()

    def _invoke_callback(self, callback, *args):
        if not callback:
            return
        try:
            callback(*args)
        except Exception as exc:
            print(f"[ws] callback error: {exc}")

    def _mark_rx(self):
        self._last_rx_at = self._now()

    def _mark_tx(self):
        self._last_tx_at = self._now()

    def _report_close(self, code, reason):
        if self._close_reported:
            return
        self._close_reported = True
        self._invoke_callback(self.on_close, self, code, reason)

    def _report_error(self, exc):
        self._invoke_callback(self.on_error, self, exc)

    def _report_timeout(self, reason):
        self._invoke_callback(self.on_timeout, self, reason)

    def _close_with_reason(self, code, reason):
        self._closed = True
        self._report_close(code, reason)

    def _handle_idle_tick(self):
        now = self._now()
        if self.keepalive_interval > 0:
            if self._awaiting_pong and self.pong_timeout > 0 and (now - self._last_ping_at) >= self.pong_timeout:
                self._report_timeout("pong timeout")
                self._close_with_reason(1001, "Ping timeout")
                raise WebSocketReadTimeoutError("WebSocket pong timeout")

            if not self._awaiting_pong and (now - self._last_tx_at) >= self.keepalive_interval:
                try:
                    self.send_ping(self.ping_payload)
                except Exception as exc:
                    self._report_error(exc)
                    self._close_with_reason(1006, "Keepalive ping failed")
                    raise WebSocketConnectionClosedError("WebSocket keepalive ping failed") from exc
                self._awaiting_pong = True
                self._last_ping_at = now
                return True

        if self.idle_timeout > 0 and (now - self._last_rx_at) >= self.idle_timeout:
            self._report_timeout("idle timeout")
            self._close_with_reason(1001, "Idle timeout")
            raise WebSocketReadTimeoutError("WebSocket idle timeout")

        return False

    def recv_frame(self):
        while True:
            try:
                frame = read_ws_frame_raw(self.sock)
            except (socket.timeout, TimeoutError) as exc:
                if self._closed:
                    raise WebSocketConnectionClosedError("WebSocket already closed") from exc
                if self._handle_idle_tick():
                    continue
                self._report_timeout("read timeout")
                raise WebSocketReadTimeoutError("WebSocket read timed out") from exc
            except ConnectionError as exc:
                self._close_with_reason(1006, "Connection closed")
                raise WebSocketConnectionClosedError("WebSocket connection closed") from exc
            except OSError as exc:
                self._close_with_reason(1006, "Connection closed")
                raise WebSocketConnectionClosedError("WebSocket connection error") from exc
            except WebSocketProtocolError as exc:
                try:
                    self.close(1002, "Protocol Error")
                except Exception:
                    pass
                self._report_error(exc)
                raise

            self._mark_rx()
            if frame.opcode == 0x8:
                self._peer_close_received = True
                code, reason = parse_close_payload(frame.payload)
                self._report_close(code or 1000, reason or "")
            elif frame.opcode == 0x9 and self.auto_pong:
                try:
                    self.send_pong(frame.payload)
                except Exception as exc:
                    self._report_error(exc)
                    raise
            elif frame.opcode == 0xA:
                self._awaiting_pong = False
            return frame

    def send_frame(self, opcode, payload=b""):
        if self._closed:
            raise WebSocketConnectionClosedError("WebSocket is closed")
        data = make_ws_frame_bytes(opcode, payload)
        with self._send_lock:
            try:
                self.sock.sendall(data)
            except (ConnectionError, OSError) as exc:
                self._close_with_reason(1006, "Connection closed")
                self._report_error(exc)
                raise WebSocketConnectionClosedError("WebSocket send failed") from exc
        self._mark_tx()
        if opcode == 0x8:
            self._closed = True

    def send_text(self, text):
        self.send_frame(0x1, text.encode("utf-8"))

    def send_binary(self, data):
        self.send_frame(0x2, data)

    def send_ping(self, payload=b""):
        self.send_frame(0x9, payload)

    def send_pong(self, payload=b""):
        self.send_frame(0xA, payload)

    def close(self, code=1000, reason=""):
        if self._closed:
            return
        payload = code.to_bytes(2, "big")
        if reason:
            payload += reason.encode("utf-8")
        try:
            self.send_frame(0x8, payload)
        finally:
            self._closed = True
            self._report_close(code, reason)


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    return data


def read_ws_frame_raw(conn):
    hdr = recv_exact(conn, 2)
    b1, b2 = hdr[0], hdr[1]
    fin = (b1 >> 7) & 1
    rsv = (b1 >> 4) & 0x07
    opcode = b1 & 0x0F
    masked = (b2 >> 7) & 1
    payload_len = b2 & 0x7F

    if rsv != 0:
        raise WebSocketProtocolError("RSV bits must be zero without negotiated extensions")

    if opcode in {0x3, 0x4, 0x5, 0x6, 0x7, 0xB, 0xC, 0xD, 0xE, 0xF}:
        raise WebSocketProtocolError(f"Reserved/unsupported opcode: {opcode}")

    if not masked:
        raise WebSocketProtocolError("Client-to-server frames must be masked")

    if payload_len == 126:
        ext = recv_exact(conn, 2)
        payload_len = int.from_bytes(ext, "big")
    elif payload_len == 127:
        ext = recv_exact(conn, 8)
        payload_len = int.from_bytes(ext, "big")

    if opcode >= 0x8:
        if not fin:
            raise WebSocketProtocolError("Control frames must not be fragmented")
        if payload_len > 125:
            raise WebSocketProtocolError("Control frame payload too large")

    if payload_len > MAX_WS_FRAME_PAYLOAD_BYTES:
        raise WebSocketProtocolError("Frame payload exceeds server limit")

    mask = None
    if masked:
        mask = recv_exact(conn, 4)

    payload = b""
    if payload_len:
        payload = recv_exact(conn, payload_len)
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return WebSocketFrame(fin, opcode, payload, bool(masked), mask)


def make_ws_frame_bytes(opcode, payload=b""):
    fin = 0x80
    b1 = fin | (opcode & 0x0F)
    payload_len = len(payload)
    header = bytes([b1])

    if payload_len <= 125:
        header += bytes([payload_len])
    elif payload_len <= 65535:
        header += bytes([126]) + payload_len.to_bytes(2, "big")
    else:
        header += bytes([127]) + payload_len.to_bytes(8, "big")

    return header + payload


def parse_close_payload(payload):
    if not payload:
        return None, None
    if len(payload) >= 2:
        code = int.from_bytes(payload[:2], "big")
        reason = ""
        if len(payload) > 2:
            try:
                reason = payload[2:].decode("utf-8", errors="ignore")
            except Exception:
                reason = ""
        return code, reason
    return None, None


def _left_rotate(n, b):
    return ((n << b) | (n >> (32 - b))) & 0xFFFFFFFF


def sha1(data_bytes):
    message = bytearray(data_bytes)
    orig_len_bits = (8 * len(message)) & 0xFFFFFFFFFFFFFFFF
    message.append(0x80)
    while (len(message) * 8) % 512 != 448:
        message.append(0)
    message += orig_len_bits.to_bytes(8, "big")

    h0 = 0x67452301
    h1 = 0xEFCDAB89
    h2 = 0x98BADCFE
    h3 = 0x10325476
    h4 = 0xC3D2E1F0

    for i in range(0, len(message), 64):
        w = [0] * 80
        chunk = message[i : i + 64]
        for j in range(16):
            w[j] = int.from_bytes(chunk[j * 4 : (j + 1) * 4], "big")
        for j in range(16, 80):
            w[j] = _left_rotate(w[j - 3] ^ w[j - 8] ^ w[j - 14] ^ w[j - 16], 1)

        a, b, c, d, e = h0, h1, h2, h3, h4
        for t in range(80):
            if 0 <= t <= 19:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif 20 <= t <= 39:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif 40 <= t <= 59:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6
            tmp = (_left_rotate(a, 5) + f + e + k + w[t]) & 0xFFFFFFFF
            e = d
            d = c
            c = _left_rotate(b, 30)
            b = a
            a = tmp

        h0 = (h0 + a) & 0xFFFFFFFF
        h1 = (h1 + b) & 0xFFFFFFFF
        h2 = (h2 + c) & 0xFFFFFFFF
        h3 = (h3 + d) & 0xFFFFFFFF
        h4 = (h4 + e) & 0xFFFFFFFF

    digest = b"".join(x.to_bytes(4, "big") for x in [h0, h1, h2, h3, h4])
    return digest


B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def base64_encode(data_bytes):
    res = []
    i = 0
    n = len(data_bytes)
    while i < n:
        b = data_bytes[i : i + 3]
        i += 3
        pad = 3 - len(b)
        val = 0
        for x in b:
            val = (val << 8) + x
        val <<= pad * 8
        for j in range(18, -1, -6):
            idx = (val >> j) & 0x3F
            res.append(B64_ALPHABET[idx])
        if pad:
            res[-pad:] = "=" * pad
    return "".join(res)

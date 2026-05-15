from .constants import MAGIC_WS
from .http import Response, send_http_response


def is_ws_request(headers):
    return headers.get("upgrade", "").lower() == "websocket"


def handshake_websocket(conn, addr, headers):
    key = headers.get("sec-websocket-key", "")
    if not key:
        send_http_response(conn, Response.text("Missing Sec-WebSocket-Key", status=400))
        return None

    subprotocol = ""
    offered = headers.get("sec-websocket-protocol", "")
    if offered:
        parts = [p.strip() for p in offered.split(",") if p.strip()]
        if parts:
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
    return WebSocket(conn, addr, subprotocol, headers)


class WebSocket:
    def __init__(self, sock, addr, subprotocol, headers):
        self.sock = sock
        self.addr = addr
        self.subprotocol = subprotocol or ""
        self.headers = headers or {}

    def recv_frame(self):
        return read_ws_frame_raw(self.sock)

    def send_frame(self, opcode, payload=b""):
        self.sock.sendall(make_ws_frame_bytes(opcode, payload))

    def send_text(self, text):
        self.send_frame(0x1, text.encode("utf-8"))

    def send_binary(self, data):
        self.send_frame(0x2, data)

    def send_ping(self, payload=b""):
        self.send_frame(0x9, payload)

    def send_pong(self, payload=b""):
        self.send_frame(0xA, payload)

    def close(self, code=1000, reason=""):
        payload = code.to_bytes(2, "big")
        if reason:
            payload += reason.encode("utf-8")
        self.send_frame(0x8, payload)


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
    opcode = b1 & 0x0F
    masked = (b2 >> 7) & 1
    payload_len = b2 & 0x7F

    if payload_len == 126:
        ext = recv_exact(conn, 2)
        payload_len = int.from_bytes(ext, "big")
    elif payload_len == 127:
        ext = recv_exact(conn, 8)
        payload_len = int.from_bytes(ext, "big")

    mask = None
    if masked:
        mask = recv_exact(conn, 4)

    payload = b""
    if payload_len:
        payload = recv_exact(conn, payload_len)
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, bool(masked), mask


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


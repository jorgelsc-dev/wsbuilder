"""QPACK field compression for HTTP/3 (RFC 9204).

HPACK cannot be reused as is. Its dynamic table depends on header blocks
arriving in the order they were encoded, which QUIC does not guarantee:
streams are delivered independently, so block 5 may arrive before block 3.
QPACK answers that by moving table updates onto their own ordered stream and
having each block declare which table state it needs.

This encoder never inserts into the dynamic table and advertises a capacity
of zero, so blocks never block on table state. That is a supported mode, not
a shortcut -- RFC 9204 section 3.2.2 makes the dynamic table optional -- and
it costs compression on repeated custom headers while keeping the static
table, which covers the fields that matter most.
"""

from .hpack import HPACKError, decode_integer, encode_integer, huffman_decode, huffman_encode

#: RFC 9204 appendix A. Indices are absolute in the static table.
STATIC_TABLE = (
    (":authority", ""), (":path", "/"), ("age", "0"),
    ("content-disposition", ""), ("content-length", "0"), ("cookie", ""),
    ("date", ""), ("etag", ""), ("if-modified-since", ""), ("if-none-match", ""),
    ("last-modified", ""), ("link", ""), ("location", ""), ("referer", ""),
    ("set-cookie", ""), (":method", "CONNECT"), (":method", "DELETE"),
    (":method", "GET"), (":method", "HEAD"), (":method", "OPTIONS"),
    (":method", "POST"), (":method", "PUT"), (":scheme", "http"),
    (":scheme", "https"), (":status", "103"), (":status", "200"),
    (":status", "304"), (":status", "404"), (":status", "503"),
    ("accept", "*/*"), ("accept", "application/dns-message"),
    ("accept-encoding", "gzip, deflate, br"), ("accept-ranges", "bytes"),
    ("access-control-allow-headers", "cache-control"),
    ("access-control-allow-headers", "content-type"),
    ("access-control-allow-origin", "*"), ("cache-control", "max-age=0"),
    ("cache-control", "max-age=2592000"), ("cache-control", "max-age=604800"),
    ("cache-control", "no-cache"), ("cache-control", "no-store"),
    ("cache-control", "public, max-age=31536000"),
    ("content-encoding", "br"), ("content-encoding", "gzip"),
    ("content-type", "application/dns-message"),
    ("content-type", "application/javascript"),
    ("content-type", "application/json"),
    ("content-type", "application/x-www-form-urlencoded"),
    ("content-type", "image/gif"), ("content-type", "image/jpeg"),
    ("content-type", "image/png"), ("content-type", "text/css"),
    ("content-type", "text/html; charset=utf-8"),
    ("content-type", "text/plain"), ("content-type", "text/plain;charset=utf-8"),
    ("range", "bytes=0-"), ("strict-transport-security", "max-age=31536000"),
    ("strict-transport-security", "max-age=31536000; includesubdomains"),
    ("strict-transport-security", "max-age=31536000; includesubdomains; preload"),
    ("vary", "accept-encoding"), ("vary", "origin"),
    ("x-content-type-options", "nosniff"), ("x-xss-protection", "1; mode=block"),
    (":status", "100"), (":status", "204"), (":status", "206"),
    (":status", "302"), (":status", "400"), (":status", "403"),
    (":status", "421"), (":status", "425"), (":status", "500"),
    ("accept-language", ""), ("access-control-allow-credentials", "FALSE"),
    ("access-control-allow-credentials", "TRUE"),
    ("access-control-allow-headers", "*"), ("access-control-allow-methods", "get"),
    ("access-control-allow-methods", "get, post, options"),
    ("access-control-allow-methods", "options"),
    ("access-control-expose-headers", "content-length"),
    ("access-control-request-headers", "content-type"),
    ("access-control-request-method", "get"),
    ("access-control-request-method", "post"), ("alt-svc", "clear"),
    ("authorization", ""),
    ("content-security-policy", "script-src 'none'; object-src 'none'; base-uri 'none'"),
    ("early-data", "1"), ("expect-ct", ""), ("forwarded", ""), ("if-range", ""),
    ("origin", ""), ("purpose", "prefetch"), ("server", ""),
    ("timing-allow-origin", "*"), ("upgrade-insecure-requests", "1"),
    ("user-agent", ""), ("x-forwarded-for", ""), ("x-frame-options", "deny"),
    ("x-frame-options", "sameorigin"),
)

STATIC_TABLE_SIZE = len(STATIC_TABLE)

_STATIC_EXACT = {}
_STATIC_BY_NAME = {}
for _index, (_name, _value) in enumerate(STATIC_TABLE):
    _STATIC_EXACT.setdefault((_name, _value), _index)
    _STATIC_BY_NAME.setdefault(_name, _index)


class QPACKError(Exception):
    """A field section that cannot be decoded."""


def encode_prefix(required_insert_count=0, base=0):
    """The field section prefix: which table state the block needs."""
    return encode_integer(required_insert_count, 8) + encode_integer(base, 7)


def decode_prefix(data, offset=0):
    required, offset = decode_integer(data, offset, 8)
    if offset > len(data):
        raise QPACKError("truncated field section prefix")
    sign = bool(data[offset - 1] & 0x80) if offset else False
    delta, offset = decode_integer(data, offset, 7)
    return required, delta, sign, offset


def _encode_string(value, prefix_bits, pattern, huffman=True):
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    if huffman:
        coded = huffman_encode(raw)
        if len(coded) < len(raw):
            head = bytearray(encode_integer(len(coded), prefix_bits))
            head[0] |= pattern | (1 << prefix_bits)
            return bytes(head) + coded
    head = bytearray(encode_integer(len(raw), prefix_bits))
    head[0] |= pattern
    return bytes(head) + raw


def _decode_string(data, offset, prefix_bits):
    if offset >= len(data):
        raise QPACKError("truncated string")
    huffman = bool(data[offset] & (1 << prefix_bits))
    length, offset = decode_integer(data, offset, prefix_bits)
    end = offset + length
    if end > len(data):
        raise QPACKError("string runs past the field section")
    raw = bytes(data[offset:end])
    if huffman:
        try:
            raw = huffman_decode(raw)
        except HPACKError as exc:
            raise QPACKError(str(exc)) from exc
    return raw.decode("utf-8"), end


def encode_field_section(headers, huffman=True):
    """Encode a field section using the static table and literals only."""
    out = bytearray(encode_prefix(0, 0))
    for name, value in headers:
        name = name.lower() if isinstance(name, str) else name.decode().lower()
        value = value if isinstance(value, str) else value.decode()

        exact = _STATIC_EXACT.get((name, value))
        if exact is not None:
            # 1Txxxxxx with T=1 for the static table.
            head = bytearray(encode_integer(exact, 6))
            head[0] |= 0xC0
            out += head
            continue

        named = _STATIC_BY_NAME.get(name)
        if named is not None:
            # 01NTxxxx, N=0 (may be indexed), T=1 (static).
            head = bytearray(encode_integer(named, 4))
            head[0] |= 0x50
            out += head
            out += _encode_string(value, 7, 0x00, huffman)
            continue

        # 001NHxxx: literal name and value.
        out += _encode_string(name, 3, 0x20, huffman)
        out += _encode_string(value, 7, 0x00, huffman)
    return bytes(out)


def decode_field_section(data):
    """Decode a field section, refusing any dynamic table reference."""
    data = bytes(data)
    required, _delta, _sign, offset = decode_prefix(data)
    if required:
        # We advertise a capacity of zero, so a peer asking us to wait for
        # table entries is asking for something we said we would not do.
        raise QPACKError("field section requires dynamic table entries")

    headers = []
    while offset < len(data):
        byte = data[offset]
        if byte & 0x80:
            static = bool(byte & 0x40)
            index, offset = decode_integer(data, offset, 6)
            if not static:
                raise QPACKError("dynamic table reference in an indexed field")
            headers.append(_static(index))
        elif byte & 0x40:
            static = bool(byte & 0x10)
            index, offset = decode_integer(data, offset, 4)
            if not static:
                raise QPACKError("dynamic table reference in a named field")
            value, offset = _decode_string(data, offset, 7)
            headers.append((_static(index)[0], value))
        elif byte & 0x20:
            name, offset = _decode_string(data, offset, 3)
            value, offset = _decode_string(data, offset, 7)
            headers.append((name.lower(), value))
        elif byte & 0x10:
            raise QPACKError("post-base indexing needs a dynamic table")
        else:
            raise QPACKError(f"unknown field line pattern 0x{byte:02x}")
    return headers


def _static(index):
    if not 0 <= index < STATIC_TABLE_SIZE:
        raise QPACKError(f"static table index {index} out of range")
    return STATIC_TABLE[index]


__all__ = [
    "QPACKError",
    "STATIC_TABLE",
    "STATIC_TABLE_SIZE",
    "decode_field_section",
    "decode_prefix",
    "encode_field_section",
    "encode_prefix",
]

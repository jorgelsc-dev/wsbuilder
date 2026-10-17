"""QPACK field compression for HTTP/3 (RFC 9204).

HPACK cannot be reused as is. Its dynamic table depends on header blocks
arriving in the order they were encoded, which QUIC does not guarantee:
streams are delivered independently, so block 5 may arrive before block 3.
QPACK answers that by moving table updates onto their own ordered stream and
having each block declare which table state it needs.

The dynamic table lives here too, on its own ordered stream. Insertions are
numbered, a field section declares the insert count it needs, and a decoder
that has not seen that far would have to wait -- which is the head-of-line
blocking QPACK exists to bound. This encoder therefore only references
entries it has already pushed and acknowledged as inserted, so a block is
never blocked, and it falls back to literals whenever the table cannot help.
Capacity zero, the mode the server advertises by default, disables the table
entirely and is still fully supported.
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


#: An entry costs its octets plus 32, as in HPACK (RFC 9204 section 3.2.1).
ENTRY_OVERHEAD = 32

#: Never placed in the table: an intermediary shares it across connections,
#: and a value recovered from there is a credential (RFC 9204 section 7.1).
NEVER_INDEXED = frozenset(
    {"authorization", "cookie", "set-cookie", "proxy-authorization"}
)


def _never_index(name):
    return name in NEVER_INDEXED

# Encoder stream instructions (RFC 9204 section 4.3).
INSERT_WITH_NAME_REFERENCE = 0x80
INSERT_WITH_LITERAL_NAME = 0x40
SET_DYNAMIC_TABLE_CAPACITY = 0x20
DUPLICATE = 0x00

# Decoder stream instructions (section 4.4).
SECTION_ACKNOWLEDGEMENT = 0x80
STREAM_CANCELLATION = 0x40
INSERT_COUNT_INCREMENT = 0x00


class DynamicTable:
    """Insertions both peers replay in the same order.

    Entries are addressed by *absolute* index, counted from the first
    insertion ever made, which is what lets a field section name an entry
    without depending on how much has been evicted since.
    """

    def __init__(self, capacity=0):
        self._entries = []
        self._size = 0
        self._capacity = int(capacity)
        #: Total insertions ever made, never reduced by eviction.
        self.insert_count = 0

    def __len__(self):
        return len(self._entries)

    @property
    def size(self):
        return self._size

    @property
    def capacity(self):
        return self._capacity

    def set_capacity(self, capacity):
        self._capacity = int(capacity)
        self._evict()

    @staticmethod
    def entry_size(name, value):
        return len(name.encode("utf-8")) + len(value.encode("utf-8")) + ENTRY_OVERHEAD

    def _evict(self):
        while self._size > self._capacity and self._entries:
            name, value = self._entries.pop(0)
            self._size -= self.entry_size(name, value)

    def add(self, name, value):
        """Insert, returning the absolute index, or None if it does not fit."""
        cost = self.entry_size(name, value)
        if cost > self._capacity:
            return None
        self._entries.append((name, value))
        self._size += cost
        self.insert_count += 1
        self._evict()
        return self.insert_count - 1

    @property
    def dropped(self):
        """How many insertions have been evicted; the first index still held."""
        return self.insert_count - len(self._entries)

    def get(self, absolute_index):
        position = absolute_index - self.dropped
        if not 0 <= position < len(self._entries):
            raise QPACKError(f"dynamic table index {absolute_index} is no longer held")
        return self._entries[position]

    def find(self, name, value=None):
        """Absolute index of a match, newest first, plus whether it was exact."""
        named = None
        for position in range(len(self._entries) - 1, -1, -1):
            entry_name, entry_value = self._entries[position]
            if entry_name != name:
                continue
            absolute = self.dropped + position
            if value is not None and entry_value == value:
                return absolute, True
            if named is None:
                named = absolute
        return named, False

    def entries(self):
        return list(self._entries)


def encode_capacity_instruction(capacity):
    head = bytearray(encode_integer(capacity, 5))
    head[0] |= SET_DYNAMIC_TABLE_CAPACITY
    return bytes(head)


def encode_insert_with_name_reference(index, value, static=True, huffman=True):
    head = bytearray(encode_integer(index, 6))
    head[0] |= INSERT_WITH_NAME_REFERENCE | (0x40 if static else 0x00)
    return bytes(head) + _encode_string(value, 7, 0x00, huffman)


def encode_insert_with_literal_name(name, value, huffman=True):
    head = _encode_string(name, 5, INSERT_WITH_LITERAL_NAME, huffman)
    return head + _encode_string(value, 7, 0x00, huffman)


def encode_duplicate(relative_index):
    return encode_integer(relative_index, 5)


def decode_encoder_stream(data, table):
    """Apply a peer's encoder stream instructions to ``table``."""
    data = bytes(data)
    offset = 0
    applied = 0
    while offset < len(data):
        byte = data[offset]
        if byte & SET_DYNAMIC_TABLE_CAPACITY and not byte & 0xC0:
            capacity, offset = decode_integer(data, offset, 5)
            table.set_capacity(capacity)
        elif byte & INSERT_WITH_NAME_REFERENCE:
            static = bool(byte & 0x40)
            index, offset = decode_integer(data, offset, 6)
            value, offset = _decode_string(data, offset, 7)
            name = _static(index)[0] if static else table.get(index)[0]
            table.add(name, value)
            applied += 1
        elif byte & INSERT_WITH_LITERAL_NAME:
            name, offset = _decode_string(data, offset, 5)
            value, offset = _decode_string(data, offset, 7)
            table.add(name.lower(), value)
            applied += 1
        else:
            relative, offset = decode_integer(data, offset, 5)
            name, value = table.get(table.insert_count - 1 - relative)
            table.add(name, value)
            applied += 1
    return applied


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


def encode_required_insert_count(count, max_entries):
    """The prefix encodes the count modulo the table, not the count itself."""
    if count == 0 or not max_entries:
        return 0
    return (count % (2 * max_entries)) + 1


def decode_required_insert_count(encoded, max_entries, total_inserted):
    if encoded == 0:
        return 0
    if not max_entries:
        raise QPACKError("insert count given for a table of no capacity")
    full_range = 2 * max_entries
    if encoded > full_range:
        raise QPACKError("encoded insert count out of range")
    max_value = total_inserted + max_entries
    wrapped = max_value - (max_value % full_range) + encoded - 1
    if wrapped > max_value:
        wrapped -= full_range
    return wrapped


def decode_field_section(data, table=None):
    """Decode a field section, resolving dynamic references against ``table``."""
    data = bytes(data)
    required_encoded, delta, sign, offset = decode_prefix(data)
    max_entries = (table.capacity // ENTRY_OVERHEAD) if table is not None else 0
    total = table.insert_count if table is not None else 0
    required = decode_required_insert_count(required_encoded, max_entries, total)
    if required and (table is None or required > table.insert_count):
        # Waiting for entries we have not seen is the head-of-line blocking
        # QPACK exists to bound; we decline rather than stall the stream.
        raise QPACKError("field section requires dynamic table entries")
    base = required - delta if sign else required + delta

    headers = []
    while offset < len(data):
        byte = data[offset]
        if byte & 0x80:
            static = bool(byte & 0x40)
            index, offset = decode_integer(data, offset, 6)
            if static:
                headers.append(_static(index))
            else:
                headers.append(_dynamic(table, base - index - 1))
        elif byte & 0x40:
            static = bool(byte & 0x10)
            index, offset = decode_integer(data, offset, 4)
            name = (
                _static(index)[0] if static else _dynamic(table, base - index - 1)[0]
            )
            value, offset = _decode_string(data, offset, 7)
            headers.append((name, value))
        elif byte & 0x20:
            name, offset = _decode_string(data, offset, 3)
            value, offset = _decode_string(data, offset, 7)
            headers.append((name.lower(), value))
        elif byte & 0x10:
            raise QPACKError("post-base indexing needs a dynamic table")
        else:
            raise QPACKError(f"unknown field line pattern 0x{byte:02x}")
    return headers


def _dynamic(table, absolute_index):
    if table is None:
        raise QPACKError("dynamic table reference without a table")
    return table.get(absolute_index)


def _static(index):
    if not 0 <= index < STATIC_TABLE_SIZE:
        raise QPACKError(f"static table index {index} out of range")
    return STATIC_TABLE[index]


class Encoder:
    """Encodes field sections, inserting into the dynamic table when useful.

    Only entries this encoder has already pushed on the encoder stream are
    referenced, so a peer decoding a section never has to wait for one.
    """

    def __init__(self, capacity=0):
        self.table = DynamicTable(capacity)
        self._pending_instructions = bytearray()
        if capacity:
            self._pending_instructions += encode_capacity_instruction(capacity)

    def set_capacity(self, capacity):
        self.table.set_capacity(capacity)
        self._pending_instructions += encode_capacity_instruction(capacity)

    def take_encoder_stream(self):
        """Instructions to send before the sections that reference them."""
        data = bytes(self._pending_instructions)
        self._pending_instructions = bytearray()
        return data

    def encode(self, headers, huffman=True):
        fields = []
        for name, value in headers:
            name = name.lower() if isinstance(name, str) else name.decode().lower()
            fields.append((name, value if isinstance(value, str) else value.decode()))

        # Two passes. Insertions during the first change insert_count, and a
        # relative index only means anything against the Base in the prefix,
        # which is that final count -- so nothing is emitted until it settles.
        plan = []
        required = 0
        for name, value in fields:
            exact = _STATIC_EXACT.get((name, value))
            if exact is not None:
                plan.append(("static", exact, None))
                continue

            absolute, is_exact = self.table.find(name, value)
            if not is_exact and self.table.capacity and not _never_index(name):
                inserted = self._insert(name, value)
                if inserted is not None:
                    absolute, is_exact = inserted, True

            if is_exact and absolute is not None:
                required = max(required, absolute + 1)
                plan.append(("dynamic", absolute, None))
                continue

            named = _STATIC_BY_NAME.get(name)
            if named is not None:
                plan.append(("static-name", named, value))
                continue
            plan.append(("literal", name, value))

        base = self.table.insert_count
        body = bytearray()
        for kind, first, value in plan:
            if kind == "static":
                head = bytearray(encode_integer(first, 6))
                head[0] |= 0xC0
                body += head
            elif kind == "dynamic":
                head = bytearray(encode_integer(base - first - 1, 6))
                head[0] |= 0x80
                body += head
            elif kind == "static-name":
                head = bytearray(encode_integer(first, 4))
                head[0] |= 0x50
                body += head
                body += _encode_string(value, 7, 0x00, huffman)
            else:
                pattern = 0x30 if _never_index(first) else 0x20
                body += _encode_string(first, 3, pattern, huffman)
                body += _encode_string(value, 7, 0x00, huffman)

        max_entries = self.table.capacity // ENTRY_OVERHEAD
        encoded_required = encode_required_insert_count(required, max_entries)
        if required:
            prefix = encode_integer(encoded_required, 8) + encode_integer(base - required, 7)
        else:
            prefix = encode_prefix(0, 0)
        return prefix + bytes(body)

    def _insert(self, name, value):
        named = _STATIC_BY_NAME.get(name)
        if named is not None:
            instruction = encode_insert_with_name_reference(named, value, static=True)
        else:
            instruction = encode_insert_with_literal_name(name, value)
        absolute = self.table.add(name, value)
        if absolute is None:
            return None
        self._pending_instructions += instruction
        return absolute


__all__ = [
    "DynamicTable",
    "ENTRY_OVERHEAD",
    "NEVER_INDEXED",
    "Encoder",
    "QPACKError",
    "decode_encoder_stream",
    "encode_capacity_instruction",
    "encode_insert_with_literal_name",
    "encode_insert_with_name_reference",
    "STATIC_TABLE",
    "STATIC_TABLE_SIZE",
    "decode_field_section",
    "decode_prefix",
    "encode_field_section",
    "encode_prefix",
]

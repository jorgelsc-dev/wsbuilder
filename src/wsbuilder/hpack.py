"""HPACK header compression for HTTP/2 (RFC 7541).

Three pieces make up the format: a prefixed integer encoding, string literals
that are optionally Huffman coded, and a table of headers split between a
fixed static half and a dynamic half the peers grow in lockstep.

The dynamic table is the part that has to be exact. Encoder and decoder each
keep their own copy and never exchange it, so a single disagreement about
what was inserted or evicted desynchronises the connection permanently --
which is why RFC 7541 makes that a connection error rather than a stream one.
"""

#: RFC 7541 appendix A. Index 0 is unused so positions match the spec.
STATIC_TABLE = (
    ("", ""),
    (":authority", ""),
    (":method", "GET"),
    (":method", "POST"),
    (":path", "/"),
    (":path", "/index.html"),
    (":scheme", "http"),
    (":scheme", "https"),
    (":status", "200"),
    (":status", "204"),
    (":status", "206"),
    (":status", "304"),
    (":status", "400"),
    (":status", "404"),
    (":status", "500"),
    ("accept-charset", ""),
    ("accept-encoding", "gzip, deflate"),
    ("accept-language", ""),
    ("accept-ranges", ""),
    ("accept", ""),
    ("access-control-allow-origin", ""),
    ("age", ""),
    ("allow", ""),
    ("authorization", ""),
    ("cache-control", ""),
    ("content-disposition", ""),
    ("content-encoding", ""),
    ("content-language", ""),
    ("content-length", ""),
    ("content-location", ""),
    ("content-range", ""),
    ("content-type", ""),
    ("cookie", ""),
    ("date", ""),
    ("etag", ""),
    ("expect", ""),
    ("expires", ""),
    ("from", ""),
    ("host", ""),
    ("if-match", ""),
    ("if-modified-since", ""),
    ("if-none-match", ""),
    ("if-range", ""),
    ("if-unmodified-since", ""),
    ("last-modified", ""),
    ("link", ""),
    ("location", ""),
    ("max-forwards", ""),
    ("proxy-authenticate", ""),
    ("proxy-authorization", ""),
    ("range", ""),
    ("referer", ""),
    ("refresh", ""),
    ("retry-after", ""),
    ("server", ""),
    ("set-cookie", ""),
    ("strict-transport-security", ""),
    ("transfer-encoding", ""),
    ("user-agent", ""),
    ("vary", ""),
    ("via", ""),
    ("www-authenticate", ""),
)

STATIC_TABLE_SIZE = len(STATIC_TABLE) - 1

#: RFC 7541 appendix B: (code, bit length) per symbol, 256 plus EOS.
HUFFMAN_CODES = (
    (0x1FF8, 13), (0x7FFFD8, 23), (0xFFFFFE2, 28), (0xFFFFFE3, 28),
    (0xFFFFFE4, 28), (0xFFFFFE5, 28), (0xFFFFFE6, 28), (0xFFFFFE7, 28),
    (0xFFFFFE8, 28), (0xFFFFEA, 24), (0x3FFFFFFC, 30), (0xFFFFFE9, 28),
    (0xFFFFFEA, 28), (0x3FFFFFFD, 30), (0xFFFFFEB, 28), (0xFFFFFEC, 28),
    (0xFFFFFED, 28), (0xFFFFFEE, 28), (0xFFFFFEF, 28), (0xFFFFFF0, 28),
    (0xFFFFFF1, 28), (0xFFFFFF2, 28), (0x3FFFFFFE, 30), (0xFFFFFF3, 28),
    (0xFFFFFF4, 28), (0xFFFFFF5, 28), (0xFFFFFF6, 28), (0xFFFFFF7, 28),
    (0xFFFFFF8, 28), (0xFFFFFF9, 28), (0xFFFFFFA, 28), (0xFFFFFFB, 28),
    (0x14, 6), (0x3F8, 10), (0x3F9, 10), (0xFFA, 12),
    (0x1FF9, 13), (0x15, 6), (0xF8, 8), (0x7FA, 11),
    (0x3FA, 10), (0x3FB, 10), (0xF9, 8), (0x7FB, 11),
    (0xFA, 8), (0x16, 6), (0x17, 6), (0x18, 6),
    (0x0, 5), (0x1, 5), (0x2, 5), (0x19, 6),
    (0x1A, 6), (0x1B, 6), (0x1C, 6), (0x1D, 6),
    (0x1E, 6), (0x1F, 6), (0x5C, 7), (0xFB, 8),
    (0x7FFC, 15), (0x20, 6), (0xFFB, 12), (0x3FC, 10),
    (0x1FFA, 13), (0x21, 6), (0x5D, 7), (0x5E, 7),
    (0x5F, 7), (0x60, 7), (0x61, 7), (0x62, 7),
    (0x63, 7), (0x64, 7), (0x65, 7), (0x66, 7),
    (0x67, 7), (0x68, 7), (0x69, 7), (0x6A, 7),
    (0x6B, 7), (0x6C, 7), (0x6D, 7), (0x6E, 7),
    (0x6F, 7), (0x70, 7), (0x71, 7), (0x72, 7),
    (0xFC, 8), (0x73, 7), (0xFD, 8), (0x1FFB, 13),
    (0x7FFF0, 19), (0x1FFC, 13), (0x3FFC, 14), (0x22, 6),
    (0x7FFD, 15), (0x3, 5), (0x23, 6), (0x4, 5),
    (0x24, 6), (0x5, 5), (0x25, 6), (0x26, 6),
    (0x27, 6), (0x6, 5), (0x74, 7), (0x75, 7),
    (0x28, 6), (0x29, 6), (0x2A, 6), (0x7, 5),
    (0x2B, 6), (0x76, 7), (0x2C, 6), (0x8, 5),
    (0x9, 5), (0x2D, 6), (0x77, 7), (0x78, 7),
    (0x79, 7), (0x7A, 7), (0x7B, 7), (0x7FFE, 15),
    (0x7FC, 11), (0x3FFD, 14), (0x1FFD, 13), (0xFFFFFFC, 28),
    (0xFFFE6, 20), (0x3FFFD2, 22), (0xFFFE7, 20), (0xFFFE8, 20),
    (0x3FFFD3, 22), (0x3FFFD4, 22), (0x3FFFD5, 22), (0x7FFFD9, 23),
    (0x3FFFD6, 22), (0x7FFFDA, 23), (0x7FFFDB, 23), (0x7FFFDC, 23),
    (0x7FFFDD, 23), (0x7FFFDE, 23), (0xFFFFEB, 24), (0x7FFFDF, 23),
    (0xFFFFEC, 24), (0xFFFFED, 24), (0x3FFFD7, 22), (0x7FFFE0, 23),
    (0xFFFFEE, 24), (0x7FFFE1, 23), (0x7FFFE2, 23), (0x7FFFE3, 23),
    (0x7FFFE4, 23), (0x1FFFDC, 21), (0x3FFFD8, 22), (0x7FFFE5, 23),
    (0x3FFFD9, 22), (0x7FFFE6, 23), (0x7FFFE7, 23), (0xFFFFEF, 24),
    (0x3FFFDA, 22), (0x1FFFDD, 21), (0xFFFE9, 20), (0x3FFFDB, 22),
    (0x3FFFDC, 22), (0x7FFFE8, 23), (0x7FFFE9, 23), (0x1FFFDE, 21),
    (0x7FFFEA, 23), (0x3FFFDD, 22), (0x3FFFDE, 22), (0xFFFFF0, 24),
    (0x1FFFDF, 21), (0x3FFFDF, 22), (0x7FFFEB, 23), (0x7FFFEC, 23),
    (0x1FFFE0, 21), (0x1FFFE1, 21), (0x3FFFE0, 22), (0x1FFFE2, 21),
    (0x7FFFED, 23), (0x3FFFE1, 22), (0x7FFFEE, 23), (0x7FFFEF, 23),
    (0xFFFEA, 20), (0x3FFFE2, 22), (0x3FFFE3, 22), (0x3FFFE4, 22),
    (0x7FFFF0, 23), (0x3FFFE5, 22), (0x3FFFE6, 22), (0x7FFFF1, 23),
    (0x3FFFFE0, 26), (0x3FFFFE1, 26), (0xFFFEB, 20), (0x7FFF1, 19),
    (0x3FFFE7, 22), (0x7FFFF2, 23), (0x3FFFE8, 22), (0x1FFFFEC, 25),
    (0x3FFFFE2, 26), (0x3FFFFE3, 26), (0x3FFFFE4, 26), (0x7FFFFDE, 27),
    (0x7FFFFDF, 27), (0x3FFFFE5, 26), (0xFFFFF1, 24), (0x1FFFFED, 25),
    (0x7FFF2, 19), (0x1FFFE3, 21), (0x3FFFFE6, 26), (0x7FFFFE0, 27),
    (0x7FFFFE1, 27), (0x3FFFFE7, 26), (0x7FFFFE2, 27), (0xFFFFF2, 24),
    (0x1FFFE4, 21), (0x1FFFE5, 21), (0x3FFFFE8, 26), (0x3FFFFE9, 26),
    (0xFFFFFFD, 28), (0x7FFFFE3, 27), (0x7FFFFE4, 27), (0x7FFFFE5, 27),
    (0xFFFEC, 20), (0xFFFFF3, 24), (0xFFFED, 20), (0x1FFFE6, 21),
    (0x3FFFE9, 22), (0x1FFFE7, 21), (0x1FFFE8, 21), (0x7FFFF3, 23),
    (0x3FFFEA, 22), (0x3FFFEB, 22), (0x1FFFFEE, 25), (0x1FFFFEF, 25),
    (0xFFFFF4, 24), (0xFFFFF5, 24), (0x3FFFFEA, 26), (0x7FFFF4, 23),
    (0x3FFFFEB, 26), (0x7FFFFE6, 27), (0x3FFFFEC, 26), (0x3FFFFED, 26),
    (0x7FFFFE7, 27), (0x7FFFFE8, 27), (0x7FFFFE9, 27), (0x7FFFFEA, 27),
    (0x7FFFFEB, 27), (0xFFFFFFE, 28), (0x7FFFFEC, 27), (0x7FFFFED, 27),
    (0x7FFFFEE, 27), (0x7FFFFEF, 27), (0x7FFFFF0, 27), (0x3FFFFEE, 26),
    (0x3FFFFFFF, 30),
)

EOS_SYMBOL = 256
DEFAULT_DYNAMIC_TABLE_SIZE = 4096
#: Every dynamic entry costs its octets plus 32 (RFC 7541 section 4.1).
ENTRY_OVERHEAD = 32


class HPACKError(Exception):
    """A header block that cannot be decoded. Always a connection error."""


def _build_huffman_tree():
    """Bit-trie for decoding: nested lists indexed by bit."""
    root = [None, None]
    for symbol, (code, length) in enumerate(HUFFMAN_CODES):
        node = root
        for shift in range(length - 1, -1, -1):
            bit = (code >> shift) & 1
            if shift == 0:
                node[bit] = symbol
            else:
                if node[bit] is None:
                    node[bit] = [None, None]
                node = node[bit]
    return root


_HUFFMAN_TREE = _build_huffman_tree()


def huffman_encode(data):
    """Huffman-code ``data``, padding the tail with EOS bits."""
    bits = 0
    length = 0
    out = bytearray()
    for byte in bytes(data):
        code, code_length = HUFFMAN_CODES[byte]
        bits = (bits << code_length) | code
        length += code_length
        while length >= 8:
            length -= 8
            out.append((bits >> length) & 0xFF)
    if length:
        # Pad with the most significant bits of EOS, per section 5.2.
        bits = (bits << (8 - length)) | ((1 << (8 - length)) - 1)
        out.append(bits & 0xFF)
    return bytes(out)


def huffman_decode(data):
    """Decode Huffman-coded ``data``, rejecting the paddings RFC 7541 forbids."""
    node = _HUFFMAN_TREE
    out = bytearray()
    padding = 0
    for byte in bytes(data):
        for shift in range(7, -1, -1):
            bit = (byte >> shift) & 1
            node = node[bit]
            if node is None:
                raise HPACKError("invalid Huffman code")
            if isinstance(node, int):
                if node == EOS_SYMBOL:
                    raise HPACKError("EOS must not appear in a Huffman string")
                out.append(node)
                node = _HUFFMAN_TREE
                padding = 0
            else:
                padding += 1
    if padding > 7:
        raise HPACKError("Huffman padding longer than 7 bits")
    if node is not _HUFFMAN_TREE:
        # Any incomplete tail must be the all-ones EOS prefix.
        probe = node
        while not isinstance(probe, int):
            if probe[1] is None:
                raise HPACKError("Huffman padding is not the EOS prefix")
            probe = probe[1]
        if probe != EOS_SYMBOL:
            raise HPACKError("Huffman padding is not the EOS prefix")
    return bytes(out)


def encode_integer(value, prefix_bits):
    """Encode an integer with an N-bit prefix (RFC 7541 section 5.1)."""
    value = int(value)
    if value < 0:
        raise ValueError("HPACK integers are not negative")
    limit = (1 << prefix_bits) - 1
    if value < limit:
        return bytes([value])
    out = bytearray([limit])
    value -= limit
    while value >= 128:
        out.append((value % 128) + 128)
        value //= 128
    out.append(value)
    return bytes(out)


def decode_integer(data, offset, prefix_bits):
    """Decode a prefixed integer, returning ``(value, next_offset)``."""
    if offset >= len(data):
        raise HPACKError("truncated integer")
    limit = (1 << prefix_bits) - 1
    value = data[offset] & limit
    offset += 1
    if value < limit:
        return value, offset
    shift = 0
    while True:
        if offset >= len(data):
            raise HPACKError("truncated integer continuation")
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
        if shift > 28:
            # A longer continuation can only be an attempt to overflow.
            raise HPACKError("integer continuation too long")
    return value, offset


def encode_string(value, huffman=True):
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    if huffman:
        coded = huffman_encode(raw)
        if len(coded) < len(raw):
            head = bytearray(encode_integer(len(coded), 7))
            head[0] |= 0x80
            return bytes(head) + coded
    return encode_integer(len(raw), 7) + raw


def decode_string(data, offset):
    if offset >= len(data):
        raise HPACKError("truncated string")
    huffman = bool(data[offset] & 0x80)
    length, offset = decode_integer(data, offset, 7)
    end = offset + length
    if end > len(data):
        raise HPACKError("string longer than the header block")
    raw = bytes(data[offset:end])
    if huffman:
        raw = huffman_decode(raw)
    return raw.decode("utf-8", errors="strict"), end


class DynamicTable:
    """The half of the header table that both peers mutate in step."""

    def __init__(self, max_size=DEFAULT_DYNAMIC_TABLE_SIZE):
        self._entries = []
        self._size = 0
        self._max_size = int(max_size)

    def __len__(self):
        return len(self._entries)

    @property
    def size(self):
        return self._size

    @property
    def max_size(self):
        return self._max_size

    def set_max_size(self, value):
        self._max_size = int(value)
        self._evict()

    @staticmethod
    def entry_size(name, value):
        return len(name.encode("utf-8")) + len(value.encode("utf-8")) + ENTRY_OVERHEAD

    def _evict(self):
        while self._size > self._max_size and self._entries:
            name, value = self._entries.pop()
            self._size -= self.entry_size(name, value)

    def add(self, name, value):
        cost = self.entry_size(name, value)
        if cost > self._max_size:
            # Section 4.4: an oversized insert empties the table instead.
            self._entries.clear()
            self._size = 0
            return False
        self._entries.insert(0, (name, value))
        self._size += cost
        self._evict()
        return True

    def get(self, index):
        """One-based index into the dynamic half."""
        if not 1 <= index <= len(self._entries):
            raise HPACKError(f"dynamic table index {index} out of range")
        return self._entries[index - 1]

    def find(self, name, value=None):
        """Return ``(index, matched_value)``; index is dynamic-half relative."""
        name_only = None
        for position, (entry_name, entry_value) in enumerate(self._entries, start=1):
            if entry_name != name:
                continue
            if value is not None and entry_value == value:
                return position, True
            if name_only is None:
                name_only = position
        return name_only, False

    def entries(self):
        return list(self._entries)


_STATIC_EXACT = {}
_STATIC_BY_NAME = {}
for _index in range(1, len(STATIC_TABLE)):
    _name, _value = STATIC_TABLE[_index]
    _STATIC_EXACT.setdefault((_name, _value), _index)
    _STATIC_BY_NAME.setdefault(_name, _index)


class Decoder:
    """Decodes header blocks, keeping the dynamic table in step with the peer."""

    def __init__(self, max_size=DEFAULT_DYNAMIC_TABLE_SIZE):
        self.table = DynamicTable(max_size)
        #: Ceiling the peer may raise the table to, from SETTINGS.
        self.max_allowed_size = int(max_size)

    def _lookup(self, index):
        if index == 0:
            raise HPACKError("index 0 is not a header field")
        if index <= STATIC_TABLE_SIZE:
            return STATIC_TABLE[index]
        return self.table.get(index - STATIC_TABLE_SIZE)

    def decode(self, block):
        data = bytes(block)
        offset = 0
        headers = []
        saw_field = False
        while offset < len(data):
            byte = data[offset]
            if byte & 0x80:
                index, offset = decode_integer(data, offset, 7)
                headers.append(self._lookup(index))
                saw_field = True
            elif byte & 0x40:
                name, value, offset = self._decode_literal(data, offset, 6)
                self.table.add(name, value)
                headers.append((name, value))
                saw_field = True
            elif byte & 0x20:
                size, offset = decode_integer(data, offset, 5)
                if size > self.max_allowed_size:
                    raise HPACKError(
                        f"table size update {size} exceeds the agreed {self.max_allowed_size}"
                    )
                if saw_field:
                    # Section 4.2: updates only occur at the start of a block.
                    raise HPACKError("table size update must precede header fields")
                self.table.set_max_size(size)
            else:
                # 0000 never-indexed and 0001 without-indexing share a shape.
                name, value, offset = self._decode_literal(data, offset, 4)
                headers.append((name, value))
                saw_field = True
        return headers

    def _decode_literal(self, data, offset, prefix_bits):
        index, offset = decode_integer(data, offset, prefix_bits)
        if index:
            name = self._lookup(index)[0]
        else:
            name, offset = decode_string(data, offset)
        value, offset = decode_string(data, offset)
        return name, value, offset


class Encoder:
    """Encodes header blocks, indexing what is worth indexing."""

    def __init__(self, max_size=DEFAULT_DYNAMIC_TABLE_SIZE):
        self.table = DynamicTable(max_size)
        self._pending_size = None

    def set_max_size(self, value):
        """Queue a table size update to emit with the next block."""
        self._pending_size = int(value)

    def encode(self, headers, huffman=True):
        out = bytearray()
        if self._pending_size is not None:
            self.table.set_max_size(self._pending_size)
            head = bytearray(encode_integer(self._pending_size, 5))
            head[0] |= 0x20
            out += bytes(head)
            self._pending_size = None
        for name, value in headers:
            name = name.lower() if isinstance(name, str) else name.decode().lower()
            value = value if isinstance(value, str) else value.decode()
            out += self._encode_field(name, value, huffman)
        return bytes(out)

    def _encode_field(self, name, value, huffman):
        static_exact = _STATIC_EXACT.get((name, value))
        if static_exact is not None:
            return _indexed(static_exact)

        dynamic_index, exact = self.table.find(name, value)
        if exact:
            return _indexed(dynamic_index + STATIC_TABLE_SIZE)

        name_index = _STATIC_BY_NAME.get(name)
        if name_index is None and dynamic_index is not None:
            name_index = dynamic_index + STATIC_TABLE_SIZE

        if _never_index(name):
            # Not added to the table: a proxy must not index these.
            return _literal(name_index, name, value, huffman, pattern=0x10, prefix_bits=4)

        self.table.add(name, value)
        return _literal(name_index, name, value, huffman, pattern=0x40, prefix_bits=6)


#: Headers a proxy must not put in a shared table (RFC 7541 section 7.1).
NEVER_INDEXED = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})


def _never_index(name):
    return name in NEVER_INDEXED


def _indexed(index):
    encoded = bytearray(encode_integer(index, 7))
    encoded[0] |= 0x80
    return bytes(encoded)


def _literal(name_index, name, value, huffman, *, pattern, prefix_bits):
    if name_index is None:
        head = bytearray(encode_integer(0, prefix_bits))
        head[0] |= pattern
        return bytes(head) + encode_string(name, huffman) + encode_string(value, huffman)
    head = bytearray(encode_integer(name_index, prefix_bits))
    head[0] |= pattern
    return bytes(head) + encode_string(value, huffman)


__all__ = [
    "DEFAULT_DYNAMIC_TABLE_SIZE",
    "Decoder",
    "DynamicTable",
    "ENTRY_OVERHEAD",
    "Encoder",
    "HPACKError",
    "HUFFMAN_CODES",
    "NEVER_INDEXED",
    "STATIC_TABLE",
    "STATIC_TABLE_SIZE",
    "decode_integer",
    "decode_string",
    "encode_integer",
    "encode_string",
    "huffman_decode",
    "huffman_encode",
]

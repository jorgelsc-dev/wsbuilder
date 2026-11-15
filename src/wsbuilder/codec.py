"""Compact binary encoding for service-to-service payloads (CBOR, RFC 8949).

JSON spends bytes on quotes, escapes, digits written as text and base64 for
binary data. CBOR describes the same data model (maps, arrays, strings,
numbers, booleans, null) with a one-octet type header and binary lengths, so
the same message is smaller on the wire and ``bytes`` travel as they are.

The encoder writes the preferred (shortest) form of every item and only
definite lengths. The decoder accepts any well-formed item, including
indefinite lengths, and treats its input as untrusted: lengths are checked
against what is actually left before anything is allocated, nesting depth is
bounded, and trailing bytes are refused.

Mapping between Python and CBOR:

========================  ==========================================
Python                    CBOR
========================  ==========================================
``None``                  simple value 22 (null)
``bool``                  simple values 20/21 (false/true)
``int``                   major types 0/1, or tags 2/3 past 64 bits
``float``                 float16, float32 or float64, the shortest
                          one that keeps the value exactly
``str``                   major type 3 (UTF-8 text)
``bytes``/``bytearray``   major type 2 (byte string)
``list``/``tuple``        major type 4 (array); decodes as ``list``
``dict``                  major type 5 (map)
``CBORTag``               major type 6 (tag); tags 2/3 decode as int
``CBORSimple``            other simple values
========================  ==========================================

``undefined`` (simple value 23) decodes as ``None``.
"""

import math
import struct

__all__ = [
    "CBORDecodeError",
    "CBOREncodeError",
    "CBORSimple",
    "CBORTag",
    "DEFAULT_MAX_DEPTH",
    "cbor_dumps",
    "cbor_loads",
]

#: Nesting deeper than this is refused when decoding (and encoding), which
#: keeps hostile input from exhausting the interpreter's recursion limit.
DEFAULT_MAX_DEPTH = 256

_MAJOR_UNSIGNED = 0
_MAJOR_NEGATIVE = 1
_MAJOR_BYTES = 2
_MAJOR_TEXT = 3
_MAJOR_ARRAY = 4
_MAJOR_MAP = 5
_MAJOR_TAG = 6
_MAJOR_SIMPLE = 7

_TAG_POSITIVE_BIGNUM = 2
_TAG_NEGATIVE_BIGNUM = 3

_BREAK = 0xFF
_UINT64_MAX = (1 << 64) - 1

_PACK_HALF = struct.Struct(">e")
_PACK_SINGLE = struct.Struct(">f")
_PACK_DOUBLE = struct.Struct(">d")
_PACK_U16 = struct.Struct(">H")
_PACK_U32 = struct.Struct(">I")
_PACK_U64 = struct.Struct(">Q")

_FALSE = b"\xf4"
_TRUE = b"\xf5"
_NULL = b"\xf6"
_SIMPLE_LEAVES = {0xF4: False, 0xF5: True, 0xF6: None}


class CBOREncodeError(ValueError):
    """A value that has no CBOR representation."""


class CBORDecodeError(ValueError):
    """Input that is not one well-formed CBOR item."""


class CBORTag:
    """A tagged item whose tag number has no built-in meaning here."""

    __slots__ = ("tag", "value")

    def __init__(self, tag, value):
        tag = int(tag)
        if not 0 <= tag <= _UINT64_MAX:
            raise ValueError("a CBOR tag is an unsigned 64-bit number")
        self.tag = tag
        self.value = value

    def __eq__(self, other):
        return isinstance(other, CBORTag) and (self.tag, self.value) == (other.tag, other.value)

    def __hash__(self):
        return hash((self.tag, _freeze(self.value)))

    def __repr__(self):
        return f"CBORTag({self.tag}, {self.value!r})"


class CBORSimple:
    """A simple value other than false, true, null and undefined."""

    __slots__ = ("value",)

    def __init__(self, value):
        value = int(value)
        if not (0 <= value <= 19 or 32 <= value <= 255):
            raise ValueError("CBOR simple values are 0..19 or 32..255")
        self.value = value

    def __eq__(self, other):
        return isinstance(other, CBORSimple) and self.value == other.value

    def __hash__(self):
        return hash(("CBORSimple", self.value))

    def __repr__(self):
        return f"CBORSimple({self.value})"


def _freeze(value):
    """A hashable stand-in, so tags holding lists can still be map keys."""
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple((_freeze(k), _freeze(v)) for k, v in value.items())
    return value


# -- encoding ---------------------------------------------------------------


def _build_small_heads():
    table = []
    for major in range(8):
        row = []
        for argument in range(256):
            if argument < 24:
                row.append(bytes(((major << 5) | argument,)))
            else:
                row.append(bytes(((major << 5) | 24, argument)))
        table.append(tuple(row))
    return tuple(table)


#: Pre-built heads for arguments below 256, which covers most strings,
#: containers and small integers without packing anything at run time.
_SMALL_HEADS = _build_small_heads()


def _head(major, argument):
    """The initial byte(s) of an item: major type plus its argument."""
    if argument < 0x100:
        return _SMALL_HEADS[major][argument]
    if argument < 0x10000:
        return bytes(((major << 5) | 25,)) + _PACK_U16.pack(argument)
    if argument < 0x100000000:
        return bytes(((major << 5) | 26,)) + _PACK_U32.pack(argument)
    return bytes(((major << 5) | 27,)) + _PACK_U64.pack(argument)


def _encode_float(value):
    if math.isnan(value):
        return b"\xf9\x7e\x00"  # the canonical quiet NaN, as float16
    if math.isinf(value):
        return b"\xf9\x7c\x00" if value > 0 else b"\xf9\xfc\x00"
    try:
        half = _PACK_HALF.pack(value)
        if _PACK_HALF.unpack(half)[0] == value:
            return b"\xf9" + half
    except OverflowError:
        pass
    try:
        single = _PACK_SINGLE.pack(value)
        if _PACK_SINGLE.unpack(single)[0] == value:
            return b"\xfa" + single
    except OverflowError:
        pass
    return b"\xfb" + _PACK_DOUBLE.pack(value)


def _encode_int(value, out):
    if 0 <= value <= _UINT64_MAX:
        out.append(_head(_MAJOR_UNSIGNED, value))
    elif -(1 << 64) <= value < 0:
        out.append(_head(_MAJOR_NEGATIVE, -1 - value))
    else:
        # RFC 8949 section 3.4.3: bignums are a tag over the magnitude bytes.
        tag, magnitude = (
            (_TAG_POSITIVE_BIGNUM, value) if value > 0 else (_TAG_NEGATIVE_BIGNUM, -1 - value)
        )
        raw = magnitude.to_bytes((magnitude.bit_length() + 7) // 8, "big")
        out.append(_head(_MAJOR_TAG, tag))
        out.append(_head(_MAJOR_BYTES, len(raw)))
        out.append(raw)


def cbor_dumps(value, *, max_depth=DEFAULT_MAX_DEPTH):
    """Encode ``value`` as one CBOR item and return the bytes."""
    max_depth = int(max_depth)
    out = []
    append = out.append
    heads = _SMALL_HEADS
    text_heads = heads[_MAJOR_TEXT]
    uint_heads = heads[_MAJOR_UNSIGNED]
    nint_heads = heads[_MAJOR_NEGATIVE]
    # Map keys repeat across the records of a payload; encode each once.
    text_cache = {}

    def encode(item, depth):
        kind = type(item)
        if kind is str:
            cached = text_cache.get(item)
            if cached is None:
                raw = item.encode("utf-8")
                size = len(raw)
                cached = (text_heads[size] if size < 256 else _head(_MAJOR_TEXT, size)) + raw
                if size <= 64:
                    text_cache[item] = cached
            append(cached)
        elif kind is int:
            if 0 <= item < 256:
                append(uint_heads[item])
            elif -256 <= item < 0:
                append(nint_heads[-1 - item])
            else:
                _encode_int(item, out)
        elif item is None:
            append(_NULL)
        elif item is True:
            append(_TRUE)
        elif item is False:
            append(_FALSE)
        elif kind is float:
            append(_encode_float(item))
        elif kind is dict:
            if depth >= max_depth:
                raise CBOREncodeError("value nests deeper than max_depth")
            size = len(item)
            append(heads[_MAJOR_MAP][size] if size < 256 else _head(_MAJOR_MAP, size))
            depth += 1
            get_text = text_cache.get
            for key, child in item.items():
                # Leaves are written inline: a call per item dominates the
                # cost of encoding a typical record.
                cached = get_text(key) if type(key) is str else None
                if cached is not None:
                    append(cached)
                else:
                    encode(key, depth)
                child_kind = type(child)
                if child_kind is str:
                    cached = get_text(child)
                    if cached is not None:
                        append(cached)
                        continue
                elif child_kind is int:
                    if 0 <= child < 256:
                        append(uint_heads[child])
                        continue
                elif child is None:
                    append(_NULL)
                    continue
                elif child is True:
                    append(_TRUE)
                    continue
                elif child is False:
                    append(_FALSE)
                    continue
                encode(child, depth)
        elif kind is list or kind is tuple:
            if depth >= max_depth:
                raise CBOREncodeError("value nests deeper than max_depth")
            size = len(item)
            append(heads[_MAJOR_ARRAY][size] if size < 256 else _head(_MAJOR_ARRAY, size))
            depth += 1
            get_text = text_cache.get
            for child in item:
                child_kind = type(child)
                if child_kind is str:
                    cached = get_text(child)
                    if cached is not None:
                        append(cached)
                        continue
                elif child_kind is int and 0 <= child < 256:
                    append(uint_heads[child])
                    continue
                encode(child, depth)
        elif kind is bytes or isinstance(item, (bytes, bytearray, memoryview)):
            raw = bytes(item)
            append(_head(_MAJOR_BYTES, len(raw)))
            append(raw)
        else:
            encode_other(item, kind, depth)

    def encode_other(item, kind, depth):
        # Subclasses and the CBOR-specific wrappers: rare, so kept off the
        # hot path above.
        if isinstance(item, bool):
            append(_TRUE if item else _FALSE)
        elif isinstance(item, int):
            _encode_int(int(item), out)
        elif isinstance(item, float):
            append(_encode_float(float(item)))
        elif isinstance(item, str):
            encode(str(item), depth)
        elif isinstance(item, dict):
            encode(dict(item), depth)
        elif isinstance(item, (list, tuple)):
            encode(list(item), depth)
        elif isinstance(item, CBORTag):
            if depth >= max_depth:
                raise CBOREncodeError("value nests deeper than max_depth")
            append(_head(_MAJOR_TAG, item.tag))
            encode(item.value, depth + 1)
        elif isinstance(item, CBORSimple):
            if item.value < 24:
                append(bytes(((_MAJOR_SIMPLE << 5) | item.value,)))
            else:
                append(bytes(((_MAJOR_SIMPLE << 5) | 24, item.value)))
        else:
            raise CBOREncodeError(f"cannot encode {kind.__name__} as CBOR")

    try:
        encode(value, 0)
    except RecursionError as exc:
        raise CBOREncodeError("value nests too deeply to encode") from exc
    return b"".join(out)


# -- decoding ---------------------------------------------------------------


def cbor_loads(data, *, max_depth=DEFAULT_MAX_DEPTH):
    """Decode exactly one CBOR item from ``data``.

    Array keys decode as tuples so they stay hashable; map keys are refused.
    """
    raw = bytes(data)
    size = len(raw)
    max_depth = int(max_depth)
    pos = 0
    unpack_u16 = _PACK_U16.unpack_from
    unpack_u32 = _PACK_U32.unpack_from
    unpack_u64 = _PACK_U64.unpack_from
    # Map keys repeat across the records of a payload; decode each once.
    key_cache = {}

    def argument(info):
        nonlocal pos
        if info < 24:
            return info
        if info == 24:
            if pos + 1 > size:
                raise CBORDecodeError("input ends inside an item")
            pos += 1
            return raw[pos - 1]
        if info == 25:
            if pos + 2 > size:
                raise CBORDecodeError("input ends inside an item")
            pos += 2
            return unpack_u16(raw, pos - 2)[0]
        if info == 26:
            if pos + 4 > size:
                raise CBORDecodeError("input ends inside an item")
            pos += 4
            return unpack_u32(raw, pos - 4)[0]
        if info == 27:
            if pos + 8 > size:
                raise CBORDecodeError("input ends inside an item")
            pos += 8
            return unpack_u64(raw, pos - 8)[0]
        raise CBORDecodeError(f"reserved additional information {info}")

    def take(count):
        nonlocal pos
        end = pos + count
        if end > size:
            raise CBORDecodeError("input ends inside an item")
        start = pos
        pos = end
        return raw[start:end]

    def string_chunks(major):
        nonlocal pos
        pieces = []
        while True:
            if pos >= size:
                raise CBORDecodeError("input ends inside an indefinite string")
            initial = raw[pos]
            pos += 1
            if initial == _BREAK:
                return b"".join(pieces)
            if initial >> 5 != major or (initial & 0x1F) == 31:
                raise CBORDecodeError("indefinite string chunk of the wrong type")
            pieces.append(take(argument(initial & 0x1F)))

    def at_break():
        nonlocal pos
        if pos >= size:
            raise CBORDecodeError("input ends before a break")
        if raw[pos] == _BREAK:
            pos += 1
            return True
        return False

    def key(depth):
        found = item(depth)
        if isinstance(found, list):
            return tuple(_freeze(entry) for entry in found)
        if isinstance(found, dict):
            raise CBORDecodeError("a map cannot be a map key")
        return found

    def item(depth):
        nonlocal pos
        if pos >= size:
            raise CBORDecodeError("input ends before an item")
        initial = raw[pos]
        pos += 1
        major = initial >> 5
        info = initial & 0x1F

        if major == _MAJOR_TEXT:
            if info < 24:
                end = pos + info
                if end > size:
                    raise CBORDecodeError("input ends inside an item")
                chunk = raw[pos:end]
                pos = end
            elif info == 31:
                chunk = string_chunks(major)
            else:
                chunk = take(argument(info))
            try:
                return chunk.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CBORDecodeError("text string is not valid UTF-8") from exc
        if major == _MAJOR_UNSIGNED:
            return info if info < 24 else argument(info)
        if major == _MAJOR_MAP:
            if depth >= max_depth:
                raise CBORDecodeError("input nests deeper than max_depth")
            depth += 1
            result = {}
            if info == 31:
                while not at_break():
                    name = key(depth)
                    result[name] = item(depth)
                return result
            count = info if info < 24 else argument(info)
            # Every item takes at least one octet, so a declared count larger
            # than what is left cannot be honest: refuse before allocating.
            if count * 2 > size - pos:
                raise CBORDecodeError("declared length exceeds the input")
            for _ in range(count):
                # Short text keys and small leaves are read inline; a call
                # per item dominates the cost of decoding a typical record.
                initial = raw[pos] if pos < size else -1
                if 0x60 <= initial < 0x78:
                    end = pos + 1 + (initial - 0x60)
                    if end > size:
                        raise CBORDecodeError("input ends inside an item")
                    chunk = raw[pos + 1:end]
                    name = key_cache.get(chunk)
                    if name is None:
                        try:
                            name = chunk.decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise CBORDecodeError("text string is not valid UTF-8") from exc
                        key_cache[chunk] = name
                    pos = end
                else:
                    name = key(depth)
                initial = raw[pos] if pos < size else -1
                if 0 <= initial < 0x18:
                    pos += 1
                    result[name] = initial
                elif 0xF4 <= initial <= 0xF6:
                    pos += 1
                    result[name] = _SIMPLE_LEAVES[initial]
                else:
                    result[name] = item(depth)
            return result
        if major == _MAJOR_ARRAY:
            if depth >= max_depth:
                raise CBORDecodeError("input nests deeper than max_depth")
            depth += 1
            if info == 31:
                values = []
                while not at_break():
                    values.append(item(depth))
                return values
            count = info if info < 24 else argument(info)
            if count > size - pos:
                raise CBORDecodeError("declared length exceeds the input")
            return [item(depth) for _ in range(count)]
        if major == _MAJOR_NEGATIVE:
            return -1 - (info if info < 24 else argument(info))
        if major == _MAJOR_BYTES:
            if info == 31:
                return string_chunks(major)
            return take(argument(info))
        if major == _MAJOR_TAG:
            if depth >= max_depth:
                raise CBORDecodeError("input nests deeper than max_depth")
            tag = argument(info)
            value = item(depth + 1)
            if tag == _TAG_POSITIVE_BIGNUM or tag == _TAG_NEGATIVE_BIGNUM:
                if not isinstance(value, bytes):
                    raise CBORDecodeError("a bignum tag must wrap a byte string")
                magnitude = int.from_bytes(value, "big")
                return magnitude if tag == _TAG_POSITIVE_BIGNUM else -1 - magnitude
            return CBORTag(tag, value)
        # Major type 7: floats and simple values.
        if info == 20:
            return False
        if info == 21:
            return True
        if info == 22 or info == 23:
            return None
        if info == 27:
            return _PACK_DOUBLE.unpack(take(8))[0]
        if info == 26:
            return _PACK_SINGLE.unpack(take(4))[0]
        if info == 25:
            return _PACK_HALF.unpack(take(2))[0]
        if info < 20:
            return CBORSimple(info)
        if info == 24:
            value = take(1)[0]
            if value < 32:
                raise CBORDecodeError("two-byte simple value below 32")
            return CBORSimple(value)
        if info == 31:
            raise CBORDecodeError("unexpected break")
        raise CBORDecodeError(f"reserved additional information {info}")

    try:
        value = item(0)
    except RecursionError as exc:
        raise CBORDecodeError("input nests too deeply to decode") from exc
    if pos != size:
        raise CBORDecodeError("trailing bytes after the item")
    return value

"""Variable-length integers (RFC 9000 section 16).

The two most significant bits of the first octet give the length of the
whole field, so a value carries its own size: 1, 2, 4 or 8 octets for
6, 14, 30 or 62 bits of payload.

Encodings are not canonical by requirement -- 0 may legally be written in
any of the four widths -- so a decoder must accept a longer form than
necessary, and an encoder that needs byte-for-byte reproduction has to be
told the width rather than choosing it.
"""

#: Largest value the 62-bit payload can hold.
MAX_VARINT = (1 << 62) - 1

_LENGTHS = (1, 2, 4, 8)
_THRESHOLDS = ((1 << 6) - 1, (1 << 14) - 1, (1 << 30) - 1, MAX_VARINT)


def varint_length(value):
    """Octets the shortest encoding of ``value`` needs."""
    value = int(value)
    if value < 0:
        raise ValueError("QUIC varints are unsigned")
    for length, threshold in zip(_LENGTHS, _THRESHOLDS):
        if value <= threshold:
            return length
    raise ValueError(f"{value} exceeds the 62-bit varint range")


def encode_varint(value, length=None):
    """Encode ``value``; ``length`` forces a wider, still legal, form."""
    value = int(value)
    minimum = varint_length(value)
    if length is None:
        length = minimum
    else:
        length = int(length)
        if length not in _LENGTHS:
            raise ValueError("a varint is 1, 2, 4 or 8 octets")
        if length < minimum:
            raise ValueError(f"{value} does not fit in {length} octets")
    prefix = _LENGTHS.index(length)
    return (value | (prefix << (length * 8 - 2))).to_bytes(length, "big")


def decode_varint(data, offset=0):
    """Decode a varint, returning ``(value, next_offset)``."""
    if offset >= len(data):
        raise ValueError("truncated varint")
    first = data[offset]
    length = _LENGTHS[first >> 6]
    end = offset + length
    if end > len(data):
        raise ValueError("varint runs past the end of the buffer")
    value = int.from_bytes(bytes(data[offset:end]), "big") & ((1 << (length * 8 - 2)) - 1)
    return value, end


def decode_varint_prefixed_bytes(data, offset=0):
    """Decode a length-prefixed blob, returning ``(payload, next_offset)``."""
    length, offset = decode_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("length-prefixed field runs past the end of the buffer")
    return bytes(data[offset:end]), end


def encode_varint_prefixed_bytes(payload):
    payload = bytes(payload)
    return encode_varint(len(payload)) + payload


__all__ = [
    "MAX_VARINT",
    "decode_varint",
    "decode_varint_prefixed_bytes",
    "encode_varint",
    "encode_varint_prefixed_bytes",
    "varint_length",
]

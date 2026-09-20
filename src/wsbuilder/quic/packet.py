"""QUIC packet formats (RFC 9000 section 17).

Two shapes exist. Long headers carry the version and both connection ids and
are used until the handshake finishes; short headers carry only the
destination id, because by then the peer knows the rest. Neither is
self-delimiting in the way a TCP segment is: several packets may share one
UDP datagram, so parsing returns how much was consumed.
"""

import os

from .varint import decode_varint, encode_varint

PACKET_INITIAL = 0x00
PACKET_ZERO_RTT = 0x01
PACKET_HANDSHAKE = 0x02
PACKET_RETRY = 0x03

LONG_PACKET_NAMES = {
    PACKET_INITIAL: "Initial",
    PACKET_ZERO_RTT: "0-RTT",
    PACKET_HANDSHAKE: "Handshake",
    PACKET_RETRY: "Retry",
}

HEADER_FORM_LONG = 0x80
FIXED_BIT = 0x40
MAX_CONNECTION_ID_LENGTH = 20
#: A client's first flight must pad the datagram to at least this, so a
#: server's reply cannot be used to amplify traffic at a spoofed victim.
MIN_INITIAL_DATAGRAM = 1200


class QuicPacketError(ValueError):
    """A datagram that cannot be parsed as a QUIC packet."""


def new_connection_id(length=8):
    if not 0 <= length <= MAX_CONNECTION_ID_LENGTH:
        raise ValueError("connection ids are at most 20 octets")
    return os.urandom(length)


def is_long_header(first_byte):
    return bool(first_byte & HEADER_FORM_LONG)


class LongHeader:
    """The parsed fields of a long-header packet, still protected."""

    __slots__ = (
        "packet_type",
        "version",
        "destination_cid",
        "source_cid",
        "token",
        "length",
        "payload_offset",
        "packet_length",
        "first_byte",
    )

    def __init__(self, **fields):
        for name in self.__slots__:
            setattr(self, name, fields.get(name))

    @property
    def name(self):
        return LONG_PACKET_NAMES.get(self.packet_type, "Unknown")

    def describe(self):
        return {
            "type": self.name,
            "version": f"0x{self.version:08x}",
            "destination_cid": self.destination_cid.hex(),
            "source_cid": self.source_cid.hex(),
            "token_length": len(self.token or b""),
            "length": self.length,
        }


def parse_long_header(datagram, offset=0):
    """Parse one long-header packet, leaving its payload protected."""
    data = bytes(datagram)
    start = offset
    if offset >= len(data):
        raise QuicPacketError("empty packet")
    first = data[offset]
    if not is_long_header(first):
        raise QuicPacketError("not a long header")
    if not first & FIXED_BIT:
        # Section 17.2: the fixed bit is 1 except for the version negotiation
        # packet, which a server never receives.
        raise QuicPacketError("fixed bit is not set")
    offset += 1
    if offset + 4 > len(data):
        raise QuicPacketError("truncated version")
    version = int.from_bytes(data[offset : offset + 4], "big")
    offset += 4

    destination_cid, offset = _read_connection_id(data, offset)
    source_cid, offset = _read_connection_id(data, offset)

    packet_type = (first & 0x30) >> 4
    token = b""
    if packet_type == PACKET_INITIAL:
        token_length, offset = decode_varint(data, offset)
        end = offset + token_length
        if end > len(data):
            raise QuicPacketError("token runs past the datagram")
        token = data[offset:end]
        offset = end
    elif packet_type == PACKET_RETRY:
        # A Retry has no length field: the rest of the datagram is its body.
        return LongHeader(
            packet_type=packet_type,
            version=version,
            destination_cid=destination_cid,
            source_cid=source_cid,
            token=data[offset:],
            length=len(data) - offset,
            payload_offset=offset,
            packet_length=len(data) - start,
            first_byte=first,
        )

    length, offset = decode_varint(data, offset)
    if offset + length > len(data):
        raise QuicPacketError("packet length runs past the datagram")
    return LongHeader(
        packet_type=packet_type,
        version=version,
        destination_cid=destination_cid,
        source_cid=source_cid,
        token=token,
        length=length,
        payload_offset=offset,
        packet_length=(offset - start) + length,
        first_byte=first,
    )


def _read_connection_id(data, offset):
    if offset >= len(data):
        raise QuicPacketError("truncated connection id length")
    length = data[offset]
    offset += 1
    if length > MAX_CONNECTION_ID_LENGTH:
        raise QuicPacketError("connection id longer than 20 octets")
    end = offset + length
    if end > len(data):
        raise QuicPacketError("connection id runs past the datagram")
    return data[offset:end], end


def build_long_header(
    packet_type,
    version,
    destination_cid,
    source_cid,
    *,
    packet_number,
    packet_number_length=4,
    payload_length=0,
    token=b"",
):
    """Assemble an unprotected long header ending with the packet number."""
    if not 1 <= packet_number_length <= 4:
        raise ValueError("a packet number is 1 to 4 octets")
    first = (
        HEADER_FORM_LONG
        | FIXED_BIT
        | ((int(packet_type) & 0x03) << 4)
        | (packet_number_length - 1)
    )
    header = bytearray([first])
    header += int(version).to_bytes(4, "big")
    header += bytes([len(destination_cid)]) + bytes(destination_cid)
    header += bytes([len(source_cid)]) + bytes(source_cid)
    if packet_type == PACKET_INITIAL:
        header += encode_varint(len(token)) + bytes(token)
    # The length covers the packet number and the protected payload, and
    # "protected" includes the AEAD tag: pass the sealed size, not the plain
    # frame bytes.
    header += encode_varint(packet_number_length + payload_length)
    header += int(packet_number).to_bytes(packet_number_length, "big")
    return bytes(header)


def build_short_header(destination_cid, *, packet_number, packet_number_length=4, key_phase=0):
    if not 1 <= packet_number_length <= 4:
        raise ValueError("a packet number is 1 to 4 octets")
    first = FIXED_BIT | ((key_phase & 1) << 2) | (packet_number_length - 1)
    return (
        bytes([first])
        + bytes(destination_cid)
        + int(packet_number).to_bytes(packet_number_length, "big")
    )


def parse_short_header(datagram, connection_id_length):
    """Short headers give no length, so the destination id size must be known."""
    data = bytes(datagram)
    if not data:
        raise QuicPacketError("empty packet")
    if is_long_header(data[0]):
        raise QuicPacketError("not a short header")
    if not data[0] & FIXED_BIT:
        raise QuicPacketError("fixed bit is not set")
    end = 1 + int(connection_id_length)
    if end > len(data):
        raise QuicPacketError("truncated connection id")
    return data[1:end], end


def iter_packets(datagram, connection_id_length=8):
    """Walk the packets coalesced into one datagram."""
    data = bytes(datagram)
    offset = 0
    while offset < len(data):
        if not is_long_header(data[offset]):
            # A short header runs to the end of the datagram.
            yield data[offset:]
            return
        header = parse_long_header(data, offset)
        yield data[offset : offset + header.packet_length]
        if header.packet_length <= 0:
            return
        offset += header.packet_length


__all__ = [
    "FIXED_BIT",
    "HEADER_FORM_LONG",
    "LongHeader",
    "MAX_CONNECTION_ID_LENGTH",
    "MIN_INITIAL_DATAGRAM",
    "PACKET_HANDSHAKE",
    "PACKET_INITIAL",
    "PACKET_RETRY",
    "PACKET_ZERO_RTT",
    "QuicPacketError",
    "build_long_header",
    "build_short_header",
    "is_long_header",
    "iter_packets",
    "new_connection_id",
    "parse_long_header",
    "parse_short_header",
]

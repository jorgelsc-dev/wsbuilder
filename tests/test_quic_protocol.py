import unittest

from wsbuilder.quic.frames import (
    FRAME_HANDSHAKE_DONE,
    FRAME_PING,
    AckFrame,
    ConnectionCloseFrame,
    CryptoFrame,
    MaxStreamDataFrame,
    PathFrame,
    QuicFrameError,
    SimpleFrame,
    StreamFrame,
    pad_to,
    parse_frames,
    serialize_frames,
)
from wsbuilder.quic.packet import (
    MIN_INITIAL_DATAGRAM,
    PACKET_HANDSHAKE,
    PACKET_INITIAL,
    QuicPacketError,
    build_long_header,
    build_short_header,
    is_long_header,
    iter_packets,
    new_connection_id,
    parse_long_header,
    parse_short_header,
)


def h(text):
    return bytes.fromhex(text.replace(" ", ""))


class TestLongHeader(unittest.TestCase):
    DCID = h("8394c8f03e515708")

    def test_matches_the_rfc_9001_initial_header(self):
        # The length field covers the packet number and the sealed payload,
        # tag included: 1162 frame octets plus a 16-octet tag.
        header = build_long_header(
            PACKET_INITIAL, 1, self.DCID, b"", packet_number=2, payload_length=1162 + 16
        )
        self.assertEqual(header.hex(), "c300000001088394c8f03e5157080000449e00000002")

    def test_round_trip(self):
        header = build_long_header(
            PACKET_HANDSHAKE, 1, self.DCID, h("aabb"), packet_number=7, payload_length=40
        )
        parsed = parse_long_header(header + b"x" * 40)
        self.assertEqual(parsed.packet_type, PACKET_HANDSHAKE)
        self.assertEqual(parsed.destination_cid, self.DCID)
        self.assertEqual(parsed.source_cid, h("aabb"))
        self.assertEqual(parsed.version, 1)

    def test_the_fixed_bit_must_be_set(self):
        header = bytearray(
            build_long_header(PACKET_INITIAL, 1, self.DCID, b"", packet_number=1)
        )
        header[0] &= ~0x40
        with self.assertRaisesRegex(QuicPacketError, "fixed bit"):
            parse_long_header(bytes(header))

    def test_an_oversized_connection_id_is_rejected(self):
        packet = bytearray(build_long_header(PACKET_INITIAL, 1, self.DCID, b"", packet_number=1))
        packet[5] = 21  # the destination id length octet
        with self.assertRaisesRegex(QuicPacketError, "20 octets"):
            parse_long_header(bytes(packet))

    def test_a_truncated_datagram_is_rejected(self):
        header = build_long_header(
            PACKET_INITIAL, 1, self.DCID, b"", packet_number=1, payload_length=100
        )
        with self.assertRaisesRegex(QuicPacketError, "past the datagram"):
            parse_long_header(header)

    def test_an_initial_token_is_carried(self):
        header = build_long_header(
            PACKET_INITIAL, 1, self.DCID, b"", packet_number=1, token=b"token"
        )
        self.assertEqual(parse_long_header(header).token, b"token")

    def test_connection_id_generation(self):
        self.assertEqual(len(new_connection_id(8)), 8)
        self.assertNotEqual(new_connection_id(8), new_connection_id(8))
        with self.assertRaises(ValueError):
            new_connection_id(21)


class TestShortHeader(unittest.TestCase):
    def test_round_trip(self):
        cid = h("0011223344556677")
        packet = build_short_header(cid, packet_number=9)
        self.assertFalse(is_long_header(packet[0]))
        self.assertEqual(parse_short_header(packet, len(cid)), (cid, 9))

    def test_the_fixed_bit_must_be_set(self):
        packet = bytearray(build_short_header(h("00112233"), packet_number=1))
        packet[0] &= ~0x40
        with self.assertRaisesRegex(QuicPacketError, "fixed bit"):
            parse_short_header(bytes(packet), 4)


class TestCoalescedDatagrams(unittest.TestCase):
    def test_several_packets_in_one_datagram(self):
        cid = h("8394c8f03e515708")
        first = build_long_header(
            PACKET_INITIAL, 1, cid, b"", packet_number=1, payload_length=8
        ) + b"a" * 8
        second = build_long_header(
            PACKET_HANDSHAKE, 1, cid, b"", packet_number=2, payload_length=4
        ) + b"b" * 4
        packets = list(iter_packets(first + second))
        self.assertEqual(packets, [first, second])

    def test_a_short_header_ends_the_datagram(self):
        cid = h("8394c8f0")
        long_packet = build_long_header(
            PACKET_INITIAL, 1, cid, b"", packet_number=1, payload_length=4
        ) + b"aaaa"
        short = build_short_header(cid, packet_number=2) + b"rest"
        self.assertEqual(list(iter_packets(long_packet + short)), [long_packet, short])


class TestFrames(unittest.TestCase):
    def test_round_trip_of_every_frame_we_build(self):
        frames = [
            CryptoFrame(0, b"hello"),
            StreamFrame(4, 16, b"body", fin=True),
            AckFrame(5, 100, [2, (1, 3)]),
            SimpleFrame(FRAME_PING),
            SimpleFrame(FRAME_HANDSHAKE_DONE),
            MaxStreamDataFrame(4, 65536),
            PathFrame(0x1A, b"12345678"),
            ConnectionCloseFrame(0x0A, "bye"),
        ]
        decoded = parse_frames(serialize_frames(frames))
        self.assertEqual(len(decoded), len(frames))
        self.assertEqual(decoded[0].data, b"hello")
        self.assertEqual((decoded[1].stream_id, decoded[1].offset, decoded[1].fin), (4, 16, True))
        self.assertEqual(decoded[2].largest, 5)
        self.assertEqual(decoded[-1].reason, b"bye")

    def test_padding_is_skipped(self):
        payload = pad_to(serialize_frames([CryptoFrame(0, b"x")]), 200)
        self.assertEqual(len(payload), 200)
        decoded = parse_frames(payload)
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].data, b"x")

    def test_a_stream_frame_without_a_length_runs_to_the_end(self):
        frame = StreamFrame(4, 0, b"tail bytes").serialize(with_length=False)
        decoded = parse_frames(frame)
        self.assertEqual(decoded[0].data, b"tail bytes")

    def test_a_frame_running_past_the_packet_is_rejected(self):
        truncated = CryptoFrame(0, b"12345678").serialize()[:-4]
        with self.assertRaisesRegex(QuicFrameError, "past the packet"):
            parse_frames(truncated)

    def test_an_unknown_frame_type_is_an_error(self):
        # Unlike HTTP/2, QUIC treats an unknown frame as a violation.
        with self.assertRaisesRegex(QuicFrameError, "unknown frame type"):
            parse_frames(b"\x3f")

    def test_a_client_first_flight_must_be_padded(self):
        self.assertEqual(len(pad_to(b"short", MIN_INITIAL_DATAGRAM)), MIN_INITIAL_DATAGRAM)


if __name__ == "__main__":
    unittest.main()

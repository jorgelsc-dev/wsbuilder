import unittest

from wsbuilder.quic.crypto import (
    INITIAL_SALT_V1,
    PacketKeys,
    apply_header_protection,
    decode_packet_number,
    hkdf_expand_label,
    initial_secrets,
    remove_header_protection,
)
from wsbuilder.quic.varint import (
    MAX_VARINT,
    decode_varint,
    decode_varint_prefixed_bytes,
    encode_varint,
    encode_varint_prefixed_bytes,
    varint_length,
)


def h(text):
    return bytes.fromhex(text.replace(" ", ""))


class TestVarint(unittest.TestCase):
    """RFC 9000 appendix A.1."""

    VECTORS = (
        ("c2197c5eff14e88c", 151288809941952652, 8),
        ("9d7f3e7d", 494878333, 4),
        ("7bbd", 15293, 2),
        ("25", 37, 1),
        ("4025", 37, 2),  # the same value in a longer, still legal, form
    )

    def test_rfc_vectors_decode(self):
        for encoded, value, length in self.VECTORS:
            with self.subTest(encoded=encoded):
                self.assertEqual(decode_varint(h(encoded)), (value, length))

    def test_canonical_encodings_round_trip(self):
        for encoded, value, length in self.VECTORS[:4]:
            with self.subTest(value=value):
                self.assertEqual(encode_varint(value), h(encoded))
                self.assertEqual(varint_length(value), length)

    def test_a_wider_form_can_be_asked_for(self):
        # Encodings are not required to be canonical, so reproducing a peer's
        # bytes sometimes means writing a value wider than it needs.
        self.assertEqual(encode_varint(37, 2), h("4025"))
        self.assertEqual(decode_varint(h("4025")), (37, 2))

    def test_boundaries(self):
        for value, length in ((63, 1), (64, 2), (16383, 2), (16384, 4),
                              (1073741823, 4), (1073741824, 8), (MAX_VARINT, 8)):
            with self.subTest(value=value):
                self.assertEqual(varint_length(value), length)
                self.assertEqual(decode_varint(encode_varint(value))[0], value)

    def test_out_of_range_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsigned"):
            encode_varint(-1)
        with self.assertRaisesRegex(ValueError, "62-bit"):
            encode_varint(MAX_VARINT + 1)

    def test_a_value_too_large_for_the_requested_width_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not fit"):
            encode_varint(1000, 1)

    def test_truncated_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "truncated"):
            decode_varint(b"")
        with self.assertRaisesRegex(ValueError, "past the end"):
            decode_varint(h("c2"))

    def test_length_prefixed_blobs(self):
        encoded = encode_varint_prefixed_bytes(b"payload")
        self.assertEqual(decode_varint_prefixed_bytes(encoded), (b"payload", len(encoded)))
        with self.assertRaisesRegex(ValueError, "past the end"):
            decode_varint_prefixed_bytes(h("0f") + b"short")


class TestInitialKeySchedule(unittest.TestCase):
    """RFC 9001 appendix A.1: derived from a salt the specification fixes."""

    DCID = h("8394c8f03e515708")

    def test_client_initial_secret(self):
        client, _server = initial_secrets(self.DCID)
        self.assertEqual(
            client.hex(),
            "c00cf151ca5be075ed0ebfb5c80323c42d6b7db67881289af4008f1f6c357aea",
        )

    def test_client_packet_keys(self):
        client, _server = initial_secrets(self.DCID)
        keys = PacketKeys(client)
        self.assertEqual(keys.key.hex(), "1f369613dd76d5467730efcbe3b1a22d")
        self.assertEqual(keys.iv.hex(), "fa044b2f42a3fd3b46fb255c")
        self.assertEqual(keys.hp.hex(), "9f50449e04a0e810283a1e9933adedd2")

    def test_server_packet_keys(self):
        _client, server = initial_secrets(self.DCID)
        self.assertEqual(PacketKeys(server).key.hex(), "cf3a5331653c364c88f0f379b6067e37")

    def test_the_salt_is_the_published_one(self):
        self.assertEqual(INITIAL_SALT_V1.hex(), "38762cf7f55934b34d179ae6a4c80cadccbb7f0a")

    def test_expand_label_prefixes_tls13(self):
        # A wrong prefix would still produce bytes, just not interoperable ones.
        self.assertEqual(
            hkdf_expand_label(b"\x00" * 32, "quic key", b"", 16),
            hkdf_expand_label(b"\x00" * 32, b"quic key", b"", 16),
        )


class TestHeaderProtection(unittest.TestCase):
    def _keys(self, hp, cipher_name):
        keys = PacketKeys.__new__(PacketKeys)
        keys.hp = hp
        keys.cipher_name = cipher_name
        return keys

    def test_aes_mask_matches_rfc_9001_a2(self):
        keys = self._keys(h("9f50449e04a0e810283a1e9933adedd2"), "aes-128-gcm")
        self.assertEqual(
            keys.header_mask(h("d1b1c98dd7689fb8ec11d242b123dc9b")).hex(), "437b9aec36"
        )

    def test_chacha20_mask_matches_rfc_9001_a5(self):
        keys = PacketKeys(
            h("9ac312a7f877468ebe69422748ad00a15443f18203a07d6060f688f30f21632b"),
            "chacha20-poly1305",
        )
        self.assertEqual(keys.key.hex(),
                         "c6d98ff3441c3fe1b2182094f69caa2ed4b716b65488960a7a984979fb23e1c8")
        self.assertEqual(keys.iv.hex(), "e0459b3474bdd0e44a41c144")
        self.assertEqual(keys.header_mask(h("5e5cd55c41f69080575d7999c25a5bfb")).hex(),
                         "aefefe7d03")

    def test_the_whole_chacha_key_takes_part(self):
        # Guards against a half-correct hp derivation passing the mask check.
        keys = PacketKeys(
            h("9ac312a7f877468ebe69422748ad00a15443f18203a07d6060f688f30f21632b"),
            "chacha20-poly1305",
        )
        tampered = self._keys(bytearray(keys.hp), "chacha20-poly1305")
        tampered.hp[20] ^= 1
        tampered.hp = bytes(tampered.hp)
        sample = h("5e5cd55c41f69080575d7999c25a5bfb")
        self.assertNotEqual(keys.header_mask(sample), tampered.header_mask(sample))

    def test_a_short_sample_is_rejected(self):
        keys = self._keys(h("9f50449e04a0e810283a1e9933adedd2"), "aes-128-gcm")
        with self.assertRaisesRegex(ValueError, "16 octets"):
            keys.header_mask(b"tooshort")


class TestPacketProtection(unittest.TestCase):
    def setUp(self):
        client, _server = initial_secrets(h("8394c8f03e515708"))
        self.keys = PacketKeys(client)
        self.header = h("c300000001088394c8f03e5157080000449e00000002")
        self.pn_offset = len(self.header) - 4

    def test_seal_and_open_round_trip(self):
        payload = b"quic frames" * 8
        sealed = self.keys.seal(2, self.header, payload)
        self.assertEqual(self.keys.open(2, self.header, sealed), payload)

    def test_the_header_is_authenticated(self):
        sealed = self.keys.seal(2, self.header, b"payload")
        tampered = bytearray(self.header)
        tampered[-1] ^= 1
        with self.assertRaises(Exception):
            self.keys.open(2, bytes(tampered), sealed)

    def test_a_different_packet_number_will_not_open_it(self):
        sealed = self.keys.seal(2, self.header, b"payload")
        with self.assertRaises(Exception):
            self.keys.open(3, self.header, sealed)

    def test_header_protection_round_trip(self):
        packet = self.header + self.keys.seal(2, self.header, b"payload" * 8)
        protected = apply_header_protection(self.keys, packet, self.pn_offset, 4)
        self.assertNotEqual(protected[:1], packet[:1])
        recovered, number, pn_length = remove_header_protection(
            self.keys, protected, self.pn_offset
        )
        self.assertEqual(number, 2)
        self.assertEqual(pn_length, 4)
        self.assertEqual(recovered, packet)

    def test_a_packet_too_short_to_sample_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "too short"):
            remove_header_protection(self.keys, self.header, self.pn_offset)


class TestPacketNumberRecovery(unittest.TestCase):
    """RFC 9000 appendix A.3."""

    def test_rfc_example(self):
        self.assertEqual(decode_packet_number(0x9B32, 2, 0xA82F30EA), 0xA82F9B32)

    def test_wrapping_forwards_and_backwards(self):
        self.assertEqual(decode_packet_number(0x00, 1, 0xFF), 0x100)
        self.assertEqual(decode_packet_number(0xFF, 1, 0x100), 0xFF)

    def test_the_first_packet(self):
        self.assertEqual(decode_packet_number(0x01, 1, 0x00), 0x01)


if __name__ == "__main__":
    unittest.main()

import unittest

from wsbuilder.hpack import (
    HUFFMAN_CODES,
    STATIC_TABLE_SIZE,
    Decoder,
    DynamicTable,
    Encoder,
    HPACKError,
    decode_integer,
    decode_string,
    encode_integer,
    encode_string,
    huffman_decode,
    huffman_encode,
)


def h(text):
    return bytes.fromhex(text.replace(" ", ""))


class TestHuffmanTable(unittest.TestCase):
    """The appendix B table is transcribed data; check its shape, not a copy."""

    def test_the_code_is_complete(self):
        # Kraft equality holds exactly for a complete prefix code.
        self.assertEqual(sum(2.0 ** -length for _, length in HUFFMAN_CODES), 1.0)

    def test_the_code_is_prefix_free(self):
        codes = {(code, length) for code, length in HUFFMAN_CODES}
        for code, length in HUFFMAN_CODES:
            for shorter in range(1, length):
                self.assertNotIn((code >> (length - shorter), shorter), codes)

    def test_symbol_count(self):
        self.assertEqual(len(HUFFMAN_CODES), 257)  # 256 octets plus EOS


class TestIntegerCoding(unittest.TestCase):
    def test_rfc_c_1_vectors(self):
        self.assertEqual(encode_integer(10, 5), h("0a"))
        self.assertEqual(encode_integer(1337, 5), h("1f9a0a"))
        self.assertEqual(encode_integer(42, 8), h("2a"))

    def test_decoding_is_the_inverse(self):
        for value in (0, 1, 30, 31, 42, 127, 128, 1337, 100000):
            for prefix in (4, 5, 6, 7, 8):
                with self.subTest(value=value, prefix=prefix):
                    encoded = encode_integer(value, prefix)
                    self.assertEqual(decode_integer(encoded, 0, prefix), (value, len(encoded)))

    def test_truncated_input_is_rejected(self):
        with self.assertRaisesRegex(HPACKError, "truncated"):
            decode_integer(b"", 0, 5)
        with self.assertRaisesRegex(HPACKError, "truncated"):
            decode_integer(h("1f"), 0, 5)

    def test_an_endless_continuation_is_refused(self):
        # Otherwise a peer could spin the decoder building a huge integer.
        with self.assertRaisesRegex(HPACKError, "too long"):
            decode_integer(h("1f") + b"\x80" * 10 + b"\x01", 0, 5)

    def test_negative_values_are_rejected(self):
        with self.assertRaises(ValueError):
            encode_integer(-1, 5)


class TestHuffmanCoding(unittest.TestCase):
    def test_rfc_c_4_string(self):
        self.assertEqual(huffman_encode(b"www.example.com"), h("f1e3c2e5f23a6ba0ab90f4ff"))
        self.assertEqual(huffman_decode(h("f1e3c2e5f23a6ba0ab90f4ff")), b"www.example.com")

    def test_round_trip_over_every_octet(self):
        payload = bytes(range(256))
        self.assertEqual(huffman_decode(huffman_encode(payload)), payload)

    def test_empty_input(self):
        self.assertEqual(huffman_encode(b""), b"")
        self.assertEqual(huffman_decode(b""), b"")

    def test_eos_in_the_stream_is_rejected(self):
        # EOS is padding only; carrying it is a decoding error.
        with self.assertRaisesRegex(HPACKError, "EOS"):
            huffman_decode(h("ffffffff"))

    def test_string_literal_falls_back_when_huffman_is_longer(self):
        # Huffman expands some inputs; the shorter form must win.
        payload = bytes([0]) * 4
        encoded = encode_string(payload.decode("latin-1"), huffman=True)
        self.assertFalse(encoded[0] & 0x80)

    def test_string_round_trip(self):
        for text in ("", "/", "www.example.com", "a" * 200, "custom-key"):
            for huffman in (True, False):
                with self.subTest(text=text, huffman=huffman):
                    encoded = encode_string(text, huffman=huffman)
                    self.assertEqual(decode_string(encoded, 0), (text, len(encoded)))

    def test_a_string_longer_than_the_block_is_rejected(self):
        with self.assertRaisesRegex(HPACKError, "longer than the header block"):
            decode_string(h("0f") + b"short", 0)


class TestRFCRequestSequences(unittest.TestCase):
    """RFC 7541 appendix C.3 and C.4: the same requests, plain and Huffman."""

    EXPECTED = (
        [(":method", "GET"), (":scheme", "http"), (":path", "/"),
         (":authority", "www.example.com")],
        [(":method", "GET"), (":scheme", "http"), (":path", "/"),
         (":authority", "www.example.com"), ("cache-control", "no-cache")],
        [(":method", "GET"), (":scheme", "https"), (":path", "/index.html"),
         (":authority", "www.example.com"), ("custom-key", "custom-value")],
    )

    def test_c_3_without_huffman(self):
        decoder = Decoder()
        blocks = (
            "8286 8441 0f77 7777 2e65 7861 6d70 6c65 2e63 6f6d",
            "8286 84be 5808 6e6f 2d63 6163 6865",
            "8287 85bf 400a 6375 7374 6f6d 2d6b 6579 0c63 7573 746f 6d2d 7661 6c75 65",
        )
        for blob, expected in zip(blocks, self.EXPECTED):
            with self.subTest(block=blob[:9]):
                self.assertEqual(decoder.decode(h(blob)), expected)
        self.assertEqual(len(decoder.table), 3)
        self.assertEqual(decoder.table.size, 164)

    def test_c_4_with_huffman(self):
        decoder = Decoder()
        blocks = (
            "8286 8441 8cf1 e3c2 e5f2 3a6b a0ab 90f4 ff",
            "8286 84be 5886 a8eb 1064 9cbf",
            "8287 85bf 4088 25a8 49e9 5ba9 7d7f 8925 a849 e95b b8e8 b4bf",
        )
        for blob, expected in zip(blocks, self.EXPECTED):
            with self.subTest(block=blob[:9]):
                self.assertEqual(decoder.decode(h(blob)), expected)
        self.assertEqual(decoder.table.size, 164)


class TestRFCResponseSequenceWithEviction(unittest.TestCase):
    """RFC 7541 appendix C.6: a 256-byte table forces entries out."""

    def test_c_6(self):
        decoder = Decoder(256)

        first = decoder.decode(h(
            "4882 6402 5885 aec3 771a 4b61 96d0 7abe 9410 54d4 44a8 2005 9504 0b81 "
            "66e0 82a6 2d1b ff6e 919d 29ad 1718 63c7 8f0b 97c8 e9ae 82ae 43d3"
        ))
        self.assertEqual(first[0], (":status", "302"))
        self.assertEqual(first[-1], ("location", "https://www.example.com"))
        self.assertEqual(decoder.table.size, 222)

        second = decoder.decode(h("4883 640e ffc1 c0bf"))
        self.assertEqual(second[0], (":status", "307"))
        self.assertEqual(len(decoder.table), 4)

        third = decoder.decode(h(
            "88c1 6196 d07a be94 1054 d444 a820 0595 040b 8166 e084 a62d 1bff c05a "
            "839b d9ab 77ad 94e7 821d d7f2 e6c7 b335 dfdf cd5b 3960 d5af 2708 7f36 "
            "72c1 ab27 0fb5 291f 9587 3160 65c0 03ed 4ee5 b106 3d50 07"
        ))
        self.assertEqual(third[0], (":status", "200"))
        self.assertEqual(
            third[-1],
            ("set-cookie", "foo=ASDJKHQKBZXOQWEOPIUAXQWEOIU; max-age=3600; version=1"),
        )
        # Room for the cookie was made by dropping older entries.
        self.assertEqual(len(decoder.table), 3)
        self.assertLessEqual(decoder.table.size, 256)


class TestRepresentations(unittest.TestCase):
    def test_literal_with_incremental_indexing(self):
        decoder = Decoder()
        block = h("400a 6375 7374 6f6d 2d6b 6579 0d63 7573 746f 6d2d 6865 6164 6572")
        self.assertEqual(decoder.decode(block), [("custom-key", "custom-header")])
        self.assertEqual(len(decoder.table), 1)

    def test_literal_without_indexing_leaves_the_table_alone(self):
        decoder = Decoder()
        self.assertEqual(
            decoder.decode(h("040c 2f73 616d 706c 652f 7061 7468")),
            [(":path", "/sample/path")],
        )
        self.assertEqual(len(decoder.table), 0)

    def test_never_indexed_leaves_the_table_alone(self):
        decoder = Decoder()
        self.assertEqual(
            decoder.decode(h("1008 7061 7373 776f 7264 0673 6563 7265 74")),
            [("password", "secret")],
        )
        self.assertEqual(len(decoder.table), 0)

    def test_indexed_static_field(self):
        self.assertEqual(Decoder().decode(h("82")), [(":method", "GET")])

    def test_index_zero_is_not_a_field(self):
        with self.assertRaisesRegex(HPACKError, "index 0"):
            Decoder().decode(h("80"))

    def test_an_index_past_the_table_is_rejected(self):
        with self.assertRaisesRegex(HPACKError, "out of range"):
            Decoder().decode(h("ff00"))


class TestDynamicTable(unittest.TestCase):
    def test_entry_cost_includes_the_overhead(self):
        self.assertEqual(DynamicTable.entry_size("a", "b"), 34)

    def test_oldest_entries_leave_first(self):
        table = DynamicTable(max_size=2 * DynamicTable.entry_size("k", "v"))
        table.add("k", "1")
        table.add("k", "2")
        table.add("k", "3")
        self.assertEqual([v for _, v in table.entries()], ["3", "2"])

    def test_an_oversized_entry_empties_the_table(self):
        table = DynamicTable(max_size=60)
        table.add("k", "v")
        self.assertEqual(len(table), 1)
        # Section 4.4: too big to store means the table is cleared instead.
        self.assertFalse(table.add("k", "v" * 100))
        self.assertEqual(len(table), 0)
        self.assertEqual(table.size, 0)

    def test_shrinking_the_limit_evicts(self):
        table = DynamicTable(4096)
        for i in range(10):
            table.add("name", str(i))
        table.set_max_size(80)
        self.assertLessEqual(table.size, 80)

    def test_lookup_prefers_an_exact_match(self):
        table = DynamicTable()
        table.add("x", "other")
        table.add("x", "wanted")
        index, exact = table.find("x", "wanted")
        self.assertTrue(exact)
        self.assertEqual(index, 1)

    def test_lookup_falls_back_to_the_name(self):
        table = DynamicTable()
        table.add("x", "something")
        index, exact = table.find("x", "absent")
        self.assertFalse(exact)
        self.assertEqual(index, 1)


class TestTableSizeUpdates(unittest.TestCase):
    def test_an_update_resizes_the_table(self):
        decoder = Decoder(4096)
        decoder.decode(h("3f e1 1f"))  # 4096
        self.assertEqual(decoder.table.max_size, 4096)
        decoder.decode(h("20"))  # 0
        self.assertEqual(decoder.table.max_size, 0)

    def test_an_update_beyond_the_agreed_maximum_is_rejected(self):
        decoder = Decoder(256)
        with self.assertRaisesRegex(HPACKError, "exceeds the agreed"):
            decoder.decode(h("3f e1 1f"))

    def test_an_update_after_a_field_is_rejected(self):
        decoder = Decoder()
        with self.assertRaisesRegex(HPACKError, "must precede header fields"):
            decoder.decode(h("82") + h("20"))


class TestEncoder(unittest.TestCase):
    def test_round_trip_through_a_fresh_decoder(self):
        encoder, decoder = Encoder(), Decoder()
        blocks = (
            [(":method", "GET"), (":scheme", "https"), (":path", "/"), (":authority", "x.test")],
            [(":method", "POST"), (":path", "/api"), ("content-type", "application/json")],
            [(":status", "200"), ("content-length", "42")],
        )
        for headers in blocks:
            with self.subTest(first=headers[0]):
                self.assertEqual(decoder.decode(encoder.encode(headers)), headers)

    def test_static_entries_compress_to_one_byte(self):
        self.assertEqual(Encoder().encode([(":method", "GET")]), h("82"))

    def test_repeats_use_the_dynamic_table(self):
        encoder = Encoder()
        headers = [("x-custom", "value")]
        first = encoder.encode(headers)
        second = encoder.encode(headers)
        self.assertLess(len(second), len(first))
        self.assertEqual(len(second), 1)

    def test_sensitive_headers_are_never_indexed(self):
        encoder = Encoder()
        encoder.encode([("authorization", "Bearer secret")])
        # Indexing it would leak the value into a shared table.
        self.assertEqual(len(encoder.table), 0)

    def test_names_are_lowercased(self):
        encoder, decoder = Encoder(), Decoder()
        self.assertEqual(
            decoder.decode(encoder.encode([("Content-Type", "text/plain")])),
            [("content-type", "text/plain")],
        )

    def test_a_queued_size_update_is_emitted_first(self):
        encoder, decoder = Encoder(), Decoder()
        encoder.set_max_size(256)
        block = encoder.encode([(":method", "GET")])
        self.assertEqual(block[0] & 0xE0, 0x20)
        self.assertEqual(decoder.decode(block), [(":method", "GET")])
        self.assertEqual(decoder.table.max_size, 256)


if __name__ == "__main__":
    unittest.main()

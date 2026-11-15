import json
import math
import unittest

from wsbuilder import (
    CBORDecodeError,
    CBOREncodeError,
    CBORSimple,
    CBORTag,
    cbor_dumps,
    cbor_loads,
)

# RFC 8949 appendix A: value, preferred encoding.
RFC_VECTORS = [
    (0, "00"),
    (1, "01"),
    (10, "0a"),
    (23, "17"),
    (24, "1818"),
    (25, "1819"),
    (100, "1864"),
    (1000, "1903e8"),
    (1000000, "1a000f4240"),
    (1000000000000, "1b000000e8d4a51000"),
    (18446744073709551615, "1bffffffffffffffff"),
    (18446744073709551616, "c249010000000000000000"),
    (-18446744073709551616, "3bffffffffffffffff"),
    (-18446744073709551617, "c349010000000000000000"),
    (-1, "20"),
    (-10, "29"),
    (-100, "3863"),
    (-1000, "3903e7"),
    (0.0, "f90000"),
    (-0.0, "f98000"),
    (1.0, "f93c00"),
    (1.1, "fb3ff199999999999a"),
    (1.5, "f93e00"),
    (65504.0, "f97bff"),
    (100000.0, "fa47c35000"),
    (3.4028234663852886e38, "fa7f7fffff"),
    (1.0e300, "fb7e37e43c8800759c"),
    (5.960464477539063e-8, "f90001"),
    (0.00006103515625, "f90400"),
    (-4.0, "f9c400"),
    (-4.1, "fbc010666666666666"),
    (math.inf, "f97c00"),
    (-math.inf, "f9fc00"),
    (False, "f4"),
    (True, "f5"),
    (None, "f6"),
    (b"", "40"),
    (b"\x01\x02\x03\x04", "4401020304"),
    ("", "60"),
    ("a", "6161"),
    ("IETF", "6449455446"),
    ("\"\\", "62225c"),
    ("ü", "62c3bc"),
    ("水", "63e6b0b4"),
    ("\U00010151", "64f0908591"),
    ([], "80"),
    ([1, 2, 3], "83010203"),
    ([1, [2, 3], [4, 5]], "8301820203820405"),
    (list(range(1, 26)), "98190102030405060708090a0b0c0d0e0f101112131415161718181819"),
    ({}, "a0"),
    ({1: 2, 3: 4}, "a201020304"),
    ({"a": 1, "b": [2, 3]}, "a26161016162820203"),
    (["a", {"b": "c"}], "826161a161626163"),
]


class TestRfcVectors(unittest.TestCase):
    def test_preferred_encodings(self):
        for value, expected in RFC_VECTORS:
            with self.subTest(value=value):
                self.assertEqual(cbor_dumps(value).hex(), expected)

    def test_vectors_decode_back(self):
        for value, encoded in RFC_VECTORS:
            with self.subTest(encoded=encoded):
                decoded = cbor_loads(bytes.fromhex(encoded))
                self.assertEqual(decoded, value)
                self.assertIs(type(decoded), type(value))

    def test_negative_zero_keeps_its_sign(self):
        self.assertEqual(math.copysign(1.0, cbor_loads(cbor_dumps(-0.0))), -1.0)

    def test_nan_round_trips(self):
        self.assertEqual(cbor_dumps(math.nan).hex(), "f97e00")
        self.assertTrue(math.isnan(cbor_loads(bytes.fromhex("f97e00"))))
        self.assertTrue(math.isnan(cbor_loads(bytes.fromhex("fb7ff8000000000000"))))

    def test_indefinite_lengths_decode(self):
        self.assertEqual(cbor_loads(bytes.fromhex("5f42010243030405ff")), b"\x01\x02\x03\x04\x05")
        self.assertEqual(cbor_loads(bytes.fromhex("7f657374726561646d696e67ff")), "streaming")
        self.assertEqual(cbor_loads(bytes.fromhex("9fff")), [])
        self.assertEqual(
            cbor_loads(bytes.fromhex("9f018202039f0405ffff")), [1, [2, 3], [4, 5]]
        )
        self.assertEqual(
            cbor_loads(bytes.fromhex("bf61610161629f0203ffff")), {"a": 1, "b": [2, 3]}
        )
        self.assertEqual(
            cbor_loads(bytes.fromhex("bf6346756ef563416d7421ff")), {"Fun": True, "Amt": -2}
        )

    def test_tags_and_simple_values(self):
        self.assertEqual(cbor_loads(bytes.fromhex("c11a514b67b0")), CBORTag(1, 1363896240))
        self.assertEqual(
            cbor_loads(bytes.fromhex("d74401020304")), CBORTag(23, b"\x01\x02\x03\x04")
        )
        self.assertEqual(cbor_loads(bytes.fromhex("f0")), CBORSimple(16))
        self.assertEqual(cbor_loads(bytes.fromhex("f8ff")), CBORSimple(255))
        self.assertIsNone(cbor_loads(bytes.fromhex("f7")))  # undefined
        self.assertEqual(cbor_dumps(CBORTag(1, 1363896240)).hex(), "c11a514b67b0")
        self.assertEqual(cbor_dumps(CBORSimple(255)).hex(), "f8ff")
        self.assertEqual(cbor_dumps(CBORSimple(16)).hex(), "f0")


class TestRoundTrips(unittest.TestCase):
    def test_a_typical_service_payload(self):
        payload = {
            "user": {"id": 42, "name": "Ada Lovelace", "admin": False, "score": 97.25},
            "tags": ["math", "engines", "ñandú"],
            "avatar": bytes(range(256)),
            "nothing": None,
            "nested": [[1, -2, 3.5], {"deep": {"deeper": [True]}}],
            "big": 2**70,
            "negative_big": -(2**70),
        }
        self.assertEqual(cbor_loads(cbor_dumps(payload)), payload)

    def test_tuples_and_bytearray_encode_as_arrays_and_bytes(self):
        self.assertEqual(cbor_loads(cbor_dumps((1, 2))), [1, 2])
        self.assertEqual(cbor_loads(cbor_dumps(bytearray(b"ab"))), b"ab")
        self.assertEqual(cbor_loads(cbor_dumps(memoryview(b"cd"))), b"cd")

    def test_subclasses_of_builtin_types(self):
        class Name(str):
            pass

        class Count(int):
            pass

        class Record(dict):
            pass

        encoded = cbor_dumps(Record({Name("n"): Count(3)}))
        self.assertEqual(cbor_loads(encoded), {"n": 3})

    def test_long_strings_and_containers_use_wide_heads(self):
        text = "x" * 70000
        items = list(range(300))
        mapping = {str(i): i for i in range(300)}
        for value in (text, items, mapping, b"y" * 70000):
            self.assertEqual(cbor_loads(cbor_dumps(value)), value)

    def test_array_keys_decode_as_tuples(self):
        self.assertEqual(cbor_loads(bytes.fromhex("a1820102f5")), {(1, 2): True})

    def test_smaller_than_json_for_records(self):
        payload = {
            "items": [
                {"id": i, "name": f"item-{i}", "price": i * 1.25, "stock": i % 7 == 0}
                for i in range(100)
            ]
        }
        as_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(cbor_dumps(payload)), len(as_json) * 0.8)

    def test_bytes_are_not_inflated_like_base64(self):
        blob = bytes(range(256)) * 16
        self.assertEqual(len(cbor_dumps(blob)), len(blob) + 3)


class TestHostileInput(unittest.TestCase):
    def assertRefused(self, hex_input, **kwargs):
        with self.assertRaises(CBORDecodeError):
            cbor_loads(bytes.fromhex(hex_input), **kwargs)

    def test_truncated_items(self):
        for hex_input in ("", "18", "19", "1a0000", "1b00", "62", "6261", "82", "8201", "a1", "a101", "c1", "fb00"):
            with self.subTest(hex_input=hex_input):
                self.assertRefused(hex_input)

    def test_trailing_bytes(self):
        self.assertRefused("0000")

    def test_a_declared_length_past_the_input_allocates_nothing(self):
        # Claims 2**64-1 items in a nine-octet input.
        self.assertRefused("9bffffffffffffffff")
        self.assertRefused("bbffffffffffffffff")
        self.assertRefused("5bffffffffffffffff")

    def test_nesting_is_bounded(self):
        self.assertRefused("81" * 300)
        self.assertEqual(cbor_loads(bytes.fromhex("81" * 10 + "00"), max_depth=10), [[[[[[[[[[0]]]]]]]]]])
        self.assertRefused("81" * 11 + "00", max_depth=10)
        self.assertRefused("9f" * 300)
        self.assertRefused("c1" * 300 + "00")

    def test_invalid_utf8(self):
        self.assertRefused("62c328")

    def test_reserved_and_misplaced_bytes(self):
        for hex_input in ("1c", "1f", "ff", "f81f", "fc", "5f01ff", "7f4100ff", "5f5f4100ffff"):
            with self.subTest(hex_input=hex_input):
                self.assertRefused(hex_input)

    def test_unterminated_indefinite_items(self):
        for hex_input in ("9f01", "bf6161", "5f4101", "7f6161"):
            with self.subTest(hex_input=hex_input):
                self.assertRefused(hex_input)

    def test_a_map_as_a_map_key(self):
        self.assertRefused("a1a00000")

    def test_a_bignum_tag_must_wrap_bytes(self):
        self.assertRefused("c201")


class TestEncodingErrors(unittest.TestCase):
    def test_unsupported_types(self):
        for value in (object(), {1, 2}, 1j):
            with self.subTest(value=value), self.assertRaises(CBOREncodeError):
                cbor_dumps(value)

    def test_nesting_is_bounded(self):
        value = []
        for _ in range(300):
            value = [value]
        with self.assertRaises(CBOREncodeError):
            cbor_dumps(value)
        self.assertEqual(cbor_loads(cbor_dumps(value, max_depth=400), max_depth=400), value)

    def test_invalid_wrapper_values(self):
        with self.assertRaises(ValueError):
            CBORSimple(24)
        with self.assertRaises(ValueError):
            CBORTag(-1, 0)


if __name__ == "__main__":
    unittest.main()

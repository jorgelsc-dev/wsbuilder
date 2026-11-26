import json
import os
import struct
import tempfile
import unittest

from wsbuilder import (
    GGUFReader,
    SafetensorsReader,
    ShardedSafetensors,
    WeightsFormatError,
    WeightTensor,
    detect_weights_format,
    open_weights,
)

# -- builders ---------------------------------------------------------------


def _f16(values):
    return struct.pack(f"<{len(values)}e", *values)


def _bf16(values):
    """bfloat16 bytes: the high half of each float32 (values must be exact in bf16)."""
    return b"".join(struct.pack("<I", value)[2:] for value in struct.unpack(
        f"<{len(values)}I", struct.pack(f"<{len(values)}f", *values)))


def safetensors_bytes(tensors, metadata=None, *, header=None):
    """``tensors``: (name, dtype, shape, raw bytes). ``header`` replaces the JSON header."""
    entries = {}
    data = b""
    for name, dtype, shape, raw in tensors:
        entries[name] = {"dtype": dtype, "shape": list(shape),
                         "data_offsets": [len(data), len(data) + len(raw)]}
        data += raw
    if metadata is not None:
        entries["__metadata__"] = metadata
    text = (json.dumps(entries) if header is None else header).encode()
    text += b" " * (-len(text) % 8)
    return struct.pack("<Q", len(text)) + text + data


GGUF_UINT8, GGUF_INT8, GGUF_UINT16, GGUF_INT16 = 0, 1, 2, 3
GGUF_UINT32, GGUF_INT32, GGUF_FLOAT32, GGUF_BOOL = 4, 5, 6, 7
GGUF_STRING, GGUF_ARRAY, GGUF_UINT64, GGUF_INT64, GGUF_FLOAT64 = 8, 9, 10, 11, 12
_CODES = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q",
          12: "d"}


def _gguf_string(text):
    raw = text.encode() if isinstance(text, str) else text
    return struct.pack("<Q", len(raw)) + raw


def _gguf_value(value_type, value):
    if value_type == GGUF_STRING:
        return _gguf_string(value)
    if value_type == GGUF_ARRAY:
        item_type, items = value
        body = b"".join(_gguf_value(item_type, item) for item in items)
        return struct.pack("<IQ", item_type, len(items)) + body
    return struct.pack("<" + _CODES[value_type], value)


def gguf_bytes(kv=(), tensors=(), *, version=3, alignment=32, offsets=None):
    """``kv``: (key, type, value). ``tensors``: (name, ggml dims ne[0] first, type id, raw)."""
    out = b"GGUF" + struct.pack("<IQQ", version, len(tensors), len(kv))
    for key, value_type, value in kv:
        out += _gguf_string(key) + struct.pack("<I", value_type) + _gguf_value(value_type, value)
    data = b""
    positions = []
    for _name, _dims, _type_id, raw in tensors:
        data += b"\x00" * (-len(data) % alignment)
        positions.append(len(data))
        data += raw
    if offsets is not None:
        positions = offsets
    for (name, dims, type_id, _raw), offset in zip(tensors, positions):
        out += _gguf_string(name) + struct.pack("<I", len(dims))
        out += b"".join(struct.pack("<Q", dim) for dim in dims)
        out += struct.pack("<IQ", type_id, offset)
    out += b"\x00" * (-len(out) % alignment)
    return out + data


def _q8_0_block(scale, quants):
    return _f16([scale]) + struct.pack("<32b", *quants)


def _q4_0_block(scale, low, high):
    """``low`` are the elements 0..15 and ``high`` 16..31, as nibbles 0..15."""
    return _f16([scale]) + bytes(lo | (hi << 4) for lo, hi in zip(low, high))


class _TempDir(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name

    def write(self, name, data):
        path = os.path.join(self.directory, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def open(self, reader):
        self.addCleanup(reader.close)
        return reader


# -- safetensors ------------------------------------------------------------


class TestSafetensors(_TempDir):
    def sample(self):
        return self.write("model.safetensors", safetensors_bytes([
            ("w.f32", "F32", (2, 3), struct.pack("<6f", 1, 2, 3, 4, 5, 6.5)),
            ("w.f16", "F16", (4,), _f16([1.0, -2.5, 0.5, 65504.0])),
            ("w.bf16", "BF16", (3,), _bf16([1.0, -2.0, 3.140625])),
            ("w.i32", "I32", (2,), struct.pack("<2i", -7, 2**31 - 1)),
            ("w.i64", "I64", (1,), struct.pack("<q", -(2**40))),
            ("w.u8", "U8", (3,), bytes([0, 128, 255])),
            ("w.bool", "BOOL", (2,), bytes([1, 0])),
            ("w.f64", "F64", (1,), struct.pack("<d", 0.1)),
            ("scalar", "F32", (), struct.pack("<f", 9.0)),
        ], metadata={"format": "pt"}))

    def test_values_of_plain_dtypes(self):
        reader = self.open(SafetensorsReader(self.sample()))
        self.assertEqual(reader.format, "safetensors")
        self.assertEqual(reader.metadata, {"format": "pt"})
        self.assertEqual(len(reader), 9)
        self.assertEqual(reader.names()[0], "w.f32")
        self.assertEqual(list(reader), reader.names())
        self.assertIn("w.f16", reader)
        self.assertNotIn("missing", reader)
        tensor = reader.tensor("w.f32")
        self.assertIsInstance(tensor, WeightTensor)
        self.assertEqual((tensor.shape, tensor.dtype, tensor.element_count, tensor.nbytes),
                         ((2, 3), "F32", 6, 24))
        self.assertEqual(tensor.rows(), [[1, 2, 3], [4, 5, 6.5]])
        self.assertEqual(tensor.to_array().typecode, "f")
        self.assertEqual(reader.tensor("w.f16").values(), [1.0, -2.5, 0.5, 65504.0])
        self.assertEqual(reader.tensor("w.f16").to_array().typecode, "f")
        self.assertEqual(reader.tensor("w.bf16").values(), [1.0, -2.0, 3.140625])
        self.assertEqual(reader.tensor("w.i32").values(), [-7, 2**31 - 1])
        self.assertEqual(reader.tensor("w.i64").values(), [-(2**40)])
        self.assertEqual(reader.tensor("w.u8").values(), [0, 128, 255])
        self.assertEqual(reader.tensor("w.bool").values(), [True, False])
        self.assertEqual(reader.tensor("w.f64").values(), [0.1])
        self.assertEqual(reader.tensor("w.f64").to_array().typecode, "d")
        self.assertEqual(reader.tensor("scalar").shape, ())
        self.assertEqual(reader.tensor("scalar").values(), [9.0])
        self.assertEqual(reader.parameter_count, 6 + 4 + 3 + 2 + 1 + 3 + 2 + 1 + 1)
        with self.assertRaises(KeyError):
            reader.tensor("missing")
        with self.assertRaises(WeightsFormatError):
            reader.tensor("w.f16").rows()

    def test_raw_is_a_read_only_view_without_copy(self):
        reader = self.open(SafetensorsReader(self.sample()))
        view = reader.tensor("w.i32").raw()
        self.addCleanup(view.release)
        self.assertIsInstance(view, memoryview)
        self.assertTrue(view.readonly)
        self.assertEqual(bytes(view), struct.pack("<2i", -7, 2**31 - 1))
        with self.assertRaises(TypeError):
            view[0] = 1

    def test_summary_is_json(self):
        reader = self.open(SafetensorsReader(self.sample()))
        summary = reader.summary()
        json.dumps(summary, allow_nan=False)
        self.assertEqual(summary["format"], "safetensors")
        self.assertEqual(summary["tensors"], 9)
        self.assertEqual(summary["parameters"], reader.parameter_count)
        self.assertEqual(summary["dtypes"]["F32"], 7)
        self.assertEqual(summary["file_size"], os.path.getsize(reader.path))

    def test_detection_and_open_weights(self):
        path = self.sample()
        self.assertEqual(detect_weights_format(path), "safetensors")
        reader = self.open(open_weights(path))
        self.assertIsInstance(reader, SafetensorsReader)
        for data in (b"", b"{}", b"\x00GGUF", b"\x02" + b"\x00" * 7 + b"[]", b"just text here"):
            self.assertIsNone(detect_weights_format(self.write("other", data)))
        self.assertIsNone(detect_weights_format(os.path.join(self.directory, "absent")))
        with self.assertRaises(WeightsFormatError):
            open_weights(self.write("notes.txt", b"plain text, not weights"))
        with self.assertRaises(FileNotFoundError):
            open_weights(os.path.join(self.directory, "absent"))

    def test_raw_only_dtypes(self):
        path = self.write("f8.safetensors", safetensors_bytes([("x", "F8_E4M3", (2,), b"\x01\x02")]))
        reader = self.open(SafetensorsReader(path))
        with reader.tensor("x").raw() as view:
            self.assertEqual(bytes(view), b"\x01\x02")
        with self.assertRaises(NotImplementedError):
            reader.tensor("x").to_array()

    def assert_rejected(self, data, **options):
        path = self.write("bad.safetensors", data)
        with self.assertRaises(WeightsFormatError):
            SafetensorsReader(path, **options).close()

    def test_malformed_headers_are_rejected(self):
        good = safetensors_bytes([("a", "F32", (1,), b"\x00" * 4)])
        self.assert_rejected(b"")
        self.assert_rejected(b"\x05\x00")
        self.assert_rejected(struct.pack("<Q", 1000) + b'{"a": 1}')  # truncated header
        self.assert_rejected(good, max_header_bytes=8)  # header too large
        self.assert_rejected(struct.pack("<Q", 8) + b"not json")
        self.assert_rejected(struct.pack("<Q", 4) + b"[1] ")
        self.assert_rejected(safetensors_bytes([], header='{"a": 1, "a": 2}'))
        self.assert_rejected(safetensors_bytes([], metadata={"n": 1}))
        self.assert_rejected(safetensors_bytes([("a", "F33", (1,), b"\x00" * 4)]))

    def test_bad_tensor_entries_are_rejected(self):
        def header(entry):
            return safetensors_bytes([], header=json.dumps({"a": entry})) + b"\x00" * 8

        self.assert_rejected(header({"dtype": "F32", "shape": [-1], "data_offsets": [0, 4]}))
        self.assert_rejected(header({"dtype": "F32", "shape": [True], "data_offsets": [0, 4]}))
        self.assert_rejected(header({"dtype": "F32", "shape": [1] * 9, "data_offsets": [0, 4]}))
        self.assert_rejected(header({"dtype": "F32", "shape": "1", "data_offsets": [0, 4]}))
        self.assert_rejected(header({"dtype": "F32", "shape": [1], "data_offsets": [4, 0]}))
        self.assert_rejected(header({"dtype": "F32", "shape": [1], "data_offsets": [0]}))
        # Outside the data area (8 bytes) and a size that does not match the shape.
        self.assert_rejected(header({"dtype": "F32", "shape": [2], "data_offsets": [4, 12]}))
        self.assert_rejected(header({"dtype": "F32", "shape": [1], "data_offsets": [0, 8]}))
        self.assert_rejected(header([1, 2]))

    def test_deeply_nested_json_is_rejected(self):
        nested = '{"a": ' + "[" * 200000 + "]" * 200000 + "}"
        data = safetensors_bytes([], header=nested)
        self.assert_rejected(data)
        self.assertIsNone(detect_weights_format(self.write("deep.safetensors", data)))

    def test_overlapping_tensors_are_rejected(self):
        entries = {
            "a": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]},
            "b": {"dtype": "F32", "shape": [1], "data_offsets": [4, 8]},
        }
        self.assert_rejected(safetensors_bytes([], header=json.dumps(entries)) + b"\x00" * 8)
        entries["b"]["data_offsets"] = [8, 12]
        path = self.write("ok.safetensors",
                          safetensors_bytes([], header=json.dumps(entries)) + b"\x00" * 12)
        self.assertEqual(len(self.open(SafetensorsReader(path))), 2)

    def test_close_is_idempotent_and_views_survive_it(self):
        reader = SafetensorsReader(self.sample())
        tensor = reader.tensor("w.i32")
        view = tensor.raw()
        reader.close()
        reader.close()
        self.assertTrue(reader.closed)
        self.assertEqual(bytes(view), struct.pack("<2i", -7, 2**31 - 1))
        view.release()
        with self.assertRaises(ValueError):
            tensor.raw()
        with self.assertRaises(ValueError):
            reader.tensor("w.i32")
        with SafetensorsReader(self.sample()) as other:
            self.assertFalse(other.closed)
        self.assertTrue(other.closed)


class TestShardedSafetensors(_TempDir):
    def make_index(self, weight_map, metadata=None):
        index = {"weight_map": weight_map}
        if metadata is not None:
            index["metadata"] = metadata
        return self.write("model.safetensors.index.json", json.dumps(index).encode())

    def shards(self):
        self.write("model-00001-of-00002.safetensors", safetensors_bytes(
            [("a", "F32", (2,), struct.pack("<2f", 1, 2))]))
        self.write("model-00002-of-00002.safetensors", safetensors_bytes(
            [("b", "I32", (1,), struct.pack("<i", 5)), ("c", "F16", (1,), _f16([0.5]))]))
        return self.make_index({
            "a": "model-00001-of-00002.safetensors",
            "b": "model-00002-of-00002.safetensors",
            "c": "model-00002-of-00002.safetensors",
        }, metadata={"total_size": 18})

    def test_reads_tensors_across_shards(self):
        reader = self.open(open_weights(self.shards()))
        self.assertIsInstance(reader, ShardedSafetensors)
        self.assertEqual(reader.format, "safetensors")
        self.assertEqual(reader.names(), ["a", "b", "c"])
        self.assertEqual(len(reader), 3)
        self.assertIn("b", reader)
        self.assertEqual(reader.metadata, {"total_size": 18})
        self.assertEqual(reader.tensor("a").values(), [1.0, 2.0])
        self.assertEqual(reader.tensor("b").values(), [5])
        self.assertEqual(reader.tensor("c").values(), [0.5])
        self.assertEqual(reader.parameter_count, 4)
        summary = reader.summary()
        json.dumps(summary)
        self.assertEqual((summary["shards"], summary["tensors"]), (2, 3))
        self.assertEqual(len(reader.shard_paths), 2)
        with self.assertRaises(KeyError):
            reader.tensor("missing")

    def test_close_closes_every_shard(self):
        reader = ShardedSafetensors(self.shards())
        tensor = reader.tensor("a")
        reader.close()
        reader.close()
        self.assertTrue(reader.closed)
        with self.assertRaises(ValueError):
            tensor.raw()
        with self.assertRaises(ValueError):
            reader.tensor("b")

    def test_unsafe_or_missing_shards_are_rejected(self):
        self.shards()
        for filename in ("../model-00001-of-00002.safetensors", "sub/../../x.safetensors",
                         os.path.join(self.directory, "model-00001-of-00002.safetensors"),
                         "..\\x.safetensors", "", 3, "absent.safetensors"):
            with self.subTest(filename=filename):
                with self.assertRaises(WeightsFormatError):
                    ShardedSafetensors(self.make_index({"a": filename}))
        with self.assertRaises(WeightsFormatError):
            ShardedSafetensors(self.write("model.safetensors.index.json", b'{"metadata": {}}'))
        with self.assertRaises(WeightsFormatError):
            ShardedSafetensors(self.write("model.safetensors.index.json", b"not json"))

    def test_deep_index_json_and_non_finite_metadata(self):
        self.shards()
        deep = '{"weight_map": ' + "[" * 200000 + "]" * 200000 + "}"
        with self.assertRaises(WeightsFormatError):
            ShardedSafetensors(self.write("model.safetensors.index.json", deep.encode()))
        index = {"metadata": {"x": {"y": float("nan"), "z": [float("inf")]}},
                 "weight_map": {"a": "model-00001-of-00002.safetensors"}}
        path = self.write("model.safetensors.index.json", json.dumps(index).encode())
        summary = self.open(ShardedSafetensors(path)).summary()
        json.dumps(summary, allow_nan=False)
        self.assertEqual(summary["metadata"], {"x": {"y": "NaN", "z": ["Infinity"]}})

    def test_a_tensor_missing_from_its_shard_is_reported(self):
        self.shards()
        reader = self.open(ShardedSafetensors(self.make_index(
            {"z": "model-00001-of-00002.safetensors"})))
        with self.assertRaises(WeightsFormatError):
            reader.tensor("z")


# -- GGUF -------------------------------------------------------------------


class TestGGUF(_TempDir):
    def metadata(self):
        return [
            ("general.architecture", GGUF_STRING, "qwen3"),
            ("general.name", GGUF_STRING, "Tiny"),
            ("u8", GGUF_UINT8, 200),
            ("i8", GGUF_INT8, -100),
            ("u16", GGUF_UINT16, 60000),
            ("i16", GGUF_INT16, -30000),
            ("u32", GGUF_UINT32, 4000000000),
            ("i32", GGUF_INT32, -2000000000),
            ("f32", GGUF_FLOAT32, 0.5),
            ("yes", GGUF_BOOL, True),
            ("u64", GGUF_UINT64, 2**63),
            ("i64", GGUF_INT64, -(2**62)),
            ("f64", GGUF_FLOAT64, 0.1),
            ("ints", GGUF_ARRAY, (GGUF_INT32, [1, -2, 3])),
            ("flags", GGUF_ARRAY, (GGUF_BOOL, [True, False])),
            ("words", GGUF_ARRAY, (GGUF_STRING, ["a", "bc", "ñ"])),
            ("nested", GGUF_ARRAY, (GGUF_ARRAY, [(GGUF_UINT8, [1, 2]), (GGUF_STRING, ["x"])])),
            ("tokenizer.ggml.tokens", GGUF_ARRAY, (GGUF_STRING, [f"t{i}" for i in range(100)])),
        ]

    def sample(self, **options):
        q8 = _q8_0_block(0.5, range(-16, 16)) + _q8_0_block(-2.0, [1] * 32)
        q4 = (_q4_0_block(0.25, range(16), range(15, -1, -1))
              + _q4_0_block(1.0, [8] * 16, [0] * 16))
        tensors = [
            ("token_embd.weight", (3, 2), 0, struct.pack("<6f", 1, 2, 3, 4, 5, 6)),
            ("half", (2,), 1, _f16([1.5, -0.25])),
            ("brain", (2,), 30, _bf16([0.5, -4.0])),
            ("ints", (2,), 26, struct.pack("<2i", 7, -7)),
            ("double", (1,), 28, struct.pack("<d", 0.1)),
            ("q8", (64,), 8, q8),
            ("q4", (32, 2), 2, q4),
            ("k", (256,), 12, b"\x00" * 144),
        ]
        return self.write("model.gguf", gguf_bytes(self.metadata(), tensors, **options))

    def test_metadata_of_every_type(self):
        reader = self.open(GGUFReader(self.sample()))
        self.assertEqual(reader.format, "gguf")
        self.assertEqual(reader.version, 3)
        self.assertEqual(reader.architecture, "qwen3")
        self.assertEqual(reader.name, "Tiny")
        meta = reader.metadata
        self.assertEqual((meta["u8"], meta["i8"], meta["u16"], meta["i16"]), (200, -100, 60000, -30000))
        self.assertEqual((meta["u32"], meta["i32"]), (4000000000, -2000000000))
        self.assertEqual((meta["u64"], meta["i64"]), (2**63, -(2**62)))
        self.assertEqual((meta["f32"], meta["f64"], meta["yes"]), (0.5, 0.1, True))
        self.assertEqual(meta["ints"], [1, -2, 3])
        self.assertEqual(meta["flags"], [True, False])
        self.assertEqual(meta["words"], ["a", "bc", "ñ"])
        self.assertEqual(meta["nested"], [[1, 2], ["x"]])
        self.assertEqual(len(meta["tokenizer.ggml.tokens"]), 100)
        self.assertEqual(reader.metadata_types["u32"], "uint32")
        self.assertEqual(reader.metadata_types["nested"], "array")
        self.assertEqual(reader.alignment, 32)

    def test_shape_is_row_major_and_data_is_aligned(self):
        reader = self.open(GGUFReader(self.sample()))
        embd = reader.tensor("token_embd.weight")
        self.assertEqual(embd.shape, (2, 3))
        self.assertEqual(embd.rows(), [[1, 2, 3], [4, 5, 6]])
        self.assertEqual(reader.data_offset % 32, 0)
        self.assertEqual(reader.tensor("q4").shape, (2, 32))
        self.assertEqual(reader.tensor("half").values(), [1.5, -0.25])
        self.assertEqual(reader.tensor("brain").values(), [0.5, -4.0])
        self.assertEqual(reader.tensor("ints").values(), [7, -7])
        self.assertEqual(reader.tensor("double").values(), [0.1])

    def test_custom_alignment_and_version_2(self):
        path = self.write("a.gguf", gguf_bytes(
            [("general.alignment", GGUF_UINT32, 64)],
            [("a", (1,), 0, struct.pack("<f", 1)), ("b", (1,), 0, struct.pack("<f", 2))],
            version=2, alignment=64,
        ))
        reader = self.open(GGUFReader(path))
        self.assertEqual((reader.version, reader.alignment), (2, 64))
        self.assertEqual(reader.data_offset % 64, 0)
        self.assertEqual(reader.tensor("b").values(), [2.0])

    def test_q8_0_is_dequantized(self):
        tensor = self.open(GGUFReader(self.sample())).tensor("q8")
        self.assertEqual((tensor.dtype, tensor.nbytes, tensor.element_count), ("Q8_0", 68, 64))
        expected = [q * 0.5 for q in range(-16, 16)] + [-2.0] * 32
        self.assertEqual(tensor.values(), expected)
        self.assertEqual(tensor.to_array().typecode, "f")

    def test_q4_0_is_dequantized(self):
        tensor = self.open(GGUFReader(self.sample())).tensor("q4")
        self.assertEqual((tensor.dtype, tensor.nbytes), ("Q4_0", 36))
        first = [(j - 8) * 0.25 for j in range(16)] + [(15 - j - 8) * 0.25 for j in range(16)]
        second = [0.0] * 16 + [-8.0] * 16
        self.assertEqual(tensor.rows(), [first, second])

    def test_other_quantized_types_are_raw_only(self):
        tensor = self.open(GGUFReader(self.sample())).tensor("k")
        self.assertEqual((tensor.dtype, tensor.nbytes), ("Q4_K", 144))
        with tensor.raw() as view:
            self.assertEqual(len(view), 144)
        with self.assertRaises(NotImplementedError):
            tensor.values()

    def test_summary_bounds_long_arrays(self):
        reader = self.open(GGUFReader(self.sample()))
        summary = reader.summary()
        json.dumps(summary, allow_nan=False)
        self.assertEqual((summary["format"], summary["architecture"], summary["name"]),
                         ("gguf", "qwen3", "Tiny"))
        self.assertEqual(summary["metadata"]["tokenizer.ggml.tokens"],
                         {"type": "array", "item_type": "string", "length": 100})
        self.assertEqual(summary["metadata"]["ints"], [1, -2, 3])
        self.assertEqual(summary["tensors"], 8)
        self.assertEqual(summary["dtypes"]["Q8_0"], 64)
        self.assertEqual(summary["parameters"], reader.parameter_count)

    def test_detection(self):
        path = self.sample()
        self.assertEqual(detect_weights_format(path), "gguf")
        self.assertIsInstance(self.open(open_weights(path)), GGUFReader)

    def test_close_is_idempotent(self):
        reader = GGUFReader(self.sample())
        view = reader.tensor("half").raw()
        reader.close()
        reader.close()
        self.assertEqual(len(view), 4)
        view.release()
        with self.assertRaises(ValueError):
            reader.tensor("half")

    def assert_rejected(self, data, **options):
        path = self.write("bad.gguf", data)
        with self.assertRaises(WeightsFormatError):
            GGUFReader(path, **options).close()

    def test_bad_headers_are_rejected(self):
        good = gguf_bytes([("k", GGUF_UINT8, 1)], [("t", (1,), 0, b"\x00" * 4)])
        self.assert_rejected(b"GGML" + good[4:])
        self.assert_rejected(gguf_bytes(version=1))
        self.assert_rejected(gguf_bytes(version=4))
        self.assert_rejected(b"GGUF" + struct.pack(">IQQ", 3, 0, 0))  # big-endian
        self.assert_rejected(good[:30])  # truncated
        self.assert_rejected(good, max_tensors=0)
        self.assert_rejected(good, max_kv=0)
        self.assert_rejected(b"")

    def test_bad_metadata_is_rejected(self):
        head = b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + _gguf_string("k")
        self.assert_rejected(head + struct.pack("<I", 8) + struct.pack("<Q", 10**6) + b"abc")
        self.assert_rejected(head + struct.pack("<I", 8) + _gguf_string("x" * 50),
                             max_string_bytes=10)
        self.assert_rejected(head + struct.pack("<I", 99) + b"\x00" * 8)
        self.assert_rejected(head + struct.pack("<IB", 7, 2))  # bool that is not 0/1
        self.assert_rejected(head + struct.pack("<IIQ", 9, 4, 10**9))  # array past the end
        self.assert_rejected(gguf_bytes([("a", GGUF_UINT8, 1), ("a", GGUF_UINT8, 2)]))
        self.assert_rejected(gguf_bytes([("general.alignment", GGUF_UINT32, 3)]))
        self.assert_rejected(gguf_bytes([("general.alignment", GGUF_STRING, "32")]))
        self.assert_rejected(gguf_bytes([(b"\xff", GGUF_UINT8, 1)]))
        deep = (GGUF_UINT8, [1])
        for _ in range(9):
            deep = (GGUF_ARRAY, [deep])
        self.assert_rejected(gguf_bytes([("deep", GGUF_ARRAY, deep)]))

    def test_bad_tensors_are_rejected(self):
        f32 = struct.pack("<f", 1)
        self.assert_rejected(gguf_bytes([], [("t", (1,), 0, f32)], offsets=[4096]))  # outside
        self.assert_rejected(gguf_bytes([], [("t", (1,), 0, f32)], offsets=[4]))  # misaligned
        self.assert_rejected(gguf_bytes([], [("t", (1,), 99, f32)]))  # unknown ggml type
        self.assert_rejected(gguf_bytes([], [("t", (16,), 8, b"\x00" * 34)]))  # not a block
        self.assert_rejected(gguf_bytes([], [("t", (2,), 0, f32)]))  # data runs past the end
        self.assert_rejected(gguf_bytes([], [("t", (1,) * 9, 0, f32)]))
        self.assert_rejected(gguf_bytes([], [("t", (2**40, 2**40), 0, f32)]))
        self.assert_rejected(gguf_bytes([], [("t", (1,), 0, f32), ("t", (1,), 0, f32)]))
        self.assert_rejected(gguf_bytes([], [("", (1,), 0, f32)]))
        self.assert_rejected(gguf_bytes(
            [], [("a", (16,), 0, b"\x00" * 64), ("b", (1,), 0, f32)], offsets=[0, 32]))


if __name__ == "__main__":
    unittest.main()

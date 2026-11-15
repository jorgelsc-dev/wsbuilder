import io
import os
import struct
import tempfile
import unittest

from wsbuilder import GGMLType, GGUFError, GGUFReader, GGUFWriter, NeuralNetwork, read_gguf_metadata
from wsbuilder.gguf import GGUFArray, GGUFValueType, decode_tensor, describe_file_type


def _llama_like(vocabulary=2000):
    writer = GGUFWriter(architecture="llama")
    writer.add_metadata("general.name", "tiny-llama")
    writer.add_metadata("general.file_type", 15)  # Q4_K_M
    writer.add_metadata("llama.context_length", 8192)
    writer.add_metadata("llama.embedding_length", 64)
    writer.add_metadata("llama.block_count", 2)
    writer.add_metadata("llama.attention.head_count", 4)
    writer.add_metadata("tokenizer.ggml.model", "llama")
    writer.add_metadata(
        "tokenizer.ggml.tokens", [f"tok{i}" for i in range(vocabulary)], item_type=GGUFValueType.STRING
    )
    writer.add_metadata("tokenizer.ggml.scores", [0.5] * vocabulary, item_type=GGUFValueType.FLOAT32)
    writer.add_metadata("tokenizer.chat_template", "{% for m in messages %}{{ m.content }}{% endfor %}")
    writer.add_tensor("token_embd.weight", [0.0] * 64 * 4, shape=(64, 4))
    writer.add_tensor("output_norm.weight", [1.0] * 64, shape=(64,), ggml_type=GGMLType.F16)
    return writer


class TestRoundTrip(unittest.TestCase):
    def test_metadata_of_every_type_survives(self):
        writer = GGUFWriter(architecture="test")
        values = {
            "u8": (7, GGUFValueType.UINT8),
            "i8": (-7, GGUFValueType.INT8),
            "u16": (60000, GGUFValueType.UINT16),
            "i16": (-30000, GGUFValueType.INT16),
            "u32": (4_000_000_000, GGUFValueType.UINT32),
            "i32": (-2_000_000_000, GGUFValueType.INT32),
            "u64": (2**63, GGUFValueType.UINT64),
            "i64": (-(2**62), GGUFValueType.INT64),
            "f32": (1.5, GGUFValueType.FLOAT32),
            "f64": (0.1, GGUFValueType.FLOAT64),
            "flag": (True, GGUFValueType.BOOL),
            "text": ("hola ñandú 水", GGUFValueType.STRING),
        }
        for key, (value, kind) in values.items():
            writer.add_metadata(key, value, kind)
        writer.add_metadata("ints", [1, -2, 3])
        writer.add_metadata("words", ["a", "b"])
        writer.add_metadata("nested", [[1, 2], [3]], item_type=GGUFValueType.ARRAY)
        reader = GGUFReader(io.BytesIO(writer.to_bytes()))
        for key, (value, kind) in values.items():
            self.assertEqual(reader.metadata[key], value, key)
            self.assertEqual(reader.metadata_types[key], kind)
        self.assertEqual(reader.metadata["ints"], [1, -2, 3])
        self.assertEqual(reader.metadata["words"], ["a", "b"])
        self.assertEqual(reader.metadata["nested"], [[1, 2], [3]])

    def test_tensors_round_trip_in_each_plain_type(self):
        writer = GGUFWriter(architecture="test")
        floats = [0.0, 1.0, -2.5, 3.25, 100.0, -0.125]
        for type_name in ("F32", "F64", "F16", "BF16"):
            writer.add_tensor(f"t.{type_name}", floats, shape=(3, 2), ggml_type=type_name)
        writer.add_tensor("t.I32", [1, -2, 3], shape=(3,), ggml_type=GGMLType.I32)
        writer.add_tensor("t.raw", b"\x01\x02\x03\x04", shape=(4,), ggml_type=GGMLType.I8)
        reader = GGUFReader(io.BytesIO(writer.to_bytes()))
        for type_name in ("F32", "F64", "F16", "BF16"):
            tensor = reader.tensor(f"t.{type_name}")
            self.assertEqual(tensor.shape, (3, 2))
            self.assertEqual(tensor.type_name, type_name)
            self.assertEqual(tensor.values(), floats)
        self.assertEqual(reader.tensor("t.I32").values(), [1, -2, 3])
        self.assertEqual(reader.tensor("t.raw").read(), b"\x01\x02\x03\x04")
        self.assertEqual(reader.parameter_count, 6 * 4 + 3 + 4)

    def test_tensor_data_is_aligned(self):
        writer = GGUFWriter(architecture="test", alignment=64)
        writer.add_tensor("a", [1.0] * 3, shape=(3,))
        writer.add_tensor("b", [2.0] * 5, shape=(5,))
        reader = GGUFReader(io.BytesIO(writer.to_bytes()))
        self.assertEqual(reader.alignment, 64)
        for tensor in reader.tensors:
            self.assertEqual(tensor.offset % 64, 0)
        self.assertEqual(reader.tensor("b").values(), [2.0] * 5)

    def test_quantized_blocks_dequantize(self):
        # One Q8_0 block: scale 0.5 (float16), then 32 signed bytes.
        q8 = struct.pack("<e", 0.5) + struct.pack("<32b", *range(-16, 16))
        self.assertEqual(decode_tensor(q8, GGMLType.Q8_0, 32), [0.5 * q for q in range(-16, 16)])
        # One Q4_0 block: scale 2.0, low nibbles are elements 0..15, high 16..31.
        q4 = struct.pack("<e", 2.0) + bytes((low | (high << 4)) for low, high in zip(range(16), range(16)))
        expected = [2.0 * (q - 8) for q in range(16)] * 2
        self.assertEqual(decode_tensor(q4, GGMLType.Q4_0, 32), expected)

    def test_k_quants_are_sized_but_not_decoded(self):
        writer = GGUFWriter(architecture="test")
        writer.add_tensor("k", b"\x00" * 144, shape=(256,), ggml_type="Q4_K")
        tensor = GGUFReader(io.BytesIO(writer.to_bytes())).tensor("k")
        self.assertEqual(tensor.nbytes, 144)
        with self.assertRaises(GGUFError):
            tensor.values()

    def test_writing_to_a_path_is_atomic_and_readable(self):
        path = os.path.join(tempfile.mkdtemp(), "model.gguf")
        _llama_like().write(path)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["model.gguf"])
        self.assertEqual(read_gguf_metadata(path)["general.name"], "tiny-llama")


class TestSummary(unittest.TestCase):
    def test_summary_of_a_language_model(self):
        reader = GGUFReader(io.BytesIO(_llama_like().to_bytes()))
        summary = reader.summary()
        self.assertEqual(summary["architecture"], "llama")
        self.assertEqual(summary["name"], "tiny-llama")
        self.assertEqual(summary["quantization"], "Q4_K_M")
        self.assertEqual(summary["context_length"], 8192)
        self.assertEqual(summary["block_count"], 2)
        self.assertEqual(summary["vocabulary_size"], 2000)
        self.assertEqual(summary["parameters"], 64 * 4 + 64)
        self.assertIn("messages", summary["chat_template"])

    def test_long_arrays_are_skipped_but_counted(self):
        reader = GGUFReader(io.BytesIO(_llama_like(5000).to_bytes()))
        tokens = reader.metadata["tokenizer.ggml.tokens"]
        self.assertIsInstance(tokens, GGUFArray)
        self.assertEqual(len(tokens), 5000)
        self.assertIsInstance(reader.metadata["tokenizer.ggml.scores"], GGUFArray)
        full = GGUFReader(io.BytesIO(_llama_like(5000).to_bytes()), max_array_items=None)
        self.assertEqual(full.metadata["tokenizer.ggml.tokens"][4999], "tok4999")

    def test_file_type_names(self):
        self.assertEqual(describe_file_type(15), "Q4_K_M")
        self.assertEqual(describe_file_type(7), "Q8_0")
        self.assertIsNone(describe_file_type(None))


class TestHostileFiles(unittest.TestCase):
    def assertRefused(self, data):
        with self.assertRaises(GGUFError):
            GGUFReader(io.BytesIO(data))

    def test_bad_magic_and_version(self):
        self.assertRefused(b"GGML" + b"\x00" * 20)
        self.assertRefused(b"GGUF" + struct.pack("<I", 99) + b"\x00" * 16)

    def test_every_truncation_is_refused(self):
        data = _llama_like(10).to_bytes()
        header_end = GGUFReader(io.BytesIO(data)).data_offset
        for cut in range(0, header_end, 7):
            with self.subTest(cut=cut):
                self.assertRefused(data[:cut])

    def test_counts_beyond_the_file_are_refused(self):
        self.assertRefused(b"GGUF" + struct.pack("<IQQ", 3, 0, 2**60))
        self.assertRefused(b"GGUF" + struct.pack("<IQQ", 3, 2**60, 0))

    def test_a_string_longer_than_the_file_is_refused(self):
        data = b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2**40) + b"key"
        self.assertRefused(data)

    def test_tensor_data_past_the_end_is_refused(self):
        writer = GGUFWriter(architecture="test")
        writer.add_tensor("t", [1.0] * 64, shape=(64,))
        data = writer.to_bytes()
        self.assertRefused(data[:-16])

    def test_a_shape_that_does_not_fit_the_block_size(self):
        with self.assertRaises(GGUFError):
            GGMLType.nbytes(GGMLType.Q4_0, 33)

    def test_writer_validation(self):
        writer = GGUFWriter(architecture="test")
        writer.add_tensor("t", [1.0], shape=(1,))
        with self.assertRaises(GGUFError):
            writer.add_tensor("t", [1.0], shape=(1,))
        with self.assertRaises(GGUFError):
            writer.add_tensor("u", [1.0, 2.0], shape=(3,))
        with self.assertRaises(GGUFError):
            writer.add_metadata("bad", object())
        with self.assertRaises(GGUFError):
            GGUFWriter(alignment=3)


class TestNeuralNetworkFiles(unittest.TestCase):
    def _trained(self):
        network = NeuralNetwork(seed=3, task="classification", learning_rate=0.5)
        network.add_dense(6, input_size=2, activation="tanh")
        network.add_dense(1, activation="sigmoid")
        network.fit_classification(
            [[0, 0], [0, 1], [1, 0], [1, 1]], ["no", "yes", "yes", "no"], epochs=300
        )
        return network

    def test_save_and_load_give_identical_predictions(self):
        network = self._trained()
        path = network.save(os.path.join(tempfile.mkdtemp(), "xor.gguf"), name="xor")
        loaded = NeuralNetwork.load(path)
        for sample in ([0, 0], [0, 1], [1, 0], [1, 1]):
            self.assertEqual(loaded.predict(sample), network.predict(sample))
            self.assertEqual(loaded.predict_class(sample), network.predict_class(sample))
        self.assertEqual((loaded.loss, loaded.task), (network.loss, network.task))
        self.assertEqual(read_gguf_metadata(path)["general.name"], "xor")

    def test_smaller_float_types_round_the_weights(self):
        network = self._trained()
        path = network.save(os.path.join(tempfile.mkdtemp(), "xor16.gguf"), dtype="F16")
        loaded = NeuralNetwork.load(path)
        self.assertAlmostEqual(loaded.predict([1, 0])[0], network.predict([1, 0])[0], places=2)

    def test_other_architectures_are_refused(self):
        path = os.path.join(tempfile.mkdtemp(), "llama.gguf")
        _llama_like(5).write(path)
        with self.assertRaises(ValueError):
            NeuralNetwork.load(path)

    def test_an_empty_network_cannot_be_saved(self):
        with self.assertRaises(ValueError):
            NeuralNetwork().save(os.path.join(tempfile.mkdtemp(), "empty.gguf"))


if __name__ == "__main__":
    unittest.main()

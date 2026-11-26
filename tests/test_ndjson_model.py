import base64
import json
import math
import os
import random
import struct
import tempfile
import unittest
from unittest import mock

from wsbuilder import (
    MODEL_FORMAT,
    ModelFormatError,
    ModelReader,
    ModelWriter,
    NeuralNetwork,
    is_model_file,
    read_model_header,
)


def _write(data, name="model.ndjson"):
    path = os.path.join(tempfile.mkdtemp(), name)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def _lines(*objects):
    return "".join(json.dumps(item) + "\n" for item in objects)


def _header(count, **extra):
    return {"format": MODEL_FORMAT, "version": 1, "metadata": {}, "tensor_count": count, **extra}


class TestRoundTrip(unittest.TestCase):
    def test_model_line_and_one_line_per_tensor(self):
        writer = ModelWriter(architecture="demo", name="demo-model")
        writer.add_metadata("context_length", 128).add_metadata("labels", ["a", "b"])
        writer.add_tensor("w", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        writer.add_tensor("b", [0.5, -0.5], dtype="F32")
        writer.add_tensor("ids", [1, 2, 3], dtype="I32")
        lines = writer.to_bytes().decode().splitlines()
        self.assertEqual(len(lines), 4)
        header = json.loads(lines[0])
        self.assertEqual(header["format"], MODEL_FORMAT)
        self.assertEqual(header["tensor_count"], 3)
        self.assertEqual(header["metadata"], {"context_length": 128, "labels": ["a", "b"]})
        self.assertEqual(json.loads(lines[1]),
                         {"tensor": "w", "shape": [2, 3], "dtype": "F64",
                          "data": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})

        path = writer.write(os.path.join(tempfile.mkdtemp(), "sub", "demo.ndjson"))
        with ModelReader(path) as reader:
            self.assertEqual((reader.architecture, reader.name), ("demo", "demo-model"))
            self.assertEqual(reader.metadata["context_length"], 128)
            self.assertEqual([t.name for t in reader.tensors], ["w", "b", "ids"])
            self.assertEqual(reader.tensor("w").shape, (2, 3))
            self.assertEqual(reader.tensor("w").rows(), [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
            self.assertEqual(reader.tensor("ids").values(), [1, 2, 3])
            self.assertIn("b", reader)
            summary = reader.summary()
        self.assertEqual(summary["format"], "ndjson")
        self.assertEqual(summary["parameters"], 11)
        self.assertEqual(summary["dtypes"], {"F64": 6, "F32": 2, "I32": 3})
        self.assertEqual(read_model_header(path)["name"], "demo-model")
        self.assertTrue(is_model_file(path))
        self.assertEqual(os.listdir(os.path.dirname(path)), ["demo.ndjson"])

    def test_smaller_floats_are_rounded(self):
        writer = ModelWriter()
        writer.add_tensor("f32", [0.1], dtype="F32").add_tensor("f16", [0.1], dtype="F16")
        writer.add_tensor("f64", [0.1])
        with ModelReader(_write(writer.to_bytes())) as reader:
            self.assertNotEqual(reader.tensor("f32").values(), [0.1])
            self.assertAlmostEqual(reader.tensor("f32").values()[0], 0.1, places=7)
            self.assertAlmostEqual(reader.tensor("f16").values()[0], 0.1, places=3)
            self.assertEqual(reader.tensor("f64").values(), [0.1])

    def test_non_finite_values_survive(self):
        writer = ModelWriter().add_tensor("x", [math.nan, math.inf, -math.inf, 1e6], dtype="F16")
        text = writer.to_bytes().decode()
        self.assertIn('"NaN"', text)
        with ModelReader(_write(text)) as reader:
            nan, pos, neg, big = reader.tensor("x").values()
        self.assertTrue(math.isnan(nan))
        self.assertEqual((pos, neg, big), (math.inf, -math.inf, math.inf))

    def test_writer_validation(self):
        writer = ModelWriter()
        with self.assertRaises(ModelFormatError):
            writer.add_tensor("x", [1, 2, 3], shape=(2, 2))
        with self.assertRaises(ModelFormatError):
            writer.add_tensor("x", [1], dtype="Q4_K")
        with self.assertRaises(ModelFormatError):
            writer.add_tensor("x", [1.5], dtype="I32")
        with self.assertRaises(ModelFormatError):
            writer.add_tensor("x", [2**40], dtype="I32")
        with self.assertRaises(ModelFormatError):
            writer.add_metadata("bad", object())
        writer.add_tensor("x", [1])
        with self.assertRaises(ModelFormatError):
            writer.add_tensor("x", [1])


class TestHostileFiles(unittest.TestCase):
    def assertRefused(self, text):
        path = _write(text)
        with self.assertRaises(ModelFormatError):
            ModelReader(path).close()
        return path

    def test_not_a_model(self):
        for text in ("", "\n", "not json\n", "[1, 2]\n", _lines({"format": "other", "version": 1})):
            with self.subTest(text=text):
                path = self.assertRefused(text)
                self.assertFalse(is_model_file(path))

    def test_versions_and_counts(self):
        self.assertRefused(_lines(_header(0, version=99)))
        self.assertRefused(_lines(_header(-1)))
        self.assertRefused(_lines(_header(2), {"tensor": "a", "shape": [1], "data": [1]}))
        self.assertRefused(_lines(_header(0), {"tensor": "a", "shape": [1], "data": [1]}))

    def test_bad_tensors(self):
        bad = [
            {"shape": [1], "data": [1]},
            {"tensor": "a", "shape": [2], "data": [1]},
            {"tensor": "a", "shape": [-1], "data": []},
            {"tensor": "a", "shape": [1], "dtype": "Q8_0", "data": [1]},
            {"tensor": "a", "shape": [1], "data": ["x"]},
            {"tensor": "a", "shape": [1], "data": [True]},
            {"tensor": "a", "shape": [1], "dtype": "I32", "data": [0.5]},
            {"tensor": "a", "shape": [1], "data": 1},
        ]
        for entry in bad:
            with self.subTest(entry=entry):
                self.assertRefused(_lines(_header(1), entry))

    def test_duplicate_names_and_truncation(self):
        entry = {"tensor": "a", "shape": [1], "data": [1]}
        self.assertRefused(_lines(_header(2), entry, entry))
        whole = ModelWriter().add_tensor("a", [1.0] * 10).to_bytes()
        self.assertRefused(whole[:-5])

    def test_blank_lines_are_ignored(self):
        text = _lines(_header(1)) + "\n" + _lines({"tensor": "a", "shape": [1], "data": [2]})
        with ModelReader(_write(text)) as reader:
            self.assertEqual(reader.tensor("a").values(), [2.0])


def _q8_0(scale, quants):
    return struct.pack("<e", scale) + struct.pack("<32b", *quants)


def _q4_0(scale, nibbles):
    return struct.pack("<e", scale) + bytes(lo | (hi << 4) for lo, hi in
                                             zip(nibbles[:16], nibbles[16:]))


class TestBase64Tensors(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name

    def read(self, writer):
        path = writer.write(os.path.join(self.directory, f"m{random.random()}.ndjson"))
        reader = ModelReader(path)
        self.addCleanup(reader.close)
        return reader

    def test_values_round_trip_in_every_float_and_int_dtype(self):
        values = [0.1, -2.5, 3.0, 1e-3, 65504.0, -0.0]
        writer = ModelWriter(architecture="demo")
        for dtype in ("F64", "F32", "F16", "BF16"):
            writer.add_tensor(dtype, values, shape=(2, 3), dtype=dtype, encoding="base64")
            writer.add_tensor(dtype + "-json", values, shape=(2, 3), dtype=dtype)
        writer.add_tensor("I32", [1, -2, 2**31 - 1], dtype="I32", encoding="base64")
        writer.add_tensor("I64", [2**40, -1], dtype="I64", encoding="base64")
        reader = self.read(writer)
        self.assertEqual(reader.version, 2)
        for dtype in ("F64", "F32", "F16", "BF16"):
            with self.subTest(dtype=dtype):
                tensor = reader.tensor(dtype)
                self.assertEqual((tensor.encoding, tensor.dtype), ("base64", dtype))
                self.assertEqual(tensor.values(), reader.tensor(dtype + "-json").values())
                self.assertEqual(tensor.rows(), [tensor.values()[:3], tensor.values()[3:]])
                self.assertEqual(tensor.to_array().typecode, "d" if dtype == "F64" else "f")
                self.assertEqual(tensor.raw(), reader.tensor(dtype + "-json").raw())
        self.assertEqual(reader.tensor("F64").values(), values)
        self.assertEqual(reader.tensor("BF16").values()[0], 0.10009765625)
        self.assertEqual(reader.tensor("I32").values(), [1, -2, 2**31 - 1])
        self.assertEqual(reader.tensor("I64").values(), [2**40, -1])
        self.assertEqual(reader.summary()["dtypes"]["BF16"], 12)

    def test_quantized_blocks_and_row_reads(self):
        rng = random.Random(5)
        rows = 7
        q8 = b"".join(_q8_0(0.5, [rng.randint(-127, 127) for _ in range(32)])
                      for _ in range(rows * 2))
        q4 = b"".join(_q4_0(0.25, [rng.randint(0, 15) for _ in range(32)]) for _ in range(rows))
        writer = ModelWriter()
        writer.add_tensor_bytes("q8", q8, shape=(rows, 64), dtype="Q8_0")
        writer.add_tensor_bytes("q4", lambda: q4, shape=(rows, 32), dtype="Q4_0")
        widths = (1, 2, 3, 5)
        for width in widths:
            writer.add_tensor(f"f32-{width}", [rng.uniform(-1, 1) for _ in range(rows * width)],
                              shape=(rows, width), dtype="F32", encoding="base64")
            writer.add_tensor(f"bf16-{width}", [rng.uniform(-1, 1) for _ in range(rows * width)],
                              shape=(rows, width), dtype="BF16", encoding="base64")
        reader = self.read(writer)
        q8_values = reader.tensor("q8").values()
        first = struct.unpack("<32b", q8[2:34])
        self.assertEqual(q8_values[:32], [0.5 * quant for quant in first])
        q4_values = reader.tensor("q4").values()
        self.assertEqual(q4_values[0], 0.25 * ((q4[2] & 0x0F) - 8))
        self.assertEqual(q4_values[16], 0.25 * ((q4[2] >> 4) - 8))
        self.assertEqual(reader.tensor("q4").raw(), q4)
        for name in ["q8", "q4"] + [f"{kind}-{w}" for kind in ("f32", "bf16") for w in widths]:
            tensor = reader.tensor(name)
            self.assertTrue(tensor.row_reads)
            flat = tensor.values()
            width = tensor.shape[1]
            for row in range(rows):
                self.assertEqual(tensor.row_array(row).tolist(),
                                 flat[row * width:(row + 1) * width], (name, row))
            with self.assertRaises(IndexError):
                tensor.row_array(rows)

    def test_chunked_checks_reads_and_writes(self):
        rng = random.Random(9)
        values = [rng.uniform(-1, 1) for _ in range(11 * 5)]
        with mock.patch("wsbuilder.ndjson_model._CHECK_CHUNK", 7), \
                mock.patch("wsbuilder.ndjson_model._B64_CHUNK", 6):
            writer = ModelWriter().add_tensor("t", values, shape=(11, 5), dtype="F32",
                                              encoding="base64")
            text = writer.to_bytes()
            reader = self.read(writer)
            tensor = reader.tensor("t")
            rows = [row.tolist() for row in tensor.row_arrays()]
        whole = ModelWriter().add_tensor("t", values, shape=(11, 5), dtype="F32",
                                         encoding="base64").to_bytes()
        self.assertEqual(text, whole)
        self.assertEqual(rows, tensor.rows())
        self.assertEqual(len(rows), 11)
        json_tensor = self.read(ModelWriter().add_tensor("j", values, shape=(11, 5)))
        self.assertEqual([r.tolist() for r in json_tensor.tensor("j").row_arrays()],
                         json_tensor.tensor("j").rows())

    def test_version_one_files_stay_version_one(self):
        reader = self.read(ModelWriter().add_tensor("a", [1.0, 2.0], dtype="F32"))
        self.assertEqual(reader.version, 1)
        self.assertFalse(reader.tensor("a").row_reads)
        with self.assertRaises(ModelFormatError):
            reader.tensor("a").row_array(0)  # 1-D
        self.assertEqual(reader.tensor("a").to_array().tolist(), [1.0, 2.0])
        self.assertEqual(self.read(ModelWriter().add_tensor("b", [1.0], dtype="BF16")).version, 2)

    def test_non_canonical_base64_lines_are_read(self):
        data = base64.b64encode(struct.pack("<3f", 1.0, 2.0, 3.0)).decode()
        entry = {"data": data, "tensor": "a", "encoding": "base64", "shape": [1, 3],
                 "dtype": "F32"}
        path = _write(_lines(_header(1, version=2), entry))
        with ModelReader(path) as reader:
            tensor = reader.tensor("a")
            self.assertFalse(tensor.row_reads)
            self.assertEqual(tensor.values(), [1.0, 2.0, 3.0])
            self.assertEqual(tensor.row_array(0).tolist(), [1.0, 2.0, 3.0])
        spaced = _lines(_header(1, version=2)) + json.dumps(dict(entry, data=None)).replace(
            '"data": null, ', "") [:-1] + f', "data": "{data}"}}\n'
        with ModelReader(_write(spaced)) as reader:
            self.assertEqual(reader.tensor("a").values(), [1.0, 2.0, 3.0])

    def test_bad_base64_tensors_are_refused(self):
        good = base64.b64encode(struct.pack("<2f", 1.0, 2.0)).decode()
        bad = [
            {"tensor": "a", "shape": [2], "dtype": "F32", "encoding": "base64", "data": good[:-4]},
            {"tensor": "a", "shape": [2], "dtype": "F32", "encoding": "base64",
             "data": "*" + good[1:]},
            {"tensor": "a", "shape": [1], "dtype": "F16", "encoding": "base64", "data": "AA=="},
            {"tensor": "a", "shape": [1], "dtype": "F16", "encoding": "base64", "data": "AA=A"},
            {"tensor": "a", "shape": [2], "dtype": "F32", "encoding": "hex", "data": good},
            {"tensor": "a", "shape": [2], "dtype": "F32", "encoding": "base64", "data": [1, 2]},
            {"tensor": "a", "shape": [16], "dtype": "Q8_0", "encoding": "base64", "data": ""},
            {"tensor": "a", "shape": [32], "dtype": "Q8_0", "data": [0] * 32},
        ]
        for entry in bad:
            with self.subTest(entry=entry):
                with self.assertRaises(ModelFormatError):
                    ModelReader(_write(_lines(_header(1, version=2), entry))).close()

    def test_writer_checks_bytes(self):
        writer = ModelWriter()
        with self.assertRaises(ModelFormatError):
            writer.add_tensor_bytes("a", b"\x00" * 7, shape=(2,), dtype="F32")
        with self.assertRaises(ModelFormatError):
            writer.add_tensor_bytes("a", b"\x00" * 34, shape=(16,), dtype="Q8_0")
        with self.assertRaises(ModelFormatError):
            writer.add_tensor("a", [1.0] * 32, dtype="Q8_0", encoding="base64")
        with self.assertRaises(ModelFormatError):
            writer.add_tensor("a", [1.0], encoding="hex")
        writer.add_tensor_bytes("lazy", lambda: b"\x00" * 4, shape=(2,), dtype="F32")
        with self.assertRaises(ModelFormatError):
            writer.to_bytes()
        target = os.path.join(self.directory, "never.ndjson")
        with self.assertRaises(ModelFormatError):
            writer.write(target)
        self.assertEqual(os.listdir(self.directory), [])

    def test_closed_reader(self):
        reader = self.read(ModelWriter().add_tensor("a", [1.0], dtype="F32", encoding="base64"))
        reader.close()
        self.assertTrue(reader.closed)
        with self.assertRaises(ModelFormatError):
            reader.tensor("a").values()


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
        path = network.save(os.path.join(tempfile.mkdtemp(), "xor.ndjson"), name="xor")
        loaded = NeuralNetwork.load(path)
        for sample in ([0, 0], [0, 1], [1, 0], [1, 1]):
            self.assertEqual(loaded.predict(sample), network.predict(sample))
            self.assertEqual(loaded.predict_class(sample), network.predict_class(sample))
        self.assertEqual((loaded.loss, loaded.task), (network.loss, network.task))
        header = read_model_header(path)
        self.assertEqual((header["name"], header["architecture"]), ("xor", "wsbuilder.dense"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(len(handle.read().splitlines()), 1 + 2 * 2)

    def test_smaller_float_types_round_the_weights(self):
        network = self._trained()
        path = network.save(os.path.join(tempfile.mkdtemp(), "xor16.ndjson"), dtype="F16")
        loaded = NeuralNetwork.load(path)
        self.assertAlmostEqual(loaded.predict([1, 0])[0], network.predict([1, 0])[0], places=2)

    def test_other_architectures_are_refused(self):
        path = ModelWriter(architecture="llama").add_tensor("x", [1.0]).write(
            os.path.join(tempfile.mkdtemp(), "llama.ndjson")
        )
        with self.assertRaises(ValueError):
            NeuralNetwork.load(path)

    def test_an_empty_network_cannot_be_saved(self):
        with self.assertRaises(ValueError):
            NeuralNetwork().save(os.path.join(tempfile.mkdtemp(), "empty.ndjson"))


if __name__ == "__main__":
    unittest.main()

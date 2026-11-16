import json
import math
import os
import tempfile
import unittest

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

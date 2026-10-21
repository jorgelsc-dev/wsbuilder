import io
import json
import math
import os
import subprocess
import sys
import unittest
import wave

from wsbuilder import (
    AudioData,
    ByteTokenizer,
    DataSet,
    HashingTextVectorizer,
    NeuralNetwork,
    audio_features,
    load_wav,
)


def pcm_wav(values, width=2, channels=1, rate=8000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(b"".join(value.to_bytes(width, "little", signed=width != 1)
                                   for value in values))
    buffer.seek(0)
    return buffer


class TestTextFeatures(unittest.TestCase):
    def test_byte_tokens_roundtrip_without_normalizing(self):
        tokenizer = ByteTokenizer()
        for text in ("", "Español", "مرحبا", "中文", "🙂", "e\u0301", "x = 3\n\t# café"):
            with self.subTest(text=text):
                tokens = tokenizer.encode(text)
                self.assertEqual(tokens, list(text.encode("utf-8")))
                self.assertEqual(tokenizer.decode(iter(tokens)), text)
        self.assertEqual(tokenizer.vocab_size, 256)

    def test_tokens_reject_invalid_values_and_utf8(self):
        tokenizer = ByteTokenizer(max_bytes=4)
        for tokens in ([True], [1.0], [-1], [256], ["1"], [0] * 5):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                tokenizer.decode(tokens)
        with self.assertRaises(UnicodeDecodeError):
            tokenizer.decode([255])
        with self.assertRaises(UnicodeEncodeError):
            tokenizer.encode("\ud800")
        with self.assertRaises(TypeError):
            tokenizer.encode(b"x")
        with self.assertRaises(ValueError):
            tokenizer.encode("🙂x")
        self.assertEqual(tokenizer.decode(tokenizer.encode("🙂")), "🙂")
        for limit in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                ByteTokenizer(max_bytes=limit)

    def test_fixed_features_multilingual_normalization_and_empty(self):
        vectorizer = HashingTextVectorizer(32)
        for text in ("Español", "مرحبا", "中文", "🙂", "x = 3\n"):
            vector = vectorizer.transform(text)
            self.assertEqual(len(vector), 32)
            self.assertAlmostEqual(sum(value * value for value in vector), 1)
        self.assertEqual(vectorizer.transform(""), [0.0] * 32)
        self.assertEqual(vectorizer.transform("ÉCOLE"), vectorizer.transform("e\u0301cole"))
        self.assertEqual(vectorizer.transform("STRASSE"), vectorizer.transform("Straße"))
        counts = HashingTextVectorizer(32, normalize=False, ngram_range=(1, 2))
        self.assertEqual(sum(counts.transform("abc")), 5)
        self.assertNotEqual(HashingTextVectorizer(lowercase=False).transform("A"),
                            HashingTextVectorizer(lowercase=False).transform("a"))

    def test_hashes_are_stable_across_processes(self):
        text = "café 中文 🙂"
        script = ("import json; from wsbuilder import HashingTextVectorizer; "
                  f"print(json.dumps(HashingTextVectorizer(16).transform({text!r})))")
        environment = dict(os.environ, PYTHONHASHSEED="23")
        result = subprocess.run([sys.executable, "-c", script], env=environment,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), HashingTextVectorizer(16).transform(text))

    def test_vectorizer_bounds_and_batch(self):
        for options in ({"n_features": 0}, {"n_features": 65537},
                        {"ngram_range": (0, 2)}, {"ngram_range": (2, 1)},
                        {"ngram_range": (1, 9)}, {"ngram_range": (True, 2)},
                        {"max_chars": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                HashingTextVectorizer(**options)
        vectorizer = HashingTextVectorizer(max_chars=1)
        for text in ("ab", "ß"):
            with self.assertRaises(ValueError):
                vectorizer.transform(text)
        with self.assertRaises(TypeError):
            vectorizer.transform_batch("abc")
        with self.assertRaises(TypeError):
            vectorizer.transform(None)
        self.assertEqual(vectorizer.transform_batch(["a", "b"]),
                         [vectorizer.transform("a"), vectorizer.transform("b")])


class TestAudioFeatures(unittest.TestCase):
    def test_pcm_widths_extremes_and_file_ownership(self):
        for width in (1, 2, 3, 4):
            scale = 1 << (width * 8 - 1)
            values = [0, 128, 255] if width == 1 else [-scale, 0, scale - 1]
            with self.subTest(width=width):
                source = pcm_wav(values, width)
                audio = load_wav(source)
                self.assertFalse(source.closed)
                self.assertEqual(audio.samples, (-1.0, 0.0, (scale - 1) / scale))
                self.assertEqual(audio.sample_rate, 8000)
                self.assertEqual(audio.duration, 3 / 8000)

    def test_stereo_downmix_and_silence(self):
        audio = load_wav(pcm_wav([12000, -12000, 0, 0], channels=2))
        self.assertEqual(audio.samples, (0.0, 0.0))
        self.assertEqual(audio_features(audio), [2 / 8000, 0.0, 0.0, 0.0, 0.0])
        empty = load_wav(pcm_wav([]))
        self.assertEqual(audio_features(empty), [0.0] * 5)

    def test_audio_features_have_known_values(self):
        features = audio_features(AudioData((-1.0, 1.0, -1.0, 1.0), 4))
        self.assertEqual(features, [1.0, 0.0, 1.0, 1.0, 1.0])
        self.assertEqual(audio_features(AudioData((0.5,), 1)), [1, 0.5, 0.5, 0.5, 0])

    def test_invalid_audio_and_resource_limits(self):
        for samples in ((float("nan"),), (float("inf"),), (1.1,), (True,), ("x",)):
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                AudioData(samples, 8000)
        with self.assertRaises(TypeError):
            AudioData([0.0], 8000)
        for rate in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                AudioData((), rate)
        with self.assertRaises(TypeError):
            audio_features([0.0])
        with self.assertRaises(ValueError):
            load_wav(pcm_wav([0, 1]), max_frames=1)
        with self.assertRaises(ValueError):
            load_wav(pcm_wav([0, 1], channels=2), max_channels=1)
        with self.assertRaises(ValueError):
            load_wav(pcm_wav([]), max_frames=True)
        truncated = pcm_wav([0, 1]).getvalue()[:-1]
        with self.assertRaises(ValueError):
            load_wav(io.BytesIO(truncated))
        incomplete = bytearray(pcm_wav([0, 1]).getvalue())
        incomplete[40:44] = (3).to_bytes(4, "little")
        with self.assertRaises(ValueError):
            load_wav(io.BytesIO(incomplete))
        with self.assertRaises((wave.Error, EOFError)):
            load_wav(io.BytesIO(b"not a wav"))

    def test_feature_vectors_integrate_with_dataset_and_network(self):
        text_features = HashingTextVectorizer(5).transform_batch(["sí", "no"])
        audio_rows = [audio_features(AudioData((0.0,), 4)),
                      audio_features(AudioData((-1.0, 1.0), 4))]
        for rows in (text_features, audio_rows):
            dataset = DataSet(rows, [[0], [1]])
            network = NeuralNetwork(seed=3)
            network.add_dense(1, input_size=dataset.feature_count)
            result = network.fit(dataset, None, epochs=2)
            self.assertEqual(len(result["loss"]), 2)
            self.assertTrue(math.isfinite(network.predict(rows[0])[0]))

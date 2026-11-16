import json
import os
import tempfile
import unittest

from wsbuilder import (
    BYTELM_ARCHITECTURE,
    ByteLanguageModel,
    LanguageModel,
    ModelStore,
    NativeBackend,
    TaskManager,
    format_chat,
    read_model_header,
    submit_lm_training_task,
)

CONVERSATIONS = [
    [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "Hola!"}],
    [{"role": "user", "content": "precio"}, {"role": "assistant", "content": "10 euros."}],
]


def _small(**options):
    sizes = {"context": 8, "embedding": 6, "hidden": (24,), "seed": 1}
    sizes.update(options)
    return ByteLanguageModel(**sizes)


_TRAINED = None


def _trained():
    """One model trained on CONVERSATIONS, shared by the tests that only read it."""
    global _TRAINED
    if _TRAINED is None:
        model = ByteLanguageModel(context=16, embedding=8, hidden=(48,), seed=1)
        model.fit_chat(CONVERSATIONS, epochs=80, learning_rate=0.1, decay=0.97, seed=0)
        _TRAINED = model
    return _TRAINED


class TestChatFormat(unittest.TestCase):
    def test_messages_become_role_blocks(self):
        self.assertEqual(format_chat("hola"), "<|user|>hola\n<|assistant|>")
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        self.assertEqual(format_chat(messages, add_generation_prompt=False), "<|system|>s\n<|user|>u")
        with self.assertRaises(ValueError):
            format_chat([{"role": "root", "content": "x"}])


class TestModel(unittest.TestCase):
    def test_sizes_are_validated(self):
        for options in ({"context": 0}, {"embedding": -1}, {"hidden": ()}, {"hidden": (0,)},
                        {"context": 1.5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                _small(**options)
        model = _small(hidden=10)
        self.assertEqual(model.hidden, (10,))
        self.assertEqual(model.parameter_count, 258 * 6 + (48 * 10 + 10) + 258 * 10 + 258)
        self.assertEqual(model.describe()["architecture"], BYTELM_ARCHITECTURE)

    def test_the_same_seed_gives_the_same_model(self):
        self.assertEqual(_small().generate("ab", max_tokens=12, seed=4),
                         _small().generate("ab", max_tokens=12, seed=4))

    def test_probabilities_sum_to_one(self):
        probabilities = _small().next_token_probabilities(list(b"hola"))
        self.assertEqual(len(probabilities), 258)
        self.assertAlmostEqual(sum(probabilities), 1.0, places=9)

    def test_training_lowers_the_loss(self):
        model = _small()
        before = model.perplexity("abcabc")
        history = model.fit(["abcabcabc"] * 3, epochs=6, learning_rate=0.1, seed=0)
        self.assertEqual([entry["epoch"] for entry in history], list(range(1, 7)))
        self.assertLess(history[-1]["loss"], history[0]["loss"])
        self.assertLess(model.perplexity("abcabc"), before)
        self.assertEqual(model.trained_bytes, 3 * 10 * 6)

    def test_training_input_is_validated(self):
        model = _small()
        with self.assertRaises(ValueError):
            model.fit([], epochs=1)
        with self.assertRaises(TypeError):
            model.fit([b"bytes"], epochs=1)
        with self.assertRaises(ValueError):
            model.fit("abc", epochs=0)
        with self.assertRaises(ValueError):
            model.fit("abc", learning_rate=0)
        with self.assertRaises(ValueError):
            model.fit("abc" * 10, max_samples=5)

    def test_a_trained_model_answers_what_it_learnt(self):
        model = _trained()
        self.assertEqual(model.chat("hola", temperature=0), "Hola!")
        self.assertEqual(model.chat("precio", temperature=0), "10 euros.")
        self.assertEqual(model.last_finish_reason, "stop")

    def test_generation_options(self):
        model = _small()
        self.assertEqual(model.generate("x", max_tokens=0), "")
        model.generate("x", max_tokens=5, seed=1)
        self.assertLessEqual(model.last_completion_tokens, 5)
        sampled = model.generate("x", max_tokens=20, temperature=1.5, top_k=5, top_p=0.9,
                                 repeat_penalty=1.3, seed=2)
        self.assertIsInstance(sampled, str)
        greedy = [model.generate("x", max_tokens=10, temperature=0) for _ in range(2)]
        self.assertEqual(greedy[0], greedy[1])
        with self.assertRaises(TypeError):
            model.generate(123)
        with self.assertRaises(ValueError):
            model.generate("x", max_tokens=-1)

    def test_stop_strings_cut_the_text(self):
        model = _trained()
        text = model.generate(format_chat("precio"), temperature=0, stop=["euros"])
        self.assertEqual(text, "10 ")
        self.assertEqual("".join(model.stream(format_chat("precio"), temperature=0, stop="eu")), "10 ")

    def test_streaming_yields_the_same_text(self):
        model = _trained()
        prompt = format_chat("hola")
        self.assertEqual("".join(model.stream(prompt, temperature=0)),
                         model.generate(prompt, temperature=0))

    def test_utf8_is_never_split(self):
        model = _small()
        model.fit(["ñandú ñandú ñandú"], epochs=30, learning_rate=0.2, seed=0)
        pieces = list(model.stream("ñ", max_tokens=40, temperature=0))
        for piece in pieces:
            piece.encode("utf-8")  # no lone surrogates or partial characters
        self.assertNotIn("�", "".join(pieces))


class TestFiles(unittest.TestCase):
    def test_save_and_load_give_the_same_answers(self):
        model = _trained()
        path = model.save(os.path.join(tempfile.mkdtemp(), "bot.ndjson"), name="bot", dtype="F64")
        header = read_model_header(path)
        self.assertEqual((header["architecture"], header["name"]), (BYTELM_ARCHITECTURE, "bot"))
        self.assertEqual(header["metadata"]["context"], 16)
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1 + 1 + 2 + 2)  # model, embeddings, layer, output
        self.assertEqual(json.loads(lines[1])["tensor"], "tok_embd.weight")
        same = ByteLanguageModel.load(path)
        for question in ("hola", "precio"):
            self.assertEqual(same.chat(question, temperature=0), model.chat(question, temperature=0))
        self.assertEqual(same.trained_bytes, model.trained_bytes)

    def test_f32_files_still_answer(self):
        path = _trained().save(os.path.join(tempfile.mkdtemp(), "bot32.ndjson"))
        self.assertEqual(ByteLanguageModel.load(path).chat("hola", temperature=0), "Hola!")

    def test_other_architectures_and_bad_shapes_are_refused(self):
        from wsbuilder import ModelWriter, NeuralNetwork

        directory = tempfile.mkdtemp()
        network = NeuralNetwork()
        network.add_dense(1, input_size=1)
        with self.assertRaises(ValueError):
            ByteLanguageModel.load(network.save(os.path.join(directory, "net.ndjson")))
        writer = ModelWriter(architecture=BYTELM_ARCHITECTURE)
        writer.add_metadata("context", 2).add_metadata("embedding", 2).add_metadata("hidden", [2])
        writer.add_tensor("tok_embd.weight", [0.0] * 4, shape=(2, 2))
        with self.assertRaises(ValueError):
            ByteLanguageModel.load(writer.write(os.path.join(directory, "bad.ndjson")))


class TestBackend(unittest.TestCase):
    def test_language_model_interface(self):
        model = LanguageModel.native(_trained(), name="bot", temperature=0)
        self.assertEqual(model.chat("hola"), "Hola!")
        self.assertEqual("".join(model.stream_chat([{"role": "user", "content": "precio"}])), "10 euros.")
        completion = model.chat_completion("hola")
        self.assertEqual(completion["finish_reason"], "stop")
        self.assertEqual(completion["usage"]["completion_tokens"], len(b"Hola!"))
        self.assertEqual(completion["usage"]["prompt_tokens"], len(format_chat("hola").encode()))
        self.assertIsInstance(model.generate("<|user|>", max_tokens=8), str)
        with self.assertRaises(ValueError):
            model.chat([{"role": "root", "content": "x"}])
        model.close()

    def test_from_file_and_bad_models(self):
        path = _trained().save(os.path.join(tempfile.mkdtemp(), "bot.ndjson"))
        model = LanguageModel.from_file(path, temperature=0)
        self.assertEqual(model.chat("precio"), "10 euros.")
        with self.assertRaises(TypeError):
            NativeBackend(object())


class TestStoreIntegration(unittest.TestCase):
    def test_stored_native_models_run_without_configuration(self):
        directory = tempfile.mkdtemp()
        store = ModelStore(os.path.join(directory, "models"))
        self.addCleanup(store.close)
        record = store.add(_trained(), name="Mi Bot")
        self.assertEqual((record.id, record.format), ("mi-bot", "ndjson"))
        self.assertEqual(record.summary["architecture"], BYTELM_ARCHITECTURE)
        model = store.load(record.id, temperature=0)
        self.assertIs(store.load(record.id), model)
        self.assertEqual(model.chat("hola"), "Hola!")
        self.assertTrue(store.verify(record.id))

    def test_models_that_are_not_language_models_cannot_be_chatted_with(self):
        from wsbuilder import NeuralNetwork

        directory = tempfile.mkdtemp()
        store = ModelStore(os.path.join(directory, "models"))
        network = NeuralNetwork()
        network.add_dense(1, input_size=1)
        record = store.add(network, name="red")
        with self.assertRaises(NotImplementedError):
            store.load(record.id)
        with self.assertRaises(TypeError):
            store.add(object())


class TestTrainingTask(unittest.TestCase):
    def test_background_training_reports_progress(self):
        manager = TaskManager()
        self.addCleanup(manager.close)
        model = _small()
        task = submit_lm_training_task(manager, model, ["abab"], epochs=3, learning_rate=0.1)
        self.assertTrue(task.wait(30))
        self.assertEqual(len(task.result), 3)
        self.assertEqual(task.metadata["epoch"], 3)
        with self.assertRaises(ValueError):
            submit_lm_training_task(manager, model)
        with self.assertRaises(TypeError):
            submit_lm_training_task(None, model, ["x"])


if __name__ == "__main__":
    unittest.main()

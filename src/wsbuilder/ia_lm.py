"""A native byte-level language model, trained and run in pure Python.

:class:`ByteLanguageModel` is wsbuilder's own text generator: no C
extension, no external engine, only the standard library. It reads the last
``context`` bytes of UTF-8 text and predicts the next byte with a small
neural network (an embedding per byte, one or more ``tanh`` layers and a
softmax over the 256 byte values plus an end-of-text token), the classic
neural probabilistic language model.

* :meth:`ByteLanguageModel.fit` trains it on texts (or chat conversations
  through :meth:`fit_chat`) with stochastic gradient descent.
* :meth:`ByteLanguageModel.generate` / :meth:`stream` sample new text with
  ``temperature``, ``top_k``, ``top_p``, ``repeat_penalty``, ``stop`` and
  ``seed``.
* :meth:`ByteLanguageModel.save` / :meth:`load` use the wsbuilder NDJSON
  model format (architecture ``wsbuilder.bytelm``), so the model and its
  weights live in one ``.ndjson`` file a :class:`~wsbuilder.llm.ModelStore`
  can keep, serve and run.
* :class:`NativeBackend` plugs a model into
  :class:`~wsbuilder.llm.LanguageModel`, so it answers ``chat`` and
  ``generate`` exactly like any other backend, including over HTTP.

It is a small model meant for small, focused corpora (commands, FAQs,
domain phrases, short dialogues). Pure Python is slow: training cost grows
with ``context * embedding * hidden`` per byte of text, so keep the sizes
modest and train in a background task (:func:`submit_lm_training_task`).
"""

import codecs
import math
import operator
import random

from .ia import _check_task_cancelled

__all__ = [
    "BYTELM_ARCHITECTURE",
    "ByteLanguageModel",
    "NativeBackend",
    "format_chat",
    "submit_lm_training_task",
]

BYTELM_ARCHITECTURE = "wsbuilder.bytelm"
_BYTES = 256
_BOS = 256  # padding before the text starts
_EOS = 257  # end of text
_VOCAB = 258
_CHAT_ROLES = ("system", "user", "assistant", "tool")
_CHAT_STOP = "\n<|"
_mul = operator.mul


def format_chat(messages, *, add_generation_prompt=True):
    """The text a chat turns into: one ``<|role|>content`` block per message.

    Messages are separated by a newline. With ``add_generation_prompt`` the
    text ends with ``<|assistant|>`` so the model continues with the
    assistant's answer; training texts leave it out.
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    parts = []
    for message in messages:
        role = str(message.get("role", ""))
        if role not in _CHAT_ROLES:
            raise ValueError(f"unknown message role {role!r}")
        parts.append(f"<|{role}|>{message.get('content', '')}")
    if add_generation_prompt:
        parts.append("<|assistant|>")
    return "\n".join(parts)


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _softmax_in_place(logits):
    peak = max(logits)
    total = 0.0
    for index, value in enumerate(logits):
        exp = math.exp(value - peak)
        logits[index] = exp
        total += exp
    inverse = 1.0 / total
    for index in range(len(logits)):
        logits[index] *= inverse
    return logits


class ByteLanguageModel:
    """A byte-level neural language model (see the module docstring).

    ``context`` is how many previous bytes the model sees, ``embedding`` the
    size of each byte's vector and ``hidden`` the sizes of the ``tanh``
    layers. The weights start random (``seed`` makes them reproducible).
    """

    architecture = BYTELM_ARCHITECTURE

    def __init__(self, *, context=24, embedding=12, hidden=(96,), seed=None):
        self.context = _positive(context, "context")
        self.embedding = _positive(embedding, "embedding")
        if isinstance(hidden, int):
            hidden = (hidden,)
        self.hidden = tuple(_positive(size, "hidden size") for size in hidden)
        if not self.hidden:
            raise ValueError("hidden needs at least one layer")
        if self.context > 4096 or self.embedding > 4096 or max(self.hidden) > 65536:
            raise ValueError("model sizes are too large")
        rng = random.Random(seed)
        self.embeddings = [[rng.gauss(0.0, 0.5) for _ in range(self.embedding)] for _ in range(_VOCAB)]
        self.layers = []
        inputs = self.context * self.embedding
        for size in self.hidden:
            scale = 1.0 / math.sqrt(inputs)
            weights = [[rng.uniform(-scale, scale) for _ in range(inputs)] for _ in range(size)]
            self.layers.append((weights, [0.0] * size))
            inputs = size
        scale = 1.0 / math.sqrt(inputs)
        self.output_weights = [[rng.uniform(-scale, scale) for _ in range(inputs)] for _ in range(_VOCAB)]
        self.output_biases = [0.0] * _VOCAB
        self.trained_bytes = 0
        self.history = []

    # -- sizes ---------------------------------------------------------

    @property
    def parameter_count(self):
        total = _VOCAB * self.embedding
        for weights, biases in self.layers:
            total += len(weights) * len(weights[0]) + len(biases)
        return total + _VOCAB * self.hidden[-1] + _VOCAB

    def describe(self):
        return {
            "architecture": self.architecture,
            "context": self.context,
            "embedding": self.embedding,
            "hidden": list(self.hidden),
            "vocab_size": _VOCAB,
            "parameters": self.parameter_count,
            "trained_bytes": self.trained_bytes,
        }

    def __repr__(self):
        return (f"<ByteLanguageModel context={self.context} embedding={self.embedding} "
                f"hidden={list(self.hidden)} parameters={self.parameter_count}>")

    # -- the network ---------------------------------------------------

    def _window(self, tokens):
        window = list(tokens[-self.context:])
        if len(window) < self.context:
            window = [_BOS] * (self.context - len(window)) + window
        return window

    def _forward(self, window):
        """Activations of every layer and the output logits for one window."""
        embeddings = self.embeddings
        values = []
        for token in window:
            values.extend(embeddings[token])
        activations = [values]
        for weights, biases in self.layers:
            values = [math.tanh(bias + sum(map(_mul, row, values))) for row, bias in zip(weights, biases)]
            activations.append(values)
        logits = [bias + sum(map(_mul, row, values))
                  for row, bias in zip(self.output_weights, self.output_biases)]
        return activations, logits

    def next_token_probabilities(self, tokens):
        """The probability of each of the 258 tokens following ``tokens``."""
        return _softmax_in_place(self._forward(self._window(tokens))[1])

    def _train_step(self, window, target, learning_rate):
        activations, logits = self._forward(window)
        probabilities = _softmax_in_place(logits)
        loss = -math.log(max(probabilities[target], 1e-300))
        grad = probabilities
        grad[target] -= 1.0
        last = activations[-1]
        width = len(last)
        # Output layer: the gradient into the last hidden layer uses the
        # weights before they are updated.
        grad_hidden = [0.0] * width
        output_weights = self.output_weights
        output_biases = self.output_biases
        for token in range(_VOCAB):
            g = grad[token]
            if -1e-7 < g < 1e-7:
                continue
            row = output_weights[token]
            for j in range(width):
                grad_hidden[j] += g * row[j]
            step = learning_rate * g
            for j in range(width):
                row[j] -= step * last[j]
            output_biases[token] -= step
        # Hidden layers, from the last to the first.
        for index in range(len(self.layers) - 1, -1, -1):
            weights, biases = self.layers[index]
            outputs = activations[index + 1]
            inputs = activations[index]
            size = len(inputs)
            grad_z = [g * (1.0 - o * o) for g, o in zip(grad_hidden, outputs)]
            grad_inputs = [0.0] * size
            for unit, g in enumerate(grad_z):
                if g == 0.0:
                    continue
                row = weights[unit]
                for j in range(size):
                    grad_inputs[j] += g * row[j]
                step = learning_rate * g
                for j in range(size):
                    row[j] -= step * inputs[j]
                biases[unit] -= step
            grad_hidden = grad_inputs
        # Embeddings of the bytes in the window.
        size = self.embedding
        for position, token in enumerate(window):
            vector = self.embeddings[token]
            offset = position * size
            for j in range(size):
                vector[j] -= learning_rate * grad_hidden[offset + j]
        return loss

    # -- training ------------------------------------------------------

    def _samples(self, texts):
        samples = []
        for text in texts:
            if not isinstance(text, str):
                raise TypeError("texts must be strings")
            tokens = [_BOS] * self.context + list(text.encode("utf-8")) + [_EOS]
            for index in range(self.context, len(tokens)):
                samples.append((tokens[index - self.context:index], tokens[index]))
        return samples

    def fit(self, texts, *, epochs=10, learning_rate=0.05, decay=0.9, shuffle=True, seed=None,
            progress=None, task_handle=None, max_samples=5_000_000):
        """Train on ``texts`` (a string or a list of strings); returns the history.

        Each text is learnt from its first byte to its end, followed by an
        end-of-text token. After every epoch the learning rate is multiplied
        by ``decay`` and ``progress(epoch, mean_loss)`` is called. The history
        lists ``{"epoch", "loss", "learning_rate"}`` per epoch (loss in nats
        per byte). ``task_handle`` makes the run cancellable.
        """
        if isinstance(texts, str):
            texts = [texts]
        texts = list(texts)
        epochs = _positive(epochs, "epochs")
        if not learning_rate > 0:
            raise ValueError("learning_rate must be positive")
        samples = self._samples(texts)
        if not samples:
            raise ValueError("there is no text to learn from")
        if len(samples) > max_samples:
            raise ValueError(f"{len(samples)} training bytes exceed max_samples={max_samples}")
        rng = random.Random(seed)
        rate = float(learning_rate)
        history = []
        for epoch in range(1, epochs + 1):
            if shuffle:
                rng.shuffle(samples)
            total = 0.0
            for count, (window, target) in enumerate(samples):
                if count % 256 == 0:
                    _check_task_cancelled(task_handle)
                total += self._train_step(window, target, rate)
            mean = total / len(samples)
            if not math.isfinite(mean):
                raise FloatingPointError("training diverged; lower the learning_rate")
            entry = {"epoch": epoch, "loss": mean, "learning_rate": rate}
            history.append(entry)
            self.history.append(entry)
            if progress is not None:
                progress(epoch, mean)
            rate *= float(decay)
        self.trained_bytes += len(samples) * epochs
        return history

    def fit_chat(self, conversations, **options):
        """Train on conversations: lists of ``{"role", "content"}`` messages."""
        texts = [format_chat(conversation, add_generation_prompt=False) for conversation in conversations]
        return self.fit(texts, **options)

    def perplexity(self, text):
        """``exp`` of the mean loss per byte on ``text`` (lower is better)."""
        samples = self._samples([text])
        total = 0.0
        for window, target in samples:
            probabilities = _softmax_in_place(self._forward(window)[1])
            total -= math.log(max(probabilities[target], 1e-300))
        return math.exp(total / len(samples))

    # -- generation ----------------------------------------------------

    def _pick(self, probabilities, rng, *, temperature, top_k, top_p, recent, repeat_penalty):
        if repeat_penalty and repeat_penalty != 1.0:
            for token in set(recent):
                probabilities[token] /= repeat_penalty
        if temperature <= 0:
            return max(range(_VOCAB), key=probabilities.__getitem__)
        if temperature != 1.0:
            power = 1.0 / temperature
            probabilities = [p ** power if p > 0 else 0.0 for p in probabilities]
        ranked = sorted(range(_VOCAB), key=probabilities.__getitem__, reverse=True)
        if top_k:
            ranked = ranked[:max(1, int(top_k))]
        if top_p is not None and 0 < top_p < 1:
            total = sum(probabilities[token] for token in ranked) or 1.0
            kept, running = [], 0.0
            for token in ranked:
                kept.append(token)
                running += probabilities[token] / total
                if running >= top_p:
                    break
            ranked = kept
        weights = [probabilities[token] for token in ranked]
        if not sum(weights) > 0:
            return ranked[0]
        return rng.choices(ranked, weights=weights)[0]

    def stream(self, prompt="", **options):
        """Yield text pieces continuing ``prompt``.

        ``max_tokens`` counts bytes. Generation ends at the end-of-text token,
        at ``max_tokens`` or before any ``stop`` string. ``temperature=0``
        always picks the most likely byte. The final ``finish_reason`` is
        available as :attr:`last_finish_reason` (``"stop"`` or ``"length"``).
        """
        state = {}
        yield from self._pieces(prompt, state, **options)
        self.last_finish_reason = state["finish_reason"]
        self.last_completion_tokens = state["completion_tokens"]

    def _pieces(self, prompt, state, *, max_tokens=256, temperature=0.8, top_k=None, top_p=None,
                repeat_penalty=None, stop=None, seed=None, **_ignored):
        state["finish_reason"] = "length"
        state["completion_tokens"] = 0
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        max_tokens = int(max_tokens if max_tokens is not None else 256)
        if max_tokens < 0:
            raise ValueError("max_tokens must not be negative")
        stops = [stop] if isinstance(stop, str) else [s for s in (stop or ()) if s]
        holdback = max((len(s) for s in stops), default=1) - 1
        rng = random.Random(seed)
        tokens = list(prompt.encode("utf-8"))
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        temperature = float(temperature if temperature is not None else 0.8)
        pending = ""
        for _ in range(max_tokens):
            probabilities = self.next_token_probabilities(tokens)
            token = self._pick(probabilities, rng, temperature=temperature, top_k=top_k, top_p=top_p,
                               recent=tokens[-self.context:], repeat_penalty=repeat_penalty)
            if token >= _BYTES:
                state["finish_reason"] = "stop"
                break
            tokens.append(token)
            state["completion_tokens"] += 1
            pending += decoder.decode(bytes((token,)))
            if stops:
                cut = min((pending.find(s) for s in stops if s in pending), default=-1)
                if cut >= 0:
                    if pending[:cut]:
                        yield pending[:cut]
                    state["finish_reason"] = "stop"
                    return
                if len(pending) > holdback:
                    ready = len(pending) - holdback
                    yield pending[:ready]
                    pending = pending[ready:]
            elif pending:
                yield pending
                pending = ""
        pending += decoder.decode(b"", final=True)
        if pending:
            yield pending

    def generate(self, prompt="", **options):
        """The text continuing ``prompt`` (see :meth:`stream` for options)."""
        return "".join(self.stream(prompt, **options))

    def chat(self, messages, **options):
        """The assistant's answer to ``messages`` (formatted by :func:`format_chat`)."""
        options.setdefault("stop", _CHAT_STOP)
        return self.generate(format_chat(messages), **options)

    # -- persistence (NDJSON) ------------------------------------------

    def to_model(self, *, dtype="F32", name=None):
        """A :class:`~wsbuilder.ndjson_model.ModelWriter` holding this model."""
        from .ndjson_model import ModelWriter

        writer = ModelWriter(architecture=self.architecture, name=name)
        writer.add_metadata("context", self.context)
        writer.add_metadata("embedding", self.embedding)
        writer.add_metadata("hidden", list(self.hidden))
        writer.add_metadata("vocab_size", _VOCAB)
        writer.add_metadata("tokenizer", "utf-8 bytes, 256=bos, 257=eos")
        writer.add_metadata("chat_template", "<|role|>content, one message per line, then <|assistant|>")
        writer.add_metadata("trained_bytes", self.trained_bytes)
        writer.add_metadata("history", [{key: entry[key] for key in ("epoch", "loss")}
                                        for entry in self.history[-100:]])
        writer.add_tensor("tok_embd.weight", self.embeddings, shape=(_VOCAB, self.embedding), dtype=dtype)
        for index, (weights, biases) in enumerate(self.layers):
            writer.add_tensor(f"blk.{index}.weight", weights, shape=(len(weights), len(weights[0])),
                              dtype=dtype)
            writer.add_tensor(f"blk.{index}.bias", biases, dtype=dtype)
        writer.add_tensor("output.weight", self.output_weights, shape=(_VOCAB, self.hidden[-1]), dtype=dtype)
        writer.add_tensor("output.bias", self.output_biases, dtype=dtype)
        return writer

    def save(self, path, *, dtype="F32", name=None):
        """Write the model and its weights to one NDJSON file (atomically)."""
        self.to_model(dtype=dtype, name=name).write(path)
        return path

    @classmethod
    def from_reader(cls, reader):
        if reader.architecture != BYTELM_ARCHITECTURE:
            raise ValueError(f"not a wsbuilder byte language model (architecture {reader.architecture!r})")
        meta = reader.metadata
        if meta.get("vocab_size", _VOCAB) != _VOCAB:
            raise ValueError("unsupported vocabulary size")
        model = cls.__new__(cls)
        model.context = _positive(meta.get("context"), "context")
        model.embedding = _positive(meta.get("embedding"), "embedding")
        model.hidden = tuple(_positive(size, "hidden size") for size in meta.get("hidden") or ())
        if not model.hidden:
            raise ValueError("the model has no hidden layers")

        def matrix(name, rows, cols):
            tensor = reader.tensor(name)
            if tensor.shape != (rows, cols):
                raise ValueError(f"{name} has shape {tensor.shape}, expected {(rows, cols)}")
            return [[float(v) for v in row] for row in tensor.rows()]

        def vector(name, size):
            tensor = reader.tensor(name)
            if tensor.shape != (size,):
                raise ValueError(f"{name} has shape {tensor.shape}, expected {(size,)}")
            return [float(v) for v in tensor.values()]

        model.embeddings = matrix("tok_embd.weight", _VOCAB, model.embedding)
        model.layers = []
        inputs = model.context * model.embedding
        for index, size in enumerate(model.hidden):
            model.layers.append((matrix(f"blk.{index}.weight", size, inputs), vector(f"blk.{index}.bias", size)))
            inputs = size
        model.output_weights = matrix("output.weight", _VOCAB, inputs)
        model.output_biases = vector("output.bias", _VOCAB)
        model.trained_bytes = int(meta.get("trained_bytes") or 0)
        model.history = list(meta.get("history") or [])
        return model

    @classmethod
    def load(cls, path):
        """Read a model written by :meth:`save`."""
        from .ndjson_model import ModelReader

        with ModelReader(path) as reader:
            return cls.from_reader(reader)


class NativeBackend:
    """A :class:`ByteLanguageModel` as a :class:`~wsbuilder.llm.LanguageModel` backend.

    ``defaults`` are generation options used when a call does not give them
    (for example ``temperature=0.7``). Token counts in ``usage`` are bytes.
    """

    name = "wsbuilder-native"

    def __init__(self, model, **defaults):
        if isinstance(model, (str, bytes)) or hasattr(model, "__fspath__"):
            model = ByteLanguageModel.load(model)
        if not isinstance(model, ByteLanguageModel):
            raise TypeError("model must be a ByteLanguageModel or the path of one")
        self.model = model
        self.defaults = dict(defaults)

    def _options(self, options):
        merged = dict(self.defaults)
        merged.update({key: value for key, value in options.items() if value is not None})
        return merged

    def _run(self, prompt, options, stream):
        state = {}
        pieces = self.model._pieces(prompt, state, **options)
        if stream:
            return pieces
        text = "".join(pieces)
        prompt_tokens = len(prompt.encode("utf-8"))
        completion = state["completion_tokens"]
        return {"text": text, "finish_reason": state["finish_reason"],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                          "total_tokens": prompt_tokens + completion}}

    def complete(self, prompt, *, stream=False, **options):
        return self._run(str(prompt), self._options(options), stream)

    def chat(self, messages, *, stream=False, **options):
        options = self._options(options)
        stops = options.get("stop")
        stops = [stops] if isinstance(stops, str) else list(stops or ())
        options["stop"] = stops + [_CHAT_STOP]
        return self._run(format_chat(messages), options, stream)

    def close(self):
        pass


def submit_lm_training_task(task_manager, model, texts=None, *, conversations=None, name="lm-train",
                            group="ia", metadata=None, **fit_options):
    """Train a :class:`ByteLanguageModel` in the background with a TaskManager.

    Pass ``texts`` or ``conversations``. The task's metadata carries
    ``{"epoch", "loss"}`` as training advances; the result is the history.
    """
    if task_manager is None or not hasattr(task_manager, "spawn"):
        raise TypeError("task_manager must provide a spawn() method")
    if (texts is None) == (conversations is None):
        raise ValueError("pass either texts or conversations")
    task_metadata = dict(metadata or {})
    task_metadata.setdefault("module", "wsbuilder.ia_lm")

    def runner(handle):
        def progress(epoch, loss):
            handle.metadata["epoch"] = epoch
            handle.metadata["loss"] = loss

        if conversations is not None:
            return model.fit_chat(conversations, progress=progress, task_handle=handle, **fit_options)
        return model.fit(texts, progress=progress, task_handle=handle, **fit_options)

    return task_manager.spawn(runner, name=name, group=group, metadata=task_metadata, pass_handle=True)

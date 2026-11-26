"""Run decoder language models described as a JSON graph, in pure Python.

A **graph model** is a directory with three files:

* ``model.json``: what the model computes, as data (format below).
* ``weights.ndjson``: the tensors, in the wsbuilder NDJSON format
  (:mod:`wsbuilder.ndjson_model`, usually base64 tensors).
* ``tokenizer.json``: a byte-level BPE tokenizer (:class:`~wsbuilder.bpe.BPETokenizer`).

``model.json``::

    {"format": "wsbuilder-graph", "version": 1, "name": "...",
     "architecture": "qwen3",              # informative only
     "weights": "weights.ndjson",          # relative to model.json
     "tokenizer": {"type": "bpe", "file": "tokenizer.json"},   # or null
     "chat_template": "qwen3",             # "qwen3", "chatml", "llama3" or null
     "config": {"context_length": 40960, "bos_token_id": null,
                "add_bos_token": false, "eos_token_ids": [151645]},
     "generation": {"temperature": 0.6, "top_k": 20, "top_p": 0.95},
     "graph": {"embed": {"weight": "token_embd.weight"},
               "blocks": {"count": 28, "ops": [...]},
               "head": {"ops": [...]}}}

The graph runs once per token with a key/value cache. ``embed`` looks up the
token's row (optionally multiplied by ``scale``) into the value ``x``; the
``blocks`` ops run ``count`` times, each time with ``{i}`` in tensor names
replaced by the block number, and must leave the residual stream in ``x``;
the ``head`` ops start from ``x`` and must produce ``logits``. Each op is
``{"op": ..., "in": [value names], "out": name, ...attributes}`` and works
on vectors (lists of floats); the ops of version 1 are listed in
:data:`GRAPH_OPS` and documented in ``docs/llm.md``. Unknown ops, missing
tensors, wrong shapes, undefined values and paths outside the model
directory raise :class:`GraphFormatError` when the model is loaded, before
anything runs.

Any decoder expressible with these ops runs without code for its
architecture (Llama, Mistral, Qwen2, Qwen3 and others; see
:mod:`wsbuilder.graph_convert` to convert them). The matrix products and the
attention run on a backend of :mod:`wsbuilder.accel` (``accel=`` or
``$WSBUILDER_ACCEL``): C kernels compiled on demand (``native``), a pool of
processes over shared memory (``process``) or the pure Python reference
(``python``, about 10^8 multiply-adds per second).
"""

import array
import json
import math
import operator
import os
import random
from collections.abc import Mapping

from .accel import AccelBackend, _softmax, create_backend
from .ia_lm import sample_token

__all__ = [
    "CHAT_TEMPLATES",
    "GRAPH_FORMAT",
    "GRAPH_FORMAT_VERSION",
    "GRAPH_OPS",
    "GraphBackend",
    "GraphCache",
    "GraphFormatError",
    "GraphModel",
    "read_graph_spec",
]

GRAPH_FORMAT = "wsbuilder-graph"
GRAPH_FORMAT_VERSION = 1
CHAT_TEMPLATES = ("qwen3", "chatml", "llama3")
DEFAULT_MAX_CONTEXT = 4096
DEFAULT_GENERATION = {"max_tokens": 256, "temperature": 0.8, "top_k": None, "top_p": None,
                      "repeat_penalty": None}
_GENERATION_KEYS = ("max_tokens", "temperature", "top_k", "top_p", "repeat_penalty")
_REPEAT_WINDOW = 64
_MAX_SPEC_BYTES = 16 * 1024 * 1024
_mul = operator.mul
_add = operator.add
_SQRT_2_OVER_PI = math.sqrt(2.0 / math.pi)
_INV_SQRT_2 = 1.0 / math.sqrt(2.0)
_REQUIRED = object()


class GraphFormatError(ValueError):
    """A ``model.json`` graph that is invalid or does not match its weights."""


# -- reading model.json ----------------------------------------------------


def _safe_path(base_dir, value, what):
    """``value`` joined to ``base_dir``, refusing absolute paths and ``..``."""
    if not isinstance(value, str) or not value:
        raise GraphFormatError(f"{what} must be a relative file name")
    parts = value.replace("\\", "/").split("/")
    if (os.path.isabs(value) or value.startswith(("/", "\\")) or os.path.splitdrive(value)[0]
            or ".." in parts):
        raise GraphFormatError(f"{what} {value!r} must stay inside the model directory")
    path = os.path.join(base_dir, *parts)
    root = os.path.realpath(base_dir)
    if os.path.commonpath([root, os.path.realpath(path)]) != root:
        raise GraphFormatError(f"{what} {value!r} must stay inside the model directory")
    return path


def _id_or_none(value, what):
    if value is not None and (type(value) is not int or value < 0):
        raise GraphFormatError(f"{what} must be a token id or null")
    return value


def read_graph_spec(path):
    """Read and check the top level of a ``model.json`` file; returns the dict.

    The graph itself is checked against the weights by :meth:`GraphModel.load`.
    """
    path = os.fspath(path)
    with open(path, "rb") as handle:
        raw = handle.read(_MAX_SPEC_BYTES + 1)
    if len(raw) > _MAX_SPEC_BYTES:
        raise GraphFormatError("model.json is too large")
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GraphFormatError("model.json is not valid JSON") from exc
    if not isinstance(spec, dict) or spec.get("format") != GRAPH_FORMAT:
        raise GraphFormatError(f"not a {GRAPH_FORMAT} model.json")
    version = spec.get("version")
    if type(version) is not int or version < 1:
        raise GraphFormatError("model.json has no valid version")
    if version > GRAPH_FORMAT_VERSION:
        raise GraphFormatError(
            f"graph version {version} is newer than this runtime ({GRAPH_FORMAT_VERSION})"
        )
    _check_keys(spec, _SPEC_KEYS, "model.json")
    for key in ("name", "architecture"):
        if spec.get(key) is not None and not isinstance(spec[key], str):
            raise GraphFormatError(f"{key} must be a string")
    base_dir = os.path.dirname(os.path.abspath(path))
    _safe_path(base_dir, spec.get("weights"), "weights")
    tokenizer = spec.get("tokenizer")
    if tokenizer is not None:
        if not isinstance(tokenizer, dict) or tokenizer.get("type") != "bpe":
            raise GraphFormatError('tokenizer must be null or {"type": "bpe", "file": ...}')
        _check_keys(tokenizer, ("type", "file"), "tokenizer")
        _safe_path(base_dir, tokenizer.get("file"), "tokenizer.file")
    template = spec.get("chat_template")
    if template is not None and template not in CHAT_TEMPLATES:
        raise GraphFormatError(f"unknown chat_template {template!r}; use one of {CHAT_TEMPLATES}")
    config = spec.setdefault("config", {})
    if not isinstance(config, dict):
        raise GraphFormatError("config must be an object")
    context = config.get("context_length")
    if context is not None and (type(context) is not int or context < 1):
        raise GraphFormatError("config.context_length must be a positive integer")
    _id_or_none(config.get("bos_token_id"), "config.bos_token_id")
    if not isinstance(config.get("add_bos_token", False), bool):
        raise GraphFormatError("config.add_bos_token must be true or false")
    eos = config.get("eos_token_ids", [])
    if not isinstance(eos, list):
        raise GraphFormatError("config.eos_token_ids must be a list of token ids")
    for token_id in eos:
        _id_or_none(token_id, "config.eos_token_ids")
    generation = spec.setdefault("generation", {})
    if not isinstance(generation, dict) or not set(generation) <= set(_GENERATION_KEYS):
        raise GraphFormatError(f"generation may only set {', '.join(_GENERATION_KEYS)}")
    for key, value in generation.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise GraphFormatError(f"generation.{key} must be a number")
    graph = spec.get("graph")
    if not isinstance(graph, dict):
        raise GraphFormatError("model.json has no graph object")
    _check_keys(graph, ("embed", "blocks", "head"), "graph")
    for key, allowed in (("embed", ("weight", "scale")), ("blocks", ("count", "ops")),
                         ("head", ("ops",))):
        if isinstance(graph.get(key), dict):
            _check_keys(graph[key], allowed, f"graph.{key}")
    return spec


# -- compiling the graph ---------------------------------------------------


class _Weights:
    """Tensors of the weights file, converted once and shared by name.

    Matrices are stored by the algebra backend (:mod:`wsbuilder.accel`).
    """

    def __init__(self, reader, backend):
        self.reader = reader
        self.backend = backend
        self.matrices = {}
        self._vectors = {}

    def tensor(self, name, where):
        if not isinstance(name, str) or not name:
            raise GraphFormatError(f"{where}: tensor names must be non-empty strings")
        if name not in self.reader:
            raise GraphFormatError(f"{where}: tensor {name!r} is missing from the weights")
        return self.reader.tensor(name)

    def matrix(self, name, where):
        rows = self.matrices.get(name)
        if rows is None:
            tensor = self.tensor(name, where)
            if len(tensor.shape) != 2:
                raise GraphFormatError(
                    f"{where}: tensor {name!r} has shape {list(tensor.shape)}, expected 2-D"
                )
            rows = self.backend.prepare_matrix(tensor.shape[0], tensor.shape[1], tensor)
            self.matrices[name] = rows
        return rows

    def matrix_shape(self, name, where):
        tensor = self.tensor(name, where)
        if len(tensor.shape) != 2:
            raise GraphFormatError(
                f"{where}: tensor {name!r} has shape {list(tensor.shape)}, expected 2-D"
            )
        return tensor.shape

    def vector(self, name, where, size):
        values = self._vectors.get(name)
        if values is None:
            tensor = self.tensor(name, where)
            if tensor.shape != (size,):
                raise GraphFormatError(
                    f"{where}: tensor {name!r} has shape {list(tensor.shape)}, expected [{size}]"
                )
            values = [float(value) for value in tensor.to_array()]
            self._vectors[name] = values
        return values


class _Rope:
    """Rotation angles of one rope configuration, cached per position."""

    def __init__(self, theta, rotary_dim, factor, freq_factors):
        half = rotary_dim // 2
        self.inv_freq = [theta ** (-2.0 * index / rotary_dim) / factor for index in range(half)]
        if freq_factors is not None:
            self.inv_freq = [inv / scale for inv, scale in zip(self.inv_freq, freq_factors)]
        self._table = {}

    def angles(self, position):
        cached = self._table.get(position)
        if cached is None:
            cached = ([math.cos(position * inv) for inv in self.inv_freq],
                      [math.sin(position * inv) for inv in self.inv_freq])
            self._table[position] = cached
        return cached


class GraphCache:
    """The key/value cache of one sequence: create it with :meth:`GraphModel.new_cache`."""

    def __init__(self, slots):
        # One (keys, values) pair per attention op, each with an array("d") per
        # KV head holding head_dim values per position: exact like the floats.
        self.slots = [([array.array("d") for _ in range(heads)],
                       [array.array("d") for _ in range(heads)]) for heads in slots]
        self.length = 0


class _Context:
    __slots__ = ("position", "cache")

    def __init__(self, position, cache):
        self.position = position
        self.cache = cache


def _rms_norm(x, weight, eps):
    inverse = 1.0 / math.sqrt(sum(map(_mul, x, x)) / len(x) + eps)
    return [value * inverse * scale for value, scale in zip(x, weight)]


def _silu(value):
    return value / (1.0 + math.exp(-value)) if value > -700.0 else 0.0


def _number(value, where, key, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GraphFormatError(f"{where}: {key} must be a number")
    if positive and value <= 0:
        raise GraphFormatError(f"{where}: {key} must be positive")
    return float(value)


def _flag(value, where, key):
    if not isinstance(value, bool):
        raise GraphFormatError(f"{where}: {key} must be true or false")
    return value


def _integer(value, where, key):
    if type(value) is not int or value < 1:
        raise GraphFormatError(f"{where}: {key} must be a positive integer")
    return value


class _Compiler:
    """Turns graph ops into closures, checking names, attributes and shapes."""

    def __init__(self, weights):
        self.weights = weights
        self.attention_heads = []  # KV heads of each attention op, in order
        self._ropes = {}

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _attr(op, key, where, default=_REQUIRED):
        if key in op:
            return op[key]
        if default is _REQUIRED:
            raise GraphFormatError(f"{where}: missing attribute {key!r}")
        return default

    @staticmethod
    def _tensor_name(op, key, where, block, default=_REQUIRED):
        name = _Compiler._attr(op, key, where, default)
        if name is None:
            return None
        if not isinstance(name, str) or not name:
            raise GraphFormatError(f"{where}: {key} must be a tensor name")
        # Plain replacement, not str.format: names are data, never format specs.
        return name if block is None else name.replace("{i}", str(block))

    def _inputs(self, op, where, sizes, count):
        names = op.get("in")
        if (not isinstance(names, list) or len(names) != count
                or not all(isinstance(name, str) and name for name in names)):
            raise GraphFormatError(f"{where}: 'in' must list {count} value name(s)")
        for name in names:
            if name not in sizes:
                raise GraphFormatError(f"{where}: value {name!r} is used before it is defined")
        return names

    @staticmethod
    def _output(op, where):
        name = op.get("out")
        if not isinstance(name, str) or not name:
            raise GraphFormatError(f"{where}: 'out' must be a value name")
        return name

    # -- ops -------------------------------------------------------------

    def compile_ops(self, ops, sizes, location, block=None):
        if not isinstance(ops, list):
            raise GraphFormatError(f"{location}.ops must be a list")
        compiled = []
        for index, op in enumerate(ops):
            where = f"{location}.ops[{index}]"
            if block is not None:
                where += f" (block {block})"
            if not isinstance(op, dict):
                raise GraphFormatError(f"{where}: each op must be an object")
            kind = op.get("op")
            builder = _OP_BUILDERS.get(kind) if isinstance(kind, str) else None
            if builder is None:
                raise GraphFormatError(
                    f"{where}: unknown op {kind!r}; version {GRAPH_FORMAT_VERSION} supports "
                    f"{', '.join(GRAPH_OPS)}"
                )
            unknown = set(op) - _COMMON_KEYS - _OP_ATTRIBUTES[kind]
            if unknown:
                raise GraphFormatError(
                    f"{where} {kind}: unknown attribute(s) {', '.join(sorted(map(str, unknown)))}"
                )
            compiled.append(builder(self, op, f"{where} {kind}", sizes, block))
        return compiled

    def _rms_norm(self, op, where, sizes, block):
        (source,) = self._inputs(op, where, sizes, 1)
        out = self._output(op, where)
        size = sizes[source]
        eps = _number(self._attr(op, "eps", where, 1e-6), where, "eps", positive=True)
        weight = self.weights.vector(self._tensor_name(op, "weight", where, block), where, size)
        if _flag(self._attr(op, "add_unit_offset", where, False), where, "add_unit_offset"):
            weight = [1.0 + value for value in weight]
        sizes[out] = size

        def run(env, _ctx):
            env[out] = _rms_norm(env[source], weight, eps)
        return run

    def _head_rms_norm(self, op, where, sizes, block):
        (source,) = self._inputs(op, where, sizes, 1)
        out = self._output(op, where)
        head_dim = _integer(self._attr(op, "head_dim", where), where, "head_dim")
        size = sizes[source]
        if size % head_dim:
            raise GraphFormatError(f"{where}: size {size} is not a multiple of head_dim {head_dim}")
        eps = _number(self._attr(op, "eps", where, 1e-6), where, "eps", positive=True)
        weight = self.weights.vector(self._tensor_name(op, "weight", where, block), where,
                                     head_dim)
        if _flag(self._attr(op, "add_unit_offset", where, False), where, "add_unit_offset"):
            weight = [1.0 + value for value in weight]
        sizes[out] = size
        starts = range(0, size, head_dim)

        def run(env, _ctx):
            x = env[source]
            result = []
            for start in starts:
                result += _rms_norm(x[start:start + head_dim], weight, eps)
            env[out] = result
        return run

    def _layer_norm(self, op, where, sizes, block):
        (source,) = self._inputs(op, where, sizes, 1)
        out = self._output(op, where)
        size = sizes[source]
        eps = _number(self._attr(op, "eps", where, 1e-5), where, "eps", positive=True)
        weight = self.weights.vector(self._tensor_name(op, "weight", where, block), where, size)
        bias_name = self._tensor_name(op, "bias", where, block, None)
        bias = None if bias_name is None else self.weights.vector(bias_name, where, size)
        sizes[out] = size

        def run(env, _ctx):
            x = env[source]
            mean = sum(x) / size
            centered = [value - mean for value in x]
            inverse = 1.0 / math.sqrt(sum(map(_mul, centered, centered)) / size + eps)
            result = [value * inverse * scale for value, scale in zip(centered, weight)]
            env[out] = result if bias is None else list(map(_add, result, bias))
        return run

    def _linear(self, op, where, sizes, block):
        (source,) = self._inputs(op, where, sizes, 1)
        out = self._output(op, where)
        name = self._tensor_name(op, "weight", where, block)
        rows_count, width = self.weights.matrix_shape(name, where)
        if width != sizes[source]:
            raise GraphFormatError(
                f"{where}: tensor {name!r} has shape {[rows_count, width]} but the input "
                f"{source!r} has size {sizes[source]}"
            )
        matrix = self.weights.matrix(name, where)
        bias_name = self._tensor_name(op, "bias", where, block, None)
        bias = (None if bias_name is None else self.weights.backend.prepare_vector(
            self.weights.vector(bias_name, where, rows_count)))
        matvec = self.weights.backend.matvec
        sizes[out] = rows_count

        def run(env, _ctx):
            env[out] = matvec(matrix, env[source], bias)
        return run

    def _binary(function):
        def build(self, op, where, sizes, _block):
            first, second = self._inputs(op, where, sizes, 2)
            out = self._output(op, where)
            if sizes[first] != sizes[second]:
                raise GraphFormatError(
                    f"{where}: {first!r} has size {sizes[first]} and {second!r} {sizes[second]}"
                )
            sizes[out] = sizes[first]

            def run(env, _ctx):
                env[out] = list(map(function, env[first], env[second]))
            return run
        return build

    def _unary(function):
        def build(self, op, where, sizes, _block):
            (source,) = self._inputs(op, where, sizes, 1)
            out = self._output(op, where)
            sizes[out] = sizes[source]

            def run(env, _ctx):
                env[out] = list(map(function, env[source]))
            return run
        return build

    def _scale(self, op, where, sizes, _block):
        (source,) = self._inputs(op, where, sizes, 1)
        out = self._output(op, where)
        factor = _number(self._attr(op, "value", where), where, "value")
        sizes[out] = sizes[source]

        def run(env, _ctx):
            env[out] = [value * factor for value in env[source]]
        return run

    def _softcap(self, op, where, sizes, _block):
        (source,) = self._inputs(op, where, sizes, 1)
        out = self._output(op, where)
        cap = _number(self._attr(op, "cap", where), where, "cap", positive=True)
        sizes[out] = sizes[source]

        def run(env, _ctx):
            env[out] = [math.tanh(value / cap) * cap for value in env[source]]
        return run

    def _split(self, op, where, sizes, _block):
        (source,) = self._inputs(op, where, sizes, 1)
        outs = op.get("out")
        split_sizes = self._attr(op, "sizes", where)
        if (not isinstance(outs, list) or not outs
                or not all(isinstance(name, str) and name for name in outs)):
            raise GraphFormatError(f"{where}: 'out' must list the value names")
        if not isinstance(split_sizes, list) or len(split_sizes) != len(outs):
            raise GraphFormatError(f"{where}: 'sizes' must give one size per output")
        for size in split_sizes:
            _integer(size, where, "sizes")
        if sum(split_sizes) != sizes[source]:
            raise GraphFormatError(
                f"{where}: sizes add up to {sum(split_sizes)}, not {sizes[source]}"
            )
        bounds = []
        start = 0
        for name, size in zip(outs, split_sizes):
            bounds.append((name, start, start + size))
            sizes[name] = size
            start += size

        def run(env, _ctx):
            x = env[source]
            for name, begin, end in bounds:
                env[name] = x[begin:end]
        return run

    def _rope(self, op, where, sizes, block):
        (source,) = self._inputs(op, where, sizes, 1)
        out = self._output(op, where)
        head_dim = _integer(self._attr(op, "head_dim", where), where, "head_dim")
        size = sizes[source]
        if size % head_dim:
            raise GraphFormatError(f"{where}: size {size} is not a multiple of head_dim {head_dim}")
        rotary_dim = _integer(self._attr(op, "rotary_dim", where, head_dim), where, "rotary_dim")
        if rotary_dim % 2 or rotary_dim > head_dim:
            raise GraphFormatError(f"{where}: rotary_dim must be even and at most head_dim")
        style = self._attr(op, "style", where, "neox")
        if style not in ("neox", "interleaved"):
            raise GraphFormatError(f"{where}: style must be 'neox' or 'interleaved'")
        theta = _number(self._attr(op, "theta", where, 10000.0), where, "theta", positive=True)
        scaling = self._attr(op, "scaling", where, None)
        factor = 1.0
        if scaling is not None:
            if not isinstance(scaling, dict) or scaling.get("type") != "linear":
                raise GraphFormatError(f"{where}: scaling must be null or a 'linear' scaling")
            factor = _number(scaling.get("factor"), where, "scaling.factor", positive=True)
        factors_name = self._tensor_name(op, "freq_factors", where, block, None)
        factors = (None if factors_name is None
                   else self.weights.vector(factors_name, where, rotary_dim // 2))
        key = (theta, rotary_dim, factor, factors_name)
        rope = self._ropes.get(key)
        if rope is None:
            rope = self._ropes[key] = _Rope(theta, rotary_dim, factor, factors)
        sizes[out] = size
        half = rotary_dim // 2
        if style == "neox":
            # Hugging Face rotate_half: element i turns with element i + rotary_dim / 2.
            pairs = [(index, index + half) for index in range(half)]
        else:
            # Original RoPE (GGUF llama): neighbouring elements 2i and 2i + 1 turn together.
            pairs = [(2 * index, 2 * index + 1) for index in range(half)]
        heads = range(0, size, head_dim)

        def run(env, ctx):
            cos, sin = rope.angles(ctx.position)
            x = list(env[source])
            for start in heads:
                for (first, second), c, s in zip(pairs, cos, sin):
                    a = x[start + first]
                    b = x[start + second]
                    x[start + first] = a * c - b * s
                    x[start + second] = b * c + a * s
            env[out] = x
        return run

    def _attention(self, op, where, sizes, _block):
        q_name, k_name, v_name = self._inputs(op, where, sizes, 3)
        out = self._output(op, where)
        heads = _integer(self._attr(op, "heads", where), where, "heads")
        kv_heads = _integer(self._attr(op, "kv_heads", where, heads), where, "kv_heads")
        head_dim = _integer(self._attr(op, "head_dim", where), where, "head_dim")
        if heads % kv_heads:
            raise GraphFormatError(f"{where}: heads ({heads}) must be a multiple of kv_heads "
                                   f"({kv_heads})")
        for name, expected in ((q_name, heads), (k_name, kv_heads), (v_name, kv_heads)):
            if sizes[name] != expected * head_dim:
                raise GraphFormatError(
                    f"{where}: {name!r} has size {sizes[name]}, expected {expected} x {head_dim}"
                )
        scale = _number(self._attr(op, "scale", where, 1.0 / math.sqrt(head_dim)), where, "scale")
        window = self._attr(op, "sliding_window", where, None)
        if window is not None:
            window = _integer(window, where, "sliding_window")
        softcap = self._attr(op, "logit_softcap", where, None)
        if softcap is not None:
            softcap = _number(softcap, where, "logit_softcap", positive=True)
        slot = len(self.attention_heads)
        self.attention_heads.append(kv_heads)
        attend = self.weights.backend.attend
        sizes[out] = heads * head_dim

        def run(env, ctx):
            keys, values = ctx.cache.slots[slot]
            k, v = env[k_name], env[v_name]
            for head in range(kv_heads):
                # One array("d") per KV head, a row of head_dim values per position.
                keys[head].extend(k[head * head_dim:(head + 1) * head_dim])
                values[head].extend(v[head * head_dim:(head + 1) * head_dim])
            start = 0 if window is None else max(0, ctx.position + 1 - window)
            env[out] = attend(env[q_name], keys, values, heads, kv_heads, head_dim, start,
                              scale, softcap)
        return run

    _gelu = _unary(lambda value: 0.5 * value * (1.0 + math.erf(value * _INV_SQRT_2)))
    _gelu_tanh = _unary(lambda value: 0.5 * value * (
        1.0 + math.tanh(_SQRT_2_OVER_PI * (value + 0.044715 * value * value * value))))
    _silu_op = _unary(_silu)
    _relu = _unary(lambda value: value if value > 0.0 else 0.0)
    _add_op = _binary(_add)
    _mul_op = _binary(_mul)
    del _binary, _unary


_OP_BUILDERS = {
    "rms_norm": _Compiler._rms_norm,
    "head_rms_norm": _Compiler._head_rms_norm,
    "layer_norm": _Compiler._layer_norm,
    "linear": _Compiler._linear,
    "add": _Compiler._add_op,
    "mul": _Compiler._mul_op,
    "scale": _Compiler._scale,
    "silu": _Compiler._silu_op,
    "gelu": _Compiler._gelu,
    "gelu_tanh": _Compiler._gelu_tanh,
    "relu": _Compiler._relu,
    "split": _Compiler._split,
    "rope": _Compiler._rope,
    "attention": _Compiler._attention,
    "softcap": _Compiler._softcap,
}
#: The ops of graph version 1.
GRAPH_OPS = tuple(_OP_BUILDERS)
_COMMON_KEYS = frozenset(("op", "in", "out"))
_OP_ATTRIBUTES = {
    "rms_norm": {"weight", "eps", "add_unit_offset"},
    "head_rms_norm": {"weight", "eps", "head_dim", "add_unit_offset"},
    "layer_norm": {"weight", "bias", "eps"},
    "linear": {"weight", "bias"},
    "add": set(), "mul": set(), "silu": set(), "gelu": set(), "gelu_tanh": set(),
    "relu": set(),
    "scale": {"value"},
    "softcap": {"cap"},
    "split": {"sizes"},
    "rope": {"head_dim", "rotary_dim", "style", "theta", "scaling", "freq_factors"},
    "attention": {"heads", "kv_heads", "head_dim", "scale", "sliding_window", "logit_softcap"},
}
_SPEC_KEYS = frozenset(("format", "version", "name", "architecture", "weights", "tokenizer",
                        "chat_template", "config", "generation", "graph", "metadata"))


def _check_keys(value, allowed, where):
    unknown = set(value) - set(allowed)
    if unknown:
        raise GraphFormatError(f"{where}: unknown key(s) {', '.join(sorted(map(str, unknown)))}")


# -- chat prompts ----------------------------------------------------------


def _chat_messages(messages, system):
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if isinstance(messages, (bytes, Mapping)):
        raise TypeError("messages must be a sequence of mappings")
    items = []
    if system is not None:
        items.append(("system", str(system)))
    for message in messages:
        if not isinstance(message, Mapping):
            raise TypeError("each message must be a mapping with role and content")
        role = message.get("role")
        if role not in ("system", "user", "assistant"):
            raise ValueError(f"unsupported chat role {role!r}; use system, user or assistant")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise TypeError("content must be a string")
        items.append((role, content or ""))
    return items


def _control_id(tokenizer, token):
    token_id = tokenizer.token_to_id(token)
    if token_id is None or token not in tokenizer.special_tokens:
        raise ValueError(f"this chat template needs {token!r} as a special token")
    return token_id


def _encode_template(tokenizer, segments):
    """Ids of ``segments`` (``int`` control ids or text); text never yields added tokens."""
    ids = []
    pending = []
    for segment in segments:
        if type(segment) is int:
            if pending:
                ids += tokenizer.encode("".join(pending), split_added_tokens=False)
                pending.clear()
            ids.append(segment)
        else:
            pending.append(segment)
    if pending:
        ids += tokenizer.encode("".join(pending), split_added_tokens=False)
    return ids


def _encode_chatml(tokenizer, messages, system):
    start = _control_id(tokenizer, "<|im_start|>")
    end = _control_id(tokenizer, "<|im_end|>")
    segments = []
    for role, content in _chat_messages(messages, system):
        segments += [start, f"{role}\n{content}", end, "\n"]
    return _encode_template(tokenizer, segments + [start, "assistant\n"])


def _encode_llama3(tokenizer, messages, system, bos_token_id):
    start = _control_id(tokenizer, "<|start_header_id|>")
    end = _control_id(tokenizer, "<|end_header_id|>")
    eot = _control_id(tokenizer, "<|eot_id|>")
    segments = [] if bos_token_id is None else [bos_token_id]
    for role, content in _chat_messages(messages, system):
        segments += [start, role, end, "\n\n" + content.strip(), eot]
    return _encode_template(tokenizer, segments + [start, "assistant", end, "\n\n"])


_TEMPLATE_END_TOKENS = {"qwen3": ("<|im_end|>", "<|endoftext|>"),
                        "chatml": ("<|im_end|>", "<|endoftext|>"),
                        "llama3": ("<|eot_id|>", "<|eom_id|>", "<|end_of_text|>")}


# -- the model -------------------------------------------------------------


class GraphModel:
    """A language model run by interpreting a ``model.json`` graph (see the module docstring).

    Build it with :meth:`load`. ``forward`` computes the logits of one token;
    ``generate``/``stream`` continue a prompt and ``chat``/``stream_chat``
    answer messages with the model's chat template. ``max_context`` (default
    the smaller of ``config.context_length`` and 4096) caps the tokens of
    one sequence. ``accel`` is a backend name of :mod:`wsbuilder.accel`
    (``"auto"``, ``"native"``, ``"process"`` or ``"python"``; default
    ``$WSBUILDER_ACCEL`` or ``"auto"``) or an
    :class:`~wsbuilder.accel.AccelBackend`, which the caller then closes;
    ``accel_name`` tells which one runs.
    """

    def __init__(self, spec, reader, tokenizer=None, *, max_context=None, path=None,
                 accel=None):
        if isinstance(accel, AccelBackend):
            self.accel, self._owns_accel = accel, False
        else:
            self.accel, self._owns_accel = create_backend(accel), True
        try:
            self._setup(spec, reader, tokenizer, max_context, path)
        except BaseException:
            if self._owns_accel:
                self.accel.close()
            raise

    def _setup(self, spec, reader, tokenizer, max_context, path):
        self._closed = False
        self.spec = spec
        self.path = path
        self.name = spec.get("name") or "graph-model"
        self.architecture = spec.get("architecture")
        self.config = spec.get("config") or {}
        self.chat_template = spec.get("chat_template")
        self.tokenizer = tokenizer
        self.reader = reader
        self.defaults = dict(DEFAULT_GENERATION)
        self.defaults.update(spec.get("generation") or {})
        self._compile(spec["graph"])
        context = self.config.get("context_length") or DEFAULT_MAX_CONTEXT
        if max_context is None:
            max_context = min(context, DEFAULT_MAX_CONTEXT)
        if type(max_context) is not int or max_context < 1:
            raise ValueError("max_context must be a positive integer")
        self.max_context = max_context
        self.bos_token_id = self.config.get("bos_token_id")
        self.add_bos_token = bool(self.config.get("add_bos_token", False))
        self._stop_ids = self._stop_token_ids()
        self._blocked_ids = self._undecodable_ids()

    @property
    def accel_name(self):
        """The name of the algebra backend: ``native``, ``process`` or ``python``."""
        return self.accel.name

    @classmethod
    def load(cls, path, *, max_context=None, accel=None):
        """Load ``model.json`` (or the directory holding it), its weights and tokenizer.

        See the class docstring for ``max_context`` and ``accel``.
        """
        from .bpe import BPETokenizer
        from .ndjson_model import ModelReader

        path = os.fspath(path)
        if os.path.isdir(path):
            path = os.path.join(path, "model.json")
        spec = read_graph_spec(path)
        base_dir = os.path.dirname(os.path.abspath(path))
        tokenizer = None
        if spec.get("tokenizer") is not None:
            tokenizer = BPETokenizer.from_file(
                _safe_path(base_dir, spec["tokenizer"]["file"], "tokenizer.file"))
            bos = spec["config"].get("bos_token_id")
            eos = spec["config"].get("eos_token_ids") or []
            for token_id in ([] if bos is None else [bos]) + eos:
                if tokenizer.id_to_token(token_id) is None:
                    raise GraphFormatError(f"config token id {token_id} is not a token")
            tokenizer.bos_token_id = bos
            tokenizer.eos_token_id = eos[0] if eos else None
        reader = ModelReader(_safe_path(base_dir, spec["weights"], "weights"))
        try:
            return cls(spec, reader, tokenizer, max_context=max_context, path=path, accel=accel)
        except BaseException:
            reader.close()
            raise

    # -- compilation -----------------------------------------------------

    def _compile(self, graph):
        compiler = _Compiler(_Weights(self.reader, self.accel))
        embed = graph.get("embed")
        if not isinstance(embed, dict):
            raise GraphFormatError("graph.embed must be an object")
        embed_name = compiler._tensor_name(embed, "weight", "graph.embed", None)
        self.vocab_size, self.hidden_size = compiler.weights.matrix_shape(embed_name,
                                                                          "graph.embed")
        scale = embed.get("scale")
        self._embed_scale = None if scale is None else _number(scale, "graph.embed", "scale")
        blocks = graph.get("blocks")
        if not isinstance(blocks, dict):
            raise GraphFormatError("graph.blocks must be an object")
        count = blocks.get("count")
        if type(count) is not int or count < 0:
            raise GraphFormatError("graph.blocks.count must be a non-negative integer")
        self._blocks = []
        for block in range(count):
            sizes = {"x": self.hidden_size}
            self._blocks.append(compiler.compile_ops(blocks.get("ops"), sizes, "graph.blocks",
                                                     block))
            if sizes["x"] != self.hidden_size:
                raise GraphFormatError(
                    f"graph.blocks (block {block}) leaves x with size {sizes['x']}, "
                    f"not {self.hidden_size}"
                )
        head = graph.get("head")
        if not isinstance(head, dict):
            raise GraphFormatError("graph.head must be an object")
        sizes = {"x": self.hidden_size}
        self._head = compiler.compile_ops(head.get("ops"), sizes, "graph.head")
        if "logits" not in sizes:
            raise GraphFormatError("graph.head must produce the value 'logits'")
        self.logits_size = sizes["logits"]
        self._attention_heads = list(compiler.attention_heads)
        # Tied embeddings: the head already stored the matrix; reuse its rows.
        self._embed_matrix = compiler.weights.matrices.get(embed_name)
        self._embed_tensor = self.reader.tensor(embed_name)
        if self._embed_matrix is None and not self._embed_tensor.row_reads:
            self._embed_matrix = compiler.weights.matrix(embed_name, "graph.embed")
        self.parameter_count = self.reader.parameter_count

    def _stop_token_ids(self):
        ids = set(self.config.get("eos_token_ids") or [])
        if self.tokenizer is not None:
            if self.tokenizer.eos_token_id is not None:
                ids.add(self.tokenizer.eos_token_id)
            for token in _TEMPLATE_END_TOKENS.get(self.chat_template, ()):
                token_id = self.tokenizer.token_to_id(token)
                if token_id is not None:
                    ids.add(token_id)
        return frozenset(ids)

    def _undecodable_ids(self):
        """Logit ids the tokenizer cannot decode (padding rows): never sampled."""
        if self.tokenizer is None:
            return ()
        return tuple(index for index in range(self.logits_size)
                     if self.tokenizer.id_to_token(index) is None)

    # -- computation -----------------------------------------------------

    def new_cache(self):
        """An empty :class:`GraphCache` for one sequence."""
        return GraphCache(self._attention_heads)

    def _embedding(self, token_id):
        if type(token_id) is not int or not 0 <= token_id < self.vocab_size:
            raise ValueError(f"token id {token_id!r} is outside the vocabulary "
                             f"(0..{self.vocab_size - 1})")
        if self._embed_matrix is not None:
            row = self._embed_matrix.row(token_id)
        else:
            row = self._embed_tensor.row_array(token_id).tolist()
        if self._embed_scale is not None:
            row = [value * self._embed_scale for value in row]
        return row

    def _step(self, token_id, position, cache, want_logits):
        if self._closed:
            raise ValueError("the model is closed")
        if not isinstance(cache, GraphCache):
            raise TypeError("cache must come from new_cache()")
        if position != cache.length:
            raise ValueError(f"position {position} does not follow the cache "
                             f"({cache.length} tokens)")
        if position >= self.max_context:
            raise ValueError(f"the sequence exceeds max_context ({self.max_context} tokens)")
        ctx = _Context(position, cache)
        x = self._embedding(token_id)
        for ops in self._blocks:
            env = {"x": x}
            for run in ops:
                run(env, ctx)
            x = env["x"]
        cache.length += 1
        if not want_logits:
            return None
        env = {"x": x}
        for run in self._head:
            run(env, ctx)
        return env["logits"]

    def forward(self, token_id, position, cache):
        """Logits (a list of floats) of the token after ``token_id`` at ``position``.

        ``position`` must equal ``cache.length``: tokens are fed in order.
        """
        return self._step(token_id, position, cache, True)

    # -- generation ------------------------------------------------------

    def encode(self, prompt):
        """Token ids of a raw prompt (control tokens in the text are honoured)."""
        if isinstance(prompt, (list, tuple)):
            ids = list(prompt)
            if not all(type(token) is int for token in ids):
                raise TypeError("a prompt given as a list must hold token ids")
            return ids
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string or a list of token ids")
        self._need_tokenizer()
        ids = self.tokenizer.encode(prompt)
        if self.add_bos_token and self.bos_token_id is not None:
            ids.insert(0, self.bos_token_id)
        return ids

    def encode_chat(self, messages, *, enable_thinking=True, system=None):
        """Prompt ids of ``messages`` with the chat template; message text never
        becomes control tokens."""
        self._need_tokenizer()
        template = self.chat_template
        if template is None:
            raise ValueError(f"{self.name!r} has no chat template; use generate()")
        if template == "qwen3":
            if isinstance(messages, str):
                messages = [{"role": "user", "content": messages}]
            ids = self.tokenizer.encode_chat(messages, enable_thinking=enable_thinking,
                                             system=system)
        elif template == "chatml":
            ids = _encode_chatml(self.tokenizer, messages, system)
        else:
            return _encode_llama3(self.tokenizer, messages, system, self.bos_token_id)
        if self.add_bos_token and self.bos_token_id is not None:
            ids.insert(0, self.bos_token_id)
        return ids

    def _need_tokenizer(self):
        if self.tokenizer is None:
            raise ValueError(f"{self.name!r} has no tokenizer; pass token ids")

    def _options(self, options):
        merged = dict(self.defaults)
        merged.update({key: value for key, value in options.items() if value is not None})
        return merged

    def _pieces(self, prompt_ids, state, *, max_tokens=256, temperature=0.8, top_k=None,
                top_p=None, repeat_penalty=None, stop=None, seed=None, **_ignored):
        state["finish_reason"] = "length"
        state["prompt_tokens"] = len(prompt_ids)
        state["completion_tokens"] = 0
        max_tokens = int(max_tokens)
        if max_tokens < 0:
            raise ValueError("max_tokens must not be negative")
        if not prompt_ids:
            raise ValueError("the prompt has no tokens")
        if len(prompt_ids) > self.max_context:
            raise ValueError(f"the prompt has {len(prompt_ids)} tokens; max_context is "
                             f"{self.max_context}")
        if repeat_penalty is not None and not repeat_penalty > 0:
            raise ValueError("repeat_penalty must be positive")
        stops = [stop] if isinstance(stop, str) else [text for text in (stop or ()) if text]
        holdback = max((len(text) for text in stops), default=1) - 1
        temperature = float(temperature)
        rng = random.Random(seed)
        decoder = (self.tokenizer.stream_decoder() if self.tokenizer is not None else None)
        if max_tokens == 0:
            return
        cache = self.new_cache()
        for position, token in enumerate(prompt_ids[:-1]):
            self._step(token, position, cache, False)
        tokens = list(prompt_ids)
        pending = ""
        for _ in range(max_tokens):
            if len(tokens) >= self.max_context:
                break  # prompt + answer never exceed max_context tokens
            logits = self._step(tokens[-1], cache.length, cache, True)
            for index in self._blocked_ids:
                logits[index] = -math.inf
            if repeat_penalty and repeat_penalty != 1.0:
                # Hugging Face / llama.cpp: shrink the logits of recent tokens.
                for index in set(tokens[-_REPEAT_WINDOW:]):
                    if 0 <= index < len(logits):
                        value = logits[index]
                        logits[index] = (value / repeat_penalty if value > 0
                                         else value * repeat_penalty)
            if temperature <= 0:
                token = max(range(len(logits)), key=logits.__getitem__)
            else:
                token = sample_token(_softmax(logits), rng, temperature=temperature, top_k=top_k,
                                     top_p=top_p)
            if token in self._stop_ids:
                state["finish_reason"] = "stop"
                break
            tokens.append(token)
            state["completion_tokens"] += 1
            if decoder is None:
                continue
            pending += decoder.push(token)
            if stops:
                cut = min((pending.find(text) for text in stops if text in pending), default=-1)
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
        state["tokens"] = tokens
        if decoder is not None:
            pending += decoder.flush()
            if pending:
                yield pending

    def stream(self, prompt, **options):
        """Yield text pieces continuing ``prompt`` (a string or token ids).

        Options: ``max_tokens`` (tokens), ``temperature`` (0 = greedy),
        ``top_k``, ``top_p``, ``repeat_penalty``, ``stop`` (strings) and
        ``seed``. Defaults come from ``generation`` in ``model.json``.
        Generation ends at an end-of-sequence token, at ``max_tokens``, at
        ``max_context`` or before a ``stop`` string.
        """
        return self._pieces(self.encode(prompt), {}, **self._options(options))

    def generate(self, prompt, **options):
        """The text continuing ``prompt`` (see :meth:`stream` for the options)."""
        return "".join(self.stream(prompt, **options))

    def stream_chat(self, messages, *, enable_thinking=True, system=None, **options):
        """Yield the assistant's answer to ``messages`` piece by piece."""
        ids = self.encode_chat(messages, enable_thinking=enable_thinking, system=system)
        return self._pieces(ids, {}, **self._options(options))

    def chat(self, messages, **options):
        """The assistant's answer to ``messages`` (see :meth:`stream_chat`)."""
        return "".join(self.stream_chat(messages, **options))

    def close(self):
        """Close the weights file and free the matrices and the backend's pools and
        shared memory (idempotent; a backend passed in by the caller stays open)."""
        self._closed = True
        self._blocks = []
        self._head = []
        self._embed_matrix = None
        self.reader.close()
        if self._owns_accel:
            self.accel.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return (f"<GraphModel {self.name} {self.architecture} {len(self._blocks)} blocks "
                f"vocab={self.vocab_size}>")


class GraphBackend:
    """A :class:`GraphModel` as a :class:`~wsbuilder.llm.LanguageModel` backend.

    ``model`` is a ``GraphModel`` or the path of a ``model.json`` (or its
    directory). ``defaults`` are generation options for calls that do not
    give them. ``usage`` counts real tokens. Chat calls accept
    ``enable_thinking`` and ``system``.
    """

    name = "wsbuilder-graph"

    def __init__(self, model, **defaults):
        if isinstance(model, (str, bytes)) or hasattr(model, "__fspath__"):
            model = GraphModel.load(model)
        if not isinstance(model, GraphModel):
            raise TypeError("model must be a GraphModel or the path of a model.json")
        self.model = model
        self.defaults = dict(defaults)

    def _options(self, options):
        merged = dict(self.defaults)
        merged.update({key: value for key, value in options.items() if value is not None})
        return self.model._options(merged)

    def _run(self, ids, options, stream):
        state = {}
        pieces = self.model._pieces(ids, state, **options)
        if stream:
            return pieces
        text = "".join(pieces)
        usage = {"prompt_tokens": state["prompt_tokens"],
                 "completion_tokens": state["completion_tokens"]}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        return {"text": text, "finish_reason": state["finish_reason"], "usage": usage}

    def complete(self, prompt, *, stream=False, **options):
        return self._run(self.model.encode(str(prompt)), self._options(options), stream)

    def chat(self, messages, *, stream=False, enable_thinking=None, system=None, **options):
        thinking = self.defaults.get("enable_thinking", True)
        if enable_thinking is not None:
            thinking = enable_thinking
        ids = self.model.encode_chat(messages, enable_thinking=bool(thinking), system=system)
        options = self._options(options)
        options.pop("enable_thinking", None)
        return self._run(ids, options, stream)

    def close(self):
        self.model.close()

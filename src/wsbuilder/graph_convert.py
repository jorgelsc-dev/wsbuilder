"""Convert Hugging Face and GGUF models to the wsbuilder graph format.

:func:`convert_to_graph` reads a Hugging Face model directory
(``config.json``, ``model.safetensors`` or ``model.safetensors.index.json``,
``tokenizer.json``) or a GGUF file and writes ``model.json``,
``weights.ndjson`` and ``tokenizer.json`` for :class:`~wsbuilder.graph.GraphModel`.

Each supported architecture is a function that builds the graph JSON from a
few hyperparameters (:data:`ARCHITECTURES`); tensors are renamed to the GGUF
names (``token_embd.weight``, ``blk.{i}.attn_q.weight``...) whatever the
source, and copied byte for byte when the dtype does not change.

Rotary embeddings: Hugging Face checkpoints rotate pairs ``(i, i + d/2)``
(``rotate_half``, style ``neox``). llama.cpp's converter permutes the Q and
K rows of **llama** models so that GGUF files rotate neighbouring pairs
``(2i, 2i + 1)``: those graphs use the ``interleaved`` style and keep the
permuted weights. Qwen2/Qwen3 GGUF files are not permuted (llama.cpp runs
them with its NEOX rope), so they use ``neox`` like Hugging Face.
"""

import array
import json
import math
import os
import struct
import sys
import uuid

from .graph import GRAPH_FORMAT, GRAPH_FORMAT_VERSION

__all__ = ["ARCHITECTURES", "convert_to_graph"]

_TARGET_DTYPES = ("F32", "F16", "BF16")
_KEPT_DTYPES = ("F32", "F16", "BF16", "Q8_0", "Q4_0")
_LITTLE = sys.byteorder == "little"

# Hugging Face name -> GGUF name ({i} is the block number).
_HF_BLOCK_NAMES = {
    "input_layernorm.weight": "attn_norm.weight",
    "self_attn.q_proj.weight": "attn_q.weight",
    "self_attn.q_proj.bias": "attn_q.bias",
    "self_attn.k_proj.weight": "attn_k.weight",
    "self_attn.k_proj.bias": "attn_k.bias",
    "self_attn.v_proj.weight": "attn_v.weight",
    "self_attn.v_proj.bias": "attn_v.bias",
    "self_attn.o_proj.weight": "attn_output.weight",
    "self_attn.o_proj.bias": "attn_output.bias",
    "self_attn.q_norm.weight": "attn_q_norm.weight",
    "self_attn.k_norm.weight": "attn_k_norm.weight",
    "post_attention_layernorm.weight": "ffn_norm.weight",
    "mlp.gate_proj.weight": "ffn_gate.weight",
    "mlp.up_proj.weight": "ffn_up.weight",
    "mlp.down_proj.weight": "ffn_down.weight",
}
_HF_GLOBAL_NAMES = {
    "model.embed_tokens.weight": "token_embd.weight",
    "model.norm.weight": "output_norm.weight",
    "lm_head.weight": "output.weight",
}


def _hf_to_gguf_name(name):
    if name in _HF_GLOBAL_NAMES:
        return _HF_GLOBAL_NAMES[name]
    parts = name.split(".", 3)
    if len(parts) == 4 and parts[:2] == ["model", "layers"] and parts[2].isdigit():
        suffix = _HF_BLOCK_NAMES.get(parts[3])
        if suffix is not None:
            return f"blk.{parts[2]}.{suffix}"
    return None


# -- architectures ---------------------------------------------------------


def _decoder_graph(p):
    """The graph of a pre-norm decoder with GQA attention, RoPE and a SwiGLU MLP.

    ``p`` holds the hyperparameters and the flags of the architecture:
    ``bias`` (Q/K/V biases), ``out_bias`` (output projection bias), ``head_norm`` (Qwen3 Q/K RMSNorm per head),
    ``rope_style``, ``freq_factors``, ``rope_scaling``, ``sliding_window`` and
    ``tied``.
    """
    eps, hd = p["eps"], p["head_dim"]
    rope = {"head_dim": hd, "style": p["rope_style"], "theta": p["theta"]}
    if p.get("rotary_dim") and p["rotary_dim"] != hd:
        rope["rotary_dim"] = p["rotary_dim"]
    if p.get("rope_scaling"):
        rope["scaling"] = p["rope_scaling"]
    if p.get("freq_factors"):
        rope["freq_factors"] = "rope_freqs.weight"
    ops = [{"op": "rms_norm", "in": ["x"], "out": "h", "weight": "blk.{i}.attn_norm.weight",
            "eps": eps}]
    for name in ("q", "k", "v"):
        op = {"op": "linear", "in": ["h"], "out": name, "weight": f"blk.{{i}}.attn_{name}.weight"}
        if p.get("bias"):
            op["bias"] = f"blk.{{i}}.attn_{name}.bias"
        ops.append(op)
    if p.get("head_norm"):
        for name in ("q", "k"):
            ops.append({"op": "head_rms_norm", "in": [name], "out": name, "head_dim": hd,
                        "weight": f"blk.{{i}}.attn_{name}_norm.weight", "eps": eps})
    for name in ("q", "k"):
        ops.append({"op": "rope", "in": [name], "out": name, **rope})
    attention = {"op": "attention", "in": ["q", "k", "v"], "out": "a", "heads": p["heads"],
                 "kv_heads": p["kv_heads"], "head_dim": hd}
    if p.get("sliding_window"):
        attention["sliding_window"] = p["sliding_window"]
    ops += [
        attention,
        dict({"op": "linear", "in": ["a"], "out": "a", "weight": "blk.{i}.attn_output.weight"},
             **({"bias": "blk.{i}.attn_output.bias"} if p.get("out_bias") else {})),
        {"op": "add", "in": ["x", "a"], "out": "x"},
        {"op": "rms_norm", "in": ["x"], "out": "h", "weight": "blk.{i}.ffn_norm.weight",
         "eps": eps},
        {"op": "linear", "in": ["h"], "out": "g", "weight": "blk.{i}.ffn_gate.weight"},
        {"op": "linear", "in": ["h"], "out": "u", "weight": "blk.{i}.ffn_up.weight"},
        {"op": "silu", "in": ["g"], "out": "g"},
        {"op": "mul", "in": ["g", "u"], "out": "g"},
        {"op": "linear", "in": ["g"], "out": "m", "weight": "blk.{i}.ffn_down.weight"},
        {"op": "add", "in": ["x", "m"], "out": "x"},
    ]
    head = [
        {"op": "rms_norm", "in": ["x"], "out": "x", "weight": "output_norm.weight", "eps": eps},
        {"op": "linear", "in": ["x"], "out": "logits",
         "weight": "token_embd.weight" if p["tied"] else "output.weight"},
    ]
    return {"embed": {"weight": "token_embd.weight"},
            "blocks": {"count": p["layers"], "ops": ops},
            "head": {"ops": head}}


def _llama(p):
    """Llama 2/3 and Mistral: no biases, interleaved RoPE in GGUF files."""
    return _decoder_graph(p)


def _qwen2(p):
    """Qwen2/Qwen2.5: biases on Q, K and V."""
    return _decoder_graph(dict(p, bias=True))


def _qwen3(p):
    """Qwen3: RMSNorm of each Q and K head before RoPE, no biases."""
    return _decoder_graph(dict(p, bias=False, head_norm=True))


#: architecture -> (graph builder, chat template, RoPE style in GGUF files)
ARCHITECTURES = {
    "llama": (_llama, None, "interleaved"),
    "mistral": (_llama, None, "interleaved"),
    "qwen2": (_qwen2, "chatml", "neox"),
    "qwen3": (_qwen3, "qwen3", "neox"),
}


def _unsupported(architecture):
    return NotImplementedError(
        f"architecture {architecture!r} cannot be converted yet; supported: "
        f"{', '.join(sorted(ARCHITECTURES))}"
    )


# -- hyperparameters -------------------------------------------------------


def _positive_int(value, what):
    if type(value) is not int or value < 1:
        raise ValueError(f"{what} must be a positive integer, not {value!r}")
    return value


def _positive_float(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError(f"{what} must be a positive number, not {value!r}")
    return float(value)


def _llama3_freq_factors(scaling, theta, rotary_dim):
    """llama 3.1 RoPE scaling as per-frequency divisors (like llama.cpp's rope_freqs)."""
    factor = _positive_float(scaling.get("factor"), "rope_scaling.factor")
    low = _positive_float(scaling.get("low_freq_factor", 1.0), "rope_scaling.low_freq_factor")
    high = _positive_float(scaling.get("high_freq_factor", 4.0),
                           "rope_scaling.high_freq_factor")
    original = _positive_int(scaling.get("original_max_position_embeddings", 8192),
                             "rope_scaling.original_max_position_embeddings")
    if high <= low:
        raise ValueError("rope_scaling.high_freq_factor must be larger than low_freq_factor")
    low_wavelen = original / low
    high_wavelen = original / high
    factors = []
    for index in range(rotary_dim // 2):
        wavelen = 2 * math.pi / theta ** (-2.0 * index / rotary_dim)
        if wavelen < high_wavelen:
            factors.append(1.0)
        elif wavelen > low_wavelen:
            factors.append(factor)
        else:
            smooth = (original / wavelen - low) / (high - low)
            factors.append(1.0 / ((1 - smooth) / factor + smooth))
    return factors


def _hf_params(config):
    architecture = config.get("model_type")
    if architecture not in ARCHITECTURES:
        raise _unsupported(architecture)
    if config.get("hidden_act", "silu") != "silu":
        raise NotImplementedError(f"hidden_act {config.get('hidden_act')!r} is not supported")
    if config.get("mlp_bias"):
        raise NotImplementedError("MLP biases are not supported")
    hidden = _positive_int(config.get("hidden_size"), "hidden_size")
    heads = _positive_int(config.get("num_attention_heads"), "num_attention_heads")
    head_dim = config.get("head_dim") or hidden // heads
    rope_parameters = config.get("rope_parameters") or {}
    theta = rope_parameters.get("rope_theta", config.get("rope_theta", 10000.0))
    scaling = config.get("rope_scaling") or (
        rope_parameters if rope_parameters.get("rope_type", "default") != "default" else None)
    p = {
        "architecture": architecture,
        "hidden": hidden,
        "layers": _positive_int(config.get("num_hidden_layers"), "num_hidden_layers"),
        "heads": heads,
        "kv_heads": _positive_int(config.get("num_key_value_heads") or heads,
                                  "num_key_value_heads"),
        "head_dim": _positive_int(head_dim, "head_dim"),
        "eps": _positive_float(config.get("rms_norm_eps", 1e-6), "rms_norm_eps"),
        "theta": _positive_float(theta, "rope_theta"),
        "tied": bool(config.get("tie_word_embeddings", False)),
        "context": config.get("max_position_embeddings"),
        "rope_style": "neox",
    }
    p["rotary_dim"] = int(p["head_dim"] * config.get("partial_rotary_factor", 1.0))
    if scaling:
        kind = scaling.get("rope_type", scaling.get("type"))
        if kind == "linear":
            p["rope_scaling"] = {"type": "linear",
                                 "factor": _positive_float(scaling.get("factor"),
                                                           "rope_scaling.factor")}
        elif kind == "llama3":
            p["freq_factors"] = _llama3_freq_factors(scaling, p["theta"], p["rotary_dim"])
        elif kind not in (None, "default"):
            raise NotImplementedError(f"RoPE scaling {kind!r} is not supported")
    if architecture == "mistral" and config.get("sliding_window"):
        p["sliding_window"] = _positive_int(config["sliding_window"], "sliding_window")
    if architecture == "qwen2" and config.get("use_sliding_window"):
        raise NotImplementedError("Qwen2 sliding-window attention is not supported")
    if architecture == "llama" and config.get("attention_bias"):
        p["bias"] = True
    return p


def _gguf_params(metadata, reader):
    architecture = metadata.get("general.architecture")
    if architecture not in ARCHITECTURES:
        raise _unsupported(architecture)

    def key(name, default=None):
        value = metadata.get(f"{architecture}.{name}", default)
        if value is None:
            raise ValueError(f"GGUF metadata needs {architecture}.{name}")
        return value

    hidden = _positive_int(key("embedding_length"), "embedding_length")
    heads = _positive_int(key("attention.head_count"), "attention.head_count")
    head_dim = _positive_int(key("attention.key_length", hidden // heads),
                             "attention.key_length")
    p = {
        "architecture": architecture,
        "hidden": hidden,
        "layers": _positive_int(key("block_count"), "block_count"),
        "heads": heads,
        "kv_heads": _positive_int(key("attention.head_count_kv", heads),
                                  "attention.head_count_kv"),
        "head_dim": head_dim,
        "rotary_dim": _positive_int(key("rope.dimension_count", head_dim),
                                    "rope.dimension_count"),
        "eps": _positive_float(key("attention.layer_norm_rms_epsilon", 1e-6),
                               "attention.layer_norm_rms_epsilon"),
        "theta": _positive_float(key("rope.freq_base", 10000.0), "rope.freq_base"),
        "tied": "output.weight" not in reader,
        "context": metadata.get(f"{architecture}.context_length"),
        "rope_style": ARCHITECTURES[architecture][2],
        "bias": "blk.0.attn_q.bias" in reader,
        "out_bias": "blk.0.attn_output.bias" in reader,
        "freq_factors": "rope_freqs.weight" in reader,
    }
    kind = metadata.get(f"{architecture}.rope.scaling.type")
    if kind == "linear":
        p["rope_scaling"] = {"type": "linear", "factor": _positive_float(
            key("rope.scaling.factor"), "rope.scaling.factor")}
    elif kind not in (None, "none"):
        raise NotImplementedError(f"RoPE scaling {kind!r} is not supported")
    window = metadata.get(f"{architecture}.attention.sliding_window")
    if window:
        p["sliding_window"] = _positive_int(window, "attention.sliding_window")
    return p


# -- tensors ---------------------------------------------------------------


def _graph_tensors(graph):
    """Every tensor name the graph uses, with ``{i}`` expanded."""
    names = [graph["embed"]["weight"]]
    keys = ("weight", "bias", "freq_factors")
    for block in range(graph["blocks"]["count"]):
        for op in graph["blocks"]["ops"]:
            names += [op[key].replace("{i}", str(block)) for key in keys if op.get(key)]
    for op in graph["head"]["ops"]:
        names += [op[key] for key in keys if op.get(key)]
    return list(dict.fromkeys(names))


def _float_bytes(values, dtype):
    """Little-endian ``dtype`` bytes of an ``array("f")`` (or a list of floats)."""
    if dtype == "F32":
        data = array.array("f", values)
        if not _LITTLE:
            data.byteswap()
        return data.tobytes()
    if dtype == "F16":
        out = bytearray()
        for start in range(0, len(values), 1 << 16):
            chunk = values[start:start + (1 << 16)]
            try:
                out += struct.pack(f"<{len(chunk)}e", *chunk)
            except (OverflowError, struct.error):
                out += b"".join(_f16(value) for value in chunk)
        return bytes(out)
    bits = array.array("I")
    bits.frombytes(_float_bytes(values, "F32"))
    if not _LITTLE:
        bits.byteswap()
    # bfloat16 = the float32 rounded to nearest even on its high 16 bits (NaN kept).
    half = array.array("H", (
        (value >> 16) | 0x40 if (value & 0x7F800000) == 0x7F800000 and value & 0x7FFFFF
        else ((value + 0x7FFF + ((value >> 16) & 1)) >> 16) & 0xFFFF
        for value in bits
    ))
    if not _LITTLE:
        half.byteswap()
    return half.tobytes()


def _f16(value):
    try:
        return struct.pack("<e", value)
    except (OverflowError, struct.error):
        return struct.pack("<e", math.copysign(math.inf, value))


class _Source:
    """A tensor of the source model, renamed and converted on demand."""

    def __init__(self, tensor, dtype, shape):
        self.tensor = tensor
        self.dtype = dtype
        self.shape = shape

    def __call__(self):
        if self.dtype == self.tensor.dtype:
            with self.tensor.raw() as view:
                return bytes(view)
        return _float_bytes(self.tensor.to_array(), self.dtype)


def _target_dtype(tensor, dtype):
    if len(tensor.shape) < 2:
        return "F32"  # norms and biases are tiny: keep them exact
    if dtype is not None:
        return dtype
    return tensor.dtype if tensor.dtype in _KEPT_DTYPES else "F32"


# -- sources ---------------------------------------------------------------


def _read_json(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _ids(value):
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [item for item in values if type(item) is int and item >= 0]


def _adds_bos(tokenizer_data, bos_token_id):
    """Whether a tokenizer.json post-processor puts the BOS token first (Llama 3)."""
    def visit(processor):
        if not isinstance(processor, dict):
            return False
        if processor.get("type") == "Sequence":
            return any(visit(item) for item in processor.get("processors") or ())
        if processor.get("type") == "TemplateProcessing":
            single = processor.get("single") or []
            first = single[0] if single else {}
            special = first.get("SpecialToken") if isinstance(first, dict) else None
            tokens = processor.get("special_tokens") or {}
            if isinstance(special, dict) and special.get("id") in tokens:
                return bos_token_id in (tokens[special["id"]].get("ids") or [])
        return False
    return visit(tokenizer_data.get("post_processor"))


def _from_hf_dir(source):
    from .bpe import BPETokenizer
    from .weights import open_weights

    config = _read_json(os.path.join(source, "config.json"))
    params = _hf_params(config)
    index = os.path.join(source, "model.safetensors.index.json")
    single = os.path.join(source, "model.safetensors")
    weights_path = index if os.path.exists(index) else single
    if not os.path.exists(weights_path):
        raise FileNotFoundError(f"{source!r} has no model.safetensors or "
                                f"model.safetensors.index.json")
    tokenizer = BPETokenizer.from_pretrained_dir(source)
    tokenizer_data = _read_json(os.path.join(source, "tokenizer.json"))
    generation = {}
    generation_path = os.path.join(source, "generation_config.json")
    if os.path.exists(generation_path):
        generation = _read_json(generation_path)
    reader = open_weights(weights_path)
    tensors = {}
    for name in reader.names():
        target = _hf_to_gguf_name(name)
        if target is not None:
            tensors[target] = name
        elif not name.endswith("rotary_emb.inv_freq"):
            reader.close()
            raise NotImplementedError(f"tensor {name!r} has no place in the "
                                      f"{params['architecture']} graph")
    if "blk.0.attn_q.bias" in tensors:
        params["bias"] = True
    if "blk.0.attn_output.bias" in tensors:
        params["out_bias"] = True
    eos = _ids(tokenizer.eos_token_id) + _ids(config.get("eos_token_id"))
    eos += _ids(generation.get("eos_token_id"))
    add_bos = tokenizer.add_bos_token or (
        tokenizer.bos_token_id is not None and _adds_bos(tokenizer_data, tokenizer.bos_token_id))
    defaults = {}
    for key, target in (("temperature", "temperature"), ("top_k", "top_k"), ("top_p", "top_p"),
                        ("repetition_penalty", "repeat_penalty")):
        if generation.get("do_sample", True) and isinstance(generation.get(key), (int, float)):
            defaults[target] = generation[key]
    return {
        "params": params, "reader": reader, "tensors": tensors, "tokenizer": tokenizer,
        "tokenizer_json": tokenizer_data, "eos": eos, "add_bos": add_bos, "generation": defaults,
        "name": os.path.basename(os.path.normpath(source)),
    }


def _from_gguf(source):
    from .bpe import BPETokenizer
    from .weights import GGUFReader

    reader = GGUFReader(source)
    try:
        params = _gguf_params(reader.metadata, reader)
        tokenizer = BPETokenizer.from_gguf_metadata(reader.metadata)
    except BaseException:
        reader.close()
        raise
    metadata = reader.metadata
    eos = _ids(tokenizer.eos_token_id) + _ids(metadata.get("tokenizer.ggml.eot_token_id"))
    return {
        "params": params, "reader": reader, "tensors": {name: name for name in reader.names()},
        "tokenizer": tokenizer, "tokenizer_json": tokenizer.to_json(), "eos": eos,
        "add_bos": tokenizer.add_bos_token and tokenizer.bos_token_id is not None,
        "generation": {}, "name": reader.name or os.path.splitext(os.path.basename(source))[0],
    }


def _chat_template(architecture, tokenizer):
    template = ARCHITECTURES[architecture][1]
    if template is None and tokenizer.token_to_id("<|start_header_id|>") is not None:
        template = "llama3"
    if template in ("qwen3", "chatml") and tokenizer.token_to_id("<|im_start|>") is None:
        template = None
    return template


def _write_json(path, data):
    temporary = f"{path}.tmp-{uuid.uuid4().hex}"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def convert_to_graph(source, out_dir, *, dtype=None, name=None):
    """Convert a model to ``out_dir/model.json`` (plus weights and tokenizer); returns its path.

    ``source`` is a Hugging Face model directory or a ``.gguf`` file of a
    supported architecture (:data:`ARCHITECTURES`; others raise
    ``NotImplementedError``). ``dtype`` (``"F32"``, ``"F16"`` or ``"BF16"``)
    converts the matrices; by default they keep their dtype when the runtime
    can read it (F32, F16, BF16, Q8_0, Q4_0) and become F32 otherwise. Norm
    and bias vectors are always F32. Tensors are copied one at a time.
    """
    from .ndjson_model import ModelWriter

    if dtype is not None and dtype not in _TARGET_DTYPES:
        raise ValueError(f"dtype must be None or one of {_TARGET_DTYPES}")
    source = os.fspath(source)
    loaded = _from_hf_dir(source) if os.path.isdir(source) else _from_gguf(source)
    reader = loaded["reader"]
    try:
        params = loaded["params"]
        architecture = params["architecture"]
        builder = ARCHITECTURES[architecture][0]
        graph = builder(params)
        used = _graph_tensors(graph)
        # A source tensor the graph does not use means a feature it lacks: refuse
        # instead of computing wrong logits.
        ignored = set(used) | ({"output.weight"} if params["tied"] else set())
        unused = sorted(set(loaded["tensors"]) - ignored)
        if unused:
            raise NotImplementedError(
                f"the {architecture} graph does not use {', '.join(unused[:5])}"
                f"{' ...' if len(unused) > 5 else ''}; this model variant is not supported"
            )
        writer = ModelWriter(architecture=architecture, name=name or loaded["name"])
        for tensor_name in used:
            if tensor_name == "rope_freqs.weight" and isinstance(params.get("freq_factors"),
                                                                  list):
                factors = params["freq_factors"]
                writer.add_tensor_bytes(tensor_name, _float_bytes(factors, "F32"),
                                        shape=(len(factors),), dtype="F32")
                continue
            source_name = loaded["tensors"].get(tensor_name)
            if source_name is None:
                raise ValueError(f"the source model has no tensor for {tensor_name!r}")
            tensor = reader.tensor(source_name)
            target = _target_dtype(tensor, dtype)
            writer.add_tensor_bytes(tensor_name, _Source(tensor, target, tensor.shape),
                                    shape=tensor.shape, dtype=target)
        tokenizer = loaded["tokenizer"]
        embed = reader.tensor(loaded["tensors"]["token_embd.weight"])
        config = {
            "vocab_size": embed.shape[0],
            "hidden_size": params["hidden"],
            "num_layers": params["layers"],
            "context_length": params.get("context") or None,
            "bos_token_id": tokenizer.bos_token_id,
            "add_bos_token": bool(loaded["add_bos"]),
            "eos_token_ids": list(dict.fromkeys(loaded["eos"])),
        }
        spec = {
            "format": GRAPH_FORMAT,
            "version": GRAPH_FORMAT_VERSION,
            "name": writer.name,
            "architecture": architecture,
            "weights": "weights.ndjson",
            "tokenizer": {"type": "bpe", "file": "tokenizer.json"},
            "chat_template": _chat_template(architecture, tokenizer),
            "config": config,
            "generation": loaded["generation"],
            "graph": graph,
        }
        os.makedirs(out_dir, exist_ok=True)
        writer.write(os.path.join(out_dir, "weights.ndjson"))
        _write_json(os.path.join(out_dir, "tokenizer.json"), loaded["tokenizer_json"])
        model_path = os.path.join(out_dir, "model.json")
        _write_json(model_path, spec)
        return model_path
    finally:
        reader.close()

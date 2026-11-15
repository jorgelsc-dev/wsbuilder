"""Read and write GGUF, the model file format of llama.cpp and Ollama.

A GGUF file is a header, a table of typed metadata (architecture, context
length, tokenizer, chat template, ...), a table of tensor descriptions and,
aligned after them, the raw tensor data. This module reads the header and
the tables without loading the tensor data, which for a language model is
gigabytes, and writes new files from metadata and tensors.

Everything here uses only the standard library. Inputs are treated as
untrusted: every count and length is checked against what the file can
actually hold before anything is allocated.

Reading::

    with GGUFReader("model.gguf") as model:
        model.metadata["general.architecture"]     # 'llama'
        model.summary()                              # name, size, context, ...
        model.tensor("token_embd.weight").shape

Writing::

    writer = GGUFWriter(architecture="wsbuilder.dense")
    writer.add_metadata("general.name", "tiny")
    writer.add_tensor("layer.0.weight", [0.1, 0.2, 0.3, 0.4], shape=(2, 2))
    writer.write("tiny.gguf")
"""

import io
import os
import struct
from array import array

__all__ = [
    "GGMLType",
    "GGUFArray",
    "GGUFError",
    "GGUFReader",
    "GGUFTensor",
    "GGUFValueType",
    "GGUFWriter",
    "GGUF_MAGIC",
    "describe_file_type",
    "read_gguf_metadata",
]

GGUF_MAGIC = b"GGUF"
#: Versions this reader understands. Version 1 used 32-bit counts and is
#: long obsolete; 2 and 3 share the layout read here.
SUPPORTED_VERSIONS = (2, 3)
WRITE_VERSION = 3
DEFAULT_ALIGNMENT = 32

#: Arrays longer than this are not materialized unless asked for: a
#: tokenizer vocabulary holds 100k+ strings that a summary never needs.
DEFAULT_MAX_ARRAY_ITEMS = 1024
#: A metadata string longer than this is refused (chat templates are a few KB).
MAX_STRING_BYTES = 64 * 1024 * 1024
MAX_TENSOR_DIMS = 8


class GGUFError(ValueError):
    """A file that is not well-formed GGUF, or a value GGUF cannot hold."""


class GGUFValueType:
    UINT8 = 0
    INT8 = 1
    UINT16 = 2
    INT16 = 3
    UINT32 = 4
    INT32 = 5
    FLOAT32 = 6
    BOOL = 7
    STRING = 8
    ARRAY = 9
    UINT64 = 10
    INT64 = 11
    FLOAT64 = 12


_SCALAR_FORMATS = {
    GGUFValueType.UINT8: "<B",
    GGUFValueType.INT8: "<b",
    GGUFValueType.UINT16: "<H",
    GGUFValueType.INT16: "<h",
    GGUFValueType.UINT32: "<I",
    GGUFValueType.INT32: "<i",
    GGUFValueType.FLOAT32: "<f",
    GGUFValueType.BOOL: "<?",
    GGUFValueType.UINT64: "<Q",
    GGUFValueType.INT64: "<q",
    GGUFValueType.FLOAT64: "<d",
}
_SCALAR_STRUCTS = {kind: struct.Struct(fmt) for kind, fmt in _SCALAR_FORMATS.items()}
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")


class GGMLType:
    """Tensor element types, with their block geometry for size checks."""

    F32 = 0
    F16 = 1
    Q4_0 = 2
    Q4_1 = 3
    Q5_0 = 6
    Q5_1 = 7
    Q8_0 = 8
    Q8_1 = 9
    Q2_K = 10
    Q3_K = 11
    Q4_K = 12
    Q5_K = 13
    Q6_K = 14
    Q8_K = 15
    IQ2_XXS = 16
    IQ2_XS = 17
    IQ3_XXS = 18
    IQ1_S = 19
    IQ4_NL = 20
    IQ3_S = 21
    IQ2_S = 22
    IQ4_XS = 23
    I8 = 24
    I16 = 25
    I32 = 26
    I64 = 27
    F64 = 28
    IQ1_M = 29
    BF16 = 30
    TQ1_0 = 34
    TQ2_0 = 35

    #: type -> (name, elements per block, bytes per block)
    INFO = {
        0: ("F32", 1, 4),
        1: ("F16", 1, 2),
        2: ("Q4_0", 32, 18),
        3: ("Q4_1", 32, 20),
        6: ("Q5_0", 32, 22),
        7: ("Q5_1", 32, 24),
        8: ("Q8_0", 32, 34),
        9: ("Q8_1", 32, 36),
        10: ("Q2_K", 256, 84),
        11: ("Q3_K", 256, 110),
        12: ("Q4_K", 256, 144),
        13: ("Q5_K", 256, 176),
        14: ("Q6_K", 256, 210),
        15: ("Q8_K", 256, 292),
        16: ("IQ2_XXS", 256, 66),
        17: ("IQ2_XS", 256, 74),
        18: ("IQ3_XXS", 256, 98),
        19: ("IQ1_S", 256, 50),
        20: ("IQ4_NL", 32, 18),
        21: ("IQ3_S", 256, 110),
        22: ("IQ2_S", 256, 82),
        23: ("IQ4_XS", 256, 136),
        24: ("I8", 1, 1),
        25: ("I16", 1, 2),
        26: ("I32", 1, 4),
        27: ("I64", 1, 8),
        28: ("F64", 1, 8),
        29: ("IQ1_M", 256, 56),
        30: ("BF16", 1, 2),
        34: ("TQ1_0", 256, 54),
        35: ("TQ2_0", 256, 66),
    }

    @classmethod
    def name(cls, ggml_type):
        info = cls.INFO.get(int(ggml_type))
        return info[0] if info else f"UNKNOWN_{int(ggml_type)}"

    @classmethod
    def from_name(cls, name):
        wanted = str(name).upper()
        for value, (label, _block, _size) in cls.INFO.items():
            if label == wanted:
                return value
        raise GGUFError(f"unknown GGML type {name!r}")

    @classmethod
    def nbytes(cls, ggml_type, element_count):
        """Bytes ``element_count`` elements take, or None for an unknown type."""
        info = cls.INFO.get(int(ggml_type))
        if info is None:
            return None
        _name, block, size = info
        if element_count % block:
            raise GGUFError(
                f"{info[0]} stores elements in blocks of {block}; {element_count} is not a multiple"
            )
        return element_count // block * size


#: general.file_type values (llama.cpp's LLAMA_FTYPE_*), for summaries.
_FILE_TYPES = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    7: "Q8_0",
    8: "Q5_0",
    9: "Q5_1",
    10: "Q2_K",
    11: "Q3_K_S",
    12: "Q3_K_M",
    13: "Q3_K_L",
    14: "Q4_K_S",
    15: "Q4_K_M",
    16: "Q5_K_S",
    17: "Q5_K_M",
    18: "Q6_K",
    19: "IQ2_XXS",
    20: "IQ2_XS",
    21: "Q2_K_S",
    22: "IQ3_XS",
    23: "IQ3_XXS",
    24: "IQ1_S",
    25: "IQ4_NL",
    26: "IQ3_S",
    27: "IQ3_M",
    28: "IQ2_S",
    29: "IQ2_M",
    30: "IQ4_XS",
    31: "IQ1_M",
    32: "BF16",
    36: "TQ1_0",
    37: "TQ2_0",
}


def describe_file_type(value):
    """The quantization name for a ``general.file_type`` value."""
    if value is None:
        return None
    return _FILE_TYPES.get(int(value), f"UNKNOWN_{int(value)}")


class GGUFArray:
    """An array left unread because it is long; its type and length are known."""

    __slots__ = ("item_type", "count")

    def __init__(self, item_type, count):
        self.item_type = int(item_type)
        self.count = int(count)

    def __len__(self):
        return self.count

    def __repr__(self):
        return f"<GGUFArray type={self.item_type} count={self.count}>"


class GGUFTensor:
    """One tensor's description: name, shape, element type and where it lives."""

    __slots__ = ("name", "shape", "ggml_type", "offset", "nbytes", "_reader")

    def __init__(self, name, shape, ggml_type, offset, nbytes, reader=None):
        self.name = name
        #: GGUF lists dimensions fastest-varying first (ne[0] is the row length).
        self.shape = tuple(shape)
        self.ggml_type = int(ggml_type)
        #: Absolute file offset of the data.
        self.offset = int(offset)
        self.nbytes = nbytes
        self._reader = reader

    @property
    def type_name(self):
        return GGMLType.name(self.ggml_type)

    @property
    def element_count(self):
        count = 1
        for dim in self.shape:
            count *= dim
        return count

    def read(self):
        """The raw bytes of this tensor."""
        if self._reader is None:
            raise GGUFError("this tensor is not attached to an open file")
        return self._reader.read_tensor_bytes(self)

    def values(self):
        """The elements as Python floats (or ints), dequantizing when supported."""
        return decode_tensor(self.read(), self.ggml_type, self.element_count)

    def describe(self):
        return {
            "name": self.name,
            "shape": list(self.shape),
            "type": self.type_name,
            "elements": self.element_count,
            "bytes": self.nbytes,
        }

    def __repr__(self):
        return f"<GGUFTensor {self.name} {self.type_name} {list(self.shape)}>"


# -- decoding tensor data ---------------------------------------------------


def _floats_from_half(raw):
    count = len(raw) // 2
    return list(struct.unpack(f"<{count}e", raw))


def _floats_from_bf16(raw):
    count = len(raw) // 2
    halves = struct.unpack(f"<{count}H", raw)
    widened = struct.pack(f"<{count}I", *(half << 16 for half in halves))
    return list(struct.unpack(f"<{count}f", widened))


def _dequantize_q8_0(raw, count):
    out = []
    for block in range(count // 32):
        base = block * 34
        (scale,) = struct.unpack_from("<e", raw, base)
        quants = struct.unpack_from("<32b", raw, base + 2)
        out.extend(scale * q for q in quants)
    return out


def _dequantize_q4_0(raw, count):
    out = []
    for block in range(count // 32):
        base = block * 18
        (scale,) = struct.unpack_from("<e", raw, base)
        packed = raw[base + 2 : base + 18]
        # Low nibbles hold elements 0..15, high nibbles 16..31.
        out.extend(scale * ((byte & 0x0F) - 8) for byte in packed)
        out.extend(scale * ((byte >> 4) - 8) for byte in packed)
    return out


_DECODERS = {
    GGMLType.F32: lambda raw, count: list(struct.unpack(f"<{count}f", raw)),
    GGMLType.F64: lambda raw, count: list(struct.unpack(f"<{count}d", raw)),
    GGMLType.F16: lambda raw, count: _floats_from_half(raw),
    GGMLType.BF16: lambda raw, count: _floats_from_bf16(raw),
    GGMLType.I8: lambda raw, count: list(struct.unpack(f"<{count}b", raw)),
    GGMLType.I16: lambda raw, count: list(struct.unpack(f"<{count}h", raw)),
    GGMLType.I32: lambda raw, count: list(struct.unpack(f"<{count}i", raw)),
    GGMLType.I64: lambda raw, count: list(struct.unpack(f"<{count}q", raw)),
    GGMLType.Q8_0: _dequantize_q8_0,
    GGMLType.Q4_0: _dequantize_q4_0,
}


def decode_tensor(raw, ggml_type, element_count):
    """Turn raw tensor bytes into numbers.

    Plain types and the Q8_0/Q4_0 quantizations are supported; the K and IQ
    quantizations are for an inference engine and raise GGUFError here.
    """
    decoder = _DECODERS.get(int(ggml_type))
    if decoder is None:
        raise GGUFError(f"decoding {GGMLType.name(ggml_type)} tensors is not supported")
    expected = GGMLType.nbytes(ggml_type, element_count)
    if expected != len(raw):
        raise GGUFError("tensor data does not match its shape and type")
    return decoder(bytes(raw), element_count)


def encode_tensor(values, ggml_type):
    """Numbers to raw bytes for the plain types (F32, F16, BF16, F64, I8..I64)."""
    values = list(values)
    count = len(values)
    if ggml_type == GGMLType.F32:
        return struct.pack(f"<{count}f", *values)
    if ggml_type == GGMLType.F64:
        return struct.pack(f"<{count}d", *values)
    if ggml_type == GGMLType.F16:
        return struct.pack(f"<{count}e", *values)
    if ggml_type == GGMLType.BF16:
        words = struct.unpack(f"<{count}I", struct.pack(f"<{count}f", *values))
        # Round to nearest even on the dropped 16 bits.
        rounded = [((word + 0x7FFF + ((word >> 16) & 1)) >> 16) & 0xFFFF for word in words]
        return struct.pack(f"<{count}H", *rounded)
    formats = {GGMLType.I8: "b", GGMLType.I16: "h", GGMLType.I32: "i", GGMLType.I64: "q"}
    if ggml_type in formats:
        return struct.pack(f"<{count}{formats[ggml_type]}", *(int(v) for v in values))
    raise GGUFError(f"encoding {GGMLType.name(ggml_type)} tensors is not supported")


# -- reading ----------------------------------------------------------------


class GGUFReader:
    """Parse the metadata and tensor table of a GGUF file.

    ``source`` is a path or a binary file object that supports ``seek``.
    Tensor data is read only on request. Arrays longer than
    ``max_array_items`` are kept as :class:`GGUFArray` placeholders (pass
    ``None`` to read them all).
    """

    def __init__(self, source, *, max_array_items=DEFAULT_MAX_ARRAY_ITEMS):
        self._owned = False
        if isinstance(source, (str, os.PathLike)):
            self.path = os.fspath(source)
            self._file = open(self.path, "rb")
            self._owned = True
        else:
            self.path = getattr(source, "name", None)
            self._file = source
        self.max_array_items = max_array_items
        try:
            self._file.seek(0, io.SEEK_END)
            self.file_size = self._file.tell()
            self._file.seek(0)
            self._parse()
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._owned and self._file is not None:
            self._file.close()
        self._file = None

    # -- primitives ----------------------------------------------------

    def _read(self, count):
        if count < 0 or self._file.tell() + count > self.file_size:
            raise GGUFError("the file ends inside the header")
        data = self._file.read(count)
        if len(data) != count:
            raise GGUFError("the file ends inside the header")
        return data

    def _u32(self):
        return _U32.unpack(self._read(4))[0]

    def _u64(self):
        return _U64.unpack(self._read(8))[0]

    def _remaining(self):
        return self.file_size - self._file.tell()

    def _string(self):
        length = self._u64()
        if length > MAX_STRING_BYTES or length > self._remaining():
            raise GGUFError("a string is longer than the file allows")
        raw = self._read(length)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # Some vocabularies hold byte tokens that are not valid UTF-8.
            return raw.decode("utf-8", errors="surrogateescape")

    def _skip_string(self):
        length = self._u64()
        if length > self._remaining():
            raise GGUFError("a string is longer than the file allows")
        self._file.seek(length, io.SEEK_CUR)

    def _value(self, kind, depth=0):
        packer = _SCALAR_STRUCTS.get(kind)
        if packer is not None:
            return packer.unpack(self._read(packer.size))[0]
        if kind == GGUFValueType.STRING:
            return self._string()
        if kind == GGUFValueType.ARRAY:
            if depth > 4:
                raise GGUFError("arrays nest too deeply")
            item_type = self._u32()
            count = self._u64()
            item_size = _SCALAR_STRUCTS[item_type].size if item_type in _SCALAR_STRUCTS else 8
            if count * item_size > self._remaining():
                raise GGUFError("an array is longer than the file allows")
            limit = self.max_array_items
            if limit is not None and count > limit:
                self._skip_array(item_type, count, depth)
                return GGUFArray(item_type, count)
            packer = _SCALAR_STRUCTS.get(item_type)
            if packer is not None:
                raw = self._read(packer.size * count)
                return list(struct.unpack(f"<{count}{packer.format[1:]}", raw))
            return [self._value(item_type, depth + 1) for _ in range(count)]
        raise GGUFError(f"unknown metadata value type {kind}")

    def _skip_array(self, item_type, count, depth):
        packer = _SCALAR_STRUCTS.get(item_type)
        if packer is not None:
            self._file.seek(packer.size * count, io.SEEK_CUR)
            return
        if item_type == GGUFValueType.STRING:
            for _ in range(count):
                self._skip_string()
            return
        for _ in range(count):
            self._value(item_type, depth + 1)

    # -- structure -----------------------------------------------------

    def _parse(self):
        if self._read(4) != GGUF_MAGIC:
            raise GGUFError("not a GGUF file (bad magic)")
        self.version = self._u32()
        if self.version not in SUPPORTED_VERSIONS:
            raise GGUFError(f"unsupported GGUF version {self.version}")
        tensor_count = self._u64()
        kv_count = self._u64()
        # Each entry takes at least a few bytes; absurd counts are lies.
        if kv_count * 12 > self._remaining() or tensor_count * 24 > self._remaining():
            raise GGUFError("counts in the header exceed the file size")

        self.metadata = {}
        self.metadata_types = {}
        for _ in range(kv_count):
            key = self._string()
            kind = self._u32()
            self.metadata[key] = self._value(kind)
            self.metadata_types[key] = kind

        alignment = self.metadata.get("general.alignment", DEFAULT_ALIGNMENT)
        if not isinstance(alignment, int) or alignment <= 0 or alignment & (alignment - 1):
            raise GGUFError("general.alignment must be a power of two")
        self.alignment = alignment

        infos = []
        for _ in range(tensor_count):
            name = self._string()
            n_dims = self._u32()
            if n_dims > MAX_TENSOR_DIMS:
                raise GGUFError(f"tensor {name!r} has {n_dims} dimensions")
            shape = [self._u64() for _ in range(n_dims)]
            ggml_type = self._u32()
            offset = self._u64()
            infos.append((name, shape, ggml_type, offset))

        position = self._file.tell()
        self.data_offset = position + (-position % alignment)
        self.tensors = []
        self._by_name = {}
        for name, shape, ggml_type, offset in infos:
            if offset % alignment:
                raise GGUFError(f"tensor {name!r} is not aligned")
            count = 1
            for dim in shape:
                count *= dim
            nbytes = GGMLType.nbytes(ggml_type, count)
            start = self.data_offset + offset
            if nbytes is not None and start + nbytes > self.file_size:
                raise GGUFError(f"tensor {name!r} runs past the end of the file")
            if name in self._by_name:
                raise GGUFError(f"duplicate tensor name {name!r}")
            tensor = GGUFTensor(name, shape, ggml_type, start, nbytes, reader=self)
            self.tensors.append(tensor)
            self._by_name[name] = tensor

    # -- access --------------------------------------------------------

    def tensor(self, name):
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no tensor named {name!r}") from None

    def read_tensor_bytes(self, tensor):
        if self._file is None:
            raise GGUFError("the file is closed")
        if tensor.nbytes is None:
            raise GGUFError(f"unknown size for {tensor.type_name} tensor {tensor.name!r}")
        self._file.seek(tensor.offset)
        data = self._file.read(tensor.nbytes)
        if len(data) != tensor.nbytes:
            raise GGUFError(f"tensor {tensor.name!r} is truncated")
        return data

    @property
    def architecture(self):
        return self.metadata.get("general.architecture")

    def arch_value(self, suffix, default=None):
        """``{architecture}.{suffix}``, e.g. ``arch_value("context_length")``."""
        arch = self.architecture
        if not arch:
            return default
        return self.metadata.get(f"{arch}.{suffix}", default)

    @property
    def parameter_count(self):
        return sum(tensor.element_count for tensor in self.tensors)

    def summary(self):
        """What someone choosing or loading the model wants to know."""
        type_bytes = {}
        for tensor in self.tensors:
            type_bytes[tensor.type_name] = type_bytes.get(tensor.type_name, 0) + (tensor.nbytes or 0)
        dominant = max(type_bytes, key=type_bytes.get) if type_bytes else None
        tokens = self.metadata.get("tokenizer.ggml.tokens")
        template = self.metadata.get("tokenizer.chat_template")
        return {
            "format": "gguf",
            "version": self.version,
            "name": self.metadata.get("general.name"),
            "architecture": self.architecture,
            "parameters": self.parameter_count,
            "file_size": self.file_size,
            "quantization": describe_file_type(self.metadata.get("general.file_type")) or dominant,
            "context_length": self.arch_value("context_length"),
            "embedding_length": self.arch_value("embedding_length"),
            "block_count": self.arch_value("block_count"),
            "attention_heads": self.arch_value("attention.head_count"),
            "tokenizer": self.metadata.get("tokenizer.ggml.model"),
            "vocabulary_size": len(tokens) if tokens is not None else None,
            "chat_template": template if isinstance(template, str) else None,
            "license": self.metadata.get("general.license"),
            "tensors": len(self.tensors),
            "tensor_types": type_bytes,
        }


def read_gguf_metadata(source, **options):
    """Shortcut: the metadata dictionary of a GGUF file."""
    with GGUFReader(source, **options) as reader:
        return dict(reader.metadata)


# -- writing ----------------------------------------------------------------


def _infer_value_type(value):
    if isinstance(value, bool):
        return GGUFValueType.BOOL
    if isinstance(value, int):
        if 0 <= value < 1 << 32:
            return GGUFValueType.UINT32
        if -(1 << 31) <= value < 0:
            return GGUFValueType.INT32
        if 0 <= value < 1 << 64:
            return GGUFValueType.UINT64
        if -(1 << 63) <= value < 0:
            return GGUFValueType.INT64
        raise GGUFError("integer out of GGUF range")
    if isinstance(value, float):
        return GGUFValueType.FLOAT32
    if isinstance(value, str):
        return GGUFValueType.STRING
    if isinstance(value, (list, tuple)):
        return GGUFValueType.ARRAY
    raise GGUFError(f"GGUF metadata cannot hold {type(value).__name__}")


def _encode_string(text):
    raw = text.encode("utf-8", errors="surrogateescape")
    return _U64.pack(len(raw)) + raw


def _encode_value(value, kind, item_type=None):
    packer = _SCALAR_STRUCTS.get(kind)
    if packer is not None:
        try:
            return packer.pack(value)
        except struct.error as exc:
            raise GGUFError(f"{value!r} does not fit metadata type {kind}") from exc
    if kind == GGUFValueType.STRING:
        return _encode_string(str(value))
    if kind == GGUFValueType.ARRAY:
        items = list(value)
        if item_type is None:
            kinds = {_infer_value_type(item) for item in items} or {GGUFValueType.UINT32}
            if kinds <= {GGUFValueType.UINT32, GGUFValueType.INT32}:
                item_type = GGUFValueType.INT32 if GGUFValueType.INT32 in kinds else GGUFValueType.UINT32
            elif len(kinds) == 1:
                item_type = kinds.pop()
            elif kinds <= {GGUFValueType.UINT32, GGUFValueType.INT32, GGUFValueType.FLOAT32}:
                item_type = GGUFValueType.FLOAT32
            else:
                raise GGUFError("a GGUF array holds items of one type")
        body = b"".join(_encode_value(item, item_type) for item in items)
        return _U32.pack(item_type) + _U64.pack(len(items)) + body
    raise GGUFError(f"unknown metadata value type {kind}")


class GGUFWriter:
    """Build a GGUF file from metadata and tensors."""

    def __init__(self, *, architecture=None, alignment=DEFAULT_ALIGNMENT):
        if alignment <= 0 or alignment & (alignment - 1):
            raise GGUFError("alignment must be a power of two")
        self.alignment = int(alignment)
        self._metadata = {}
        self._tensors = []
        self._names = set()
        if architecture:
            self.add_metadata("general.architecture", str(architecture))
        if alignment != DEFAULT_ALIGNMENT:
            self.add_metadata("general.alignment", self.alignment, GGUFValueType.UINT32)

    def add_metadata(self, key, value, value_type=None, *, item_type=None):
        """Add one key. The type is inferred unless ``value_type`` is given."""
        kind = _infer_value_type(value) if value_type is None else int(value_type)
        self._metadata[str(key)] = (value, kind, item_type)

    def add_tensor(self, name, data, *, shape, ggml_type=GGMLType.F32):
        """Add a tensor from raw bytes, or from numbers for the plain types.

        ``shape`` lists dimensions fastest-varying first, as GGUF stores them.
        """
        name = str(name)
        if name in self._names:
            raise GGUFError(f"duplicate tensor name {name!r}")
        shape = tuple(int(dim) for dim in shape)
        if not shape or len(shape) > MAX_TENSOR_DIMS or any(dim <= 0 for dim in shape):
            raise GGUFError("a tensor needs 1 to 8 positive dimensions")
        count = 1
        for dim in shape:
            count *= dim
        ggml_type = GGMLType.from_name(ggml_type) if isinstance(ggml_type, str) else int(ggml_type)
        if isinstance(data, (bytes, bytearray, memoryview)):
            raw = bytes(data)
        elif isinstance(data, array):
            raw = encode_tensor(data.tolist(), ggml_type)
        else:
            raw = encode_tensor(data, ggml_type)
        expected = GGMLType.nbytes(ggml_type, count)
        if expected is None or expected != len(raw):
            raise GGUFError(f"tensor {name!r}: {len(raw)} bytes do not match shape {list(shape)}")
        self._tensors.append((name, shape, ggml_type, raw))
        self._names.add(name)

    def to_bytes(self):
        out = io.BytesIO()
        self._write(out)
        return out.getvalue()

    def write(self, destination):
        """Write to a path (atomically, through a temporary file) or a file object."""
        if not isinstance(destination, (str, os.PathLike)):
            self._write(destination)
            return
        path = os.fspath(destination)
        temporary = f"{path}.tmp-{os.getpid()}"
        try:
            with open(temporary, "wb") as handle:
                self._write(handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _write(self, out):
        alignment = self.alignment
        out.write(GGUF_MAGIC)
        out.write(_U32.pack(WRITE_VERSION))
        out.write(_U64.pack(len(self._tensors)))
        out.write(_U64.pack(len(self._metadata)))
        written = 24
        for key, (value, kind, item_type) in self._metadata.items():
            chunk = _encode_string(key) + _U32.pack(kind) + _encode_value(value, kind, item_type)
            out.write(chunk)
            written += len(chunk)
        offset = 0
        offsets = []
        for name, shape, ggml_type, raw in self._tensors:
            offsets.append(offset)
            chunk = (
                _encode_string(name)
                + _U32.pack(len(shape))
                + b"".join(_U64.pack(dim) for dim in shape)
                + _U32.pack(ggml_type)
                + _U64.pack(offset)
            )
            out.write(chunk)
            written += len(chunk)
            offset += len(raw) + (-len(raw) % alignment)
        out.write(b"\x00" * (-written % alignment))
        for _name, _shape, _type, raw in self._tensors:
            out.write(raw)
            out.write(b"\x00" * (-len(raw) % alignment))

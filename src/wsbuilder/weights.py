"""Read model weights stored as safetensors or GGUF, with the standard library.

Both formats keep the raw tensor bytes in one file next to a small header, so
:mod:`mmap` maps the file and each tensor is read only when it is needed.

* **safetensors** (Hugging Face): 8 bytes with the header length (unsigned,
  little-endian), a JSON object, then the data area. Each JSON entry gives a
  tensor's ``dtype``, ``shape`` (row-major) and ``data_offsets`` ``[begin,
  end)`` relative to the start of the data area; the optional
  ``__metadata__`` entry maps strings to strings. Large models are split in
  shards listed by a ``model.safetensors.index.json`` file
  (:class:`ShardedSafetensors`).
* **GGUF** (llama.cpp), versions 2 and 3, little-endian: the magic
  ``b"GGUF"``, a version, the tensor and key/value counts, typed metadata
  (integers, floats, booleans, strings and nested arrays), one record per
  tensor (name, dimensions, ggml type and offset) and the data area, aligned
  to ``general.alignment`` (32 by default). GGUF lists dimensions from the
  innermost one; :attr:`WeightTensor.shape` reverses them to row-major
  order, like safetensors.

:func:`open_weights` picks the reader for a file. Every reader offers the same
interface (``tensor(name)``, ``names()``, ``metadata``, ``summary()``...) and
returns :class:`WeightTensor` objects: ``raw()`` is a read-only view of the
mapped bytes (no copy) and ``to_array()``/``values()`` convert them. Plain
float and integer types convert; of the quantized GGUF types only ``Q8_0`` and
``Q4_0`` are dequantized, the others raise ``NotImplementedError`` (their raw
bytes are still available).

Readers validate the header before returning: counts, string lengths, shapes,
offsets and sizes are checked against the file, so a truncated or hostile file
raises :class:`WeightsFormatError` instead of reading out of bounds.

Closing a reader releases its mapping. ``close()`` is idempotent and never
fails because of views returned by ``raw()``: while one is alive the mapping
stays valid for it and is released when the last view goes away. Every other
access through a closed reader raises ``ValueError``.
"""

import array
import json
import math
import mmap
import operator
import os
import struct
import sys
from itertools import repeat

__all__ = [
    "GGML_TYPES",
    "GGUFReader",
    "SAFETENSORS_DTYPES",
    "SafetensorsReader",
    "ShardedSafetensors",
    "WeightTensor",
    "WeightsFormatError",
    "detect_weights_format",
    "open_weights",
]

#: safetensors headers larger than this are refused unless the caller raises it.
DEFAULT_MAX_HEADER_BYTES = 100 * 1024**2
DEFAULT_MAX_TENSORS = 1 << 20
DEFAULT_MAX_KV = 1 << 20
DEFAULT_MAX_STRING_BYTES = 16 * 1024**2
GGUF_MAGIC = b"GGUF"
GGUF_DEFAULT_ALIGNMENT = 32
_MAX_DIMS = 8
_MAX_ARRAY_DEPTH = 8
_MAX_DIM_VALUE = 2**63 - 1
_LITTLE = sys.byteorder == "little"
_SUMMARY_LIST_ITEMS = 16
_SUMMARY_STRING_CHARS = 8192


class WeightsFormatError(ValueError):
    """A file that is not a valid safetensors or GGUF weights file."""


def _typecode(itemsize, kind):
    """The ``array`` typecode with this size: kind ``s`` signed, ``u`` unsigned, ``f`` float."""
    candidates = {"s": "bhilq", "u": "BHILQ", "f": "fd"}[kind]
    for code in candidates:
        if array.array(code).itemsize == itemsize:
            return code
    raise NotImplementedError(f"this platform has no {itemsize}-byte array type")


# dtype -> (bytes per element, how to convert: ("array", kind) / "f16" / "bf16" / None)
_PLAIN = {
    "F64": (8, ("array", "f")),
    "F32": (4, ("array", "f")),
    "F16": (2, "f16"),
    "BF16": (2, "bf16"),
    "I64": (8, ("array", "s")),
    "I32": (4, ("array", "s")),
    "I16": (2, ("array", "s")),
    "I8": (1, ("array", "s")),
    "U64": (8, ("array", "u")),
    "U32": (4, ("array", "u")),
    "U16": (2, ("array", "u")),
    "U8": (1, ("array", "u")),
    "BOOL": (1, ("array", "u")),
}

#: safetensors dtypes and their size in bytes. ``F8_*`` tensors are raw only.
SAFETENSORS_DTYPES = {name: size for name, (size, _) in _PLAIN.items()}
SAFETENSORS_DTYPES.update({"F8_E4M3": 1, "F8_E5M2": 1})

#: ggml type id -> (name, elements per block, bytes per block).
GGML_TYPES = {
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
    39: ("MXFP4", 32, 17),
}

# GGUF metadata value types: id -> (name, struct code or None)
_GGUF_VALUE_TYPES = {
    0: ("uint8", "<B"),
    1: ("int8", "<b"),
    2: ("uint16", "<H"),
    3: ("int16", "<h"),
    4: ("uint32", "<I"),
    5: ("int32", "<i"),
    6: ("float32", "<f"),
    7: ("bool", "<B"),
    8: ("string", None),
    9: ("array", None),
    10: ("uint64", "<Q"),
    11: ("int64", "<q"),
    12: ("float64", "<d"),
}
_GGUF_STRING = 8
_GGUF_ARRAY = 9
_GGUF_BOOL = 7


def _count(dims):
    total = 1
    for dim in dims:
        total *= dim
    return total


# -- conversions ------------------------------------------------------------


def _f16_scales(data, block_bytes, blocks):
    """The f16 numbers at the start of each block of ``data``, as floats."""
    scale = bytearray(2 * blocks)
    scale[0::2] = data[0::block_bytes]
    scale[1::2] = data[1::block_bytes]
    return struct.unpack(f"<{blocks}e", scale)


def _scaled(quants, scales, block):
    """``quants[i] * scales[i // block]`` as float32, without a Python loop per element."""
    factors = (factor for scale in scales for factor in repeat(scale, block))
    return array.array("f", map(operator.mul, quants, factors))


# Q4_0 nibbles stored as unsigned 4-bit values; the weight is (nibble - 8) * d.
_LOW_NIBBLE = bytes(((byte & 0x0F) - 8) & 0xFF for byte in range(256))
_HIGH_NIBBLE = bytes(((byte >> 4) - 8) & 0xFF for byte in range(256))


def _dequantize_q8_0(data, count):
    blocks = count // 32
    quants = bytearray(count)
    for index in range(32):
        quants[index::32] = data[2 + index::34]
    return _scaled(array.array("b", bytes(quants)), _f16_scales(data, 34, blocks), 32)


def _dequantize_q4_0(data, count):
    blocks = count // 32
    packed = bytearray(16 * blocks)
    for index in range(16):
        packed[index::16] = data[2 + index::18]
    low = packed.translate(_LOW_NIBBLE)
    high = packed.translate(_HIGH_NIBBLE)
    quants = bytearray(count)
    for index in range(16):
        quants[index::32] = low[index::16]
        quants[16 + index::32] = high[index::16]
    return _scaled(array.array("b", bytes(quants)), _f16_scales(data, 18, blocks), 32)


_DEQUANTIZERS = {"Q8_0": _dequantize_q8_0, "Q4_0": _dequantize_q4_0}


def _convert(dtype, view, count):
    """Convert little-endian tensor bytes (a memoryview) to an ``array.array``."""
    if dtype in _DEQUANTIZERS:
        return _DEQUANTIZERS[dtype](bytes(view), count)
    rule = _PLAIN.get(dtype, (None, None))[1]
    if rule is None:
        raise NotImplementedError(
            f"converting {dtype} tensors is not supported yet; use raw() for the bytes"
        )
    if rule == "f16":
        # struct handles half floats on every supported Python and byte order.
        return array.array("f", struct.unpack(f"<{count}e", view))
    if rule == "bf16":
        # A bfloat16 is the high half of a float32: put its two bytes there.
        data = bytes(view)
        widened = bytearray(4 * count)
        if _LITTLE:
            widened[2::4] = data[0::2]
            widened[3::4] = data[1::2]
        else:
            widened[0::4] = data[1::2]
            widened[1::4] = data[0::2]
        result = array.array("f")
        result.frombytes(widened)
        return result
    size = _PLAIN[dtype][0]
    result = array.array(_typecode(size, rule[1]))
    result.frombytes(view)
    if not _LITTLE and size > 1:
        result.byteswap()
    return result


class WeightTensor:
    """One tensor of a weights file: metadata now, bytes and values on demand."""

    __slots__ = ("name", "shape", "dtype", "element_count", "nbytes", "_reader", "_offset")

    def __init__(self, name, shape, dtype, nbytes, reader, offset):
        self.name = name
        self.shape = tuple(shape)
        self.dtype = dtype
        self.element_count = _count(self.shape)
        self.nbytes = nbytes
        self._reader = reader
        self._offset = offset

    def raw(self):
        """A read-only ``memoryview`` of the tensor bytes in the mapped file (no copy).

        Release it (``view.release()`` or a ``with`` block) when done; the
        mapping stays valid for it even after the reader is closed.
        """
        return self._reader._view(self._offset, self.nbytes)

    def to_array(self):
        """The values as an ``array.array``: ``"f"`` for F32/F16/BF16 and dequantized
        types, ``"d"`` for F64, the matching integer typecode otherwise."""
        with self.raw() as view:
            return _convert(self.dtype, view, self.element_count)

    def values(self):
        """The flat list of values in row-major order (``bool`` for BOOL tensors)."""
        values = self.to_array().tolist()
        if self.dtype == "BOOL":
            return [bool(value) for value in values]
        return values

    def rows(self):
        """A 2-D tensor as a list of rows."""
        if len(self.shape) != 2:
            raise WeightsFormatError(f"{self.name!r} has shape {list(self.shape)}, not 2-D")
        flat = self.values()
        width = self.shape[1]
        return [flat[row * width:(row + 1) * width] for row in range(self.shape[0])]

    def __repr__(self):
        return f"<WeightTensor {self.name} {self.dtype} {list(self.shape)}>"


# -- shared reader behaviour -----------------------------------------------


def _summary_value(value):
    """A JSON-friendly, bounded version of a metadata value."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, str):
        text = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        if len(text) > _SUMMARY_STRING_CHARS:
            return {"type": "string", "length": len(text)}
        return text
    if isinstance(value, list):
        nested = any(isinstance(item, (list, str)) and len(item) > 64 for item in value)
        if len(value) > _SUMMARY_LIST_ITEMS or nested:
            kinds = sorted({_kind(item) for item in value})
            return {"type": "array", "item_type": "|".join(kinds) or None, "length": len(value)}
        return [_summary_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _summary_value(item) for key, item in value.items()}
    return value


def _kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    return "array"


class _WeightsBase:
    """The interface every weights reader shares."""

    format = None

    def _tensor_map(self):
        return self._tensors

    @property
    def architecture(self):
        return None

    @property
    def name(self):
        return None

    def names(self):
        return list(self._tensor_map())

    def __iter__(self):
        return iter(self.names())

    def __len__(self):
        return len(self._tensor_map())

    def __contains__(self, name):
        return name in self._tensor_map()

    def tensor(self, name):
        self._check_open()
        try:
            return self._tensor_map()[name]
        except KeyError:
            raise KeyError(f"no tensor {name!r}") from None

    def tensors(self):
        """All tensors, in file order."""
        return [self.tensor(name) for name in self.names()]

    @property
    def parameter_count(self):
        return sum(tensor.element_count for tensor in self.tensors())

    def _summary_extra(self):
        return {}

    def summary(self):
        """What someone choosing or loading the weights wants to know (JSON-serializable).

        Keys match :meth:`wsbuilder.ModelReader.summary`: ``parameters`` and
        ``tensors`` are counts and ``dtypes`` maps each type to its number of
        values. Long metadata arrays (such as a GGUF vocabulary) and very long
        strings are replaced by their type and length.
        """
        dtypes = {}
        parameters = 0
        for tensor in self.tensors():
            dtypes[tensor.dtype] = dtypes.get(tensor.dtype, 0) + tensor.element_count
            parameters += tensor.element_count
        summary = {
            "format": self.format,
            "name": self.name,
            "architecture": self.architecture,
            "parameters": parameters,
            "tensors": len(self),
            "dtypes": dtypes,
            "metadata": {key: _summary_value(value) for key, value in self.metadata.items()},
            "file_size": self.file_size,
        }
        summary.update(self._summary_extra())
        return summary

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return f"<{type(self).__name__} {self.path} {len(self)} tensors>"


class _MappedFile(_WeightsBase):
    """A weights file mapped read-only in memory."""

    def _map(self, path):
        self.path = os.fspath(path)
        self._mm = None
        with open(self.path, "rb") as handle:
            self.file_size = os.fstat(handle.fileno()).st_size
            if self.file_size == 0:
                raise WeightsFormatError("the file is empty")
            # The mapping keeps its own reference to the file; the handle can go.
            self._mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)

    def _check_open(self):
        if self._mm is None:
            raise ValueError("the weights reader is closed")

    def _view(self, offset, length):
        self._check_open()
        with memoryview(self._mm) as whole:
            return whole[offset:offset + length].toreadonly()

    @property
    def closed(self):
        return self._mm is None

    def close(self):
        """Release the mapping (idempotent). Views from ``raw()`` stay valid until released."""
        mapping, self._mm = self._mm, None
        if mapping is not None:
            try:
                mapping.close()
            except BufferError:
                # A raw() view still uses the mapping: dropping our reference
                # lets the mapping go away when the last view is released.
                pass


def _close_on_error(reader, parse):
    try:
        parse()
    except BaseException:
        reader.close()
        raise


# -- safetensors ------------------------------------------------------------


def _unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise WeightsFormatError(f"the header repeats the key {key!r}")
        data[key] = value
    return data


def _read_safetensors_header(handle, file_size, max_header_bytes):
    prefix = handle.read(8)
    if len(prefix) < 8:
        raise WeightsFormatError("the file is too short for a safetensors header")
    (length,) = struct.unpack("<Q", prefix)
    if length < 2:
        raise WeightsFormatError("the safetensors header is empty")
    if length > max_header_bytes:
        raise WeightsFormatError(
            f"the safetensors header ({length} bytes) is larger than max_header_bytes"
        )
    if length > file_size - 8:
        raise WeightsFormatError("the safetensors header is truncated")
    raw = handle.read(length)
    if len(raw) != length or raw[:1] != b"{":
        raise WeightsFormatError("the safetensors header is not a JSON object")
    try:
        header = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, WeightsFormatError):
            raise
        raise WeightsFormatError("the safetensors header is not valid JSON") from exc
    if not isinstance(header, dict):
        raise WeightsFormatError("the safetensors header is not a JSON object")
    return length, header


def _safetensors_shape(name, shape):
    if not isinstance(shape, list) or len(shape) > _MAX_DIMS:
        raise WeightsFormatError(f"tensor {name!r} has a bad shape")
    for dim in shape:
        if isinstance(dim, bool) or not isinstance(dim, int) or not 0 <= dim <= _MAX_DIM_VALUE:
            raise WeightsFormatError(f"tensor {name!r} has a bad shape")
    return tuple(shape)


def _safetensors_offsets(name, offsets):
    if (
        not isinstance(offsets, list)
        or len(offsets) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in offsets)
    ):
        raise WeightsFormatError(f"tensor {name!r} has bad data_offsets")
    begin, end = offsets
    if not 0 <= begin <= end:
        raise WeightsFormatError(f"tensor {name!r} has bad data_offsets")
    return begin, end


class SafetensorsReader(_MappedFile):
    """Read a ``.safetensors`` file through a read-only memory map.

    ``metadata`` holds the ``__metadata__`` strings. Opening checks every
    tensor entry: dtype, shape, that ``data_offsets`` lie inside the data area,
    that their length matches the shape and that no two tensors overlap.
    """

    format = "safetensors"

    def __init__(self, path, *, max_header_bytes=DEFAULT_MAX_HEADER_BYTES):
        self.max_header_bytes = int(max_header_bytes)
        self._map(path)
        _close_on_error(self, self._parse)

    def _parse(self):
        self._mm.seek(0)
        length, header = _read_safetensors_header(self._mm, self.file_size, self.max_header_bytes)
        self.header_size = 8 + length
        data_size = self.file_size - self.header_size
        metadata = header.pop("__metadata__", None)
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
        ):
            raise WeightsFormatError("__metadata__ must map strings to strings")
        self.metadata = metadata
        self._tensors = {}
        spans = []
        for name, entry in header.items():
            if not name or not isinstance(entry, dict):
                raise WeightsFormatError(f"tensor {name!r} has no valid entry")
            dtype = entry.get("dtype")
            if dtype not in SAFETENSORS_DTYPES:
                raise WeightsFormatError(f"tensor {name!r} has an unknown dtype {dtype!r}")
            shape = _safetensors_shape(name, entry.get("shape"))
            begin, end = _safetensors_offsets(name, entry.get("data_offsets"))
            if end > data_size:
                raise WeightsFormatError(f"tensor {name!r} lies outside the data area")
            nbytes = _count(shape) * SAFETENSORS_DTYPES[dtype]
            if end - begin != nbytes:
                raise WeightsFormatError(
                    f"tensor {name!r} has {end - begin} bytes but shape {list(shape)} of "
                    f"{dtype} needs {nbytes}"
                )
            self._tensors[name] = WeightTensor(
                name, shape, dtype, nbytes, self, self.header_size + begin
            )
            spans.append((begin, end, name))
        _check_overlaps(spans)

    def _summary_extra(self):
        return {"header_size": self.header_size}


def _check_overlaps(spans):
    spans.sort()
    previous_end, previous_name = 0, None
    for begin, end, name in spans:
        if begin == end:
            continue
        if previous_name is not None and begin < previous_end:
            raise WeightsFormatError(f"tensors {previous_name!r} and {name!r} overlap")
        previous_end, previous_name = end, name


class ShardedSafetensors(_WeightsBase):
    """A model split in several safetensors files, read through its index.

    ``index_path`` is a ``model.safetensors.index.json`` file: its
    ``weight_map`` maps each tensor name to a shard file in the same directory
    (absolute paths and ``..`` are refused). Shards are opened on first use
    and closed together by :meth:`close`. ``metadata`` is the index's own
    ``metadata`` object (such as ``total_size``).
    """

    format = "safetensors"

    def __init__(self, index_path, *, max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                 max_index_bytes=DEFAULT_MAX_HEADER_BYTES):
        self.path = os.fspath(index_path)
        self.max_header_bytes = int(max_header_bytes)
        self._shards = {}
        self._closed = False
        directory = os.path.dirname(os.path.abspath(self.path))
        with open(self.path, "rb") as handle:
            raw = handle.read(int(max_index_bytes) + 1)
        if len(raw) > max_index_bytes:
            raise WeightsFormatError("the safetensors index is larger than max_index_bytes")
        try:
            index = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            if isinstance(exc, WeightsFormatError):
                raise
            raise WeightsFormatError("the safetensors index is not valid JSON") from exc
        if not isinstance(index, dict) or not isinstance(index.get("weight_map"), dict):
            raise WeightsFormatError("the safetensors index has no weight_map object")
        metadata = index.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise WeightsFormatError("the safetensors index metadata must be an object")
        self.metadata = metadata
        self._files = {}
        for name, filename in index["weight_map"].items():
            if not name:
                raise WeightsFormatError("the weight_map has an empty tensor name")
            self._files[name] = self._shard_path(directory, filename)
        for shard in set(self._files.values()):
            if not os.path.isfile(shard):
                raise WeightsFormatError(f"the shard {shard!r} does not exist")

    @staticmethod
    def _shard_path(directory, filename):
        if not isinstance(filename, str) or not filename:
            raise WeightsFormatError(f"bad shard file name {filename!r}")
        parts = filename.replace("\\", "/").split("/")
        if os.path.isabs(filename) or filename.startswith(("/", "\\")) or ".." in parts or (
            os.path.splitdrive(filename)[0]
        ):
            raise WeightsFormatError(f"the shard {filename!r} is outside the index directory")
        return os.path.join(directory, *parts)

    def _check_open(self):
        if self._closed:
            raise ValueError("the weights reader is closed")

    @property
    def closed(self):
        return self._closed

    def _shard(self, path):
        self._check_open()
        reader = self._shards.get(path)
        if reader is None:
            reader = SafetensorsReader(path, max_header_bytes=self.max_header_bytes)
            self._shards[path] = reader
        return reader

    def _tensor_map(self):
        return self._files

    def tensor(self, name):
        self._check_open()
        try:
            path = self._files[name]
        except KeyError:
            raise KeyError(f"no tensor {name!r}") from None
        shard = self._shard(path)
        if name not in shard:
            raise WeightsFormatError(f"tensor {name!r} is not in its shard {path!r}")
        return shard.tensor(name)

    @property
    def shard_paths(self):
        """The shard files, in the order they first appear in the index."""
        return list(dict.fromkeys(self._files.values()))

    @property
    def file_size(self):
        return sum(os.path.getsize(path) for path in self.shard_paths)

    def _summary_extra(self):
        return {"shards": len(self.shard_paths)}

    def close(self):
        """Close every opened shard (idempotent)."""
        self._closed = True
        shards, self._shards = self._shards, {}
        for reader in shards.values():
            reader.close()


# -- GGUF -------------------------------------------------------------------


class _Cursor:
    """Bounds-checked little-endian reads from a mapped buffer."""

    def __init__(self, buffer, size, max_string_bytes):
        self.buffer = buffer
        self.size = size
        self.position = 0
        self.max_string_bytes = max_string_bytes

    def remaining(self):
        return self.size - self.position

    def unpack(self, code):
        width = struct.calcsize(code)
        if width > self.remaining():
            raise WeightsFormatError("the GGUF header is truncated")
        (value,) = struct.unpack_from(code, self.buffer, self.position)
        self.position += width
        return value

    def string_bytes(self):
        length = self.unpack("<Q")
        if length > self.max_string_bytes:
            raise WeightsFormatError(f"a GGUF string of {length} bytes exceeds max_string_bytes")
        if length > self.remaining():
            raise WeightsFormatError("a GGUF string runs past the end of the file")
        start = self.position
        self.position += length
        return bytes(self.buffer[start:self.position])

    def text(self, what):
        try:
            return self.string_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeightsFormatError(f"{what} is not valid UTF-8") from exc


# Smallest encoding of one value of each type, to bound array counts.
_GGUF_MIN_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 8: 8, 9: 12, 10: 8, 11: 8, 12: 8}


def _gguf_value(cursor, value_type, depth=0):
    if value_type not in _GGUF_VALUE_TYPES:
        raise WeightsFormatError(f"unknown GGUF value type {value_type}")
    if value_type == _GGUF_STRING:
        # Text values keep any invalid UTF-8 byte (surrogateescape) instead of failing.
        return cursor.string_bytes().decode("utf-8", "surrogateescape")
    if value_type == _GGUF_ARRAY:
        if depth >= _MAX_ARRAY_DEPTH:
            raise WeightsFormatError("GGUF arrays are nested too deeply")
        item_type = cursor.unpack("<I")
        count = cursor.unpack("<Q")
        if item_type not in _GGUF_VALUE_TYPES:
            raise WeightsFormatError(f"unknown GGUF value type {item_type}")
        if count * _GGUF_MIN_SIZE[item_type] > cursor.remaining():
            raise WeightsFormatError("a GGUF array runs past the end of the file")
        code = _GGUF_VALUE_TYPES[item_type][1]
        if code is not None and item_type != _GGUF_BOOL:
            width = struct.calcsize(code)
            values = struct.unpack_from(f"<{count}{code[1]}", cursor.buffer, cursor.position)
            cursor.position += width * count
            return list(values)
        return [_gguf_value(cursor, item_type, depth + 1) for _ in range(count)]
    value = cursor.unpack(_GGUF_VALUE_TYPES[value_type][1])
    if value_type == _GGUF_BOOL:
        if value > 1:
            raise WeightsFormatError(f"bad GGUF boolean {value}")
        return bool(value)
    return value


class GGUFReader(_MappedFile):
    """Read a GGUF file (version 2 or 3, little-endian) through a read-only memory map.

    ``metadata`` holds every key/value pair (arrays as lists, strings as
    ``str``) and ``metadata_types`` the GGUF type name of each one
    (``"uint32"``, ``"array"``...). Opening checks the counts against
    ``max_tensors``/``max_kv``, every string against ``max_string_bytes`` and
    the bytes left, and every tensor's type, size, alignment and bounds.
    """

    format = "gguf"

    def __init__(self, path, *, max_tensors=DEFAULT_MAX_TENSORS, max_kv=DEFAULT_MAX_KV,
                 max_string_bytes=DEFAULT_MAX_STRING_BYTES):
        self.max_tensors = int(max_tensors)
        self.max_kv = int(max_kv)
        self.max_string_bytes = int(max_string_bytes)
        self._map(path)
        _close_on_error(self, self._parse)

    def _parse(self):
        with memoryview(self._mm) as buffer:
            cursor = _Cursor(buffer, self.file_size, self.max_string_bytes)
            if self.file_size < 4 or bytes(buffer[:4]) != GGUF_MAGIC:
                raise WeightsFormatError("not a GGUF file (bad magic)")
            cursor.position = 4
            version = cursor.unpack("<I")
            if version not in (2, 3):
                if version & 0xFFFF == 0 and (version >> 24) in (2, 3):
                    raise WeightsFormatError("big-endian GGUF files are not supported")
                if version == 1:
                    raise WeightsFormatError("GGUF version 1 is not supported (use version 2 or 3)")
                raise WeightsFormatError(f"unsupported GGUF version {version}")
            self.version = version
            tensor_count = cursor.unpack("<Q")
            kv_count = cursor.unpack("<Q")
            if tensor_count > self.max_tensors:
                raise WeightsFormatError(f"{tensor_count} tensors exceed max_tensors")
            if kv_count > self.max_kv:
                raise WeightsFormatError(f"{kv_count} metadata entries exceed max_kv")
            self.metadata = {}
            self.metadata_types = {}
            for _ in range(kv_count):
                key = cursor.text("a GGUF metadata key")
                if key in self.metadata:
                    raise WeightsFormatError(f"the GGUF metadata repeats the key {key!r}")
                value_type = cursor.unpack("<I")
                self.metadata[key] = _gguf_value(cursor, value_type)
                self.metadata_types[key] = _GGUF_VALUE_TYPES[value_type][0]
            self.alignment = self._alignment()
            infos = [self._tensor_info(cursor) for _ in range(tensor_count)]
            end_of_header = cursor.position
        self.data_offset = -(-end_of_header // self.alignment) * self.alignment
        self._tensors = {}
        spans = []
        for name, shape, dtype, nbytes, offset in infos:
            if name in self._tensors:
                raise WeightsFormatError(f"tensor {name!r} appears twice")
            if offset % self.alignment:
                raise WeightsFormatError(f"tensor {name!r} is not aligned to {self.alignment}")
            if self.data_offset + offset + nbytes > self.file_size:
                raise WeightsFormatError(f"tensor {name!r} lies outside the file")
            self._tensors[name] = WeightTensor(
                name, shape, dtype, nbytes, self, self.data_offset + offset
            )
            spans.append((offset, offset + nbytes, name))
        _check_overlaps(spans)

    def _alignment(self):
        alignment = self.metadata.get("general.alignment", GGUF_DEFAULT_ALIGNMENT)
        if (
            isinstance(alignment, bool)
            or not isinstance(alignment, int)
            or alignment <= 0
            or alignment & (alignment - 1)
        ):
            raise WeightsFormatError(f"general.alignment must be a power of two, not {alignment!r}")
        return alignment

    def _tensor_info(self, cursor):
        name = cursor.text("a GGUF tensor name")
        if not name:
            raise WeightsFormatError("a GGUF tensor has an empty name")
        n_dims = cursor.unpack("<I")
        if n_dims > _MAX_DIMS:
            raise WeightsFormatError(f"tensor {name!r} has {n_dims} dimensions")
        dims = [cursor.unpack("<Q") for _ in range(n_dims)]
        if any(dim > _MAX_DIM_VALUE for dim in dims):
            raise WeightsFormatError(f"tensor {name!r} has a bad dimension")
        type_id = cursor.unpack("<I")
        offset = cursor.unpack("<Q")
        if type_id not in GGML_TYPES:
            raise WeightsFormatError(f"tensor {name!r} has an unknown ggml type {type_id}")
        dtype, block, block_bytes = GGML_TYPES[type_id]
        count = _count(dims)
        if block > 1 and (not dims or dims[0] % block):
            raise WeightsFormatError(
                f"tensor {name!r}: the inner dimension is not a multiple of the {dtype} block"
            )
        nbytes = count // block * block_bytes
        if nbytes > self.file_size:
            raise WeightsFormatError(f"tensor {name!r} is larger than the file")
        # GGUF lists ne[0] (the innermost dimension) first; expose row-major order.
        return name, tuple(reversed(dims)), dtype, nbytes, offset

    @property
    def architecture(self):
        value = self.metadata.get("general.architecture")
        return value if isinstance(value, str) else None

    @property
    def name(self):
        value = self.metadata.get("general.name")
        return value if isinstance(value, str) else None

    def _summary_extra(self):
        return {"version": self.version, "alignment": self.alignment}


# -- detection --------------------------------------------------------------


def detect_weights_format(path, *, max_header_bytes=DEFAULT_MAX_HEADER_BYTES):
    """``"gguf"``, ``"safetensors"`` or ``None``, from the file's first bytes.

    GGUF files start with ``b"GGUF"``; a safetensors file starts with a
    plausible header length followed by a JSON object. Only the header is
    read; ``None`` is also returned for files that cannot be read.
    """
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            prefix = handle.read(8)
            if prefix[:4] == GGUF_MAGIC:
                return "gguf"
            if len(prefix) < 8:
                return None
            (length,) = struct.unpack("<Q", prefix)
            if not 2 <= length <= min(max_header_bytes, size - 8) or handle.read(1) != b"{":
                return None
            handle.seek(0)
            _read_safetensors_header(handle, size, max_header_bytes)
            return "safetensors"
    except (OSError, WeightsFormatError):
        return None


def open_weights(path, **options):
    """Open ``path`` with the right reader.

    A ``*.index.json`` file opens as :class:`ShardedSafetensors`; other files
    by their content (:func:`detect_weights_format`). ``options`` go to the
    reader. Raises :class:`WeightsFormatError` for other files.
    """
    path = os.fspath(path)
    if path.endswith(".index.json"):
        return ShardedSafetensors(path, **options)
    kind = detect_weights_format(path)
    if kind == "gguf":
        return GGUFReader(path, **options)
    if kind == "safetensors":
        return SafetensorsReader(path, **options)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    raise WeightsFormatError(f"{path!r} is not a safetensors or GGUF file")

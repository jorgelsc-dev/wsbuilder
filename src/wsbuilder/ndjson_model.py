"""The wsbuilder model format: a model and its weights in one NDJSON file.

NDJSON is JSON with one object per line. A model file has:

* **Line 1, the model**: ``{"format": "wsbuilder-model", "version": 1,
  "architecture": ..., "name": ..., "metadata": {...}, "tensor_count": N}``.
* **Lines 2 to N+1, the weights**: one tensor per line,
  ``{"tensor": "blk.0.weight", "shape": [2, 3], "dtype": "F32",
  "data": [0.1, 0.2, ...]}``. ``data`` is flat, in row-major order, and holds
  exactly ``prod(shape)`` numbers.
* **Compact tensors** (version 2): ``{"tensor": ..., "shape": ...,
  "dtype": ..., "encoding": "base64", "data": "<base64>"}``, where ``data``
  is the base64 text of the little-endian tensor bytes. Numbers written as
  text are too big for large models; base64 costs 4/3 of the raw bytes.

Supported dtypes: ``F64`` (exact), ``F32``, ``F16`` and ``BF16`` (values
rounded to that precision when written), ``I32`` and ``I64``, plus the ggml
blocks ``Q8_0`` and ``Q4_0`` (base64 only; 32 values per block, the last
dimension must be a multiple of 32). JSON has no NaN or infinity, so those
float values are written as the strings ``"NaN"``, ``"Infinity"`` and
``"-Infinity"`` and read back as floats. A file that uses only JSON lists
and the dtypes of version 1 is still written as version 1.

The file is plain text, so any language with a JSON parser can read it line
by line, and ``grep``/``head`` work on it. :class:`ModelReader` indexes the
tensors when it opens a file and reads each one on demand, so the whole file
never needs to be in memory; ``to_array()`` decodes a tensor to an
``array.array`` (``"f"`` for every float and quantized dtype) and
``row_array()`` reads one row of a base64 tensor without the rest.
:class:`ModelWriter` writes atomically.
Everything here uses the standard library only.
"""

import array
import base64
import binascii
import json
import math
import os
import struct
import threading
import uuid

from .weights import _convert

__all__ = [
    "MODEL_FORMAT",
    "MODEL_FORMAT_VERSION",
    "MODEL_DTYPES",
    "MODEL_ENCODINGS",
    "ModelFormatError",
    "ModelReader",
    "ModelTensor",
    "ModelWriter",
    "is_model_file",
    "read_model_header",
]

MODEL_FORMAT = "wsbuilder-model"
MODEL_FORMAT_VERSION = 2
MODEL_DTYPES = ("F64", "F32", "F16", "BF16", "I32", "I64", "Q8_0", "Q4_0")
MODEL_ENCODINGS = ("json", "base64")
_FLOAT_DTYPES = ("F64", "F32", "F16", "BF16")
_V1_DTYPES = ("F64", "F32", "F16", "I32", "I64")
_ITEM_SIZES = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I32": 4, "I64": 8}
_PACK_CODES = {"F64": "d", "F32": "f", "F16": "e", "I32": "i", "I64": "q"}
#: ggml block dtypes: (values per block, bytes per block). Base64 only.
_BLOCK_DTYPES = {"Q8_0": (32, 34), "Q4_0": (32, 18)}
_B64_ALPHABET = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_DATA_KEY = b'"data":"'
_CHECK_CHUNK = 1 << 20
_JSON_SPACE = b" \t\r\n"
#: Raw bytes per chunk when base64 is decoded or encoded piece by piece (multiple of 3).
_B64_CHUNK = 3 << 18
_INT_RANGES = {"I32": (-(2**31), 2**31 - 1), "I64": (-(2**63), 2**63 - 1)}
_NON_FINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}
_NAME_LIMIT = 1024
_MAX_DIMS = 8
#: Lines longer than this are refused; raise it for very large tensors.
DEFAULT_MAX_LINE_BYTES = 4 * 1024**3


class ModelFormatError(ValueError):
    """A file that is not a valid wsbuilder NDJSON model."""


def _dtype(name):
    dtype = str(name).upper()
    if dtype not in MODEL_DTYPES:
        raise ModelFormatError(f"unknown dtype {name!r}; use one of {', '.join(MODEL_DTYPES)}")
    return dtype


def _shape(shape):
    if isinstance(shape, int):
        shape = (shape,)
    try:
        dims = tuple(int(dim) for dim in shape)
    except (TypeError, ValueError) as exc:
        raise ModelFormatError(f"bad shape {shape!r}") from exc
    if not dims or len(dims) > _MAX_DIMS or any(dim < 0 for dim in dims):
        raise ModelFormatError(f"bad shape {shape!r}")
    return dims


def _count(dims):
    total = 1
    for dim in dims:
        total *= dim
    return total


def _nbytes(name, dtype, dims):
    """Bytes of a tensor of ``dims`` in ``dtype`` (checks the block size)."""
    count = _count(dims)
    if dtype in _BLOCK_DTYPES:
        block, block_bytes = _BLOCK_DTYPES[dtype]
        if dims[-1] % block:
            raise ModelFormatError(
                f"tensor {name!r}: the last dimension of a {dtype} tensor must be a multiple of "
                f"{block}"
            )
        return count // block * block_bytes
    return count * _ITEM_SIZES[dtype]


def _bf16_bits(value):
    """The bfloat16 bits of ``value``, rounded to nearest even like PyTorch."""
    (bits,) = struct.unpack("<I", struct.pack("<f", _round_float(value, "F32")))
    if math.isnan(value):
        return (bits >> 16) | 0x40
    return ((bits + 0x7FFF + ((bits >> 16) & 1)) >> 16) & 0xFFFF


def _round_float(value, dtype):
    if dtype == "F64" or not math.isfinite(value):
        return value
    if dtype == "BF16":
        (number,) = struct.unpack("<f", struct.pack("<I", _bf16_bits(value) << 16))
        return number
    code = "<f" if dtype == "F32" else "<e"
    try:
        return struct.unpack(code, struct.pack(code, value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _encode_value(value, dtype):
    if dtype in _FLOAT_DTYPES:
        number = _round_float(float(value), dtype)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        return number
    if isinstance(value, float) and not value.is_integer():
        raise ModelFormatError(f"{dtype} tensors hold integers, not {value!r}")
    number = int(value)
    low, high = _INT_RANGES[dtype]
    if not low <= number <= high:
        raise ModelFormatError(f"{number} does not fit in {dtype}")
    return number


def _decode_value(value, dtype):
    if dtype in _FLOAT_DTYPES:
        if isinstance(value, str):
            if value not in _NON_FINITE:
                raise ModelFormatError(f"bad float value {value!r}")
            return _NON_FINITE[value]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModelFormatError(f"bad float value {value!r}")
        return float(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelFormatError(f"{dtype} tensors hold integers, not {value!r}")
    low, high = _INT_RANGES[dtype]
    if not low <= value <= high:
        raise ModelFormatError(f"{value} does not fit in {dtype}")
    return value


def _json_object(raw, what):
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ModelFormatError(f"{what} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ModelFormatError(f"{what} is not a JSON object")
    return data


def _check_header(header):
    if header.get("format") != MODEL_FORMAT:
        raise ModelFormatError(f"not a {MODEL_FORMAT} file")
    version = header.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ModelFormatError("the model line has no valid version")
    if version > MODEL_FORMAT_VERSION:
        raise ModelFormatError(
            f"format version {version} is newer than this reader ({MODEL_FORMAT_VERSION})"
        )
    count = header.get("tensor_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ModelFormatError("the model line has no valid tensor_count")
    if not isinstance(header.get("metadata", {}), dict):
        raise ModelFormatError("metadata must be a JSON object")
    for key in ("architecture", "name"):
        if header.get(key) is not None and not isinstance(header[key], str):
            raise ModelFormatError(f"{key} must be a string")
    return header


def read_model_header(path, *, max_line_bytes=64 * 1024 * 1024):
    """The first line (the model object) of an NDJSON model file, validated."""
    with open(path, "rb") as handle:
        line = handle.readline(max_line_bytes + 1)
    if len(line) > max_line_bytes:
        raise ModelFormatError("the model line is too long")
    if not line.strip():
        raise ModelFormatError("the file is empty")
    return _check_header(_json_object(line, "the model line"))


def is_model_file(path):
    """True when ``path`` starts with a valid wsbuilder model line."""
    try:
        with open(path, "rb") as handle:
            if handle.read(1) != b"{":
                return False
        read_model_header(path)
        return True
    except (OSError, ModelFormatError):
        return False


def _encode_bytes(values, dtype):
    """Little-endian bytes of ``values`` stored as ``dtype`` (rounded like JSON values)."""
    if dtype in _BLOCK_DTYPES:
        raise ModelFormatError(f"{dtype} tensors are written from bytes: use add_tensor_bytes")
    if dtype == "BF16":
        return struct.pack(f"<{len(values)}H", *(_bf16_bits(float(v)) for v in values))
    if dtype in _FLOAT_DTYPES:
        numbers = [_round_float(float(value), dtype) for value in values]
    else:
        numbers = [_encode_value(value, dtype) for value in values]
    return struct.pack(f"<{len(numbers)}{_PACK_CODES[dtype]}", *numbers)


def _check_base64(name, buffer, start, length, nbytes):
    """Check the length, padding and alphabet of the base64 text ``buffer[start:start+length]``.

    Nothing is decoded and the text is checked in chunks, so a large tensor
    line is never copied whole.
    """
    padding = (3 - nbytes % 3) % 3
    if length != 4 * -(-nbytes // 3):
        raise ModelFormatError(f"tensor {name!r} has base64 data of the wrong length")
    body_end = start + length - padding
    if buffer[body_end:start + length] != b"=" * padding:
        raise ModelFormatError(f"tensor {name!r} has invalid base64 data")
    for chunk in range(start, body_end, _CHECK_CHUNK):
        if buffer[chunk:min(chunk + _CHECK_CHUNK, body_end)].translate(None, _B64_ALPHABET):
            raise ModelFormatError(f"tensor {name!r} has invalid base64 data")


def _b64decode(name, text):
    try:
        return binascii.a2b_base64(text, strict_mode=True)
    except binascii.Error as exc:
        raise ModelFormatError(f"tensor {name!r} has invalid base64 data") from exc


def _array_from_values(values, dtype):
    if dtype in ("F32", "F16", "BF16"):
        return array.array("f", values)
    if dtype == "F64":
        return array.array("d", values)
    return array.array("q", values)


class ModelTensor:
    """A tensor of a :class:`ModelReader`: its shape now, its values on demand.

    ``encoding`` is ``"json"`` (a list of numbers) or ``"base64"`` (the raw
    little-endian bytes, see the module docstring).
    """

    __slots__ = ("name", "shape", "dtype", "encoding", "nbytes", "_reader", "_offset",
                 "_length", "_data_start", "_data_length")

    def __init__(self, name, shape, dtype, reader, offset, length, *, encoding="json",
                 data_start=None, data_length=None):
        self.name = name
        self.shape = shape
        self.dtype = dtype
        self.encoding = encoding
        self.nbytes = _nbytes(name, dtype, shape)
        self._reader = reader
        self._offset = offset
        self._length = length
        self._data_start = data_start
        self._data_length = data_length

    @property
    def element_count(self):
        return _count(self.shape)

    def _entry(self):
        return self._reader._read_line(self._offset, self._length, self.name)

    def _base64_text(self):
        if self._data_start is not None:
            return self._reader._read_bytes(self._data_start, self._data_length)
        data = self._entry().get("data")
        if not isinstance(data, str):
            raise ModelFormatError(f"tensor {self.name!r} changed on disk")
        return data.encode("ascii", "replace")

    def raw(self):
        """The little-endian tensor bytes (encoded from the values for JSON tensors)."""
        if self.encoding == "base64":
            data = _b64decode(self.name, self._base64_text())
            if len(data) != self.nbytes:
                raise ModelFormatError(f"tensor {self.name!r} changed on disk")
            return data
        return _encode_bytes(self.values(), self.dtype)

    def to_array(self):
        """The values as an ``array.array``: ``"f"`` for F32/F16/BF16/Q8_0/Q4_0,
        ``"d"`` for F64 and an integer typecode for I32/I64."""
        if self.encoding == "base64":
            return _convert(self.dtype, memoryview(self.raw()), self.element_count)
        return _array_from_values(self.values(), self.dtype)

    def values(self):
        """The flat list of values (floats or ints), in row-major order."""
        if self.encoding == "base64":
            return self.to_array().tolist()
        entry = self._entry()
        return [_decode_value(value, self.dtype) for value in entry["data"]]

    def rows(self):
        """A 2-D tensor as a list of rows."""
        if len(self.shape) != 2:
            raise ModelFormatError(f"{self.name!r} has shape {self.shape}, not 2-D")
        flat = self.values()
        width = self.shape[1]
        return [flat[row * width:(row + 1) * width] for row in range(self.shape[0])]

    @property
    def row_reads(self):
        """True when :meth:`row_array` reads only the bytes of the row."""
        return self.encoding == "base64" and self._data_start is not None

    def _check_2d(self):
        if len(self.shape) != 2:
            raise ModelFormatError(f"{self.name!r} has shape {self.shape}, not 2-D")
        return self.shape

    def _rows_bytes(self, first_row, end_row):
        """The decoded bytes of rows ``[first_row, end_row)`` of a base64 tensor."""
        row_bytes = self.nbytes // self.shape[0]
        start = first_row * row_bytes
        end = end_row * row_bytes
        # base64 maps 3 bytes to 4 characters: read the aligned window around the rows.
        first = start - start % 3
        last = min(-(-end // 3) * 3, self.nbytes)
        text = self._reader._read_bytes(self._data_start + first // 3 * 4,
                                        -(-last // 3) * 4 - first // 3 * 4)
        data = _b64decode(self.name, text)[start - first:end - first]
        if len(data) != end - start:
            raise ModelFormatError(f"tensor {self.name!r} changed on disk")
        return data

    def row_array(self, index):
        """Row ``index`` of a 2-D tensor as an ``array.array`` (see :meth:`to_array`).

        For base64 tensors only the base64 text of that row is read and
        decoded, so a large embedding can be looked up without loading it.
        """
        rows, width = self._check_2d()
        if type(index) is not int or not 0 <= index < rows:
            raise IndexError(f"row {index!r} is out of range for {self.name!r}")
        if not self.row_reads:
            return self.to_array()[index * width:(index + 1) * width]
        return _convert(self.dtype, memoryview(self._rows_bytes(index, index + 1)), width)

    def row_arrays(self):
        """Yield every row of a 2-D tensor as an ``array.array``.

        Base64 tensors are read and decoded about 1 MiB at a time, so the
        whole tensor is never in memory twice.
        """
        rows, width = self._check_2d()
        if not self.row_reads:
            flat = self.to_array()
            for row in range(rows):
                yield flat[row * width:(row + 1) * width]
            return
        step = max(1, _CHECK_CHUNK // max(1, self.nbytes // rows))
        for first in range(0, rows, step):
            last = min(rows, first + step)
            flat = _convert(self.dtype, memoryview(self._rows_bytes(first, last)),
                            (last - first) * width)
            for row in range(last - first):
                yield flat[row * width:(row + 1) * width]

    def __repr__(self):
        return f"<ModelTensor {self.name} {self.dtype} {list(self.shape)}>"


def _fast_base64_entry(line):
    """Split a canonical base64 tensor line without parsing its (large) data string.

    Writers put ``"data":"..."`` last; base64 has no quote or backslash, so
    the last ``"data":"`` starts the data. Returns ``(entry, start, length)``
    with ``entry["data"] = None``, or ``None`` when the line has another layout.
    """
    end = len(line)
    while end and line[end - 1] in _JSON_SPACE:
        end -= 1
    if end < 2 or line[end - 2:end] != b'"}':
        return None
    position = line.rfind(_DATA_KEY, 0, end)
    before = position - 1
    while before >= 0 and line[before] in _JSON_SPACE:
        before -= 1
    if position < 1 or before < 0 or line[before] not in b",{":
        return None
    start = position + len(_DATA_KEY)
    if line.find(b'"', start, end - 2) >= 0 or line.find(b"\\", start, end - 2) >= 0:
        return None
    try:
        entry = json.loads(line[:position] + b'"data":null}')
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("encoding") != "base64":
        return None
    return entry, start, end - 2 - start


class ModelReader:
    """Read a wsbuilder NDJSON model file.

    Opening checks the model line and every tensor line (names, dtypes,
    shapes and value counts; for base64 tensors the length, padding and
    alphabet of the data) and remembers where each tensor is, so
    :meth:`tensor` reads only the line it needs. Reads are thread-safe. Use
    it as a context manager or call :meth:`close`.
    """

    def __init__(self, path, *, max_line_bytes=DEFAULT_MAX_LINE_BYTES):
        self.path = os.fspath(path)
        self.max_line_bytes = int(max_line_bytes)
        self.file_size = os.path.getsize(self.path)
        self._lock = threading.Lock()
        self._file = open(self.path, "rb")
        try:
            self._index()
        except BaseException:
            self._file.close()
            raise

    def _index(self):
        handle = self._file
        first = handle.readline(self.max_line_bytes + 1)
        if len(first) > self.max_line_bytes:
            raise ModelFormatError("the model line is too long")
        if not first.strip():
            raise ModelFormatError("the file is empty")
        self.header = _check_header(_json_object(first, "the model line"))
        self.metadata = dict(self.header.get("metadata") or {})
        self.tensors = []
        self._by_name = {}
        line_number = 1
        while True:
            offset = handle.tell()
            line = handle.readline(self.max_line_bytes + 1)
            if not line:
                break
            line_number += 1
            if len(line) > self.max_line_bytes:
                raise ModelFormatError(f"line {line_number} is longer than max_line_bytes")
            if not line.strip():
                continue
            fast = _fast_base64_entry(line)
            if fast is not None:
                entry, start, length = fast
                tensor = self._check_tensor(entry, line_number, offset, len(line),
                                            data=(line, start, length),
                                            data_start=offset + start)
            else:
                entry = _json_object(line, f"line {line_number}")
                tensor = self._check_tensor(entry, line_number, offset, len(line))
            if tensor.name in self._by_name:
                raise ModelFormatError(f"tensor {tensor.name!r} appears twice")
            self._by_name[tensor.name] = tensor
            self.tensors.append(tensor)
        expected = self.header["tensor_count"]
        if len(self.tensors) != expected:
            raise ModelFormatError(
                f"the model line announces {expected} tensors but the file has {len(self.tensors)}"
            )

    def _check_tensor(self, entry, line_number, offset, length, *, data=None, data_start=None):
        name = entry.get("tensor")
        if not isinstance(name, str) or not name or len(name) > _NAME_LIMIT:
            raise ModelFormatError(f"line {line_number} has no valid tensor name")
        dtype = _dtype(entry.get("dtype", "F64"))
        shape = _shape(entry.get("shape"))
        encoding = entry.get("encoding", "json")
        if encoding not in MODEL_ENCODINGS:
            raise ModelFormatError(f"tensor {name!r} has an unknown encoding {encoding!r}")
        nbytes = _nbytes(name, dtype, shape)
        if encoding == "base64":
            if data is None:
                text = entry.get("data")
                if not isinstance(text, str):
                    raise ModelFormatError(f"tensor {name!r} has no base64 data string")
                text = text.encode("ascii", "replace")
                data = (text, 0, len(text))
            _check_base64(name, *data, nbytes)
            return ModelTensor(name, shape, dtype, self, offset, length, encoding="base64",
                               data_start=data_start,
                               data_length=None if data_start is None else data[2])
        if dtype in _BLOCK_DTYPES:
            raise ModelFormatError(f"tensor {name!r}: {dtype} tensors must use base64")
        data = entry.get("data")
        if not isinstance(data, list):
            raise ModelFormatError(f"tensor {name!r} has no data list")
        if len(data) != _count(shape):
            raise ModelFormatError(
                f"tensor {name!r} has {len(data)} values but shape {list(shape)} needs {_count(shape)}"
            )
        for value in data:
            _decode_value(value, dtype)
        return ModelTensor(name, shape, dtype, self, offset, length)

    def _read_bytes(self, offset, length):
        with self._lock:
            if self._file is None:
                raise ModelFormatError("the reader is closed")
            self._file.seek(offset)
            data = self._file.read(length)
        if len(data) != length:
            raise ModelFormatError("the file changed on disk")
        return data

    def _read_line(self, offset, length, name):
        raw = self._read_bytes(offset, length)
        entry = _json_object(raw, f"tensor {name!r}")
        if entry.get("tensor") != name:
            raise ModelFormatError(f"tensor {name!r} changed on disk")
        return entry

    @property
    def architecture(self):
        return self.header.get("architecture")

    @property
    def name(self):
        return self.header.get("name")

    @property
    def version(self):
        return self.header["version"]

    @property
    def parameter_count(self):
        return sum(tensor.element_count for tensor in self.tensors)

    @property
    def closed(self):
        return self._file is None

    def tensor(self, name):
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no tensor {name!r}") from None

    def names(self):
        return [tensor.name for tensor in self.tensors]

    def __contains__(self, name):
        return name in self._by_name

    def summary(self):
        """What someone choosing or loading the model wants to know."""
        dtypes = {}
        for tensor in self.tensors:
            dtypes[tensor.dtype] = dtypes.get(tensor.dtype, 0) + tensor.element_count
        return {
            "format": "ndjson",
            "version": self.version,
            "name": self.name,
            "architecture": self.architecture,
            "parameters": self.parameter_count,
            "tensors": len(self.tensors),
            "dtypes": dtypes,
            "metadata": self.metadata,
            "file_size": self.file_size,
        }

    def close(self):
        with self._lock:
            handle, self._file = self._file, None
        if handle is not None:
            handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return f"<ModelReader {self.path} {self.architecture} {len(self.tensors)} tensors>"


def _check_name(name, names):
    name = str(name)
    if not name or len(name) > _NAME_LIMIT:
        raise ModelFormatError("tensor names are 1 to 1024 characters")
    if name in names:
        raise ModelFormatError(f"tensor {name!r} was already added")
    return name


class ModelWriter:
    """Build a wsbuilder NDJSON model file.

    ``add_metadata`` stores any JSON value; ``add_tensor`` takes a flat
    sequence of numbers (or a list of rows for 2-D data) and its shape, and
    ``encoding="base64"`` stores them as compact bytes; ``add_tensor_bytes``
    takes the little-endian bytes directly (any dtype, including ``Q8_0`` and
    ``Q4_0``). :meth:`write` writes the file atomically; :meth:`to_bytes`
    returns it.
    """

    def __init__(self, *, architecture=None, name=None):
        self.architecture = None if architecture is None else str(architecture)
        self.name = None if name is None else str(name)
        self.metadata = {}
        self._tensors = []
        self._names = set()

    def add_metadata(self, key, value):
        key = str(key)
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ModelFormatError(f"metadata {key!r} is not plain JSON: {exc}") from exc
        self.metadata[key] = value
        return self

    def add_tensor(self, name, values, *, shape=None, dtype="F64", encoding="json"):
        name = _check_name(name, self._names)
        dtype = _dtype(dtype)
        if encoding not in MODEL_ENCODINGS:
            raise ModelFormatError(f"unknown encoding {encoding!r}; use one of {MODEL_ENCODINGS}")
        if dtype in _BLOCK_DTYPES:
            raise ModelFormatError(f"{dtype} tensors are written from bytes: use add_tensor_bytes")
        values = list(values)
        if values and isinstance(values[0], (list, tuple)):
            if shape is None:
                shape = (len(values), len(values[0]))
            values = [value for row in values for value in row]
        dims = _shape(shape if shape is not None else (len(values),))
        if len(values) != _count(dims):
            raise ModelFormatError(
                f"tensor {name!r} has {len(values)} values but shape {list(dims)} needs {_count(dims)}"
            )
        if encoding == "base64":
            data = _encode_bytes(values, dtype)
            self._tensors.append({"tensor": name, "shape": list(dims), "dtype": dtype,
                                  "encoding": "base64", "bytes": data})
        else:
            data = [_encode_value(value, dtype) for value in values]
            self._tensors.append({"tensor": name, "shape": list(dims), "dtype": dtype,
                                  "data": data})
        self._names.add(name)
        return self

    def add_tensor_bytes(self, name, data, *, shape, dtype):
        """Add a tensor from its little-endian bytes, stored as base64.

        ``data`` is bytes-like, or a function without arguments that returns
        them when the file is written (so a converter keeps one tensor in
        memory at a time). The length must match ``shape`` and ``dtype``.
        """
        name = _check_name(name, self._names)
        dtype = _dtype(dtype)
        dims = _shape(shape)
        nbytes = _nbytes(name, dtype, dims)
        if not callable(data):
            data = bytes(data)
            if len(data) != nbytes:
                raise ModelFormatError(
                    f"tensor {name!r} has {len(data)} bytes but shape {list(dims)} of {dtype} "
                    f"needs {nbytes}"
                )
        self._tensors.append({"tensor": name, "shape": list(dims), "dtype": dtype,
                              "encoding": "base64", "bytes": data, "nbytes": nbytes})
        self._names.add(name)
        return self

    def _header(self):
        version = 1
        for tensor in self._tensors:
            if tensor.get("encoding") == "base64" or tensor["dtype"] not in _V1_DTYPES:
                version = 2
        header = {"format": MODEL_FORMAT, "version": version}
        if self.architecture is not None:
            header["architecture"] = self.architecture
        if self.name is not None:
            header["name"] = self.name
        header["metadata"] = self.metadata
        header["tensor_count"] = len(self._tensors)
        return header

    @staticmethod
    def _dumps(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def _parts(self):
        """The file as text pieces (a large base64 string is one piece, never copied)."""
        yield self._dumps(self._header()) + "\n"
        for tensor in self._tensors:
            if tensor.get("encoding") != "base64":
                yield self._dumps(tensor) + "\n"
                continue
            data = tensor["bytes"]
            if callable(data):
                data = bytes(data())
                expected = tensor["nbytes"]
                if len(data) != expected:
                    raise ModelFormatError(
                        f"tensor {tensor['tensor']!r} produced {len(data)} bytes, not {expected}"
                    )
            meta = {key: tensor[key] for key in ("tensor", "shape", "dtype", "encoding")}
            # "data" goes last so readers can index the line without parsing it.
            yield self._dumps(meta)[:-1] + ',"data":"'
            with memoryview(data) as view:
                for start in range(0, len(view), _B64_CHUNK):
                    yield base64.b64encode(view[start:start + _B64_CHUNK]).decode("ascii")
            yield '"}\n'

    def to_bytes(self):
        return "".join(self._parts()).encode("utf-8")

    def write(self, path):
        """Write the file (via a temporary file and ``os.replace``); returns ``path``."""
        path = os.fspath(path)
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        temporary = os.path.join(directory, f".{os.path.basename(path)}.tmp-{uuid.uuid4().hex}")
        try:
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                for part in self._parts():
                    handle.write(part)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
        return path

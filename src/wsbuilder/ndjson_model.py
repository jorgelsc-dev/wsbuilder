"""The wsbuilder model format: a model and its weights in one NDJSON file.

NDJSON is JSON with one object per line. A model file has:

* **Line 1, the model**: ``{"format": "wsbuilder-model", "version": 1,
  "architecture": ..., "name": ..., "metadata": {...}, "tensor_count": N}``.
* **Lines 2 to N+1, the weights**: one tensor per line,
  ``{"tensor": "blk.0.weight", "shape": [2, 3], "dtype": "F32",
  "data": [0.1, 0.2, ...]}``. ``data`` is flat, in row-major order, and holds
  exactly ``prod(shape)`` numbers.

Supported dtypes: ``F64`` (exact), ``F32`` and ``F16`` (values rounded to
that precision when written), ``I32`` and ``I64``. JSON has no NaN or
infinity, so those float values are written as the strings ``"NaN"``,
``"Infinity"`` and ``"-Infinity"`` and read back as floats.

The file is plain text, so any language with a JSON parser can read it line
by line, and ``grep``/``head`` work on it. :class:`ModelReader` indexes the
tensors when it opens a file and reads each one on demand, so the whole file
never needs to be in memory. :class:`ModelWriter` writes atomically.
Everything here uses the standard library only.
"""

import json
import math
import os
import struct
import uuid

__all__ = [
    "MODEL_FORMAT",
    "MODEL_FORMAT_VERSION",
    "MODEL_DTYPES",
    "ModelFormatError",
    "ModelReader",
    "ModelTensor",
    "ModelWriter",
    "is_model_file",
    "read_model_header",
]

MODEL_FORMAT = "wsbuilder-model"
MODEL_FORMAT_VERSION = 1
MODEL_DTYPES = ("F64", "F32", "F16", "I32", "I64")
_FLOAT_DTYPES = ("F64", "F32", "F16")
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


def _round_float(value, dtype):
    if dtype == "F64" or not math.isfinite(value):
        return value
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


class ModelTensor:
    """A tensor of a :class:`ModelReader`: its shape now, its values on demand."""

    __slots__ = ("name", "shape", "dtype", "_reader", "_offset", "_length")

    def __init__(self, name, shape, dtype, reader, offset, length):
        self.name = name
        self.shape = shape
        self.dtype = dtype
        self._reader = reader
        self._offset = offset
        self._length = length

    @property
    def element_count(self):
        return _count(self.shape)

    def values(self):
        """The flat list of values (floats or ints), in row-major order."""
        entry = self._reader._read_line(self._offset, self._length, self.name)
        return [_decode_value(value, self.dtype) for value in entry["data"]]

    def rows(self):
        """A 2-D tensor as a list of rows."""
        if len(self.shape) != 2:
            raise ModelFormatError(f"{self.name!r} has shape {self.shape}, not 2-D")
        flat = self.values()
        width = self.shape[1]
        return [flat[row * width:(row + 1) * width] for row in range(self.shape[0])]

    def __repr__(self):
        return f"<ModelTensor {self.name} {self.dtype} {list(self.shape)}>"


class ModelReader:
    """Read a wsbuilder NDJSON model file.

    Opening checks the model line and every tensor line (names, dtypes,
    shapes and value counts) and remembers where each tensor is, so
    :meth:`tensor` reads only the line it needs. Use it as a context
    manager or call :meth:`close`.
    """

    def __init__(self, path, *, max_line_bytes=DEFAULT_MAX_LINE_BYTES):
        self.path = os.fspath(path)
        self.max_line_bytes = int(max_line_bytes)
        self.file_size = os.path.getsize(self.path)
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

    def _check_tensor(self, entry, line_number, offset, length):
        name = entry.get("tensor")
        if not isinstance(name, str) or not name or len(name) > _NAME_LIMIT:
            raise ModelFormatError(f"line {line_number} has no valid tensor name")
        dtype = _dtype(entry.get("dtype", "F64"))
        shape = _shape(entry.get("shape"))
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

    def _read_line(self, offset, length, name):
        if self._file is None:
            raise ModelFormatError("the reader is closed")
        self._file.seek(offset)
        raw = self._file.read(length)
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

    def tensor(self, name):
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no tensor {name!r}") from None

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
        handle, self._file = self._file, None
        if handle is not None:
            handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return f"<ModelReader {self.path} {self.architecture} {len(self.tensors)} tensors>"


class ModelWriter:
    """Build a wsbuilder NDJSON model file.

    ``add_metadata`` stores any JSON value; ``add_tensor`` takes a flat
    sequence of numbers (or a list of rows for 2-D data) and its shape.
    :meth:`write` writes the file atomically; :meth:`to_bytes` returns it.
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

    def add_tensor(self, name, values, *, shape=None, dtype="F64"):
        name = str(name)
        if not name or len(name) > _NAME_LIMIT:
            raise ModelFormatError("tensor names are 1 to 1024 characters")
        if name in self._names:
            raise ModelFormatError(f"tensor {name!r} was already added")
        dtype = _dtype(dtype)
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
        data = [_encode_value(value, dtype) for value in values]
        self._tensors.append({"tensor": name, "shape": list(dims), "dtype": dtype, "data": data})
        self._names.add(name)
        return self

    def _header(self):
        header = {"format": MODEL_FORMAT, "version": MODEL_FORMAT_VERSION}
        if self.architecture is not None:
            header["architecture"] = self.architecture
        if self.name is not None:
            header["name"] = self.name
        header["metadata"] = self.metadata
        header["tensor_count"] = len(self._tensors)
        return header

    def _lines(self):
        yield json.dumps(self._header(), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        for tensor in self._tensors:
            yield json.dumps(tensor, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def to_bytes(self):
        return "".join(line + "\n" for line in self._lines()).encode("utf-8")

    def write(self, path):
        """Write the file (via a temporary file and ``os.replace``); returns ``path``."""
        path = os.fspath(path)
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        temporary = os.path.join(directory, f".{os.path.basename(path)}.tmp-{uuid.uuid4().hex}")
        try:
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                for line in self._lines():
                    handle.write(line)
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
        return path

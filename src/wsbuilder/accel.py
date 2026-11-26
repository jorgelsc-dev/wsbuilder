"""Linear algebra backends of the graph engine (:mod:`wsbuilder.graph`).

The graph engine spends almost all its time in matrix-vector products. This
module gives it one interface with three implementations, all written in
this repository and using only the standard library:

* ``python``: ``sum(map(operator.mul, row, x))`` per row, the reference.
* ``native``: ``_accel_kernels.c`` (shipped with the package) compiled on
  demand with the system C compiler (``$CC``, the compiler Python was built
  with, ``cc``, ``gcc`` or ``clang``) and called through :mod:`ctypes`. The
  matrices keep their file dtype (F32, F64, F16, BF16, Q8_0 or Q4_0) in one
  contiguous buffer, products accumulate in double and large matrices are
  split by rows over a thread pool (ctypes releases the GIL). Attention runs
  in C too.
* ``process``: no compiler needed. Large matrices live in
  :mod:`multiprocessing.shared_memory` and a pool of processes computes row
  ranges with the Python reference loop, so the results are the same as
  ``python``; small matrices stay local because the IPC would cost more.

:func:`create_backend` picks one: ``auto`` (the default, also read from
``$WSBUILDER_ACCEL``) tries ``native`` and falls back to ``python``;
``process`` runs only when asked for, since its workers re-import the main
script like any ``spawn``/``forkserver`` :mod:`multiprocessing` code.

The compiled library is cached in ``$WSBUILDER_CACHE/accel`` (default
``~/.cache/wsbuilder/accel``): a directory created with mode 0700 and refused
when it is not owned by the user or is writable by others. Its file name
is a SHA-256 of the C source, the platform, the Python version and the
compiler command; a sidecar records the SHA-256 of the library built, and a
library whose sidecar or bytes do not match is rebuilt, never loaded. A
file lock in the directory serialises the check, the build and the load
between processes; builds are moved into place atomically. Libraries of
older versions stay in the directory until it is deleted.
"""

import array
import ctypes
import hashlib
import json
import math
import multiprocessing
import operator
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import threading
import weakref
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from importlib import resources
from itertools import chain
from multiprocessing import shared_memory

from .logs import get_log

try:
    import fcntl
except ImportError:  # not POSIX: the native backend is unavailable
    fcntl = None
from .weights import _convert

__all__ = [
    "ACCEL_BACKENDS",
    "AccelBackend",
    "AccelError",
    "NativeAccel",
    "ProcessAccel",
    "PythonAccel",
    "create_backend",
    "describe",
]

ACCEL_BACKENDS = ("auto", "native", "process", "python")
ENV_ACCEL = "WSBUILDER_ACCEL"
ENV_CACHE = "WSBUILDER_CACHE"
KERNEL_SOURCE = "_accel_kernels.c"
#: Compiler flags. No -march=native (the cache may be copied to another CPU),
#: no -ffast-math and no fused multiply-add (-ffp-contract=off): the results
#: must not depend on the CPU or the compiler's defaults.
CFLAGS = ("-O3", "-ffp-contract=off", "-fPIC", "-shared")
COMPILE_TIMEOUT_SECONDS = 120
#: Multiply-adds per native thread: smaller matrices use fewer threads (waking
#: a thread costs about as much as 10^5 multiply-adds), down to one call.
NATIVE_CHUNK_MACS = 1 << 18
#: Matrices with fewer multiply-adds stay in the parent process.
PROCESS_MIN_MACS = 1 << 18
MAX_THREADS = 32
_ABI_VERSION = 1
_DTYPE_CODES = {"F32": 0, "F64": 1, "F16": 2, "BF16": 3, "Q8_0": 4, "Q4_0": 5}
_MAX_SIDECAR_BYTES = 4096
_LITTLE = sys.byteorder == "little"
_mul = operator.mul
_add = operator.add


class AccelError(RuntimeError):
    """A backend that cannot run here (no compiler, failed build, unsafe cache)."""


def _softmax(scores):
    peak = max(scores)
    exps = [math.exp(score - peak) for score in scores]
    total = sum(exps)
    return [value / total for value in exps]


def _thread_count(value):
    if value is None:
        return max(1, min(MAX_THREADS, os.cpu_count() or 1))
    if type(value) is not int or value < 1:
        raise ValueError("threads must be a positive integer")
    return value


def _row_ranges(rows, parts):
    parts = max(1, min(parts, rows))
    step, extra = divmod(rows, parts)
    start = 0
    for index in range(parts):
        end = start + step + (1 if index < extra else 0)
        yield start, end
        start = end


def _check_attention(q, keys, values, heads, kv_heads, head_dim, start):
    """Check the shapes of an attention call; returns the cached length."""
    for name, value in (("heads", heads), ("kv_heads", kv_heads), ("head_dim", head_dim)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if heads % kv_heads:
        raise ValueError("heads must be a multiple of kv_heads")
    if len(q) != heads * head_dim:
        raise ValueError(f"the query has {len(q)} values, expected {heads * head_dim}")
    if len(keys) != kv_heads or len(values) != kv_heads:
        raise ValueError(f"the cache must have {kv_heads} key and value rows")
    length = len(keys[0]) // head_dim
    for rows in (*keys, *values):
        if len(rows) != length * head_dim:
            raise ValueError("every KV head must hold the same positions of head_dim values")
    if type(start) is not int or not 0 <= start < length:
        raise ValueError(f"start must be in [0, {length})")
    return length


def _rows_of(source):
    """The rows of ``source``: a tensor (``row_arrays()``) or an iterable of rows."""
    rows = getattr(source, "row_arrays", None)
    return rows() if callable(rows) else iter(source)


def _exact_row(row):
    """``row`` as an array that keeps its values exactly."""
    if isinstance(row, array.array) and row.typecode in "fd":
        return row
    return array.array("d", row)


# -- matrices --------------------------------------------------------------


class _RowsMatrix:
    """A matrix as a list of ``array`` rows (python backend, small process matrices)."""

    __slots__ = ("rows", "cols", "row_list")

    def __init__(self, rows, cols, source):
        self.rows = rows
        self.cols = cols
        self.row_list = []
        for row in _rows_of(source):
            row = _exact_row(row)
            if len(row) != cols:
                raise ValueError(f"a matrix row has {len(row)} values, expected {cols}")
            self.row_list.append(row)
        if len(self.row_list) != rows:
            raise ValueError(f"the matrix has {len(self.row_list)} rows, expected {rows}")

    def row(self, index):
        return list(self.row_list[index])


class _NativeMatrix:
    """One contiguous buffer: the raw file bytes, or float32/float64 values."""

    __slots__ = ("rows", "cols", "dtype", "code", "buffer", "address", "row_bytes")

    def __init__(self, rows, cols, source):
        self.rows = rows
        self.cols = cols
        dtype = getattr(source, "dtype", None)
        if (_LITTLE and dtype in _DTYPE_CODES and getattr(source, "encoding", None) == "base64"
                and callable(getattr(source, "raw_chunks", None))):
            self._from_raw(source, dtype)
        else:
            self._from_rows(source)
        self.address = self.buffer.buffer_info()[0]

    def _from_raw(self, tensor, dtype):
        if dtype in ("Q8_0", "Q4_0"):
            if self.cols % 32:
                raise ValueError(f"{dtype} rows must hold a multiple of 32 values")
            row_bytes = self.cols // 32 * (34 if dtype == "Q8_0" else 18)
        else:
            row_bytes = self.cols * (8 if dtype == "F64" else 4 if dtype == "F32" else 2)
        total = row_bytes * self.rows
        buffer = array.array("B", [0]) * max(1, total)
        view = memoryview(buffer)
        position = 0
        try:
            for chunk in tensor.raw_chunks():
                end = position + len(chunk)
                if end > total:
                    raise ValueError("the tensor has more bytes than its shape")
                view[position:end] = chunk
                position = end
        finally:
            view.release()
        if position != total:
            raise ValueError("the tensor has fewer bytes than its shape")
        self.dtype = dtype
        self.code = _DTYPE_CODES[dtype]
        self.buffer = buffer
        self.row_bytes = row_bytes

    def _from_rows(self, source):
        iterator = _rows_of(source)
        first = next(iterator, None)
        if first is None:
            raise ValueError("the matrix has no rows")
        first = _exact_row(first)
        typecode = first.typecode
        buffer = array.array(typecode, [0.0]) * max(1, self.rows * self.cols)
        view = memoryview(buffer)
        count = 0
        try:
            for index, row in enumerate(chain([first], iterator)):
                if index >= self.rows:
                    raise ValueError(f"the matrix has more than {self.rows} rows")
                if not (isinstance(row, array.array) and row.typecode == typecode):
                    row = array.array(typecode, row)
                if len(row) != self.cols:
                    raise ValueError(f"a matrix row has {len(row)} values, expected {self.cols}")
                view[index * self.cols:(index + 1) * self.cols] = row
                count += 1
        finally:
            view.release()
        if count != self.rows:
            raise ValueError(f"the matrix has {count} rows, expected {self.rows}")
        self.dtype = "F64" if typecode == "d" else "F32"
        self.code = _DTYPE_CODES[self.dtype]
        self.buffer = buffer
        self.row_bytes = self.cols * buffer.itemsize

    def row(self, index):
        if not 0 <= index < self.rows:
            raise IndexError(f"row {index} is out of range")
        if self.buffer.typecode != "B":
            return self.buffer[index * self.cols:(index + 1) * self.cols].tolist()
        start = index * self.row_bytes
        with memoryview(self.buffer)[start:start + self.row_bytes] as view:
            return _convert(self.dtype, view, self.cols).tolist()


class _SharedMatrix:
    """A matrix in a shared memory segment, float32 or float64, row-major."""

    __slots__ = ("rows", "cols", "typecode", "segment", "name")

    def __init__(self, rows, cols, source, segment_factory):
        self.rows = rows
        self.cols = cols
        iterator = _rows_of(source)
        first = next(iterator, None)
        if first is None:
            raise ValueError("the matrix has no rows")
        first = _exact_row(first)
        self.typecode = first.typecode
        itemsize = array.array(self.typecode).itemsize
        self.segment = segment_factory(rows * cols * itemsize)
        self.name = self.segment.name
        count = 0
        with self.segment.buf[:rows * cols * itemsize] as raw, raw.cast(self.typecode) as view:
            for index, row in enumerate(chain([first], iterator)):
                if index >= rows:
                    raise ValueError(f"the matrix has more than {rows} rows")
                if not (isinstance(row, array.array) and row.typecode == self.typecode):
                    row = array.array(self.typecode, row)
                if len(row) != cols:
                    raise ValueError(f"a matrix row has {len(row)} values, expected {cols}")
                view[index * cols:(index + 1) * cols] = row
                count += 1
        if count != rows:
            raise ValueError(f"the matrix has {count} rows, expected {rows}")

    def row(self, index):
        if not 0 <= index < self.rows:
            raise IndexError(f"row {index} is out of range")
        itemsize = array.array(self.typecode).itemsize
        start = index * self.cols * itemsize
        with self.segment.buf[start:start + self.cols * itemsize] as raw:
            with raw.cast(self.typecode) as view:
                return view.tolist()


# -- backends --------------------------------------------------------------


class AccelBackend:
    """The operations the graph engine needs; :class:`PythonAccel` is the reference.

    ``prepare_matrix(rows, cols, source)`` stores a matrix once (``source``
    is a :class:`~wsbuilder.ndjson_model.ModelTensor` or an iterable of
    rows) and returns an object with ``rows``, ``cols`` and ``row(index)``;
    ``matvec(matrix, x, bias)`` multiplies it by the vector ``x``;
    ``attend(...)`` runs the attention of one token over a KV cache.
    """

    name = "base"

    def prepare_matrix(self, rows, cols, source):
        raise NotImplementedError

    def prepare_vector(self, values):
        """A bias vector in the form :meth:`matvec` takes."""
        return [float(value) for value in values]

    def matvec(self, matrix, x, bias=None):
        raise NotImplementedError

    def attend(self, q, keys, values, heads, kv_heads, head_dim, start, scale, softcap):
        """Attention of one query (``heads`` x ``head_dim`` floats) over the cache.

        ``keys[h]``/``values[h]`` are ``array("d")`` with one ``head_dim`` row per
        position of KV head ``h``; positions ``start`` and later are attended.
        Returns ``heads x head_dim`` floats.
        """
        _check_attention(q, keys, values, heads, kv_heads, head_dim, start)
        group = heads // kv_heads
        result = []
        for head in range(heads):
            query = q[head * head_dim:(head + 1) * head_dim]
            key_rows = keys[head // group]
            value_rows = values[head // group]
            length = len(key_rows) // head_dim
            scores = [sum(map(_mul, query, key_rows[index * head_dim:(index + 1) * head_dim]))
                      * scale for index in range(start, length)]
            if softcap is not None:
                scores = [math.tanh(score / softcap) * softcap for score in scores]
            weights = _softmax(scores)
            base = start * head_dim
            result += [sum(map(_mul, weights, value_rows[base + column::head_dim]))
                       for column in range(head_dim)]
        return result

    def describe(self):
        return {"name": self.name}

    def close(self):
        """Free pools and buffers (idempotent)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PythonAccel(AccelBackend):
    """Pure Python: ``sum(map(operator.mul, row, x))`` for each ``array`` row."""

    name = "python"

    def prepare_matrix(self, rows, cols, source):
        return _RowsMatrix(rows, cols, source)

    def matvec(self, matrix, x, bias=None):
        result = [sum(map(_mul, row, x)) for row in matrix.row_list]
        return result if bias is None else list(map(_add, result, bias))


class NativeAccel(AccelBackend):
    """C kernels compiled on demand (see the module docstring).

    ``threads`` (default: the CPU count, at most 32) split large matrices by
    rows, one range per ``chunk_macs`` multiply-adds at most. ``compiler`` overrides
    ``$CC``; ``cache_dir`` the cache directory. Raises :class:`AccelError`
    when the library cannot be built or loaded.
    """

    name = "native"

    def __init__(self, *, threads=None, cache_dir=None, compiler=None,
                 chunk_macs=NATIVE_CHUNK_MACS):
        self.threads = _thread_count(threads)
        self.chunk_macs = max(1, int(chunk_macs))
        library, self.library_path, self.compiled = load_native_library(
            cache_dir=cache_dir, compiler=compiler)
        self._matvec = library.wsb_matvec
        self._attend = library.wsb_attend
        self._pool = None
        self._pool_lock = threading.Lock()
        self._closed = False

    def prepare_matrix(self, rows, cols, source):
        return _NativeMatrix(rows, cols, source)

    def prepare_vector(self, values):
        return array.array("d", values)

    def _executor(self):
        with self._pool_lock:
            if self._closed:
                raise ValueError("the backend is closed")
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.threads - 1,
                                                thread_name_prefix="wsbuilder-accel")
            return self._pool

    def matvec(self, matrix, x, bias=None):
        rows, cols = matrix.rows, matrix.cols
        if len(x) != cols:
            raise ValueError(f"the vector has {len(x)} values, expected {cols}")
        if bias is not None and len(bias) != rows:
            raise ValueError(f"the bias has {len(bias)} values, expected {rows}")
        if bias is not None and not (isinstance(bias, array.array) and bias.typecode == "d"):
            bias = array.array("d", bias)
        xs = array.array("d", x)
        out = array.array("d", [0.0]) * rows
        args = (matrix.code, matrix.address, xs.buffer_info()[0],
                None if bias is None else bias.buffer_info()[0], out.buffer_info()[0], rows, cols)
        parts = min(self.threads, rows, rows * cols // self.chunk_macs)
        if parts > 1:
            ranges = list(_row_ranges(rows, parts))
            pool = self._executor()
            try:
                futures = [pool.submit(self._matvec, *args, start, end)
                           for start, end in ranges[1:]]
            except RuntimeError as exc:  # close() shut the pool down meanwhile
                raise ValueError("the backend is closed") from exc
            codes = [self._matvec(*args, *ranges[0])] + [future.result() for future in futures]
        else:
            codes = [self._matvec(*args, 0, rows)]
        if any(codes):
            raise AccelError("the native matvec kernel refused its arguments")
        return out.tolist()

    def attend(self, q, keys, values, heads, kv_heads, head_dim, start, scale, softcap):
        length = _check_attention(q, keys, values, heads, kv_heads, head_dim, start)
        group = heads // kv_heads
        queries = array.array("d", q)
        out = array.array("d", [0.0]) * (heads * head_dim)
        scores = array.array("d", [0.0]) * max(1, length - start)
        stride = group * head_dim * queries.itemsize
        q_address = queries.buffer_info()[0]
        out_address = out.buffer_info()[0]
        scores_address = scores.buffer_info()[0]
        cap = 0.0 if softcap is None else float(softcap)
        for kv in range(kv_heads):
            key_rows, value_rows = keys[kv], values[kv]
            if key_rows.typecode != "d" or value_rows.typecode != "d":
                raise ValueError("the KV cache must hold array('d') rows")
            code = self._attend(q_address + kv * stride, key_rows.buffer_info()[0],
                                value_rows.buffer_info()[0], out_address + kv * stride,
                                scores_address, group, head_dim, start, length, float(scale), cap)
            if code:
                raise AccelError("the native attention kernel refused its arguments")
        return out.tolist()

    def describe(self):
        return {"name": self.name, "threads": self.threads, "library": self.library_path,
                "compiled_now": self.compiled}

    def close(self):
        with self._pool_lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)


# -- process backend -------------------------------------------------------

_WORKER_SEGMENTS = {}


def _open_segment(name):
    """Attach a segment without registering it with the resource tracker
    (the parent owns and unlinks it)."""
    if sys.version_info >= (3, 13):
        return shared_memory.SharedMemory(name=name, track=False)
    from multiprocessing import resource_tracker

    register = resource_tracker.register
    resource_tracker.register = lambda *_args, **_kwargs: None
    try:
        return shared_memory.SharedMemory(name=name)
    finally:
        resource_tracker.register = register


def _worker_segment(name):
    segment = _WORKER_SEGMENTS.get(name)
    if segment is None:
        segment = _WORKER_SEGMENTS[name] = _open_segment(name)
    return segment


def _shared_rows(matrix_segment, typecode, cols, x_segment, out_segment, start, end):
    """Rows ``[start, end)`` of a shared matrix times the shared vector, into ``out``."""
    with x_segment.buf[:cols * 8] as raw, raw.cast("d") as view:
        x = view.tolist()
    itemsize = array.array(typecode).itemsize
    results = array.array("d")
    with matrix_segment.buf[start * cols * itemsize:end * cols * itemsize] as raw:
        with raw.cast(typecode) as rows:
            for index in range(end - start):
                results.append(sum(map(_mul, rows[index * cols:(index + 1) * cols], x)))
    with out_segment.buf[start * 8:end * 8] as raw, raw.cast("d") as view:
        view[:] = results
    return end - start


def _process_rows(matrix_name, typecode, cols, x_name, out_name, start, end):
    """Worker process entry point (see :func:`_shared_rows`)."""
    return _shared_rows(_worker_segment(matrix_name), typecode, cols, _worker_segment(x_name),
                        _worker_segment(out_name), start, end)


def _free_segments(segments):
    """Close and unlink every segment, even if one fails; re-raise the first error."""
    error = None
    for segment in segments:
        try:
            segment.close()
        except BufferError as exc:  # a view is still alive; unlink anyway
            error = error or exc
        try:
            segment.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            error = error or exc
    if error is not None:
        raise error


def _release_process_state(state):
    pool, state["pool"] = state["pool"], None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)
    segments = list(state["segments"])
    state["segments"].clear()
    _free_segments(segments)


def _default_start_method():
    methods = multiprocessing.get_all_start_methods()
    # Forking a process that runs threads can deadlock; forkserver and spawn do not.
    return "forkserver" if "forkserver" in methods else "spawn"


class ProcessAccel(AccelBackend):
    """A pool of processes over shared memory, for machines without a compiler.

    Matrices with at least ``min_macs`` multiply-adds go to shared memory
    and are split by rows over ``workers`` processes (default: the CPU
    count, at most 32) started with ``start_method`` (default
    ``forkserver``, else ``spawn``); the vector and the result travel
    through shared memory too. Smaller matrices run locally. Results equal
    the python backend's. :meth:`close` stops the pool and unlinks every
    segment (also done at exit).

    Like any :mod:`multiprocessing` code started with ``spawn`` or
    ``forkserver``, the main script must keep its work under
    ``if __name__ == "__main__":``; if the workers cannot start, the backend
    logs a warning and computes in the parent process.
    """

    name = "process"

    def __init__(self, *, workers=None, start_method=None, min_macs=PROCESS_MIN_MACS):
        self.workers = _thread_count(workers)
        self.min_macs = int(min_macs)
        self._context = multiprocessing.get_context(start_method or _default_start_method())
        self.start_method = self._context.get_start_method()
        self._pool = None
        self._broken = False
        self._segments = []
        self._x = None
        self._out = None
        self._lock = threading.Lock()
        self._closed = False
        # Frees the pool and the segments if the backend is collected unclosed.
        self._state = {"pool": None, "segments": self._segments}
        # weakref.finalize also runs at interpreter exit.
        self._finalizer = weakref.finalize(self, _release_process_state, self._state)

    @property
    def segment_names(self):
        """Names of the shared memory segments this backend owns now."""
        return [segment.name for segment in self._state["segments"]]

    def _new_segment(self, size):
        if self._closed:
            raise ValueError("the backend is closed")
        segment = shared_memory.SharedMemory(create=True, size=max(1, size))
        self._segments.append(segment)
        return segment

    def prepare_matrix(self, rows, cols, source):
        if self.workers < 2 or rows < 2 or rows * cols < self.min_macs:
            return _RowsMatrix(rows, cols, source)
        created = []

        def segment_factory(size):
            created.append(self._new_segment(size))
            return created[-1]

        with self._lock:
            try:
                return _SharedMatrix(rows, cols, source, segment_factory)
            except BaseException:
                for segment in created:
                    self._segments.remove(segment)
                _free_segments(created)
                raise

    def _io_segment(self, current, size):
        if current is not None and current.size >= size:
            return current
        if current is not None:
            # Workers keep the old segment mapped: restart them so it is freed.
            if self._pool is not None:
                self._pool.shutdown(wait=True)
                self._pool = self._state["pool"] = None
            self._segments.remove(current)
            _free_segments([current])
        return self._new_segment(size)

    def matvec(self, matrix, x, bias=None):
        if len(x) != matrix.cols:
            raise ValueError(f"the vector has {len(x)} values, expected {matrix.cols}")
        if isinstance(matrix, _RowsMatrix):
            result = [sum(map(_mul, row, x)) for row in matrix.row_list]
        else:
            result = self._shared_matvec(matrix, x)
        return result if bias is None else list(map(_add, result, bias))

    def _shared_matvec(self, matrix, x):
        rows, cols = matrix.rows, matrix.cols
        with self._lock:
            if self._closed:
                raise ValueError("the backend is closed")
            self._x = self._io_segment(self._x, cols * 8)
            self._out = self._io_segment(self._out, rows * 8)
            with self._x.buf[:cols * 8] as raw, raw.cast("d") as view:
                view[:] = array.array("d", x)
            if not self._broken:
                self._pool_rows(matrix, rows, cols)
            if self._broken:
                _shared_rows(matrix.segment, matrix.typecode, cols, self._x, self._out, 0, rows)
            with self._out.buf[:rows * 8] as raw, raw.cast("d") as view:
                return view.tolist()

    def _pool_rows(self, matrix, rows, cols):
        try:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(self.workers, mp_context=self._context)
                self._state["pool"] = self._pool
            futures = [self._pool.submit(_process_rows, matrix.name, matrix.typecode, cols,
                                         self._x.name, self._out.name, start, end)
                       for start, end in _row_ranges(rows, self.workers)]
            done = sum(future.result() for future in futures)
        except (BrokenProcessPool, OSError, RuntimeError) as exc:
            get_log().warning("accel.process_unavailable",
                              f"worker processes failed ({exc!r}); computing in this process")
            self._broken = True
            pool, self._pool = self._pool, None
            self._state["pool"] = None
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            return
        if done != rows:
            raise AccelError("the worker processes skipped rows")

    def describe(self):
        return {"name": self.name, "workers": self.workers, "start_method": self.start_method,
                "shared_segments": len(self._segments)}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pool = self._x = self._out = None
        self._finalizer()


# -- the compiled library --------------------------------------------------

_LIBRARIES = {}
_LIBRARY_LOCK = threading.Lock()


def _kernel_source():
    return resources.files(__package__).joinpath(KERNEL_SOURCE).read_bytes()


def _cache_path(path=None):
    if path is None:
        root = os.environ.get(ENV_CACHE)
        if root:
            path = os.path.join(root, "accel")
        else:
            path = os.path.join(os.path.expanduser("~"), ".cache", "wsbuilder", "accel")
    return os.path.abspath(os.fspath(path))


def _check_ancestors(path):
    """Refuse a cache under a directory another user could rename things in."""
    _check_tree(os.path.dirname(path))


def _check_tree(current):
    """Check ``current`` and every directory above it (see :func:`_check_ancestors`)."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    while True:
        info = os.stat(current)
        if uid is not None and info.st_uid not in (0, uid):
            raise AccelError(f"the kernel cache is under {current}, owned by another user")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise AccelError(f"the kernel cache is under {current}, writable by other users; "
                             f"set {ENV_CACHE} to a private directory")
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _make_private_dirs(path):
    """Create ``path`` and its missing parents with mode 0700 (``os.makedirs``
    would give the parents the umask's mode, often group-writable)."""
    missing = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass


def cache_directory(path=None):
    """The private directory for compiled kernels, created (mode 0700) if needed.

    Returns its real path. Raises :class:`AccelError` when it is a symlink,
    not a directory, owned by another user or writable by group or others,
    or when a directory above it is owned by another user (other than root)
    or writable by others without the sticky bit.
    """
    path = _cache_path(path)
    try:
        # Check the closest existing directory first: nothing is created under
        # a parent that would be refused.
        existing = path
        while not os.path.lexists(existing) and os.path.dirname(existing) != existing:
            existing = os.path.dirname(existing)
        _check_tree(os.path.realpath(existing if existing != path else os.path.dirname(path)))
        _make_private_dirs(path)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AccelError(f"the kernel cache {path} is not a plain directory")
        path = os.path.realpath(path)
        info = os.lstat(path)
        _check_ancestors(path)
    except OSError as exc:
        raise AccelError(f"cannot use the kernel cache {path}: {exc}") from exc
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise AccelError(f"the kernel cache {path} belongs to another user")
    if info.st_mode & 0o022:
        raise AccelError(f"the kernel cache {path} is writable by other users")
    return path


def find_compiler(compiler=None):
    """The C compiler command as a list, or ``None``.

    ``compiler`` (a string or list), else ``$CC`` (used as given, even if it
    does not exist), else the compiler Python was built with, ``cc``, ``gcc``
    or ``clang``.
    """
    if compiler is None:
        compiler = os.environ.get("CC") or None
    if compiler is not None:
        candidates = [compiler]
    else:
        candidates = [sysconfig.get_config_var("CC"), "cc", "gcc", "clang"]
    for candidate in candidates:
        if not candidate:
            continue
        args = shlex.split(candidate) if isinstance(candidate, str) else list(candidate)
        if not args:
            continue
        executable = shutil.which(args[0])
        if executable is not None:
            return [executable, *args[1:]]
    return None


def _library_key(source, command):
    digest = hashlib.sha256()
    try:
        info = os.stat(command[0])
        compiler_id = f"{info.st_size}:{info.st_mtime_ns}"
    except OSError:
        compiler_id = ""
    for part in (source, sysconfig.get_platform(), platform.machine(),
                 sys.implementation.cache_tag or "", "\0".join(command), compiler_id,
                 " ".join(CFLAGS), str(_ABI_VERSION)):
        data = part if isinstance(part, bytes) else str(part).encode("utf-8", "surrogateescape")
        digest.update(len(data).to_bytes(8, "little"))
        digest.update(data)
    return digest.hexdigest()


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_file(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        return False
    return not hasattr(os, "getuid") or info.st_uid == os.getuid()


def _verified(path, sidecar, key):
    """True when ``path`` is the library this module built for ``key``."""
    if not (_private_file(path) and _private_file(sidecar)):
        return False
    try:
        with open(sidecar, "rb") as handle:
            record = json.loads(handle.read(_MAX_SIDECAR_BYTES + 1))
        return (isinstance(record, dict) and record.get("key") == key
                and record.get("sha256") == _file_sha256(path))
    except (OSError, ValueError):
        return False


def _compile(command, source, directory, path, sidecar, key):
    """Build the library into ``path`` (called with the directory lock held)."""
    # Fixed names under the lock: the source path ends up in the binary.
    stem = os.path.join(directory, f".build-{key[:40]}")
    c_path, so_path, json_path = stem + ".c", stem + ".so", stem + ".json"
    try:
        with open(c_path, "wb") as stream:
            stream.write(source)
        args = [*command, *CFLAGS, "-o", so_path, c_path, "-lm"]
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=COMPILE_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AccelError(f"cannot run the C compiler {command[0]}: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[-2000:]
            raise AccelError(f"the C compiler failed ({result.returncode}): {detail}")
        if not os.path.isfile(so_path):
            raise AccelError("the C compiler did not write the library")
        os.chmod(so_path, 0o700)
        digest = _file_sha256(so_path)
        with open(json_path, "w", encoding="utf-8") as stream:
            json.dump({"key": key, "sha256": digest}, stream)
        os.chmod(json_path, 0o600)
        os.replace(so_path, path)
        os.replace(json_path, sidecar)
    finally:
        for temporary in (c_path, so_path, json_path):
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _open_library(path):
    try:
        library = ctypes.CDLL(path)
    except OSError as exc:
        raise AccelError(f"cannot load the native kernels {path}: {exc}") from exc
    library.wsb_abi_version.restype = ctypes.c_int
    library.wsb_abi_version.argtypes = []
    if library.wsb_abi_version() != _ABI_VERSION:
        raise AccelError(f"{path} is not the expected kernel version")
    library.wsb_init.restype = None
    library.wsb_init.argtypes = []
    library.wsb_init()
    pointer, int64 = ctypes.c_void_p, ctypes.c_int64
    library.wsb_matvec.restype = ctypes.c_int
    library.wsb_matvec.argtypes = [ctypes.c_int, pointer, pointer, pointer, pointer,
                                   int64, int64, int64, int64]
    library.wsb_attend.restype = ctypes.c_int
    library.wsb_attend.argtypes = [pointer, pointer, pointer, pointer, pointer, int64, int64,
                                   int64, int64, ctypes.c_double, ctypes.c_double]
    return library


def load_native_library(*, cache_dir=None, compiler=None):
    """Build (once) and load the C kernels; returns ``(library, path, compiled_now)``.

    Raises :class:`AccelError` when there is no compiler, the build fails,
    the cache directory is unsafe or the library does not load.
    """
    if os.name != "posix" or fcntl is None:
        raise AccelError("the native backend needs a POSIX system with a C compiler")
    command = find_compiler(compiler)
    if command is None:
        raise AccelError("no C compiler found; set CC or install cc, gcc or clang")
    source = _kernel_source()
    key = _library_key(source, command)
    directory = cache_directory(cache_dir)
    with _LIBRARY_LOCK:
        loaded = _LIBRARIES.get((directory, key))
        if loaded is not None:
            return loaded[0], loaded[1], False
        path = os.path.join(directory, f"wsbaccel-{key[:40]}.so")
        sidecar = path + ".json"
        compiled = False
        # The file lock keeps other processes from building or replacing the
        # library between the check and the load.
        lock = os.open(os.path.join(directory, ".lock"),
                       os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not _verified(path, sidecar, key):
                _compile(command, source, directory, path, sidecar, key)
                compiled = True
                if not _verified(path, sidecar, key):
                    raise AccelError(f"the library built in {directory} does not verify")
            library = _open_library(path)
        finally:
            os.close(lock)  # releases the lock
        _LIBRARIES[(directory, key)] = (library, path)
        return library, path, compiled


# -- selection -------------------------------------------------------------


def _requested(name):
    if name is None:
        name = os.environ.get(ENV_ACCEL) or "auto"
    if not isinstance(name, str) or name.strip().lower() not in ACCEL_BACKENDS:
        raise ValueError(f"accel must be one of {', '.join(ACCEL_BACKENDS)}, not {name!r}")
    return name.strip().lower()


def create_backend(name=None, *, threads=None, cache_dir=None, compiler=None,
                   start_method=None):
    """A backend by name: ``auto`` (default, or ``$WSBUILDER_ACCEL``), ``native``,
    ``process`` or ``python``.

    ``auto`` falls back from ``native`` to ``python`` and logs a warning with
    :func:`wsbuilder.logs.get_log`; ``process`` is used only when asked
    for, because its workers re-import the main script (see
    :class:`ProcessAccel`). An explicit ``native`` that cannot build raises
    :class:`AccelError`. ``threads`` sets the native threads or the worker
    processes.
    """
    name = _requested(name)
    if name == "python":
        return PythonAccel()
    if name == "process":
        return ProcessAccel(workers=threads, start_method=start_method)
    if name == "native":
        return NativeAccel(threads=threads, cache_dir=cache_dir, compiler=compiler)
    try:
        return NativeAccel(threads=threads, cache_dir=cache_dir, compiler=compiler)
    except AccelError as exc:
        get_log().warning("accel.fallback", f"native kernels unavailable ({exc}); using the "
                          "python backend (WSBUILDER_ACCEL=process runs worker processes)")
    return PythonAccel()


def describe(cache_dir=None, compiler=None):
    """What :func:`create_backend` would work with, without compiling or creating files."""
    command = find_compiler(compiler)
    directory = _cache_path(cache_dir)
    cached = None
    if command is not None:
        path = os.path.join(directory,
                            f"wsbaccel-{_library_key(_kernel_source(), command)[:40]}.so")
        cached = path if os.path.exists(path) else None
    return {"requested": os.environ.get(ENV_ACCEL) or "auto", "backends": ACCEL_BACKENDS,
            "cpu_count": os.cpu_count() or 1, "compiler": command, "cache_dir": directory,
            "cached_library": cached}

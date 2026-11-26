import array
import gc
import json
import math
import operator
import os
import random
import stat
import struct
import sys
import tempfile
import threading
import tomllib
import unittest
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import shared_memory
from pathlib import Path
from unittest import mock

from test_graph import (
    QWEN3,
    TOKENS,
    graph_logits,
    random_weights,
    write_gguf,
    write_hf_dir,
)
from wsbuilder import GraphModel, ModelReader, ModelWriter, accel, convert_to_graph

ROOT = Path(__file__).resolve().parents[1]
HAS_COMPILER = os.name == "posix" and accel.find_compiler() is not None
MISSING_CC = "/nonexistent/wsbuilder-test-cc"
_saved_cache = None
_cache_dir = None


def setUpModule():
    # Compiled kernels go to a private temporary cache, never to ~/.cache.
    global _saved_cache, _cache_dir
    _cache_dir = tempfile.TemporaryDirectory()
    _saved_cache = os.environ.get(accel.ENV_CACHE)
    os.environ[accel.ENV_CACHE] = _cache_dir.name


def tearDownModule():
    if _saved_cache is None:
        os.environ.pop(accel.ENV_CACHE, None)
    else:
        os.environ[accel.ENV_CACHE] = _saved_cache
    accel._LIBRARIES.clear()
    _cache_dir.cleanup()


def random_rows(rows, cols, seed, typecode="f"):
    rng = random.Random(seed)
    return [array.array(typecode, [rng.uniform(-1.0, 1.0) for _ in range(cols)])
            for _ in range(rows)]


def random_vector(size, seed):
    rng = random.Random(seed)
    return [rng.uniform(-2.0, 2.0) for _ in range(size)]


def reference_attention(q, keys, values, heads, kv_heads, head_dim, start, scale, softcap):
    """The attention of graph.py before the backends, on lists of array rows."""
    group = heads // kv_heads
    result = []
    for head in range(heads):
        query = q[head * head_dim:(head + 1) * head_dim]
        kv = head // group
        scores = [sum(map(operator.mul, query, key)) * scale for key in keys[kv][start:]]
        if softcap is not None:
            scores = [math.tanh(score / softcap) * softcap for score in scores]
        peak = max(scores)
        exps = [math.exp(score - peak) for score in scores]
        total = sum(exps)
        weights = [value / total for value in exps]
        result += [sum(map(operator.mul, weights, column))
                   for column in zip(*values[kv][start:])]
    return result


def kv_cache(kv_heads, head_dim, length, seed):
    rng = random.Random(seed)
    rows = [[[rng.uniform(-1, 1) for _ in range(head_dim)] for _ in range(length)]
            for _ in range(kv_heads)]
    flat = [array.array("d", [value for row in head for value in row]) for head in rows]
    return rows, flat


class _Close(unittest.TestCase):
    def assertVectorClose(self, actual, expected, rows=None, x=None, relative=1e-12):
        """Equal up to the rounding of a different summation order."""
        self.assertEqual(len(actual), len(expected))
        for index, (a, b) in enumerate(zip(actual, expected)):
            scale = abs(b)
            if rows is not None:
                scale = max(scale, sum(abs(w * v) for w, v in zip(rows[index], x)))
            self.assertLessEqual(abs(a - b), relative * scale + 1e-300, f"index {index}")


# -- the python reference -----------------------------------------------------


class TestPythonBackend(_Close):
    def test_matvec_is_the_reference_loop(self):
        backend = accel.PythonAccel()
        rows = random_rows(9, 13, 1)
        x = random_vector(13, 2)
        bias = random_vector(9, 3)
        matrix = backend.prepare_matrix(9, 13, rows)
        expected = [sum(map(operator.mul, row, x)) for row in rows]
        self.assertEqual(backend.matvec(matrix, x), expected)
        self.assertEqual(backend.matvec(matrix, x, backend.prepare_vector(bias)),
                         [value + extra for value, extra in zip(expected, bias)])
        self.assertEqual(matrix.row(4), list(rows[4]))

    def test_attention_is_the_reference(self):
        backend = accel.PythonAccel()
        q = random_vector(4 * 8, 5)
        rows, flat = kv_cache(2, 8, 6, 6)
        for start, softcap in ((0, None), (3, None), (2, 3.0)):
            key_rows = [[array.array("d", row) for row in head] for head in rows]
            expected = reference_attention(q, key_rows, key_rows, 4, 2, 8, start, 0.3, softcap)
            self.assertEqual(backend.attend(q, flat, flat, 4, 2, 8, start, 0.3, softcap),
                             expected)

    def test_bad_shapes(self):
        backend = accel.PythonAccel()
        with self.assertRaises(ValueError):
            backend.prepare_matrix(2, 3, [[1.0, 2.0, 3.0]])
        with self.assertRaises(ValueError):
            backend.prepare_matrix(1, 3, [[1.0, 2.0]])


# -- every backend against the reference -------------------------------------


class _BackendCases:
    """Shared checks (a mixin); subclasses set ``make_backend``."""

    exact = False

    def make_backend(self):
        raise NotImplementedError

    def setUp(self):
        self.backend = self.make_backend()
        self.addCleanup(self.backend.close)
        self.reference = accel.PythonAccel()

    def check(self, rows_count, cols, seed, *, bias=False, typecode="f"):
        rows = random_rows(rows_count, cols, seed, typecode)
        x = random_vector(cols, seed + 1)
        extra = random_vector(rows_count, seed + 2) if bias else None
        expected = self.reference.matvec(self.reference.prepare_matrix(rows_count, cols, rows), x,
                                         extra)
        matrix = self.backend.prepare_matrix(rows_count, cols, rows)
        actual = self.backend.matvec(matrix, x, None if extra is None
                                     else self.backend.prepare_vector(extra))
        if self.exact:
            self.assertEqual(actual, expected)
        else:
            self.assertVectorClose(actual, expected, rows, x)
        self.assertEqual(matrix.row(rows_count - 1), list(rows[-1]))
        return matrix

    def test_matvec_shapes(self):
        for seed, (rows_count, cols) in enumerate(((1, 1), (1, 9), (9, 1), (13, 37), (37, 64),
                                                   (101, 33))):
            with self.subTest(shape=(rows_count, cols)):
                self.check(rows_count, cols, 10 * seed)

    def test_bias_and_float64_rows(self):
        self.check(23, 17, 70, bias=True)
        self.check(23, 17, 80, bias=True, typecode="d")

    def test_wrong_vector_size(self):
        matrix = self.backend.prepare_matrix(3, 4, random_rows(3, 4, 1))
        with self.assertRaises(ValueError):
            self.backend.matvec(matrix, [1.0, 2.0])

    def test_attention(self):
        q = random_vector(6 * 16, 9)
        _rows, keys = kv_cache(3, 16, 11, 10)
        _rows, values = kv_cache(3, 16, 11, 11)
        for start, softcap in ((0, None), (7, None), (10, None), (3, 2.5)):
            expected = self.reference.attend(q, keys, values, 6, 3, 16, start, 0.25, softcap)
            actual = self.backend.attend(q, keys, values, 6, 3, 16, start, 0.25, softcap)
            if self.exact:
                self.assertEqual(actual, expected)
            else:
                self.assertVectorClose(actual, expected, relative=1e-12)

    def test_attention_shapes_are_checked(self):
        q = random_vector(4 * 8, 1)
        _rows, keys = kv_cache(2, 8, 5, 2)
        bad_calls = [
            (q[:-1], keys, keys, 4, 2, 8, 0),      # short query
            (q, keys, keys, 4, 3, 8, 0),           # heads not a multiple of kv_heads
            (q, keys, keys, 4, 0, 8, 0),           # no kv heads
            (q, keys[:1], keys[:1], 4, 2, 8, 0),   # missing KV head
            (q, keys, keys, 4, 2, 8, -1),          # negative start
            (q, keys, keys, 4, 2, 8, 5),           # nothing to attend
            (q, [keys[0], keys[1][:-1]], keys, 4, 2, 8, 0),  # ragged cache
        ]
        for args in bad_calls:
            with self.subTest(args=args[3:]), self.assertRaises(ValueError):
                self.backend.attend(*args, 0.5, None)

    def test_close_while_another_thread_multiplies(self):
        rows = random_rows(64, 64, 3)
        matrix = self.backend.prepare_matrix(64, 64, rows)
        x = random_vector(64, 4)
        started = threading.Event()
        outcome = []

        def work():
            try:
                while True:
                    self.backend.matvec(matrix, x)
                    started.set()
            except ValueError as exc:  # closed: a clean error, no hang
                outcome.append(exc)
            except BaseException as exc:  # noqa: BLE001 - reported below
                outcome.append(exc)
            finally:
                started.set()

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(started.wait(30))
        self.backend.close()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], ValueError)


@unittest.skipUnless(HAS_COMPILER, "no C compiler")
class TestNativeBackend(_BackendCases, _Close):
    def make_backend(self):
        # Five threads and one range per row: the ranges do not divide evenly.
        return accel.NativeAccel(threads=5, chunk_macs=1)

    def test_threaded_matvec_after_close_raises(self):
        matrix = self.backend.prepare_matrix(8, 8, random_rows(8, 8, 1))
        self.backend.close()
        with self.assertRaises(ValueError):
            self.backend.matvec(matrix, random_vector(8, 2))

    def test_single_thread(self):
        self.backend.close()
        self.backend = accel.NativeAccel(threads=1)
        self.check(37, 64, 3, bias=True)
        self.assertEqual(self.backend.describe()["threads"], 1)

    def test_tensor_dtypes_keep_their_bytes(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        rng = random.Random(4)
        rows_count, cols = 7, 64
        writer = ModelWriter(architecture="test")
        values = [rng.uniform(-1, 1) for _ in range(rows_count * cols)]
        writer.add_tensor("f32", values, shape=[rows_count, cols], dtype="F32",
                          encoding="base64")
        writer.add_tensor("f16", values, shape=[rows_count, cols], dtype="F16",
                          encoding="base64")
        writer.add_tensor("bf16", values, shape=[rows_count, cols], dtype="BF16",
                          encoding="base64")
        writer.add_tensor("f64", values, shape=[rows_count, cols], dtype="F64",
                          encoding="base64")
        writer.add_tensor("json", values, shape=[rows_count, cols], dtype="F32")
        blocks = rows_count * cols // 32
        q8 = b"".join(struct.pack("<e", rng.uniform(0.001, 0.05))
                      + bytes(rng.randrange(256) for _ in range(32)) for _ in range(blocks))
        q4 = b"".join(struct.pack("<e", rng.uniform(0.001, 0.05))
                      + bytes(rng.randrange(256) for _ in range(16)) for _ in range(blocks))
        writer.add_tensor_bytes("q8", q8, shape=[rows_count, cols], dtype="Q8_0")
        writer.add_tensor_bytes("q4", q4, shape=[rows_count, cols], dtype="Q4_0")
        path = writer.write(os.path.join(directory.name, "w.ndjson"))
        x = random_vector(cols, 5)
        with ModelReader(path) as reader:
            for name in ("f32", "f16", "bf16", "f64", "json", "q8", "q4"):
                with self.subTest(dtype=name):
                    tensor = reader.tensor(name)
                    rows = list(tensor.row_arrays())
                    matrix = self.backend.prepare_matrix(rows_count, cols, tensor)
                    if name in ("q8", "q4", "f16", "bf16"):
                        self.assertEqual(len(matrix.buffer), len(tensor.raw()))
                    expected = self.reference.matvec(
                        self.reference.prepare_matrix(rows_count, cols, rows), x)
                    self.assertVectorClose(self.backend.matvec(matrix, x), expected, rows, x)
                    self.assertEqual(matrix.row(3), rows[3].tolist())


class TestProcessBackend(_BackendCases, _Close):
    exact = True  # the workers run the reference loop on the same values

    def make_backend(self):
        return accel.ProcessAccel(workers=3, min_macs=0)

    def test_small_matrices_stay_local(self):
        backend = accel.ProcessAccel(workers=3)
        self.addCleanup(backend.close)
        backend.prepare_matrix(4, 4, random_rows(4, 4, 1))
        self.assertEqual(backend.segment_names, [])

    def test_close_unlinks_every_segment(self):
        self.check(31, 40, 1)
        self.check(64, 50, 2)  # larger: the vector and result segments grow
        names = self.backend.segment_names
        self.assertGreaterEqual(len(names), 4)
        self.backend.close()
        self.backend.close()
        self.assertEqual(self.backend.segment_names, [])
        for name in names:
            with self.assertRaises(FileNotFoundError):
                shared_memory.SharedMemory(name=name)
        with self.assertRaises(ValueError):
            self.backend.prepare_matrix(8, 8, random_rows(8, 8, 1))

    def test_failed_prepare_unlinks_its_segment(self):
        rows = random_rows(9, 21, 1) + [array.array("f", [1.0])]  # ragged last row
        created = []
        real = accel.shared_memory.SharedMemory

        def tracking(*args, **kwargs):
            created.append(real(*args, **kwargs))
            return created[-1]

        with mock.patch.object(accel.shared_memory, "SharedMemory", side_effect=tracking):
            with self.assertRaises(ValueError):
                self.backend.prepare_matrix(10, 21, rows)
        self.assertEqual(len(created), 1)
        self.assertEqual(self.backend.segment_names, [])
        with self.assertRaises(FileNotFoundError):
            real(name=created[0].name)

    def test_collected_backend_frees_its_segments(self):
        backend = accel.ProcessAccel(workers=2, min_macs=0)
        matrix = backend.prepare_matrix(9, 21, random_rows(9, 21, 2))
        backend.matvec(matrix, random_vector(21, 3))
        names = backend.segment_names
        del backend, matrix
        gc.collect()
        for name in names:
            with self.assertRaises(FileNotFoundError):
                shared_memory.SharedMemory(name=name)

    def test_spawn_start_method(self):
        backend = accel.ProcessAccel(workers=2, start_method="spawn", min_macs=0)
        self.addCleanup(backend.close)
        self.assertEqual(backend.start_method, "spawn")
        rows = random_rows(9, 21, 6)
        x = random_vector(21, 7)
        expected = [sum(map(operator.mul, row, x)) for row in rows]
        self.assertEqual(backend.matvec(backend.prepare_matrix(9, 21, rows), x), expected)

    def test_broken_pool_computes_locally(self):
        rows = random_rows(9, 21, 8)
        x = random_vector(21, 9)
        expected = [sum(map(operator.mul, row, x)) for row in rows]
        matrix = self.backend.prepare_matrix(9, 21, rows)
        log = mock.Mock()
        with mock.patch.object(accel, "ProcessPoolExecutor",
                               side_effect=BrokenProcessPool("no workers")), \
                mock.patch.object(accel, "get_log", return_value=log):
            self.assertEqual(self.backend.matvec(matrix, x), expected)
            self.assertEqual(self.backend.matvec(matrix, x), expected)
        log.warning.assert_called_once()
        self.assertEqual(log.warning.call_args.args[0], "accel.process_unavailable")


# -- graph models on each backend --------------------------------------------


class TestGraphModels(_Close):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        weights = random_weights(QWEN3)
        hf = write_hf_dir(os.path.join(self.directory, "hf"), QWEN3, weights)
        self.f32 = convert_to_graph(hf, os.path.join(self.directory, "f32"))
        self.f16 = convert_to_graph(hf, os.path.join(self.directory, "f16"), dtype="F16")
        gguf = write_gguf(os.path.join(self.directory, "m.gguf"), QWEN3, weights,
                          q8_tensor="blk.0.ffn_down.weight")
        self.q8 = convert_to_graph(gguf, os.path.join(self.directory, "q8"))

    def load(self, path, accel_option):
        model = GraphModel.load(path, accel=accel_option)
        self.addCleanup(model.close)
        return model

    def compare(self, accel_option, *, exact=False):
        for path in (self.f32, self.f16, self.q8):
            with self.subTest(model=os.path.basename(path)):
                reference = self.load(path, "python")
                model = self.load(path, accel_option)
                expected = graph_logits(reference, TOKENS)
                actual = graph_logits(model, TOKENS)
                for row, ref in zip(actual, expected):
                    if exact:
                        self.assertEqual(row, ref)
                    else:
                        # Relative to the largest logit: summation order only.
                        scale = max(1.0, max(abs(value) for value in ref))
                        worst = max(abs(a - b) for a, b in zip(row, ref))
                        self.assertLessEqual(worst, 1e-9 * scale)
                prompt = TOKENS[:4]
                self.assertEqual(model.generate(prompt, max_tokens=5, temperature=0),
                                 reference.generate(prompt, max_tokens=5, temperature=0))
                self.assertEqual(self._greedy(model, prompt), self._greedy(reference, prompt))

    @staticmethod
    def _greedy(model, prompt, count=6):
        cache = model.new_cache()
        logits = None
        for position, token in enumerate(prompt):
            logits = model.forward(token, position, cache)
        out = []
        for _ in range(count):
            token = max(range(len(logits)), key=logits.__getitem__)
            out.append(token)
            logits = model.forward(token, cache.length, cache)
        return out

    def test_python_backend_name(self):
        model = self.load(self.f32, "python")
        self.assertEqual(model.accel_name, "python")

    @unittest.skipUnless(HAS_COMPILER, "no C compiler")
    def test_native_logits_and_greedy_generation(self):
        backend = accel.NativeAccel(threads=3, chunk_macs=1)
        self.addCleanup(backend.close)
        self.compare(backend)
        self.assertEqual(self.load(self.f32, "native").accel_name, "native")

    def test_process_logits_are_exact(self):
        backend = accel.ProcessAccel(workers=2, min_macs=0)
        self.addCleanup(backend.close)
        self.compare(backend, exact=True)
        self.assertTrue(backend.segment_names)

    def test_close_frees_an_owned_backend_only(self):
        model = GraphModel.load(self.f32, accel="process")
        backend = model.accel
        model.close()
        model.close()
        self.assertTrue(backend._closed)
        with self.assertRaises(ValueError):
            model.forward(1, 0, model.new_cache())
        shared = accel.PythonAccel()
        shared.close = mock.Mock()
        GraphModel.load(self.f32, accel=shared).close()
        shared.close.assert_not_called()

    def test_environment_selects_the_backend(self):
        with mock.patch.dict(os.environ, {accel.ENV_ACCEL: "python"}):
            self.assertEqual(self.load(self.f32, None).accel_name, "python")
        with self.assertRaises(ValueError):
            GraphModel.load(self.f32, accel="gpu")


# -- selection and the compiled library ---------------------------------------


class TestSelection(unittest.TestCase):
    def test_names(self):
        self.assertIsInstance(accel.create_backend("python"), accel.PythonAccel)
        with mock.patch.dict(os.environ, {accel.ENV_ACCEL: " Python "}):
            self.assertIsInstance(accel.create_backend(), accel.PythonAccel)
        for bad in ("gpu", "", 3):
            with self.assertRaises(ValueError):
                accel.create_backend(bad)

    def test_auto_without_compiler_falls_back_to_python_with_a_warning(self):
        # Never to process: its workers would re-import the user's main script.
        log = mock.Mock()
        with mock.patch.dict(os.environ, {"CC": MISSING_CC}), \
                mock.patch.object(accel, "get_log", return_value=log):
            self.assertIsInstance(accel.create_backend("auto", threads=4), accel.PythonAccel)
        log.warning.assert_called_once()
        self.assertEqual(log.warning.call_args.args[0], "accel.fallback")
        self.assertIn("no C compiler", log.warning.call_args.args[1])
        process = accel.create_backend("process", threads=2)
        self.addCleanup(process.close)
        self.assertIsInstance(process, accel.ProcessAccel)

    def test_explicit_native_without_compiler_raises(self):
        with mock.patch.dict(os.environ, {"CC": MISSING_CC}):
            self.assertIsNone(accel.find_compiler())
            with self.assertRaises(accel.AccelError):
                accel.create_backend("native")

    def test_describe_creates_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "cache")
            info = accel.describe(cache_dir=target)
            self.assertEqual(info["cache_dir"], target)
            self.assertIn("native", info["backends"])
            self.assertFalse(os.path.exists(target))


@unittest.skipUnless(HAS_COMPILER, "no C compiler")
class TestNativeCache(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.cache = os.path.join(self.root, "accel")
        self.addCleanup(accel._LIBRARIES.clear)

    def load(self):
        accel._LIBRARIES.clear()  # what a new process would do
        return accel.load_native_library(cache_dir=self.cache)

    @staticmethod
    def replace_file(path, data):
        # A new file, as another program would write it: rewriting the loaded
        # library in place would corrupt the mapping of this process.
        with open(path + ".new", "wb") as handle:
            handle.write(data)
        os.replace(path + ".new", path)

    def test_built_once_then_reused(self):
        self.cache = os.path.join(self.root, "new", "parents", "accel")
        old_umask = os.umask(0o002)  # group-writable defaults must not leak in
        try:
            _library, path, compiled = self.load()
        finally:
            os.umask(old_umask)
        self.assertTrue(compiled)
        for directory in (self.cache, os.path.dirname(self.cache),
                          os.path.join(self.root, "new")):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        self.assertTrue(os.path.basename(path).startswith("wsbaccel-"))
        with mock.patch.object(accel.subprocess, "run") as run:
            _library, again, compiled = self.load()
        run.assert_not_called()
        self.assertFalse(compiled)
        self.assertEqual(again, path)
        self.assertEqual([name for name in os.listdir(self.cache) if name.startswith(".build-")],
                         [])

    def test_other_source_gets_another_library(self):
        _library, path, _compiled = self.load()
        source = accel._kernel_source() + b"\n/* another version */\n"
        with mock.patch.object(accel, "_kernel_source", return_value=source):
            _library, other, compiled = self.load()
        self.assertTrue(compiled)
        self.assertNotEqual(other, path)

    def test_foreign_file_with_the_expected_name_is_rebuilt(self):
        _library, path, _compiled = self.load()
        for sidecar in (None, {"key": "x", "sha256": "0" * 64}):
            with self.subTest(sidecar=sidecar):
                self.replace_file(path, b"not a library")
                if sidecar is None:
                    os.unlink(path + ".json")
                else:
                    self.replace_file(path + ".json", json.dumps(sidecar).encode())
                _library, rebuilt, compiled = self.load()
                self.assertTrue(compiled)
                self.assertEqual(rebuilt, path)
                with open(rebuilt, "rb") as handle:
                    self.assertNotEqual(handle.read(), b"not a library")

    def test_tampered_library_with_its_old_sidecar_is_rebuilt(self):
        _library, path, _compiled = self.load()
        with open(path, "rb") as handle:
            data = handle.read()
        self.replace_file(path, data + b"\0")
        self.assertTrue(self.load()[2])

    def test_unsafe_directories_are_refused(self):
        os.makedirs(self.cache, mode=0o700)
        os.chmod(self.cache, 0o777)
        self.addCleanup(os.chmod, self.cache, 0o700)
        with self.assertRaises(accel.AccelError):
            self.load()
        link = os.path.join(self.root, "link")
        os.chmod(self.cache, 0o700)
        os.symlink(self.cache, link)
        with self.assertRaises(accel.AccelError):
            accel.load_native_library(cache_dir=link)

    def test_parents_writable_by_others_are_refused(self):
        shared = os.path.join(self.root, "shared")
        os.mkdir(shared)
        os.chmod(shared, 0o777)
        self.addCleanup(os.chmod, shared, 0o700)
        with self.assertRaises(accel.AccelError) as caught:
            accel.cache_directory(os.path.join(shared, "accel", "deeper"))
        self.assertIn("writable by other users", str(caught.exception))
        self.assertEqual(os.listdir(shared), [])  # refused before creating anything
        os.chmod(shared, 0o1777)  # sticky, like /tmp: others cannot rename our entries
        self.assertEqual(accel.cache_directory(os.path.join(shared, "accel")),
                         os.path.realpath(os.path.join(shared, "accel")))

    def test_compiler_errors_are_reported(self):
        failing = [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
        accel._LIBRARIES.clear()
        with self.assertRaises(accel.AccelError) as caught:
            accel.load_native_library(cache_dir=self.cache, compiler=failing)
        self.assertIn("boom", str(caught.exception))
        self.assertEqual([name for name in os.listdir(self.cache) if name.startswith(".build-")],
                         [])


# -- packaging -----------------------------------------------------------------


class TestPackaging(unittest.TestCase):
    def test_kernel_source_ships_as_data_and_the_wheel_stays_pure(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            config = tomllib.load(handle)
        setuptools = config["tool"]["setuptools"]
        self.assertNotIn("ext-modules", setuptools)
        self.assertNotIn("ext_modules", setuptools)
        self.assertIn(accel.KERNEL_SOURCE, setuptools["package-data"]["wsbuilder"])
        self.assertNotIn("ext_modules", (ROOT / "pyproject.toml").read_text())
        self.assertFalse((ROOT / "setup.py").exists())
        self.assertIn(f"include src/wsbuilder/{accel.KERNEL_SOURCE}",
                      (ROOT / "MANIFEST.in").read_text())
        self.assertTrue(accel._kernel_source().startswith(b"/*"))


if __name__ == "__main__":
    unittest.main()

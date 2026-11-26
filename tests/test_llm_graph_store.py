"""Graph models (model.json + weights.ndjson + tokenizer.json) in ModelStore."""

import hashlib
import http.server
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from test_graph import (
    QWEN3,
    _f32,
    _flat,
    _shape,
    random_weights,
    safetensors_bytes,
    write_gguf,
    write_hf_dir,
)
from wsbuilder import (
    App,
    DownloadError,
    GraphBackend,
    GraphFormatError,
    GraphModel,
    HTTPServer,
    ModelStore,
    convert_to_graph,
)

_saved_env = {}
_env_dir = None


def setUpModule():
    # Models and compiled kernels go to private directories, never to ~/.cache.
    global _env_dir
    _env_dir = tempfile.TemporaryDirectory()
    for key, sub in (("WSBUILDER_MODELS", "models"), ("WSBUILDER_CACHE", "cache")):
        _saved_env[key] = os.environ.get(key)
        os.environ[key] = os.path.join(_env_dir.name, sub)


def tearDownModule():
    for key, value in _saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _env_dir.cleanup()


class _StoreCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.store = ModelStore(self.path("models"))
        self.addCleanup(self.store.close)

    def path(self, *parts):
        return os.path.join(self.directory, *parts)

    def hf_dir(self, name="hf", cfg=QWEN3):
        return write_hf_dir(self.path(name), cfg, random_weights(cfg))

    def graph_dir(self, name="graph"):
        convert_to_graph(self.hf_dir(f"{name}-hf"), self.path(name), name=name)
        return self.path(name)

    def assertStoreClean(self):
        """No model, and nothing left in the staging area."""
        self.assertEqual(self.store.list(), [])
        self.assertIn(sorted(os.listdir(self.store.root)), ([], [".staging"]))
        staging = os.path.join(self.store.root, ".staging")
        self.assertEqual(os.listdir(staging) if os.path.isdir(staging) else [], [])

    def rewrite_spec(self, directory, **changes):
        spec_path = os.path.join(directory, "model.json")
        with open(spec_path, encoding="utf-8") as handle:
            spec = json.load(handle)
        spec.update(changes)
        with open(spec_path, "w", encoding="utf-8") as handle:
            json.dump(spec, handle)


class TestImportGraph(_StoreCase):
    def test_import_from_model_json_and_from_directory(self):
        source = self.graph_dir()
        by_json = self.store.import_graph(os.path.join(source, "model.json"))
        by_dir = self.store.import_graph(source, name="Other name", model_id="other")
        self.assertEqual((by_json.id, by_json.format, by_json.filename), ("graph", "graph",
                                                                          "model.json"))
        self.assertEqual((by_dir.id, by_dir.name), ("other", "Other name"))
        files = by_json.summary["files"]
        self.assertEqual(sorted(files), ["model.json", "tokenizer.json", "weights.ndjson"])
        for name, info in files.items():
            with open(os.path.join(source, name), "rb") as handle:
                data = handle.read()
            self.assertEqual(info, {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(by_json.size, sum(info["size"] for info in files.values()))
        self.assertEqual(by_json.sha256, files["model.json"]["sha256"])
        summary = by_json.summary
        self.assertEqual((summary["architecture"], summary["chat_template"]), ("qwen3", "qwen3"))
        self.assertEqual(summary["config"]["num_layers"], QWEN3["layers"])
        self.assertGreater(summary["parameters"], 0)
        self.assertEqual(list(summary["dtypes"]), ["F32"])
        self.assertTrue(by_json.source.startswith("file:"))
        self.assertTrue(self.store.verify(by_json.id))

    def test_import_file_delegates_graphs(self):
        source = self.graph_dir()
        self.assertEqual(self.store.import_file(source).format, "graph")
        self.assertEqual(self.store.import_file(os.path.join(source, "model.json")).format, "graph")

    def test_only_referenced_files_are_copied(self):
        source = self.graph_dir()
        for extra in ("notes.txt", "model.safetensors"):
            with open(os.path.join(source, extra), "wb") as handle:
                handle.write(b"not part of the model")
        record = self.store.import_graph(source)
        self.assertEqual(sorted(os.listdir(record.directory)),
                         ["manifest.json", "model.json", "tokenizer.json", "weights.ndjson"])

    def test_references_in_sub_directories_keep_their_names(self):
        source = self.graph_dir()
        os.makedirs(os.path.join(source, "parts"))
        os.replace(os.path.join(source, "weights.ndjson"),
                   os.path.join(source, "parts", "weights.ndjson"))
        self.rewrite_spec(source, weights="parts/weights.ndjson")
        record = self.store.import_graph(source)
        self.assertIn("parts/weights.ndjson", record.summary["files"])
        self.assertTrue(self.store.verify(record.id))
        with self.store.open(record.id) as reader:
            self.assertGreater(len(reader.names()), 0)

    def test_escaping_references_are_refused(self):
        source = self.graph_dir()
        shutil.copyfile(os.path.join(source, "weights.ndjson"), self.path("outside.ndjson"))
        for weights in ("../outside.ndjson", self.path("outside.ndjson")):
            with self.subTest(weights=weights):
                self.rewrite_spec(source, weights=weights)
                with self.assertRaises(ValueError):
                    self.store.import_graph(source)
        self.assertStoreClean()

    def test_a_symlink_that_escapes_is_refused(self):
        source = self.graph_dir()
        weights = os.path.join(source, "weights.ndjson")
        os.replace(weights, self.path("outside.ndjson"))
        os.symlink(self.path("outside.ndjson"), weights)
        with self.assertRaises(ValueError):
            self.store.import_graph(source)
        self.assertStoreClean()

    def test_a_symlink_inside_the_directory_is_copied(self):
        source = self.graph_dir()
        os.replace(os.path.join(source, "weights.ndjson"), os.path.join(source, "real.ndjson"))
        os.symlink("real.ndjson", os.path.join(source, "weights.ndjson"))
        record = self.store.import_graph(source)
        stored = os.path.join(record.directory, "weights.ndjson")
        self.assertFalse(os.path.islink(stored))
        self.assertTrue(self.store.verify(record.id))

    def test_duplicate_and_reserved_names_are_refused(self):
        source = self.graph_dir()
        for changes in ({"tokenizer": {"type": "bpe", "file": "weights.ndjson"}},
                        {"weights": "manifest.json"}, {"weights": ".hidden"}):
            with self.subTest(changes=changes):
                self.rewrite_spec(source, **changes)
                with self.assertRaises(ValueError):
                    self.store.import_graph(source)
                self.rewrite_spec(source, weights="weights.ndjson",
                                  tokenizer={"type": "bpe", "file": "tokenizer.json"})
        self.assertStoreClean()

    def test_invalid_json_and_corrupt_weights_are_refused(self):
        source = self.graph_dir()
        with open(os.path.join(source, "weights.ndjson"), "r+b") as handle:
            handle.seek(10)
            handle.write(b"\x00garbage\x00")
        with self.assertRaises(ValueError):
            self.store.import_graph(source)
        with open(os.path.join(source, "model.json"), "w") as handle:
            handle.write("{not json")
        with self.assertRaises(GraphFormatError):
            self.store.import_graph(source)
        with self.assertRaises(FileNotFoundError):
            self.store.import_graph(self.path("missing"))
        self.assertStoreClean()

    def test_missing_graph_tensors_and_bad_tokenizers_are_refused(self):
        source = self.graph_dir()
        with open(os.path.join(source, "model.json"), encoding="utf-8") as handle:
            spec = json.load(handle)
        spec["graph"]["embed"]["weight"] = "no.such.tensor"
        self.rewrite_spec(source, graph=spec["graph"])
        with self.assertRaisesRegex(ValueError, "no.such.tensor"):
            self.store.import_graph(source)
        source = self.graph_dir("second")
        with open(os.path.join(source, "tokenizer.json"), "w") as handle:
            json.dump({"model": {"type": "Unigram"}}, handle)
        with self.assertRaisesRegex(ValueError, "tokenizer"):
            self.store.import_graph(source)
        self.assertStoreClean()

    def test_absurd_block_counts_are_refused_before_expanding(self):
        source = self.graph_dir()
        with open(os.path.join(source, "model.json"), encoding="utf-8") as handle:
            graph = json.load(handle)["graph"]
        for count, ops in ((10**7, graph["blocks"]["ops"]), (10**12, []), (-1, []), ("2", [])):
            with self.subTest(count=count, ops=len(ops)):
                self.rewrite_spec(source, graph=dict(graph, blocks={"count": count, "ops": ops}))
                with self.assertRaisesRegex(ValueError, "count"):
                    self.store.import_graph(source)
        self.assertStoreClean()

    def test_a_failure_half_way_leaves_nothing(self):
        source = self.graph_dir()
        real_copy = shutil.copyfile
        calls = []

        def failing_copy(src, dst, *args, **kwargs):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_copy(src, dst, *args, **kwargs)

        with mock.patch("wsbuilder.llm.shutil.copyfile", failing_copy), \
                self.assertRaises(OSError):
            self.store.import_graph(source)
        self.assertStoreClean()
        with mock.patch.object(ModelStore, "_write_manifest", side_effect=OSError("boom")), \
                self.assertRaises(OSError):
            self.store.import_graph(source)
        self.assertStoreClean()
        # The source is untouched and imports fine afterwards.
        self.assertEqual(self.store.import_graph(source).id, "graph")

    def test_move_and_restore_on_failure(self):
        source = self.graph_dir()
        weights = os.path.join(source, "weights.ndjson")
        with open(weights, "rb") as handle:
            original = handle.read()
        with open(os.path.join(source, "tokenizer.json"), "w") as handle:
            json.dump({"model": {"type": "Unigram"}}, handle)
        with self.assertRaises(ValueError):
            self.store.import_graph(source, move=True)
        with open(weights, "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertStoreClean()
        source = self.graph_dir("good")
        record = self.store.import_graph(source, move=True)
        self.assertEqual(os.listdir(source), [])
        self.assertTrue(self.store.verify(record.id))


class TestConvert(_StoreCase):
    def test_convert_a_hugging_face_directory(self):
        source = self.hf_dir()
        record = self.store.convert(source, name="Tiny Qwen3", dtype="F16")
        self.assertEqual((record.id, record.format, record.name), ("tiny-qwen3", "graph",
                                                                   "Tiny Qwen3"))
        self.assertEqual(record.source, f"convert:{os.path.abspath(source)}")
        self.assertIn("F16", record.summary["dtypes"])
        self.assertTrue(self.store.verify(record.id))
        self.assertEqual(os.listdir(os.path.join(self.store.root, ".staging")), [])

    def test_convert_a_local_gguf_file(self):
        path = write_gguf(self.path("tiny.gguf"), QWEN3, random_weights(QWEN3))
        record = self.store.convert(path)
        self.assertEqual((record.format, record.summary["architecture"]), ("graph", "qwen3"))
        self.assertEqual(record.name, "tiny-qwen3")

    def test_convert_a_stored_gguf_model(self):
        path = write_gguf(self.path("tiny.gguf"), QWEN3, random_weights(QWEN3))
        gguf = self.store.import_file(path, model_id="tiny-gguf")
        with self.assertRaises(NotImplementedError) as caught:
            self.store.load(gguf.id)
        self.assertIn("store.convert('tiny-gguf')", str(caught.exception))
        record = self.store.convert(gguf.id, model_id="tiny-graph")
        self.assertEqual((record.id, record.source), ("tiny-graph", "convert:tiny-gguf"))
        model = self.store.load(record.id, accel="python")
        self.assertIsInstance(model.generate("hello", max_tokens=3, temperature=0), str)
        with self.assertRaisesRegex(ValueError, "already a graph"):
            self.store.convert(record.id)

    def test_a_stored_safetensors_file_cannot_be_converted(self):
        source = self.hf_dir()
        record = self.store.import_file(os.path.join(source, "model.safetensors"))
        self.assertEqual(record.format, "safetensors")
        with self.assertRaisesRegex(ValueError, "config.json"):
            self.store.convert(record.id)

    def test_unsupported_architectures_and_missing_sources(self):
        source = self.hf_dir(cfg=dict(QWEN3, architecture="gemma"))
        with self.assertRaises(NotImplementedError):
            self.store.convert(source)
        with self.assertRaises(FileNotFoundError):
            self.store.convert(self.path("nothing-here"))
        self.assertStoreClean()


class TestStoredGraphs(_StoreCase):
    def setUp(self):
        super().setUp()
        self.source = self.graph_dir()
        self.record = self.store.import_graph(self.source)

    def test_load_generates_like_graph_model(self):
        model = self.store.load(self.record.id, accel="python", max_context=32)
        self.assertIs(self.store.load(self.record.id), model)
        self.assertEqual(model.name, self.record.id)
        self.assertEqual(model.backend.model.accel_name, "python")
        self.assertEqual(model.backend.model.max_context, 32)
        direct = GraphModel.load(self.source, accel="python")
        self.addCleanup(direct.close)
        expected = GraphBackend(direct).complete("hello", max_tokens=6, temperature=0)["text"]
        self.assertEqual(model.generate("hello", max_tokens=6, temperature=0), expected)
        chat = model.chat([{"role": "user", "content": "hi"}], max_tokens=3, temperature=0)
        self.assertIsInstance(chat, str)
        self.store.unload(self.record.id)

    def test_open_returns_the_weights_reader(self):
        with self.store.open(self.record.id) as reader:
            self.assertIn("token_embd.weight", reader.names())

    def test_verify_detects_altered_and_missing_files(self):
        self.assertTrue(self.store.verify(self.record.id))
        weights = os.path.join(self.record.directory, "weights.ndjson")
        with open(weights, "rb") as handle:
            original = handle.read()
        with open(weights, "r+b") as handle:
            handle.seek(len(original) - 5)
            handle.write(b"9")
        self.assertFalse(self.store.verify(self.record.id))
        with open(weights, "wb") as handle:
            handle.write(original)
        self.assertTrue(self.store.verify(self.record.id))
        os.unlink(os.path.join(self.record.directory, "tokenizer.json"))
        self.assertFalse(self.store.verify(self.record.id))

    def test_export_copies_the_whole_directory(self):
        target = self.path("exported")
        self.assertEqual(self.store.export(self.record.id, target), target)
        self.assertEqual(sorted(os.listdir(target)),
                         ["model.json", "tokenizer.json", "weights.ndjson"])
        with self.assertRaises(FileExistsError):
            self.store.export(self.record.id, target)
        self.assertEqual([entry for entry in os.listdir(self.directory) if ".tmp-" in entry], [])
        again = self.store.import_graph(target, model_id="again")
        self.assertEqual(again.summary["files"], self.record.summary["files"])

    def test_list_rename_total_size_and_remove(self):
        listed = self.store.list()
        self.assertEqual([record.id for record in listed], [self.record.id])
        json.dumps([record.to_dict() for record in listed])
        self.assertEqual(self.store.total_size(), self.record.size)
        self.assertEqual(self.store.rename(self.record.id, "Renamed").name, "Renamed")
        self.assertEqual(self.store.get(self.record.id).summary, self.record.summary)
        self.store.load(self.record.id, accel="python")
        self.store.remove(self.record.id)
        self.assertNotIn(self.record.id, self.store)
        self.assertFalse(os.path.exists(self.record.directory))


# -- a stand-in for the Hub serving whole model repositories ----------------


def _git_oid(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class _RepoHandler(http.server.BaseHTTPRequestHandler):
    files = {}
    lfs = set()
    listed = {}  # overrides of the listed checksum, by path
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append(self.path)
        if self.path.startswith("/api/models/owner/model/tree/main"):
            entries = []
            for path, data in self.files.items():
                entry = {"type": "file", "path": path, "size": len(data), "oid": _git_oid(data)}
                if path in self.lfs:
                    entry["lfs"] = {"oid": hashlib.sha256(data).hexdigest(), "size": len(data)}
                if path in self.listed:
                    entry.update(self.listed[path])
                entries.append(entry)
            return self._send(200, json.dumps(entries).encode())
        prefix = "/owner/model/resolve/main/"
        if self.path.startswith(prefix):
            data = self.files.get(urllib.parse.unquote(self.path[len(prefix):]))
            if data is not None:
                return self._send(200, data)
        return self._send(404, b"missing")

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


class TestDownloadHuggingFaceModel(_StoreCase):
    def setUp(self):
        super().setUp()
        source = self.hf_dir()
        self.weights = random_weights(QWEN3)
        _RepoHandler.files = {name: _read(os.path.join(source, name))
                              for name in ("config.json", "tokenizer.json",
                                           "tokenizer_config.json", "model.safetensors")}
        _RepoHandler.files.update({"README.md": b"# readme", "pytorch_model.bin": b"x" * 64,
                                   "extra.safetensors": b"y" * 64})
        _RepoHandler.lfs = {"model.safetensors", "pytorch_model.bin", "extra.safetensors"}
        _RepoHandler.listed = {}
        _RepoHandler.seen = []
        self.hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RepoHandler)
        threading.Thread(target=self.hub.serve_forever, daemon=True).start()
        self.addCleanup(self.hub.server_close)
        self.addCleanup(self.hub.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.hub.server_port}"

    def download(self, **options):
        return self.store.download_huggingface_model("owner/model", endpoint=self.endpoint,
                                                     allow_http=True, **options)

    def downloaded(self):
        prefix = "/owner/model/resolve/main/"
        return sorted(path[len(prefix):] for path in _RepoHandler.seen if path.startswith(prefix))

    def test_downloads_only_what_the_conversion_needs(self):
        seen = []
        record = self.download(name="Tiny", progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(self.downloaded(), ["config.json", "model.safetensors", "tokenizer.json",
                                             "tokenizer_config.json"])
        self.assertEqual((record.id, record.format, record.source), ("tiny", "graph",
                                                                     "hf:owner/model@main"))
        total = sum(len(_RepoHandler.files[name]) for name in self.downloaded())
        self.assertEqual(seen[-1], (total, total))
        self.assertTrue(self.store.verify(record.id))
        self.assertEqual(os.listdir(os.path.join(self.store.root, ".staging")), [])
        model = self.store.load(record.id, accel="python")
        self.assertIsInstance(model.generate("hello", max_tokens=2, temperature=0), str)

    def test_sharded_checkpoints(self):
        items = list(self.weights.items())
        half = len(items) // 2
        shards = {"model-00001-of-00002.safetensors": items[:half],
                  "model-00002-of-00002.safetensors": items[half:]}
        del _RepoHandler.files["model.safetensors"]
        weight_map = {}
        for shard, tensors in shards.items():
            _RepoHandler.files[shard] = safetensors_bytes(
                [(name, "F32", _shape(value), _f32(_flat(value))) for name, value in tensors])
            _RepoHandler.lfs.add(shard)
            weight_map.update({name: shard for name, _value in tensors})
        _RepoHandler.files["model.safetensors.index.json"] = json.dumps(
            {"metadata": {}, "weight_map": weight_map}).encode()
        record = self.download()
        self.assertEqual(record.id, "model")
        self.assertIn("model-00002-of-00002.safetensors", self.downloaded())
        self.assertNotIn("extra.safetensors", self.downloaded())

    def test_odd_shard_names_are_refused(self):
        _RepoHandler.files["model.safetensors.index.json"] = json.dumps(
            {"weight_map": {"a": "../escape.safetensors"}}).encode()
        with self.assertRaisesRegex(DownloadError, "shard"):
            self.download()
        self.assertStoreClean()

    def test_checksum_mismatches_fail_and_clean_up(self):
        for path, override in (("model.safetensors", {"lfs": {"oid": "0" * 64, "size": 1}}),
                               ("config.json", {"oid": "0" * 40})):
            with self.subTest(path=path):
                _RepoHandler.listed = {path: override}
                with self.assertRaises(DownloadError):
                    self.download()
                self.assertStoreClean()

    def test_files_without_a_checksum_need_verify_false(self):
        _RepoHandler.listed = {"config.json": {"oid": None}}
        with self.assertRaisesRegex(DownloadError, "verify=False"):
            self.download()
        self.assertStoreClean()
        self.assertEqual(self.download(verify=False).format, "graph")

    def test_a_cancel_at_the_end_of_the_download_registers_nothing(self):
        cancel = threading.Event()
        total = sum(len(data) for name, data in _RepoHandler.files.items()
                    if name in ("config.json", "tokenizer.json", "tokenizer_config.json",
                                "model.safetensors"))

        def progress(done, _total):
            if done == total:
                cancel.set()

        with self.assertRaisesRegex(DownloadError, "cancelled"):
            self.download(progress=progress, cancel_event=cancel)
        self.assertTrue(cancel.is_set())
        self.assertStoreClean()

    def test_missing_files_and_size_limit(self):
        with self.assertRaisesRegex(DownloadError, "max_bytes"):
            self.download(max_bytes=1000)
        del _RepoHandler.files["model.safetensors"]
        with self.assertRaisesRegex(DownloadError, "model.safetensors"):
            self.download()
        del _RepoHandler.files["tokenizer.json"]
        with self.assertRaisesRegex(DownloadError, "tokenizer.json"):
            self.download()
        self.assertStoreClean()


# -- HTTP ---------------------------------------------------------------------


class TestHttpGraphRoutes(_StoreCase):
    def setUp(self):
        super().setUp()
        self.app = App()
        self.addCleanup(self.app.close)
        self.app.enable_llm(self.store, allow_download=True, allow_import=True,
                            download_hosts=["huggingface.co"],
                            backend_options={"accel": "python", "max_tokens": 3})
        self.server = HTTPServer("127.0.0.1", 0, self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.stop)
        self.assertTrue(self.server.wait_until_serving(5))
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def finished(self, task_id):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status, snapshot = self.call("GET", f"/api/llm/tasks?id={task_id}")
            self.assertEqual(status, 200)
            if snapshot["status"] in ("completed", "failed"):
                return snapshot
            time.sleep(0.05)
        self.fail("the task did not finish")

    def test_import_a_graph_directory_and_chat(self):
        status, record = self.call("POST", "/api/llm/import", {"path": self.graph_dir()})
        self.assertEqual((status, record["format"]), (201, "graph"))
        status, listing = self.call("GET", "/api/llm/models")
        self.assertEqual(listing["models"][0]["summary"]["chat_template"], "qwen3")
        status, answer = self.call("POST", "/api/llm/chat", {
            "model": record["id"], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertIsInstance(answer["text"], str)
        broken = self.graph_dir("broken")
        self.rewrite_spec(broken, weights="../x.ndjson")
        self.assertEqual(self.call("POST", "/api/llm/import", {"path": broken})[0], 400)

    def test_convert_runs_as_a_task(self):
        status, body = self.call("POST", "/api/llm/convert",
                                 {"source": self.hf_dir(), "name": "Over HTTP", "dtype": "F32"})
        self.assertEqual(status, 202)
        snapshot = self.finished(body["task"])
        self.assertEqual(snapshot["status"], "completed", snapshot.get("error"))
        self.assertEqual((snapshot["result"]["id"], snapshot["result"]["format"]),
                         ("over-http", "graph"))
        status, body = self.call("POST", "/api/llm/convert", {"source": self.path("nothing")})
        snapshot = self.finished(body["task"])
        self.assertEqual(snapshot["status"], "failed")
        for bad in ({}, {"source": 3}, {"source": "x", "dtype": 16}, {"source": "x", "name": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.call("POST", "/api/llm/convert", bad)[0], 400)

    def test_download_with_convert_uses_the_whole_repository(self):
        record = self.store.import_graph(self.graph_dir())
        with mock.patch.object(self.store, "download_huggingface_model",
                               return_value=record) as fetch:
            status, body = self.call("POST", "/api/llm/download",
                                     {"repo": "owner/model", "convert": True, "dtype": "F16",
                                      "name": "Mine"})
            self.assertEqual(status, 202)
            snapshot = self.finished(body["task"])
        self.assertEqual(snapshot["status"], "completed", snapshot.get("error"))
        self.assertEqual(snapshot["result"]["id"], record.id)
        args, kwargs = fetch.call_args
        self.assertEqual(args, ("owner/model",))
        self.assertEqual((kwargs["dtype"], kwargs["name"], kwargs["revision"]),
                         ("F16", "Mine", "main"))
        self.assertEqual(self.call("POST", "/api/llm/download",
                                   {"repo": "owner/model", "convert": True,
                                    "filename": "model.safetensors"})[0], 400)
        self.assertEqual(self.call("POST", "/api/llm/download",
                                   {"repo": "bad", "convert": True})[0], 400)

    def test_download_with_convert_respects_hosts(self):
        with mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://mirror.example"}):
            status, _body = self.call("POST", "/api/llm/download",
                                      {"repo": "owner/model", "convert": True})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()

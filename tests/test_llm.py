import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request

from wsbuilder import (
    App,
    DownloadError,
    GGUFWriter,
    HTTPServer,
    LanguageModel,
    LlamaCppBackend,
    ModelStore,
    OpenAICompatibleBackend,
    download_file,
    huggingface_files,
    huggingface_url,
    install_llm,
)
from wsbuilder.llm import detect_model_format, read_safetensors_header


def _gguf_bytes(name="tiny"):
    writer = GGUFWriter(architecture="llama")
    writer.add_metadata("general.name", name)
    writer.add_metadata("llama.context_length", 2048)
    writer.add_tensor("token_embd.weight", [0.25] * 32, shape=(8, 4))
    return writer.to_bytes()


def _safetensors_bytes():
    header = json.dumps(
        {"w": {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, 16]}, "__metadata__": {"format": "pt"}}
    ).encode()
    return len(header).to_bytes(8, "little") + header + b"\x00" * 16


def _write(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


# -- a stand-in for the Hugging Face Hub and its CDN ------------------------


class _HubHandler(http.server.BaseHTTPRequestHandler):
    files = {}
    seen = []
    cut_after = None  # bytes to send before dropping the first connection

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Range")))
        if self.path.startswith("/api/models/owner/repo/tree/main"):
            body = json.dumps(
                [
                    {"type": "directory", "path": "sub"},
                    {"type": "file", "path": "README.md", "size": 10},
                    *[
                        {"type": "file", "path": name, "size": len(data),
                         "lfs": {"oid": hashlib.sha256(data).hexdigest(), "size": len(data)}}
                        for name, data in self.files.items()
                    ],
                ]
            ).encode()
            return self._send(200, body, "application/json")
        if self.path.startswith("/owner/repo/resolve/main/"):
            # Only known files are redirected, and the name written into the
            # Location header is ours, not the one the request carried.
            name = {known: known for known in self.files}.get(self.path.rsplit("/", 1)[-1])
            if name is None:
                return self._send(404, b"missing", "text/plain")
            # The Hub redirects weights to a CDN on another host.
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/cdn/{name}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if self.path.startswith("/cdn/") or self.path.startswith("/files/"):
            name = self.path.rsplit("/", 1)[-1]
            data = self.files.get(name)
            if data is None:
                return self._send(404, b"missing", "text/plain")
            start = 0
            wanted = self.headers.get("Range")
            if wanted and wanted.startswith("bytes="):
                start = int(wanted[6:].split("-")[0])
                if start >= len(data):
                    return self._send(416, b"", "text/plain")
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            else:
                self.send_response(200)
            chunk = data[start:]
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            cut = type(self).cut_after
            if cut is not None and not wanted:
                type(self).cut_after = None
                self.wfile.write(chunk[:cut])
                self.wfile.flush()
                self.connection.shutdown(2)
                return None
            self.wfile.write(chunk)
            return None
        return self._send(404, b"not found", "text/plain")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return None


class HubFixture(unittest.TestCase):
    def setUp(self):
        _HubHandler.files = {"tiny.gguf": _gguf_bytes(), "weights.safetensors": _safetensors_bytes()}
        _HubHandler.seen = []
        _HubHandler.cut_after = None
        self.hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HubHandler)
        threading.Thread(target=self.hub.serve_forever, daemon=True).start()
        self.addCleanup(self.hub.server_close)
        self.addCleanup(self.hub.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.hub.server_port}"
        self.directory = tempfile.mkdtemp()
        self.store = ModelStore(os.path.join(self.directory, "models"))
        self.addCleanup(self.store.close)


class TestDownloads(HubFixture):
    def test_a_download_is_verified_and_atomic(self):
        data = _HubHandler.files["tiny.gguf"]
        target = os.path.join(self.directory, "tiny.gguf")
        seen = []
        digest = download_file(
            f"{self.endpoint}/files/tiny.gguf", target, sha256=hashlib.sha256(data).hexdigest(),
            allow_http=True, progress=lambda done, total: seen.append((done, total)),
        )
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), data)
        self.assertFalse(os.path.exists(target + ".part"))
        self.assertEqual(seen[-1], (len(data), len(data)))

    def test_a_checksum_mismatch_deletes_the_file(self):
        target = os.path.join(self.directory, "bad.gguf")
        with self.assertRaises(DownloadError):
            download_file(f"{self.endpoint}/files/tiny.gguf", target, sha256="0" * 64, allow_http=True)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".part"))

    def test_an_interrupted_download_resumes_with_a_range_request(self):
        data = _HubHandler.files["tiny.gguf"]
        target = os.path.join(self.directory, "resumed.gguf")
        url = f"{self.endpoint}/files/tiny.gguf"
        sha = hashlib.sha256(data).hexdigest()
        _HubHandler.cut_after = 100
        with self.assertRaises(DownloadError):
            download_file(url, target, sha256=sha, allow_http=True)
        self.assertEqual(os.path.getsize(target + ".part"), 100)
        download_file(url, target, sha256=sha, allow_http=True)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), data)
        self.assertEqual(_HubHandler.seen[-1][2], "bytes=100-")

    def test_size_limit(self):
        with self.assertRaises(DownloadError):
            download_file(f"{self.endpoint}/files/tiny.gguf", os.path.join(self.directory, "x"),
                          allow_http=True, max_bytes=10)

    def test_plain_http_needs_opting_in(self):
        with self.assertRaises(DownloadError):
            download_file(f"{self.endpoint}/files/tiny.gguf", os.path.join(self.directory, "x"))

    def test_the_token_does_not_follow_a_redirect_to_another_host(self):
        url = huggingface_url("owner/repo", "tiny.gguf", endpoint=self.endpoint)
        download_file(url, os.path.join(self.directory, "t.gguf"), token="secret", allow_http=True)
        by_path = {path: auth for path, auth, _range in _HubHandler.seen}
        self.assertEqual(by_path["/owner/repo/resolve/main/tiny.gguf"], "Bearer secret")
        self.assertIsNone(by_path["/cdn/tiny.gguf"])

    def test_hub_listing_reports_sizes_and_checksums(self):
        files = huggingface_files("owner/repo", endpoint=self.endpoint, allow_http=True)
        by_path = {entry["path"]: entry for entry in files}
        self.assertNotIn("sub", by_path)
        self.assertIsNone(by_path["README.md"]["sha256"])
        data = _HubHandler.files["tiny.gguf"]
        self.assertEqual(by_path["tiny.gguf"], {"path": "tiny.gguf", "size": len(data),
                                                "sha256": hashlib.sha256(data).hexdigest()})
        only = huggingface_files("owner/repo", endpoint=self.endpoint, allow_http=True, suffix=".gguf")
        self.assertEqual([entry["path"] for entry in only], ["tiny.gguf"])

    def test_bad_repository_names(self):
        for repo in ("noslash", "a/b/c", "../etc"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                huggingface_url(repo, "x.gguf")


class TestStore(HubFixture):
    def test_import_list_get_export_verify_remove(self):
        source = _write(self.directory, "My-Model.Q4.gguf", _gguf_bytes("My Model"))
        record = self.store.import_file(source)
        self.assertEqual(record.id, "my-model")
        self.assertEqual(record.format, "gguf")
        self.assertEqual(record.summary["context_length"], 2048)
        self.assertEqual(record.sha256, hashlib.sha256(_gguf_bytes("My Model")).hexdigest())
        self.assertTrue(os.path.exists(source), "import copies by default")
        self.assertEqual([r.id for r in self.store.list()], ["my-model"])
        self.assertIn("my-model", self.store)
        self.assertTrue(self.store.verify("my-model"))

        exported = self.store.export("my-model", self.directory + "/out.gguf")
        with open(exported, "rb") as handle:
            self.assertEqual(handle.read(), _gguf_bytes("My Model"))
        with self.assertRaises(FileExistsError):
            self.store.export("my-model", exported)

        self.assertEqual(self.store.rename("my-model", "Renamed").name, "Renamed")
        self.assertEqual(ModelStore(self.store.root).get("my-model").name, "Renamed")

        self.store.remove("my-model")
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(KeyError):
            self.store.get("my-model")

    def test_ids_are_unique_and_validated(self):
        source = _write(self.directory, "a.gguf", _gguf_bytes("same"))
        first = self.store.import_file(source)
        second = self.store.import_file(source)
        self.assertEqual((first.id, second.id), ("same", "same-2"))
        with self.assertRaises(FileExistsError):
            self.store.import_file(source, model_id="same")
        with self.assertRaises(ValueError):
            self.store.import_file(source, model_id="../escape")

    def test_tampering_is_detected(self):
        record = self.store.import_file(_write(self.directory, "t.gguf", _gguf_bytes()))
        with open(record.path, "ab") as handle:
            handle.write(b"x")
        self.assertFalse(self.store.verify(record.id))

    def test_move_and_safetensors(self):
        source = _write(self.directory, "w.safetensors", _safetensors_bytes())
        record = self.store.import_file(source, move=True, name="weights")
        self.assertFalse(os.path.exists(source))
        self.assertEqual(record.format, "safetensors")
        self.assertEqual(record.summary["parameters"], 4)
        self.assertEqual(record.summary["metadata"], {"format": "pt"})

    def test_unknown_and_malformed_files_are_refused(self):
        junk = _write(self.directory, "notes.txt", b"just text, not a model")
        with self.assertRaises(ValueError):
            self.store.import_file(junk)
        self.assertEqual(self.store.import_file(junk, require_known_format=False).format, "unknown")
        broken = _write(self.directory, "broken.gguf", _gguf_bytes()[:40])
        with self.assertRaises(ValueError):
            self.store.import_file(broken)
        self.assertEqual(len(self.store.list()), 1)

    def test_a_broken_manifest_hides_the_entry(self):
        record = self.store.import_file(_write(self.directory, "m.gguf", _gguf_bytes()))
        with open(os.path.join(record.directory, "manifest.json"), "w") as handle:
            handle.write("{not json")
        self.assertEqual(self.store.list(), [])

    def test_download_into_the_store(self):
        data = _HubHandler.files["tiny.gguf"]
        record = self.store.download(
            f"{self.endpoint}/files/tiny.gguf", allow_http=True, sha256=hashlib.sha256(data).hexdigest()
        )
        self.assertEqual(record.source, f"{self.endpoint}/files/tiny.gguf")
        self.assertEqual(record.size, len(data))
        self.assertTrue(self.store.verify(record.id))

    def test_hub_download_takes_the_checksum_from_the_listing(self):
        record = self.store.download_huggingface(
            "owner/repo", "tiny.gguf", endpoint=self.endpoint, allow_http=True
        )
        self.assertEqual(record.source, "hf:owner/repo@main/tiny.gguf")
        self.assertEqual(record.sha256, hashlib.sha256(_HubHandler.files["tiny.gguf"]).hexdigest())
        with self.assertRaises(DownloadError):
            self.store.download_huggingface("owner/repo", "absent.gguf", endpoint=self.endpoint,
                                            allow_http=True)
        with self.assertRaises(DownloadError):
            self.store.download_huggingface("owner/repo", "README.md", endpoint=self.endpoint,
                                            allow_http=True)

    def test_hub_download_detects_a_tampered_file(self):
        listing_data = _HubHandler.files["tiny.gguf"]
        record_sha = hashlib.sha256(listing_data).hexdigest()
        with self.assertRaises(DownloadError):
            self.store.download_huggingface(
                "owner/repo", "tiny.gguf", endpoint=self.endpoint, allow_http=True,
                sha256=record_sha[::-1],
            )
        self.assertEqual(self.store.list(), [])

    def test_format_detection(self):
        self.assertEqual(detect_model_format(_write(self.directory, "a", _gguf_bytes())), "gguf")
        self.assertEqual(detect_model_format(_write(self.directory, "b", _safetensors_bytes())), "safetensors")
        self.assertEqual(detect_model_format(_write(self.directory, "c", b"{}")), "unknown")
        bad = _write(self.directory, "d", (10**6).to_bytes(8, "little") + b"{}")
        with self.assertRaises(ValueError):
            read_safetensors_header(bad)


# -- backends ---------------------------------------------------------------


class _FakeLlama:
    instances = []

    def __init__(self, **options):
        self.options = options
        self.closed = False
        type(self).instances.append(self)

    def __call__(self, prompt, stream=False, **params):
        text = f"echo:{prompt}"
        if stream:
            return iter([{"choices": [{"text": piece}]} for piece in ("echo:", prompt)])
        return {"choices": [{"text": text, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}

    def create_chat_completion(self, messages, stream=False, **params):
        last = messages[-1]["content"]
        if stream:
            return iter([{"choices": [{"delta": {"role": "assistant"}}]}]
                        + [{"choices": [{"delta": {"content": piece}}]} for piece in ("hola ", last)])
        return {"choices": [{"message": {"role": "assistant", "content": f"hola {last}"},
                             "finish_reason": "stop"}], "usage": {"total_tokens": 5}, "params": params}

    def close(self):
        self.closed = True


class FakeLlamaCpp(unittest.TestCase):
    def setUp(self):
        _FakeLlama.instances = []
        module = types.ModuleType("llama_cpp")
        module.Llama = _FakeLlama
        previous = sys.modules.get("llama_cpp")
        sys.modules["llama_cpp"] = module
        self.addCleanup(lambda: sys.modules.pop("llama_cpp", None) if previous is None
                        else sys.modules.__setitem__("llama_cpp", previous))


class TestLlamaCppBackend(FakeLlamaCpp):
    def test_generate_and_chat(self):
        model = LanguageModel.from_gguf("/models/x.gguf", n_ctx=512, n_gpu_layers=10)
        self.assertEqual(_FakeLlama.instances[0].options["n_ctx"], 512)
        self.assertEqual(_FakeLlama.instances[0].options["n_gpu_layers"], 10)
        self.assertEqual(model.generate("hi", max_tokens=5), "echo:hi")
        self.assertEqual("".join(model.stream_generate("hi")), "echo:hi")
        self.assertEqual(model.chat("mundo"), "hola mundo")
        self.assertEqual("".join(model.stream_chat([{"role": "user", "content": "mundo"}])), "hola mundo")
        completion = model.chat_completion("x", temperature=0.2, unknown_option=1)
        self.assertEqual(completion["usage"], {"total_tokens": 5})
        model.close()
        self.assertTrue(_FakeLlama.instances[0].closed)

    def test_bad_roles_are_refused(self):
        model = LanguageModel.from_gguf("/models/x.gguf")
        with self.assertRaises(ValueError):
            model.chat([{"role": "root", "content": "x"}])

    def test_store_load_caches_and_unloads(self):
        directory = tempfile.mkdtemp()
        store = ModelStore(os.path.join(directory, "models"))
        record = store.import_file(_write(directory, "m.gguf", _gguf_bytes("m")))
        first = store.load(record.id)
        self.assertIs(store.load(record.id), first)
        self.assertEqual(_FakeLlama.instances[0].options["model_path"], record.path)
        store.remove(record.id)
        self.assertTrue(_FakeLlama.instances[0].closed)

    def test_store_load_refuses_non_gguf(self):
        directory = tempfile.mkdtemp()
        store = ModelStore(os.path.join(directory, "models"))
        record = store.import_file(_write(directory, "w.safetensors", _safetensors_bytes()))
        with self.assertRaises(ValueError):
            store.load(record.id)


class TestMissingLlamaCpp(unittest.TestCase):
    def test_a_clear_install_hint(self):
        previous = sys.modules.get("llama_cpp")
        sys.modules["llama_cpp"] = None  # makes the import fail
        try:
            with self.assertRaises(ImportError) as caught:
                LlamaCppBackend("/models/x.gguf")
        finally:
            if previous is None:
                sys.modules.pop("llama_cpp", None)
            else:
                sys.modules["llama_cpp"] = previous
        self.assertIn("pip install wsbuilder[llm]", str(caught.exception))


class _OpenAIHandler(http.server.BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, self.headers.get("Authorization"), body))
        if self.path.endswith("/chat/completions"):
            reply = "respuesta a " + body["messages"][-1]["content"]
            key = "delta"
        else:
            reply = "texto para " + body["prompt"]
            key = "text"
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in reply.split(" "):
                choice = {"delta": {"content": piece + " "}} if key == "delta" else {"text": piece + " "}
                self.wfile.write(f"data: {json.dumps({'choices': [choice]})}\n\n".encode())
            self.wfile.write(b": keep-alive\n\ndata: [DONE]\n\n")
            return
        choice = ({"message": {"role": "assistant", "content": reply}} if key == "delta"
                  else {"text": reply})
        choice["finish_reason"] = "stop"
        payload = json.dumps({"choices": [choice], "usage": {"total_tokens": 7}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TestOpenAICompatibleBackend(unittest.TestCase):
    def setUp(self):
        _OpenAIHandler.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

    def test_chat_and_completion_with_and_without_streaming(self):
        model = LanguageModel.from_openai(self.base, "llama3.2", api_key="k")
        self.assertEqual(model.chat("hola", temperature=0.1), "respuesta a hola")
        path, auth, body = _OpenAIHandler.requests[-1]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer k"))
        self.assertEqual(body["model"], "llama3.2")
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual("".join(model.stream_chat("hola")).strip(), "respuesta a hola")
        self.assertEqual(model.generate("x"), "texto para x")
        self.assertEqual("".join(model.stream_generate("x")).strip(), "texto para x")

    def test_unreachable_server(self):
        model = LanguageModel.from_openai("http://127.0.0.1:9/v1", "m", timeout=2)
        with self.assertRaises(ConnectionError):
            model.chat("hola")

    def test_bad_base_url(self):
        with self.assertRaises(ValueError):
            OpenAICompatibleBackend("ftp://x", "m")


def _tiny_llama(path):
    """A real (random-weight) llama model, written with GGUFWriter."""
    import random

    from wsbuilder.gguf import GGUFValueType as T

    rng = random.Random(0)
    embd, heads, ff = 32, 4, 64
    words = ["▁", "▁hola", "▁mundo", "a", "o"]
    tokens = ["<unk>", "<s>", "</s>"] + [f"<0x{b:02X}>" for b in range(256)] + words
    vocab = len(tokens)
    writer = GGUFWriter(architecture="llama")
    writer.add_metadata("general.name", "tiny-random-llama")
    for key, value in {"context_length": 128, "embedding_length": embd, "block_count": 1,
                       "feed_forward_length": ff, "attention.head_count": heads,
                       "attention.head_count_kv": heads, "rope.dimension_count": embd // heads}.items():
        writer.add_metadata(f"llama.{key}", value, T.UINT32)
    writer.add_metadata("llama.attention.layer_norm_rms_epsilon", 1e-5, T.FLOAT32)
    writer.add_metadata("tokenizer.ggml.model", "llama")
    writer.add_metadata("tokenizer.ggml.tokens", tokens, item_type=T.STRING)
    writer.add_metadata("tokenizer.ggml.scores", [0.0] * 3 + [-1000.0] * 256 + [-1.0] * len(words),
                        item_type=T.FLOAT32)
    writer.add_metadata("tokenizer.ggml.token_type", [2, 3, 3] + [6] * 256 + [1] * len(words),
                        item_type=T.INT32)
    writer.add_metadata("tokenizer.ggml.bos_token_id", 1, T.UINT32)
    writer.add_metadata("tokenizer.ggml.eos_token_id", 2, T.UINT32)
    writer.add_metadata("tokenizer.chat_template",
                        "{% for m in messages %}{{ m['content'] }}\n{% endfor %}")

    def weights(*shape):
        count = 1
        for dim in shape:
            count *= dim
        return [rng.gauss(0, 0.02) for _ in range(count)]

    writer.add_tensor("token_embd.weight", weights(embd, vocab), shape=(embd, vocab))
    writer.add_tensor("output_norm.weight", [1.0] * embd, shape=(embd,))
    writer.add_tensor("output.weight", weights(embd, vocab), shape=(embd, vocab))
    writer.add_tensor("blk.0.attn_norm.weight", [1.0] * embd, shape=(embd,))
    for name in ("attn_q", "attn_k", "attn_v", "attn_output"):
        writer.add_tensor(f"blk.0.{name}.weight", weights(embd, embd), shape=(embd, embd))
    writer.add_tensor("blk.0.ffn_norm.weight", [1.0] * embd, shape=(embd,))
    writer.add_tensor("blk.0.ffn_gate.weight", weights(embd, ff), shape=(embd, ff))
    writer.add_tensor("blk.0.ffn_up.weight", weights(embd, ff), shape=(embd, ff))
    writer.add_tensor("blk.0.ffn_down.weight", weights(ff, embd), shape=(ff, embd))
    writer.write(path)
    return path


def _real_llama_cpp():
    module = sys.modules.get("llama_cpp")
    if module is not None and not isinstance(module, types.ModuleType):
        return False
    try:
        import llama_cpp
    except ImportError:
        return False
    return hasattr(llama_cpp, "Llama") and llama_cpp.Llama is not _FakeLlama


@unittest.skipUnless(_real_llama_cpp(), "install wsbuilder[llm] to run a real GGUF model")
class TestRealLlamaCpp(unittest.TestCase):
    def test_a_model_written_by_gguf_writer_runs_in_llama_cpp(self):
        directory = tempfile.mkdtemp()
        store = ModelStore(os.path.join(directory, "models"))
        self.addCleanup(store.close)
        record = store.import_file(_tiny_llama(os.path.join(directory, "tiny.gguf")))
        model = store.load(record.id, n_ctx=64, seed=1)
        self.assertIsInstance(model.generate("hola", max_tokens=4, temperature=0), str)
        self.assertIsInstance("".join(model.stream_chat("hola", max_tokens=4, temperature=0)), str)


# -- HTTP endpoints ---------------------------------------------------------


class _EchoBackend:
    name = "echo"

    def complete(self, prompt, *, stream=False, **options):
        if stream:
            return iter(["eco:", prompt])
        return {"text": f"eco:{prompt}", "finish_reason": "stop", "usage": None}

    def chat(self, messages, *, stream=False, **options):
        last = messages[-1]["content"]
        if stream:
            return iter(["eco ", last])
        return {"text": f"eco {last}", "finish_reason": "stop", "usage": {"total_tokens": 2}}

    def close(self):
        pass


class TestInstallLlm(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.store = ModelStore(os.path.join(self.directory, "models"))
        self.store.import_file(_write(self.directory, "stored.gguf", _gguf_bytes("stored")))
        self.app = App()
        self.addCleanup(self.app.close)
        self.app.enable_llm(
            self.store, models={"eco": LanguageModel(_EchoBackend())},
            allow_download=True, allow_import=True, allow_delete=True,
            download_hosts=["huggingface.co"],
        )
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
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type"), exc.read()

    def test_listing_and_info(self):
        status, _type, body = self.call("GET", "/api/llm/models")
        listing = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in listing["models"]], ["stored"])
        self.assertEqual(listing["remote"], ["eco"])
        self.assertEqual(json.loads(self.call("GET", "/api/llm/model?id=stored")[2])["format"], "gguf")
        self.assertEqual(self.call("GET", "/api/llm/model?id=nope")[0], 404)

    def test_chat_plain_and_streamed(self):
        status, _type, body = self.call("POST", "/api/llm/chat",
                                        {"model": "eco", "messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual((status, json.loads(body)["text"]), (200, "eco hola"))
        status, content_type, body = self.call(
            "POST", "/api/llm/chat", {"model": "eco", "stream": True,
                                      "messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual(content_type, "application/x-ndjson")
        pieces = [json.loads(line)["content"] for line in body.decode().splitlines()]
        self.assertEqual("".join(pieces), "eco hola")
        self.assertEqual(json.loads(self.call("POST", "/api/llm/generate",
                                              {"model": "eco", "prompt": "x"})[2])["text"], "eco:x")
        self.assertEqual(self.call("POST", "/api/llm/chat", {"model": "ghost", "messages": []})[0], 404)
        self.assertEqual(self.call("POST", "/api/llm/chat", ["not", "an", "object"])[0], 400)

    def test_openai_compatible_endpoint(self):
        models = json.loads(self.call("GET", "/v1/models")[2])
        self.assertEqual({m["id"] for m in models["data"]}, {"stored", "eco"})
        status, _type, body = self.call(
            "POST", "/v1/chat/completions", {"model": "eco", "messages": [{"role": "user", "content": "hi"}]})
        completion = json.loads(body)
        self.assertEqual(completion["object"], "chat.completion")
        self.assertEqual(completion["choices"][0]["message"], {"role": "assistant", "content": "eco hi"})
        # Our own OpenAI client can talk to it.
        model = LanguageModel.from_openai(self.base + "/v1", "eco")
        self.assertEqual("".join(model.stream_chat("hola")), "eco hola")

    def test_download_endpoint_checks_hosts_and_scheme(self):
        status, _type, body = self.call("POST", "/api/llm/download", {"url": "https://evil.example/x.gguf"})
        self.assertEqual(status, 403)
        status, _type, _body = self.call("POST", "/api/llm/download", {"url": "http://huggingface.co/x.gguf"})
        self.assertEqual(status, 400)

    def test_download_task_reports_its_outcome(self):
        # huggingface.co is allowed but unreachable from tests: the task fails cleanly.
        status, _type, body = self.call("POST", "/api/llm/download",
                                        {"url": "https://huggingface.co/owner/repo/resolve/main/x.gguf"})
        self.assertEqual(status, 202)
        task_id = json.loads(body)["task"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snapshot = json.loads(self.call("GET", f"/api/llm/tasks?id={task_id}")[2])
            if snapshot["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        self.assertIn(snapshot["status"], ("completed", "failed"))
        self.assertEqual(self.call("GET", "/api/llm/tasks?id=unknown")[0], 404)

    def test_import_and_delete(self):
        path = _write(self.directory, "another.gguf", _gguf_bytes("another"))
        status, _type, body = self.call("POST", "/api/llm/import", {"path": path})
        self.assertEqual((status, json.loads(body)["id"]), (201, "another"))
        self.assertEqual(self.call("POST", "/api/llm/import", {"path": "/no/such/file"})[0], 400)
        self.assertEqual(json.loads(self.call("POST", "/api/llm/delete", {"id": "another"})[2]),
                         {"deleted": "another"})
        self.assertEqual(self.call("POST", "/api/llm/delete", {"id": "another"})[0], 404)

    def test_disk_changing_routes_are_off_by_default(self):
        app = App()
        self.addCleanup(app.close)
        install_llm(app, self.store)
        paths = {route["path"] for route in app.describe()["routes"]}
        self.assertIn("/api/llm/models", paths)
        for path in ("/api/llm/download", "/api/llm/import", "/api/llm/delete"):
            self.assertNotIn(path, paths)


if __name__ == "__main__":
    unittest.main()

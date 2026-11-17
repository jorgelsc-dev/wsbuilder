import hashlib
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from wsbuilder import (
    App,
    DownloadError,
    HTTPServer,
    LanguageModel,
    ModelStore,
    ModelWriter,
    NeuralNetwork,
    OpenAICompatibleBackend,
    download_file,
    huggingface_files,
    huggingface_url,
    install_llm,
)
from wsbuilder.llm import detect_model_format


def _model_bytes(name="tiny"):
    writer = ModelWriter(architecture="tiny-lm", name=name)
    writer.add_metadata("context_length", 2048)
    writer.add_tensor("token_embd.weight", [0.25] * 32, shape=(8, 4), dtype="F32")
    return writer.to_bytes()


def _write(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


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
        _HubHandler.files = {"tiny.ndjson": _model_bytes(), "notes.txt": b"not a model"}
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
        data = _HubHandler.files["tiny.ndjson"]
        target = os.path.join(self.directory, "tiny.ndjson")
        seen = []
        digest = download_file(
            f"{self.endpoint}/files/tiny.ndjson", target, sha256=hashlib.sha256(data).hexdigest(),
            allow_http=True, progress=lambda done, total: seen.append((done, total)),
        )
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), data)
        self.assertFalse(os.path.exists(target + ".part"))
        self.assertEqual(seen[-1], (len(data), len(data)))

    def test_a_checksum_mismatch_deletes_the_file(self):
        target = os.path.join(self.directory, "bad.ndjson")
        with self.assertRaises(DownloadError):
            download_file(f"{self.endpoint}/files/tiny.ndjson", target, sha256="0" * 64, allow_http=True)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".part"))

    def test_an_interrupted_download_resumes_with_a_range_request(self):
        data = _HubHandler.files["tiny.ndjson"]
        target = os.path.join(self.directory, "resumed.ndjson")
        url = f"{self.endpoint}/files/tiny.ndjson"
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
            download_file(f"{self.endpoint}/files/tiny.ndjson", os.path.join(self.directory, "x"),
                          allow_http=True, max_bytes=10)

    def test_plain_http_needs_opting_in(self):
        with self.assertRaises(DownloadError):
            download_file(f"{self.endpoint}/files/tiny.ndjson", os.path.join(self.directory, "x"))

    def test_the_token_does_not_follow_a_redirect_to_another_host(self):
        url = huggingface_url("owner/repo", "tiny.ndjson", endpoint=self.endpoint)
        download_file(url, os.path.join(self.directory, "t.ndjson"), token="secret", allow_http=True)
        by_path = {path: auth for path, auth, _range in _HubHandler.seen}
        self.assertEqual(by_path["/owner/repo/resolve/main/tiny.ndjson"], "Bearer secret")
        self.assertIsNone(by_path["/cdn/tiny.ndjson"])

    def test_hub_listing_reports_sizes_and_checksums(self):
        files = huggingface_files("owner/repo", endpoint=self.endpoint, allow_http=True)
        by_path = {entry["path"]: entry for entry in files}
        self.assertNotIn("sub", by_path)
        self.assertIsNone(by_path["README.md"]["sha256"])
        data = _HubHandler.files["tiny.ndjson"]
        self.assertEqual(by_path["tiny.ndjson"], {"path": "tiny.ndjson", "size": len(data),
                                                "sha256": hashlib.sha256(data).hexdigest()})
        only = huggingface_files("owner/repo", endpoint=self.endpoint, allow_http=True, suffix=".ndjson")
        self.assertEqual([entry["path"] for entry in only], ["tiny.ndjson"])

    def test_bad_repository_names(self):
        for repo in ("noslash", "a/b/c", "../etc"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                huggingface_url(repo, "x.ndjson")


class TestStore(HubFixture):
    def test_import_list_get_export_verify_remove(self):
        source = _write(self.directory, "My-Model.Q4.ndjson", _model_bytes("My Model"))
        record = self.store.import_file(source)
        self.assertEqual(record.id, "my-model")
        self.assertEqual(record.format, "ndjson")
        self.assertEqual(record.summary["metadata"], {"context_length": 2048})
        self.assertEqual(record.summary["parameters"], 32)
        self.assertEqual(record.summary["dtypes"], {"F32": 32})
        self.assertEqual(record.sha256, hashlib.sha256(_model_bytes("My Model")).hexdigest())
        self.assertTrue(os.path.exists(source), "import copies by default")
        self.assertEqual([r.id for r in self.store.list()], ["my-model"])
        self.assertIn("my-model", self.store)
        self.assertTrue(self.store.verify("my-model"))

        exported = self.store.export("my-model", self.directory + "/out.ndjson")
        with open(exported, "rb") as handle:
            self.assertEqual(handle.read(), _model_bytes("My Model"))
        with self.assertRaises(FileExistsError):
            self.store.export("my-model", exported)

        self.assertEqual(self.store.rename("my-model", "Renamed").name, "Renamed")
        self.assertEqual(ModelStore(self.store.root).get("my-model").name, "Renamed")

        self.store.remove("my-model")
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(KeyError):
            self.store.get("my-model")

    def test_ids_are_unique_and_validated(self):
        source = _write(self.directory, "a.ndjson", _model_bytes("same"))
        first = self.store.import_file(source)
        second = self.store.import_file(source)
        self.assertEqual((first.id, second.id), ("same", "same-2"))
        with self.assertRaises(FileExistsError):
            self.store.import_file(source, model_id="same")
        with self.assertRaises(ValueError):
            self.store.import_file(source, model_id="../escape")

    def test_tampering_is_detected(self):
        record = self.store.import_file(_write(self.directory, "t.ndjson", _model_bytes()))
        with open(record.path, "ab") as handle:
            handle.write(b"x")
        self.assertFalse(self.store.verify(record.id))

    def test_move(self):
        source = _write(self.directory, "w.ndjson", _model_bytes("weights"))
        record = self.store.import_file(source, move=True, name="weights")
        self.assertFalse(os.path.exists(source))
        self.assertEqual(record.format, "ndjson")
        self.assertEqual(record.summary["parameters"], 32)

    def test_unknown_and_malformed_files_are_refused(self):
        junk = _write(self.directory, "notes.txt", b"just text, not a model")
        with self.assertRaises(ValueError):
            self.store.import_file(junk)
        self.assertEqual(self.store.import_file(junk, require_known_format=False).format, "unknown")
        broken = _write(self.directory, "broken.ndjson", _model_bytes()[:-20])
        with self.assertRaises(ValueError):
            self.store.import_file(broken)
        self.assertEqual(len(self.store.list()), 1)

    def test_a_broken_manifest_hides_the_entry(self):
        record = self.store.import_file(_write(self.directory, "m.ndjson", _model_bytes()))
        with open(os.path.join(record.directory, "manifest.json"), "w") as handle:
            handle.write("{not json")
        self.assertEqual(self.store.list(), [])

    def test_download_into_the_store(self):
        data = _HubHandler.files["tiny.ndjson"]
        record = self.store.download(
            f"{self.endpoint}/files/tiny.ndjson", allow_http=True, sha256=hashlib.sha256(data).hexdigest()
        )
        self.assertEqual(record.source, f"{self.endpoint}/files/tiny.ndjson")
        self.assertEqual(record.size, len(data))
        self.assertTrue(self.store.verify(record.id))

    def test_hub_download_takes_the_checksum_from_the_listing(self):
        record = self.store.download_huggingface(
            "owner/repo", "tiny.ndjson", endpoint=self.endpoint, allow_http=True
        )
        self.assertEqual(record.source, "hf:owner/repo@main/tiny.ndjson")
        self.assertEqual(record.sha256, hashlib.sha256(_HubHandler.files["tiny.ndjson"]).hexdigest())
        with self.assertRaises(DownloadError):
            self.store.download_huggingface("owner/repo", "absent.ndjson", endpoint=self.endpoint,
                                            allow_http=True)
        with self.assertRaises(DownloadError):
            self.store.download_huggingface("owner/repo", "README.md", endpoint=self.endpoint,
                                            allow_http=True)

    def test_hub_download_detects_a_tampered_file(self):
        listing_data = _HubHandler.files["tiny.ndjson"]
        record_sha = hashlib.sha256(listing_data).hexdigest()
        with self.assertRaises(DownloadError):
            self.store.download_huggingface(
                "owner/repo", "tiny.ndjson", endpoint=self.endpoint, allow_http=True,
                sha256=record_sha[::-1],
            )
        self.assertEqual(self.store.list(), [])

    def test_format_detection(self):
        self.assertEqual(detect_model_format(_write(self.directory, "a", _model_bytes())), "ndjson")
        self.assertEqual(detect_model_format(_write(self.directory, "c", b"{}")), "unknown")
        self.assertEqual(detect_model_format(_write(self.directory, "d", b"\x00GGUF")), "unknown")
        self.assertEqual(detect_model_format(_write(self.directory, "e", b"")), "unknown")


# -- backends ---------------------------------------------------------------


class TestStoreLoader(unittest.TestCase):
    def test_without_a_loader_stored_models_cannot_be_chatted_with(self):
        directory = tempfile.mkdtemp()
        store = ModelStore(os.path.join(directory, "models"))
        record = store.import_file(_write(directory, "m.ndjson", _model_bytes("m")))
        with self.assertRaises(NotImplementedError):
            store.load(record.id)

    def test_a_loader_runs_stored_models_and_is_cached_and_unloaded(self):
        directory = tempfile.mkdtemp()
        seen = []

        def loader(record, **options):
            seen.append((record.id, record.path, options))
            return LanguageModel(_EchoBackend(), name=record.id)

        store = ModelStore(os.path.join(directory, "models"), loader=loader)
        record = store.import_file(_write(directory, "m.ndjson", _model_bytes("m")))
        first = store.load(record.id, temperature=0)
        self.assertIs(store.load(record.id), first)
        self.assertEqual(seen, [(record.id, record.path, {"temperature": 0})])
        self.assertEqual(first.chat("hola"), "eco hola")
        store.remove(record.id)
        self.assertEqual(store.list(), [])

    def test_bad_roles_are_refused(self):
        model = LanguageModel(_EchoBackend())
        with self.assertRaises(ValueError):
            model.chat([{"role": "root", "content": "x"}])

    def test_stored_networks_load_back(self):
        directory = tempfile.mkdtemp()
        store = ModelStore(os.path.join(directory, "models"))
        network = NeuralNetwork(seed=3)
        network.add_dense(3, input_size=2, activation="tanh")
        network.add_dense(1, activation="sigmoid")
        path = network.save(os.path.join(directory, "net.ndjson"), name="red")
        record = store.import_file(path)
        self.assertEqual(record.format, "ndjson")
        self.assertEqual(record.summary["architecture"], "wsbuilder.dense")
        self.assertEqual(record.summary["parameters"], 3 * 2 + 3 + 3 + 1)
        same = store.load_network(record.id)
        self.assertEqual(same.predict([0.2, -0.4]), network.predict([0.2, -0.4]))
        with store.open(record.id) as reader:
            self.assertEqual(reader.tensor("blk.0.weight").shape, (3, 2))


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


# -- HTTP endpoints ---------------------------------------------------------


class TestInstallLlm(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.store = ModelStore(os.path.join(self.directory, "models"))
        self.store.import_file(_write(self.directory, "stored.ndjson", _model_bytes("stored")))
        self.app = App()
        self.addCleanup(self.app.close)
        self.app.enable_llm(
            self.store, models={"eco": LanguageModel(_EchoBackend())},
            allow_download=True, allow_import=True, allow_delete=True, allow_train=True,
            download_hosts=["huggingface.co"], backend_options={"temperature": 0},
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
            with exc:
                return exc.code, exc.headers.get("Content-Type"), exc.read()

    def test_listing_and_info(self):
        status, _type, body = self.call("GET", "/api/llm/models")
        listing = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in listing["models"]], ["stored"])
        self.assertEqual(listing["remote"], ["eco"])
        self.assertEqual(json.loads(self.call("GET", "/api/llm/model?id=stored")[2])["format"], "ndjson")
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
        status, _type, body = self.call("POST", "/api/llm/download", {"url": "https://evil.example/x.ndjson"})
        self.assertEqual(status, 403)
        status, _type, _body = self.call("POST", "/api/llm/download", {"url": "http://huggingface.co/x.ndjson"})
        self.assertEqual(status, 400)

    def test_download_task_reports_its_outcome(self):
        # huggingface.co is allowed but unreachable from tests: the task fails cleanly.
        status, _type, body = self.call("POST", "/api/llm/download",
                                        {"url": "https://huggingface.co/owner/repo/resolve/main/x.ndjson"})
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
        path = _write(self.directory, "another.ndjson", _model_bytes("another"))
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
        for path in ("/api/llm/download", "/api/llm/import", "/api/llm/delete", "/api/llm/train",
                     "/api/llm/tasks"):
            self.assertNotIn(path, paths)

    def _finished(self, task_id):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            snapshot = json.loads(self.call("GET", f"/api/llm/tasks?id={task_id}")[2])
            if snapshot["status"] in ("completed", "failed"):
                return snapshot
            time.sleep(0.05)
        self.fail("the task did not finish")

    def test_train_a_native_model_over_http_and_chat_with_it(self):
        conversations = [[{"role": "user", "content": "hola"}, {"role": "assistant", "content": "Hola!"}]]
        status, _type, body = self.call("POST", "/api/llm/train", {
            "name": "Bot HTTP", "conversations": conversations, "context": 16, "embedding": 8,
            "hidden": [32], "epochs": 60, "learning_rate": 0.1, "seed": 1,
        })
        self.assertEqual(status, 202)
        snapshot = self._finished(json.loads(body)["task"])
        self.assertEqual(snapshot["status"], "completed", snapshot.get("error"))
        self.assertEqual(snapshot["result"]["id"], "bot-http")
        self.assertEqual(snapshot["result"]["summary"]["architecture"], "wsbuilder.bytelm")
        self.assertEqual(snapshot["metadata"]["epoch"], 60)

        status, _type, body = self.call("POST", "/api/llm/chat", {
            "model": "bot-http", "messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual((status, json.loads(body)["text"]), (200, "Hola!"))
        status, _type, body = self.call("POST", "/v1/chat/completions", {
            "model": "bot-http", "stream": False, "messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "Hola!")

    def test_train_requests_are_validated(self):
        bad = [
            {},
            {"texts": ["a"], "conversations": []},
            {"texts": [1]},
            {"texts": ["a"], "epochs": 0},
            {"texts": ["a"], "epochs": 10**6},
            {"texts": ["a"], "hidden": [10**6]},
            {"texts": ["a"], "context": 10**4},
            {"texts": ["a"], "learning_rate": "fast"},
            {"texts": ["x" * (2 * 1024 * 1024)]},
            {"conversations": ["no"]},
        ]
        for payload in bad:
            with self.subTest(payload=str(payload)[:60]):
                # An oversized body may already be refused by the server (413).
                self.assertIn(self.call("POST", "/api/llm/train", payload)[0], (400, 413))

    def test_stored_models_that_cannot_chat_answer_501(self):
        status, _type, body = self.call("POST", "/api/llm/chat", {
            "model": "stored", "messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual(status, 501)
        self.assertIn("not a language model", json.loads(body)["error"]["message"])


if __name__ == "__main__":
    unittest.main()

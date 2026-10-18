import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wsbuilder import App, Request
from wsbuilder.logs import ConsoleLog, NDJSONLog, append_ndjson, console, get_log


class TestNDJSONLogs(unittest.TestCase):
    def test_append_ndjson_writes_one_record_per_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "events.ndjson"

            first = append_ndjson(path, {"event": "start", "ok": True})
            second = append_ndjson(path, {"event": "stop", "code": 0})

            self.assertEqual(first["event"], "start")
            self.assertEqual(second["event"], "stop")

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), {"event": "start", "ok": True})
            self.assertEqual(json.loads(lines[1]), {"event": "stop", "code": 0})

    def test_concurrent_appends_are_serialized_as_single_writes(self):
        state_lock = threading.Lock()
        barrier = threading.Barrier(8)
        state = {"active": 0, "max_active": 0, "writes": []}

        class FakeFile:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def write(self, text):
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(
                        state["max_active"],
                        state["active"],
                    )
                time.sleep(0.005)
                with state_lock:
                    state["writes"].append(text)
                    state["active"] -= 1
                return len(text)

        def append_record(index):
            barrier.wait()
            append_ndjson(
                "ignored.ndjson",
                {"index": index},
                ensure_parent=False,
            )

        with patch.object(Path, "open", side_effect=lambda *args, **kwargs: FakeFile()):
            threads = [
                threading.Thread(target=append_record, args=(index,))
                for index in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(state["max_active"], 1)
        self.assertEqual(len(state["writes"]), 8)
        self.assertTrue(all(line.endswith("\n") for line in state["writes"]))
        self.assertEqual(
            {json.loads(line)["index"] for line in state["writes"]},
            set(range(8)),
        )

    def test_app_enable_logs_attaches_writer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "app.ndjson"
            app = App()

            logs = app.enable_logs(path=path)
            self.assertIs(app.logs, logs)
            self.assertIsInstance(logs, NDJSONLog)
            self.assertTrue(app.describe()["logs_enabled"])

            logs.event("request", method="GET", path="/")

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(
                json.loads(lines[0]),
                {"event": "request", "method": "GET", "path": "/"},
            )



def _boom():
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        return exc


class TestConsoleLog(unittest.TestCase):
    def test_lines_are_tagged_by_event_and_filtered_by_level(self):
        stream = io.StringIO()
        log = ConsoleLog(level="info", stream=stream)

        log.debug("orm", "hidden")
        log.info("server", "listening")
        log.warning("tls", "handshake error")

        self.assertEqual(stream.getvalue(), "[server] listening\n[tls] handshake error\n")

    def test_only_error_records_print_their_traceback(self):
        stream = io.StringIO()
        log = ConsoleLog(stream=stream)

        log.warning("http", "send error", exc=_boom())
        self.assertEqual(stream.getvalue(), "[http] send error\n")

        record = log.exception("http", "handler error", _boom())
        output = stream.getvalue()
        self.assertIn("[http] handler error\nTraceback (most recent call last):", output)
        self.assertIn("RuntimeError: boom", output)
        self.assertEqual(record["error"]["type"], "RuntimeError")

    def test_unknown_levels_are_rejected(self):
        with self.assertRaises(ValueError):
            ConsoleLog(level="loud")
        with self.assertRaises(ValueError):
            ConsoleLog().log("verbose", "x", "y")


class TestLeveledNDJSONLog(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.path = Path(tmpdir.name) / "app.ndjson"
        self.stream = io.StringIO()
        patcher = patch.object(console, "stream", self.stream)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _records(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_records_carry_level_event_message_and_error(self):
        log = NDJSONLog(self.path)
        log.info("server", "listening", port=8765)
        log.exception("http", "handler error", _boom())

        info, error = self._records()
        self.assertEqual(
            {key: info[key] for key in ("level", "event", "message", "port")},
            {"level": "info", "event": "server", "message": "listening", "port": 8765},
        )
        self.assertIsInstance(info["ts"], float)
        self.assertEqual(error["level"], "error")
        self.assertEqual(error["error"]["message"], "boom")
        self.assertIn("RuntimeError: boom", error["error"]["traceback"])

    def test_warnings_and_errors_are_echoed_to_the_console(self):
        log = NDJSONLog(self.path)
        log.info("server", "listening")
        log.warning("tls", "handshake error")

        self.assertEqual(self.stream.getvalue(), "[tls] handshake error\n")
        self.assertEqual(len(self._records()), 2)

    def test_console_echo_can_be_disabled(self):
        log = NDJSONLog(self.path, console_level=None)
        log.exception("http", "handler error", _boom())

        self.assertEqual(self.stream.getvalue(), "")
        self.assertEqual(len(self._records()), 1)

    def test_get_log_prefers_the_app_writer(self):
        app = App()
        self.addCleanup(app.close)
        self.assertIs(get_log(), console)
        self.assertIs(get_log(app), console)

        app.logs = object()  # not a leveled writer
        self.assertIs(get_log(app), console)

        logs = app.enable_logs(path=self.path, console_level="error")
        self.assertIs(get_log(app), logs)
        self.assertEqual(logs.describe()["console_level"], "error")

    def test_failing_handler_is_logged_with_traceback(self):
        app = App()
        self.addCleanup(app.close)
        app.enable_logs(path=self.path)

        @app.api("/fail", methods=("GET",))
        def fail(_request):
            raise RuntimeError("boom")

        response = app.dispatch(
            Request(
                method="GET",
                path="/fail",
                query_string="",
                headers={},
                body=b"",
                client=("127.0.0.1", 1234),
                tls={},
            )
        )

        self.assertEqual(response.status, 500)
        (record,) = self._records()
        self.assertEqual(record["event"], "http")
        self.assertEqual(record["message"], "handler error GET /fail: boom")
        self.assertIn("raise RuntimeError", record["error"]["traceback"])
        self.assertIn("[http] handler error GET /fail: boom\nTraceback", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()

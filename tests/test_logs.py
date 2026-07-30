import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wsbuilder import App
from wsbuilder.logs import NDJSONLog, append_ndjson


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


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

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

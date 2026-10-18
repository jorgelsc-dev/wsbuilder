import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wsbuilder import Request

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "full-project" / "app.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("wsbuilder_full_project_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestFullProjectExample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = _load_example()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        # Keep the example's database and logs out of the source tree.
        cls.patches = [
            patch.object(cls.example, "DATA_DIR", root / "data"),
            patch.object(cls.example, "LOG_DIR", root / "logs"),
            patch.object(cls.example, "DB_PATH", root / "data" / "demo.sqlite3"),
        ]
        for item in cls.patches:
            item.start()
        cls.app = cls.example.build_app()

    @classmethod
    def tearDownClass(cls):
        cls.example.shutdown_example(cls.app)
        cls.app.close()
        for item in cls.patches:
            item.stop()
        cls.tmp.cleanup()

    def _call(self, method, path, body=b"", query=""):
        request = Request(
            method,
            path,
            query,
            {"host": "localhost", "content-type": "application/json"},
            body,
            ("127.0.0.1", 1234),
        )
        response = self.app.dispatch(request)
        return response.status, json.loads(response.body or b"null")

    def test_creating_a_note_then_repeating_its_title_is_a_conflict(self):
        body = json.dumps({"title": "unique-title-for-test"}).encode()
        status, payload = self._call("POST", "/api/notes", body)
        self.assertEqual(status, 201)
        self.assertEqual(payload["title"], "unique-title-for-test")

        status, payload = self._call("POST", "/api/notes", body)
        self.assertEqual(status, 409)
        self.assertEqual(payload["status"], "error")

    def test_predict_rejects_non_numeric_input(self):
        for value in ("abc", "nan", "inf"):
            with self.subTest(value=value):
                status, payload = self._call("GET", "/api/ml/predict", query=f"x={value}")
                self.assertEqual(status, 400)
                self.assertEqual(payload["status"], "error")

    def test_predict_accepts_a_number(self):
        status, payload = self._call("GET", "/api/ml/predict", query="x=2")
        self.assertEqual(status, 200)
        self.assertEqual(payload["x"], 2.0)


if __name__ == "__main__":
    unittest.main()

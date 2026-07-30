"""NDJSON log helpers."""

import json
import threading
from collections.abc import Mapping
from pathlib import Path

DEFAULT_LOG_PATH = "logs/wsbuilder.ndjson"
_APPEND_LOCK = threading.RLock()


def _normalize_record(record):
    if isinstance(record, Mapping):
        return dict(record)
    raise TypeError("NDJSON log records must be mappings")


def append_ndjson(path, record, *, ensure_parent=True):
    target = Path(path)
    payload = _normalize_record(record)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _APPEND_LOCK:
        if ensure_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    return payload


class NDJSONLog:
    def __init__(self, path=DEFAULT_LOG_PATH, *, ensure_parent=True):
        self.path = Path(path)
        self.ensure_parent = bool(ensure_parent)

    def append(self, record):
        return append_ndjson(self.path, record, ensure_parent=self.ensure_parent)

    def event(self, name, **fields):
        payload = {"event": str(name)}
        payload.update(fields)
        return self.append(payload)

    def describe(self):
        return {
            "path": str(self.path),
            "ensure_parent": self.ensure_parent,
        }

    def close(self):
        return None

    def __call__(self, record):
        return self.append(record)


def install_logs(app, path=DEFAULT_LOG_PATH, attr_name="logs", ensure_parent=True):
    logger = NDJSONLog(path=path, ensure_parent=ensure_parent)
    setattr(app, str(attr_name or "logs"), logger)
    return logger


__all__ = [
    "DEFAULT_LOG_PATH",
    "NDJSONLog",
    "append_ndjson",
    "install_logs",
]

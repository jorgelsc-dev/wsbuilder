"""Framework logging: NDJSON files plus a console fallback.

Every framework message goes through ``get_log(app)``: the app's ``logs``
writer when ``app.enable_logs()`` was called, otherwise the stderr console.
"""

import json
import sys
import threading
import time
import traceback
from collections.abc import Mapping
from pathlib import Path

DEFAULT_LOG_PATH = "logs/wsbuilder.ndjson"
_APPEND_LOCK = threading.RLock()
_CONSOLE_LOCK = threading.Lock()

LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}


def _level_number(level):
    if level is None:
        return None
    name = str(level).lower()
    if name not in LEVELS:
        raise ValueError(f"unknown log level {level!r}; expected one of {sorted(LEVELS)}")
    return LEVELS[name]


def _error_fields(exc):
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(exc)),
    }


def build_record(level, event, message="", *, exc=None, **fields):
    """The dict both writers log: level, event, message, extra fields, error."""
    _level_number(level)
    record = {
        "ts": time.time(),
        "level": str(level).lower(),
        "event": str(event),
        "message": str(message),
    }
    record.update(fields)
    if exc is not None:
        record["error"] = _error_fields(exc)
    return record


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


class _LevelMethods:
    def debug(self, event, message="", **fields):
        return self.log("debug", event, message, **fields)

    def info(self, event, message="", **fields):
        return self.log("info", event, message, **fields)

    def warning(self, event, message="", **fields):
        return self.log("warning", event, message, **fields)

    def error(self, event, message="", **fields):
        return self.log("error", event, message, **fields)

    def exception(self, event, message, exc, **fields):
        """Log ``exc`` at error level, traceback included."""
        return self.log("error", event, message, exc=exc, **fields)


class ConsoleLog(_LevelMethods):
    """Human-readable lines on stderr, for apps without ``enable_logs()``.

    Error records carry their traceback; lower levels print one line.
    """

    def __init__(self, level="info", stream=None):
        self.level = level
        self.stream = stream

    @property
    def level(self):
        return self._level

    @level.setter
    def level(self, value):
        _level_number(value)
        self._level = value

    def enabled_for(self, level):
        threshold = _level_number(self._level)
        return threshold is not None and _level_number(level) >= threshold

    def write(self, record):
        if not self.enabled_for(record["level"]):
            return
        line = f"[{record['event']}] {record['message']}\n"
        error = record.get("error")
        if error and record["level"] == "error":
            line += error["traceback"]
        # Resolved per call so redirected or replaced stderr is honoured.
        stream = self.stream if self.stream is not None else sys.stderr
        with _CONSOLE_LOCK:
            stream.write(line)
            stream.flush()

    def log(self, level, event, message="", *, exc=None, **fields):
        record = build_record(level, event, message, exc=exc, **fields)
        self.write(record)
        return record


#: Shared console used when an app has no ``logs`` writer of its own.
console = ConsoleLog()


class NDJSONLog(_LevelMethods):
    """Append records to an NDJSON file.

    Records at ``console_level`` or above are echoed to ``console`` too, so
    errors stay visible while they are stored; ``None`` keeps them file-only.
    """

    def __init__(self, path=DEFAULT_LOG_PATH, *, ensure_parent=True, console_level="warning"):
        self.path = Path(path)
        self.ensure_parent = bool(ensure_parent)
        _level_number(console_level)
        self.console_level = console_level

    def log(self, level, event, message="", *, exc=None, **fields):
        record = build_record(level, event, message, exc=exc, **fields)
        self.append(record)
        threshold = _level_number(self.console_level)
        if threshold is not None and LEVELS[record["level"]] >= threshold:
            console.write(record)
        return record

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
            "console_level": self.console_level,
        }

    def close(self):
        return None

    def __call__(self, record):
        return self.append(record)


def install_logs(
    app, path=DEFAULT_LOG_PATH, attr_name="logs", ensure_parent=True, console_level="warning"
):
    logger = NDJSONLog(path=path, ensure_parent=ensure_parent, console_level=console_level)
    setattr(app, str(attr_name or "logs"), logger)
    return logger


def get_log(app=None):
    """The writer framework messages for ``app`` go to.

    ``app.logs`` when it can take leveled records, else the shared console.
    """
    logs = getattr(app, "logs", None) if app is not None else None
    if logs is not None and callable(getattr(logs, "log", None)):
        return logs
    return console


__all__ = [
    "DEFAULT_LOG_PATH",
    "LEVELS",
    "ConsoleLog",
    "NDJSONLog",
    "append_ndjson",
    "build_record",
    "console",
    "get_log",
    "install_logs",
]

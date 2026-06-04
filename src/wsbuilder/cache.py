"""In-memory SQLite3 cache engine with TTL, tags and runtime stats."""

from __future__ import annotations

import json
import pickle
import sqlite3
import threading
import time
from collections.abc import Mapping

DEFAULT_NAMESPACE = "default"
DEFAULT_CLEANUP_INTERVAL_SECONDS = 30.0


def _safe_float(value, default):
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


class _ValueCodec:
    def __init__(self, *, allow_pickle=False):
        self.allow_pickle = bool(allow_pickle)

    def encode(self, value):
        if value is None:
            return "none", b""
        if isinstance(value, bool):
            return "bool", b"1" if value else b"0"
        if isinstance(value, int):
            return "int", str(value).encode("utf-8")
        if isinstance(value, float):
            return "float", repr(value).encode("utf-8")
        if isinstance(value, str):
            return "str", value.encode("utf-8")
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "bytes", bytes(value)
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            return "json", payload
        except Exception:
            if not self.allow_pickle:
                raise TypeError(
                    "Unsupported cache value type. Use JSON-compatible values, bytes, text, "
                    "numbers, booleans or enable allow_pickle=True."
                )
            return "pickle", pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

    def decode(self, value_type, payload):
        kind = str(value_type or "")
        raw = bytes(payload or b"")
        if kind == "none":
            return None
        if kind == "bool":
            return raw == b"1"
        if kind == "int":
            return int(raw.decode("utf-8"))
        if kind == "float":
            return float(raw.decode("utf-8"))
        if kind == "str":
            return raw.decode("utf-8", errors="ignore")
        if kind == "bytes":
            return raw
        if kind == "json":
            return json.loads(raw.decode("utf-8"))
        if kind == "pickle":
            if not self.allow_pickle:
                raise ValueError("Value was serialized with pickle but allow_pickle=False")
            return pickle.loads(raw)
        raise ValueError(f"Unknown cache value type: {kind}")


class SQLiteMemoryCache:
    """Thread-safe in-memory cache backed by sqlite3."""

    def __init__(
        self,
        *,
        default_namespace=DEFAULT_NAMESPACE,
        default_ttl=None,
        cleanup_interval_seconds=DEFAULT_CLEANUP_INTERVAL_SECONDS,
        max_entries=0,
        max_bytes=0,
        timeout=5.0,
        allow_pickle=False,
        pragmas=None,
    ):
        self.default_namespace = self._normalize_namespace(default_namespace)
        self.default_ttl = None if default_ttl is None else max(0.0, _safe_float(default_ttl, 0.0))
        self.cleanup_interval_seconds = max(
            0.0,
            _safe_float(cleanup_interval_seconds, DEFAULT_CLEANUP_INTERVAL_SECONDS),
        )
        self.max_entries = max(0, _safe_int(max_entries, 0))
        self.max_bytes = max(0, _safe_int(max_bytes, 0))
        self._codec = _ValueCodec(allow_pickle=allow_pickle)

        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_cleanup_at = 0.0
        self._closed = False
        self._stats = {
            "sets": 0,
            "gets": 0,
            "hits": 0,
            "misses": 0,
            "deletes": 0,
            "expired": 0,
            "evictions": 0,
            "increments": 0,
            "errors": 0,
        }

        self._conn = sqlite3.connect(
            ":memory:",
            check_same_thread=False,
            timeout=max(0.1, _safe_float(timeout, 5.0)),
        )
        self._conn.row_factory = sqlite3.Row
        self._install_pragmas(pragmas=pragmas, timeout=timeout)
        self._create_schema()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _install_pragmas(self, *, pragmas=None, timeout=5.0):
        cfg = {
            "foreign_keys": 1,
            "journal_mode": "MEMORY",
            "synchronous": "NORMAL",
            "temp_store": "MEMORY",
            "busy_timeout": max(100, int(max(0.1, _safe_float(timeout, 5.0)) * 1000)),
        }
        if isinstance(pragmas, Mapping):
            cfg.update(pragmas)
        cur = self._conn.cursor()
        for key, value in cfg.items():
            cur.execute(f"PRAGMA {key}={value}")
        cur.close()

    def _create_schema(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_blob BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NULL,
                    hits INTEGER NOT NULL DEFAULT 0,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cache_entries_expires_at
                ON cache_entries (expires_at)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cache_entries_updated_at
                ON cache_entries (updated_at)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_tags (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (namespace, key, tag),
                    FOREIGN KEY (namespace, key)
                        REFERENCES cache_entries (namespace, key)
                        ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cache_tags_tag
                ON cache_tags (namespace, tag)
                """
            )
            self._conn.commit()
            cur.close()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def _require_open(self):
        if self._closed:
            raise RuntimeError("Cache is closed")

    def _inc_stat(self, name, step=1):
        self._stats[name] = self._stats.get(name, 0) + int(step)

    def _normalize_namespace(self, namespace):
        text = str(namespace or "").strip()
        if not text:
            return DEFAULT_NAMESPACE
        return text

    def _normalize_key(self, key):
        text = str(key if key is not None else "").strip()
        if not text:
            raise ValueError("Cache key must be a non-empty string")
        return text

    def _normalize_tags(self, tags):
        if tags is None:
            return []
        if isinstance(tags, str):
            rows = [tags]
        else:
            rows = list(tags)
        normalized = []
        seen = set()
        for tag in rows:
            text = str(tag or "").strip()
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized

    def _resolve_expires_at(self, ttl, now):
        resolved = ttl
        if resolved is None:
            resolved = self.default_ttl
        if resolved is None:
            return None
        seconds = _safe_float(resolved, 0.0)
        if seconds <= 0.0:
            return now
        return now + seconds

    def _maybe_cleanup_locked(self):
        if self.cleanup_interval_seconds <= 0:
            return
        now = time.time()
        if (now - self._last_cleanup_at) < self.cleanup_interval_seconds:
            return
        self._cleanup_expired_locked(namespace=None, now=now)
        self._last_cleanup_at = now

    def _cleanup_expired_locked(self, namespace=None, now=None):
        ns = None if namespace is None else self._normalize_namespace(namespace)
        now_ts = now if now is not None else time.time()
        cur = self._conn.cursor()
        if ns is None:
            cur.execute("DELETE FROM cache_entries WHERE expires_at IS NOT NULL AND expires_at <= ?", (now_ts,))
        else:
            cur.execute(
                """
                DELETE FROM cache_entries
                WHERE namespace = ?
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                """,
                (ns, now_ts),
            )
        removed = int(cur.rowcount or 0)
        if removed > 0:
            self._inc_stat("expired", removed)
            self._inc_stat("deletes", removed)
            self._conn.commit()
        cur.close()
        return removed

    def cleanup(self, namespace=None):
        with self._lock:
            self._require_open()
            removed = self._cleanup_expired_locked(namespace=namespace, now=time.time())
            self._last_cleanup_at = time.time()
            return removed

    def _evict_if_needed_locked(self):
        if self.max_entries <= 0 and self.max_bytes <= 0:
            return 0
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS total_entries, COALESCE(SUM(size_bytes), 0) AS total_bytes
            FROM cache_entries
            """
        )
        row = cur.fetchone()
        total_entries = int(row["total_entries"] or 0)
        total_bytes = int(row["total_bytes"] or 0)

        over_entries = self.max_entries > 0 and total_entries > self.max_entries
        over_bytes = self.max_bytes > 0 and total_bytes > self.max_bytes
        if not over_entries and not over_bytes:
            cur.close()
            return 0

        removed = 0
        while True:
            over_entries = self.max_entries > 0 and total_entries > self.max_entries
            over_bytes = self.max_bytes > 0 and total_bytes > self.max_bytes
            if not over_entries and not over_bytes:
                break
            cur.execute(
                """
                SELECT namespace, key, size_bytes
                FROM cache_entries
                ORDER BY updated_at ASC, hits ASC
                LIMIT 1
                """
            )
            victim = cur.fetchone()
            if not victim:
                break
            cur.execute(
                "DELETE FROM cache_entries WHERE namespace = ? AND key = ?",
                (victim["namespace"], victim["key"]),
            )
            deleted = int(cur.rowcount or 0)
            if deleted <= 0:
                break
            removed += deleted
            total_entries -= deleted
            total_bytes = max(0, total_bytes - int(victim["size_bytes"] or 0))

        if removed > 0:
            self._inc_stat("evictions", removed)
            self._inc_stat("deletes", removed)
            self._conn.commit()
        cur.close()
        return removed

    def set(
        self,
        key,
        value,
        *,
        ttl=None,
        namespace=None,
        tags=None,
        only_if_absent=False,
        only_if_present=False,
    ):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        now = time.time()
        expires_at = self._resolve_expires_at(ttl, now)
        if expires_at is not None and expires_at <= now:
            self.delete(cache_key, namespace=ns)
            return False

        value_type, blob = self._codec.encode(value)
        size_bytes = len(blob)
        tag_rows = self._normalize_tags(tags)

        with self._lock:
            self._require_open()
            self._maybe_cleanup_locked()
            cur = self._conn.cursor()
            cur.execute(
                "SELECT created_at, hits FROM cache_entries WHERE namespace = ? AND key = ?",
                (ns, cache_key),
            )
            existing = cur.fetchone()
            if only_if_absent and existing is not None:
                cur.close()
                return False
            if only_if_present and existing is None:
                cur.close()
                return False

            created_at = now if existing is None else float(existing["created_at"])
            hits = 0 if existing is None else int(existing["hits"] or 0)
            cur.execute(
                """
                INSERT INTO cache_entries (
                    namespace, key, value_type, value_blob, created_at, updated_at, expires_at, hits, size_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value_type = excluded.value_type,
                    value_blob = excluded.value_blob,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at,
                    hits = excluded.hits,
                    size_bytes = excluded.size_bytes
                """,
                (
                    ns,
                    cache_key,
                    value_type,
                    sqlite3.Binary(blob),
                    created_at,
                    now,
                    expires_at,
                    hits,
                    size_bytes,
                ),
            )
            if tags is not None:
                cur.execute(
                    "DELETE FROM cache_tags WHERE namespace = ? AND key = ?",
                    (ns, cache_key),
                )
                if tag_rows:
                    cur.executemany(
                        "INSERT OR IGNORE INTO cache_tags (namespace, key, tag) VALUES (?, ?, ?)",
                        [(ns, cache_key, tag) for tag in tag_rows],
                    )

            self._conn.commit()
            cur.close()
            self._inc_stat("sets")
            self._evict_if_needed_locked()
            return True

    def add(self, key, value, *, ttl=None, namespace=None, tags=None):
        return self.set(
            key,
            value,
            ttl=ttl,
            namespace=namespace,
            tags=tags,
            only_if_absent=True,
        )

    def replace(self, key, value, *, ttl=None, namespace=None, tags=None):
        return self.set(
            key,
            value,
            ttl=ttl,
            namespace=namespace,
            tags=tags,
            only_if_present=True,
        )

    def _fetch_row_locked(self, namespace, cache_key):
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT namespace, key, value_type, value_blob, created_at, updated_at, expires_at, hits, size_bytes
            FROM cache_entries
            WHERE namespace = ? AND key = ?
            """,
            (namespace, cache_key),
        )
        row = cur.fetchone()
        cur.close()
        return row

    def _delete_row_locked(self, namespace, cache_key):
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM cache_entries WHERE namespace = ? AND key = ?",
            (namespace, cache_key),
        )
        removed = int(cur.rowcount or 0)
        cur.close()
        if removed > 0:
            self._conn.commit()
            self._inc_stat("deletes", removed)
        return removed

    def get(self, key, default=None, *, namespace=None, touch=False, ttl=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        now = time.time()

        with self._lock:
            self._require_open()
            self._maybe_cleanup_locked()
            self._inc_stat("gets")
            row = self._fetch_row_locked(ns, cache_key)
            if row is None:
                self._inc_stat("misses")
                return default

            expires_at = row["expires_at"]
            if expires_at is not None and float(expires_at) <= now:
                self._delete_row_locked(ns, cache_key)
                self._inc_stat("expired")
                self._inc_stat("misses")
                return default

            try:
                value = self._codec.decode(row["value_type"], row["value_blob"])
            except Exception:
                self._inc_stat("errors")
                self._inc_stat("misses")
                return default

            hits = int(row["hits"] or 0) + 1
            next_expires_at = expires_at
            if touch:
                if ttl is not None:
                    next_expires_at = self._resolve_expires_at(ttl, now)
                elif self.default_ttl is not None:
                    next_expires_at = now + self.default_ttl

            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE cache_entries
                SET hits = ?, updated_at = ?, expires_at = ?
                WHERE namespace = ? AND key = ?
                """,
                (hits, now, next_expires_at, ns, cache_key),
            )
            self._conn.commit()
            cur.close()

            self._inc_stat("hits")
            return value

    def get_with_meta(self, key, default=None, *, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        now = time.time()

        with self._lock:
            self._require_open()
            row = self._fetch_row_locked(ns, cache_key)
            if row is None:
                return default, None
            expires_at = row["expires_at"]
            if expires_at is not None and float(expires_at) <= now:
                self._delete_row_locked(ns, cache_key)
                self._inc_stat("expired")
                return default, None
            value = self._codec.decode(row["value_type"], row["value_blob"])
            tags = self.get_tags(cache_key, namespace=ns)
            meta = {
                "namespace": ns,
                "key": cache_key,
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
                "expires_at": None if row["expires_at"] is None else float(row["expires_at"]),
                "hits": int(row["hits"] or 0),
                "size_bytes": int(row["size_bytes"] or 0),
                "value_type": str(row["value_type"] or ""),
                "tags": tags,
            }
            return value, meta

    def get_many(self, keys, default=None, *, namespace=None):
        result = {}
        for key in keys or ():
            result[str(key)] = self.get(key, default=default, namespace=namespace)
        return result

    def mget(self, keys, default=None, *, namespace=None):
        return self.get_many(keys, default=default, namespace=namespace)

    def set_many(self, mapping, *, ttl=None, namespace=None, tags=None):
        if not isinstance(mapping, Mapping):
            raise TypeError("mapping must be a mapping")
        written = 0
        for key, value in mapping.items():
            per_key_tags = tags.get(key) if isinstance(tags, Mapping) else tags
            ok = self.set(
                key,
                value,
                ttl=ttl,
                namespace=namespace,
                tags=per_key_tags,
            )
            if ok:
                written += 1
        return written

    def mset(self, mapping, *, ttl=None, namespace=None, tags=None):
        return self.set_many(mapping, ttl=ttl, namespace=namespace, tags=tags)

    def has(self, key, *, namespace=None):
        sentinel = object()
        return self.get(key, default=sentinel, namespace=namespace) is not sentinel

    def exists(self, key, *, namespace=None):
        return self.has(key, namespace=namespace)

    def delete(self, key, *, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        with self._lock:
            self._require_open()
            removed = self._delete_row_locked(ns, cache_key)
            return bool(removed)

    def delete_many(self, keys, *, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        normalized = [self._normalize_key(key) for key in (keys or ())]
        if not normalized:
            return 0
        placeholders = ", ".join(["?"] * len(normalized))
        params = [ns, *normalized]
        with self._lock:
            self._require_open()
            cur = self._conn.cursor()
            cur.execute(
                f"DELETE FROM cache_entries WHERE namespace = ? AND key IN ({placeholders})",
                tuple(params),
            )
            removed = int(cur.rowcount or 0)
            cur.close()
            if removed > 0:
                self._conn.commit()
                self._inc_stat("deletes", removed)
            return removed

    def pop(self, key, default=None, *, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        with self._lock:
            self._require_open()
            row = self._fetch_row_locked(ns, cache_key)
            if row is None:
                return default
            expires_at = row["expires_at"]
            if expires_at is not None and float(expires_at) <= time.time():
                self._delete_row_locked(ns, cache_key)
                self._inc_stat("expired")
                return default
            value = self._codec.decode(row["value_type"], row["value_blob"])
            self._delete_row_locked(ns, cache_key)
            return value

    def clear(self, *, namespace=None):
        with self._lock:
            self._require_open()
            cur = self._conn.cursor()
            if namespace is None:
                cur.execute("DELETE FROM cache_entries")
            else:
                ns = self._normalize_namespace(namespace)
                cur.execute("DELETE FROM cache_entries WHERE namespace = ?", (ns,))
            removed = int(cur.rowcount or 0)
            cur.close()
            if removed > 0:
                self._conn.commit()
                self._inc_stat("deletes", removed)
            return removed

    def expire(self, key, ttl, *, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        now = time.time()
        expires_at = self._resolve_expires_at(ttl, now)
        with self._lock:
            self._require_open()
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE cache_entries
                SET expires_at = ?, updated_at = ?
                WHERE namespace = ? AND key = ?
                """,
                (expires_at, now, ns, cache_key),
            )
            changed = int(cur.rowcount or 0)
            cur.close()
            if changed > 0:
                self._conn.commit()
            return bool(changed)

    def touch(self, key, *, ttl=None, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        now = time.time()
        with self._lock:
            self._require_open()
            row = self._fetch_row_locked(ns, cache_key)
            if row is None:
                return False
            expires_at = row["expires_at"]
            if expires_at is not None and float(expires_at) <= now:
                self._delete_row_locked(ns, cache_key)
                self._inc_stat("expired")
                return False
            next_expires = expires_at
            if ttl is not None:
                next_expires = self._resolve_expires_at(ttl, now)
            elif self.default_ttl is not None:
                next_expires = now + self.default_ttl
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE cache_entries
                SET updated_at = ?, expires_at = ?
                WHERE namespace = ? AND key = ?
                """,
                (now, next_expires, ns, cache_key),
            )
            changed = int(cur.rowcount or 0)
            cur.close()
            if changed > 0:
                self._conn.commit()
            return bool(changed)

    def ttl(self, key, *, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        now = time.time()
        with self._lock:
            self._require_open()
            row = self._fetch_row_locked(ns, cache_key)
            if row is None:
                return -2
            expires_at = row["expires_at"]
            if expires_at is None:
                return -1
            remaining = float(expires_at) - now
            if remaining <= 0:
                self._delete_row_locked(ns, cache_key)
                self._inc_stat("expired")
                return -2
            return round(remaining, 3)

    def incr(self, key, amount=1, *, initial=0, ttl=None, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        delta = amount
        if not isinstance(delta, (int, float)):
            raise TypeError("amount must be int or float")
        now = time.time()
        with self._lock:
            self._require_open()
            self._maybe_cleanup_locked()
            row = self._fetch_row_locked(ns, cache_key)
            if row is None:
                current = initial
                if not isinstance(current, (int, float)):
                    raise TypeError("initial must be int or float")
                current = current + delta
                expires_at = self._resolve_expires_at(ttl, now)
                hits = 0
                created_at = now
            else:
                expires_at_row = row["expires_at"]
                if expires_at_row is not None and float(expires_at_row) <= now:
                    self._delete_row_locked(ns, cache_key)
                    self._inc_stat("expired")
                    current = initial + delta
                    expires_at = self._resolve_expires_at(ttl, now)
                    hits = 0
                    created_at = now
                else:
                    old_value = self._codec.decode(row["value_type"], row["value_blob"])
                    if isinstance(old_value, bool) or not isinstance(old_value, (int, float)):
                        raise TypeError("Cannot increment a non-numeric cache value")
                    current = old_value + delta
                    hits = int(row["hits"] or 0)
                    created_at = float(row["created_at"])
                    if ttl is None:
                        expires_at = row["expires_at"]
                    else:
                        expires_at = self._resolve_expires_at(ttl, now)

            value_type, blob = self._codec.encode(current)
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO cache_entries (
                    namespace, key, value_type, value_blob, created_at, updated_at, expires_at, hits, size_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value_type = excluded.value_type,
                    value_blob = excluded.value_blob,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at,
                    hits = excluded.hits,
                    size_bytes = excluded.size_bytes
                """,
                (
                    ns,
                    cache_key,
                    value_type,
                    sqlite3.Binary(blob),
                    created_at,
                    now,
                    expires_at,
                    hits,
                    len(blob),
                ),
            )
            self._conn.commit()
            cur.close()
            self._inc_stat("increments")
            self._inc_stat("sets")
            self._evict_if_needed_locked()
            return current

    def decr(self, key, amount=1, *, initial=0, ttl=None, namespace=None):
        return self.incr(
            key,
            amount=-amount,
            initial=initial,
            ttl=ttl,
            namespace=namespace,
        )

    def keys(self, *, namespace=None, prefix=None, limit=None, include_expired=False):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        sql = "SELECT key, expires_at FROM cache_entries WHERE namespace = ?"
        params = [ns]
        if prefix:
            sql += " AND key LIKE ?"
            params.append(f"{str(prefix)}%")
        if not include_expired:
            sql += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(time.time())
        sql += " ORDER BY key ASC"
        lim = _safe_int(limit, None)
        if lim is not None and lim > 0:
            sql += " LIMIT ?"
            params.append(lim)
        with self._lock:
            self._require_open()
            cur = self._conn.cursor()
            cur.execute(sql, tuple(params))
            rows = [str(row["key"]) for row in cur.fetchall()]
            cur.close()
            return rows

    def tag(self, key, tags, *, namespace=None, replace=False):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        tag_rows = self._normalize_tags(tags)
        if not tag_rows:
            return 0
        with self._lock:
            self._require_open()
            row = self._fetch_row_locked(ns, cache_key)
            if row is None:
                return 0
            if row["expires_at"] is not None and float(row["expires_at"]) <= time.time():
                self._delete_row_locked(ns, cache_key)
                self._inc_stat("expired")
                return 0
            cur = self._conn.cursor()
            if replace:
                cur.execute("DELETE FROM cache_tags WHERE namespace = ? AND key = ?", (ns, cache_key))
            cur.executemany(
                "INSERT OR IGNORE INTO cache_tags (namespace, key, tag) VALUES (?, ?, ?)",
                [(ns, cache_key, tag) for tag in tag_rows],
            )
            changed = int(cur.rowcount or 0)
            cur.close()
            self._conn.commit()
            return changed

    def untag(self, key, tags=None, *, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        with self._lock:
            self._require_open()
            cur = self._conn.cursor()
            if tags is None:
                cur.execute(
                    "DELETE FROM cache_tags WHERE namespace = ? AND key = ?",
                    (ns, cache_key),
                )
            else:
                tag_rows = self._normalize_tags(tags)
                if not tag_rows:
                    cur.close()
                    return 0
                placeholders = ", ".join(["?"] * len(tag_rows))
                cur.execute(
                    f"""
                    DELETE FROM cache_tags
                    WHERE namespace = ? AND key = ? AND tag IN ({placeholders})
                    """,
                    (ns, cache_key, *tag_rows),
                )
            removed = int(cur.rowcount or 0)
            cur.close()
            if removed > 0:
                self._conn.commit()
            return removed

    def get_tags(self, key, *, namespace=None):
        ns = self._normalize_namespace(namespace or self.default_namespace)
        cache_key = self._normalize_key(key)
        with self._lock:
            self._require_open()
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT tag
                FROM cache_tags
                WHERE namespace = ? AND key = ?
                ORDER BY tag ASC
                """,
                (ns, cache_key),
            )
            rows = [str(row["tag"]) for row in cur.fetchall()]
            cur.close()
            return rows

    def invalidate_tag(self, tag, *, namespace=None):
        tags = self._normalize_tags([tag])
        if not tags:
            return 0
        return self.invalidate_tags(tags, namespace=namespace)

    def invalidate_tags(self, tags, *, namespace=None):
        tag_rows = self._normalize_tags(tags)
        if not tag_rows:
            return 0
        with self._lock:
            self._require_open()
            placeholders = ", ".join(["?"] * len(tag_rows))
            cur = self._conn.cursor()
            if namespace is None:
                cur.execute(
                    f"""
                    DELETE FROM cache_entries
                    WHERE (namespace, key) IN (
                        SELECT namespace, key
                        FROM cache_tags
                        WHERE tag IN ({placeholders})
                    )
                    """,
                    tuple(tag_rows),
                )
            else:
                ns = self._normalize_namespace(namespace)
                cur.execute(
                    f"""
                    DELETE FROM cache_entries
                    WHERE namespace = ?
                      AND key IN (
                        SELECT key
                        FROM cache_tags
                        WHERE namespace = ?
                          AND tag IN ({placeholders})
                    )
                    """,
                    (ns, ns, *tag_rows),
                )
            removed = int(cur.rowcount or 0)
            cur.close()
            if removed > 0:
                self._conn.commit()
                self._inc_stat("deletes", removed)
            return removed

    def count(self, *, namespace=None):
        with self._lock:
            self._require_open()
            self._cleanup_expired_locked(namespace=namespace, now=time.time())
            cur = self._conn.cursor()
            if namespace is None:
                cur.execute("SELECT COUNT(*) AS total FROM cache_entries")
            else:
                ns = self._normalize_namespace(namespace)
                cur.execute("SELECT COUNT(*) AS total FROM cache_entries WHERE namespace = ?", (ns,))
            row = cur.fetchone()
            cur.close()
            return int(row["total"] or 0)

    def size_bytes(self, *, namespace=None):
        with self._lock:
            self._require_open()
            self._cleanup_expired_locked(namespace=namespace, now=time.time())
            cur = self._conn.cursor()
            if namespace is None:
                cur.execute("SELECT COALESCE(SUM(size_bytes), 0) AS total FROM cache_entries")
            else:
                ns = self._normalize_namespace(namespace)
                cur.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM cache_entries WHERE namespace = ?",
                    (ns,),
                )
            row = cur.fetchone()
            cur.close()
            return int(row["total"] or 0)

    def reset_stats(self):
        with self._lock:
            self._stats = {
                "sets": 0,
                "gets": 0,
                "hits": 0,
                "misses": 0,
                "deletes": 0,
                "expired": 0,
                "evictions": 0,
                "increments": 0,
                "errors": 0,
            }

    def stats(self):
        with self._lock:
            self._require_open()
            now = time.time()
            self._cleanup_expired_locked(namespace=None, now=now)
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS entries_total, COALESCE(SUM(size_bytes), 0) AS bytes_total
                FROM cache_entries
                """
            )
            storage = cur.fetchone()
            cur.execute(
                """
                SELECT namespace, COUNT(*) AS entries, COALESCE(SUM(size_bytes), 0) AS bytes
                FROM cache_entries
                GROUP BY namespace
                ORDER BY entries DESC, namespace ASC
                """
            )
            by_namespace = []
            for row in cur.fetchall():
                by_namespace.append(
                    {
                        "namespace": str(row["namespace"]),
                        "entries": int(row["entries"] or 0),
                        "bytes": int(row["bytes"] or 0),
                    }
                )
            cur.execute("SELECT COUNT(*) AS total FROM cache_tags")
            tags_total = int((cur.fetchone() or {"total": 0})["total"] or 0)
            cur.close()
            gets = int(self._stats["gets"])
            hits = int(self._stats["hits"])
            hit_ratio = 0.0 if gets <= 0 else round(hits / float(gets), 6)
            return {
                "engine": "sqlite3-memory",
                "uptime_seconds": round(now - self._started_at, 3),
                "config": {
                    "default_namespace": self.default_namespace,
                    "default_ttl": self.default_ttl,
                    "cleanup_interval_seconds": self.cleanup_interval_seconds,
                    "max_entries": self.max_entries,
                    "max_bytes": self.max_bytes,
                    "allow_pickle": bool(self._codec.allow_pickle),
                },
                "counters": dict(self._stats),
                "ratios": {"hit_ratio": hit_ratio},
                "storage": {
                    "entries_total": int(storage["entries_total"] or 0),
                    "bytes_total": int(storage["bytes_total"] or 0),
                    "namespaces_total": len(by_namespace),
                    "tags_total": tags_total,
                    "by_namespace": by_namespace,
                },
            }

    def metrics_snapshot(self):
        return {"cache": self.stats()}


def install_cache(app, cache=None, attr_name="cache"):
    resolved = cache or SQLiteMemoryCache()
    setattr(app, str(attr_name or "cache"), resolved)
    return resolved


Cache = SQLiteMemoryCache

__all__ = [
    "Cache",
    "DEFAULT_CLEANUP_INTERVAL_SECONDS",
    "DEFAULT_NAMESPACE",
    "SQLiteMemoryCache",
    "install_cache",
]

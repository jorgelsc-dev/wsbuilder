"""HTTP response cache for view routes using in-memory SQLite storage."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .cache import SQLiteMemoryCache
from .http import Response

DEFAULT_VIEW_CACHE_NAMESPACE = "http-view-cache"
DEFAULT_CACHE_METHODS = ("GET", "HEAD")

_THREAD_HEADERS = {
    "wsbuilder-thread",
    "wsbuilder-thread-host",
    "wsbuilder-thread-port",
    "wsbuilder-thread-mode",
}
_DROP_HEADERS = {
    "connection",
    "content-length",
    "set-cookie",
    "transfer-encoding",
}


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


def _normalize_mimetype(content_type):
    text = str(content_type or "").strip().lower()
    if not text:
        return ""
    return text.split(";", 1)[0].strip()


def _normalize_methods(methods):
    if methods is None:
        rows = DEFAULT_CACHE_METHODS
    elif isinstance(methods, str):
        rows = [methods]
    else:
        rows = list(methods)
    result = []
    seen = set()
    for method in rows:
        text = str(method or "").strip().upper()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    if not result:
        return tuple(DEFAULT_CACHE_METHODS)
    return tuple(result)


def _strip_uncacheable_headers(headers):
    clean = {}
    for key, value in (headers or {}).items():
        k = str(key or "")
        low = k.lower()
        if low in _DROP_HEADERS or low in _THREAD_HEADERS:
            continue
        clean[k] = str(value)
    return clean


@dataclass(slots=True)
class GlobalCacheRule:
    ttl_seconds: float
    path_pattern: str = "*"
    mimetype_pattern: str = "*"
    methods: tuple[str, ...] = DEFAULT_CACHE_METHODS
    name: str = ""

    def matches(self, *, path, method, mimetype):
        method_text = str(method or "").upper()
        if self.methods and method_text not in self.methods:
            return False
        if not fnmatch.fnmatchcase(str(path or ""), self.path_pattern):
            return False
        mime = _normalize_mimetype(mimetype or "")
        wanted = str(self.mimetype_pattern or "*").lower().strip() or "*"
        return fnmatch.fnmatchcase(mime or "", wanted)

    def describe(self):
        return {
            "name": self.name,
            "ttl_seconds": self.ttl_seconds,
            "path_pattern": self.path_pattern,
            "mimetype_pattern": self.mimetype_pattern,
            "methods": list(self.methods),
        }


class ViewResponseCache:
    def __init__(
        self,
        *,
        store=None,
        namespace=DEFAULT_VIEW_CACHE_NAMESPACE,
        default_ttl=None,
    ):
        self.namespace = str(namespace or DEFAULT_VIEW_CACHE_NAMESPACE).strip() or DEFAULT_VIEW_CACHE_NAMESPACE
        self.default_ttl = None if default_ttl is None else max(0.0, _safe_float(default_ttl, 0.0))
        self.store = store or SQLiteMemoryCache()
        self._owns_store = store is None
        self._lock = threading.RLock()
        self._rules = []
        self._stats = {
            "lookups": 0,
            "hits": 0,
            "misses": 0,
            "stores": 0,
            "skips": 0,
            "invalidations": 0,
            "errors": 0,
        }
        self._started_at = time.time()

    def close(self):
        if self._owns_store and hasattr(self.store, "close"):
            self.store.close()

    def _inc_stat(self, key, step=1):
        self._stats[key] = self._stats.get(key, 0) + int(step)

    def add_global_rule(
        self,
        *,
        ttl_seconds,
        path_pattern="*",
        mimetype_pattern="*",
        methods=None,
        name="",
    ):
        ttl = max(0.0, _safe_float(ttl_seconds, 0.0))
        if ttl <= 0:
            raise ValueError("ttl_seconds must be > 0")
        rule = GlobalCacheRule(
            ttl_seconds=ttl,
            path_pattern=str(path_pattern or "*"),
            mimetype_pattern=str(mimetype_pattern or "*"),
            methods=_normalize_methods(methods),
            name=str(name or "").strip(),
        )
        with self._lock:
            self._rules.append(rule)
        return rule

    def declare_global(self, ttl_seconds, *, path="*", mimetype="*", methods=None, name=""):
        return self.add_global_rule(
            ttl_seconds=ttl_seconds,
            path_pattern=path,
            mimetype_pattern=mimetype,
            methods=methods,
            name=name,
        )

    def set_global_wildcard(self, ttl_seconds, *, methods=None, name=""):
        return self.add_global_rule(
            ttl_seconds=ttl_seconds,
            path_pattern="*",
            mimetype_pattern="*",
            methods=methods,
            name=name,
        )

    def set_global_mimetype(self, mimetype, ttl_seconds, *, path="*", methods=None, name=""):
        return self.add_global_rule(
            ttl_seconds=ttl_seconds,
            path_pattern=path,
            mimetype_pattern=str(mimetype or "*"),
            methods=methods,
            name=name,
        )

    def clear_global_rules(self):
        with self._lock:
            self._rules = []

    def _global_rule_count(self):
        with self._lock:
            return len(self._rules)

    def _route_cache_config(self, route):
        raw = getattr(route, "cache_config", None)
        if raw is None:
            return None
        if raw is False:
            return {"enabled": False}
        if raw is True:
            return {"enabled": True}
        if isinstance(raw, (int, float)):
            ttl = max(0.0, _safe_float(raw, 0.0))
            if ttl <= 0:
                return {"enabled": False}
            return {"enabled": True, "ttl": ttl}
        if isinstance(raw, Mapping):
            cfg = dict(raw)
            enabled = cfg.get("enabled", True)
            cfg["enabled"] = bool(enabled)
            if "ttl" in cfg and cfg.get("ttl") is not None:
                cfg["ttl"] = max(0.0, _safe_float(cfg.get("ttl"), 0.0))
            return cfg
        return {"enabled": bool(raw)}

    def _route_allows_lookup(self, route, request):
        if getattr(route, "kind", "") != "plain":
            return False
        method = str(getattr(request, "method", "") or "").upper()
        if method not in DEFAULT_CACHE_METHODS:
            return False
        cfg = self._route_cache_config(route)
        if cfg is not None and cfg.get("enabled") is False:
            return False
        if cfg is not None and cfg.get("enabled") is True:
            return True
        if self.default_ttl is not None:
            return True
        return self._global_rule_count() > 0

    def _cache_key(self, request, route, cfg):
        method = str(getattr(request, "method", "") or "").upper()
        path = str(getattr(request, "path", "") or "")
        query_string = str(getattr(request, "query_string", "") or "")
        query = dict(getattr(request, "query", {}) or {})
        headers = dict(getattr(request, "headers", {}) or {})

        include_query = True
        vary_query = None
        vary_headers = None
        custom_key = None
        if isinstance(cfg, Mapping):
            include_query = bool(cfg.get("include_query", True))
            vary_query = cfg.get("vary_query")
            vary_headers = cfg.get("vary_headers")
            custom_key = cfg.get("key")

        key_data = {
            "method": method,
            "path": path,
            "route": getattr(route, "path", path),
        }
        if include_query:
            if vary_query:
                if isinstance(vary_query, str):
                    vary_query_names = [vary_query]
                else:
                    vary_query_names = list(vary_query)
                key_data["query"] = {
                    str(name): str(query.get(str(name), ""))
                    for name in sorted(str(x) for x in vary_query_names)
                }
            else:
                key_data["query_string"] = query_string

        if vary_headers:
            if isinstance(vary_headers, str):
                vary_header_names = [vary_headers]
            else:
                vary_header_names = list(vary_headers)
            key_data["headers"] = {
                str(name).lower(): str(headers.get(str(name).lower(), ""))
                for name in sorted(str(x) for x in vary_header_names)
            }

        if callable(custom_key):
            try:
                key_data["custom"] = str(custom_key(request))
            except Exception:
                key_data["custom"] = ""
        elif custom_key is not None:
            key_data["custom"] = str(custom_key)

        raw = json.dumps(key_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha1(raw).hexdigest()
        return f"resp:{digest}"

    def _resolve_ttl(self, route, request, response):
        cfg = self._route_cache_config(route)
        if cfg is not None and cfg.get("enabled") is False:
            return None
        if cfg is not None and cfg.get("ttl") is not None:
            ttl = max(0.0, _safe_float(cfg.get("ttl"), 0.0))
            return ttl if ttl > 0 else None

        mimetype = _normalize_mimetype((response.headers or {}).get("Content-Type", ""))
        method = str(getattr(request, "method", "") or "").upper()
        path = str(getattr(request, "path", "") or "")

        with self._lock:
            for rule in reversed(self._rules):
                if rule.matches(path=path, method=method, mimetype=mimetype):
                    return max(0.0, float(rule.ttl_seconds))

        if cfg is not None and cfg.get("enabled") is True and self.default_ttl is not None:
            return self.default_ttl
        if cfg is None and self.default_ttl is not None:
            return self.default_ttl
        return None

    def _cacheable_statuses(self, cfg):
        if isinstance(cfg, Mapping) and cfg.get("statuses") is not None:
            values = cfg.get("statuses")
            if isinstance(values, (str, bytes)):
                rows = [values]
            elif isinstance(values, Iterable):
                rows = list(values)
            else:
                rows = [values]
            result = set()
            for value in rows:
                parsed = _safe_int(value, None)
                if parsed is not None:
                    result.add(int(parsed))
            if result:
                return result
        return {200}

    def _is_cache_control_blocked(self, response):
        has_set_cookie = any(str(k).lower() == "set-cookie" for k in (response.headers or {}).keys())
        if has_set_cookie:
            return True
        low = str((response.headers or {}).get("Cache-Control", "")).lower()
        return "no-store" in low or "private" in low

    def fetch(self, request, route):
        if not self._route_allows_lookup(route, request):
            return None

        cfg = self._route_cache_config(route)
        key = self._cache_key(request, route, cfg)

        self._inc_stat("lookups")
        try:
            payload = self.store.get(key, namespace=self.namespace)
        except Exception:
            self._inc_stat("errors")
            self._inc_stat("misses")
            return None

        if not isinstance(payload, Mapping):
            self._inc_stat("misses")
            return None

        try:
            status = int(payload.get("status", 200))
            reason = str(payload.get("reason", "") or "")
            headers = {
                str(k): str(v)
                for k, v in dict(payload.get("headers", {}) or {}).items()
            }
            body = base64.b64decode(str(payload.get("body_b64", "")).encode("ascii"))
            cached_at = _safe_float(payload.get("cached_at"), 0.0)
            resp = Response(status=status, reason=reason or None, headers=headers, body=body)
            resp.headers["X-WSBuilder-Cache"] = "HIT"
            if cached_at > 0:
                age = max(0, int(time.time() - cached_at))
                resp.headers["Age"] = str(age)
            self._inc_stat("hits")
            return resp
        except Exception:
            self._inc_stat("errors")
            self._inc_stat("misses")
            return None

    def store_response(self, request, route, response):
        if getattr(route, "kind", "") != "plain":
            return False
        if str(getattr(request, "method", "") or "").upper() not in DEFAULT_CACHE_METHODS:
            self._inc_stat("skips")
            return False
        if getattr(response, "is_stream", False):
            self._inc_stat("skips")
            return False
        cfg = self._route_cache_config(route)
        if cfg is not None and cfg.get("enabled") is False:
            self._inc_stat("skips")
            return False
        if int(response.status) not in self._cacheable_statuses(cfg):
            self._inc_stat("skips")
            return False
        if self._is_cache_control_blocked(response):
            self._inc_stat("skips")
            return False

        ttl = self._resolve_ttl(route, request, response)
        if ttl is None or ttl <= 0:
            self._inc_stat("skips")
            return False

        cfg = self._route_cache_config(route)
        key = self._cache_key(request, route, cfg)
        clean_headers = _strip_uncacheable_headers(response.headers)
        record = {
            "status": int(response.status),
            "reason": str(response.reason or ""),
            "headers": clean_headers,
            "content_type": _normalize_mimetype(clean_headers.get("Content-Type", "")),
            "body_b64": base64.b64encode(bytes(response.body or b"")).decode("ascii"),
            "cached_at": time.time(),
        }
        tags = [
            f"path:{str(getattr(request, 'path', '') or '')}",
            f"route:{str(getattr(route, 'path', '') or '')}",
            f"mime:{record['content_type']}",
        ]
        try:
            ok = self.store.set(
                key,
                record,
                ttl=float(ttl),
                namespace=self.namespace,
                tags=tags,
            )
            if ok:
                self._inc_stat("stores")
            else:
                self._inc_stat("skips")
            return bool(ok)
        except Exception:
            self._inc_stat("errors")
            return False

    def invalidate_path(self, path):
        target = str(path or "").strip()
        if not target:
            return 0
        removed = self.store.invalidate_tag(f"path:{target}", namespace=self.namespace)
        self._inc_stat("invalidations", removed)
        return removed

    def clear(self):
        removed = int(self.store.clear(namespace=self.namespace))
        self._inc_stat("invalidations", removed)
        return removed

    def snapshot(self):
        try:
            storage = self.store.stats()
        except Exception:
            storage = {}
        with self._lock:
            rules = [rule.describe() for rule in self._rules]
            counters = dict(self._stats)
        return {
            "enabled": True,
            "engine": "sqlite3-memory",
            "namespace": self.namespace,
            "default_ttl": self.default_ttl,
            "uptime_seconds": round(time.time() - self._started_at, 3),
            "rules_total": len(rules),
            "rules": rules,
            "counters": counters,
            "storage": storage.get("storage", {}),
        }

    def metrics_snapshot(self):
        return {"http_cache": self.snapshot()}


def install_caches(app, caches=None, attr_name="caches"):
    resolved = caches or ViewResponseCache()
    setattr(app, str(attr_name or "caches"), resolved)
    return resolved


__all__ = [
    "DEFAULT_CACHE_METHODS",
    "DEFAULT_VIEW_CACHE_NAMESPACE",
    "GlobalCacheRule",
    "ViewResponseCache",
    "install_caches",
]

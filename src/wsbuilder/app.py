import json
import threading
import time
import uuid
from html import escape as html_escape

from .constants import DEFAULT_CORS_ALLOW_ORIGIN
from .cookies import build_set_cookie
from .http import Response
from .tasks import TaskManager
from .ws import sha1

THREAD_COOKIE_NAME = "wsbuilder-thread"
THREAD_RESPONSE_ID_HEADER = "WSBuilder-Thread"
THREAD_RESPONSE_HOST_HEADER = "WSBuilder-Thread-Host"
THREAD_RESPONSE_PORT_HEADER = "WSBuilder-Thread-Port"
THREAD_RESPONSE_MODE_HEADER = "WSBuilder-Thread-Mode"

DEFAULT_WORKER_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_PENDING_JOBS = 256
DEFAULT_AFFINITY_TTL_SECONDS = 900.0
DEFAULT_AFFINITY_MAX_ENTRIES = 10000


def _normalize_ws_protocols(value):
    if not value:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(str(part).strip() for part in value if str(part).strip())


def _constant_time_equals(a, b):
    left = str(a or "").encode("utf-8")
    right = str(b or "").encode("utf-8")
    max_len = max(len(left), len(right))
    diff = len(left) ^ len(right)
    for i in range(max_len):
        x = left[i] if i < len(left) else 0
        y = right[i] if i < len(right) else 0
        diff |= x ^ y
    return diff == 0


def _docs_sanitize(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, dict):
        return {str(key): _docs_sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_docs_sanitize(item) for item in value]
    return str(value)


class _RouteExecutionError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


class _RouteJob:
    def __init__(self, request):
        self.request = request
        self.done = threading.Event()
        self.result = None
        self.error = None


class _RouteThreadWorker:
    def __init__(self, route, index):
        self.route = route
        self.index = int(index)
        self.thread_id = str(uuid.uuid4())
        self.host = route.thread_host
        self.port = route.thread_base_port + self.index if route.thread_base_port > 0 else 0
        # Ports are metadata only for tracing/debugging. The main server socket handles HTTP I/O.
        self.listening = False
        self.listen_error = "disabled"

        self.worker_timeout_seconds = route.worker_timeout_seconds
        self.requests_per_thread = route.requests_per_thread
        # Backward-compatible alias used by existing callers/tests.
        self.max_pending_jobs = self.requests_per_thread

        self._jobs = []
        self._jobs_cond = threading.Condition()
        self._running = True
        self._active_jobs = 0
        self._served_jobs = 0
        self._thread = threading.Thread(
            target=self._run,
            name=f"wsbuilder-view-thread-{route.path}-{self.thread_id}",
            daemon=True,
        )
        self._thread.start()

    def _run(self):
        while True:
            with self._jobs_cond:
                while self._running and not self._jobs:
                    self._jobs_cond.wait()
                if not self._running and not self._jobs:
                    break
                job = self._jobs.pop(0)
                self._active_jobs += 1
            try:
                job.result = self.route.handler(job.request)
            except Exception as e:
                job.error = e
            finally:
                with self._jobs_cond:
                    if self._active_jobs > 0:
                        self._active_jobs -= 1
                    self._served_jobs += 1
                job.done.set()

    def submit(self, request):
        job = _RouteJob(request)
        with self._jobs_cond:
            if not self._running:
                raise _RouteExecutionError(503, "Route worker is closed")
            current_load = self._active_jobs + len(self._jobs)
            if self.requests_per_thread > 0 and current_load >= self.requests_per_thread:
                raise _RouteExecutionError(503, "Route worker capacity reached")
            self._jobs.append(job)
            self._jobs_cond.notify()

        timeout = self.worker_timeout_seconds
        ok = job.done.wait(None if timeout <= 0 else timeout)
        if not ok:
            raise _RouteExecutionError(504, "Route worker execution timeout")
        if job.error is not None:
            raise job.error
        return job.result

    def close(self):
        with self._jobs_cond:
            self._running = False
            self._jobs_cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def pending_jobs(self):
        with self._jobs_cond:
            return len(self._jobs)

    def active_jobs(self):
        with self._jobs_cond:
            return self._active_jobs

    def load(self):
        with self._jobs_cond:
            return self._active_jobs + len(self._jobs)

    def served_jobs(self):
        with self._jobs_cond:
            return self._served_jobs

    def describe(self):
        return {
            "id": self.thread_id,
            "host": self.host,
            "port": self.port,
            "listening": self.listening,
            "listen_error": self.listen_error,
            "pending_jobs": self.pending_jobs(),
            "active_jobs": self.active_jobs(),
            "current_load": self.load(),
            "served_jobs": self.served_jobs(),
            "requests_per_thread": self.requests_per_thread,
            "max_pending_jobs": self.max_pending_jobs,
            "worker_timeout_seconds": self.worker_timeout_seconds,
        }


class _RouteThreadPool:
    def __init__(self, route):
        self.route = route
        self._lock = threading.Lock()
        self.min_threads = max(0, int(route.min_threads))
        self.max_threads = max(0, int(route.max_threads))
        if self.max_threads < self.min_threads:
            raise ValueError("max_threads for view routes must be >= min_threads")

        self.workers = []
        self.by_id = {}
        self.default_worker = None
        self._next_index = 0

        for _ in range(self.min_threads):
            self._spawn_worker_locked()

    def _spawn_worker_locked(self):
        if self.max_threads > 0 and len(self.workers) >= self.max_threads:
            return None
        worker = _RouteThreadWorker(self.route, self._next_index)
        self._next_index += 1
        self.workers.append(worker)
        self.by_id[worker.thread_id] = worker
        if self.default_worker is None:
            self.default_worker = worker
        return worker

    def _pick_least_busy_locked(self):
        if not self.workers:
            return None
        candidates = []
        for worker in self.workers:
            load = worker.load()
            if self.route.requests_per_thread > 0 and load >= self.route.requests_per_thread:
                continue
            candidates.append((load, worker.index, worker))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1]))
        return candidates[0][2]

    def _pick_any_least_busy_locked(self):
        if not self.workers:
            return None
        candidates = [(worker.load(), worker.index, worker) for worker in self.workers]
        candidates.sort(key=lambda row: (row[0], row[1]))
        return candidates[0][2]

    def resolve(self, request=None, cookie_thread_id=""):
        _ = request
        _ = cookie_thread_id
        with self._lock:
            selected = self._pick_least_busy_locked()
            if selected is not None:
                return selected

            can_grow = self.max_threads == 0 or len(self.workers) < self.max_threads
            if can_grow:
                created = self._spawn_worker_locked()
                if created is not None:
                    return created

            return self._pick_any_least_busy_locked()

    def close(self):
        for worker in self.workers:
            worker.close()

    def describe(self):
        rows = []
        with self._lock:
            for worker in self.workers:
                row = worker.describe()
                row["default"] = bool(self.default_worker and worker.thread_id == self.default_worker.thread_id)
                row["min_threads"] = self.min_threads
                row["max_threads"] = self.max_threads
                row["workers_total"] = len(self.workers)
                row["distribution"] = "least_busy"
                rows.append(row)
        return rows


class Route:
    def __init__(
        self,
        path,
        methods,
        handler,
        kind,
        thread_count=0,
        min_threads=None,
        max_threads=None,
        thread_host="127.0.0.1",
        thread_base_port=0,
        max_clients=100,
        worker_timeout_seconds=DEFAULT_WORKER_REQUEST_TIMEOUT_SECONDS,
        max_pending_jobs=DEFAULT_MAX_PENDING_JOBS,
        requests_per_thread=None,
        affinity_ttl_seconds=DEFAULT_AFFINITY_TTL_SECONDS,
        affinity_max_entries=DEFAULT_AFFINITY_MAX_ENTRIES,
        cache=None,
    ):
        self.path = path
        self.methods = {m.upper() for m in methods}
        self.handler = handler
        self.kind = kind

        self.thread_host = thread_host or "127.0.0.1"
        self.thread_base_port = max(0, int(thread_base_port or 0))
        self.max_clients = max(0, int(max_clients))
        self.worker_timeout_seconds = max(0.0, float(worker_timeout_seconds))
        if requests_per_thread is None:
            resolved_requests_per_thread = int(max_pending_jobs)
        else:
            resolved_requests_per_thread = int(requests_per_thread)
        self.requests_per_thread = max(0, resolved_requests_per_thread)
        # Backward-compatible alias.
        self.max_pending_jobs = self.requests_per_thread
        self.affinity_ttl_seconds = max(0.0, float(affinity_ttl_seconds))
        self.affinity_max_entries = max(1, int(affinity_max_entries))
        self.cache_config = cache

        if kind == "plain":
            count = int(thread_count) if thread_count is not None else 0
            if count < 0:
                raise ValueError("thread_count for view routes must be >= 0")

            resolved_min = count if min_threads is None else int(min_threads)
            resolved_max = count if max_threads is None else int(max_threads)
            if resolved_min < 0:
                raise ValueError("min_threads for view routes must be >= 0")
            if resolved_max < 0:
                raise ValueError("max_threads for view routes must be >= 0")
            if resolved_max > 0 and resolved_max < resolved_min:
                raise ValueError("max_threads for view routes must be >= min_threads")

            self.min_threads = resolved_min
            self.max_threads = resolved_max
            # Keep existing field name for compatibility in metadata/tests.
            self.thread_count = resolved_max
            self.thread_pool = _RouteThreadPool(self) if resolved_max > 0 else None
        else:
            self.min_threads = 0
            self.max_threads = 0
            self.thread_count = 0
            self.thread_pool = None

    def describe(self):
        handler = self.handler
        thread_pool = self.thread_pool.describe() if self.thread_pool else []
        return {
            "path": self.path,
            "methods": sorted(self.methods),
            "kind": self.kind,
            "handler_name": getattr(handler, "__name__", ""),
            "handler_module": getattr(handler, "__module__", ""),
            "thread_count": int(self.thread_count),
            "min_threads": int(self.min_threads),
            "max_threads": int(self.max_threads),
            "thread_host": self.thread_host,
            "thread_base_port": int(self.thread_base_port),
            "max_clients": int(self.max_clients),
            "worker_timeout_seconds": float(self.worker_timeout_seconds),
            "requests_per_thread": int(self.requests_per_thread),
            "affinity_ttl_seconds": float(self.affinity_ttl_seconds),
            "affinity_max_entries": int(self.affinity_max_entries),
            "thread_mode": "thread-pool" if self.thread_pool else "direct",
            "thread_pool": thread_pool,
            "cache_config": self.cache_config if self.cache_config is not None else None,
        }


class Router:
    def __init__(self):
        self.routes = []

    def add(self, route):
        self.routes.append(route)

    def resolve(self, path, method=None):
        for route in self.routes:
            if route.path != path:
                continue
            if method is None or method.upper() in route.methods:
                return route
        return None


class App:
    def __init__(
        self,
        cors_allow_origin=DEFAULT_CORS_ALLOW_ORIGIN,
        thread_cookie_secret="",
        thread_cookie_name=THREAD_COOKIE_NAME,
    ):
        self.router = Router()
        self.ws_routes = {}
        self.startup_hooks = []
        self.cors_allow_origin = (cors_allow_origin or "").strip()
        self.metrics = None
        self.security = None
        self.proxyi = None
        self.caches = None
        self.tasks = TaskManager(app=self)

        raw_secret = str(thread_cookie_secret or "").strip()
        if not raw_secret:
            raw_secret = f"{uuid.uuid4()}|{uuid.uuid4()}|{time.time_ns()}"
        self._thread_cookie_secret = raw_secret.encode("utf-8")
        self.thread_cookie_name = str(thread_cookie_name or THREAD_COOKIE_NAME)

    def _sign_thread_cookie(self, route_path, thread_id):
        payload = f"{route_path}|{thread_id}".encode("utf-8")
        digest = sha1(self._thread_cookie_secret + b"|" + payload + b"|" + self._thread_cookie_secret)
        sig = digest.hex()
        return f"{thread_id}.{sig}"

    def _verify_thread_cookie(self, route_path, raw_cookie):
        value = str(raw_cookie or "").strip()
        if not value or "." not in value:
            return ""
        thread_id, sig = value.split(".", 1)
        try:
            normalized = str(uuid.UUID(thread_id))
        except Exception:
            return ""

        payload = f"{route_path}|{normalized}".encode("utf-8")
        expected = sha1(self._thread_cookie_secret + b"|" + payload + b"|" + self._thread_cookie_secret).hex()
        if not _constant_time_equals(sig, expected):
            return ""
        return normalized

    def route(
        self,
        path,
        methods=("GET",),
        kind="plain",
        thread_count=None,
        min_threads=None,
        max_threads=None,
        thread_host="127.0.0.1",
        thread_base_port=0,
        max_clients=100,
        worker_timeout_seconds=DEFAULT_WORKER_REQUEST_TIMEOUT_SECONDS,
        max_pending_jobs=DEFAULT_MAX_PENDING_JOBS,
        requests_per_thread=None,
        affinity_ttl_seconds=DEFAULT_AFFINITY_TTL_SECONDS,
        affinity_max_entries=DEFAULT_AFFINITY_MAX_ENTRIES,
        cache=None,
    ):
        def decorator(func):
            if thread_count is None:
                resolved_count = 0 if kind == "plain" else 0
            else:
                resolved_count = int(thread_count)
            self.router.add(
                Route(
                    path,
                    methods,
                    func,
                    kind,
                    thread_count=resolved_count,
                    min_threads=min_threads,
                    max_threads=max_threads,
                    thread_host=thread_host,
                    thread_base_port=thread_base_port,
                    max_clients=max_clients,
                    worker_timeout_seconds=worker_timeout_seconds,
                    max_pending_jobs=max_pending_jobs,
                    requests_per_thread=requests_per_thread,
                    affinity_ttl_seconds=affinity_ttl_seconds,
                    affinity_max_entries=affinity_max_entries,
                    cache=cache,
                )
            )
            return func

        return decorator

    def view(
        self,
        path,
        methods=("GET",),
        thread_count=0,
        min_threads=None,
        max_threads=None,
        thread_host="127.0.0.1",
        thread_base_port=0,
        max_clients=100,
        worker_timeout_seconds=DEFAULT_WORKER_REQUEST_TIMEOUT_SECONDS,
        max_pending_jobs=DEFAULT_MAX_PENDING_JOBS,
        requests_per_thread=None,
        affinity_ttl_seconds=DEFAULT_AFFINITY_TTL_SECONDS,
        affinity_max_entries=DEFAULT_AFFINITY_MAX_ENTRIES,
        cache=None,
    ):
        return self.route(
            path,
            methods=methods,
            kind="plain",
            thread_count=thread_count,
            min_threads=min_threads,
            max_threads=max_threads,
            thread_host=thread_host,
            thread_base_port=thread_base_port,
            max_clients=max_clients,
            worker_timeout_seconds=worker_timeout_seconds,
            max_pending_jobs=max_pending_jobs,
            requests_per_thread=requests_per_thread,
            affinity_ttl_seconds=affinity_ttl_seconds,
            affinity_max_entries=affinity_max_entries,
            cache=cache,
        )

    def api(self, path, methods=("GET",)):
        return self.route(path, methods=methods, kind="api")

    def ws(
        self,
        path,
        *,
        subprotocols=(),
        idle_timeout=0.0,
        keepalive_interval=0.0,
        pong_timeout=0.0,
        auto_pong=True,
        on_close=None,
        on_error=None,
        on_timeout=None,
        io_poll_interval=1.0,
        ping_payload=b"",
    ):
        def decorator(func):
            self.ws_routes[path] = {
                "handler": func,
                "subprotocols": _normalize_ws_protocols(subprotocols),
                "idle_timeout": float(idle_timeout or 0.0),
                "keepalive_interval": float(keepalive_interval or 0.0),
                "pong_timeout": float(pong_timeout or 0.0),
                "auto_pong": bool(auto_pong),
                "on_close": on_close,
                "on_error": on_error,
                "on_timeout": on_timeout,
                "io_poll_interval": float(io_poll_interval or 1.0),
                "ping_payload": (
                    ping_payload.encode("utf-8")
                    if isinstance(ping_payload, str)
                    else bytes(ping_payload or b"")
                ),
            }
            return func

        return decorator

    def add_startup(self, func):
        self.startup_hooks.append(func)

    def enable_metrics(
        self,
        path="/api/metrics",
        stream_path="/api/metrics/stream",
        app_name=None,
    ):
        from .metrics import install_metrics

        def _extra_snapshot():
            data = {"threads": self.thread_metrics_snapshot()}
            cache = getattr(self, "cache", None)
            if cache and hasattr(cache, "metrics_snapshot"):
                try:
                    cache_snapshot = cache.metrics_snapshot()
                    if isinstance(cache_snapshot, dict):
                        data.update(cache_snapshot)
                except Exception as e:
                    data["cache_metrics_error"] = str(e)
            caches = getattr(self, "caches", None)
            if caches and hasattr(caches, "metrics_snapshot"):
                try:
                    caches_snapshot = caches.metrics_snapshot()
                    if isinstance(caches_snapshot, dict):
                        data.update(caches_snapshot)
                except Exception as e:
                    data["http_cache_metrics_error"] = str(e)
            security = getattr(self, "security", None)
            if security:
                data["security"] = security.snapshot()
            proxyi = getattr(self, "proxyi", None)
            if proxyi:
                try:
                    data["proxyi"] = proxyi.snapshot() if hasattr(proxyi, "snapshot") else proxyi.metrics_snapshot()
                except Exception as e:
                    data["proxyi_metrics_error"] = str(e)
            return data

        return install_metrics(
            self,
            path=path,
            stream_path=stream_path,
            app_name=app_name,
            extra_snapshot_provider=_extra_snapshot,
        )

    def enable_security(self, policy=None):
        from .security import install_security

        return install_security(self, policy=policy)

    def enable_caches(self, caches=None):
        from .caches import install_caches

        return install_caches(self, caches=caches)

    def describe(self):
        routes = [route.describe() for route in self.router.routes]
        ws_routes = []
        for path, cfg in sorted(self.ws_routes.items(), key=lambda item: item[0]):
            handler = cfg.get("handler")
            ws_routes.append(
                {
                    "path": path,
                    "handler_name": getattr(handler, "__name__", ""),
                    "handler_module": getattr(handler, "__module__", ""),
                    "subprotocols": list(cfg.get("subprotocols") or ()),
                    "idle_timeout": float(cfg.get("idle_timeout") or 0.0),
                    "keepalive_interval": float(cfg.get("keepalive_interval") or 0.0),
                    "pong_timeout": float(cfg.get("pong_timeout") or 0.0),
                    "auto_pong": bool(cfg.get("auto_pong")),
                    "io_poll_interval": float(cfg.get("io_poll_interval") or 0.0),
                    "ping_payload": (cfg.get("ping_payload") or b"").decode("utf-8", errors="ignore"),
                }
            )

        metrics = getattr(self, "metrics", None)
        security = getattr(self, "security", None)
        proxyi = getattr(self, "proxyi", None)
        cache = getattr(self, "cache", None)
        caches = getattr(self, "caches", None)
        tasks = getattr(self, "tasks", None)

        view_routes = [row for row in routes if row["kind"] == "plain"]
        api_routes = [row for row in routes if row["kind"] == "api"]
        return _docs_sanitize({
            "app_name": self.__class__.__name__,
            "cors_allow_origin": self.cors_allow_origin,
            "thread_cookie_name": self.thread_cookie_name,
            "startup_hooks_total": len(self.startup_hooks),
            "routes_total": len(routes),
            "view_routes_total": len(view_routes),
            "api_routes_total": len(api_routes),
            "ws_routes_total": len(ws_routes),
            "metrics_enabled": bool(metrics),
            "security_enabled": bool(security),
            "proxyi_enabled": bool(proxyi),
            "cache_enabled": bool(cache),
            "caches_enabled": bool(caches),
            "tasks_enabled": bool(tasks),
            "metrics": metrics.snapshot() if metrics and hasattr(metrics, "snapshot") else None,
            "security": security.snapshot() if security and hasattr(security, "snapshot") else None,
            "proxyi": proxyi.snapshot() if proxyi and hasattr(proxyi, "snapshot") else None,
            "cache": cache.snapshot() if cache and hasattr(cache, "snapshot") else None,
            "http_cache": caches.snapshot() if caches and hasattr(caches, "snapshot") else None,
            "tasks": tasks.snapshot() if tasks and hasattr(tasks, "snapshot") else None,
            "routes": routes,
            "ws_routes": ws_routes,
        })

    def _render_docs_html(self, data, title="wsbuilder docs", description="Documentacion automatica de la aplicacion", schema_path="/docs.json"):
        def esc(value):
            return html_escape("" if value is None else str(value), quote=True)

        def yesno(value):
            return "Si" if value else "No"

        def render_stat(label, value):
            return f"""
            <div class=\"card\">
              <div class=\"k\">{esc(label)}</div>
              <div class=\"v\">{esc(value)}</div>
            </div>
            """

        routes = list(data.get("routes") or [])
        ws_routes = list(data.get("ws_routes") or [])
        summary = data.get("routes_total", 0)
        views_total = data.get("view_routes_total", 0)
        api_total = data.get("api_routes_total", 0)
        ws_total = data.get("ws_routes_total", 0)

        route_rows = []
        for row in routes:
            route_rows.append(
                "<tr>"
                f"<td data-label=\"Metodos\"><code>{esc(', '.join(row.get('methods') or []))}</code></td>"
                f"<td data-label=\"Ruta\"><code>{esc(row.get('path'))}</code></td>"
                f"<td data-label=\"Tipo\">{esc(row.get('kind'))}</td>"
                f"<td data-label=\"Ejecucion\">{esc(row.get('thread_mode'))}</td>"
                f"<td data-label=\"Handler\">{esc(row.get('handler_name'))}</td>"
                "</tr>"
            )

        ws_rows = []
        for row in ws_routes:
            protocols = ", ".join(row.get("subprotocols") or []) or "-"
            ws_rows.append(
                "<tr>"
                f"<td data-label=\"Ruta\"><code>{esc(row.get('path'))}</code></td>"
                f"<td data-label=\"Subprotocolos\">{esc(protocols)}</td>"
                f"<td data-label=\"Handler\">{esc(row.get('handler_name'))}</td>"
                f"<td data-label=\"Idle\">{esc(row.get('idle_timeout'))}</td>"
                f"<td data-label=\"Keepalive\">{esc(row.get('keepalive_interval'))}</td>"
                f"<td data-label=\"Pong\">{esc(row.get('pong_timeout'))}</td>"
                f"<td data-label=\"Auto pong\">{yesno(row.get('auto_pong'))}</td>"
                "</tr>"
            )

        raw_json = esc(json.dumps(data, ensure_ascii=False, indent=2))
        return f"""<!doctype html>
<html lang=\"es\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{esc(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f8fafc;
      --panel: #ffffff;
      --ink: #0f172a;
      --muted: #475569;
      --line: #dbe3ea;
      --brand: #0f766e;
      --brand-soft: rgba(15, 118, 110, 0.08);
      --shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(20, 184, 166, 0.12), transparent 24%),
        linear-gradient(180deg, #fff, var(--bg));
      color: var(--ink);
    }}
    a {{ color: var(--brand); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .wrap {{
      max-width: 1240px;
      margin: 0 auto;
      padding: 28px 20px 56px;
    }}
    .hero {{
      display: grid;
      gap: 14px;
      padding: 24px;
      background: linear-gradient(135deg, rgba(15,118,110,0.12), rgba(15,23,42,0.04));
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      margin-bottom: 22px;
    }}
    .hero h1 {{
      margin: 0;
      font-size: clamp(2rem, 4vw, 3.25rem);
      line-height: 1.05;
      letter-spacing: -0.04em;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      font-size: 1.02rem;
      max-width: 72ch;
    }}
    .links {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      border-radius: 999px;
      background: var(--panel);
      border: 1px solid var(--line);
      color: var(--ink);
      box-shadow: 0 8px 18px rgba(15, 23, 42, 0.05);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 20px 0 28px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 8px 18px rgba(15, 23, 42, 0.04);
      min-width: 0;
    }}
    .k {{
      color: var(--muted);
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }}
    .v {{
      margin-top: 8px;
      font-size: 1.6rem;
      font-weight: 800;
      letter-spacing: -0.03em;
    }}
    .section {{
      margin-top: 22px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .section h2 {{
      margin: 0;
      padding: 18px 20px 10px;
      font-size: 1.2rem;
      letter-spacing: -0.02em;
    }}
    .section p.lead {{
      margin: 0;
      padding: 0 20px 16px;
      color: var(--muted);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      vertical-align: top;
      padding: 12px 20px;
      border-top: 1px solid var(--line);
      word-break: break-word;
    }}
    th {{
      background: var(--brand-soft);
      color: var(--ink);
      font-size: 0.86rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    code {{
      background: rgba(15, 23, 42, 0.06);
      padding: 0.12em 0.35em;
      border-radius: 0.4rem;
      color: var(--ink);
    }}
    pre {{
      margin: 0;
      padding: 18px 20px 22px;
      background: #0b1020;
      color: #e2e8f0;
      overflow: auto;
      font-size: 0.86rem;
      line-height: 1.6;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    details {{
      border-top: 1px solid var(--line);
      padding: 0 20px 20px;
    }}
    details > summary {{
      cursor: pointer;
      padding: 14px 0;
      color: var(--brand);
      font-weight: 700;
    }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 960px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 680px) {{
      .wrap {{ padding: 18px 12px 40px; }}
      .hero {{ padding: 18px; border-radius: 18px; }}
      .grid {{ grid-template-columns: 1fr; }}
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-top: 1px solid var(--line); }}
      td {{ border-top: 0; padding: 10px 20px; }}
      td::before {{
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: 0.78rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        margin-bottom: 4px;
        font-weight: 700;
      }}
    }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <header class=\"hero\">
      <h1>{esc(title)}</h1>
      <p>{esc(description)}</p>
      <div class=\"links\">
        <a class=\"pill\" href=\"{esc(schema_path)}\">Ver JSON</a>
        <a class=\"pill\" href=\"#routes\">Rutas</a>
        <a class=\"pill\" href=\"#ws\">WebSocket</a>
        <a class=\"pill\" href=\"#estado\">Estado</a>
      </div>
    </header>

    <section class=\"grid\" aria-label=\"Resumen\">
      {render_stat("Rutas totales", summary)}
      {render_stat("Views", views_total)}
      {render_stat("APIs", api_total)}
      {render_stat("WS", ws_total)}
    </section>

    <section id=\"estado\" class=\"section\">
      <h2>Estado de la aplicacion</h2>
      <p class=\"lead\">La documentacion nativa se genera directamente desde la instancia activa de `App` y se actualiza con cada ruta registrada.</p>
      <table>
        <tbody>
          <tr><th>Campo</th><th>Valor</th></tr>
          <tr><td data-label=\"Campo\">CORS</td><td data-label=\"Valor\"><code>{esc(data.get('cors_allow_origin'))}</code></td></tr>
          <tr><td data-label=\"Campo\">Cookie de afinidad</td><td data-label=\"Valor\"><code>{esc(data.get('thread_cookie_name'))}</code></td></tr>
          <tr><td data-label=\"Campo\">Metricas</td><td data-label=\"Valor\">{yesno(data.get('metrics_enabled'))}</td></tr>
          <tr><td data-label=\"Campo\">Seguridad</td><td data-label=\"Valor\">{yesno(data.get('security_enabled'))}</td></tr>
          <tr><td data-label=\"Campo\">ProxyI</td><td data-label=\"Valor\">{yesno(data.get('proxyi_enabled'))}</td></tr>
          <tr><td data-label=\"Campo\">Cache</td><td data-label=\"Valor\">{yesno(data.get('cache_enabled'))}</td></tr>
          <tr><td data-label=\"Campo\">Cache HTTP</td><td data-label=\"Valor\">{yesno(data.get('caches_enabled'))}</td></tr>
          <tr><td data-label=\"Campo\">Tareas</td><td data-label=\"Valor\">{yesno(data.get('tasks_enabled'))}</td></tr>
        </tbody>
      </table>
    </section>

    <section id=\"routes\" class=\"section\">
      <h2>Rutas HTTP</h2>
      <p class=\"lead\">Cada ruta expone metodo, tipo, estrategia de ejecucion y el handler que la atiende.</p>
      <table>
        <thead>
          <tr>
            <th>Metodos</th>
            <th>Ruta</th>
            <th>Tipo</th>
            <th>Ejecucion</th>
            <th>Handler</th>
          </tr>
        </thead>
        <tbody>
          {''.join(route_rows) or '<tr><td colspan=\"5\" class=\"muted\">No hay rutas registradas.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section id=\"ws\" class=\"section\">
      <h2>Rutas WebSocket</h2>
      <p class=\"lead\">La vista incluye los detalles del handshake y del ciclo de vida del canal persistente.</p>
      <table>
        <thead>
          <tr>
            <th>Ruta</th>
            <th>Subprotocolos</th>
            <th>Handler</th>
            <th>Idle</th>
            <th>Keepalive</th>
            <th>Pong</th>
            <th>Auto pong</th>
          </tr>
        </thead>
        <tbody>
          {''.join(ws_rows) or '<tr><td colspan=\"7\" class=\"muted\">No hay rutas WebSocket registradas.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section class=\"section\">
      <h2>JSON completo</h2>
      <p class=\"lead\">Este bloque es el mismo contenido que entrega el endpoint JSON, util para automatizacion o integracion con otras herramientas.</p>
      <details open>
        <summary>Ver payload</summary>
        <pre>{raw_json}</pre>
      </details>
    </section>
  </div>
</body>
</html>"""

    def enable_docs(self, path="/docs", json_path="/docs.json", title=None, description=None):
        docs_title = title or f"{self.__class__.__name__} docs"
        docs_description = description or "Documentacion automatica y nativa de la aplicacion activa."

        @self.api(json_path, methods=("GET",))
        def _docs_json(_request):
            return self.describe()

        @self.view(path, methods=("GET",))
        def _docs_view(_request):
            payload = self.describe()
            return Response.html(
                self._render_docs_html(
                    payload,
                    title=docs_title,
                    description=docs_description,
                    schema_path=json_path,
                )
            )

        return {"path": path, "json_path": json_path}

    def list_view_threads(self, path, method="GET"):
        route = self.router.resolve(path, method=method)
        if not route or not route.thread_pool:
            return []
        return route.thread_pool.describe()

    def thread_metrics_snapshot(self):
        routes = []
        workers_total = 0
        active_jobs_total = 0
        pending_jobs_total = 0
        current_load_total = 0

        for route in self.router.routes:
            if route.kind != "plain":
                continue
            workers = route.thread_pool.describe() if route.thread_pool else []
            workers_total += len(workers)
            active_jobs = sum(int(row.get("active_jobs", 0)) for row in workers)
            pending_jobs = sum(int(row.get("pending_jobs", 0)) for row in workers)
            current_load = sum(int(row.get("current_load", 0)) for row in workers)
            active_jobs_total += active_jobs
            pending_jobs_total += pending_jobs
            current_load_total += current_load

            routes.append(
                {
                    "path": route.path,
                    "methods": sorted(route.methods),
                    "min_threads": int(route.min_threads),
                    "max_threads": int(route.max_threads),
                    "requests_per_thread": int(route.requests_per_thread),
                    "distribution": "least_busy",
                    "workers_total": len(workers),
                    "active_jobs": active_jobs,
                    "pending_jobs": pending_jobs,
                    "current_load": current_load,
                    "workers": workers,
                }
            )

        return {
            "distribution": "least_busy",
            "routes_total": len(routes),
            "workers_total": workers_total,
            "active_jobs_total": active_jobs_total,
            "pending_jobs_total": pending_jobs_total,
            "current_load_total": current_load_total,
            "routes": routes,
        }

    def close(self):
        tasks = getattr(self, "tasks", None)
        if tasks:
            try:
                tasks.close(wait=False)
            except Exception:
                pass
        caches = getattr(self, "caches", None)
        if caches and hasattr(caches, "close"):
            try:
                caches.close()
            except Exception:
                pass
        proxyi = getattr(self, "proxyi", None)
        if proxyi and hasattr(proxyi, "close"):
            try:
                proxyi.close()
            except Exception:
                pass
        for route in self.router.routes:
            pool = getattr(route, "thread_pool", None)
            if pool:
                pool.close()

    def dispatch(self, request):
        try:
            request.app = self
        except Exception:
            pass
        cors_allow_origin = self.cors_allow_origin

        security = getattr(self, "security", None)
        if security:
            decision = security.evaluate(request)
            if not decision.allowed:
                route_for_format = self.router.resolve(request.path, request.method)
                headers = decision.response_headers()
                if cors_allow_origin:
                    headers.setdefault("Access-Control-Allow-Origin", cors_allow_origin)
                    if cors_allow_origin != "*":
                        headers.setdefault("Vary", "Origin")
                if route_for_format and route_for_format.kind == "api":
                    return Response.json(
                        {"status": "error", "message": decision.message, "reason": decision.reason},
                        status=decision.status,
                        headers=headers,
                    )
                return Response.text(decision.message, status=decision.status, headers=headers)

        if request.method == "OPTIONS":
            route = self.router.resolve(request.path, method=None)
            if route:
                allow = sorted(route.methods | {"OPTIONS"})
                headers = {
                    "Access-Control-Allow-Methods": ", ".join(allow),
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Key",
                }
                if cors_allow_origin:
                    headers["Access-Control-Allow-Origin"] = cors_allow_origin
                    if cors_allow_origin != "*":
                        headers["Vary"] = "Origin"
                return Response(status=200, body=b"", headers=headers)

        route = self.router.resolve(request.path, request.method)
        if not route:
            return Response.text("Not Found", status=404)

        caches = getattr(self, "caches", None)
        if caches and route.kind == "plain":
            cached_response = caches.fetch(request, route)
            if cached_response is not None:
                return cached_response

        selected_worker = None
        try:
            if route.thread_pool:
                selected_worker = route.thread_pool.resolve(request=request)
                if selected_worker is None:
                    raise _RouteExecutionError(503, "No route workers available")
                result = selected_worker.submit(request)
            else:
                result = route.handler(request)
        except _RouteExecutionError as e:
            return Response.text(e.message, status=e.status)
        except Exception as e:
            print(f"[http] handler error {request.method} {request.path}: {e}")
            if route.kind == "api":
                headers = {}
                if cors_allow_origin:
                    headers["Access-Control-Allow-Origin"] = cors_allow_origin
                    if cors_allow_origin != "*":
                        headers["Vary"] = "Origin"
                return Response.json(
                    {"status": "error", "message": "Internal Server Error"},
                    status=500,
                    headers=headers,
                )
            return Response.text("Internal Server Error", status=500)

        if isinstance(result, Response):
            resp = result
        elif route.kind == "api" and isinstance(result, (dict, list)):
            resp = Response.json(result)
        else:
            resp = Response.text("" if result is None else str(result))

        if caches and route.kind == "plain":
            caches.store_response(request, route, resp)

        thread_info = selected_worker
        if thread_info is not None:
            resp.headers.setdefault(THREAD_RESPONSE_ID_HEADER, thread_info.thread_id)
            resp.headers.setdefault(THREAD_RESPONSE_HOST_HEADER, thread_info.host)
            if thread_info.port > 0:
                resp.headers.setdefault(THREAD_RESPONSE_PORT_HEADER, str(thread_info.port))
            resp.headers.setdefault(THREAD_RESPONSE_MODE_HEADER, "worker")
            signed_cookie = self._sign_thread_cookie(route.path, thread_info.thread_id)
            resp.headers["Set-Cookie"] = build_set_cookie(
                self.thread_cookie_name,
                signed_cookie,
                path=route.path or "/",
                http_only=True,
                secure=bool((request.tls or {}).get("enabled")),
                same_site="Lax",
            )

        if route.kind == "api":
            if cors_allow_origin:
                resp.headers.setdefault("Access-Control-Allow-Origin", cors_allow_origin)
                if cors_allow_origin != "*":
                    resp.headers.setdefault("Vary", "Origin")

        return resp

    def run(self, host, port, ssl_context=None):
        from .server import HTTPServer

        server = HTTPServer(host, port, self, ssl_context=ssl_context)
        server.serve_forever()

import threading
import time
import uuid

from .constants import DEFAULT_CORS_ALLOW_ORIGIN
from .cookies import build_set_cookie
from .http import Response
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
        self.caches = None

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
        caches = getattr(self, "caches", None)
        if caches and hasattr(caches, "close"):
            try:
                caches.close()
            except Exception:
                pass
        for route in self.router.routes:
            pool = getattr(route, "thread_pool", None)
            if pool:
                pool.close()

    def dispatch(self, request):
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

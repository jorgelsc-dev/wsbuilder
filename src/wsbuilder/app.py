import threading
import time
import uuid

from .constants import DEFAULT_CORS_ALLOW_ORIGIN
from .cookies import build_set_cookie, get_cookie
from .headers import get_header
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
        # Ports are metadata only for affinity/debugging. The main server socket handles HTTP I/O.
        self.listening = False
        self.listen_error = "disabled"

        self.worker_timeout_seconds = route.worker_timeout_seconds
        self.max_pending_jobs = route.max_pending_jobs

        self._jobs = []
        self._jobs_cond = threading.Condition()
        self._running = True
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
            try:
                job.result = self.route.handler(job.request)
            except Exception as e:
                job.error = e
            finally:
                job.done.set()

    def submit(self, request):
        job = _RouteJob(request)
        with self._jobs_cond:
            if not self._running:
                raise _RouteExecutionError(503, "Route worker is closed")
            if self.max_pending_jobs > 0 and len(self._jobs) >= self.max_pending_jobs:
                raise _RouteExecutionError(503, "Route worker queue is full")
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

    def describe(self):
        return {
            "id": self.thread_id,
            "host": self.host,
            "port": self.port,
            "listening": self.listening,
            "listen_error": self.listen_error,
            "pending_jobs": self.pending_jobs(),
            "max_pending_jobs": self.max_pending_jobs,
            "worker_timeout_seconds": self.worker_timeout_seconds,
        }


class _RouteThreadPool:
    def __init__(self, route):
        self.route = route
        self.max_clients = max(0, int(route.max_clients))
        self.affinity_ttl_seconds = max(0.0, float(route.affinity_ttl_seconds))
        self.affinity_max_entries = max(1, int(route.affinity_max_entries))

        self.workers = [_RouteThreadWorker(route, i) for i in range(route.thread_count)]
        self.by_id = {worker.thread_id: worker for worker in self.workers}
        self.default_worker = self.workers[0] if self.workers else None

        self._lock = threading.Lock()
        self._client_to_worker = {}  # fingerprint -> worker_id
        self._client_last_seen = {}  # fingerprint -> unix timestamp
        self._worker_clients = {worker.thread_id: set() for worker in self.workers}

    def _fingerprint(self, request):
        client = request.client or ("", 0)
        ip = str(client[0] or "")
        xff = str(get_header(request.headers, "x-forwarded-for", default="") or "").strip()
        if xff:
            ip = xff.split(",", 1)[0].strip() or ip
        ua = str(get_header(request.headers, "user-agent", default="") or "")
        return f"{ip}|{ua}"

    def _evict_fingerprint_locked(self, fingerprint):
        worker_id = self._client_to_worker.pop(fingerprint, None)
        self._client_last_seen.pop(fingerprint, None)
        if worker_id:
            members = self._worker_clients.get(worker_id)
            if members and fingerprint in members:
                members.remove(fingerprint)

    def _evict_stale_locked(self, now):
        if self.affinity_ttl_seconds > 0:
            cutoff = now - self.affinity_ttl_seconds
            for fp, seen in list(self._client_last_seen.items()):
                if seen < cutoff:
                    self._evict_fingerprint_locked(fp)

        over = len(self._client_last_seen) - self.affinity_max_entries
        if over > 0:
            oldest = sorted(self._client_last_seen.items(), key=lambda item: item[1])[:over]
            for fp, _seen in oldest:
                self._evict_fingerprint_locked(fp)

    def _worker_from_map_locked(self, fingerprint):
        worker_id = self._client_to_worker.get(fingerprint)
        if not worker_id:
            return None
        return self.by_id.get(worker_id)

    def _can_accept_locked(self, worker, fingerprint):
        if self.max_clients <= 0:
            return True
        clients = self._worker_clients.get(worker.thread_id, set())
        if fingerprint in clients:
            return True
        return len(clients) < self.max_clients

    def _assign_client_locked(self, fingerprint, worker, now):
        previous = self._client_to_worker.get(fingerprint)
        if previous and previous != worker.thread_id:
            prev_members = self._worker_clients.get(previous)
            if prev_members and fingerprint in prev_members:
                prev_members.remove(fingerprint)
        self._client_to_worker[fingerprint] = worker.thread_id
        self._client_last_seen[fingerprint] = now
        self._worker_clients.setdefault(worker.thread_id, set()).add(fingerprint)

    def _pick_best_worker_locked(self, fingerprint):
        candidates = []
        for worker in self.workers:
            if not self._can_accept_locked(worker, fingerprint):
                continue
            load = len(self._worker_clients.get(worker.thread_id, set()))
            candidates.append((load, worker.index, worker))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1]))
        return candidates[0][2]

    def resolve(self, request, cookie_thread_id=""):
        with self._lock:
            now = time.time()
            self._evict_stale_locked(now)
            fingerprint = self._fingerprint(request)

            if cookie_thread_id:
                cookie_worker = self.by_id.get(cookie_thread_id)
                if cookie_worker and self._can_accept_locked(cookie_worker, fingerprint):
                    self._assign_client_locked(fingerprint, cookie_worker, now)
                    return cookie_worker, cookie_worker

            mapped_worker = self._worker_from_map_locked(fingerprint)
            if mapped_worker and self._can_accept_locked(mapped_worker, fingerprint):
                self._assign_client_locked(fingerprint, mapped_worker, now)
                return None, mapped_worker

            assigned_worker = self._pick_best_worker_locked(fingerprint)
            if assigned_worker:
                self._assign_client_locked(fingerprint, assigned_worker, now)
                return None, assigned_worker

            return None, None

    def close(self):
        for worker in self.workers:
            worker.close()

    def describe(self):
        rows = []
        with self._lock:
            for worker in self.workers:
                clients = len(self._worker_clients.get(worker.thread_id, set()))
                row = worker.describe()
                row["default"] = bool(self.default_worker and worker.thread_id == self.default_worker.thread_id)
                row["clients"] = clients
                row["max_clients"] = self.max_clients
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
        thread_host="127.0.0.1",
        thread_base_port=0,
        max_clients=100,
        worker_timeout_seconds=DEFAULT_WORKER_REQUEST_TIMEOUT_SECONDS,
        max_pending_jobs=DEFAULT_MAX_PENDING_JOBS,
        affinity_ttl_seconds=DEFAULT_AFFINITY_TTL_SECONDS,
        affinity_max_entries=DEFAULT_AFFINITY_MAX_ENTRIES,
    ):
        self.path = path
        self.methods = {m.upper() for m in methods}
        self.handler = handler
        self.kind = kind

        self.thread_host = thread_host or "127.0.0.1"
        self.thread_base_port = max(0, int(thread_base_port or 0))
        self.max_clients = max(0, int(max_clients))
        self.worker_timeout_seconds = max(0.0, float(worker_timeout_seconds))
        self.max_pending_jobs = max(1, int(max_pending_jobs))
        self.affinity_ttl_seconds = max(0.0, float(affinity_ttl_seconds))
        self.affinity_max_entries = max(1, int(affinity_max_entries))

        if kind == "plain":
            count = int(thread_count) if thread_count is not None else 0
            if count < 0:
                raise ValueError("thread_count for view routes must be >= 0")
            self.thread_count = count
            self.thread_pool = _RouteThreadPool(self) if count > 0 else None
        else:
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
        thread_host="127.0.0.1",
        thread_base_port=0,
        max_clients=100,
        worker_timeout_seconds=DEFAULT_WORKER_REQUEST_TIMEOUT_SECONDS,
        max_pending_jobs=DEFAULT_MAX_PENDING_JOBS,
        affinity_ttl_seconds=DEFAULT_AFFINITY_TTL_SECONDS,
        affinity_max_entries=DEFAULT_AFFINITY_MAX_ENTRIES,
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
                    thread_host=thread_host,
                    thread_base_port=thread_base_port,
                    max_clients=max_clients,
                    worker_timeout_seconds=worker_timeout_seconds,
                    max_pending_jobs=max_pending_jobs,
                    affinity_ttl_seconds=affinity_ttl_seconds,
                    affinity_max_entries=affinity_max_entries,
                )
            )
            return func

        return decorator

    def view(
        self,
        path,
        methods=("GET",),
        thread_count=0,
        thread_host="127.0.0.1",
        thread_base_port=0,
        max_clients=100,
        worker_timeout_seconds=DEFAULT_WORKER_REQUEST_TIMEOUT_SECONDS,
        max_pending_jobs=DEFAULT_MAX_PENDING_JOBS,
        affinity_ttl_seconds=DEFAULT_AFFINITY_TTL_SECONDS,
        affinity_max_entries=DEFAULT_AFFINITY_MAX_ENTRIES,
    ):
        return self.route(
            path,
            methods=methods,
            kind="plain",
            thread_count=thread_count,
            thread_host=thread_host,
            thread_base_port=thread_base_port,
            max_clients=max_clients,
            worker_timeout_seconds=worker_timeout_seconds,
            max_pending_jobs=max_pending_jobs,
            affinity_ttl_seconds=affinity_ttl_seconds,
            affinity_max_entries=affinity_max_entries,
        )

    def api(self, path, methods=("GET",)):
        return self.route(path, methods=methods, kind="api")

    def ws(self, path):
        def decorator(func):
            self.ws_routes[path] = func
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

        return install_metrics(
            self,
            path=path,
            stream_path=stream_path,
            app_name=app_name,
        )

    def list_view_threads(self, path, method="GET"):
        route = self.router.resolve(path, method=method)
        if not route or not route.thread_pool:
            return []
        return route.thread_pool.describe()

    def close(self):
        for route in self.router.routes:
            pool = getattr(route, "thread_pool", None)
            if pool:
                pool.close()

    def dispatch(self, request):
        cors_allow_origin = self.cors_allow_origin
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

        selected_worker = None
        assigned_worker = None
        try:
            if route.thread_pool:
                raw_cookie = get_cookie(request.headers, self.thread_cookie_name, default="")
                cookie_thread_id = self._verify_thread_cookie(route.path, raw_cookie)
                selected_worker, assigned_worker = route.thread_pool.resolve(request, cookie_thread_id)
                if selected_worker is not None:
                    result = selected_worker.submit(request)
                else:
                    result = route.handler(request)
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

        thread_info = selected_worker or assigned_worker
        if thread_info is not None:
            resp.headers.setdefault(THREAD_RESPONSE_ID_HEADER, thread_info.thread_id)
            resp.headers.setdefault(THREAD_RESPONSE_HOST_HEADER, thread_info.host)
            if thread_info.port > 0:
                resp.headers.setdefault(THREAD_RESPONSE_PORT_HEADER, str(thread_info.port))
            if selected_worker is not None:
                resp.headers.setdefault(THREAD_RESPONSE_MODE_HEADER, "worker")
            else:
                resp.headers.setdefault(THREAD_RESPONSE_MODE_HEADER, "parent-assigned")
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

import socket
import threading
import uuid

from .constants import DEFAULT_CORS_ALLOW_ORIGIN
from .cookies import build_set_cookie, get_cookie
from .headers import get_header
from .http import Response

THREAD_COOKIE_NAME = "wsbuilder-thread"
THREAD_RESPONSE_ID_HEADER = "WSBuilder-Thread"
THREAD_RESPONSE_HOST_HEADER = "WSBuilder-Thread-Host"
THREAD_RESPONSE_PORT_HEADER = "WSBuilder-Thread-Port"
THREAD_RESPONSE_MODE_HEADER = "WSBuilder-Thread-Mode"


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
        self.port = 0
        self.listening = False
        self.listen_error = None
        self._listen_socket = None
        self._jobs = []
        self._jobs_cond = threading.Condition()
        self._running = True
        self._open_listener()
        self._thread = threading.Thread(
            target=self._run,
            name=f"wsbuilder-view-thread-{route.path}-{self.thread_id}",
            daemon=True,
        )
        self._thread.start()

    def _open_listener(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, 0))
            sock.listen(8)
            self._listen_socket = sock
            self.port = sock.getsockname()[1]
            self.listening = True
        except OSError as e:
            self.listen_error = str(e)
            self._listen_socket = None
            self.port = 0
            self.listening = False

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
                raise RuntimeError("route thread is closed")
            self._jobs.append(job)
            self._jobs_cond.notify()
        job.done.wait()
        if job.error is not None:
            raise job.error
        return job.result

    def close(self):
        with self._jobs_cond:
            self._running = False
            self._jobs_cond.notify_all()
        try:
            if self._listen_socket is not None:
                self._listen_socket.close()
        except Exception:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def describe(self):
        return {
            "id": self.thread_id,
            "host": self.host,
            "port": self.port,
            "listening": self.listening,
            "listen_error": self.listen_error,
        }


class _RouteThreadPool:
    def __init__(self, route):
        self.route = route
        self.max_clients = max(0, int(route.max_clients))
        self.workers = [_RouteThreadWorker(route, i) for i in range(route.thread_count)]
        self.by_id = {worker.thread_id: worker for worker in self.workers}
        self.default_worker = self.workers[0] if self.workers else None
        self._lock = threading.Lock()
        self._client_to_worker = {}
        self._worker_clients = {worker.thread_id: set() for worker in self.workers}

    def _fingerprint(self, request):
        client = request.client or ("", 0)
        ip = str(client[0] or "")
        ua = str(get_header(request.headers, "user-agent", default="") or "")
        return f"{ip}|{ua}"

    def _worker_from_cookie(self, cookie_value):
        raw = str(cookie_value or "").strip()
        if not raw:
            return None
        try:
            normalized = str(uuid.UUID(raw))
        except Exception:
            return None
        return self.by_id.get(normalized)

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

    def _assign_client_locked(self, fingerprint, worker):
        prev_worker_id = self._client_to_worker.get(fingerprint)
        if prev_worker_id and prev_worker_id != worker.thread_id:
            prev_set = self._worker_clients.get(prev_worker_id)
            if prev_set is not None and fingerprint in prev_set:
                prev_set.remove(fingerprint)
        self._client_to_worker[fingerprint] = worker.thread_id
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

    def resolve(self, request, cookie_value):
        with self._lock:
            fingerprint = self._fingerprint(request)
            cookie_worker = self._worker_from_cookie(cookie_value)
            if cookie_worker is not None:
                self._assign_client_locked(fingerprint, cookie_worker)
                return cookie_worker, cookie_worker

            mapped_worker = self._worker_from_map_locked(fingerprint)
            if mapped_worker is not None and self._can_accept_locked(mapped_worker, fingerprint):
                self._assign_client_locked(fingerprint, mapped_worker)
                return None, mapped_worker

            assigned_worker = self._pick_best_worker_locked(fingerprint)
            if assigned_worker is not None:
                self._assign_client_locked(fingerprint, assigned_worker)
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
                row["default"] = worker.thread_id == self.default_worker.thread_id
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
        max_clients=100,
    ):
        self.path = path
        self.methods = {m.upper() for m in methods}
        self.handler = handler
        self.kind = kind
        self.thread_host = thread_host or "127.0.0.1"
        self.max_clients = max(0, int(max_clients))
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
    def __init__(self, cors_allow_origin=DEFAULT_CORS_ALLOW_ORIGIN):
        self.router = Router()
        self.ws_routes = {}
        self.startup_hooks = []
        self.cors_allow_origin = (cors_allow_origin or "").strip()
        self.metrics = None

    def route(
        self,
        path,
        methods=("GET",),
        kind="plain",
        thread_count=None,
        thread_host="127.0.0.1",
        max_clients=100,
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
                    max_clients=max_clients,
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
        max_clients=100,
    ):
        return self.route(
            path,
            methods=methods,
            kind="plain",
            thread_count=thread_count,
            thread_host=thread_host,
            max_clients=max_clients,
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
                cookie_value = get_cookie(request.headers, THREAD_COOKIE_NAME, default="")
                selected_worker, assigned_worker = route.thread_pool.resolve(request, cookie_value)
                if selected_worker is not None:
                    result = selected_worker.submit(request)
                else:
                    result = route.handler(request)
            else:
                result = route.handler(request)
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
            resp.headers.setdefault(THREAD_RESPONSE_PORT_HEADER, str(thread_info.port))
            if selected_worker is not None:
                resp.headers.setdefault(THREAD_RESPONSE_MODE_HEADER, "worker")
            else:
                resp.headers.setdefault(THREAD_RESPONSE_MODE_HEADER, "parent-assigned")
                resp.headers["Set-Cookie"] = build_set_cookie(
                    THREAD_COOKIE_NAME,
                    thread_info.thread_id,
                    path=route.path or "/",
                    http_only=False,
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

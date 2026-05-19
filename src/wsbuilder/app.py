from .constants import DEFAULT_CORS_ALLOW_ORIGIN
from .http import Response


class Route:
    def __init__(self, path, methods, handler, kind):
        self.path = path
        self.methods = {m.upper() for m in methods}
        self.handler = handler
        self.kind = kind


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

    def route(self, path, methods=("GET",), kind="plain"):
        def decorator(func):
            self.router.add(Route(path, methods, func, kind))
            return func

        return decorator

    def view(self, path, methods=("GET",)):
        return self.route(path, methods=methods, kind="plain")

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

        try:
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

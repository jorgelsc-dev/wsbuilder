import argparse

from wsbuilder import App, Response

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780


def build_upstream_app():
    app = App(cors_allow_origin="*")
    app.enable_metrics(app_name="full-project-upstream")
    app.enable_docs(
        path="/docs",
        json_path="/docs.json",
        title="full-project upstream",
        description="Aplicacion upstream para la demo de ProxyI.",
    )

    @app.view("/")
    def home(_request):
        return Response.html(
            "<h1>full-project upstream</h1>"
            "<p>Usa <code>/api/proxy/upstream</code> para probar el proxy.</p>"
        )

    @app.api("/api/proxy/upstream")
    def upstream_status(request):
        return {
            "ok": True,
            "service": "upstream",
            "path": request.path,
            "query": request.query,
            "host": request.headers.get("host", ""),
        }

    return app


def main():
    parser = argparse.ArgumentParser(description="Upstream local para la demo completa de wsbuilder")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host de escucha")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Puerto de escucha")
    args = parser.parse_args()

    app = build_upstream_app()
    app.run(args.host, args.port)


if __name__ == "__main__":
    main()

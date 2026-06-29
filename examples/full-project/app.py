import argparse
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from wsbuilder import (
    App,
    Database,
    DateTimeField,
    IntegerField,
    JSONField,
    LocalDNSServer,
    Model,
    NeuralNetwork,
    Predictor,
    ProxyI,
    Response,
    SecurityPolicy,
    SQLiteMemoryCache,
    TextField,
    ViewResponseCache,
    install_cache,
    parse_close_payload,
    submit_training_task,
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
DB_PATH = DATA_DIR / "demo.sqlite3"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_UPSTREAM_PORT = 8780
DEFAULT_DNS_HOST = "127.0.0.1"
DEFAULT_DNS_PORT = 5533

XOR_X = [[0, 0], [0, 1], [1, 0], [1, 1]]
XOR_LABELS = ["no", "yes", "yes", "no"]


class Note(Model):
    __tablename__ = "notes"

    id = IntegerField(primary_key=True, auto_increment=True)
    title = TextField(unique=True, null=False, index=True)
    body = TextField(default="", null=False)
    tags = JSONField(default=list, null=False)
    created_at = DateTimeField(default=lambda: datetime.now(UTC), null=False)


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def build_predictor():
    predictor = Predictor()
    predictor.fit([[1], [2], [3], [4]], [[2], [4], [6], [8]])
    return predictor


def build_network():
    network = NeuralNetwork(
        seed=7,
        learning_rate=0.3,
        loss="binary_cross_entropy",
        task="classification",
    )
    network.add_dense(6, input_size=2, activation="tanh")
    network.add_dense(1, activation="sigmoid")
    return network


def note_to_dict(note):
    return {
        "id": note.id,
        "title": note.title,
        "body": note.body,
        "tags": list(note.tags or []),
        "created_at": note.created_at.isoformat() if getattr(note, "created_at", None) else None,
    }


def seed_notes(db):
    if Note.objects(db).count():
        return
    Note.create(db, title="welcome", body="Primer registro de ejemplo", tags=["demo", "docs"])
    Note.create(db, title="cache", body="Esta vista alimenta el ejemplo de cache", tags=["cache"])


def build_dns(host, port):
    return LocalDNSServer(
        host=host,
        port=port,
        ttl=60,
        records={
            "full-project.local": {"A": "127.0.0.1"},
            "api.full-project.local": {"A": "127.0.0.1"},
            "ws.full-project.local": {"TXT": "wsbuilder demo"},
        },
    )


def build_app(*, upstream_port=DEFAULT_UPSTREAM_PORT, enable_dns=False, dns_host=DEFAULT_DNS_HOST, dns_port=DEFAULT_DNS_PORT):
    ensure_dirs()

    app = App(cors_allow_origin="*")
    app.enable_metrics(app_name="full-project")
    app.enable_docs(
        path="/docs",
        json_path="/docs.json",
        title="full-project runtime docs",
        description="Ejemplo integral de wsbuilder con HTTP, WS, ORM, cache, seguridad, proxy e IA.",
    )
    app.enable_logs(path=LOG_DIR / "app.ndjson")

    install_cache(
        app,
        SQLiteMemoryCache(
            default_ttl=120,
            cleanup_interval_seconds=0,
            max_entries=512,
        ),
    )
    http_cache = ViewResponseCache(default_ttl=20)
    http_cache.add_global_rule(
        ttl_seconds=30,
        path_pattern="/pages/*",
        mimetype_pattern="text/html*",
        methods=("GET",),
        name="html-pages",
    )
    app.enable_caches(http_cache)

    policy = SecurityPolicy(
        rate_limit_requests=240,
        rate_limit_window_seconds=60.0,
        block_duration_seconds=30.0,
    )
    policy.deny(name="deny-admin-post", methods=("POST",), path="/api/admin")
    app.enable_security(policy=policy)

    app.db = Database(str(DB_PATH), enable_replicas=True, replica_count=2, enable_wal=True, cache_size_mb=10)
    Note.create_table(app.db)
    seed_notes(app.db)

    app.predictor = build_predictor()
    app.latest_network = build_network()
    app.latest_training_task_id = ""
    app.upstream_port = int(upstream_port)

    if enable_dns:
        app.dns = build_dns(dns_host, dns_port)

        def start_dns():
            app.dns.start()

        app.add_startup(start_dns)
    else:
        app.dns = None

    proxy = ProxyI(
        name="full-project-edge",
        dashboard_path="/proxy",
        metrics_path="/api/proxy/metrics",
        metrics_stream_path="/api/proxy/metrics/stream",
    )
    proxy.route(
        name="local-upstream",
        path="/api/proxy/upstream",
        methods=("GET",),
    ).upstream(
        f"http://127.0.0.1:{app.upstream_port}",
        name="demo-upstream",
        preserve_host=True,
    ).build()
    proxy.install(app)

    @app.view("/")
    def home(_request):
        return Response.html(
            "<h1>wsbuilder full-project</h1>"
            "<p>Demo integral con HTTP, WS, ORM, cache, seguridad, proxy e IA.</p>"
            "<ul>"
            "<li><a href='/docs'>/docs</a></li>"
            "<li><a href='/pages/overview'>/pages/overview</a></li>"
            "<li><a href='/api/notes'>/api/notes</a></li>"
            "<li><a href='/api/metrics'>/api/metrics</a></li>"
            "<li><a href='/api/proxy/upstream'>/api/proxy/upstream</a></li>"
            "<li><a href='/proxy'>/proxy</a></li>"
            "</ul>"
        )

    @app.view(
        "/pages/overview",
        min_threads=1,
        max_threads=3,
        requests_per_thread=6,
        cache={"ttl": 20, "vary_query": ["lang"]},
    )
    def overview(request):
        lang = request.query.get("lang", "es")
        notes_total = Note.objects(app.db).count()
        replica_total = app.db.read_replica_scalar("SELECT COUNT(*) FROM notes", default=0)
        return Response.html(
            "<h2>Resumen</h2>"
            f"<p>lang={lang}</p>"
            f"<p>thread={threading.current_thread().name}</p>"
            f"<p>notes_total={notes_total}</p>"
            f"<p>replica_total={replica_total}</p>"
            f"<p>cache_entries={app.cache.count()}</p>"
        )

    @app.api("/api/health")
    def health(_request):
        return {
            "ok": True,
            "notes_total": Note.objects(app.db).count(),
            "replica_total": app.db.read_replica_scalar("SELECT COUNT(*) FROM notes", default=0),
            "dns_enabled": bool(app.dns),
            "upstream_port": app.upstream_port,
            "thread_metrics": app.thread_metrics_snapshot(),
        }

    @app.api("/api/notes", methods=("GET", "POST"))
    def notes(request):
        if request.method == "GET":
            rows = [note_to_dict(note) for note in Note.objects(app.db).order_by("-id").all()]
            return {"items": rows, "total": len(rows)}

        payload = request.json() or {}
        title = str(payload.get("title", "")).strip()
        if not title:
            return Response.json({"status": "error", "message": "title is required"}, status=400)

        raw_tags = payload.get("tags", [])
        if isinstance(raw_tags, list):
            tags = [str(tag) for tag in raw_tags]
        elif raw_tags:
            tags = [str(raw_tags)]
        else:
            tags = []

        note = Note.create(
            app.db,
            title=title,
            body=str(payload.get("body", "")),
            tags=tags,
        )
        app.logs.event("note_created", note_id=note.id, title=note.title)
        if app.caches:
            app.caches.invalidate_path("/pages/overview")
        return Response.json(note_to_dict(note), status=201)

    @app.api("/api/cache/demo")
    def cache_demo(_request):
        counter = app.cache.incr("demo-counter", namespace="demo", initial=0)
        app.cache.set("last-counter", {"value": counter}, namespace="demo", tags=["demo", "cache"])
        return {
            "counter": counter,
            "demo_keys": app.cache.keys(namespace="demo"),
            "stats": app.cache.stats(),
        }

    @app.api("/api/ml/predict")
    def predict(request):
        x_value = float(request.query.get("x", "5"))
        prediction, sigma, lower, upper = app.predictor.predict([x_value])
        return {
            "x": x_value,
            "prediction": prediction[0],
            "sigma": sigma[0],
            "lower": lower[0],
            "upper": upper[0],
        }

    @app.api("/api/ml/dataset")
    def dataset_summary(_request):
        dataset = {
            "samples": len(XOR_X),
            "features": len(XOR_X[0]),
            "labels": list(XOR_LABELS),
        }
        return dataset

    @app.api("/api/tasks/train", methods=("POST",))
    def train_task(request):
        app.latest_network = build_network()
        task = submit_training_task(
            request.app.tasks,
            app.latest_network,
            XOR_X,
            XOR_LABELS,
            classification=True,
            epochs=1500,
            batch_size=4,
            shuffle=False,
            name="xor-training",
            group="ml",
            metadata={"kind": "xor"},
        )
        app.latest_training_task_id = task.id
        app.logs.event("task_started", task_id=task.id, group="ml")
        return {
            "task_id": task.id,
            "status": task.status,
        }

    @app.api("/api/tasks/status")
    def task_status(request):
        task_id = request.query.get("task_id", "") or app.latest_training_task_id
        if not task_id:
            return Response.json({"status": "error", "message": "task_id is required"}, status=400)
        task = app.tasks.get(task_id)
        if task is None:
            return Response.json({"status": "error", "message": "task not found"}, status=404)
        payload = task.snapshot()
        if task.finished() and app.latest_network is not None:
            payload["prediction_yes"] = app.latest_network.predict_class([1, 0])
        return payload

    @app.api("/api/proxy/upstream")
    def proxy_upstream(request):
        return app.proxyi.dispatch(request)

    @app.api("/api/dns/status")
    def dns_status(_request):
        if not app.dns:
            return {"enabled": False}
        return {
            "enabled": True,
            "host": app.dns.host,
            "port": app.dns.port,
            "default_ttl": app.dns.ttl,
        }

    @app.api("/api/admin", methods=("POST",))
    def admin(_request):
        return {"ok": True}

    @app.ws("/ws/echo", keepalive_interval=20.0, pong_timeout=10.0)
    def ws_echo(ws, _request):
        while True:
            frame = ws.recv_frame()
            if frame.opcode == 0x8:
                code, reason = parse_close_payload(frame.payload)
                ws.close(code or 1000, reason or "bye")
                break
            if frame.opcode == 0x9:
                ws.send_pong(frame.payload)
                continue
            if frame.opcode == 0x1:
                text = frame.payload.decode("utf-8", errors="ignore")
                ws.send_text(json.dumps({"echo": text}))
            elif frame.opcode == 0x2:
                ws.send_binary(frame.payload)

    return app


def shutdown_example(app):
    cache = getattr(app, "cache", None)
    if cache is not None:
        try:
            cache.close()
        except Exception:
            pass
    db = getattr(app, "db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass
    dns = getattr(app, "dns", None)
    if dns is not None:
        try:
            dns.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Proyecto integral de ejemplo para wsbuilder")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host de escucha")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Puerto de escucha")
    parser.add_argument(
        "--upstream-port",
        type=int,
        default=int(os.environ.get("WSB_FULL_DEMO_UPSTREAM_PORT", str(DEFAULT_UPSTREAM_PORT))),
        help="Puerto del upstream para la demo de ProxyI",
    )
    parser.add_argument("--dns-host", default=DEFAULT_DNS_HOST, help="Host para DNS opcional")
    parser.add_argument("--dns-port", type=int, default=DEFAULT_DNS_PORT, help="Puerto para DNS opcional")
    parser.add_argument(
        "--enable-dns",
        action="store_true",
        default=str(os.environ.get("WSB_FULL_DEMO_ENABLE_DNS", "")).lower() in {"1", "true", "yes", "on"},
        help="Activa el servidor DNS de ejemplo",
    )
    args = parser.parse_args()

    app = build_app(
        upstream_port=args.upstream_port,
        enable_dns=args.enable_dns,
        dns_host=args.dns_host,
        dns_port=args.dns_port,
    )
    try:
        app.run(args.host, args.port)
    finally:
        shutdown_example(app)


if __name__ == "__main__":
    main()

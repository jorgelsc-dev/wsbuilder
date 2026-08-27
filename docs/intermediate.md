# Guia para usuarios intermedios

Esta guia parte de una app HTTP basica y agrega las piezas que se usan en un
servicio interno real: persistencia, cache, seguridad, metricas, logs y tareas.

## Estructura recomendada

Para una app pequena, un solo archivo funciona bien:

```text
my_service/
├── app.py
├── data/
└── logs/
```

Cuando crece, separa por responsabilidad:

```text
my_service/
├── app.py
├── models.py
├── routes.py
├── services.py
└── settings.py
```

`wsbuilder` no impone esta estructura. Lo importante es que la instancia `App`
sea el punto donde se registran rutas e integraciones.

## Modelo y base SQLite

```python
from datetime import UTC, datetime
from wsbuilder import Database, DateTimeField, IntegerField, JSONField, Model, TextField

class Note(Model):
    __tablename__ = "notes"

    id = IntegerField(primary_key=True, auto_increment=True)
    title = TextField(unique=True, null=False, index=True)
    body = TextField(default="", null=False)
    tags = JSONField(default=list, null=False)
    created_at = DateTimeField(default=lambda: datetime.now(UTC), null=False)

db = Database("data/app.sqlite3", enable_wal=True, enable_replicas=True, replica_count=2)
Note.create_table(db)
```

Consultas frecuentes:

```python
Note.create(db, title="hola", tags=["demo"])
latest = Note.objects(db).order_by("-id").limit(10).all()
tagged = Note.objects(db).filter(title__icontains="ho").all()
count = Note.objects(db).using("replica").count()
```

Usa `transaction()` para operaciones que deben confirmarse juntas:

```python
with db.transaction():
    Note.create(db, title="a")
    Note.create(db, title="b")
```

## API CRUD simple

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")
app.db = db

@app.api("/api/notes", methods=("GET", "POST"))
def notes(request):
    if request.method == "GET":
        rows = [note.to_dict() for note in Note.objects(app.db).order_by("-id").all()]
        return {"items": rows, "total": len(rows)}

    payload = request.json() or {}
    title = str(payload.get("title", "")).strip()
    if not title:
        return Response.json({"message": "title is required"}, status=400)

    note = Note.create(
        app.db,
        title=title,
        body=str(payload.get("body", "")),
        tags=list(payload.get("tags", [])),
    )
    return Response.json(note.to_dict(), status=201)
```

## Cache clave-valor

Instala una cache para datos calculados, contadores o snapshots temporales:

```python
from wsbuilder import SQLiteMemoryCache, install_cache

app.cache = install_cache(
    app,
    SQLiteMemoryCache(default_ttl=120, cleanup_interval_seconds=0, max_entries=512),
)

@app.api("/api/cache/demo")
def cache_demo(_request):
    counter = app.cache.incr("counter", namespace="demo", initial=0)
    app.cache.set("last", {"counter": counter}, namespace="demo", tags=["demo"])
    return {"counter": counter, "stats": app.cache.stats()}
```

Usa tags cuando necesites invalidar grupos:

```python
app.cache.set("note:1", {"title": "hola"}, tags=["notes"])
app.cache.invalidate_tag("notes")
```

## Cache HTTP de vistas

`ViewResponseCache` trabaja sobre rutas `view`, no sobre rutas `api`.

```python
from wsbuilder import ViewResponseCache

http_cache = ViewResponseCache(default_ttl=20)
http_cache.add_global_rule(
    ttl_seconds=30,
    path_pattern="/pages/*",
    mimetype_pattern="text/html*",
    methods=("GET",),
    name="html-pages",
)
app.enable_caches(http_cache)

@app.view("/pages/overview", cache={"ttl": 20, "vary_query": ["lang"]})
def overview(request):
    lang = request.query.get("lang", "es")
    return f"lang={lang}"
```

Si una ruta cambia datos que afectan vistas cacheadas, invalida el path:

```python
app.caches.invalidate_path("/pages/overview")
```

## Seguridad basica

```python
from wsbuilder import SecurityPolicy

policy = SecurityPolicy(
    rate_limit_requests=240,
    rate_limit_window_seconds=60.0,
    block_duration_seconds=30.0,
)
policy.deny(name="deny-admin-post", methods=("POST",), path="/api/admin")
app.enable_security(policy=policy)
```

Para una politica cerrada, usa `acl_default="deny"` y agrega reglas `allow(...)`.

```python
policy = SecurityPolicy(acl_default="deny")
policy.allow(methods=("GET",), path_prefix="/api/public")
policy.allow(methods=("GET",), path="/api/health")
```

## Metricas, logs y docs runtime

```python
app.enable_metrics(app_name="notes-service")
app.enable_logs(path="logs/app.ndjson")
app.enable_docs(path="/docs", json_path="/docs.json")
```

Cada snapshot de metricas puede incluir:

- HTTP y conexiones TCP.
- WebSocket.
- workers por ruta.
- cache KV.
- cache HTTP.
- seguridad.
- proxy, si esta instalado.
- tareas en background.

## Tareas en background

```python
@app.api("/api/tasks/run", methods=("POST",))
def run_task(request):
    def work():
        return {"ok": True}

    task = request.app.tasks.spawn(work, name="demo", group="jobs", request=request)
    return {"task_id": task.id, "status": task.status}

@app.api("/api/tasks/status")
def task_status(request):
    task_id = request.query.get("task_id", "")
    task = request.app.tasks.get(task_id)
    if task is None:
        return Response.json({"message": "task not found"}, status=404)
    return task.snapshot()
```

La cancelacion es cooperativa. Si tu funcion tarda mucho, usa
`pass_handle=True` y revisa `handle.cancelled` dentro del trabajo.

```python
def work(handle):
    for step in range(100):
        if handle.cancelled:
            return {"cancelled_at": step}
    return {"ok": True}

task = app.tasks.spawn(work, pass_handle=True)
```

## Checklist para apps intermedias

- Usa `venv` y Python `3.11+`.
- Activa `enable_docs()` en entornos internos para verificar rutas.
- Devuelve `Response.json(..., status=...)` cuando necesites estados distintos a
  `200`.
- Usa `Database(..., enable_wal=True)` para archivos SQLite persistentes.
- Invalida cache HTTP despues de escrituras que afectan vistas.
- Configura `SecurityPolicy` antes de exponer rutas sensibles.
- Cierra recursos propios (`db`, DNS, caches externas) en tu flujo de apagado.

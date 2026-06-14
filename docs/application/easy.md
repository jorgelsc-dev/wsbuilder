# Aplicacion facil

Esta guia te deja una base pequena en un solo archivo. Copia el fichero, ejecuta el ejemplo y luego recorta lo que no necesites.

## Opciones incluidas

<div class="cards">
<div class="card"><strong>HTTP</strong>Ruta HTML principal con `Response.html()` y `Response.text()`.</div>
<div class="card"><strong>JSON</strong>Endpoints `api()` con respuestas estructuradas.</div>
<div class="card"><strong>Metricas</strong>Snapshot y stream listos para observar la app.</div>
<div class="card"><strong>Seguridad</strong>Rate limiting y base para ACL.</div>
<div class="card"><strong>Cache</strong>Cache local para respuestas repetidas.</div>
<div class="card"><strong>SQLite</strong>Modelo simple con `Database` y `Model`.</div>
<div class="card"><strong>Tareas</strong>`TaskManager` para trabajo en background.</div>
<div class="card"><strong>WebSocket</strong>Canal persistente opcional.</div>
</div>

## Fichero listo para copiar

Ruta sugerida: `src/myservice/app.py`

```python
from wsbuilder import (
    App,
    Database,
    IntegerField,
    Model,
    Response,
    SecurityPolicy,
    SQLiteMemoryCache,
    TaskManager,
    TextField,
    install_cache,
    install_metrics,
    install_security,
)

app = App(cors_allow_origin="*")
install_metrics(app, app_name="starter-service")
install_security(app, SecurityPolicy(rate_limit_requests=120, rate_limit_window_seconds=60))
install_cache(app, SQLiteMemoryCache(default_ttl=30))
tasks = TaskManager(app, max_concurrent=4)

db = Database("app.db")

class User(Model):
    id = IntegerField(primary_key=True, auto_increment=True)
    username = TextField(unique=True, index=True, null=False)
    email = TextField(null=False)

User.create_table(db)

@app.view("/")
def home(_request):
    return Response.html(
        """
        <h1>starter-service</h1>
        <ul>
          <li><a href="/api/health">/api/health</a></li>
          <li><a href="/api/users">/api/users</a></li>
        </ul>
        """
    )

@app.api("/api/health")
def health(_request):
    return {"ok": True}

@app.api("/api/users", methods=("GET",))
def users(_request):
    return [user.to_dict() for user in User.objects(db).all()]

@app.api("/api/users", methods=("POST",))
def create_user(request):
    payload = request.json() or {}
    user = User.create(
        db,
        username=payload.get("username", "guest"),
        email=payload.get("email", "guest@example.com"),
    )
    return {"created": user.to_dict()}

@app.api("/api/jobs", methods=("POST",))
def jobs(_request):
    task = tasks.spawn(lambda: "done", name="refresh-index")
    return {"task_id": task.id, "status": task.status}

@app.ws("/ws/")
def socket_handler(ws, _request):
    while True:
        frame = ws.recv_frame()
        if frame.opcode == 0x8:
            ws.close(1000, "bye")
            break
        if frame.opcode == 0x1:
            ws.send_text(frame.payload.decode("utf-8", errors="ignore"))

app.run("127.0.0.1", 8765)
```

Si quieres seguir creciendo, pasa a [Aplicacion avanzada](advanced.md).

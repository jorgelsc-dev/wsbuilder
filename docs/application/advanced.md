# Aplicacion avanzada

Esta guia es para una app ya operativa: seguridad, cache, tareas, replicas, proxy y DNS local.

## Opciones incluidas

<div class="cards">
<div class="card"><strong>DNS local</strong>Dominios de laboratorio para el entorno de pruebas.</div>
<div class="card"><strong>Proxy</strong>Vhosts, balanceo y destinos hacia otros servicios.</div>
<div class="card"><strong>Replicas</strong>Lecturas SQLite separadas de la escritura primaria.</div>
<div class="card"><strong>Seguridad</strong>ACL y rate limiting desde el borde de la app.</div>
<div class="card"><strong>Metricas</strong>Snapshot y stream para observabilidad.</div>
<div class="card"><strong>Cache</strong>Respuestas repetidas con TTL y namespace.</div>
<div class="card"><strong>Tareas</strong>Procesos en background sin bloquear la request.</div>
<div class="card"><strong>WebSocket</strong>Canal persistente para eventos en vivo.</div>
</div>

## Fichero listo para copiar

Ruta sugerida: `src/orders/app.py`

```python
from wsbuilder import (
    App,
    Database,
    IntegerField,
    LocalDNSServer,
    Model,
    ProxyI,
    Response,
    SecurityPolicy,
    SQLiteMemoryCache,
    TaskManager,
    TextField,
    install_cache,
    install_metrics,
    install_proxyi,
    install_security,
)

app = App(cors_allow_origin="https://dashboard.example.com")
install_metrics(app, app_name="orders-service")
install_cache(app, SQLiteMemoryCache(default_namespace="orders", default_ttl=20, max_entries=1000))
install_security(
    app,
    SecurityPolicy(
        acl_default="deny",
        trust_x_forwarded_for=False,
        rate_limit_requests=120,
        rate_limit_window_seconds=60,
    ),
)
tasks = TaskManager(app, max_concurrent=8)

dns = LocalDNSServer(
    records={
        "api.test.local": "127.0.0.1",
        "app.test.local": "127.0.0.1",
    },
    ttl=60,
)
dns.start()

db = Database("orders.db", enable_replicas=True, replica_count=2, cache_size_mb=32)

class OrderRecord(Model):
    id = IntegerField(primary_key=True, auto_increment=True)
    code = TextField(unique=True, index=True, null=False)
    status = TextField(index=True, null=False)

OrderRecord.create_table(db)

proxy = ProxyI(name="orders-gateway")
proxy.vhost("api.test.local", name="api") \
    .location("/api") \
    .balance("best") \
    .upstream("http://127.0.0.1:8080", name="api-primary", weight=2) \
    .upstream("http://127.0.0.1:8081", name="api-secondary", weight=1) \
    .build()
proxy.vhost("app.test.local", name="app") \
    .location("/") \
    .upstream("http://127.0.0.1:3000", name="frontend") \
    .build()
install_proxyi(app, proxy=proxy)

@app.api("/api/health")
def health(_request):
    return {"ok": True, "service": "orders"}

@app.api("/api/orders", methods=("GET",))
def list_orders(request):
    limit = int(request.query.get("limit", "20") or 20)
    rows = db.read_replica_fetchall(
        'SELECT id, code, status FROM "order_record" ORDER BY id DESC LIMIT ?',
        (limit,),
    )
    return {"items": [dict(row) for row in rows], "source": "read-replica"}

@app.api("/api/orders", methods=("POST",))
def create_order(request):
    payload = request.json() or {}
    order = OrderRecord.create(
        db,
        code=payload.get("code", "ORD-001"),
        status=payload.get("status", "new"),
    )
    task = tasks.spawn(lambda: "indexed", name="refresh-orders")
    return {"created": order.to_dict(), "task_id": task.id}

@app.api("/api/orders/reindex", methods=("POST",))
def reindex_orders(_request):
    task = tasks.spawn(lambda: "ok", name="reindex-orders")
    return {"queued": True, "task_id": task.id}

@app.ws("/ws/orders")
def orders_ws(ws, _request):
    while True:
        frame = ws.recv_frame()
        if frame.opcode == 0x8:
            ws.close(1000, "bye")
            break
        if frame.opcode == 0x1:
            ws.send_text(frame.payload.decode("utf-8", errors="ignore"))

@app.view("/")
def home(_request):
    return Response.html(
        "<h1>orders-service</h1>"
        "<p>HTTP, WebSocket, cache, replicas, proxy y DNS local en un solo archivo.</p>"
        "<p>Proxy dashboard: <code>/__proxyi__</code></p>"
        "<p>Metrics: <code>/api/metrics</code></p>"
    )

app.run("0.0.0.0", 8765)
```

## Ejemplos por escenario

<div class="cards">
<div class="card"><strong>Servicio publico</strong>Combina seguridad, cache y metrics desde el arranque.</div>
<div class="card"><strong>Topologia local</strong>Activa DNS y proxy para probar varios dominios en una sola maquina.</div>
<div class="card"><strong>Lecturas intensivas</strong>Usa replicas SQLite para separar escritura y consulta.</div>
</div>

Si no necesitas DNS local o proxy, elimina esos bloques y deja el resto.

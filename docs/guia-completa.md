# Guia completa

Esta pagina esta pensada para GitHub Pages y MkDocs Material. Usa tabs para que cada modulo tenga su espacio, y para que la tab `App` concentre una aplicacion completa que puedas estudiar de principio a fin.

## Como leerla

- Empieza por `App` si quieres ver como se conectan todas las piezas.
- Abre el tab del modulo que quieras aprender y luego salta a su pagina de referencia.
- Si solo quieres una pieza puntual, usa la referencia detallada en la seccion inferior.

## Orden sugerido

1. `App`
2. `HTTP`
3. `WebSocket`
4. `ORM`
5. `Cache`
6. `Seguridad`
7. `Metricas`
8. `Proxy`
9. `Tareas`
10. `DNS`
11. `Utilidades`
12. `Avanzado`

=== "App"
    ## Aplicacion completa

    Esta tab muestra una aplicacion de aprendizaje completa:

    - una app HTTP con rutas, docs y metricas;
    - seguridad y cache;
    - ORM con una base primaria de escritura y dos replicas de lectura;
    - proxy virtual host con dominios locales;
    - tareas en background;
    - WebSocket;
    - DNS local para los dominios de prueba.

    ### Mapa mental

    <div class="diagram">
    <div class="diagram-title">Aplicacion completa</div>
    <div class="diagram-track">
    <div class="diagram-node">DNS local</div>
    <div class="diagram-arrow">→</div>
    <div class="diagram-node">ProxyI</div>
    <div class="diagram-arrow">→</div>
    <div class="diagram-node">App</div>
    <div class="diagram-arrow">→</div>
    <div class="diagram-node">ORM + Cache + Seguridad</div>
    <div class="diagram-arrow">→</div>
    <div class="diagram-node">Metrics / Docs</div>
    </div>
    <div class="diagram-note" style="margin-top: 0.85rem;">La idea es aprender una topologia real, no solo un ejemplo aislado.</div>
    </div>

    ### Codigo completo

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
    install_metrics(app, app_name="academy-gateway")
    install_cache(app, SQLiteMemoryCache(default_namespace="academy", default_ttl=20))

    policy = SecurityPolicy(
        acl_default="allow",
        rate_limit_requests=180,
        rate_limit_window_seconds=60,
        trust_x_forwarded_for=False,
    )
    policy.allow(name="health", methods=("GET",), path="/api/health")
    policy.allow(name="api", methods=("GET", "POST"), path_prefix="/api/")
    policy.deny(name="admin-block", path_prefix="/admin")
    install_security(app, policy)

    tasks = TaskManager(app, max_concurrent=4)

    dns = LocalDNSServer(
        records={
            "api.test.local": "127.0.0.1",
            "auth.test.local": "127.0.0.1",
            "app.test.local": "127.0.0.1",
        },
        ttl=60,
    )
    dns.start()

    # Una sola base primaria para escritura y dos replicas de lectura.
    db = Database("academy.db", enable_replicas=True, replica_count=2, cache_size_mb=32)

    class User(Model):
        id = IntegerField(primary_key=True, auto_increment=True)
        username = TextField(unique=True, index=True, null=False)
        email = TextField(unique=True, index=True, null=False)
        role = TextField(index=True, null=False, default="viewer")

    User.create_table(db)

    proxy = ProxyI(name="academy-proxy")
    proxy.vhost("api.test.local", name="api-vhost") \
        .location("/api") \
        .header("x-env", equals="prod") \
        .balance("best") \
        .upstream("http://127.0.0.1:8080", name="api-primary", weight=2) \
        .upstream("http://127.0.0.1:8081", name="api-secondary", weight=1) \
        .build()

    proxy.vhost("auth.test.local", name="auth-vhost") \
        .location("/") \
        .upstream("http://127.0.0.1:8090", name="auth-service") \
        .build()

    proxy.vhost("app.test.local", name="app-vhost") \
        .location("/") \
        .upstream("http://127.0.0.1:8100", name="frontend") \
        .build()

    install_proxyi(app, proxy=proxy)

    @app.api("/api/health")
    def health(_request):
        return {"ok": True, "service": "academy-gateway"}

    @app.api("/api/users", methods=("GET",))
    def list_users(request):
        limit = int(request.query.get("limit", "20") or 20)
        # Las lecturas van al pool de replicas.
        rows = User.objects(db).using("replica").order_by("-id").limit(limit).values(
            "id", "username", "email", "role"
        )
        return {
            "items": rows,
            "source": "read-replica",
            "replicas": 2,
        }

    @app.api("/api/users", methods=("POST",))
    def create_user(request):
        payload = request.json() or {}
        # La escritura va a la conexion principal.
        user = User(
            username=payload.get("username", "guest"),
            email=payload.get("email", "guest@example.com"),
            role=payload.get("role", "viewer"),
        )
        user.save(db)
        return {"created": user.to_dict(), "source": "write-db"}

    @app.api("/api/users/reindex", methods=("POST",))
    def reindex_users(_request):
        task = tasks.spawn(lambda: "user index rebuilt", name="reindex-users", group="maintenance")
        return {"queued": True, "task_id": task.id}

    @app.ws("/ws/updates")
    def updates(ws, _request):
        while True:
            frame = ws.recv_frame()
            if frame.opcode == 0x8:
                ws.close(1000, "bye")
                break
            if frame.opcode == 0x9:
                ws.send_pong(frame.payload)
                continue
            if frame.opcode == 0x1:
                ws.send_text(frame.payload.decode("utf-8", errors="ignore"))

    @app.view("/")
    def home(_request):
        return Response.html(
            "<h1>academy-gateway</h1>"
            "<p>HTTP, WebSocket, proxy, ORM, cache, seguridad y metrics en un solo sitio.</p>"
            "<p>Proxy dashboard: <code>/__proxyi__</code></p>"
            "<p>Metrics: <code>/api/metrics</code></p>"
        )

    app.enable_docs(
        path="/docs",
        json_path="/docs.json",
        title="academy-gateway docs",
        description="Documentacion viva de una app completa con proxy, ORM y observabilidad.",
    )

    app.run("0.0.0.0", 8765)
    ```

    ### Que aprende esta app

    - `App` como centro de orquestacion.
    - `SecurityPolicy` para control de acceso y rate limiting.
    - `SQLiteMemoryCache` para cache de respuesta.
    - `Database` con una escritura primaria y dos replicas de lectura.
    - lectura por replicas con `User.objects(db).using("replica")`.
    - `ProxyI` para vhosts, locations, headers y balanceo `best`.
    - `TaskManager` para trabajo que no debe bloquear la request.
    - `LocalDNSServer` para dominios de prueba.
    - `enable_metrics()` y `enable_docs()` como panel operativo.

    ### Como usarla para aprender

    1. Arranca con la app y confirma `GET /api/health`.
    2. Inserta un usuario y luego lee con `User.objects(db).using("replica")` para ver el split write/read.
    3. Prueba `api.test.local`, `auth.test.local` y `app.test.local` para validar VHOST.
    4. Abre `GET /__proxyi__` para ver la capa de proxy y el balanceo.
    5. Abre `GET /api/metrics` para revisar el snapshot operativo.

=== "HTTP"
    ## HTTP y respuesta

    Este tab cubre la capa de entrada y salida:

    - `Request` representa method, path, headers, body y client.
    - `Response` construye texto, JSON, HTML o stream.
    - `parse_http_request()` y `send_http_response()` trabajan al nivel del protocolo.

    ### Ejemplo minimo

    ```python
    from wsbuilder import Request, Response

    def handler(request):
        name = request.query.get("name", "mundo")
        return Response.json({"hello": name, "path": request.path})
    ```

    ### Que debes recordar

    - `Response.text()` para texto plano.
    - `Response.json()` para payloads estructurados.
    - `Response.html()` para paginas renderizadas.
    - `Response.stream()` para salida incremental.

    ### Mas detalle

    Ve la pagina [HTTP y respuesta](reference/http.md).

=== "WebSocket"
    ## WebSocket

    Este tab cubre handshake, frames y ciclo de vida:

    - `handshake_websocket()` y `handshake_websocket_with_options()`.
    - `WebSocket` para sesiones persistentes.
    - `WebSocketFrame` para leer opcodes y payloads.

    ### Ejemplo minimo

    ```python
    from wsbuilder import App, parse_close_payload

    app = App()

    @app.ws("/ws/")
    def ws_handler(ws, _request):
        while True:
            frame = ws.recv_frame()
            if frame.opcode == 0x8:
                code, reason = parse_close_payload(frame.payload)
                ws.close(code or 1000, reason or "")
                break
            if frame.opcode == 0x9:
                ws.send_pong(frame.payload)
                continue
            if frame.opcode == 0x1:
                ws.send_text(frame.payload.decode("utf-8", errors="ignore"))
    ```

    ### Mas detalle

    Ve la pagina [WebSocket](reference/websocket.md).

=== "ORM"
    ## ORM

    Este tab cubre `Database`, `Model`, `QuerySet` y `Transaction`.

    ### Escritura y replicas

    ```python
    from wsbuilder import Database, IntegerField, Model, TextField

    db = Database("academy.db", enable_replicas=True, replica_count=2)

    class User(Model):
        id = IntegerField(primary_key=True, auto_increment=True)
        username = TextField(unique=True, index=True, null=False)
        email = TextField(unique=True, index=True, null=False)

    User.create_table(db)
    User.create(db, username="alice", email="alice@example.com")

    rows = User.objects(db).using("replica").order_by("-id").values("id", "username", "email")
    ```

    ### Puntos clave

    - `db.execute(...)` escribe sobre la conexion principal.
    - `User.objects(db).using("replica")` lee desde el pool de replicas cuando existe.
    - `User.objects(db).filter(...)` construye consultas expresivas.
    - `with db.transaction():` agrupa escrituras.

    ### Mas detalle

    Ve la pagina [ORM](reference/orm.md).

=== "Cache"
    ## Cache

    Este tab cubre cache en memoria con SQLite:

    - `SQLiteMemoryCache` para TTL, namespaces y tags.
    - `install_cache()` para instalar una cache principal.
    - `install_caches()` para reglas globales y cache HTTP.

    ### Ejemplo minimo

    ```python
    from wsbuilder import App, SQLiteMemoryCache, install_cache

    app = App()
    cache = SQLiteMemoryCache(default_namespace="academy", default_ttl=20)
    install_cache(app, cache)
    ```

    ### Cuando usarlo

    - respuestas HTML repetidas;
    - endpoints JSON que no cambian a cada segundo;
    - vistas pesadas que quieres servir mas rapido.

    ### Mas detalle

    Ve la pagina [Cache](reference/cache.md).

=== "Seguridad"
    ## Seguridad

    Este tab cubre ACL, listas blanca/negra, rate limiting y bloqueos.

    ### Ejemplo minimo

    ```python
    from wsbuilder import App, SecurityPolicy, install_security

    app = App()
    policy = SecurityPolicy(rate_limit_requests=180, rate_limit_window_seconds=60)
    policy.allow(name="health", methods=("GET",), path="/api/health")
    policy.deny(name="admin", path_prefix="/admin")
    install_security(app, policy)
    ```

    ### Lo que puedes combinar

    - `path`, `path_prefix` y `path_regex`.
    - `methods`.
    - headers exactos, contains o regex.
    - IPs y TLS.

    ### Mas detalle

    Ve la pagina [Seguridad](reference/security.md).

=== "Metricas"
    ## Metricas

    Este tab cubre `AppMetrics` y el stream NDJSON.

    ### Ejemplo minimo

    ```python
    from wsbuilder import App

    app = App()
    app.enable_metrics(app_name="academy-gateway")
    ```

    ### Que expone

    - `GET /api/metrics`
    - `GET /api/metrics/stream`
    - `threads`, `security`, `cache` y `proxyi` si estan activos.

    ### Lo importante

    - contador de requests y respuestas;
    - latencia media y maxima;
    - bytes in/out;
    - errores y estado actual.

    ### Mas detalle

    Ve la pagina [Metricas](reference/metrics.md).

=== "Proxy"
    ## Proxy / VHost

    Este tab cubre `ProxyI`, `ProxyRule` y `ProxyTarget`.

    ### Ejemplo minimo

    ```python
    from wsbuilder import ProxyI

    proxy = ProxyI(name="gateway")
    proxy.vhost("api.test.local", name="api") \
        .location("/api") \
        .header("x-env", equals="prod") \
        .balance("best") \
        .upstream("http://127.0.0.1:8080", name="api-primary") \
        .upstream("http://127.0.0.1:8081", name="api-secondary") \
        .build()
    ```

    ### Lo que soporta

    - vhosts por host exacto o glob.
    - locations por path, prefijo, contiene o regex.
    - headers exactos, contains o regex.
    - balanceo round robin, pesos, latencia y score.
    - metrics y dashboard nativos.

    ### Mas detalle

    Ve la pagina [Proxy / VHost](reference/proxyi.md).

=== "Tareas"
    ## Tareas en background

    Este tab cubre `TaskManager`, `TaskHandle` y `TaskContext`.

    ### Ejemplo minimo

    ```python
    from wsbuilder import App, TaskManager

    app = App()
    tasks = TaskManager(app, max_concurrent=4)

    def rebuild_index():
        return "ok"

    task = tasks.spawn(rebuild_index, name="rebuild-index", group="maintenance")
    ```

    ### Cuando usarlo

    - reportes;
    - rebuilds;
    - notificaciones internas;
    - trabajo que puede seguir despues de responder.

    ### Mas detalle

    Ve la pagina [Tareas en background](reference/tasks.md).

=== "DNS"
    ## DNS local

    Este tab cubre `LocalDNSServer`.

    ### Ejemplo minimo

    ```python
    from wsbuilder import LocalDNSServer

    dns = LocalDNSServer(records={
        "api.test.local": "127.0.0.1",
        "auth.test.local": "127.0.0.1",
        "app.test.local": "127.0.0.1",
    })
    dns.start()
    ```

    ### Cuando usarlo

    - laboratorios locales;
    - dominios de prueba para el proxy;
    - pruebas sin infraestructura externa.

    ### Mas detalle

    Ve la pagina [DNS local](reference/dns.md).

=== "Utilidades"
    ## Utilidades HTTP

    Este tab cubre helpers de headers y cookies.

    ### Ejemplo minimo

    ```python
    from wsbuilder import build_set_cookie, get_header, parse_cookie_header, set_header

    headers = {}
    set_header(headers, "X-Env", "prod")
    cookie_map = parse_cookie_header("sid=abc123; theme=dark")
    cookie = build_set_cookie("sid", "abc123", http_only=True, secure=True)
    ```

    ### Mas detalle

    Ve la pagina [Utilidades HTTP](reference/utilities.md).

=== "Avanzado"
    ## Avanzado

    Este tab cubre piezas para servicios mas finos:

    - `OptimizedDatabase`
    - `SQLite3OptimizationConfig`
    - `DatabaseReplica`
    - `DatabaseReplicaPool`
    - `Predictor`

    ### Ejemplo minimo

    ```python
    from wsbuilder import OptimizedDatabase, SQLite3OptimizationConfig

    db = OptimizedDatabase(
        "academy.db",
        optimization_config=SQLite3OptimizationConfig(),
        enable_replicas=True,
        replica_count=2,
    )
    ```

    ### Cuando usarlo

    - si quieres ajustar pragmas de SQLite;
    - si quieres separar mejor lectura y escritura;
    - si necesitas utilidades avanzadas fuera del flujo comun.

    ### Mas detalle

    Ve la pagina [Avanzado](reference/advanced.md).

## Referencia rapida

- [HTTP y respuesta](reference/http.md)
- [WebSocket](reference/websocket.md)
- [ORM](reference/orm.md)
- [Cache](reference/cache.md)
- [Seguridad](reference/security.md)
- [Metricas](reference/metrics.md)
- [Proxy / VHost](reference/proxyi.md)
- [Tareas en background](reference/tasks.md)
- [Utilidades HTTP](reference/utilities.md)
- [DNS local](reference/dns.md)
- [Avanzado](reference/advanced.md)

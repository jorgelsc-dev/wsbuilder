# Aplicacion avanzada

Esta guia es para cuando la app ya no es un demo. Aqui entran decisiones de arquitectura, tuning de workers, observabilidad, seguridad, cache, tareas, WebSocket y SQLite optimizado.

## Cuando usarla

Usa esta guia si tu aplicacion ya tiene al menos una de estas condiciones:

- varios equipos tocando el codigo;
- trafico concurrente real;
- endpoints caros o con cache;
- tareas de fondo;
- WebSocket persistente;
- datos locales con SQLite que necesitan cuidado;
- necesidad de diagnostico y trazabilidad.

## Modelo mental

La regla es separar responsabilidades:

- `HTTPServer` transporta bytes.
- `App` decide y orquesta.
- `SecurityPolicy` filtra.
- `ViewResponseCache` evita recomputar.
- `TaskManager` saca trabajo pesado del camino.
- `Database` y `Model` concentran datos.
- `AppMetrics` cuenta y mide.

<div class="diagram">
<div class="diagram-title">Capas de una app seria</div>
<div class="diagram-track">
<div class="diagram-node">Cliente</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">HTTPServer</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">App</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Capas transversales</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Handler</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">La clave no es meter mas cosas, sino decidir en que capa vive cada responsabilidad.</div>
</div>

## Estructura recomendada

```text
src/
  myservice/
    __init__.py
    app.py
    models.py
    tasks.py
    security.py
    metrics.py
docs/
  application/
  architecture.md
tests/
```

### Regla de oro

- handlers finos;
- logica de dominio en modulos propios;
- configuracion centralizada;
- nada de estado global innecesario;
- cada servicio con su propia base y su propio cache.

## Base completa de arranque

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

app = App(cors_allow_origin="https://example.com")
install_metrics(app, app_name="orders-service")
install_security(
    app,
    SecurityPolicy(
        rate_limit_requests=240,
        rate_limit_window_seconds=60,
        acl_default="allow",
    ),
)
install_cache(app, SQLiteMemoryCache(default_ttl=20))
tasks = TaskManager(app, max_concurrent=8)

db = Database(
    "orders.db",
    enable_replicas=True,
    replica_count=2,
    cache_size_mb=32,
)

class Order(Model):
    id = IntegerField(primary_key=True, auto_increment=True)
    code = TextField(unique=True, index=True, null=False)
    status = TextField(index=True, null=False)

Order.create_table(db)

@app.api("/health")
def health(_request):
    return {"ok": True, "service": "orders"}

@app.view("/")
def home(_request):
    return Response.html("<h1>orders-service</h1>")
```

## Tuning de rutas `view()`

Las rutas `view()` pueden usar workers por ruta. Esto sirve cuando una pagina o calculo pesado no debe bloquear las demas.

### Cuandos subir workers

- si la ruta hace trabajo CPU/IO pesado;
- si necesitas aislar latencia por endpoint;
- si quieres una cola local y controlada por ruta;
- si quieres mantener lecturas rapidas aunque haya una ruta costosa.

### Ejemplo

```python
@app.view("/report", min_threads=1, max_threads=4, requests_per_thread=2)
def report(_request):
    return Response.html("<p>Reporte listo</p>")
```

### Regla practica

- `min_threads=1` si quieres calentar un worker desde el inicio.
- `max_threads>1` si esperas concurrencia.
- `requests_per_thread` bajo si quieres repartir carga rapido.
- `requests_per_thread` alto si prefieres menos cambios de worker.

## Cache avanzada

El cache de `view()` funciona bien cuando:

- la respuesta no cambia muy seguido;
- la variacion depende de headers, query o ruta;
- quieres bajar latencia en paginas repetidas;
- tienes respuestas HTML o texto costosas de generar.

### Recomendacion

- usa TTL corto para contenido dinamico;
- usa TTL largo para vistas estables;
- invalida por cambios de negocio, no por intuicion;
- evita cachear respuestas con datos sensibles o por usuario si no tienes clave correcta.

### Patron

```python
from wsbuilder import SQLiteMemoryCache, install_cache

cache = SQLiteMemoryCache(default_namespace="orders-ui", default_ttl=15, max_entries=1000)
install_cache(app, cache)
```

## Seguridad avanzada

La seguridad no deberia vivir solo en el gateway. Esta capa te deja reforzar el borde de cada app.

### Lo que puedes hacer

- `allow()` y `deny()` por ruta, metodo, IP o regex;
- listas blancas y negras;
- rate limiting por IP;
- bloqueo temporal por violaciones;
- validacion de cabeceras o TLS.

### Ejemplo de politica

```python
from wsbuilder import ACLRule, SecurityPolicy, install_security

policy = SecurityPolicy(
    acl_default="deny",
    trust_x_forwarded_for=False,
    rate_limit_requests=120,
    rate_limit_window_seconds=60,
)

policy.allow(name="health", methods=("GET",), path="/health")
policy.allow(name="api", methods=("GET", "POST"), path_prefix="/api/")
policy.deny(name="admin-block", path_prefix="/admin")

install_security(app, policy)
```

### Criterio

- si el servicio es publico, no confies en defaults;
- si hay un gateway, aun asi protege el servicio;
- si hay cabeceras o IPs importantes, documenta el contrato;
- si una regla es temporal, deja claro por que existe.

## Metricas y operacion

`AppMetrics` te da una vista de:

- conexiones activas;
- requests HTTP;
- rutas mas usadas;
- latencia media y maxima;
- sesiones WebSocket;
- bytes entrantes y salientes;
- errores.

### Buenas practicas

- activa metricas desde el arranque;
- usa nombres estables para `app_name`;
- observa tendencias, no solo valores puntuales;
- revisa rutas calientes y respuestas lentas;
- agrega extra snapshot providers si tienes datos propios de runtime.

### Ejemplo

```python
install_metrics(app, path="/metrics", stream_path="/metrics/stream", app_name="orders-service")
```

## Proxy y balanceo

`ProxyI` te deja declarar vhosts y destinos con una sintaxis cercana a nginx, pero integrada con el resto del paquete.

### Ejemplo minimo

```python
from wsbuilder import App, ProxyI, install_proxyi

app = App()
proxy = ProxyI(name="gateway")

proxy.vhost("api.test.local").location("/api").header("x-env", equals="prod").upstream(
    "http://127.0.0.1:8080",
    name="api-backend",
)

install_proxyi(app, proxy=proxy)
app.proxyi = proxy
```

### Que guarda

- requests y responses por target;
- bytes in y out;
- latencia media;
- desviacion estandar;
- incertidumbre y rango de confianza al 95%;
- score compuesto para seleccionar el mejor destino.

### Balanceo disponible

- round robin;
- round robin ponderado;
- random;
- least connections;
- least response time;
- least requests;
- least bytes in;
- least bytes out;
- ip hash;
- consistent hash;
- first available;
- power of two choices;
- best.

### Area de metricas

El proxy expone:

- `GET /__proxyi__`
- `GET /__proxyi__/metrics`
- `GET /__proxyi__/metrics/stream`

`App.enable_metrics()` y `App.enable_docs()` incluyen el bloque `proxyi` cuando la instancia esta adjunta a la aplicacion.

## Tareas en background

`TaskManager` sirve para trabajo local que no quieres ejecutar dentro del handler.

### Usa tareas para

- recomputar materializados;
- reconstruir indices o caches;
- notificar sistemas internos;
- generar reportes;
- hacer trabajo que puede terminar despues de la respuesta.

### No lo uses para

- coordinar procesos distribuidos;
- reemplazar una cola externa si la necesitas;
- esconder latencia larga que deberia ser una operacion explicita.

### Ejemplo

```python
def rebuild_cache():
    return "cache rebuilt"

handle = tasks.spawn(rebuild_cache, name="rebuild-cache", group="maintenance")
```

### Operacion

- pon `max_concurrent` si quieres limitar el impacto;
- agrupa tareas relacionadas con `group`;
- consulta `tasks.snapshot()` para diagnostico;
- cancela grupos completos cuando sea necesario.

## WebSocket en produccion

Usa WebSocket cuando el estado en vivo aporte algo real:

- chat;
- presencia;
- consola remota;
- panel de eventos;
- telemetria push.

### Buenas practicas

- define timeouts de idle y pong;
- documenta subprotocolos si los usas;
- cierra con codigo y razon cuando corresponda;
- controla mensajes entrantes y salientes;
- no dejes conexiones abiertas sin estrategia.

### Ejemplo

```python
@app.ws("/ws/live", keepalive_interval=20, pong_timeout=10, idle_timeout=120)
def live_socket(ws, request):
    ws.send_text("ready")
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
```

## SQLite optimizado y replicas

Si tu app escribe poco y lee mucho, el modulo de replicas y optimizacion te ayuda a separar preocupaciones.

### Cuando vale la pena

- lectura intensa;
- consultas repetidas;
- varias rutas concurrentes;
- base local con latencia sensible;
- necesidad de pragmas y WAL cuidados.

### Con `Database`

```python
db = Database(
    "service.db",
    enable_replicas=True,
    replica_count=3,
    cache_size_mb=64,
    enable_wal=True,
)
```

### Con `OptimizedDatabase`

Si tu caso necesita configuracion explicita de optimizacion, usa `OptimizedDatabase` y `SQLite3OptimizationConfig` para declarar el objetivo en vez de asumir defaults.

### Criterio

- no actives replicas si tu servicio es muy chico y no las necesita;
- si las activas, mide antes y despues;
- documenta que operaciones son de lectura y cuales de escritura;
- evita usar este modulo como excusa para no modelar bien el dominio.

## Despliegue

### Antes de ponerlo en produccion

- confirma `GET /health`;
- confirma `GET /metrics`;
- prueba la ruta de error;
- prueba cierres limpios;
- revisa tiempo de arranque;
- valida configuracion de CORS;
- mide el impacto del cache y de las tareas;
- verifica que el service no dependa de estado local efimero para funcionar.

### Recomendacion de proceso

- un proceso por servicio;
- un `Database` por servicio;
- cache local por servicio;
- seguridad local por servicio;
- metricas locales por servicio.

## Ejemplo final

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

app = App(cors_allow_origin="https://dashboard.example.com")
install_metrics(app, app_name="billing-service")

policy = SecurityPolicy(
    acl_default="deny",
    rate_limit_requests=180,
    rate_limit_window_seconds=60,
)
policy.allow(name="health", methods=("GET",), path="/health")
policy.allow(name="api", methods=("GET", "POST"), path_prefix="/api/")
install_security(app, policy)

install_cache(app, SQLiteMemoryCache(default_namespace="billing", default_ttl=20))
tasks = TaskManager(app, max_concurrent=4)

db = Database("billing.db", enable_replicas=True, replica_count=2)

class Invoice(Model):
    id = IntegerField(primary_key=True, auto_increment=True)
    number = TextField(unique=True, index=True, null=False)
    status = TextField(index=True, null=False)

Invoice.create_table(db)

@app.api("/health")
def health(_request):
    return {"ok": True}

@app.api("/api/invoices")
def invoices(_request):
    return {"items": []}

@app.view("/admin")
def admin(_request):
    return Response.html("<h1>Admin</h1>")

@app.ws("/ws/updates")
def updates(ws, _request):
    ws.send_text("ready")

app.run("0.0.0.0", 8765)
```

## Documentacion nativa

`wsbuilder` puede exponer una vista automatica parecida a Swagger sin depender de un paquete externo. La idea es simple:

- activas una URL publica;
- el framework inspecciona rutas HTTP, WebSocket, seguridad, cache, metricas y tareas;
- obtienes una vista HTML y un JSON automatico sincronizados con la app real.

### Activacion

```python
from wsbuilder import App

app = App(cors_allow_origin="https://dashboard.example.com")
app.enable_docs(path="/docs", json_path="/docs.json")
```

### Que expone

- `GET /docs` para la vista HTML;
- `GET /docs.json` para el payload estructurado;
- listado de rutas `view()` y `api()`;
- rutas `ws()` con timeouts y subprotocolos;
- informacion de seguridad, cache, metrics y tareas;
- resumen de estado de la aplicacion en tiempo real.

### Cuando usarlo

- si quieres explorar la API viva sin mantener documentacion manual duplicada;
- si operas varios servicios y cada uno necesita su propia superficie visible;
- si quieres un panel nativo que puedas abrir por URL y que siempre refleje el estado actual.

## Ejemplos por escenario

=== "MS"

    Este ejemplo muestra una implementacion avanzada separada por servicios: un gateway publico y servicios de dominio autonomos. Cada servicio tiene su propia seguridad, metricas, cache y base SQLite.

    ### Gateway

    ```python
    from wsbuilder import App, Response, install_metrics

    gateway = App(cors_allow_origin="https://dashboard.example.com")
    install_metrics(gateway, app_name="gateway")

    @gateway.api("/health")
    def gateway_health(_request):
        return {"ok": True, "service": "gateway"}

    @gateway.api("/api/auth/login", methods=("POST",))
    def auth_proxy(_request):
        return {"service": "auth-service", "action": "login"}

    @gateway.api("/api/orders", methods=("GET", "POST"))
    def orders_proxy(_request):
        return {"service": "orders-service"}

    @gateway.view("/")
    def home(_request):
        return Response.html("<h1>Gateway</h1><p>Entrada publica del sistema.</p>")

    gateway.run("0.0.0.0", 8080)
    ```

    ### Auth service

    ```python
    from wsbuilder import (
        App,
        Database,
        IntegerField,
        Model,
        SecurityPolicy,
        SQLiteMemoryCache,
        TaskManager,
        TextField,
        install_cache,
        install_metrics,
        install_security,
    )

    auth = App(cors_allow_origin="https://dashboard.example.com")
    install_metrics(auth, app_name="auth-service")
    install_security(
        auth,
        SecurityPolicy(
            acl_default="deny",
            rate_limit_requests=90,
            rate_limit_window_seconds=60,
        ),
    )
    install_cache(auth, SQLiteMemoryCache(default_namespace="auth", default_ttl=10))
    auth_tasks = TaskManager(auth, max_concurrent=2)

    db = Database("auth.db", enable_replicas=True, replica_count=2, cache_size_mb=16)

    class User(Model):
        id = IntegerField(primary_key=True, auto_increment=True)
        email = TextField(unique=True, index=True, null=False)
        password_hash = TextField(null=False)

    User.create_table(db)

    @auth.api("/health")
    def health(_request):
        return {"ok": True, "service": "auth-service"}

    @auth.api("/api/login", methods=("POST",))
    def login(_request):
        def issue_token():
            return "token-ready"

        task = auth_tasks.spawn(issue_token, name="issue-token")
        return {"queued": True, "task_id": task.id}

    @auth.api("/api/users")
    def users(_request):
        return {"items": []}
    ```

    ### Orders service

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

    orders = App(cors_allow_origin="https://dashboard.example.com")
    install_metrics(orders, app_name="orders-service")
    install_security(
        orders,
        SecurityPolicy(
            acl_default="deny",
            rate_limit_requests=180,
            rate_limit_window_seconds=60,
        ),
    )
    install_cache(orders, SQLiteMemoryCache(default_namespace="orders", default_ttl=20))
    orders_tasks = TaskManager(orders, max_concurrent=4)

    db = Database("orders.db", enable_replicas=True, replica_count=3, cache_size_mb=32)

    class Order(Model):
        id = IntegerField(primary_key=True, auto_increment=True)
        code = TextField(unique=True, index=True, null=False)
        status = TextField(index=True, null=False)

    Order.create_table(db)

    @orders.api("/health")
    def health(_request):
        return {"ok": True, "service": "orders-service"}

    @orders.api("/api/orders")
    def list_orders(_request):
        rows = []
        for row in Order.objects(db).limit(50):
            rows.append(
                {
                    "id": row.id,
                    "code": row.code,
                    "status": row.status,
                }
            )
        return {"items": rows}

    @orders.view("/report", min_threads=1, max_threads=4, requests_per_thread=2)
    def report(_request):
        return Response.html("<h1>Reporte de pedidos</h1>")
    ```

    ### Billing service

    ```python
    from wsbuilder import (
        App,
        Database,
        IntegerField,
        Model,
        SecurityPolicy,
        SQLiteMemoryCache,
        TaskManager,
        TextField,
        install_cache,
        install_metrics,
        install_security,
    )

    billing = App(cors_allow_origin="https://dashboard.example.com")
    install_metrics(billing, app_name="billing-service")
    install_security(
        billing,
        SecurityPolicy(
            acl_default="deny",
            rate_limit_requests=120,
            rate_limit_window_seconds=60,
        ),
    )
    install_cache(billing, SQLiteMemoryCache(default_namespace="billing", default_ttl=15))
    billing_tasks = TaskManager(billing, max_concurrent=2)

    db = Database("billing.db", enable_replicas=True, replica_count=2, cache_size_mb=16)

    class Invoice(Model):
        id = IntegerField(primary_key=True, auto_increment=True)
        number = TextField(unique=True, index=True, null=False)
        status = TextField(index=True, null=False)

    Invoice.create_table(db)

    @billing.api("/health")
    def health(_request):
        return {"ok": True, "service": "billing-service"}

    @billing.api("/api/invoices")
    def invoices(_request):
        return {"items": []}

    @billing.api("/api/reconcile", methods=("POST",))
    def reconcile(_request):
        task = billing_tasks.spawn(lambda: "reconciled", name="reconcile")
        return {"queued": True, "task_id": task.id}
    ```

    ### Lo importante

    - cada servicio tiene su propia base;
    - cada servicio expone `health` y `metrics`;
    - cada servicio puede exponer tambien `docs` y `docs.json` de forma nativa;
    - el gateway no contiene logica de dominio pesada;
    - las tareas largas no bloquean la request;
    - la seguridad se aplica en cada borde;
    - el cache y las replicas se ajustan por servicio, no globalmente.

=== "Servicio unico"

    Si tu despliegue no es distribuido todavia, usa una sola app pero conserva las mismas capas internas.

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

    app = App(cors_allow_origin="https://dashboard.example.com")
    install_metrics(app, app_name="monolith-service")
    install_security(app, SecurityPolicy(rate_limit_requests=180, rate_limit_window_seconds=60))
    install_cache(app, SQLiteMemoryCache(default_namespace="monolith", default_ttl=20))
    tasks = TaskManager(app, max_concurrent=4)

    db = Database("monolith.db", enable_replicas=True, replica_count=2)

    class Item(Model):
        id = IntegerField(primary_key=True, auto_increment=True)
        name = TextField(unique=True, index=True, null=False)

    Item.create_table(db)

    @app.api("/health")
    def health(_request):
        return {"ok": True}

    @app.view("/")
    def home(_request):
        return Response.html("<h1>Monolith</h1>")
    ```

## Checklist final

- [ ] Las responsabilidades estan separadas por capa.
- [ ] La seguridad no depende solo del borde externo.
- [ ] Las metricas estan activas.
- [ ] Las tareas pesadas no bloquean la request.
- [ ] El cache tiene una politica clara.
- [ ] WebSocket se usa solo donde aporta valor.
- [ ] SQLite esta configurado con la estrategia correcta.
- [ ] Existe un plan para crecimiento, diagnostico y rollback.

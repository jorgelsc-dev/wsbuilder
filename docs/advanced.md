# Guia para usuarios avanzados

Esta guia cubre decisiones de operacion, limites y composicion avanzada. Esta
pensada para servicios internos, laboratorios de red, herramientas de borde y
demos con varias capas activas.

## Configurar el servidor HTTP

`app.run(host, port, ssl_context=None)` crea un `HTTPServer`. Si necesitas tocar
limites de transporte, instancia el servidor:

```python
from wsbuilder import App, HTTPServer

app = App()
server = HTTPServer("127.0.0.1", 8765, app)
server.MAX_CONNECTION_WORKERS = 128
server.MAX_REQUEST_HEADER_BYTES = 128 * 1024
server.MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
server.REQUEST_READ_TIMEOUT_SECONDS = 15.0
server.serve_forever()
```

El servidor valida:

- HTTP/1.0 y HTTP/1.1.
- `Host` requerido en HTTP/1.1.
- duplicados peligrosos como `Content-Length` y `Host`.
- combinacion ambigua de `Content-Length` y `Transfer-Encoding`.
- cuerpos incompletos, demasiado grandes o sin framing.
- `Expect: 100-continue` solo con `Content-Length`.

## TLS

`HTTPServer` acepta un `ssl_context` ya construido:

```python
import ssl
from wsbuilder import HTTPServer

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("cert.pem", "key.pem")

HTTPServer("0.0.0.0", 8443, app, ssl_context=ctx).serve_forever()
```

`Request.tls` queda poblado con `enabled`, `peer_cert`, `cipher` y `version`.

## Worker pools por ruta

Las rutas `view` pueden ejecutarse en un pool dedicado. Sirve para trabajo CPU o
I/O pesado que quieres aislar de rutas directas.

```python
@app.view(
    "/reports",
    min_threads=1,
    max_threads=4,
    requests_per_thread=8,
    worker_timeout_seconds=2.0,
    affinity_ttl_seconds=900,
)
def reports(_request):
    return "ready"
```

Seleccion de worker:

- Se respeta afinidad por cookie firmada si el worker todavia tiene capacidad.
- Si no hay afinidad valida, se elige el worker menos cargado.
- El pool puede crecer hasta `max_threads`.
- Si el handler ya empezo, el timeout no lo interrumpe por fuerza; evita trabajos
  no acotados dentro de rutas.

Configura `thread_cookie_secret` con un valor privado y estable cuando uses
afinidad en produccion.

## Seguridad detras de proxies

No confies en `X-Forwarded-For` salvo que controles el proxy directo. Usa
`trusted_proxy_cidrs`:

```python
from wsbuilder import SecurityPolicy

policy = SecurityPolicy(
    trust_x_forwarded_for=True,
    trusted_proxy_cidrs=("10.0.0.0/8", "192.168.0.0/16"),
    rate_limit_requests=600,
    rate_limit_window_seconds=60,
)
app.enable_security(policy)
```

La IP se resuelve desde la derecha de la cadena `X-Forwarded-For`, descartando
saltos confiables. Si el peer directo no pertenece a una red confiable, se ignora
la cabecera.

## ProxyI como reverse proxy

`ProxyI` puede instalar metricas y dashboard, pero el forwarding real ocurre
cuando una ruta llama `app.proxyi.dispatch(request)`.

```python
from wsbuilder import ProxyI

proxy = ProxyI(
    name="edge",
    default_balance="least_response_time",
    max_request_body_bytes=2 * 1024 * 1024,
)
proxy.vhost("api.example.local", name="api").location("/api").balance("round_robin").upstream(
    "http://127.0.0.1:9001",
    name="api-1",
).upstream(
    "http://127.0.0.1:9002",
    name="api-2",
).build()
proxy.install(
    app,
    metrics_path="/api/proxy/metrics",
    stream_path="/api/proxy/metrics/stream",
    dashboard_path="/proxy",
)

@app.api("/api/upstream")
def upstream(request):
    return app.proxyi.dispatch(request)
```

Puntos importantes:

- `verify_tls=True` por defecto en targets HTTPS.
- headers hop-by-hop se eliminan antes de reenviar.
- `X-Forwarded-*` se reconstruye desde el peer, host y TLS reales.
- headers sensibles en snapshots se redactan.
- targets `enabled=False` no participan en balanceo.
- los snapshots no avanzan round-robin.

## DNS local avanzado

`LocalDNSServer` soporta records en formato nombrado y plano:

```python
from wsbuilder import LocalDNSServer

dns = LocalDNSServer(
    host="127.0.0.1",
    port=5533,
    ttl=60,
    records={
        "app.local": {
            "A": "127.0.0.1",
            "TXT": "internal demo",
            "MX": {"preference": 10, "exchange": "mail.app.local"},
        },
        "*.dev.local": {"A": "127.0.0.2"},
    },
    upstream_servers=[("1.1.1.1", 53)],
    fallback_to_upstream=True,
)
dns.start()
```

Tipos con encoding especifico incluyen `A`, `AAAA`, `NS`, `CNAME`, `PTR`, `MX`,
`TXT`, `SRV`, `SOA`, `CAA`, `NAPTR`, `URI`, `DS`, `DNSKEY`, `TLSA` y `SSHFP`.
Para otros tipos puedes usar `TYPE####` con `rdata`, `hex` o `base64`.

El fallback upstream valida endpoint de origen, transaction ID y pregunta antes
de aceptar una respuesta.

## WebSocket con ciclo de vida controlado

```python
def on_close(ws, code, reason):
    print("closed", code, reason)

def on_timeout(ws, reason):
    print("timeout", reason)

@app.ws(
    "/ws/events",
    subprotocols=("json",),
    idle_timeout=60,
    keepalive_interval=20,
    pong_timeout=10,
    auto_pong=True,
    on_close=on_close,
    on_timeout=on_timeout,
)
def events(ws, _request):
    while True:
        frame = ws.recv_frame()
        if frame.opcode == 0x8:
            ws.close(1000, "bye")
            break
        if frame.opcode == 0x1:
            ws.send_text(frame.payload.decode("utf-8"))
```

El lector reensambla mensajes fragmentados y valida RSV, opcodes, mascaras,
longitudes minimas, frames de control y UTF-8 en texto.

## SQLite de lectura intensiva

Para lectura intensiva sobre archivo SQLite:

```python
from wsbuilder import OptimizedDatabase, SQLite3OptimizationConfig

cfg = SQLite3OptimizationConfig(cache_size=32768, wal_autocheckpoint=500)
db = OptimizedDatabase(
    "data/app.sqlite3",
    optimization_config=cfg,
    enable_replicas=True,
    replica_count=4,
)
```

Usa replicas para lecturas:

```python
rows = db.read_replica_fetchall("SELECT * FROM events ORDER BY id DESC LIMIT 100")
```

Las replicas son conexiones read-only. Las escrituras deben ir a la conexion
principal.

## Observabilidad para operacion

Activa metricas y logs al iniciar:

```python
app.enable_metrics(app_name="edge-service")
app.enable_logs(path="logs/edge.ndjson")
app.enable_docs(path="/docs", json_path="/docs.json")
```

Endpoints utiles:

- `/api/metrics`: snapshot JSON.
- `/api/metrics/stream?interval=1&limit=10`: stream NDJSON finito.
- `/api/metrics/stream?interval=1&follow=1`: stream continuo.
- `/docs.json`: snapshot de rutas e integraciones activas.

## Apagado y recursos

`App.close()` cierra tareas, cache HTTP, proxy, logs y pools de rutas. Si adjuntas
recursos propios como `app.db` o `app.dns`, cierralos en tu flujo principal:

```python
try:
    app.run("127.0.0.1", 8765)
finally:
    if getattr(app, "db", None):
        app.db.close()
    if getattr(app, "dns", None):
        app.dns.close()
```

## Checklist avanzado

- Define limites de body, headers y workers segun tu carga.
- Usa TLS o un proxy TLS delante cuando salgas de localhost.
- Configura `trusted_proxy_cidrs` antes de leer IPs de cabeceras forwarded.
- No uses `verify_tls=False` para upstreams no confiables.
- Activa metricas y logs desde el arranque.
- Usa replicas solo para lecturas.
- Invalida caches despues de escrituras.
- Mantiene rutas WebSocket con timeouts y keepalive.
- Cierra recursos propios en `finally`.

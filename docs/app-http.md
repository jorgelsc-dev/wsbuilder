# App y HTTP

## `App`

`App` es el centro del framework. Mantiene el router, las rutas WebSocket y las
integraciones opcionales de metricas, seguridad, cache, logs, proxy y tareas.
Una aplicacion puede usarse de dos formas:

- con `app.run(host, port)`, para aceptar conexiones reales.
- con `app.dispatch(request)`, para pruebas o integraciones programaticas.

Constructor:

```python
from wsbuilder import App

app = App(
    cors_allow_origin="*",
    thread_cookie_secret="dev-secret",
    thread_cookie_name="wsbuilder-thread",
)
```

## Tipos de ruta

### `@app.view`

Pensada para HTML o texto. Si el handler devuelve:

- `Response`, se envia tal cual.
- `str`, `bytes` o `None`, se convierte en `Response.text(...)`.

```python
from wsbuilder import Response

@app.view("/")
def home(_request):
    return Response.html("<h1>Inicio</h1>")
```

### `@app.api`

Pensada para JSON. Si el handler devuelve `dict` o `list`, la conversion a JSON
es automatica.

```python
@app.api("/api/health")
def health(_request):
    return {"ok": True}
```

### `@app.route`

Es la forma mas generica. Permite elegir `kind="plain"` o `kind="api"`.

```python
@app.route("/status.txt", methods=("GET",), kind="plain")
def status(_request):
    return "ok"
```

El path debe empezar con `/` y no puede contener caracteres de control. Los
metodos se normalizan a mayusculas. Si declaras una ruta `GET`, `HEAD` funciona
automaticamente con las mismas cabeceras y sin cuerpo.

## `Request`

`Request` expone:

- `method`, `path`, `query_string`, `query`.
- `headers`, `body`, `client`, `tls`.
- `app`, que `App.dispatch()` rellena automaticamente.
- helpers `text()` y `json()`.

`query` decodifica percent-encoding y `+` con las reglas de formularios URL. Si
una clave aparece varias veces conserva el ultimo valor y limita el numero de
campos aceptados.

```python
@app.api("/api/echo", methods=("POST",))
def echo(request):
    payload = request.json() or {}
    return {"received": payload, "query": request.query}
```

Si el cuerpo no es JSON valido, `request.json()` devuelve `None`. Eso permite
escribir handlers defensivos sin envolver cada parseo en `try/except`.

## `Response`

Constructores de clase:

- `Response.json(data, status=200, headers=None)`
- `Response.text(text, status=200, headers=None)`
- `Response.html(html, status=200, headers=None)`
- `Response.stream(chunks, status=200, headers=None, content_type=None)`

Ejemplo de streaming:

```python
from wsbuilder import Response

@app.api("/api/stream")
def stream(_request):
    def chunks():
        yield '{"step": 1}\n'
        yield '{"step": 2}\n'
    return Response.stream(chunks(), content_type="application/x-ndjson")
```

## CORS y `OPTIONS`

Si `cors_allow_origin` esta configurado:

- las rutas `api` incluyen `Access-Control-Allow-Origin`.
- el `OPTIONS` automatico responde con la union de metodos permitidos y
  cabeceras basicas.

Ademas, `HEAD` reutiliza una ruta `GET` y envia sus mismas cabeceras sin cuerpo.
Un metodo no permitido para una ruta existente responde `405` e incluye
`Allow`; una ruta inexistente responde `404`.

## Probar una ruta sin red

```python
from wsbuilder import Request

request = Request(
    method="GET",
    path="/api/health",
    query_string="",
    headers={},
    body=b"",
    client=("127.0.0.1", 1234),
)

response = app.dispatch(request)
assert response.status == 200
```

Ese patron aparece en la suite porque evita abrir sockets y permite probar la
logica de rutas de forma determinista.

## Vistas con hilos dedicados

Las rutas `view` soportan worker pools por ruta:

```python
@app.view(
    "/heavy",
    min_threads=1,
    max_threads=4,
    requests_per_thread=8,
    worker_timeout_seconds=2.0,
)
def heavy(_request):
    return "ok"
```

Campos relevantes:

- `min_threads` y `max_threads`: rango del pool.
- `requests_per_thread`: cola maxima por worker.
- `worker_timeout_seconds`: timeout de ejecucion.
- `thread_host`, `thread_base_port`: metadatos de trazabilidad.

Cuando una vista usa pool, la respuesta incluye cabeceras de trazabilidad y una
cookie de afinidad por ruta, firmada con HMAC y limitada por
`affinity_ttl_seconds`. Configura un `thread_cookie_secret` privado y estable en
produccion. Si una solicitud vence mientras aun esta en cola se retira antes de
que el handler comience; un handler que ya esta ejecutandose no puede
interrumpirse de forma forzada.

## Startup hooks

`add_startup()` permite preparar recursos antes de aceptar trafico:

```python
def prepare():
    print("ready")

app.add_startup(prepare)
```

Los hooks se ejecutan al inicio de `HTTPServer.serve_forever()`. Si un hook falla,
el error se imprime y el servidor sigue intentando arrancar.

## Integraciones `enable_*`

`App` expone accesos rapidos para las capas mas comunes:

| Metodo | Que instala |
| --- | --- |
| `enable_metrics(...)` | `app.metrics` y endpoints JSON/NDJSON |
| `enable_security(policy=None)` | `app.security` con `SecurityPolicy` |
| `enable_caches(caches=None)` | `app.caches` con cache HTTP de vistas |
| `enable_logs(path=...)` | `app.logs` con escritor NDJSON |
| `enable_docs(...)` | rutas HTML/JSON de documentacion runtime |

La cache clave-valor usa `install_cache(app, cache)` porque puede haber muchas
instancias o namespaces segun el diseno de la aplicacion.

## Documentacion automatica

```python
app.enable_docs(
    path="/docs",
    json_path="/docs.json",
    title="runtime docs",
    description="Snapshot de la aplicacion en vivo.",
)
```

Esto publica las rutas HTTP, las rutas WS y el estado de integraciones como
metricas, seguridad, cache, logs, tareas y proxy.

## `HTTPServer`

`HTTPServer` es el servidor TCP/HTTP incluido. Se usa normalmente a traves de
`app.run(host, port)`, pero tambien puede instanciarse de forma explicita.

Caracteristicas practicas:

- limite de workers de conexion.
- timeout de lectura de request.
- validacion estricta de linea inicial, cabeceras y cuerpo.
- rechazo de framing ambiguo, cuerpos incompletos y `Transfer-Encoding` no
  soportado.
- soporte de `Expect: 100-continue` para cuerpos con `Content-Length`.
- soporte TLS via `ssl_context`.
- handshake WebSocket integrado.

Limites por defecto:

| Campo | Valor |
| --- | --- |
| `MAX_CONNECTION_WORKERS` | `64` conexiones activas |
| `MAX_REQUEST_HEADER_BYTES` | `64 KiB` |
| `MAX_REQUEST_BODY_BYTES` | `2 MiB` |
| `REQUEST_READ_TIMEOUT_SECONDS` | `10.0` segundos |
| `ACCEPT_TIMEOUT_SECONDS` | `0.5` segundos |

Para cambiarlos, instancia `HTTPServer` manualmente y ajusta atributos antes de
llamar `serve_forever()`.

```python
from wsbuilder import HTTPServer

server = HTTPServer("127.0.0.1", 8765, app)
server.MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
server.serve_forever()
```

## Demo incluida

El paquete trae una demo ejecutable:

```bash
python -m wsbuilder --host 0.0.0.0 --port 8765
```

La demo habilita metricas, documentacion runtime, monitor HTML y una ruta
WebSocket de eco. El host predeterminado es `127.0.0.1`; usa `0.0.0.0`
explicitamente solo cuando quieras exponer la demo a la red.

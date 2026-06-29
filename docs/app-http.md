# App y HTTP

## `App`

`App` es el centro del framework. Mantiene el router, las rutas WebSocket y las
integraciones opcionales de metricas, seguridad, cache, logs, proxy y tareas.

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

## `Request`

`Request` expone:

- `method`, `path`, `query_string`, `query`.
- `headers`, `body`, `client`, `tls`.
- `app`, que `App.dispatch()` rellena automaticamente.
- helpers `text()` y `json()`.

```python
@app.api("/api/echo", methods=("POST",))
def echo(request):
    payload = request.json() or {}
    return {"received": payload, "query": request.query}
```

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
- el `OPTIONS` automatico responde con metodos permitidos y cabeceras basicas.

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
cookie firmada de afinidad por ruta.

## Startup hooks

`add_startup()` permite preparar recursos antes de aceptar trafico:

```python
def prepare():
    print("ready")

app.add_startup(prepare)
```

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
- validacion de cabeceras y cuerpo.
- soporte TLS via `ssl_context`.
- handshake WebSocket integrado.

## Demo incluida

El paquete trae una demo ejecutable:

```bash
python -m wsbuilder --host 0.0.0.0 --port 8765
```

La demo habilita metricas, documentacion runtime, monitor HTML y una ruta
WebSocket de eco.

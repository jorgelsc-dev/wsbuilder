# wsbuilder Docs

Documentacion tecnica y de uso del proyecto `wsbuilder` en su estado actual.

## 1. Resumen

`wsbuilder` es un framework ligero en Python (sin dependencias externas) para:

- Servidor HTTP.
- WebSocket.
- ORM SQLite.
- Metricas de aplicacion.
- DNS local minimo.
- Routing de `view` con pool dinamico por ruta y distribucion `least_busy`.

## 2. Estructura del proyecto

Rutas principales:

- `src/wsbuilder/app.py`: enrutado, dispatch, view workers y pool dinamico.
- `src/wsbuilder/server.py`: servidor HTTP/TCP, control de conexiones, limites y timeouts.
- `src/wsbuilder/http.py`: parser/request/response HTTP.
- `src/wsbuilder/ws.py`: handshake y frames WebSocket.
- `src/wsbuilder/orm.py`: ORM SQLite.
- `src/wsbuilder/metrics.py`: snapshot y stream NDJSON de metricas.
- `src/wsbuilder/dns.py`: DNS UDP local.
- `src/wsbuilder/cookies.py`: utilidades de cookie.
- `src/wsbuilder/headers.py`: utilidades de headers.
- `src/wsbuilder/__main__.py`: CLI/demo modular actual.
- `src/wsbuilder/ws_demo.py`: demo legacy monolitico (referencia, no entrypoint principal).
- `tests/`: suite de pruebas unitarias.
- `.github/workflows/`: CI/CD y release.

## 3. Arquitectura runtime

### 3.1 HTTP server

`HTTPServer`:

- Abre socket TCP principal.
- Acepta conexiones con limite de workers concurrentes.
- Aplica timeouts de `accept` y lectura de request.
- Parsea request HTTP.
- Hace dispatch a routes HTTP o WebSocket.
- Registra metricas si estan habilitadas.
- En shutdown invoca `app.close()` para liberar pools/hilos.

### 3.2 Router y App

`App` maneja:

- `view(path, ...)`: rutas plain-text/HTML.
- `api(path, ...)`: rutas API (dict/list -> JSON).
- `ws(path)`: rutas WebSocket.
- CORS basico para rutas API.
- Hooks de startup.

### 3.3 Thread pool por view

Para `view` puedes declarar workers dedicados:

```python
@app.view("/jobs", min_threads=1, max_threads=4, requests_per_thread=0)
def jobs(request):
    return "ok"
```

Comportamiento:

- `thread_count=0` (default): la `view` corre en el hilo padre.
- `min_threads`/`max_threads`: rango del pool por ruta.
- `requests_per_thread=0`: capacidad ilimitada por worker.
- Distribucion siempre `least_busy` (menor carga actual).

Seguridad:

- Cookie firmada con digest SHA1 interno del proyecto (anti-forzado de worker id).
- Comparacion de firma con funcion interna de tiempo constante.
- Cookie emitida con `HttpOnly=True`.
- `Secure=True` cuando la request llega por TLS.
- La cookie queda como metadata de trazabilidad (no controla el balanceo).

Control de capacidad y estabilidad:

- `requests_per_thread`: limite por worker (0 = sin limite).
- `worker_timeout_seconds`: timeout de ejecucion de job de worker.
- Escalado dinamico: se crean workers hasta `max_threads` cuando hay carga.

## 4. API HTTP y tipos base

### 4.1 `Request`

Incluye:

- `method`, `path`, `query_string`, `query`.
- `headers`, `body`.
- `client` (ip, port).
- `tls` metadata.

### 4.2 `Response`

Helpers:

- `Response.text(...)`
- `Response.json(...)`
- `Response.html(...)`
- `Response.stream(...)`

El server soporta streaming chunked automaticamente cuando aplica.

## 5. WebSocket

`ws.py` implementa:

- Deteccion de upgrade.
- Handshake con `Sec-WebSocket-Accept`.
- Validacion de `Connection`, `Upgrade`, `Sec-WebSocket-Version`, `Sec-WebSocket-Key`.
- Lectura/envio de frames.
- Validacion de protocolo:
  - frames cliente->servidor deben ir enmascarados.
  - RSV bits sin extensiones deben ser 0.
  - opcodes reservados invalidos.
  - limites de payload.
  - control frames no fragmentados y max 125 bytes.

## 6. DNS local (`LocalDNSServer`)

Clase en `dns.py`:

- UDP DNS minimalista.
- Responde `A` y `AAAA` para `localhost`.
- `NXDOMAIN` para nombres desconocidos.
- Soporta `records` custom.

Default actual para minimizar conflictos:

- `host="127.0.0.1"`
- `port=5533`

## 7. ORM SQLite

Capacidades:

- Definicion de modelos por clases (`Model` + `Field`).
- Tipos: `IntegerField`, `TextField`, `RealField`, `BlobField`, `BooleanField`, `DateTimeField`, `JSONField`.
- Query builder (`QuerySet`) con `filter`, `exclude`, `order_by`, `limit`, `offset`, `count`, `update`, `delete`.
- Transacciones anidadas via savepoints.
- Validacion de identificadores SQL para reducir riesgo de SQL injection por nombres dinamicos.

## 8. Metricas

`App.enable_metrics()` registra:

- `GET /api/metrics`: snapshot JSON.
- `GET /api/metrics/stream`: stream NDJSON.

Incluye:

- conexiones activas/totales.
- trafico in/out.
- estado HTTP por metodo/path/status.
- WebSocket upgrades y mensajes.
- errores recientes.

## 9. CLI

Entrypoint:

- `python -m wsbuilder`
- `wsbuilder`

Lanza demo modular (no el `ws_demo.py` legacy).

Opciones:

- `--host`
- `--port`

## 10. Testing

Suite actual:

- `tests/test_orm.py`
- `tests/test_metrics.py`
- `tests/test_dns.py`
- `tests/test_headers_cookies.py`
- `tests/test_app_threads.py`

Ejecucion local:

```bash
PYTHONPATH=src pytest -q
```

## 11. CI/CD

Workflows relevantes:

- `package-build.yml`: build + `twine check` + tests + smoke import.
- `release-from-main.yml`: calcula semver, crea `release/vX.Y.Z`, actualiza version, tag y release.
- `publish-packages.yml`: publica a TestPyPI/PyPI y adjunta artefactos a GitHub Release.
- `codeql.yml`: analisis de seguridad.

Dependabot:

- actualizacion semanal para `pip` y `github-actions`.

## 12. Seguridad y hardening actual

Incluye:

- Timeouts de socket y request en servidor HTTP.
- Limites de body/header.
- Cola y timeout por worker de view.
- Firma de cookie de trazabilidad de worker.
- Distribucion por worker menos ocupado (`least_busy`).
- Validaciones de protocolo WebSocket.

## 13. Limitaciones conocidas

- HTTP parser minimalista, no cubre todo RFC7230.
- WebSocket cubre casos comunes, no todas las extensiones RFC6455.
- Afinidad de worker esta orientada a simplicidad, no reemplaza un LB externo.
- `ws_demo.py` sigue existiendo como referencia legacy.

## 14. Ejemplo integral minimo

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")
app.enable_metrics()

@app.view("/", min_threads=1, max_threads=2, requests_per_thread=0)
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("0.0.0.0", 8765)
```

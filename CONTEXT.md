# Contexto de wsbuilder

## Cómo usar y mantener este archivo

Lee este archivo al comenzar una tarea para ubicar el cambio sin recorrer todo el
repositorio. Contiene la estructura, las APIs, los límites, los comandos y los
contratos más consultados, de modo que en la mayoría de tareas basta con abrir
después solo los módulos y tests implicados. Usa búsquedas dirigidas con `rg`
(por ejemplo `rg -n "def enable_" src/wsbuilder/app.py`) para resolver dudas.

Este resumen describe la estructura y las decisiones estables del proyecto. El
código, la configuración y los tests vigentes determinan el comportamiento real.
Si falta un dato o encuentras una diferencia, verifica los archivos implicados y
actualiza la sección correspondiente en el mismo cambio.

Actualiza este archivo cuando cambien la arquitectura, las rutas de archivos, las
APIs de entrada, las dependencias permitidas, los comandos de desarrollo, las
validaciones o el flujo de publicación. Mantén las referencias concretas y evita
duplicar implementaciones. No almacenes secretos, datos personales, resultados
temporales de CI, listas de PRs pendientes ni números de versión que cambian con
cada release.

## Propósito y puntos de entrada

`wsbuilder` es un framework modular para Python 3.11+ con HTTP/1.1, HTTP/2,
HTTP/3 (QUIC), WebSocket, persistencia SQLite y utilidades de infraestructura
(cache, seguridad, métricas, logs, tareas, DNS, mDNS, reverse proxy, PKI, redes
neuronales). El núcleo usa solo la biblioteca estándar; TLS/QUIC y mDNS tienen
extras opcionales. El paquete está clasificado como Alpha en
[pyproject.toml](pyproject.toml). Licencia MIT.

- API pública: [src/wsbuilder/__init__.py](src/wsbuilder/__init__.py) (`__all__`).
  Los nombres de PKI (`CertificateAuthority`, `CertificateManager`,
  `MaterializedFiles`, `TLSMaterial`, `install_tls`) se resuelven de forma
  diferida con `__getattr__` para no exigir `cryptography` al importar.
- Fachada de compatibilidad: [src/wsbuilder/framework.py](src/wsbuilder/framework.py);
  reexporta las implementaciones de los módulos especializados. Si añades un
  símbolo público, expórtalo en `__init__.py` (y en `__all__`), no solo aquí.
- Aplicación y rutas: [src/wsbuilder/app.py](src/wsbuilder/app.py), con `App`,
  `Router`, `Route`, `app.view()`, `app.api()` y `app.ws()`.
- CLI y demo: [src/wsbuilder/__main__.py](src/wsbuilder/__main__.py).
  `python -m wsbuilder`, `wsbuilder` y `wsbuilder-demo` usan `main()`;
  `build_demo_app()` construye la app de demo.
- Ejemplo completo: [examples/full-project/](examples/full-project/README.md)
  (`app.py`, `upstream.py`; `data/` y `logs/` se excluyen del sdist).
- Guía pública: [README.md](README.md). Documentación publicada:
  <https://wsbuilder.jorgelsc.dev/>. Soporte: [SUPPORT.md](SUPPORT.md).

## Estructura del repositorio

```text
src/wsbuilder/          paquete (layout src; setuptools package-dir "" = "src")
  __init__.py           API pública, __version__, carga diferida de PKI
  __main__.py           CLI/demo (argparse, CLI_LIMITS, build_demo_app, main)
  framework.py          fachada de reexportación
  app.py                App, Router, Route, workers por vista, docs, dispatch
  http.py               Request, Response, parse_query_string
  http1.py http2.py     protocolos HTTP/1.x y HTTP/2 (+ hpack.py)
  http3.py http3_server.py http3_websocket.py qpack.py   HTTP/3 sobre QUIC
  quic/                 address, connection, crypto, frames, packet, recovery, tls, varint
  server.py             HTTPServer (TCP, TLS/ALPN, límites, keep-alive, apagado)
  ws.py                 WebSocket, frames, handshake, errores WebSocket*
  headers.py cookies.py helpers de cabeceras y cookies
  constants.py          MAGIC_WS, DEFAULT_CORS_ALLOW_ORIGIN, STATUS_MESSAGES
  _coerce.py            coerción interna de valores
  orm.py db_replicas.py SQLite ORM y réplicas/optimización
  cache.py caches.py    cache KV SQLite en memoria y cache de respuestas de vistas
  security.py           ACL, rate limit, bloqueo de IP
  logs.py metrics.py tasks.py   observabilidad y tareas en segundo plano
  dns.py mdns.py        servidor DNS local, mDNS/DNS-SD
  proxyi.py             reverse proxy y balanceo
  pki.py                CA, certificados y material TLS (extra tls)
  ia.py predicts.py     redes neuronales y predicción
tests/                  suite unittest ejecutada con pytest (test_*.py)
docs/ + mkdocs.yml      sitio MkDocs (docs/reference, docs/help, stylesheets, assets)
examples/full-project/  proyecto de ejemplo
.github/workflows/      CI, empaquetado, docs, release y política de PR
.github/rulesets/       protect-main/develop/release/tags-v (sincronizados por workflow)
.github/CODEOWNERS, .github/dependabot.yml
CONTRIBUTING.md, SUPPORT.md, LICENSE, MANIFEST.in, requirements-docs.txt
```

Directorios generados que no se versionan ni se deben leer para orientarse:
`dist/`, `build/`, `site/`, `__pycache__/`, `.pytest_cache/`, `*.egg-info/`,
`.venv*/` (ver [.gitignore](.gitignore)).

## Mapa de implementación y pruebas

Los nombres de módulos de esta tabla son relativos a `src/wsbuilder/`; los de
tests, a `tests/`; los de docs, a `docs/`.

| Área | Módulos principales | Tests de referencia | Docs |
| --- | --- | --- | --- |
| Rutas, despacho, CORS y workers por vista | `app.py`, `headers.py`, `cookies.py` | `test_app_threads.py`, `test_headers_cookies.py`, `test_http.py` | `app-http.md` |
| Request, Response y HTTP/1 | `http.py`, `http1.py`, `server.py` | `test_http.py`, `test_http1.py`, `test_server.py`, `test_server_errors.py` | `http.md` |
| HTTP/2 y HPACK | `http2.py`, `hpack.py` | `test_http2.py`, `test_hpack.py` | `http-versions.md` |
| HTTP/3 y WebSocket sobre HTTP/3 | `http3.py`, `http3_server.py`, `http3_websocket.py`, `qpack.py` | `test_http3.py`, `test_http3_server.py`, `test_http3_streaming.py`, `test_http3_streaming_udp.py` | `http-versions.md` |
| Transporte QUIC | `quic/` | `test_quic_protocol.py`, `test_quic_crypto.py`, `test_quic_tls.py`, `test_quic_recovery.py`, `test_quic_address.py` | `http-versions.md` |
| WebSocket y frames | `ws.py` | `test_ws.py`, `test_server.py` | `websocket.md` |
| SQLite, ORM y réplicas | `orm.py`, `db_replicas.py` | `test_orm.py`, `test_orm_replicas.py` | `data-orm.md`, `persistence.md` |
| Cache clave-valor y cache de vistas | `cache.py`, `caches.py` | `test_cache.py`, `test_caches.py` | `cache.md`, `cache-security.md` |
| ACL, rate limiting y bloqueo de IP | `security.py` | `test_security.py` | `security.md` |
| Logs, métricas y tareas | `logs.py`, `metrics.py`, `tasks.py` | `test_logs.py`, `test_metrics.py`, `test_tasks.py` | `observability.md`, `observability-tasks.md`, `tasks.md` |
| DNS, mDNS y descubrimiento | `dns.py`, `mdns.py` | `test_dns.py`, `test_mdns.py` | `dns.md`, `mdns.md`, `network-edge.md` |
| Reverse proxy y balanceo | `proxyi.py` | `test_proxyi.py`, `test_proxyi_routing.py` | `proxy.md` |
| Certificados y material TLS | `pki.py` | `test_pki.py` | `tls-certificates.md` |
| Redes neuronales y predicción | `ia.py`, `predicts.py` | `test_ia.py`, `test_predicts.py` | `ia.md`, `ai-predict.md` |
| Demo, documentación y políticas de contribución | `__main__.py`, `docs/`, `.github/workflows/`, `CONTRIBUTING.md` | `test_demo_monitor.py`, `test_docs.py`, `test_documentation_files.py`, `test_contributing_policy.py`, `test_pr_identity_policy.py` | `index.md`, `architecture.md`, `api-map.md` |

Otros documentos de `docs/`: `install.md`, `getting-started.md`,
`beginners.md`, `intermediate.md`, `advanced.md`, `capabilities.md`,
`full-project.md`, `reference/index.md`, `help/index.md`.

## Referencia rápida de la API

### App (`app.py`)

```python
App(cors_allow_origin="", thread_cookie_secret="", thread_cookie_name="wsbuilder-thread")
```

Atributos: `router`, `ws_routes`, `startup_hooks`, `metrics`, `security`,
`proxyi`, `caches`, `logs`, `tls`, `tasks` (`TaskManager`, siempre presente).
Los atributos opcionales valen `None` hasta llamar al `enable_*` o `install_*`.

- `@app.view(path, methods=("GET",), thread_count=0, min_threads=None,
  max_threads=None, thread_host="127.0.0.1", thread_base_port=0,
  max_clients=100, worker_timeout_seconds=5.0, max_pending_jobs=256,
  requests_per_thread=None, affinity_ttl_seconds=900.0,
  affinity_max_entries=10000, cache=None)`: vista con workers opcionales.
- `@app.api(path, methods=("GET",))`: ruta API (despacho directo, sin workers).
- `@app.route(path, methods, kind="plain", ...)`: base de `view`.
- `@app.ws(path, subprotocols=(), idle_timeout=0.0, keepalive_interval=0.0,
  pong_timeout=0.0, auto_pong=True, on_close=None, on_error=None,
  on_timeout=None, io_poll_interval=1.0, ping_payload=b"",
  max_frame_bytes=None, max_message_bytes=None)`; el handler recibe
  `(ws, request)`.
- `app.add_startup(func)`, `app.enable_metrics(path="/api/metrics",
  stream_path="/api/metrics/stream", app_name=None, max_tracked_labels=None)`,
  `app.enable_security(policy=None)`, `app.enable_caches(caches=None)`,
  `app.enable_tls(manager=None, ca=None, common_name="localhost",
  dns_names=None, ip_addresses=("127.0.0.1",), rotate=False, attr_name="tls")`,
  `app.enable_logs(...)`, `app.enable_docs(path="/docs",
  json_path="/docs.json", title=None, description=None)`.
- `app.describe()`, `app.list_view_threads(path, method="GET")`,
  `app.thread_metrics_snapshot()`, `app.dispatch(request)`, `app.close()`,
  `app.run(host, port, ssl_context=None, **server_options)` (crea `HTTPServer`
  y llama a `serve_forever()`).
- Afinidad de workers: cookie `wsbuilder-thread` firmada con HMAC-SHA256
  (`thread_id.issued.sig`) y cabeceras `WSBuilder-Thread`,
  `WSBuilder-Thread-Host`, `WSBuilder-Thread-Port`, `WSBuilder-Thread-Mode`.

### Request y Response (`http.py`)

- `Request(method, path, query_string, headers, body, client, tls=None,
  version="HTTP/1.1", trailers=None)`; atributos `method` (mayúsculas), `path`,
  `query` (dict de `parse_query_string`, máx. 1024 campos), `headers`, `body`
  (bytes), `client`, `tls`, `version`, `trailers`, `app`; métodos `text()` y
  `json()` (devuelve `None` si no es JSON válido).
- `Response(status=200, body=b"", headers=None, reason=None, stream=None)`; el
  estado debe estar en 100–599. Constructores: `Response.json(data)`,
  `Response.text(text)`, `Response.html(html)`,
  `Response.stream(chunks, content_type=None)`; todos aceptan `status` y
  `headers`.
- Helpers: `get_header`, `has_header`, `set_header`, `normalize_header_name`
  (`headers.py`); `parse_cookie_header`, `get_cookie`, `build_set_cookie`
  (`cookies.py`).

### HTTPServer (`server.py`)

`HTTPServer(host, port, app, ssl_context=None, **limits)`. Los límites son
atributos de clase que cada instancia puede sobrescribir con `LIMIT_OPTIONS`
(una clave desconocida lanza `TypeError`; `None` conserva el valor por defecto):

| Keyword | Atributo | Valor por defecto |
| --- | --- | --- |
| `max_connection_workers` | `MAX_CONNECTION_WORKERS` | 64 |
| `max_request_header_bytes` | `MAX_REQUEST_HEADER_BYTES` | 64 KiB |
| `max_request_body_bytes` | `MAX_REQUEST_BODY_BYTES` | 2 MiB |
| `request_read_timeout` | `REQUEST_READ_TIMEOUT_SECONDS` | 10.0 |
| `keepalive_timeout` | `KEEPALIVE_TIMEOUT_SECONDS` | 5.0 |
| `max_keepalive_requests` | `MAX_KEEPALIVE_REQUESTS` | 100 (0 = ilimitado, 1 = sin reutilización) |
| `max_concurrent_streams` | `MAX_CONCURRENT_STREAMS` | 100 (HTTP/2) |
| `graceful_shutdown_timeout` | `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS` | 10.0 |
| `max_ws_frame_bytes` | `MAX_WS_FRAME_BYTES` | `None` (defecto de `ws.py`) |
| `max_ws_message_bytes` | `MAX_WS_MESSAGE_BYTES` | `None` (defecto de `ws.py`) |

Otros atributos: `ENABLE_HTTP2 = True` (ALPN y preface en claro),
`HANDLE_SIGTERM = True`, `ACCEPT_TIMEOUT_SECONDS = 0.5`,
`ACQUIRE_WORKER_TIMEOUT_SECONDS = 1.0`. Los `max_*_bytes` de una ruta WebSocket
tienen prioridad sobre los del servidor. HTTP/3: `Http3Server` e
`install_http3(app, tls, host="0.0.0.0", port=0, attr_name="http3")` en
`http3_server.py`; `alt_svc_header(port, max_age=86400)` anuncia HTTP/3.

### CLI (`__main__.py`)

`python -m wsbuilder --host H --port P` más los flags de `CLI_LIMITS`, que se
traducen a keywords de `HTTPServer`: `--max-workers`, `--max-header-bytes`,
`--max-body-bytes`, `--request-timeout`, `--keepalive-timeout`,
`--max-keepalive-requests`, `--shutdown-timeout`, `--max-ws-message-bytes`.
Un `ValueError` del servidor se reporta con el nombre del flag. Los textos de
ayuda de la CLI están en español.

### WebSocket (`ws.py`)

`WebSocket.recv_frame()` devuelve `WebSocketFrame` (`opcode`, `payload`);
`send_text`, `send_binary`, `send_ping`, `send_pong`, `send_frame`,
`close(code=1000, reason="")`. Opcodes: 0x1 texto, 0x2 binario, 0x8 cierre,
0x9 ping, 0xA pong. Errores: `WebSocketProtocolError`,
`WebSocketMessageTooBigError`, `WebSocketReadError`,
`WebSocketReadTimeoutError`, `WebSocketConnectionClosedError`. Helpers:
`parse_close_payload`, `handshake_websocket_with_options`, `is_ws_request`,
`make_ws_frame_bytes`, `read_ws_frame_raw`.

### ORM (`orm.py`, `db_replicas.py`)

- `Database`: `execute`, `executemany`, `fetchone`, `fetchall`, `scalar`,
  `transaction()` (`Transaction`), `in_transaction`, `set_pragma`,
  `get_pragma`, `checkpoint`, `vacuum`, `optimize`, `close`, variantes
  `read_replica_*`.
- Campos: `Field`, `IntegerField`, `RealField`, `TextField`, `BlobField`,
  `BooleanField`, `DateTimeField`, `JSONField`; `SQL(expression)` para
  expresiones literales.
- `Model`: `create_table`, `drop_table`, `objects(db)`, `raw`, `create`, `get`,
  `filter`, `save`, `to_dict`, `pk_value`; `create_tables` / `drop_tables`.
- `QuerySet`: `filter`, `exclude`, `where_raw`, `using`, `order_by`,
  `order_by_raw`, `limit`, `offset`, `paginate`, `all`, `first`, `get`,
  `values`, `count`, `exists`, `update`, `delete`, `create`.
- Lookups (`campo__op`, ver `QuerySet._build_lookup`): `eq`, `ne`, `gt`,
  `gte`, `lt`, `lte`, `like`, `ilike`, `contains`, `icontains`, `startswith`,
  `istartswith`, `endswith`, `iendswith`, `in`, `not_in`.
- Identificadores: `validate_identifier` y `quote_identifier`.
- Réplicas: `DatabaseReplica`, `DatabaseReplicaPool`, `OptimizedDatabase`,
  `SQLite3OptimizationConfig`.

### Logs (`logs.py`)

`LEVELS`: `debug`, `info`, `warning`, `error`; ruta por defecto
`logs/wsbuilder.ndjson`. `NDJSONLog(path, ensure_parent=True,
console_level="warning")` con `log(level, event, message="", exc=None,
**fields)`, `debug/info/warning/error`, `event`, `append`, `describe`, `close`.
`ConsoleLog` escribe en stderr; `console` es la instancia compartida.
`get_log(app)` devuelve `app.logs` o `console`; `install_logs(app, ...)` /
`app.enable_logs(...)`; `build_record` y `append_ndjson` para registros sueltos.

### Otros componentes

- Cache: `SQLiteMemoryCache` / `Cache`, `install_cache(app, cache=None,
  attr_name="cache")`; cache de vistas `ViewResponseCache`, `GlobalCacheRule`,
  `install_caches(app, caches=None, attr_name="caches")`.
- Seguridad: `SecurityPolicy`, `ACLRule`, `SecurityDecision`,
  `install_security(app, policy=None)`.
- Métricas: `AppMetrics`, `install_metrics(...)`; el snapshot incluye threads,
  cache, caches, security, tls y proxyi cuando están habilitados.
- Tareas: `TaskManager.spawn(...)`, `get`, `list(group=, status=)`, `cancel`,
  `cancel_group`, `cancel_all`, `wait`, `result`, `snapshot`,
  `close(wait=True, timeout=None)`; `TaskHandle` (`status`, `result`,
  `exception`, `cancel`, `wait`, `join`, `get`, `snapshot`); estados
  `TASK_PENDING/RUNNING/COMPLETED/FAILED/CANCELLED/REJECTED`.
- Proxy: `ProxyI`, `proxyi`, `ProxyRule`, `ProxyRouteBuilder`, `ProxyTarget`,
  `install_proxyi`, `normalize_balance_mode`, `normalize_target`; estrategias
  `BALANCING_*` listadas en `SUPPORTED_BALANCING_STRATEGIES`.
- DNS/mDNS: `LocalDNSServer`, `LocalMDNSServer`, `MDNSService`,
  `discover_mdns_services`.
- PKI: `CertificateAuthority`, `CertificateManager`, `TLSMaterial`,
  `MaterializedFiles`, `install_tls`.
- IA: `NeuralNetwork`, `DenseLayer`, `DataSet`, `DataSummary`, `ErrorSummary`,
  `describe_data`, `evaluate_errors`, `submit_training_task`; `Predictor`.

## Flujo y contratos que deben conservarse

1. `HTTPServer` recibe conexiones TCP; `server.py` selecciona HTTP/1 o HTTP/2 y
   gestiona TLS, timeouts, límites y apagado. HTTP/3 usa `Http3Server` sobre UDP.
2. La capa de protocolo construye un `Request`; `App.dispatch()` aplica la
   seguridad y resuelve el handler, con los componentes de cache, métricas y logs
   habilitados por la aplicación. Las rutas WebSocket realizan su propio upgrade
   y ciclo de vida en la capa de transporte.
3. Los handlers pueden devolver `Response`, estructuras JSON, texto o `None`.
   La aplicación normaliza la respuesta y el transporte la serializa.
4. Las vistas pueden usar workers por ruta (`min_threads`, `max_threads`,
   `requests_per_thread`) y cookies de afinidad. Las APIs mantienen su ruta de
   despacho específica. Conserva las cookies `Set-Cookie` que añade el handler.
5. El cierre debe liberar sockets, pools, tareas y recursos de la aplicación.
   Los tests de red usan puertos efímeros y limpieza explícita.

Al tocar protocolos, revisa especialmente los límites de cabeceras y cuerpos,
el control de flujo, el streaming y la liberación de conexiones y streams.
El timeout de handshake/lectura y la espera entre requests tienen funciones
distintas. WebSocket debe consumir los bytes ya leídos por el buffer HTTP.
HTTP/1.0 no usa codificación chunked. HPACK/QPACK decodifican las cadenas de
campos como ISO-8859-1; los límites y tablas deben contar los octetos recibidos.
HTTP/3 ejecuta los handlers fuera del hilo que recibe los datagramas.

Al tocar persistencia, conserva las búsquedas literales del ORM y los prefijos
literales del cache, incluidos `%`, `_` y barras invertidas. `QuerySet.count()`
y `exists()` respetan `limit` y `offset`.

## Restricciones técnicas

- Núcleo: biblioteca estándar de Python. No añadir dependencias de runtime.
- Extra `tls`: `cryptography>=42`, utilizado únicamente por `pki.py`, `quic/` y
  `http3_server.py`. Los helpers PKI de la API pública se cargan de forma diferida
  para que importar el núcleo no requiera este extra.
- Extra `mdns`: `zeroconf>=0.131`, utilizado únicamente por `mdns.py`; su backend
  sigue siendo opcional al importar el paquete.
- Herramientas permitidas: `setuptools`, `wheel`, `build`, `twine`, `pytest`,
  `ruff`, `mkdocs` y `mkdocs-material`. Las restricciones de documentación están
  en [requirements-docs.txt](requirements-docs.txt). Cualquier dependencia
  adicional requiere autorización expresa del mantenedor.
- Logs: usar `wsbuilder.logs`, `get_log(app)`, `NDJSONLog`, `append_ndjson` o
  `app.enable_logs()` / `app.logs`. Los niveles, tracebacks y salida a stderr ya
  tienen soporte nativo. No usar el módulo `logging` ni librerías de logging.
- Para necesidades nuevas, usar la biblioteca estándar: `unittest.mock`,
  `threading`, `selectors`, `asyncio`, `socket`, `ssl`, `sqlite3`, `json`,
  `signal`, `http.client`, `urllib`.
- Estilo: Python 3.11+, PEP 8, cuatro espacios, `snake_case` para funciones y
  módulos, `PascalCase` para clases, `UPPER_SNAKE_CASE` para constantes,
  módulos pequeños y helpers explícitos. Imitar el código circundante.
- Ruff (`pyproject.toml`): `target-version = "py311"`, `line-length = 100`,
  reglas `E`, `F`, `W`, `I`, `B`, `UP`; se ignoran `E501`, `B905` y `UP031`;
  en `tests/*` también `B023`. isort con `known-first-party = ["wsbuilder"]`.
- Tests: estilo `unittest` (`unittest.TestCase`), con `unittest.mock` para
  sustituciones; ejecución mediante pytest, recursos aislados (puertos efímeros,
  directorios temporales, SQLite en memoria), sincronización determinista con
  `threading.Event` en lugar de esperas fijas y limpieza en `tearDown` /
  `addCleanup`. Cubrir rutas de éxito y de error.

## Entorno y comandos

Ejecutar desde la raíz, con el entorno virtual del proyecto activado. Si no
existe, crear uno con `python3 -m venv .venv` y activar con
`source .venv/bin/activate`.

```sh
python -m pip install -e ".[tls,mdns]" pytest ruff build twine
python -m pip install -r requirements-docs.txt

ruff check src tests examples
PYTHONPATH=src pytest -q
python -m build
python -m twine check dist/*
mkdocs build --strict

python -m wsbuilder --host 127.0.0.1 --port 8765
```

Para trabajo focalizado: `PYTHONPATH=src pytest -q tests/test_http2.py`, cambiando
el archivo por el área de la tabla, o `-k nombre_del_test` para un caso concreto.
Las pruebas de transporte necesitan sockets TCP/UDP locales; si el entorno los
bloquea, la validación debe ejecutarse en uno que los permita.
`python -m build --no-isolation` requiere tener instalados `setuptools>=77` y
`wheel`; el build normal prepara un entorno aislado. Para comprobar que el
núcleo importa sin extras: `python -c "import wsbuilder"` en un entorno sin
`cryptography` ni `zeroconf`.

La demo expone `/`, `/monitor`, `/thread-demo`, `/api/health`, `/api/metrics`,
`/api/metrics/stream`, `/docs`, `/docs.json` y `/ws/` (eco de texto y binario).

## Qué validar según el cambio

| Cambio | Validación |
| --- | --- |
| Lógica de un módulo | Tests del área y Ruff; suite completa al cambiar contratos compartidos o concurrencia |
| Exportaciones, extras o empaquetado | Build, Twine, importación del núcleo sin extras y tests asociados |
| Documentación y enlaces | Revisar las rutas referenciadas; `pytest -q tests/test_documentation_files.py`; MkDocs estricto si afecta al sitio |
| Políticas o workflows | Tests de contribución e identidad y comprobaciones de la PR |

## CI y automatización

| Workflow | Función |
| --- | --- |
| [package-build.yml](.github/workflows/package-build.yml) | Lint, build, Twine, tests e instalación de prueba del wheel en Python 3.11–3.14 |
| [main-only-from-develop.yml](.github/workflows/main-only-from-develop.yml) | Job `enforce-source`: valida autor, rama de origen e identidades de las PRs a `main` |
| [codeql.yml](.github/workflows/codeql.yml) | Análisis CodeQL de Python |
| [docs-pages.yml](.github/workflows/docs-pages.yml) | Construye y publica el sitio MkDocs para cambios pertinentes en `main` |
| [release-from-main.yml](.github/workflows/release-from-main.yml) | Prueba `main`, calcula la versión, crea `release/vX.Y.Z`, tag y GitHub Release, y abre la PR de la siguiente versión `.devN` |
| [publish-packages.yml](.github/workflows/publish-packages.yml) | Valida y publica los paquetes de release |
| [sync-rulesets.yml](.github/workflows/sync-rulesets.yml) | Sincroniza `.github/rulesets/*.json` con las reglas del repositorio |

La navegación del sitio vive en [mkdocs.yml](mkdocs.yml); la documentación de
APIs generada por `App.enable_docs()` es independiente de ese sitio.
Dependabot se configura en `.github/dependabot.yml`.

## Git, PRs y versiones

- Seguir [CONTRIBUTING.md](CONTRIBUTING.md). La única cuenta humana autorizada es
  `jorgelsc-dev`; comprobar `gh api user --jq .login` y las identidades de autor y
  committer (`git var GIT_AUTHOR_IDENT`, `git var GIT_COMMITTER_IDENT`) antes de
  crear commits. Configurar la identidad solo en este repo.
- Actualizar referencias con `git fetch origin`, conservar cualquier cambio
  existente y trabajar en una rama temática desde `main` actualizado.
  Prefijos: `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, `perf/`.
- Cada PR aborda un cambio lógico, usa un asunto convencional y apunta a `main`.
  Incluir las validaciones y dejar la revisión y fusión al mantenedor salvo que
  haya una autorización expresa para fusionar.
- `enforce-source` comprueba cuenta, rama e identidades. La protección de `main`
  también exige revisión y comprobaciones actualizadas; consultar las reglas
  vigentes antes de atribuir un bloqueo a un conflicto de código.
- La versión está en `pyproject.toml` y `src/wsbuilder/__init__.py`; ambos valores
  deben coincidir. `main` declara una versión de desarrollo con sufijo `.devN`.
  Un cambio que solo ajusta las declaraciones de versión respecto al último tag
  no genera otra release. No cambiar versiones ni crear tags manualmente por
  una tarea que no lo requiera.
- Mantener fuera de los commits los archivos ignorados y artefactos como `dist/`,
  `build/`, `site/`, caches, bytecode y entornos virtuales. El contenido del sdist
  se controla con [MANIFEST.in](MANIFEST.in).
- Reportar vulnerabilidades por un canal privado, como indica la política de
  contribución.

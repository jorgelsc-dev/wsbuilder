# Contexto de wsbuilder

## Cómo usar y mantener este archivo

Lee este archivo al comenzar una tarea para ubicar el cambio sin recorrer todo el
repositorio. Después, consulta únicamente los módulos, tests y documentos que
correspondan al trabajo. Usa búsquedas dirigidas con `rg` para resolver dudas.

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

`wsbuilder` es un framework modular para Python 3.11+ con HTTP, WebSocket,
persistencia SQLite y utilidades de infraestructura. El núcleo usa la biblioteca
estándar; TLS/QUIC y mDNS tienen extras opcionales. El paquete está clasificado
como Alpha en [pyproject.toml](pyproject.toml).

- API pública: [src/wsbuilder/__init__.py](src/wsbuilder/__init__.py).
- Fachada de compatibilidad: [src/wsbuilder/framework.py](src/wsbuilder/framework.py);
  reexporta las implementaciones de los módulos especializados.
- Aplicación y rutas: [src/wsbuilder/app.py](src/wsbuilder/app.py), con `App`,
  `Router`, `Route`, `app.view()`, `app.api()` y `app.ws()`.
- CLI y demo: [src/wsbuilder/__main__.py](src/wsbuilder/__main__.py).
  `python -m wsbuilder`, `wsbuilder` y `wsbuilder-demo` usan esa entrada.
- Ejemplo completo: [examples/full-project/README.md](examples/full-project/README.md).
- Guía pública: [README.md](README.md). Documentación publicada:
  <https://wsbuilder.jorgelsc.dev/>.

## Mapa de implementación y pruebas

Los nombres de módulos de esta tabla son relativos a `src/wsbuilder/`; los de
tests, a `tests/`. Los directorios de documentación y configuración se indican
desde la raíz del repositorio.

| Área | Módulos principales | Tests de referencia |
| --- | --- | --- |
| Rutas, despacho, CORS y workers por vista | `app.py`, `headers.py`, `cookies.py` | `test_app_threads.py`, `test_headers_cookies.py`, `test_http.py` |
| Request, Response y HTTP/1 | `http.py`, `http1.py`, `server.py` | `test_http.py`, `test_http1.py`, `test_server.py`, `test_server_errors.py` |
| HTTP/2 y HPACK | `http2.py`, `hpack.py` | `test_http2.py`, `test_hpack.py` |
| HTTP/3 y WebSocket sobre HTTP/3 | `http3.py`, `http3_server.py`, `http3_websocket.py`, `qpack.py` | `test_http3.py`, `test_http3_server.py`, `test_http3_streaming.py`, `test_http3_streaming_udp.py` |
| Transporte QUIC | `quic/` | `test_quic_protocol.py`, `test_quic_crypto.py`, `test_quic_tls.py`, `test_quic_recovery.py`, `test_quic_address.py` |
| WebSocket y frames | `ws.py` | `test_ws.py`, `test_server.py` |
| SQLite, ORM y réplicas | `orm.py`, `db_replicas.py` | `test_orm.py`, `test_orm_replicas.py` |
| Cache clave-valor y cache de vistas | `cache.py`, `caches.py` | `test_cache.py`, `test_caches.py` |
| ACL, rate limiting y bloqueo de IP | `security.py` | `test_security.py` |
| Logs, métricas y tareas | `logs.py`, `metrics.py`, `tasks.py` | `test_logs.py`, `test_metrics.py`, `test_tasks.py` |
| DNS, mDNS y descubrimiento | `dns.py`, `mdns.py` | `test_dns.py`, `test_mdns.py` |
| Reverse proxy y balanceo | `proxyi.py` | `test_proxyi.py`, `test_proxyi_routing.py` |
| Certificados y material TLS | `pki.py` | `test_pki.py` |
| Redes neuronales y predicción | `ia.py`, `predicts.py` | `test_ia.py`, `test_predicts.py` |
| Demo, documentación y políticas de contribución | `__main__.py`, `docs/`, `.github/workflows/`, `CONTRIBUTING.md` | `test_demo_monitor.py`, `test_docs.py`, `test_documentation_files.py`, `test_contributing_policy.py`, `test_pr_identity_policy.py` |

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
- Estilo: Python 3.11+, PEP 8, cuatro espacios, módulos pequeños y helpers
  explícitos. Ruff se configura en `pyproject.toml`.
- Tests: estilo `unittest`, con `unittest.mock` para sustituciones; ejecución
  mediante pytest, recursos aislados y sincronización determinista.

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
el archivo por el área de la tabla. Las pruebas de transporte necesitan sockets
TCP/UDP locales; si el entorno los bloquea, la validación debe ejecutarse en uno
que los permita. `python -m build --no-isolation` requiere tener instalados
`setuptools>=77` y `wheel`; el build normal prepara un entorno aislado.

La demo expone `/`, `/monitor`, `/thread-demo`, `/api/health`, `/api/metrics`,
`/api/metrics/stream`, `/docs`, `/docs.json` y `/ws/`.

## Qué validar según el cambio

| Cambio | Validación |
| --- | --- |
| Lógica de un módulo | Tests del área y Ruff; suite completa al cambiar contratos compartidos o concurrencia |
| Exportaciones, extras o empaquetado | Build, Twine, importación del núcleo sin extras y tests asociados |
| Documentación y enlaces | Revisar las rutas referenciadas; `pytest -q tests/test_documentation_files.py`; MkDocs estricto si afecta al sitio |
| Políticas o workflows | Tests de contribución e identidad y comprobaciones de la PR |

CI en [package-build.yml](.github/workflows/package-build.yml) ejecuta lint,
build, Twine, tests y una instalación de prueba del wheel en Python 3.11–3.14.
[codeql.yml](.github/workflows/codeql.yml) analiza Python.
[docs-pages.yml](.github/workflows/docs-pages.yml) construye y publica el sitio
para los cambios pertinentes en `main`. La navegación vive en
[mkdocs.yml](mkdocs.yml); la documentación de APIs generada por `App.enable_docs()`
es independiente de ese sitio.

## Git, PRs y versiones

- Seguir [CONTRIBUTING.md](CONTRIBUTING.md). La única cuenta humana autorizada es
  `jorgelsc-dev`; comprobar `gh api user --jq .login` y las identidades de autor y
  committer antes de crear commits. Configurar la identidad solo en este repo.
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
- [release-from-main.yml](.github/workflows/release-from-main.yml) prueba `main`,
  calcula la versión semántica, crea una rama `release/vX.Y.Z`, un tag y un
  GitHub Release; activa la publicación de paquetes y abre la PR de la siguiente
  versión de desarrollo. Un cambio que solo ajusta las declaraciones de versión
  respecto al último tag no genera otra release.
- [publish-packages.yml](.github/workflows/publish-packages.yml) valida y publica
  los paquetes de release. No cambiar versiones o generar tags manualmente por
  una tarea que no lo requiera.
- Mantener fuera de los commits los archivos ignorados y artefactos como `dist/`,
  `build/`, `site/`, caches, bytecode y entornos virtuales. El contenido del sdist
  se controla con [MANIFEST.in](MANIFEST.in).
- Reportar vulnerabilidades por un canal privado, como indica la política de
  contribución.

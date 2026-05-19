# wsbuilder

`wsbuilder` es un paquete Python extraido de `PortHound4` para construir servidores HTTP + WebSocket sin dependencias externas.

Lightweight Python HTTP + WebSocket framework for building real-time APIs and custom servers.

**Keywords:** `python`, `http-server`, `websocket`, `framework`, `real-time`, `api`, `socket`.

## Incluye

- `wsbuilder.framework`: router, request/response, servidor HTTP, handshake WS y utilidades de frames.
- `wsbuilder.ws_demo`: demo completo HTTP + WebSocket + REST + SQLite.

## Instalacion local

```bash
python -m pip install -e .
```

## Uso rapido del framework

```python
from wsbuilder import App, Response, parse_close_payload

app = App()

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

@app.ws("/ws/")
def ws_handler(ws, _request):
    while True:
        fin, opcode, payload, _masked, _mask = ws.recv_frame()
        if opcode == 0x8:
            code, reason = parse_close_payload(payload)
            ws.close(code or 1000, reason or "")
            break
        if opcode == 0x9:
            ws.send_pong(payload)
            continue
        if opcode == 0x1:
            ws.send_text(payload.decode("utf-8", errors="ignore"))
        elif opcode == 0x2:
            ws.send_binary(payload)

app.run("0.0.0.0", 8765)
```

Para CORS sin variables de entorno:

```python
app = App(cors_allow_origin="*")
```

## HTTP streaming (chunked)

```python
import time
from wsbuilder import App, Response

app = App()

@app.view("/stream")
def stream(_request):
    def chunks():
        for i in range(5):
            yield f"chunk {i}\n"
            time.sleep(1)
    return Response.stream(chunks(), content_type="text/plain; charset=utf-8")
```

## Ejecutar demo incluido

```bash
python -m wsbuilder --host 0.0.0.0 --port 8765
# o
wsbuilder --host 0.0.0.0 --port 8765
```

## Configuracion CORS

- Usa `App(cors_allow_origin="https://tu-dominio.com")`.
- Usa `App(cors_allow_origin="*")` para permitir cualquier origen.

## CI/CD (GitHub Actions)

- `package-build.yml`: construye `sdist` + `wheel`, valida metadata y prueba instalacion/import.
- `release-from-main.yml` en push a `main`: calcula `semver` automaticamente, crea rama `release/v<version>` desde `main`, actualiza version del paquete, crea tag `v<version>` y publica GitHub Release.
- `publish-packages.yml` en push de tag `v*`: construye y publica a PyPI/TestPyPI (instalable con `pip`).
- `publish-packages.yml` en tags `v*`: adjunta los paquetes al GitHub Release.
- `main-only-from-develop.yml` (workflow `main-pr-source-check`): valida metadata minima del PR hacia `main`.

### Reglas de versionado automatico

- `major`: si algun commit trae `BREAKING CHANGE`, `type(scope)!:` o ramas/commits marcados como `breaking`/`major`.
- `minor`: si hay suficiente peso de `feat`/`feature` (proporcional frente a cambios `patch`) y no hay `major`.
- `patch`: cualquier otro caso.

Recomendado usar Conventional Commits para que el calculo de version sea preciso.

### Secrets requeridos

- `RELEASE_BOT_TOKEN` (recomendado): PAT/fine-grained token para crear rama `release/*`, crear tag y publicar GitHub Release.
- `RULESET_ADMIN_TOKEN` (fallback): usado automaticamente si no existe `RELEASE_BOT_TOKEN`.
- `PYPI_API_TOKEN`: token de publicacion para PyPI.
- `TEST_PYPI_API_TOKEN` (opcional): token de publicacion para TestPyPI.

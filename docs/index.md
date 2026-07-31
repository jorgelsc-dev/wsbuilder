# wsbuilder

`wsbuilder` es una libreria Python `3.11+` para construir servicios HTTP,
WebSocket y utilidades de infraestructura con Python puro y sin dependencias de
runtime.

Esta documentacion esta organizada para tres perfiles:

- **Principiantes**: levantar una API, devolver HTML/JSON y entender `Request` y
  `Response`.
- **Intermedios**: persistencia SQLite, cache, seguridad, metricas, logs y tareas
  en background.
- **Avanzados**: worker pools, TLS, limites de transporte, reverse proxy, DNS,
  replicas SQLite y operacion interna.

## Que puedes construir

- APIs JSON pequenas o medianas.
- Vistas HTML simples y streaming HTTP.
- Endpoints WebSocket para echo, chat, eventos o telemetria.
- Servicios con SQLite embebido y modelos declarativos.
- Herramientas internas con cache, ACL, rate limit, logs y metricas.
- Reverse proxy local con balanceo y dashboard.
- Servidor DNS local para demos, labs y entornos internos.
- Entrenamiento o prediccion simple desde Python puro.

Para una lista completa, lee [Que puedes hacer con wsbuilder](capabilities.md).

## Primer ejemplo

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")
app.enable_metrics()
app.enable_docs(path="/docs", json_path="/docs.json")

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("127.0.0.1", 8765)
```

Ejecuta:

```bash
python app.py
```

Abre:

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/api/health`
- `http://127.0.0.1:8765/docs`
- `http://127.0.0.1:8765/api/metrics`

## Ruta de aprendizaje

| Nivel | Empieza aqui | Que aprendes |
| --- | --- | --- |
| Inicial | [Principiantes](beginners.md) | instalar, crear rutas, devolver JSON/HTML, activar docs runtime |
| Medio | [Intermedios](intermediate.md) | CRUD SQLite, cache, seguridad, metricas, logs y tareas |
| Avanzado | [Avanzados](advanced.md) | HTTPServer, TLS, worker pools, DNS, proxy, replicas y operacion |

## Guias por tema

| Tema | Guia |
| --- | --- |
| Instalacion normal, local y offline | [Instalacion](install.md) |
| Arquitectura y flujo interno | [Arquitectura](architecture.md) |
| Rutas HTTP, requests, responses y docs runtime | [App y HTTP](app-http.md) |
| WebSocket y frames | [WebSocket](websocket.md) |
| SQLite, modelos y replicas | [Datos y ORM](data-orm.md) |
| Cache KV, cache HTTP y seguridad | [Cache y Seguridad](cache-security.md) |
| Metricas, logs y tareas | [Observabilidad y Tareas](observability-tasks.md) |
| DNS y reverse proxy | [Red y Edge](network-edge.md) |
| IA, estadistica y prediccion | [IA y Prediccion](ai-predict.md) |
| Exportaciones publicas | [API publica](api-map.md) |
| Ejemplo completo | [Proyecto integral](full-project.md) |

## Demo incluida

Despues de instalar el paquete:

```bash
wsbuilder --host 127.0.0.1 --port 8765
```

La demo publica HTTP, WebSocket, metricas, monitor HTML y documentacion runtime.

## Estado del paquete

El proyecto se declara como `Development Status :: 3 - Alpha` en `pyproject.toml`.
Tratalo como una libreria util para servicios internos, prototipos y laboratorios
controlados. Revisa limites, seguridad, TLS y apagado de recursos antes de
exponerlo fuera de localhost.

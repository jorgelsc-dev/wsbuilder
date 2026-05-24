# wsbuilder

`wsbuilder` es una libreria Python para construir servidores HTTP, WebSocket y utilidades de infraestructura en un unico paquete.

Se apoya en la biblioteca estandar y expone bloques pequenos y composables para:

- routing HTTP y respuestas tipadas.
- WebSocket de bajo nivel con control de frames.
- ORM ligero para SQLite.
- cache, seguridad, metricas y tareas en background.
- DNS local y replicas SQLite optimizadas.

## Por que destaca

- Menos superficie de dependencia en runtime.
- Flujo explicito: request, dispatch, respuesta y cierre.
- Modulos separados que puedes activar solo cuando los necesitas.
- API publica uniforme: `App`, `Response`, `Database`, `TaskManager`, `LocalDNSServer`.

## Instalacion

```bash
python -m pip install -e .
```

## Inicio rapido

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")
app.enable_metrics()

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("0.0.0.0", 8765)
```

## Mapa de la libreria

- `wsbuilder.framework`: fachada publica con `App`, `Request`, `Response`, `HTTPServer`, `WebSocket` y helpers.
- `wsbuilder.http`: parseo HTTP, request/response y streaming.
- `wsbuilder.ws`: handshake, frames y errores WebSocket.
- `wsbuilder.orm`: modelos SQLite, `QuerySet`, transacciones y helpers SQL.
- `wsbuilder.cache` y `wsbuilder.caches`: cache en memoria y cache declarativa de respuestas.
- `wsbuilder.security`: ACL, rate limiting y decision engine.
- `wsbuilder.metrics`: snapshot y stream de observabilidad.
- `wsbuilder.tasks`: trabajo asincrono controlado por `TaskManager`.
- `wsbuilder.dns`: servidor DNS UDP local.
- `wsbuilder.db_replicas`: lectura optimizada y pool de replicas SQLite.
- `wsbuilder.cookies` y `wsbuilder.headers`: utilidades HTTP de bajo nivel.
- `wsbuilder.predicts`: utilidad matematica `Predictor`.

## Casos de uso

1. APIs REST pequenas con respuestas JSON y HTML.
2. Chat, notificaciones y telemetria sobre WebSocket.
3. Persistencia local con SQLite y modelo declarativo.
4. Cache de rutas o contenido calculado.
5. Control de acceso, bloqueo temporal y observabilidad interna.
6. Procesos de background y lectura optimizada sobre SQLite.

## Documentacion

- [Inicio](docs/index.md)
- [Arquitectura](docs/architecture.md)
- [Referencia](docs/reference/index.md)

## Contribucion y soporte

- Crea ramas desde `main` usando `feat/<nombre>` o `fix/<nombre>`.
- Mantiene los cambios enfocados en un solo tema por PR.
- Abre el pull request hacia `main` con una descripcion corta y notas de riesgo.
- Si encuentras un problema de seguridad, reportalo de forma privada.

### Soporte opcional

Si `wsbuilder` te resulta util y quieres apoyar el mantenimiento del proyecto, puedes donar en BTC:

`bc1q3lhxpr9yantvefmvhpd2h4lu0ykf3t45zvuve2`

Red: `BTC mainnet` (Native SegWit).

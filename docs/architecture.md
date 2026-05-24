# Arquitectura

`wsbuilder` esta organizado como una capa HTTP central con integraciones opcionales alrededor.

## Flujo general

1. `App` registra rutas HTTP, API y WebSocket.
2. `HTTPServer` acepta conexiones TCP, aplica timeouts y parsea la request.
3. La request entra en `App.dispatch()`.
4. Si la ruta es `api`, la respuesta se serializa a JSON cuando el handler devuelve `dict` o `list`.
5. Si la ruta es `view`, el handler devuelve texto, HTML o un objeto `Response`.
6. Si la ruta es WebSocket, se hace handshake y se entrega un objeto `WebSocket` al handler.

## Componentes clave

- `wsbuilder.app`: router, `App`, pool por ruta y cierre ordenado.
- `wsbuilder.server`: socket server, limites de lectura y timeout.
- `wsbuilder.http`: `Request`, `Response`, parser y writer HTTP.
- `wsbuilder.ws`: handshake y protocolo WebSocket.
- `wsbuilder.metrics`: snapshot y stream NDJSON.
- `wsbuilder.security`: ACL, rate limiting y bloqueo temporal.
- `wsbuilder.cache` y `wsbuilder.caches`: cache de respuesta y cache SQLite en memoria.
- `wsbuilder.orm`: ORM SQLite.
- `wsbuilder.dns`: servidor DNS local.
- `wsbuilder.db_replicas`: lectura optimizada y pool de replicas.
- `wsbuilder.predicts`: utilidades matematicas / predictor.

## Views con workers

Las rutas `view()` pueden ejecutar su handler en un pool dedicado.

```python
@app.view("/jobs", min_threads=1, max_threads=4, requests_per_thread=0)
def jobs(_request):
    return "ok"
```

Puntos importantes:

- `min_threads` define el arranque del pool.
- `max_threads` limita el crecimiento.
- `requests_per_thread=0` desactiva el limite por worker.
- La distribucion es `least_busy`.
- La respuesta incluye metadatos del worker en headers y cookie firmada.

## Integraciones opcionales

- `app.enable_metrics()` instala `/api/metrics` y `/api/metrics/stream`.
- `install_cache(app, ...)` expone cache simple en memoria.
- `install_caches(app, ...)` activa reglas de cache de respuesta.
- `install_security(app, ...)` conecta el motor de seguridad.

## Cierre

`App.close()` cierra:

- caches instaladas.
- pools de workers por ruta.

`HTTPServer.serve_forever()` tambien invoca `app.close()` durante el apagado.


# Arquitectura

`wsbuilder` esta organizado como un nucleo HTTP/WebSocket pequeno con modulos opcionales alrededor.

## Flujo general

```text
Cliente
  -> HTTPServer
  -> parse_http_request()
  -> App.dispatch()
  -> Router.resolve()
  -> opcional: cache / security / task hooks
  -> handler
  -> Response / dict / list / str
  -> send_http_response()
```

Para WebSocket el flujo cambia en el ultimo tramo:

```text
Cliente WS
  -> HTTPServer
  -> is_ws_request()
  -> handshake_websocket_with_options()
  -> WebSocket
  -> recv_frame() / send_*()
  -> cierre con parse_close_payload()
```

## Componentes clave

- `wsbuilder.app`: registro de rutas, dispatch, CORS y pools por ruta.
- `wsbuilder.server`: socket server, limites de lectura, timeout y cierre.
- `wsbuilder.http`: `Request`, `Response`, parseo y escritura HTTP.
- `wsbuilder.ws`: handshake, frames, errores y helpers de bajo nivel.
- `wsbuilder.metrics`: snapshot, stream NDJSON y metadatos del servidor.
- `wsbuilder.security`: ACL, rate limiting, listas y bloqueos temporales.
- `wsbuilder.cache` y `wsbuilder.caches`: cache en memoria y cache declarativa de respuestas.
- `wsbuilder.tasks`: ejecucion en background con control de capacidad y estados.
- `wsbuilder.orm`: ORM SQLite con `QuerySet`, transacciones y filtros.
- `wsbuilder.dns`: DNS UDP local.
- `wsbuilder.db_replicas`: optimizacion de lectura y replicas SQLite.
- `wsbuilder.predicts`: utilidad matematica `Predictor`.

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
- La respuesta puede incluir metadatos del worker en headers y cookie firmada.

## Integraciones opcionales

- `app.enable_metrics()` instala `/api/metrics` y `/api/metrics/stream`.
- `install_cache(app, ...)` expone cache simple en memoria.
- `install_caches(app, ...)` activa reglas de cache de respuesta.
- `install_security(app, ...)` conecta el motor de seguridad.
- `TaskManager` da una cola de trabajos con cancelacion, grupos y estados.

## Cierre

`App.close()` cierra:

- caches instaladas.
- pools de workers por ruta.
- tareas en background vinculadas al `TaskManager`.

`HTTPServer.serve_forever()` tambien invoca `app.close()` durante el apagado.

## Por que este diseño funciona bien

- Separa protocolos de negocio.
- Te deja usar solo el modulo que necesitas.
- Permite extender desde el nivel alto (`App`) o el nivel bajo (`Request`, `Response`, frames WS).
- Hace visible el camino completo de una request, lo que simplifica diagnostico y pruebas.

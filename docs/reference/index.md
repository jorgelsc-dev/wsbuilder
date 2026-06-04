# Referencia

Esta seccion resume la API publica exportada por `wsbuilder` y la organiza por modulo para que puedas ir directo a la pieza que necesitas.

## Atajo de importacion

```python
from wsbuilder import (
    App,
    Response,
    Database,
    Model,
    LocalDNSServer,
    TaskManager,
    SQLiteMemoryCache,
)
```

## Mapa de modulos

<div class="diagram">
<div class="diagram-title">Mapa de modulos</div>
<div class="diagram-track">
<div class="diagram-node">App / HTTPServer</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">HTTP y respuesta</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">WebSocket</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Cache / Seguridad</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Metricas / Tareas / ORM</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">Cada bloque de la API resuelve una parte distinta del servicio y se puede leer por separado.</div>
</div>

## Guia rapida por modulo

### [HTTP y respuesta](http.md)

- `Request` para representar la entrada.
- `Response` para devolver texto, JSON, HTML o stream.
- `parse_http_request()` y `send_http_response()` para trabajar al nivel del protocolo.

### [WebSocket](websocket.md)

- handshake, frames y errores de protocolo.
- `WebSocket` para sesiones persistentes.
- util si quieres chat, telemetria o eventos en tiempo real.

### [ORM](orm.md)

- `Database`, `Model`, `QuerySet` y `Transaction`.
- ideal para servicios con SQLite local o embebido.

### [Cache](cache.md)

- `SQLiteMemoryCache` para valores TTL, namespaces y tags.
- `install_cache()` y `install_caches()` para integrar cache con rutas `view()`.

### [Seguridad](security.md)

- `SecurityPolicy`, `ACLRule` y `install_security()`.
- filtra por IP, path, metodo, headers y comportamiento.

### [Metricas](metrics.md)

- `AppMetrics` y `install_metrics()`.
- expone snapshot JSON y stream NDJSON.

### [Tareas en background](tasks.md)

- `TaskManager`, `TaskHandle` y `TaskContext`.
- controla concurrencia, cancelacion y estados.

### [Utilidades HTTP](utilities.md)

- helpers de headers y cookies.
- utiles cuando necesitas una capa de protocolo clara y ligera.

### [DNS local](dns.md)

- `LocalDNSServer` para laboratorios y entornos de desarrollo.

### [Avanzado](advanced.md)

- replicas SQLite, optimizacion y piezas auxiliares.

## Como se conectan en una app

<div class="diagram">
<div class="diagram-title">Como se conectan en una app</div>
<div class="diagram-track">
<div class="diagram-node">Request</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">App</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Router</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Handler</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Response</div>
</div>
<div class="diagram-rows" style="margin-top: 1rem;">
<div class="diagram-row">
<div class="diagram-step">Security / Metrics</div>
<div class="diagram-arrow">→</div>
<div class="diagram-step">Cache / Tasks / Utilities</div>
<div class="diagram-note">Se activan como capas transversales durante la ejecucion.</div>
</div>
<div class="diagram-row">
<div class="diagram-step">ORM</div>
<div class="diagram-arrow">→</div>
<div class="diagram-step">SQLite</div>
<div class="diagram-note">La persistencia queda aislada del transporte.</div>
</div>
</div>
</div>

La regla practica es:

- `App` coordina.
- `HTTPServer` transporta.
- `Request` y `Response` modelan entrada y salida.
- `ORM`, `Cache`, `Security`, `Metrics` y `Tasks` agregan capacidades transversales.

## Exportaciones destacadas

- `App`, `Router`, `Route`
- `Request`, `Response`, `HTTPServer`
- `WebSocket`, `WebSocketFrame`, `WebSocketProtocolError`
- `Database`, `Model`, `QuerySet`, `Transaction`
- `SQLiteMemoryCache`, `install_cache`, `install_caches`
- `SecurityPolicy`, `ACLRule`, `install_security`
- `AppMetrics`, `install_metrics`
- `TaskManager`, `TaskHandle`, `TaskContext`
- `LocalDNSServer`
- `DatabaseReplica`, `DatabaseReplicaPool`, `OptimizedDatabase`, `SQLite3OptimizationConfig`
- `Predictor`

## Seleccion rapida

- Si quieres leer una request y responder, ve a [HTTP y respuesta](http.md).
- Si necesitas tiempo real, ve a [WebSocket](websocket.md).
- Si necesitas datos, ve a [ORM](orm.md).
- Si necesitas observabilidad, ve a [Metricas](metrics.md).
- Si necesitas trabajo diferido, ve a [Tareas en background](tasks.md).
- Si estas montando una topologia distribuida, ve a [Ayuda](../help/index.md).

# Referencia

Esta seccion resume la API publica exportada por `wsbuilder`.

## Importes principales

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

## Bloques de la API

- [HTTP y respuesta](http.md)
- [WebSocket](websocket.md)
- [ORM](orm.md)
- [Cache](cache.md)
- [Seguridad](security.md)
- [Metricas](metrics.md)
- [Tareas en background](tasks.md)
- [Utilidades HTTP](utilities.md)
- [DNS local](dns.md)
- [Avanzado](advanced.md)

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

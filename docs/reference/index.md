# Referencia

Esta seccion resume la API publica exportada por `wsbuilder`.

## Importes principales

```python
from wsbuilder import App, Response, Database, Model, LocalDNSServer
```

## Bloques de la API

- [HTTP y respuesta](http.md)
- [WebSocket](websocket.md)
- [ORM](orm.md)
- [Cache](cache.md)
- [Seguridad](security.md)
- [Metricas](metrics.md)
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
- `LocalDNSServer`
- `DatabaseReplica`, `DatabaseReplicaPool`, `OptimizedDatabase`
- `Predictor`


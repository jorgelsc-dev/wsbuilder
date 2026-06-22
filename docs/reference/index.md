# Referencia

Esta pagina resume la superficie publica del paquete y te lleva al modulo correcto sin hacerte recorrer el codigo.

## Importacion recomendada

```python
from wsbuilder import App, Response, Database, Model, SQLiteMemoryCache
```

## Mapa de modulos

| Area | Exportes clave | Uso |
| --- | --- | --- |
| Core HTTP | `App`, `HTTPServer`, `Request`, `Response`, `Route`, `Router` | Servir HTML, JSON y servicios mixtos |
| WebSocket | `WebSocket`, `handshake_websocket_with_options`, `parse_close_payload` | Canales persistentes |
| Tareas | `TaskManager`, `TaskHandle`, `TaskContext`, `TASK_*` | Background jobs y cancelacion |
| Cache | `Cache`, `SQLiteMemoryCache`, `ViewResponseCache`, `GlobalCacheRule` | TTL, tags y cache de respuestas |
| Seguridad | `SecurityPolicy`, `SecurityDecision`, `ACLRule` | ACL, listas y rate limit |
| Metricas | `AppMetrics`, `install_metrics` | Snapshots y streaming |
| Logs | `NDJSONLog`, `install_logs` | Telemetria simple en NDJSON |
| Persistencia | `Database`, `Model`, `QuerySet`, `Transaction`, fields | ORM SQLite |
| Replicas | `DatabaseReplica`, `DatabaseReplicaPool`, `OptimizedDatabase`, `SQLite3OptimizationConfig` | Lecturas optimizadas |
| Proxy | `ProxyI`, `proxyi`, `ProxyRule`, `ProxyRouteBuilder`, `ProxyTarget` | Reverse proxy y balancing |
| DNS | `LocalDNSServer` | Resolucion local y tests |
| IA | `DataSet`, `DataSummary`, `ErrorSummary`, `NeuralNetwork`, `Predictor` | Datos y modelos sencillos |
| Utilidades | `get_header`, `set_header`, `build_set_cookie`, `parse_cookie_header` | Helpers HTTP |

## Core HTTP

### `App`

Principales metodos:

- `view(path, ...)`
- `api(path, ...)`
- `ws(path, ...)`
- `enable_docs(path="/docs", json_path="/docs.json", ...)`
- `enable_metrics(path="/api/metrics", stream_path="/api/metrics/stream", ...)`
- `enable_security(policy=None)`
- `enable_caches(caches=None)`
- `enable_logs(path="logs/wsbuilder.ndjson", ...)`
- `dispatch(request)`
- `run(host, port, ssl_context=None)`

### `Request`

```python
request.method
request.path
request.query
request.headers
request.body
request.client
request.tls
request.text()
request.json()
```

### `Response`

```python
Response.text("ok")
Response.html("<h1>ok</h1>")
Response.json({"ok": True})
Response.stream(chunks, content_type="text/plain")
```

## WebSocket

- `WebSocket.recv_frame()`
- `WebSocket.send_text()`
- `WebSocket.send_binary()`
- `WebSocket.send_ping()`
- `WebSocket.send_pong()`
- `WebSocket.close()`
- `WebSocketFrame`
- `WebSocketReadError`
- `WebSocketReadTimeoutError`
- `WebSocketConnectionClosedError`
- `WebSocketProtocolError`

## Tareas

- `TaskManager.spawn()`
- `TaskManager.submit()`
- `TaskManager.cancel()`
- `TaskManager.cancel_group()`
- `TaskManager.cancel_all()`
- `TaskManager.list()`
- `TaskManager.snapshot()`
- `TaskHandle.get()`
- `TaskHandle.wait()`
- `TaskHandle.cancel()`

## Cache

### `SQLiteMemoryCache`

```python
set, add, replace, get, pop, delete
set_many, get_many, delete_many
incr, decr, ttl, touch, expire
tag, untag, get_tags, invalidate_tag, invalidate_tags
count, size_bytes, stats, metrics_snapshot
```

### `ViewResponseCache`

```python
declare_global, set_global_wildcard, set_global_mimetype
fetch, store_response, invalidate_path, clear, snapshot
```

## Seguridad

- `SecurityPolicy.evaluate(request)`
- `SecurityPolicy.observe_response(request, status_code)`
- `SecurityPolicy.block_ip()`
- `SecurityPolicy.unblock_ip()`
- `SecurityPolicy.snapshot()`

## Persistencia

- `Database.execute()`
- `Database.fetchall()`
- `Database.fetchone()`
- `Database.scalar()`
- `Database.transaction()`
- `Model.create_table()`
- `Model.drop_table()`
- `Model.objects()`
- `QuerySet.filter()`
- `QuerySet.exclude()`
- `QuerySet.values()`
- `QuerySet.paginate()`
- `create_tables(db, *models)`
- `drop_tables(db, *models)`

### Fields

- `Field`
- `IntegerField`
- `TextField`
- `RealField`
- `BlobField`
- `BooleanField`
- `DateTimeField`
- `JSONField`

## Replicas

- `DatabaseReplica.connect()`
- `DatabaseReplica.execute()`
- `DatabaseReplicaPool.get_replica()`
- `DatabaseReplicaPool.fetchall()`
- `OptimizedDatabase.set_pragma()`
- `OptimizedDatabase.get_pragma()`

## Proxy y DNS

- `ProxyI.route()`
- `ProxyI.vhost()`
- `ProxyI.location()`
- `ProxyI.default()`
- `ProxyI.dispatch()`
- `ProxyRule.matches()`
- `ProxyRule.choose_target()`
- `ProxyRouteBuilder.build()`
- `ProxyTarget.snapshot()`
- `LocalDNSServer.start()`
- `LocalDNSServer.serve_forever()`
- `LocalDNSServer.add_record()`

## IA

- `DataSet.split()`
- `DataSet.describe_features()`
- `DataSet.describe_targets()`
- `DenseLayer.forward()`
- `NeuralNetwork.add_dense()`
- `NeuralNetwork.fit()`
- `NeuralNetwork.fit_classification()`
- `NeuralNetwork.predict()`
- `NeuralNetwork.predict_class()`
- `Predictor.fit()`
- `Predictor.predict()`
- `describe_data()`
- `evaluate_errors()`
- `submit_training_task()`

## Utilidades

- `normalize_header_name()`
- `get_header()`
- `has_header()`
- `set_header()`
- `parse_cookie_header()`
- `get_cookie()`
- `build_set_cookie()`
- `parse_query_string()`
- `normalize_balance_mode()`
- `normalize_target()`

## Siguiente nivel

Si ya sabes que modulo necesitas, abre la pagina dedicada de esa seccion en la guia.
La referencia esta pensada para buscar nombres y firmas exactas, no para explicar el concepto desde cero.

# API publica

Esta pagina funciona como mapa rapido de exportaciones publicas visibles desde
`from wsbuilder import ...`.

## Nucleo

| Grupo | Exportaciones |
| --- | --- |
| App | `App`, `Route`, `Router`, `HTTPServer` |
| HTTP | `Request`, `Response`, `parse_query_string` |
| WebSocket | `WebSocket`, `parse_close_payload`, `handshake_websocket_with_options` |

## Tareas y concurrencia

| Grupo | Exportaciones |
| --- | --- |
| Tareas | `TaskManager`, `TaskHandle`, `TaskContext` |
| Errores | `TaskError`, `TaskClosedError`, `TaskCancelledError`, `TaskRejectedError` |
| Estados | `TASK_PENDING`, `TASK_RUNNING`, `TASK_COMPLETED`, `TASK_FAILED`, `TASK_CANCELLED`, `TASK_REJECTED` |

## Cache y seguridad

| Grupo | Exportaciones |
| --- | --- |
| Cache KV | `Cache`, `SQLiteMemoryCache`, `install_cache` |
| Cache HTTP | `GlobalCacheRule`, `ViewResponseCache`, `install_caches` |
| Seguridad | `ACLRule`, `SecurityDecision`, `SecurityPolicy`, `install_security` |

## Observabilidad

| Grupo | Exportaciones |
| --- | --- |
| Metricas | `AppMetrics`, `install_metrics` |
| Logs | `NDJSONLog`, `install_logs` |

## Persistencia

| Grupo | Exportaciones |
| --- | --- |
| ORM | `Database`, `Model`, `QuerySet`, `Transaction`, `Field` |
| Campos | `IntegerField`, `TextField`, `RealField`, `BlobField`, `BooleanField`, `DateTimeField`, `JSONField` |
| Helpers | `SQL`, `create_tables`, `drop_tables`, `quote_identifier`, `validate_identifier` |
| Replicas | `DatabaseReplica`, `DatabaseReplicaPool`, `OptimizedDatabase`, `SQLite3OptimizationConfig` |

## Utilidades HTTP

| Grupo | Exportaciones |
| --- | --- |
| Cabeceras | `normalize_header_name`, `get_header`, `has_header`, `set_header` |
| Cookies | `parse_cookie_header`, `get_cookie`, `build_set_cookie` |

## DNS y proxy

| Grupo | Exportaciones |
| --- | --- |
| DNS | `LocalDNSServer` |
| Proxy | `ProxyI`, `proxyi`, `ProxyRule`, `ProxyRouteBuilder`, `ProxyTarget`, `ProxyMetricsBucket`, `RunningStats`, `install_proxyi`, `normalize_balance_mode`, `normalize_target` |

## Balanceo soportado

- `BALANCING_ROUND_ROBIN`
- `BALANCING_WEIGHTED_ROUND_ROBIN`
- `BALANCING_RANDOM`
- `BALANCING_LEAST_CONNECTIONS`
- `BALANCING_LEAST_RESPONSE_TIME`
- `BALANCING_LEAST_REQUESTS`
- `BALANCING_LEAST_BYTES_IN`
- `BALANCING_LEAST_BYTES_OUT`
- `BALANCING_IP_HASH`
- `BALANCING_CONSISTENT_HASH`
- `BALANCING_FIRST_AVAILABLE`
- `BALANCING_POWER_OF_TWO_CHOICES`
- `BALANCING_BEST`
- `SUPPORTED_BALANCING_STRATEGIES`

## IA y prediccion

| Grupo | Exportaciones |
| --- | --- |
| Datos | `DataSet`, `DataSummary`, `ErrorSummary` |
| Redes | `DenseLayer`, `NeuralNetwork`, `submit_training_task` |
| Analisis | `describe_data`, `evaluate_errors` |
| Prediccion | `Predictor` |

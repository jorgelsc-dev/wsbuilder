# Que puedes hacer con wsbuilder

`wsbuilder` es un paquete Python puro para construir servicios pequenos o
medianos sin depender de frameworks externos. La idea central es componer piezas:
creas una `App`, registras rutas, y activas solo las capas que necesitas.

## Resumen por area

| Area | Sirve para | APIs principales |
| --- | --- | --- |
| HTTP | APIs JSON, vistas HTML, texto, streaming y demo local | `App`, `Request`, `Response`, `HTTPServer` |
| WebSocket | Echo, chat, notificaciones, telemetria y canales persistentes | `@app.ws`, `WebSocket`, `parse_close_payload` |
| Datos | Persistencia SQLite embebida, modelos declarativos y consultas | `Database`, `Model`, `QuerySet`, campos ORM |
| Replicas SQLite | Separar lecturas de la conexion principal y aplicar pragmas de rendimiento | `DatabaseReplica`, `DatabaseReplicaPool`, `OptimizedDatabase` |
| Cache KV | Guardar valores temporales con TTL, tags, namespaces y contadores | `SQLiteMemoryCache`, `install_cache` |
| Cache HTTP | Cachear respuestas de vistas por ruta, query, headers y reglas globales | `ViewResponseCache`, `GlobalCacheRule` |
| Seguridad | ACL, listas de IP, rate limit, bloqueo temporal y deteccion de respuestas sospechosas | `SecurityPolicy`, `ACLRule`, `SecurityDecision` |
| Observabilidad | Metricas JSON, stream NDJSON, snapshots de integraciones y logs | `AppMetrics`, `NDJSONLog`, `enable_metrics`, `enable_logs` |
| Tareas | Trabajo en background, cancelacion cooperativa y limites de concurrencia | `TaskManager`, `TaskHandle`, `submit_training_task` |
| DNS | Servidor DNS UDP local autoritativo con wildcard y fallback upstream | `LocalDNSServer` |
| Proxy | Reverse proxy, balanceo, metricas por regla/target y dashboard HTML | `ProxyI`, `ProxyRule`, `ProxyTarget` |
| IA y prediccion | Regresion simple, red neuronal desde cero y estadistica de error | `Predictor`, `DataSet`, `NeuralNetwork` |
| Utilidades HTTP | Normalizar headers y construir cookies seguras para respuestas | `get_header`, `set_header`, `build_set_cookie` |

## Casos de uso recomendados

- Prototipos y herramientas internas que necesitan HTTP, JSON y WebSocket en un
  solo archivo.
- Servicios pequenos con SQLite local, metricas y tareas en background.
- Demos de infraestructura donde conviene mostrar DNS, proxy, cache y seguridad
  sin instalar servicios externos.
- Pruebas o laboratorios donde quieres controlar el protocolo HTTP/WS desde
  Python puro.
- Aplicaciones de borde con reverse proxy simple, reglas por host/path/header y
  observabilidad embebida.

## Lo que no intenta reemplazar

`wsbuilder` no es ASGI/WSGI, no trae middleware ecosystem, no incluye plantillas
HTML avanzadas ni cliente HTTP asincrono. Para aplicaciones grandes con muchas
integraciones externas, un framework establecido puede ser una mejor base. Aqui
el valor esta en una superficie pequena, auditable y sin dependencias de runtime.

## Que leer segun tu objetivo

| Objetivo | Lectura recomendada |
| --- | --- |
| Levantar la primera API | [Principiantes](beginners.md) y [App y HTTP](app-http.md) |
| Crear una app con datos y cache | [Intermedios](intermediate.md), [Datos y ORM](data-orm.md), [Cache y Seguridad](cache-security.md) |
| Exponer metricas y tareas | [Observabilidad y Tareas](observability-tasks.md) |
| Agregar WebSocket | [WebSocket](websocket.md) |
| Usar DNS o reverse proxy | [Red y Edge](network-edge.md) |
| Preparar despliegue interno | [Avanzados](advanced.md) |
| Ver todas las exportaciones | [API publica](api-map.md) |

## Ejemplo de composicion

```python
from wsbuilder import (
    App,
    Response,
    SecurityPolicy,
    SQLiteMemoryCache,
    ViewResponseCache,
    install_cache,
)

app = App(cors_allow_origin="*")
app.enable_metrics(app_name="service")
app.enable_security(SecurityPolicy(rate_limit_requests=120))
install_cache(app, SQLiteMemoryCache(default_ttl=60))
app.enable_caches(ViewResponseCache(default_ttl=20))
app.enable_docs(path="/docs", json_path="/docs.json")

@app.view("/", cache={"ttl": 10})
def home(_request):
    return Response.html("<h1>service</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("127.0.0.1", 8765)
```

Ese patron es la base del proyecto: primero una `App`, luego rutas, y despues
capas opcionales con `enable_*` o `install_*`.

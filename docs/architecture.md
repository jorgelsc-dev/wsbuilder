# Arquitectura

`wsbuilder` esta organizado como un paquete modular donde `App` actua como
fachada principal y el resto de modulos se activan por composicion.

## Mapa general

| Area | Modulos | Superficie principal |
| --- | --- | --- |
| Nucleo HTTP | `app.py`, `http.py`, `server.py` | `App`, `HTTPServer`, `Request`, `Response`, `Route`, `Router` |
| WebSocket | `ws.py` | `WebSocket`, `WebSocketFrame`, handshake y utilidades de frames |
| Persistencia | `orm.py`, `db_replicas.py` | `Database`, `Model`, `QuerySet`, `OptimizedDatabase`, `DatabaseReplicaPool` |
| Cache | `cache.py`, `caches.py` | `SQLiteMemoryCache`, `ViewResponseCache`, `install_cache`, `install_caches` |
| Seguridad | `security.py` | `SecurityPolicy`, `ACLRule`, `SecurityDecision`, `install_security` |
| Observabilidad | `metrics.py`, `logs.py`, `tasks.py` | `AppMetrics`, `NDJSONLog`, `TaskManager` |
| Red y edge | `dns.py`, `proxyi.py` | `LocalDNSServer`, `ProxyI`, `ProxyRule`, `ProxyTarget` |
| IA y prediccion | `ia.py`, `predicts.py` | `DataSet`, `NeuralNetwork`, `DenseLayer`, `Predictor` |
| Compatibilidad | `framework.py`, `__init__.py` | reexport de la API publica |

## Flujo de ejecucion

1. `HTTPServer` acepta la conexion TCP y parsea la peticion HTTP.
2. Se crea un `Request` con `method`, `path`, `query`, `headers`, `body`,
   `client` y metadatos TLS.
3. `App.dispatch()` aplica seguridad, cache de vistas y resolucion de rutas.
4. El handler devuelve `Response`, `dict`, `list`, `str`, `bytes` o `None`.
5. Para rutas `api`, los `dict` y `list` se convierten automaticamente en JSON.
6. `send_http_response()` serializa la respuesta, incluyendo streaming si aplica.
7. Si la ruta es WebSocket, el servidor hace el handshake y delega al handler.

## Estilo de composicion

El proyecto evita acoplar todo por herencia. Lo habitual es:

- crear `App()`.
- habilitar piezas opcionales con `enable_*` o `install_*`.
- registrar rutas HTTP y WS con decoradores.
- adjuntar recursos propios a la instancia, por ejemplo `app.db` o `app.proxyi`.

Ejemplo minimo:

```python
from wsbuilder import App, Response, SecurityPolicy, SQLiteMemoryCache, install_cache

app = App(cors_allow_origin="*")
app.enable_metrics(app_name="service")
app.enable_security(SecurityPolicy(rate_limit_requests=120))
install_cache(app, SQLiteMemoryCache(default_ttl=60))

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}
```

## Superficies de entrada

- `from wsbuilder import ...`: forma recomendada para consumir la API publica.
- `python -m wsbuilder`: levanta la demo incluida del paquete.
- `framework.py`: fachada de compatibilidad que reexporta casi toda la capa
  publica original.

## Documentacion en runtime

`App.enable_docs()` expone dos superficies automaticas:

- `path`: HTML navegable para inspeccionar rutas y capacidades activas.
- `json_path`: snapshot JSON del estado publico de la aplicacion.

Eso es util para demos, entornos internos y validacion rapida sin depender de
OpenAPI ni generadores externos.

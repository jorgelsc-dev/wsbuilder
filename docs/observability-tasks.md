# Observabilidad y Tareas

## Metricas con `AppMetrics`

La forma mas directa de activarlas es:

```python
app.enable_metrics(
    path="/api/metrics",
    stream_path="/api/metrics/stream",
    app_name="service",
)
```

Esto publica:

- snapshot JSON en `/api/metrics`
- stream NDJSON en `/api/metrics/stream`

La informacion incluye HTTP, conexiones TCP, WebSocket, errores y, cuando la
aplicacion las tiene activadas, snapshots extra de cache, seguridad, proxy y
workers por ruta.

Campos principales del snapshot:

| Campo | Contenido |
| --- | --- |
| `connections` | TCP activos/totales, HTTP en vuelo y WS activos |
| `http` | requests, responses, metodos, paths, status y latencias |
| `websocket` | upgrades, mensajes, bytes y paths |
| `traffic` | bytes de entrada y salida |
| `errors` | contador y ultimo error registrado |
| `threads` | worker pools de rutas `view` |

## Streaming de metricas

`AppMetrics.response_stream()` permite seguir los puntos de forma continua.

Parametros utiles del endpoint generado:

- `interval`
- `limit`
- `follow=1`

`limit` produce un stream finito. `follow=1` lo mantiene abierto. El intervalo se
normaliza entre `0.1` y `60.0` segundos.

## Logs NDJSON

```python
logs = app.enable_logs(path="logs/wsbuilder.ndjson")
logs.event("request", method="GET", path="/")
```

`NDJSONLog` mantiene un registro linea a linea, facil de consumir con `jq`,
scripts shell o pipelines de procesamiento. Las escrituras concurrentes dentro
del mismo proceso se serializan y cada registro se emite con una sola escritura.

Metodos:

- `append(record)`
- `event(name, **fields)`
- `describe()`
- `close()`

Tambien existe `append_ndjson(path, record)` en `wsbuilder.logs` para escribir un
registro suelto sin crear una instancia persistente.

## Tareas en background

Cada `App` crea `app.tasks = TaskManager(app=self)` automaticamente.

```python
@app.api("/api/tasks/run")
def launch(request):
    def worker():
        return {"ok": True}

    task = request.app.tasks.spawn(worker, name="demo-task", group="jobs", request=request)
    return {"task_id": task.id}
```

## `TaskManager`

Metodos principales:

- `spawn(...)`
- `get(task_id)`
- `list(group=None, status=None)`
- `cancel(task_id)`
- `cancel_group(group)`
- `cancel_all()`
- `wait(task_id, timeout=None)`
- `result(task_id, timeout=None)`
- `snapshot()`
- `close(wait=True, timeout=None)`

`max_concurrent` permite limitar concurrencia con semaforo interno.
`spawn()` y `close()` se coordinan de forma atomica: una tarea iniciada antes del
cierre queda registrada y participa en la espera; las posteriores se rechazan.

Parametros utiles de `spawn()`:

| Parametro | Uso |
| --- | --- |
| `name` | nombre humano de la tarea |
| `group` | agrupar para listar o cancelar |
| `metadata` | datos serializables para snapshots |
| `daemon` | controla si el thread es daemon |
| `pass_handle` | pasa el `TaskHandle` a la funcion |
| `request` | adjunta metadatos de la solicitud |
| `timeout_seconds` | tiempo maximo esperando cupo cuando `max_concurrent` limita |

## `TaskHandle`

Cada tarea expone estado y resultado. `context.app` usa la aplicacion del
`TaskManager` aunque no se adjunte un request. Cancelar una tarea ya terminal es
una operacion nula y devuelve `False`.

- `status`, `running`, `started`, `finished`, `cancelled`
- `wait(timeout=None)`, `join(timeout=None)`, `get(timeout=None)`
- `result`, `exception`, `snapshot()`

`finished` y `started` son eventos de `threading.Event`; por eso se consultan con
`task.finished.is_set()` o `task.started.wait(...)`.

Errores especificos:

- `TaskError`
- `TaskClosedError`
- `TaskCancelledError`
- `TaskRejectedError`

## `app.describe()`

`App.describe()` produce un snapshot amplio de la aplicacion:

- rutas HTTP y WS
- metricas
- seguridad
- proxy
- cache
- logs
- tareas

Es la base de la documentacion runtime generada por `enable_docs()`.

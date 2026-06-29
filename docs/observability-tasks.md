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

## Streaming de metricas

`AppMetrics.response_stream()` permite seguir los puntos de forma continua.

Parametros utiles del endpoint generado:

- `interval`
- `limit`
- `follow=1`

## Logs NDJSON

```python
logs = app.enable_logs(path="logs/wsbuilder.ndjson")
logs.event("request", method="GET", path="/")
```

`NDJSONLog` mantiene un registro linea a linea, facil de consumir con `jq`,
scripts shell o pipelines de procesamiento.

Metodos:

- `append(record)`
- `event(name, **fields)`
- `describe()`
- `close()`

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

## `TaskHandle`

Cada tarea expone estado y resultado:

- `status()`, `running()`, `finished()`, `cancelled()`
- `wait(timeout=None)`, `join(timeout=None)`, `get(timeout=None)`
- `result()`, `exception()`, `snapshot()`

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

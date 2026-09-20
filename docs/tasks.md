# Tareas

`TaskManager` ejecuta trabajo en background con estados, cancelacion y control de concurrencia.

## Estados

- `pending`
- `running`
- `completed`
- `failed`
- `cancelled`
- `rejected`

## Spawning

```python
from wsbuilder import App

app = App()

def worker():
    return {"ok": True}

task = app.tasks.spawn(worker, name="job-1", group="jobs")
result = task.get(timeout=5)
```

## Concurrencia

```python
from wsbuilder import TaskManager

manager = TaskManager(max_concurrent=2)
```

Si `max_concurrent` es mayor que cero, el manager usa un semaforo para limitar tareas simultaneas.

## Cancelacion

```python
task.cancel()
```

La cancelacion es cooperativa. El trabajador debe respetar `task.cancel_event` o salir por su cuenta.

## `TaskHandle`

`TaskHandle` expone:

- `status`
- `started_at`
- `finished_at`
- `result`
- `exception`
- `wait()`
- `get()`
- `cancel()`

Si pasas `pass_handle=True`, el worker recibe el propio handle como primer argumento.

## Ejemplo con request

```python
@app.view("/launch")
def launch(request):
    def worker():
        return {"path": request.path}

    task = request.app.tasks.spawn(worker, request=request, group="jobs")
    return {"task_id": task.id}
```

## Snapshot

`TaskManager.snapshot()` devuelve un resumen con:

- total de tareas
- conteos por estado
- tareas por grupo
- metadatos basicos de cada handle

## Integracion con IA

`submit_training_task()` envuelve un entrenamiento de `NeuralNetwork` para ejecutarlo en background.
Es la forma mas simple de mover un entrenamiento corto fuera del request path.

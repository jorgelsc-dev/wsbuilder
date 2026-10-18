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

manager = TaskManager(max_concurrent=2, max_pending_tasks=512)
```

Con `max_concurrent > 0`, solo las tareas en ejecución tienen un hilo. El manager
mantiene hasta ese número de hilos de trabajo y una cola con hasta
`max_pending_tasks` tareas adicionales (512 por defecto). Cuando se agota la
capacidad, `spawn()` lanza `TaskRejectedError` sin crear otro hilo. El valor cero
de `max_pending_tasks` permite solo las plazas de ejecución.

`timeout_seconds` limita la espera en la cola; una tarea cuyo plazo vence termina
con estado `rejected`. Cancelar una tarea pendiente la retira y libera su
capacidad inmediatamente. El plazo no interrumpe una tarea que ya está ejecutándose.

El manager usa un único hilo de coordinación daemon mientras hay trabajo y,
si hay tareas pendientes con `daemon=False`, un único guardián no daemon para
mantener vivo el proceso hasta que puedan empezar. Estos dos auxiliares están
acotados y terminan al dejar de ser necesarios. Cada tarea conserva su opción
`daemon` cuando empieza.

`max_concurrent=0` conserva el modo sin límite: cada `spawn()` inicia su propio
hilo y `max_pending_tasks` no se aplica. Usa un límite positivo para acotar recursos.

`close(wait=True, timeout=None)` cancela el trabajo pendiente y solicita la
cancelación del trabajo en ejecución. Desde fuera de una tarea espera su cierre,
respetando el plazo global indicado. Cuando una tarea llama a `close()`, solicita
la cancelación y retorna sin esperar a otras tareas para evitar ciclos de espera.

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

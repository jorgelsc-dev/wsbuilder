# Tareas en background

<div class="diagram">
<div class="diagram-title">Tareas en background</div>
<div class="diagram-track">
<div class="diagram-node">Request</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">spawn</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">TaskHandle</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Worker</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Result</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">La tarea se ejecuta aparte y luego deja un snapshot con estado, resultado o error.</div>
</div>

`TaskManager` permite ejecutar trabajo asincrono con control de concurrencia, cancelacion y estados.

## Tipos principales

- `TaskManager`
- `TaskHandle`
- `TaskContext`
- `TaskError`
- `TaskClosedError`
- `TaskCancelledError`
- `TaskRejectedError`

## Estados

- `TASK_PENDING`
- `TASK_RUNNING`
- `TASK_COMPLETED`
- `TASK_FAILED`
- `TASK_CANCELLED`
- `TASK_REJECTED`

## Flujo

1. Llamas `spawn()` con una funcion target.
2. Obtienes un `TaskHandle`.
3. Observas `status`, `result`, `exception` o `snapshot()`.
4. Cancelas con `cancel()` si la tarea aun no debe terminar.

## Ejemplo

```python
from wsbuilder import TaskManager

tasks = TaskManager(max_concurrent=4)

def build_report():
    return {"ok": True}

job = tasks.spawn(build_report, name="report")
job.wait()
print(job.result)
```

## Capacidades

- Limite global de concurrencia.
- Agrupacion por `group`.
- Metadatos asociados a request o contexto propio.
- `pass_handle=True` para pasar el handle a la funcion.
- `timeout_seconds` para rechazar tareas que no consiguen hueco.

## Casos de uso

- Generacion de reportes.
- Procesamiento diferido de requests largas.
- Sincronizacion o tareas de mantenimiento.
- Fan-out controlado para trabajo paralelo.

## Rol del modulo

- Evita bloquear el ciclo HTTP con trabajo pesado.
- Da visibilidad al estado, resultado y error de cada tarea.
- Permite modelar jobs locales sin introducir un orquestador externo.

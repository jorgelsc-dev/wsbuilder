# Logs

<div class="diagram">
<div class="diagram-title">Logs NDJSON</div>
<div class="diagram-track">
<div class="diagram-node">Evento</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">JSON</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Archivo .ndjson</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">Cada append deja una linea JSON valida y no reescribe el archivo completo.</div>
</div>

`wsbuilder.logs` ofrece un writer minimo para trazas NDJSON en disco.

## API principal

- `NDJSONLog(path="logs/wsbuilder.ndjson")`
- `append_ndjson(path, record)`
- `install_logs(app, path="logs/wsbuilder.ndjson")`

## Ejemplo

```python
from wsbuilder.logs import NDJSONLog

logs = NDJSONLog("logs/app.ndjson")
logs.event("request", method="GET", path="/health")
logs.append({"event": "response", "status": 200})
```

## Integracion con `App`

```python
from wsbuilder import App

app = App()
logs = app.enable_logs(path="logs/app.ndjson")
logs.event("boot", status="ok")
```

## Reglas

- Usa `pathlib.Path` y `open(..., "a")`.
- Crea el directorio padre si hace falta.
- Cada linea queda en formato NDJSON, una por evento.
- No sustituye al modulo `logging` de la stdlib.

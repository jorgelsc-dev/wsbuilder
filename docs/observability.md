# Observabilidad

WSBuilder expone metricas y logs sin obligarte a meter un stack pesado.

## Metricas

```python
from wsbuilder import App, install_metrics

app = App()
metrics = install_metrics(app, app_name="my-app")
```

Eso crea:

- `GET /api/metrics`
- `GET /api/metrics/stream`

## `AppMetrics`

`AppMetrics` registra:

- conexiones TCP
- requests HTTP
- responses HTTP
- upgrades WebSocket
- mensajes WebSocket
- errores

### Snapshot

```python
snap = app.metrics.snapshot()
```

El snapshot incluye totales, tasas y el estado de cada subsistema cuando la app agrega snapshots extra.

### Streaming

```python
for chunk in app.metrics.stream_chunks(interval_seconds=1.0):
    print(chunk.decode("utf-8"))
```

## Metricas de la app

`App.enable_metrics()` añade informacion adicional:

- resumen de threads por ruta
- snapshot de cache general
- snapshot de cache HTTP
- snapshot de security
- snapshot de proxyi
- snapshot de tasks

## Logs

```python
from wsbuilder import App

app = App()
logger = app.enable_logs(path="logs/wsbuilder.ndjson")
logger.event("server_start", host="0.0.0.0", port=8765)
```

`NDJSONLog` escribe un evento por linea y funciona bien para ingest para herramientas externas.

### Mensajes del framework

Los avisos y errores del propio framework (handlers que fallan, errores TLS,
arranque y apagado del servidor) pasan por `wsbuilder.logs`:

- sin `enable_logs()`, salen por **stderr** como `[evento] mensaje`; los errores
  incluyen el traceback completo;
- con `enable_logs()`, cada mensaje se guarda en el NDJSON con `ts`, `level`,
  `event`, `message` y, si hubo excepcion, `error.type`, `error.message` y
  `error.traceback`. Los de nivel `warning` o superior tambien se muestran por
  stderr.

```python
logger = app.enable_logs(path="logs/wsbuilder.ndjson", console_level="error")
logger.info("deploy", "version publicada", version="1.0.3")
logger.exception("pagos", "fallo el cobro", exc)

from wsbuilder.logs import console
console.level = "warning"  # sin enable_logs: oculta los mensajes info
```

Niveles: `debug`, `info`, `warning`, `error`. `console_level=None` deja los
mensajes solo en el archivo.

## Docs nativas

`App.enable_docs()` publica una vista HTML y un endpoint JSON con `App.describe()`.
Es util para inspeccion local, automatizacion o integracion con dashboards de desarrollo.

## Ruta de trabajo sugerida

1. Activa `install_metrics()`.
2. Activa `enable_logs()` si quieres traza historica.
3. Usa `enable_docs()` para exponer la configuracion real de la app.
4. Consulta `/api/metrics` cuando algo no cuadre en produccion.

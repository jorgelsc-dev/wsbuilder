# Metricas

## Collector

`AppMetrics` acumula:

- conexiones TCP activas y totales.
- requests HTTP activas y totales.
- respuestas HTTP.
- metodos, paths y status.
- conexiones WebSocket y mensajes.
- trafico in/out.
- errores recientes.

## Integracion

```python
from wsbuilder import App

app = App()
app.enable_metrics()
```

Esto monta:

- `GET /api/metrics`
- `GET /api/metrics/stream`

## Stream

El endpoint de stream emite NDJSON.

Parametros:

- `interval`
- `limit`
- `follow`

## Flujo tipico

1. Tomas una instantanea con `GET /api/metrics`.
2. Observas tendencias con `GET /api/metrics/stream`.
3. Conectas el stream a dashboards o terminales.

Ejemplo:

```bash
curl -N "http://127.0.0.1:8765/api/metrics/stream?interval=1&follow=1"
```

## Casos de uso

- Ver carga y salud del servidor.
- Auditar rutas mas utilizadas.
- Detectar errores recientes.
- Alimentar dashboards simples sin dependencia adicional.

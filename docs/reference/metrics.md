# Metricas

<div class="diagram">
<div class="diagram-title">Metricas</div>
<div class="diagram-track">
<div class="diagram-node">Eventos</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">AppMetrics</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Snapshot</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Stream</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Dashboard</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">Convierte eventos internos en una vista operativa clara para observacion y diagnostico.</div>
</div>

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

## Rol del modulo

- Convertir eventos internos en una vista operativa clara.
- Dar un plano comun para HTTP, WebSocket, trafico y errores.
- Exponer un stream facil de consumir sin acoplar el proyecto a otra herramienta.

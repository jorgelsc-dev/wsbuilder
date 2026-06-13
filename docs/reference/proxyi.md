# Proxy / VHost

<div class="diagram">
<div class="diagram-title">Proxy / VHost</div>
<div class="diagram-track">
<div class="diagram-node">Request</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">ProxyI</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Rule</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Target</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Metrics / Dashboard</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">Pensado para declarar vhosts, rutas tipo nginx y balanceo adaptativo sin salir del paquete.</div>
</div>

`ProxyI` es un motor de reverse proxy y enrutamiento por reglas. Permite declarar:

- vhosts por host exacto o con glob;
- locations por path, prefijo o regex;
- condiciones por headers exactos, contains o regex;
- upstreams por IP o dominio;
- balanceo por round robin, pesos, latencia, carga y score compuesto;
- una area de metricas con snapshot, stream y dashboard HTML.

## Clases

- `ProxyI`
- `ProxyRule`
- `ProxyTarget`
- `ProxyMetricsBucket`
- `RunningStats`
- `install_proxyi(app, ...)`

## Ejemplo

```python
from wsbuilder import ProxyI

proxy = ProxyI(name="edge")
proxy.vhost("api.test.local").location("/api").header("x-env", equals="prod").upstream(
    "http://127.0.0.1:8080",
    name="api-backend",
)
```

## Balanceo

Modos soportados:

- `round_robin`
- `weighted_round_robin`
- `random`
- `least_connections`
- `least_response_time`
- `least_requests`
- `least_bytes_in`
- `least_bytes_out`
- `ip_hash`
- `consistent_hash`
- `first_available`
- `power_of_two_choices`
- `best`

`best` usa las metricas observadas para calcular un score compuesto. El score toma en cuenta la latencia media, la carga activa, el error rate y el tamano promedio de peticiones y respuestas.

## Metricas

Cada target y cada regla guardan:

- `requests_total` y `responses_total`
- `errors_total`
- `active_requests`
- `bytes_in_total` y `bytes_out_total`
- media
- desviacion estandar
- error estandar
- incertidumbre al 95%
- limite inferior y superior al 95%

Estas estadisticas estan disponibles en:

- `proxy.snapshot()`
- `proxy.metrics_snapshot()`
- `GET /__proxyi__/metrics`
- `GET /__proxyi__/metrics/stream`
- `GET /__proxyi__`

## Integracion con App

Si quieres exponer el dashboard y las metricas dentro de una `App`:

```python
from wsbuilder import App, ProxyI, install_proxyi

app = App()
proxy = ProxyI(name="gateway")
install_proxyi(app, proxy=proxy)
app.proxyi = proxy
```

La `App` tambien incluye el bloque `proxyi` dentro de `app.enable_metrics()` y `app.enable_docs()` cuando la instancia esta adjunta a la aplicacion.

## Casos de uso

- Gateway interno con varias APIs detras.
- Balanceo basado en latencia y carga real.
- Ruteo por host y path en estilo nginx.
- Observabilidad nativa del proxy sin dependencias externas.

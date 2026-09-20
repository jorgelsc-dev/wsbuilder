# Proxy

`ProxyI` es la capa de reverse proxy y virtual host routing de WSBuilder.

## Cuadro general

- enruta por host, path, metodos y headers
- balancea entre varios targets
- preserva o reescribe host segun la regla
- expone snapshot, dashboard y metricas

## Crear reglas

```python
from wsbuilder import ProxyI

proxy = ProxyI(name="edge")
proxy.vhost("api.test.local", name="api-vhost").location("/api").upstream("http://127.0.0.1:9000").build()
```

## DSL de reglas

`ProxyRouteBuilder` permite componer filtros:

- `host()`, `host_contains()`, `host_regex()`
- `path()`, `path_prefix()`, `path_contains()`, `path_regex()`
- `header()`, `header_equals()`, `header_contains()`
- `balance()`, `priority()`, `default()`
- `strip_prefix()` y `preserve_host()`

## Targets

`ProxyTarget` representa un upstream:

- `url`
- `name`
- `weight`
- `priority`
- `timeout_seconds`
- `verify_tls`
- `extra_headers`

## Balanceo

Estrategias disponibles:

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

## Ejemplo con headers

```python
proxy.route(name="lb", path_prefix="/api", balance="least_response_time") \
    .header("x-env", equals="prod") \
    .upstream("http://127.0.0.1:9001", name="backend-1") \
    .upstream("http://127.0.0.1:9002", name="backend-2") \
    .build()
```

## Dashboard

`ProxyI` trae una zona interna de dashboard y metricas:

- `dashboard_path`
- `metrics_path`
- `metrics_stream_path`

Tambien puedes montar la instancia sobre un `App` con `proxy.install(app)`.

## Snapshot

`proxy.snapshot()` devuelve:

- reglas
- targets
- contadores globales
- estado por target y por regla

Eso hace mas facil diagnosticar por que una regla selecciona un backend concreto.

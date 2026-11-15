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

## Seguridad y limites de respuesta

`ProxyI` acepta estas opciones al construirse:

| Opcion | Defecto | Efecto |
| --- | --- | --- |
| `max_request_body_bytes` | `8 MiB` | Un cuerpo de peticion mayor recibe `413`. |
| `max_response_bytes` | `16 MiB` | Un cuerpo de upstream con buffer mayor recibe `502`. |
| `response_timeout_seconds` | `30.0` | Plazo total para leer un cuerpo con buffer; si se agota, `504`. |
| `expose_upstream_header` | `False` | Con `True` anade `X-ProxyI-Upstream` con la URL interna del upstream. |

- Un path con segmentos `.` o `..`, literales o codificados (`%2e`, `%2f`,
  `%5c`, tambien `..;param`), se rechaza con `400` antes de evaluar reglas, para
  que no escape de un prefijo ni del `base_path` del target.
- Las respuestas con `Content-Length` se leen con buffer bajo esos dos limites.
  Si el upstream cierra antes de entregar la longitud declarada, se responde
  `502`.
  Las respuestas `chunked`, delimitadas por el cierre de la conexion o
  `text/event-stream` se retransmiten como `Response.stream` por bloques, sin
  cargarlas en memoria. En ese modo no hay plazo total ni limite de bytes: cada
  lectura respeta `timeout_seconds` del target, de modo que un upstream que sigue
  enviando datos (por ejemplo SSE) mantiene ocupado el worker mientras dure.
- Se conservan todos los `Set-Cookie` del upstream: las cabeceras repetidas
  llegan a `Response.headers` como lista.
- `X-ProxyI-Upstream` ya no se envia por defecto; `X-ProxyI-Rule` y
  `X-ProxyI-Target` se mantienen.

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

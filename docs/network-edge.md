# Red y Edge

## DNS local con `LocalDNSServer`

`LocalDNSServer` es un servidor DNS UDP autoritativo con fallback opcional a
upstream.

```python
from wsbuilder import LocalDNSServer

dns = LocalDNSServer(
    host="127.0.0.1",
    port=5533,
    ttl=60,
    records={
        "demo.local": {"A": "127.0.0.1"},
        "api.demo.local": {"CNAME": "demo.local"},
    },
)
dns.start()
```

Capacidades visibles en la suite:

- registros `A`, `AAAA`, `TXT`, `MX`, `SRV`, `CNAME`.
- records wildcard.
- records con `TYPE####` y `rdata` crudo.
- fallback a upstreams remotos cuando se habilita.

Metodos publicos:

- `add_record`
- `add_raw_record`
- `remove_record`
- `clear_records`
- `serve_forever`
- `start`
- `close`

## Proxy HTTP con `ProxyI`

`ProxyI` es una capa de reverse proxy y balanceo sin dependencias externas.

```python
from wsbuilder import ProxyI

proxy = ProxyI(name="edge")
proxy.vhost("api.test.local", name="api-vhost").location("/api").upstream(
    "http://127.0.0.1:9000",
    name="backend-1",
).build()
```

## Construccion de reglas

`ProxyRouteBuilder` soporta filtros por:

- host exacto, multiples hosts, host parcial o regex.
- path exacto, prefijo, contains o regex.
- headers con `equals`, `contains` o regex.
- metodos y prioridad.

Tambien permite:

- `balance(value)`
- `strip_prefix(value=True)`
- `preserve_host(value=True)`
- `hash_key(value)`
- `default(value=True)`
- `upstream(target, **kwargs)`

## Estrategias de balanceo

La API publica expone:

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

## Instalacion sobre `App`

```python
proxy.install(
    app,
    metrics_path="/api/proxy/metrics",
    stream_path="/api/proxy/metrics/stream",
    dashboard_path="/proxy",
)
```

Eso publica:

- snapshot JSON de metricas del proxy
- stream de metricas
- dashboard HTML

Para despachar trafico real, una ruta HTTP de tu aplicacion debe invocar
`app.proxyi.dispatch(request)`.

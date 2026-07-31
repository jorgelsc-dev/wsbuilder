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

- registros `A`, `AAAA`, `NS`, `CNAME`, `PTR`, `MX`, `TXT`, `SRV`, `SOA`,
  `CAA`, `NAPTR`, `URI`, `DS`, `DNSKEY`, `TLSA` y `SSHFP`.
- records wildcard.
- records con `TYPE####` y `rdata` crudo.
- fallback a upstreams remotos cuando se habilita.

El parser rechaza labels con tipos reservados, labels invalidos y nombres que
superan el limite DNS. Un paquete malformado se descarta sin detener el loop del
servidor. En respuestas wildcard, el owner sintetizado conserva el nombre
consultado por el cliente.

Cuando se usa fallback, una respuesta UDP solo se acepta si procede del
upstream configurado, es una respuesta DNS y coincide en transaction ID y
pregunta con la consulta original.

Metodos publicos:

- `add_record`
- `add_raw_record`
- `remove_record`
- `clear_records`
- `serve_forever`
- `start`
- `close`

Ejemplo de registro plano:

```python
dns = LocalDNSServer(
    records=[
        {"name": "app.local", "type": "A", "value": "127.0.0.1", "ttl": 60},
        {"name": "raw.local", "type": "TYPE65280", "hex": "01020304"},
    ],
)
```

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

Los targets HTTPS verifican certificados TLS por defecto. La opcion
`verify_tls=False` queda disponible para entornos locales controlados, pero no
debe usarse frente a upstreams no confiables.

`ProxyTarget` acepta opciones como `name`, `weight`, `enabled`,
`preserve_host`, `verify_tls`, `strip_prefix`, `extra_headers`, `connect_timeout`
y `read_timeout`.

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
- `to(target, **kwargs)` como alias de `upstream`

Los prefijos respetan limites de segmento: `/api` coincide con `/api` y
`/api/users`, no con `/apix`. `strip_prefix` aplica la misma regla. Si
`preserve_host=False` se configura en una regla, el upstream recibe su propia
autoridad como `Host`.

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

`ProxyI(default_balance=...)` establece el balance de las reglas que no lo
sobrescriban. Los targets con `enabled=False` no participan en seleccion ni se
reactivan implicitamente cuando todos estan deshabilitados.

`normalize_balance_mode(value)` acepta constantes y nombres de estrategia con
guiones o underscores.

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

Los snapshots no consumen turnos de round-robin. Los valores de headers
sensibles en `extra_headers` y filtros de reglas, como `Authorization`, cookies,
tokens o API keys, se muestran como `[REDACTED]`; el valor real solo se conserva
para el forwarding.

ProxyI elimina headers hop-by-hop, incluidos los nombrados por `Connection`, y
reconstruye `X-Forwarded-*` desde el peer, host y TLS reales. No reutiliza
valores `Forwarded` aportados por un cliente directo. El limite
`max_request_body_bytes` tambien se aplica al usar `dispatch()` directamente y
responde `413` cuando el cuerpo lo supera.

Para despachar trafico real, una ruta HTTP de tu aplicacion debe invocar
`app.proxyi.dispatch(request)`.

```python
@app.api("/api/proxy/upstream")
def proxy_upstream(request):
    return app.proxyi.dispatch(request)
```

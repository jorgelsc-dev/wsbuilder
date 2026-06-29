# Cache y Seguridad

## Cache clave-valor con `SQLiteMemoryCache`

`SQLiteMemoryCache` es una cache embebida, sin dependencias externas, con TTL,
tags y namespaces.

```python
from wsbuilder import SQLiteMemoryCache, install_cache

cache = install_cache(app, SQLiteMemoryCache(default_ttl=60, cleanup_interval_seconds=0))
cache.set("user:1", {"name": "Alice"}, tags=["users", "team:docs"])
value = cache.get("user:1")
```

Funciones utiles:

- `set`, `get`, `delete`, `pop`
- `add`, `replace`
- `get_many`, `set_many`, `mget`, `mset`
- `expire`, `touch`, `ttl`
- `incr`, `decr`
- `tag`, `untag`, `invalidate_tag`, `invalidate_tags`
- `keys`, `count`, `size_bytes`, `stats`, `metrics_snapshot`

## Cache HTTP de vistas con `ViewResponseCache`

Esta capa cachea respuestas de rutas `view`.

```python
from wsbuilder import ViewResponseCache

http_cache = ViewResponseCache(default_ttl=20)
http_cache.add_global_rule(
    ttl_seconds=30,
    path_pattern="/pages/*",
    mimetype_pattern="text/html*",
    methods=("GET",),
    name="public-pages",
)
app.enable_caches(http_cache)
```

Tambien puedes declarar cache por ruta:

```python
@app.view("/pages/overview", cache={"ttl": 15, "vary_query": ["lang"]})
def overview(request):
    return f"lang={request.query.get('lang', 'es')}"
```

Notas practicas:

- solo actua sobre rutas `plain`.
- no cachea respuestas con `Set-Cookie`.
- por defecto cachea `status=200`.
- anade `X-WSBuilder-Cache: HIT` cuando sirve desde cache.

## `SecurityPolicy`

`SecurityPolicy` combina ACL, listas blancas/negras, rate limiting, deteccion
de comportamiento sospechoso y bloqueos temporales.

```python
from wsbuilder import SecurityPolicy

policy = SecurityPolicy(
    rate_limit_requests=120,
    rate_limit_window_seconds=60.0,
    block_duration_seconds=300.0,
)
policy.deny(name="deny-admin-post", methods=("POST",), path="/api/admin")
app.enable_security(policy=policy)
```

Metodos frecuentes:

- `add_whitelist`, `add_blacklist`
- `allow`, `deny`, `add_acl_rule`
- `block_ip`, `unblock_ip`
- `evaluate(request)`
- `observe_response(request, status_code)`
- `snapshot()`

## `SecurityDecision`

Es el resultado de `evaluate(request)` y ofrece:

- `allowed`
- `status`
- `message`
- `reason`
- `response_headers()`
- `to_response()`

`App.dispatch()` usa automaticamente ese resultado para producir texto o JSON
segun el tipo de ruta.

## Headers y cookies

Utilidades incluidas:

- `normalize_header_name`
- `get_header`
- `has_header`
- `set_header`
- `parse_cookie_header`
- `get_cookie`
- `build_set_cookie`

Son helpers de bajo nivel utiles para middlewares, autenticacion ligera y tests.

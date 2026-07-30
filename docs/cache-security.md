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

Una entrada expirada se considera ausente para `add` y `replace`, incluso si
`cleanup_interval_seconds=0`.

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

Las solicitudes con `Authorization` o `Cookie` no usan la cache compartida por
defecto. Una ruta puede habilitarlas de forma explicita:

```python
@app.view("/account", cache={"ttl": 15, "allow_private": True})
def account(request):
    return render_account(request)
```

Con `allow_private=True`, las cabeceras privadas presentes se incorporan
automaticamente a la clave. Si una respuesta declara `Vary`, todos sus nombres
deben aparecer en `vary_headers`; de lo contrario la respuesta no se almacena.

Notas practicas:

- solo actua sobre rutas `plain`.
- no cachea respuestas con `Set-Cookie`.
- respeta sin distinguir mayusculas las directivas `Cache-Control` privadas o
  que requieren revalidacion.
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

Si la aplicacion esta detras de un proxy, no confies en
`X-Forwarded-For` sin delimitar sus redes:

```python
policy = SecurityPolicy(
    trust_x_forwarded_for=True,
    trusted_proxy_cidrs=("10.0.0.0/8", "192.168.0.0/16"),
)
```

La cabecera solo se usa cuando el peer directo pertenece a una de esas redes.
La IP cliente se resuelve desde la derecha de la cadena, descartando saltos
confiables. Sin `trusted_proxy_cidrs`, se conserva siempre la IP del peer.

Metodos frecuentes:

- `add_whitelist`, `add_blacklist`
- `allow`, `deny`, `add_acl_rule`
- `block_ip`, `unblock_ip`
- `evaluate(request)`
- `observe_response(request, status_code)`
- `snapshot()`

Las entradas de la whitelist pueden prevalecer sobre blacklist y bloqueos de
comportamiento mediante `whitelist_overrides_blacklist` y
`whitelist_bypass_behavior`, ambos activos por defecto.

Los filtros ACL con `path_prefix` respetan limites de segmento: `/api`
coincide con `/api` y `/api/users`, pero no con `/api-private`.

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
Los constructores rechazan nombres, valores y atributos con caracteres de
control para evitar inyeccion de cabeceras.

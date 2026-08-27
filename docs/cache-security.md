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

Tipos de valor soportados por defecto:

- `None`, `bool`, `int`, `float`, `str`, `bytes`.
- estructuras compatibles con JSON, como `dict` y `list`.

`allow_pickle=True` permite otros objetos, pero solo debe usarse con datos de
confianza porque `pickle` no es un formato seguro frente a entradas no confiables.

Una entrada expirada se considera ausente para `add` y `replace`, incluso si
`cleanup_interval_seconds=0`.

Codigos `ttl()`:

| Valor | Significado |
| --- | --- |
| numero positivo | segundos restantes |
| `-1` | la clave existe sin expiracion |
| `-2` | la clave no existe o ya expiro |

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

Configuracion por ruta:

| Clave | Uso |
| --- | --- |
| `ttl` | segundos de vida de la respuesta |
| `enabled` | `False` desactiva cache aunque exista regla global |
| `vary_query` | lista de parametros de query que forman parte de la clave |
| `vary_headers` | headers permitidos en `Vary` y parte de la clave |
| `allow_private` | permite cachear requests con `Authorization` o `Cookie` |
| `statuses` | codigos HTTP cacheables |

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

Parametros importantes del constructor:

| Parametro | Uso |
| --- | --- |
| `acl_default` | `allow` o `deny` cuando ninguna regla coincide |
| `rate_limit_requests` | cantidad maxima de requests por ventana |
| `rate_limit_window_seconds` | tamano de la ventana de rate limit |
| `violation_threshold` | bloquea al acumular denegaciones |
| `suspicious_status_codes` | codigos que cuentan como comportamiento sospechoso |
| `suspicious_threshold` | cantidad de respuestas sospechosas antes de bloquear |
| `block_duration_seconds` | duracion del bloqueo temporal |

Las entradas de la whitelist pueden prevalecer sobre blacklist y bloqueos de
comportamiento mediante `whitelist_overrides_blacklist` y
`whitelist_bypass_behavior`, ambos activos por defecto.

Los filtros ACL con `path_prefix` respetan limites de segmento: `/api`
coincide con `/api` y `/api/users`, pero no con `/api-private`.

Ejemplo con politica cerrada:

```python
policy = SecurityPolicy(acl_default="deny")
policy.allow(methods=("GET",), path="/api/health")
policy.allow(methods=("GET",), path_prefix="/api/public")
app.enable_security(policy)
```

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

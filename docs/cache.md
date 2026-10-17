# Cache

WSBuilder tiene dos niveles de cache:

1. `SQLiteMemoryCache` para valores generales con TTL, namespaces y tags.
2. `ViewResponseCache` para respuestas HTTP de rutas `view`.

## Cache general

```python
from wsbuilder import SQLiteMemoryCache

cache = SQLiteMemoryCache(default_ttl=30)
cache.set("user:1", {"name": "Alice"}, tags=["users"])
value = cache.get("user:1")
```

### Caracteristicas

- TTL por clave
- namespaces aislados
- tags para invalidacion
- contadores de hits, misses y evictions
- estadisticas y snapshot para metricas

### Operaciones comunes

```python
cache.add("k1", "v1")
cache.replace("k1", "v2")
cache.incr("counter")
cache.decr("counter")
cache.expire("k1", 10)
cache.touch("k1", ttl=60)
cache.tag("k1", ["home", "public"])
cache.invalidate_tag("home")
```

## Cache de respuestas

`ViewResponseCache` cachea respuestas de rutas `view` cuando la configuracion de la ruta o las reglas globales
lo permiten.

```python
from wsbuilder import App

app = App()
app.enable_caches()

@app.view("/home", cache={"ttl": 60})
def home(_request):
    return "contenido estable"
```

### Reglas globales

```python
caches = app.enable_caches()
caches.set_global_wildcard(30)
caches.set_global_mimetype("text/plain", 15)
```

Tambien puedes declarar reglas con `declare_global()` o `add_global_rule()` si quieres controlar rutas,
metodos y tipos MIME con mas precision.

## Invalidacion

- `invalidate_path(path)` borra las entradas de una ruta concreta.
- `clear()` limpia toda la cache.
- `clear_global_rules()` elimina reglas compartidas.

## Integracion con App

```python
from wsbuilder import App, SQLiteMemoryCache, install_cache, install_metrics

app = App()
install_cache(app, SQLiteMemoryCache(default_ttl=10))
app.enable_caches()
install_metrics(app, app_name="cache-demo")
```

Cuando la cache esta activa, `App.dispatch()` puede responder con `X-WSBuilder-Cache: HIT`.

## Lo que conviene recordar

- La cache de respuestas solo aplica a rutas `view`.
- `cache=False` en una ruta desactiva la herencia de reglas globales.
- `metrics_snapshot()` expone el estado de uso para observabilidad.

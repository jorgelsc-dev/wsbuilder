# Cache

## Cache en memoria

`SQLiteMemoryCache` es una cache thread-safe en memoria respaldada por SQLite.

Soporta:

- TTL por clave.
- namespaces.
- tags.
- expiracion y limpieza automatica.
- evicciones por limite de entradas o bytes.

Ejemplo:

```python
from wsbuilder import SQLiteMemoryCache

cache = SQLiteMemoryCache(default_ttl=60)
cache.set("greeting", "hola")
```

## Helpers de integracion

- `install_cache(app, ...)`
- `install_caches(app, ...)`

## Reglas de cache

- `GlobalCacheRule`
- `ViewResponseCache`

Usalas cuando quieras cachear respuestas de rutas `view()` de forma declarativa.


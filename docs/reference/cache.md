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

## Que resuelve

- TTL por clave.
- Namespaces para aislar dominios de cache.
- Tags para invalidacion selectiva.
- Limpieza automatica y evicciones por tamano.

## Helpers de integracion

- `install_cache(app, ...)`
- `install_caches(app, ...)`

## Reglas de cache

- `GlobalCacheRule`
- `ViewResponseCache`

Usalas cuando quieras cachear respuestas de rutas `view()` de forma declarativa.

## Casos de uso

- Respuestas HTML costosas.
- Fragmentos calculados que cambian poco.
- Reutilizacion de resultados de lectura en servicios SQLite.
- Contadores temporales o limitacion basica de frecuencia.

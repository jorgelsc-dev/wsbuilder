# Cache

<div class="diagram">
<div class="diagram-title">Cache</div>
<div class="diagram-track">
<div class="diagram-node">Request</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Lookup</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Hit o Miss</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Store</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">Un hit evita recomputar; un miss ejecuta el handler y guarda la respuesta para despues.</div>
</div>

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

## Rol del modulo

- Reduce costo de calculo o serializacion.
- Mantiene TTL, namespaces y tags para invalidacion controlada.
- Se integra con rutas `view()` y no compite con la persistencia de dominio.

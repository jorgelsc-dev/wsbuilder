# Ayuda

Si algo no funciona, revisa primero esta lista corta.

## Instalacion

```bash
python -m pip install -e .
```

Si `import wsbuilder` falla en un checkout local, casi siempre falta `PYTHONPATH=src` o una instalacion editable.

## Docs vacias o no actualizadas

1. Asegurate de que `mkdocs.yml` apunte a paginas que existen.
2. Revisa `docs/index.md` y la navegacion.
3. Ejecuta `mkdocs build --strict`.

## Ruta devuelve 404

- Verifica que la ruta registrada coincida con el path solicitado.
- Confirma que el metodo esta incluido en `methods=`.
- Asegurate de que la funcion decorada sigue devolviendo algo valido.

## Ruta devuelve 500

- Revisa la excepcion en el handler.
- Si es una `api`, WSBuilder devuelve JSON con `status=error`.
- Si es `view`, responde texto plano con `Internal Server Error`.

## WebSocket falla en el handshake

- La request necesita `Connection: Upgrade`.
- La request necesita `Upgrade: websocket`.
- La version soportada es `13`.
- El cliente debe enviar `Sec-WebSocket-Key`.

## Cache no hace hit

- La cache de respuestas solo aplica a rutas `view`.
- Usa `cache={"ttl": 60}` o una regla global.
- Si la ruta devuelve `Response.html` con tipo incompatible, la regla MIME puede dejarla fuera.

## ORM o SQLite bloquea

- Usa `WAL` en archivos persistentes.
- Cierra conexiones al terminar.
- Para cargas de solo lectura, usa replicas o `using("replica")`.

## Metricas no aparecen

- Confirma que llamaste `install_metrics(app, ...)` o `app.enable_metrics()`.
- Comprueba que consultas `/api/metrics` y no otro path.
- Si hay un error en un snapshot extra, la respuesta lo refleja en el payload.

## Tareas no avanzan

- `TaskManager` puede limitar concurrencia con `max_concurrent`.
- `TaskHandle.get()` espera a que termine.
- Si cancelas una tarea, el worker debe cooperar.

## Comandos de saneamiento

```bash
PYTHONPATH=src pytest -q
mkdocs build --strict
python -m wsbuilder --host 0.0.0.0 --port 8765
```

## Si sigues atascado

- Abre la [referencia](../reference/index.md) para encontrar la firma exacta.
- Revisa la [arquitectura](../architecture.md) para ver donde encaja cada modulo.
- Mira la [guia de inicio](../getting-started.md) para validar que tu base sigue sana.

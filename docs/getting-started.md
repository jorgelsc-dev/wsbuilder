# Empezar

Esta pagina resume la forma mas rapida de arrancar `wsbuilder` en local y entender su flujo minimo.

## Instalacion

```bash
python -m pip install -e .
```

## Ejecutar el ejemplo integrado

```bash
python -m wsbuilder --host 0.0.0.0 --port 8765
```

Tambien puedes usar el entrypoint equivalente:

```bash
wsbuilder --host 0.0.0.0 --port 8765
```

Eso levanta un servidor HTTP con:

- `GET /`
- `GET /api/health`
- `GET /api/metrics`
- `GET /api/metrics/stream`
- `GET /docs`
- `GET /docs.json`
- `GET /monitor`
- `WS /ws/`

## Primer app

```python
from wsbuilder import App, Response

app = App()

@app.view("/")
def home(_request):
    return Response.text("hola desde wsbuilder")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("127.0.0.1", 8765)
```

## Primeros modulos utiles

- `app.enable_metrics()` para exponer snapshot y stream de metricas.
- `app.enable_security(policy)` para aplicar ACL y rate limiting.
- `app.enable_caches(...)` para cachear respuestas de `view()`.
- `TaskManager` para lanzar trabajo en background con estados y control de capacidad.

## CORS

Para permitir cualquier origen:

```python
app = App(cors_allow_origin="*")
```

Para limitarlo a un dominio:

```python
app = App(cors_allow_origin="https://tu-dominio.com")
```

## Flujo de lectura recomendado

1. Ejecuta el ejemplo incluido y prueba `GET /api/health`.
2. Si quieres una ruta guiada de construccion, abre [Aplicacion - Facil](application/easy.md).
3. Lee [Arquitectura](architecture.md) para entender como se resuelve una request.
4. Abre [Ayuda](help/index.md) si quieres adaptar `wsbuilder` a Microservicios.
5. Abre [Referencia](reference/index.md) cuando necesites una clase, metodo o helper concreto.

## Recomendacion de desarrollo

Si trabajas sobre el repo, usa:

```bash
PYTHONPATH=src pytest -q
```

Eso deja el paquete resolviendo el codigo del directorio `src/` sin instalar nada adicional.

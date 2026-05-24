# Empezar

Esta pagina resume la forma mas rapida de usar `wsbuilder` en local y dejarlo listo para publicarlo.

## Instalacion

```bash
python -m pip install -e .
```

Si solo quieres levantar la documentacion local:

```bash
python -m pip install -r requirements-docs.txt
```

## Ejecutar el demo integrado

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
- `GET /monitor`
- `GET /thread-demo`
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

## CORS

Para permitir cualquier origen:

```python
app = App(cors_allow_origin="*")
```

Para limitarlo a un dominio:

```python
app = App(cors_allow_origin="https://tu-dominio.com")
```

## Metricas

```python
from wsbuilder import App

app = App()
app.enable_metrics()
```

Esto expone:

- `GET /api/metrics`
- `GET /api/metrics/stream`

## Recomendacion de desarrollo

Si trabajas sobre el repo, usa:

```bash
PYTHONPATH=src pytest -q
```

Eso te deja el paquete localmente resolviendo el codigo del directorio `src/`.


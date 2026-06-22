# Empezar

Esta guia te lleva de cero a un servidor funcionando en pocos minutos.

## Instalacion

```bash
python -m pip install -e .
```

Si prefieres usar la fuente del repositorio sin instalarla globalmente:

```bash
PYTHONPATH=src python -m wsbuilder --host 0.0.0.0 --port 8765
```

## Primer servidor

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")

@app.view("/")
def home(_request):
    return Response.html("<h1>WSBuilder</h1><p>Servidor listo.</p>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("0.0.0.0", 8765)
```

## Estructura minima

1. Crea un `App`.
2. Registra rutas con `@app.view`, `@app.api` o `@app.ws`.
3. Devuelve `Response.text`, `Response.html`, `Response.json` o un `dict`/`list`.
4. Llama a `app.run(host, port)` para arrancar el servidor.

## Docs en vivo

`App` puede exponer una vista automatica de la app con datos de rutas, metricas y WebSocket.

```python
from wsbuilder import App

app = App()
app.enable_docs(path="/docs", json_path="/docs.json", title="Docs")
```

Eso crea dos endpoints:

- `/docs` devuelve HTML.
- `/docs.json` devuelve el snapshot JSON de la aplicacion.

## Opcionales recomendados

Si quieres una base mas completa desde el inicio:

```python
from wsbuilder import App, SQLiteMemoryCache, SecurityPolicy, install_cache, install_metrics, install_security

app = App(cors_allow_origin="*")
install_metrics(app, app_name="my-app")
install_cache(app, SQLiteMemoryCache(default_ttl=30))
install_security(app, SecurityPolicy(rate_limit_requests=240))
app.enable_docs()
```

## Comandos utiles

```bash
PYTHONPATH=src pytest -q
mkdocs build --strict
python -m wsbuilder --host 0.0.0.0 --port 8765
```

## Siguiente paso

- Lee la [arquitectura](architecture.md) para entender el flujo interno.
- Abre la [referencia](reference/index.md) cuando necesites firmas exactas.
- Revisa [HTTP](http.md) y [WebSocket](websocket.md) para los dos caminos de entrada principales.

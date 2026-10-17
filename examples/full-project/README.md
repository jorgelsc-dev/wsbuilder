# full-project

Proyecto separado de ejemplo para `wsbuilder`. Integra:

- HTTP, JSON y HTML.
- WebSocket.
- documentacion runtime.
- metricas y logs NDJSON.
- tareas en background.
- ORM SQLite con persistencia local.
- cache de objetos y cache de vistas.
- `SecurityPolicy`.
- `ProxyI` con upstream aparte.
- `LocalDNSServer` opcional.
- `Predictor` y `NeuralNetwork`.

## Preparacion

Desde este repositorio:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ../..
```

## Correr el upstream

En una terminal:

```bash
cd examples/full-project
python upstream.py --port 8780
```

## Correr la app principal

En otra terminal:

```bash
cd examples/full-project
python app.py --port 8765 --upstream-port 8780
```

## Superficies utiles

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/docs`
- `http://127.0.0.1:8765/docs.json`
- `http://127.0.0.1:8765/api/metrics`
- `http://127.0.0.1:8765/api/notes`
- `http://127.0.0.1:8765/api/cache/demo`
- `http://127.0.0.1:8765/api/ml/predict?x=5`
- `http://127.0.0.1:8765/api/proxy/upstream`
- `http://127.0.0.1:8765/proxy`
- `ws://127.0.0.1:8765/ws/echo`

## DNS opcional

El ejemplo no inicia DNS por defecto. Para activarlo:

```bash
WSB_FULL_DEMO_ENABLE_DNS=1 python app.py
```

Por defecto usa `127.0.0.1:5533` para evitar privilegios elevados.

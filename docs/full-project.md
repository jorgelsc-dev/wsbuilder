# Proyecto integral

El repositorio ahora incluye un ejemplo separado en:

```text
examples/full-project/
```

## Que cubre

El proyecto demuestra en una sola base:

- `App`, rutas `view`, `api` y `ws`.
- documentacion runtime con `enable_docs()`.
- metricas, logs NDJSON y tareas en background.
- ORM SQLite con modelos persistentes.
- cache clave-valor y cache HTTP de vistas.
- `SecurityPolicy`.
- `ProxyI` con upstream separado.
- `LocalDNSServer` opcional.
- `Predictor` y `NeuralNetwork` para una capa simple de IA.

## Archivos principales

```text
examples/full-project/
├── README.md
├── pyproject.toml
├── app.py
└── upstream.py
```

## Como correrlo

1. Instala `wsbuilder` desde este repo o desde la wheel local.
2. Levanta el upstream:

   ```bash
   cd examples/full-project
   python upstream.py --port 8780
   ```

3. En otra terminal, levanta la app principal:

   ```bash
   cd examples/full-project
   python app.py --port 8765 --upstream-port 8780
   ```

4. Abre:

   - `/`
   - `/docs`
   - `/api/metrics`
   - `/proxy`
   - `/api/notes`
   - `/api/cache/demo`
   - `/api/ml/predict?x=5`
   - `/api/tasks/train`
   - `/api/tasks/status`
   - `/api/proxy/upstream`
   - `/api/dns/status`
   - `/ws/echo`

## Que demuestra cada endpoint

| Endpoint | Demuestra |
| --- | --- |
| `/` | vista HTML simple |
| `/docs` y `/docs.json` | documentacion runtime |
| `/pages/overview` | vista con worker pool y cache HTTP |
| `/api/health` | API JSON y snapshot de estado |
| `/api/notes` | CRUD SQLite con `Model` y `QuerySet` |
| `/api/cache/demo` | cache KV, namespaces, tags y stats |
| `/api/ml/predict` | `Predictor` |
| `/api/ml/dataset` | datos de ejemplo para IA |
| `/api/tasks/train` | entrenamiento en background con `TaskManager` |
| `/api/tasks/status` | snapshot de una tarea |
| `/api/proxy/upstream` | forwarding con `ProxyI` |
| `/proxy` | dashboard del proxy |
| `/api/dns/status` | estado del DNS opcional |
| `/ws/echo` | WebSocket de eco |

## DNS opcional

El ejemplo no inicia el servidor DNS por defecto para no ocupar puertos ni
introducir ruido en demos simples. Para activarlo:

```bash
WSB_FULL_DEMO_ENABLE_DNS=1 python app.py
```

El ejemplo usa `127.0.0.1:5533` como valor por defecto para evitar privilegios
de root.

Tambien puedes activarlo por argumento:

```bash
python app.py --enable-dns --dns-port 5533
```

## Archivos generados

El ejemplo crea:

- `examples/full-project/data/demo.sqlite3`
- `examples/full-project/logs/app.ndjson`

Ambos son datos locales de ejecucion y no forman parte del paquete publicado.

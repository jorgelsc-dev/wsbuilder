# wsbuilder

`wsbuilder` es una libreria Python `3.11+` para construir servicios HTTP,
WebSocket y utilidades de infraestructura en un paquete pequeno, modular y sin
dependencias de runtime.

Sirve para crear:

- APIs JSON y vistas HTML.
- canales WebSocket.
- servicios con SQLite embebido y ORM ligero.
- cache clave-valor y cache HTTP de vistas.
- rate limiting, ACL, listas de IP y bloqueo temporal.
- metricas JSON, stream NDJSON, logs y tareas en background.
- DNS local, reverse proxy y balanceo.
- prediccion simple y redes neuronales basicas desde Python puro.

## Ruta de documentacion

La documentacion esta organizada por nivel:

- [Principiantes](docs/beginners.md): instala, crea la primera app y entiende
  rutas `view` y `api`.
- [Intermedios](docs/intermediate.md): agrega SQLite, cache, seguridad, metricas,
  logs y tareas.
- [Avanzados](docs/advanced.md): configura limites HTTP, TLS, worker pools, DNS,
  ProxyI, replicas SQLite y operacion.

Tambien puedes leer:

- [Que puedes hacer con wsbuilder](docs/capabilities.md)
- [Instalacion](docs/install.md)
- [Arquitectura](docs/architecture.md)
- [API publica](docs/api-map.md)
- [Proyecto integral](docs/full-project.md)

## Instalacion rapida

```bash
python -m pip install --upgrade pip
python -m pip install wsbuilder
```

En Debian, Kali, Ubuntu y otros entornos que aplican PEP 668, `pip` puede
rechazar la instalacion sobre el Python del sistema con
`externally-managed-environment`. Usa un entorno virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install wsbuilder
```

Verifica:

```bash
python -c "import wsbuilder; print(wsbuilder.__version__)"
wsbuilder --help
```

## Inicio rapido

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")
app.enable_metrics()
app.enable_docs(path="/docs", json_path="/docs.json")

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("127.0.0.1", 8765)
```

Ejecuta:

```bash
python app.py
```

Abre:

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/api/health`
- `http://127.0.0.1:8765/docs`
- `http://127.0.0.1:8765/api/metrics`

## Demo incluida

```bash
wsbuilder --host 127.0.0.1 --port 8765
```

La demo incluye:

- `/`
- `/monitor`
- `/thread-demo`
- `/api/health`
- `/api/metrics`
- `/api/metrics/stream`
- `/docs`
- `/docs.json`
- `/ws/`

## Mapa de modulos

| Area | Modulos | APIs principales |
| --- | --- | --- |
| HTTP | `app.py`, `http.py`, `server.py` | `App`, `Request`, `Response`, `HTTPServer` |
| WebSocket | `ws.py` | `WebSocket`, `parse_close_payload`, handshake |
| Datos | `orm.py`, `db_replicas.py` | `Database`, `Model`, `QuerySet`, `OptimizedDatabase` |
| Cache | `cache.py`, `caches.py` | `SQLiteMemoryCache`, `ViewResponseCache` |
| Seguridad | `security.py` | `SecurityPolicy`, `ACLRule`, `SecurityDecision` |
| Observabilidad | `metrics.py`, `logs.py`, `tasks.py` | `AppMetrics`, `NDJSONLog`, `TaskManager` |
| Red y edge | `dns.py`, `proxyi.py` | `LocalDNSServer`, `ProxyI` |
| IA y prediccion | `ia.py`, `predicts.py` | `DataSet`, `NeuralNetwork`, `Predictor` |

## Ejemplo con datos

```python
from datetime import UTC, datetime
from wsbuilder import App, Database, DateTimeField, IntegerField, Model, Response, TextField

class Note(Model):
    __tablename__ = "notes"

    id = IntegerField(primary_key=True, auto_increment=True)
    title = TextField(unique=True, null=False, index=True)
    created_at = DateTimeField(default=lambda: datetime.now(UTC), null=False)

app = App(cors_allow_origin="*")
app.db = Database("data/app.sqlite3", enable_wal=True)
Note.create_table(app.db)

@app.api("/api/notes", methods=("GET", "POST"))
def notes(request):
    if request.method == "GET":
        rows = [note.to_dict() for note in Note.objects(app.db).order_by("-id").all()]
        return {"items": rows}

    payload = request.json() or {}
    title = str(payload.get("title", "")).strip()
    if not title:
        return Response.json({"message": "title is required"}, status=400)
    note = Note.create(app.db, title=title)
    return Response.json(note.to_dict(), status=201)
```

## Instalacion local y offline

Desde este repositorio:

```bash
python -m pip install .
```

Instalacion editable para desarrollo:

```bash
python -m pip install -e .
```

Compilar wheel sin red, si ya tienes `build`, `setuptools>=77` y `wheel`
disponibles:

```bash
python -m build --no-isolation
python -m pip install --no-index dist/wsbuilder-*.whl
```

La guia completa esta en [docs/install.md](docs/install.md).

## Desarrollo

Comandos utiles del repositorio:

```bash
python -m pip install -e .
PYTHONPATH=src pytest -q
python -m build
python -m twine check dist/*
mkdocs build --strict
```

## Estado del proyecto

El paquete esta marcado como `Development Status :: 3 - Alpha`. Es adecuado para
servicios internos, prototipos, demos y laboratorios controlados. Antes de usarlo
fuera de localhost, revisa limites de transporte, TLS, seguridad, logs,
observabilidad y apagado de recursos.

## Contribucion y soporte

- Crea ramas desde `main` usando prefijos como `feat/`, `fix/` o `docs/`.
- Mantiene cada PR enfocado en un solo cambio logico.
- Ejecuta las validaciones relevantes antes de abrir PR.
- Reporta vulnerabilidades de forma privada.

### Soporte opcional

Si `wsbuilder` te resulta util y quieres apoyar el mantenimiento del proyecto,
puedes donar en BTC:

`bc1q3lhxpr9yantvefmvhpd2h4lu0ykf3t45zvuve2`

Red: `BTC mainnet` (Native SegWit).

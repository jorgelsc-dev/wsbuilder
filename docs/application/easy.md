# Aplicacion facil

Esta guia te lleva de cero a una app util en una sola pasada. La idea es que termines con una base que puedas copiar para un servicio pequeno, una demo o un prototipo serio.

## Objetivo

Vas a construir una app con:

- una ruta HTML;
- una ruta JSON;
- metricas basicas;
- un cache simple;
- una tarea en background opcional;
- una base SQLite para persistencia local.

## Paso 1: instala y ejecuta

```bash
python -m pip install -e .
python -m wsbuilder --host 127.0.0.1 --port 8765
```

Eso levanta el demo integrado del proyecto y te deja probar el flujo completo antes de escribir tu propia app.

## Paso 2: crea la app minima

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")

@app.view("/")
def home(_request):
    return Response.html("<h1>Hola desde wsbuilder</h1><p>Tu app ya responde.</p>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("127.0.0.1", 8765)
```

### Que hace esta version

- `App` define el enrutador y coordina la request.
- `view()` devuelve HTML o texto.
- `api()` serializa automaticamente `dict` y `list` como JSON.
- `Response.html()` y `Response.text()` te dejan controlar el formato cuando lo necesitas.

## Paso 3: agrega una pagina inicial util

Conviene que la ruta `/` explique que hace la app y donde estan las rutas utiles.

```python
@app.view("/")
def home(_request):
    return Response.html(
        """
        <h1>Mi servicio</h1>
        <ul>
          <li><a href="/api/health">/api/health</a></li>
          <li><a href="/api/users">/api/users</a></li>
        </ul>
        """
    )
```

## Paso 4: crea una API JSON

```python
@app.api("/api/users")
def users(_request):
    return [
        {"id": 1, "name": "Ada"},
        {"id": 2, "name": "Grace"},
    ]
```

Si la funcion devuelve un `dict` o un `list`, `wsbuilder` lo convierte en JSON sin que tengas que serializarlo a mano.

## Paso 5: agrega persistencia simple

```python
from wsbuilder import Database, IntegerField, Model, TextField

db = Database("app.db")

class User(Model):
    id = IntegerField(primary_key=True, auto_increment=True)
    username = TextField(unique=True, index=True, null=False)
    email = TextField(null=False)

User.create_table(db)
```

### Cuando usar esto

- Si necesitas guardar datos locales sin meter un stack externo.
- Si quieres una API pequena con SQLite y modelos declarativos.
- Si prefieres SQL controlado pero con menos ruido repetitivo.

## Paso 6: activa metricas

```python
from wsbuilder import install_metrics

install_metrics(app, app_name="my-service")
```

Eso expone:

- `GET /api/metrics`
- `GET /api/metrics/stream`

La version stream sirve para dashboards simples o para ver cambios en vivo.

## Paso 7: protege la app

```python
from wsbuilder import SecurityPolicy, install_security

policy = SecurityPolicy(rate_limit_requests=120, rate_limit_window_seconds=60)
install_security(app, policy)
```

Con esto ya tienes un primer limite contra abuso y una base para ACL, listas blancas o negras y bloqueo temporal.

## Paso 8: cachea respuestas repetidas

```python
from wsbuilder import SQLiteMemoryCache, install_cache

cache = SQLiteMemoryCache(default_ttl=30)
install_cache(app, cache)
```

Esto es util para rutas `view()` que devuelven HTML repetible o contenido calculado.

## Paso 9: agrega trabajo en background

```python
from wsbuilder import TaskManager

tasks = TaskManager(app, max_concurrent=4)

def rebuild_indexes():
    return "ok"

handle = tasks.spawn(rebuild_indexes, name="rebuild-indexes")
```

Uso recomendado:

- tareas cortas y locales;
- trabajos que no deben bloquear la request;
- procesos que quieras observar o cancelar.

## Paso 10: agrega WebSocket si hace falta

```python
@app.ws("/ws/")
def socket_handler(ws, _request):
    while True:
        frame = ws.recv_frame()
        if frame.opcode == 0x8:
            ws.close(1000, "bye")
            break
        if frame.opcode == 0x1:
            text = frame.payload.decode("utf-8", errors="ignore")
            ws.send_text(f"echo: {text}")
```

No uses WebSocket si una API HTTP normal ya resuelve el problema. Usalo cuando la conexion persistente aporte valor real.

## Ejemplo completo

```python
from wsbuilder import (
    App,
    Database,
    IntegerField,
    Model,
    Response,
    SecurityPolicy,
    SQLiteMemoryCache,
    TaskManager,
    TextField,
    install_cache,
    install_metrics,
    install_security,
)

app = App(cors_allow_origin="*")
install_metrics(app, app_name="starter-service")
install_security(app, SecurityPolicy(rate_limit_requests=120, rate_limit_window_seconds=60))
install_cache(app, SQLiteMemoryCache(default_ttl=30))
tasks = TaskManager(app, max_concurrent=2)

db = Database("starter.db")

class User(Model):
    id = IntegerField(primary_key=True, auto_increment=True)
    username = TextField(unique=True, index=True, null=False)

User.create_table(db)

@app.view("/")
def home(_request):
    return Response.text("starter service")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

@app.api("/api/jobs")
def jobs(_request):
    handle = tasks.spawn(lambda: "done", name="demo-job")
    return {"task_id": handle.id, "status": handle.status}

app.run("127.0.0.1", 8765)
```

## Checklist de salida

- [ ] La ruta `/` responde con una pagina clara.
- [ ] `GET /api/health` devuelve JSON.
- [ ] `GET /api/metrics` existe si activaste metricas.
- [ ] La seguridad esta instalada si el servicio va a exponerse.
- [ ] Los trabajos largos no bloquean el handler.
- [ ] Las respuestas repetidas aprovechan cache si conviene.

## Errores comunes

- Devolver `str` en una ruta `api()` cuando querias JSON.
- Poner toda la logica en `app.view("/")` en vez de separar modulo y handler.
- Activar WebSocket para un problema que era solamente request/response.
- Olvidar cerrar el servicio o el proceso de prueba cuando ya terminaste.

## Siguiente paso

Cuando esta guia ya te quede corta, pasa a [Aplicacion avanzada](advanced.md). Alli se ve como convertir esta base en un servicio serio con limites, tuning y operacion.

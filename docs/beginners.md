# Guia para principiantes

Esta guia asume que sabes ejecutar Python, pero no necesitas conocer el codigo
interno de `wsbuilder`.

## 1. Instalar

La forma mas segura es usar un entorno virtual:

=== "Linux / macOS"

    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install wsbuilder
    ```

=== "Windows PowerShell"

    ```powershell
    py -m venv .venv
    .venv\Scripts\Activate.ps1
    py -m pip install --upgrade pip
    py -m pip install wsbuilder
    ```

Verifica:

```bash
python -c "import wsbuilder; print(wsbuilder.__version__)"
wsbuilder --help
```

## 2. Crear tu primera app

Crea un archivo `app.py`:

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")

@app.view("/")
def home(_request):
    return Response.html("<h1>Hola desde wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

if __name__ == "__main__":
    app.run("127.0.0.1", 8765)
```

Ejecuta:

```bash
python app.py
```

Abre:

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/api/health`

## 3. Entender rutas `view` y `api`

Usa `@app.view` para HTML, texto o respuestas manuales:

```python
@app.view("/hello")
def hello(request):
    name = request.query.get("name", "mundo")
    return Response.html(f"<p>Hola {name}</p>")
```

Usa `@app.api` para JSON:

```python
@app.api("/api/echo", methods=("POST",))
def echo(request):
    payload = request.json() or {}
    return {"received": payload}
```

Si una ruta `api` devuelve `dict` o `list`, `wsbuilder` lo convierte a JSON. Si
necesitas controlar estado o headers, devuelve `Response.json(...)`.

```python
@app.api("/api/items", methods=("POST",))
def create_item(request):
    payload = request.json() or {}
    if not payload.get("name"):
        return Response.json({"message": "name is required"}, status=400)
    return Response.json({"name": payload["name"]}, status=201)
```

## 4. Activar documentacion y metricas

Para una app inicial conviene activar dos superficies de inspeccion:

```python
app.enable_metrics(path="/api/metrics")
app.enable_docs(path="/docs", json_path="/docs.json")
```

Con eso puedes abrir:

- `/api/metrics`: snapshot JSON de la app.
- `/docs`: documentacion runtime generada desde la instancia activa.
- `/docs.json`: version JSON de esa documentacion.

## 5. Probar sin abrir un puerto

`App.dispatch()` permite llamar una ruta directamente. Es util para tests y
scripts pequenos.

```python
from wsbuilder import App, Request

app = App()

@app.api("/api/health")
def health(_request):
    return {"ok": True}

request = Request(
    method="GET",
    path="/api/health",
    query_string="",
    headers={},
    body=b"",
    client=("127.0.0.1", 1234),
)

response = app.dispatch(request)
print(response.status)
print(response.body.decode("utf-8"))
```

## 6. Usar la demo incluida

Despues de instalar el paquete:

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

## Problemas comunes

| Sintoma | Causa probable | Solucion |
| --- | --- | --- |
| `externally-managed-environment` | El Python del sistema esta protegido por la distribucion | Usa un `venv` |
| `Address already in use` | Otro proceso usa el puerto | Cambia `8765` por otro puerto |
| `404 Not Found` | La ruta no coincide exactamente | Revisa que el path registrado empiece con `/` |
| `405 Method Not Allowed` | La ruta existe, pero no para ese metodo | Agrega `methods=("POST",)` o usa el metodo correcto |
| `request.json()` devuelve `None` | El cuerpo no es JSON valido | Envia JSON y `Content-Type: application/json` |

## Siguiente paso

Cuando ya tengas una ruta funcionando, pasa a [Intermedios](intermediate.md) para
agregar SQLite, cache, seguridad, logs y tareas.

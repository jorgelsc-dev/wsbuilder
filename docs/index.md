# wsbuilder

`wsbuilder` se distribuye como paquete Python puro, sin dependencias de runtime.
Eso permite una instalacion simple con `pip` en Linux, macOS y Windows siempre que
el usuario tenga Python `3.11+`.

## Instalacion recomendada

```bash
python -m pip install --upgrade pip
python -m pip install wsbuilder
```

La guia completa esta en [Instalacion](install.md).

## Instalacion en entorno virtual

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

## Verificar que quedo bien

```bash
python -c "import wsbuilder; print(wsbuilder.__version__)"
wsbuilder --help
```

## Instalar desde codigo fuente

Para instalar desde este repositorio:

```bash
python -m pip install .
```

La instalacion editable solo se recomienda para desarrollo:

```bash
python -m pip install -e .
```

## Instalar sin internet

Si necesitas compilar e instalar localmente sin acceso a PyPI, usa:

```bash
python -m build --no-isolation
python -m pip install --no-index dist/wsbuilder-*.whl
```

La explicacion detallada y la alternativa desde codigo fuente estan en [Instalacion](install.md).

## Inicio rapido

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("0.0.0.0", 8765)
```

## Demo incluida

Tras instalar el paquete puedes levantar la demo:

```bash
wsbuilder --host 0.0.0.0 --port 8765
```

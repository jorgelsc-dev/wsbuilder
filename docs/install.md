# Instalacion

`wsbuilder` se distribuye como paquete Python puro y no arrastra dependencias de
runtime. Eso simplifica tanto la instalacion normal con `pip` como la instalacion
local sin internet.

## 1. Instalar con pip

La ruta recomendada para la mayoria de usuarios es instalar desde PyPI:

```bash
python -m pip install --upgrade pip
python -m pip install wsbuilder
```

### En un entorno virtual

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

### Verificar la instalacion

```bash
python -c "import wsbuilder; print(wsbuilder.__version__)"
wsbuilder --help
```

## 2. Compilar en local e instalar en local sin internet

Usa esta ruta cuando ya tienes una copia local del repositorio y no quieres que
`pip` ni el sistema de build intenten consultar PyPI.

### Requisitos previos

- Tener el repositorio clonado o copiado en la maquina local.
- Tener Python `3.11+`.
- Tener disponibles en el entorno `build`, `setuptools` y `wheel`.
- Si vas a usar un `venv`, crearlo antes de desconectarte o con herramientas ya instaladas localmente.

### Compilar el wheel local sin red

El flag `--no-isolation` es importante: evita que `build` cree un entorno aislado
e intente descargar dependencias de compilacion.

```bash
python -m build --no-isolation
```

El resultado queda en `dist/`, por ejemplo:

```text
dist/wsbuilder-0.9.1.dev0-py3-none-any.whl
dist/wsbuilder-0.9.1.dev0.tar.gz
```

### Instalar el wheel local sin consultar PyPI

```bash
python -m pip install --no-index dist/wsbuilder-*.whl
```

### Instalar desde el codigo fuente local sin wheel

Si no quieres pasar por `dist/`, puedes instalar directamente desde el arbol del
repositorio. El flag `--no-build-isolation` evita que `pip` intente construir en
un entorno aislado y el flag `--no-deps` evita cualquier resolucion externa.

```bash
python -m pip install --no-index --no-build-isolation --no-deps .
```

### Verificar la instalacion offline

```bash
python -c "import wsbuilder; print(wsbuilder.__version__)"
wsbuilder --help
```

### Flujo recomendado para distribuir en una red cerrada

1. En una maquina de build, ejecuta `python -m build --no-isolation`.
2. Copia el archivo `dist/wsbuilder-*.whl` a la maquina destino.
3. En la maquina destino, instala con `python -m pip install --no-index dist/wsbuilder-*.whl`.

Ese flujo es el mas estable porque instala desde un artefacto ya construido y no
depende de herramientas de compilacion en la maquina final.

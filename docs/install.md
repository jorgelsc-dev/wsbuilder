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

En Debian, Kali, Ubuntu y otros entornos que aplican PEP 668, `pip` puede
rechazar la instalacion sobre el Python del sistema con el error
`externally-managed-environment`. En esos casos, instala dentro de un entorno
virtual.

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

Tambien existe el alias `wsbuilder-demo`, que ejecuta la misma demo incluida.

### Soporte mDNS opcional

Para publicar y descubrir servicios en la red local, instala el extra `mdns`:

```bash
python -m pip install 'wsbuilder[mdns]'
```

Este extra agrega `zeroconf`; la instalacion base permanece sin dependencias de
runtime. Consulta los [ejemplos de mDNS y DNS-SD](mdns.md).

### Instalar desde GitHub

Para probar lo ultimo que se ha integrado en `main`, o una version concreta sin
esperar a PyPI, instala directamente desde el repositorio:

```bash
# Una version publicada concreta (reproducible)
python -m pip install "git+https://github.com/jorgelsc-dev/wsbuilder@v1.5.0"

# Lo ultimo de main
python -m pip install --upgrade "git+https://github.com/jorgelsc-dev/wsbuilder@main"

# Con extras
python -m pip install "wsbuilder[tls,mdns] @ git+https://github.com/jorgelsc-dev/wsbuilder@main"
```

La version se calcula a partir de los tags de git (con `setuptools-scm`): un tag
`vX.Y.Z` produce `X.Y.Z`, y cada commit posterior produce una version distinta,
por ejemplo `X.Y.(Z+1).dev3+g1a2b3c4`. Por eso `pip install --upgrade` instala el
commit nuevo en cuanto cambia `main`.

!!! note "Versiones anteriores a este cambio"
    Antes, `main` declaraba la misma version (`X.Y.Z.dev0`) durante muchos
    commits, y `pip install --upgrade git+...` respondia "Requirement already
    satisfied" sin instalar nada nuevo. Si tienes instalada una copia de esa
    epoca, fuerza la reinstalacion una vez:

    ```bash
    python -m pip install --force-reinstall --no-deps "git+https://github.com/jorgelsc-dev/wsbuilder@main"
    ```

Comprueba que version tienes con `python -m pip show wsbuilder` o
`python -c "import wsbuilder; print(wsbuilder.__version__)"`.

## 2. Compilar en local e instalar en local sin internet

Usa esta ruta cuando ya tienes una copia local del repositorio y no quieres que
`pip` ni el sistema de build intenten consultar PyPI.

### Requisitos previos

- Tener el repositorio clonado o copiado en la maquina local.
- Tener Python `3.11+`.
- Tener disponibles en el entorno `build`, `setuptools>=77`, `setuptools-scm>=8` y `wheel`.
- Tener el historial y los tags de git (`git clone` completo). Sin ellos la version
  sale como `0.0.0.dev0`; puedes fijarla con la variable de entorno
  `SETUPTOOLS_SCM_PRETEND_VERSION=1.2.3`.
- Si vas a usar un `venv`, crearlo antes de desconectarte o con herramientas ya instaladas localmente.

### Compilar el wheel local sin red

El flag `--no-isolation` es importante: evita que `build` cree un entorno aislado
e intente descargar dependencias de compilacion.

```bash
python -m build --no-isolation
```

El resultado queda en `dist/`, por ejemplo:

```text
dist/wsbuilder-<version>-py3-none-any.whl
dist/wsbuilder-<version>.tar.gz
```

`<version>` se deriva de git: el commit de un tag `vX.Y.Z` compila `X.Y.Z`, y
cualquier otro commit compila una version de desarrollo posterior al ultimo tag
(`X.Y.(Z+1).devN+g<hash>`). Los artefactos publicados en PyPI salen siempre de un
tag.

### Instalar el wheel local sin consultar PyPI

```bash
python -m pip install --no-index dist/wsbuilder-*.whl
```

Si ese comando falla con `externally-managed-environment`, el wheel esta bien y
el bloqueo viene del Python del sistema. La ruta soportada es:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index dist/wsbuilder-*.whl
```

Solo si necesitas forzar una instalacion global en ese Python gestionado por el
sistema, `pip` ofrece `--break-system-packages`, pero queda fuera del flujo
recomendado porque puede interferir con paquetes de la distribucion:

```bash
python -m pip install --break-system-packages --no-index dist/wsbuilder-*.whl
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
3. En la maquina destino, instala dentro de un `venv` con `python -m pip install --no-index dist/wsbuilder-*.whl`.

Ese flujo es el mas estable porque instala desde un artefacto ya construido y no
depende de herramientas de compilacion en la maquina final.

## 3. Instalar para desarrollo

Desde la raiz del repositorio:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Validar el paquete:

```bash
PYTHONPATH=src pytest -q
ruff check src tests examples
```

Validar la documentacion:

```bash
python -m pip install -r requirements-docs.txt
mkdocs build --strict
```

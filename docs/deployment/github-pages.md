# GitHub Pages

Este repositorio ya puede publicarse como sitio de documentacion estatico con MkDocs.

## Requisitos

1. Tener una rama base (`main`) con este sitio de docs.
2. Activar GitHub Pages con fuente `GitHub Actions`.
3. Permitir al workflow escribir en `pages`.

## Workflow incluido

El archivo `.github/workflows/docs-pages.yml`:

- instala Python.
- instala `requirements-docs.txt`.
- construye `mkdocs`.
- publica el artefacto en GitHub Pages.

## Configuracion en GitHub

En el repositorio:

1. Ve a `Settings`.
2. Entra en `Pages`.
3. En `Build and deployment`, selecciona `GitHub Actions`.
4. Guarda.

## URL final

Cuando el workflow se ejecute en `main`, el sitio quedara disponible en la URL de Pages del repositorio.

## Preview local

```bash
python -m pip install -r requirements-docs.txt
mkdocs serve
```

Luego abre `http://127.0.0.1:8000`.


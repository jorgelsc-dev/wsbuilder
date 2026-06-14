# Repository Guidelines

## Project Structure & Module Organization
- `src/wsbuilder/` contains the package code. Public entry points are surfaced through `__init__.py` and `__main__.py`; feature modules stay split by concern (`http.py`, `ws.py`, `orm.py`, `cache.py`, `security.py`, `metrics.py`, `tasks.py`, `dns.py`, `proxyi.py`).
- `tests/` holds the automated suite. Files follow `test_*.py` naming and usually map to one feature area.
- `docs/` contains MkDocs content, with navigation and theme defined in `mkdocs.yml`.
- `.github/workflows/` contains CI, packaging, and docs publication workflows.

## Build, Test, and Development Commands
- `python -m pip install -e .` installs the package in editable mode for local development.
- `PYTHONPATH=src pytest -q` runs the test suite against the in-tree source layout.
- `python -m build` creates the wheel and source distribution in `dist/`.
- `python -m twine check dist/*` validates packaged artifacts.
- `mkdocs build --strict` builds the documentation site; `mkdocs serve` is useful for local preview.
- `python -m wsbuilder --host 0.0.0.0 --port 8765` starts the bundled demo server.

## Coding Style & Naming Conventions
- Target Python 3.11+ and keep to standard PEP 8 conventions.
- Use 4-space indentation, `snake_case` for functions/modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.
- Match the surrounding style: small focused modules, explicit helper functions, and minimal runtime dependencies.
- No formatter or linter is enforced in-repo, so keep changes consistent with existing code.

## Testing Guidelines
- Tests use `unittest` style classes such as `TestSQLiteMemoryCache`.
- Prefer deterministic tests with clear setup/teardown and isolated resources.
- Add coverage for both success and failure paths when touching public APIs, concurrency, caches, DNS, WebSocket, or persistence code.

## Commit & Pull Request Guidelines
- Branch from `main` using `feat/<name>` or `fix/<name>`.
- Keep each branch and PR focused on one logical change.
- Commit subjects are concise and often use conventional prefixes like `feat:`, `fix:`, `docs:`, or `chore(release):`.
- Open PRs against `main` and include validation notes for the changed area; add screenshots or rendered-doc links when documentation changes affect the site.

## Security & Configuration Tips
- Report vulnerabilities privately and avoid public exploit details until triage.
- Keep generated artifacts out of commits (`dist/`, `.pytest_cache/`, `__pycache__/`).
- When running tests locally, use either editable install or `PYTHONPATH=src` so imports resolve correctly.

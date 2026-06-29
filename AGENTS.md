# Repository Guidelines

## Mandatory Agent Workflow
- Before making any file edits, the agent must move work off `main`.
- The default workflow is:
  1. Run `git fetch origin` when network access is available and confirm the state of `origin/main`.
  2. Create or switch to a fresh topic branch with an allowed prefix.
  3. Apply the requested changes on that branch.
  4. Run the relevant validation commands.
  5. Commit with a focused message.
  6. Push the branch and open or reuse a PR against `main`.
- Allowed branch prefixes are `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, and `perf/`.
- If the current branch is a clean topic branch, the agent must switch back to `main`, fast-forward from `origin/main`, and create a fresh topic branch before starting a new task.
- If the current branch is `main` and already has uncommitted changes, the agent must immediately create a topic branch from the current `HEAD` before making any further edits, then continue the task on that branch.
- If a non-`main` branch already has uncommitted changes, the agent must not stack unrelated work on top of it.
- Every PR opened by the agent must target `main`.
- The agent must not leave task changes on `main`.
- If `gh` authentication or network access is unavailable, the agent must still prepare the branch locally and report the exact `gh pr create --base main --head <branch> --fill` command needed to finish.
- Keep the workflow definition in this file. Do not reintroduce a helper script unless the repository explicitly decides to make the script the source of truth again.

## Project Structure & Module Organization
- `src/wsbuilder/` contains the package code. Public entry points are surfaced through `__init__.py` and `__main__.py`; feature modules stay split by concern (`http.py`, `ws.py`, `orm.py`, `cache.py`, `security.py`, `metrics.py`, `tasks.py`, `dns.py`, `proxyi.py`).
- `tests/` holds the automated suite. Files follow `test_*.py` naming and usually map to one feature area.
- `docs/` contains MkDocs content, with navigation and theme defined in `mkdocs.yml`.
- `.github/workflows/` contains CI, packaging, and docs publication workflows.

## Build, Test, and Development Commands
- `git fetch origin` refreshes remote references before agent-driven work when the network is available.
- `git switch main && git pull --ff-only origin main && git switch -c docs/example-change` is the standard clean-tree branch creation flow.
- `git switch -c docs/example-change` is the required fallback when `main` already has uncommitted task changes that must be preserved before editing.
- `git push -u origin <branch>` publishes the current topic branch.
- `gh pr create --base main --head <branch> --fill` opens the PR against `main`; `gh pr view --json url --jq .url` is the preferred way to check whether a PR already exists.
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

## Agent Workflow Procedure
- Before starting any task, sync references from GitHub with `git fetch origin` and confirm the latest `origin/main`.
- Do not work directly on `main`. If the local `main` has unrelated changes, use a clean branch or worktree based on `origin/main` to avoid mixing work.
- Create the task branch from the updated `main` using the prefix that matches the change: `feat/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>`, `refactor/<name>`, or `test/<name>`.
- If `main` already contains the task's uncommitted edits, immediately preserve them with `git switch -c <type>/<slug>` before changing files.
- If a parallel spike or risky experiment is necessary, create an auxiliary branch from the current topic branch using one of the same allowed prefixes, keep it temporary, and fold it back into the primary task branch before opening the final PR unless it is reviewable on its own.
- Apply the requested changes only in that branch and keep the diff focused on a single logical objective.
- Run the relevant validation for the touched area before opening a PR.
- Push with `git push -u origin <branch>`, then open or reuse the PR with `gh pr create --base main --head <branch> --fill`.
- Open the PR against `main`, then share the branch name, validation performed, and PR summary for approval before merge.

## AGENTS.md Loading
- Compatible coding agents automatically read the repository-root `AGENTS.md` when they enter the workspace. Keep this file at the repository root and keep task-critical rules here if they must be applied automatically.
- Git, GitHub, and Python tooling do not execute `AGENTS.md`. If the workflow must be enforced outside the agent runtime, add repository tests or CI checks that fail when this file drifts or references removed tooling.
- When this file changes, update any tests and human-facing documentation that mirror the workflow so the repository has a single coherent protocol.

## Security & Configuration Tips
- Report vulnerabilities privately and avoid public exploit details until triage.
- Keep generated artifacts out of commits (`dist/`, `.pytest_cache/`, `__pycache__/`).
- When running tests locally, use either editable install or `PYTHONPATH=src` so imports resolve correctly.

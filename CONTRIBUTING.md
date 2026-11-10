# Contributing to Bifrost

## Run the checks only

You do not need the `claude` CLI or `gh` to run the tests and checks. You need `uv` and Docker:

```bash
docker compose up -d --wait postgres
uv sync --all-extras
make migrate
make test
```

`docker compose` creates only the `bifrost` database. The test fixtures create `bifrost_test` if it is missing, and refuse any database whose name does not contain `test`. `uv sync` creates `.venv` in the repo root. pyright reads `venvPath = "."` and `venv = ".venv"` from `pyproject.toml`, so a venv elsewhere fails `tests/e2e/test_quality_gates.py::test_type_safety_gate`. The leak gate needs no database. Without `LEAK_DENYLIST` set it skips its one term test and runs the other four: the secret scan of tracked files, a check of the term-list loader, a check of the word-prefix matcher and a check that its exemptions are tracked files:

```bash
uv run python -m pytest -q -p no:cacheprovider --noconftest tests/test_leak_gate.py
```

## Set up

Follow [RUNNING.md](RUNNING.md). For tests you need two things: `uv sync --all-extras` and Postgres with pgvector running (`docker compose up -d`).

The backend tests connect to `postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost_test`. Set `TEST_DATABASE_URL` to use a different database. If its role cannot create databases, create the database yourself first. Without a reachable Postgres most of the suite errors at collection or setup.

## Run the checks

```bash
uv run pytest tests/ -q --ignore=tests/e2e
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
cd ui && npx vitest run
pre-commit install          # once; runs the same checks on commit
```

`npx tsc -b` reports 27 type errors, so `npm run build` fails and is not in this list. CI runs the same set, with `npx tsc --noEmit` as its UI typecheck, which checks no source file because `ui/tsconfig.json` has `"files": []`. It also runs a pyright gate (`scripts/pyright_gate.py`), an import of every `bifrost.*` module, a shadow-import audit (`scripts/pr-stability-check.sh`), `pip-audit`, `npm audit`, an Alembic single-head check and a gate that rejects browser-automation packages.

## Write the test first

For a behavior change, write the failing test, run it and watch it fail, then write the code. Put the failing test in its own commit so the history shows it failed first. Do not edit or weaken a test to make it pass. If the test is wrong, say so in the pull request and explain why.

A change is done when the check that proves it passes, and you can paste the command and its output.

## Pull requests

- Keep a pull request to one change.
- Add or update tests with the code. The tests in `tests/test_neutral_names.py`, `tests/test_ui_neutral_copy.py` and `tests/test_leak_gate.py` enforce that no organization-specific names or secrets land in the repo.
- Comments in code explain a non-obvious reason, in at most three lines. Do not restate the code.
- Migrations: this repo has one baseline migration. Add new migrations after it and keep a single Alembic head.
- Do not add libraries that drive a browser (puppeteer, playwright, selenium). External data comes through `git clone`, `gh`, MCP servers, or `httpx` with explicit token auth.

## Reporting bugs and security issues

Open an issue for bugs. For vulnerabilities, follow [SECURITY.md](SECURITY.md) and do not open a public issue.

## License

By contributing you agree your contribution is licensed under Apache-2.0, the same as the project.

# ADR: Technology Stack — Bifrost V1

**Status:** ACCEPTED
**Language:** Python 3.12+

---

## Quick Reference — All 17 Decisions

> Update: D8 and D15 record the original plan to call the Anthropic API with an SDK key. The shipped implementation runs the `claude -p` CLI as a subprocess and uses your Claude login, so there is no API key and no `anthropic` SDK call in the extraction or research path.

### Status as built (2026-11)

The rows below were checked against the code on 2026-11-10. The original rows and decisions further down are unchanged.

| Entry | ADR says | Code does | Where |
|---|---|---|---|
| D0 | Python 3.13 in the decision text, 3.12+ in the header | `requires-python = ">=3.12"`, CI runs 3.12 | `pyproject.toml`, `.github/workflows/pr-check.yml` |
| D8 | `anthropic` SDK is required | `anthropic` is still listed as a dependency. Only `extraction_probe/agent.py` imports it. Everything else spawns `claude -p`. | `pyproject.toml`, `src/bifrost/extraction/cli_client.py` |
| D9 | `event_log` table and `LISTEN/NOTIFY` | An in-memory bus with a per-task replay buffer. No `event_log` table and no `LISTEN` in `src/`. | `src/bifrost/api/sse.py` |
| D10 | Postgres advisory locks | No advisory lock in `src/` | none |
| D11 | 1536-dimension embeddings | 384-dimension `all-MiniLM-L6-v2` vectors, `Vector(384)` | `src/bifrost/storage/models.py`, `src/bifrost/research/embeddings.py` |
| D14 | Compose with Postgres, Redis and an `api` service | Compose has one service, Postgres, on image `pgvector/pgvector:0.8.0-pg16` | `docker-compose.yml` |
| D17 | `pyrightconfig.json`, strict mode | `[tool.pyright]` in `pyproject.toml`, `typeCheckingMode = "standard"`, plus a gate script | `pyproject.toml`, `scripts/pyright_gate.py` |
| Package layout | `server/` and `db/` packages | No `server/` or `db/`. The API is in `api/` and the models are in `storage/`. Other packages are `extraction/`, `research/`, `collaboration/`, `actions/`, `manifest/`, `agents/`, `orchestration/`, `adapters/`, `core/`, `cli/` and `benchmarks/`. | `src/bifrost/` |

### Original quick reference

| # | Area | Choice | Rationale |
|---|------|--------|-----------|
| D0 | Language | Python 3.12+ | Agent SDK support, mature async database stack |
| D1 | Package Manager | `uv` | Fast, single tool, lock file |
| D2 | ORM | SQLAlchemy 2.0 + asyncpg | Typed async ORM |
| D3 | Web Framework | FastAPI + uvicorn | Pydantic-native, async |
| D4 | CLI Framework | Typer | Industry standard for Python CLIs |
| D5 | Test Framework | pytest + pytest-asyncio | Async fixtures, per-test DB sessions |
| D6 | DB Migrations | Alembic | Native to SQLAlchemy |
| D7 | Logging | structlog | Industry standard |
| D8 | LLM SDK | `anthropic` | REQUIRED — no alternative |
| D9 | Event Bus | asyncio + pg `event_log` table | Same pattern as TypeScript design |
| D10 | Concurrency | PostgreSQL advisory locks | Same mechanism, via SQLAlchemy |
| D11 | Vector Search | `pgvector` + HNSW | Keeps vectors in the same database |
| D12 | HTTP Client | `httpx` | Modern async; used by FastAPI ecosystem |
| D13 | Validation | Pydantic v2 | REQUIRED — FastAPI native |
| D14 | Container | Docker + Compose v2 | REQUIRED — no alternative |
| D15 | RESTRICTED repos | Same Claude login as other repos | Data handling follows the terms of your Claude plan |
| D16 | Linting | `ruff` | One fast tool for lint and format |
| D17 | Type Checking | `pyright` | Strong Pydantic and SQLAlchemy inference |

---

## Why Python (Not TypeScript, Not Go)

**Decision:** Python 3.12+ as the sole implementation language.

| Language | Anthropic Agent SDK | PostgreSQL + pgvector backend ecosystem | Verdict |
|----------|--------------------|-----------------------------|---------|
| Python | ✓ Available now | ✓ FastAPI + asyncpg + SQLAlchemy 2.0 + pgvector | **CHOSEN** |
| TypeScript | ✓ Available now | Partial: the UI is TypeScript, but the backend ORM and vector tooling are weaker | Rejected |
| Go | ✗ Client SDK only — NO Agent SDK | ✓ Strong backend language | Rejected |

**Rationale:** Track B (Managed Agents) requires the Anthropic Agent SDK, which exists only for Python and TypeScript. Python is the only language with both the Agent SDK and a mature stack for what Bifrost needs (FastAPI + SQLAlchemy + asyncpg + pgvector).

---

## Resolved Open Questions

| Question | Answer |
|----------|--------|
| Embedding dimension | 1536 (OpenAI text-embedding-3-small compatible) — revisit in V2 |
| Auto-generated API docs | FastAPI generates OpenAPI 3.1 natively — no extra plugin needed |
| Package layout | `src/bifrost/{core,server,cli,db,adapters,agents}/` (flat src layout) |
| RESTRICTED repos (D15) | Same runtime as other repos; your Claude plan's data terms apply |
| Self-hosted LLM (V1) | Not needed — RESTRICTED repos use the same runtime |

---

## Install Summary

```bash
# Install uv (one-time global)
curl -LsSf https://astral.sh/uv/install.sh | sh

# From repo root after initial scaffold
uv add fastapi uvicorn[standard] sqlalchemy[asyncio] asyncpg alembic \
       pydantic pydantic-settings structlog httpx pgvector typer anthropic

uv add --dev pytest pytest-asyncio pytest-cov ruff pyright
```

---

## Decisions

### D0: Language

**Decision:** Python 3.13
**Why:** See "Why Python" section above. Single language across all layers eliminates FFI, cross-language type drift, and split toolchains. Async-first with `asyncio` matches FastAPI + asyncpg's async model.

---

### D1: Package Manager

**Decision:** `uv`
**Install:** `curl -LsSf https://astral.sh/uv/install.sh | sh`

**Why:** Fastest Python package manager (~10-100x faster than pip). Built-in virtual environment management. Lock file support (`uv.lock`). Single tool replaces pip + virtualenv + pip-tools.

| Alternative | Why rejected |
|-------------|--------------|
| pip + venv | No lock file; slow; no workspace support |
| Poetry | Slower resolver; less adoption momentum than uv |
| Conda | Wrong scope — for data science envs, not pure Python services |

---

### D2: ORM

**Decision:** SQLAlchemy 2.0 (async mode) + asyncpg
**Install:** `uv add sqlalchemy[asyncio] asyncpg`

**Why:** SQLAlchemy 2.0's async session (`AsyncSession`) integrates directly with asyncpg for native PostgreSQL async I/O. The typed ORM models (via `DeclarativeBase`) are the single source of truth for DB schema and Python types — no impedance mismatch.

| Alternative | Why rejected |
|-------------|--------------|
| Tortoise ORM | Smaller ecosystem; fewer PostgreSQL-specific features |
| databases + raw SQL | No ORM conveniences; typed query results require manual mapping |
| asyncpg directly | Writing typed query builders for 15+ entity types by hand is untenable |

---

### D3: Web Framework

**Decision:** FastAPI + uvicorn
**Install:** `uv add fastapi uvicorn[standard]`

**Why:** Pydantic-native request/response validation. Auto-generates OpenAPI 3.1 docs from type annotations — no separate swagger plugin needed. Async-first design matches asyncpg and the async agent pipeline. SSE streaming (`StreamingResponse`) built-in for the `bifrost ask` endpoint. Native dependency injection for auth, cost tracking, and DB sessions.

| Alternative | Why rejected |
|-------------|--------------|
| Flask | Synchronous by default; no native Pydantic integration; older patterns |
| Starlette | FastAPI is Starlette — FastAPI adds the DI and OpenAPI layer |
| Django REST Framework | Too opinionated for a service-oriented architecture; heavier |

---

### D4: CLI Framework

**Decision:** Typer
**Install:** `uv add typer`

**Why:** Typer builds on FastAPI's type annotation approach — commands are functions with typed arguments, automatically generating help text and validation. Zero boilerplate for the ~12 Bifrost commands (`ask`, `repos add/list/remove`, `domains create/list`, `init`, `index`, `status`). Integrates cleanly with the `src/bifrost/cli/` package structure.

| Alternative | Why rejected |
|-------------|--------------|
| Click | More verbose; no automatic type inference from annotations |
| argparse | stdlib, but too low-level; manual help text |
| Fire | Too magical; less control over argument parsing |

---

### D5: Test Framework

**Decision:** pytest + pytest-asyncio + pytest-cov
**Install:** `uv add --dev pytest pytest-asyncio pytest-cov`

**Why:** The standard Python test runner. `pytest-asyncio` enables async test functions with `@pytest.mark.asyncio` — essential for testing async FastAPI routes and SQLAlchemy sessions. `pytest-cov` for coverage reporting. Fixtures provide clean per-test DB sessions without manual setup/teardown.

| Alternative | Why rejected |
|-------------|--------------|
| unittest | No fixtures; verbose; poor async support |
| nose2 | Declining adoption; pytest is the de facto standard |

---

### D6: Database Migration Tool

**Decision:** Alembic
**Install:** `uv add alembic`

**Why:** Native companion to SQLAlchemy. Autogenerates migration scripts from SQLAlchemy model diffs (`alembic revision --autogenerate`). Explicit SQL migration files for production deployments — reviewable, versionable, reversible.

| Alternative | Why rejected |
|-------------|--------------|
| django-south / django migrations | Django-specific; not applicable |
| Flyway | Requires Java or Docker sidecar; pure SQL only (no SQLAlchemy sync) |

---

### D7: Logging

**Decision:** structlog
**Install:** `uv add structlog`

**Why:** Structured JSON logging by default — feeds directly into CloudWatch/Datadog/Loki without format translation. Processor pipeline allows adding context (request_id, repo_id, agent_type) at any layer. Works alongside Python's `logging` stdlib via `structlog.stdlib`. Standard for Python services.

| Alternative | Why rejected |
|-------------|--------------|
| loguru | No processor pipeline; JSON output requires extra config |
| stdlib logging | Unstructured text; JSON requires custom formatters |

---

### D8: LLM SDK — REQUIRED

**Decision:** `anthropic` (Python SDK)
**Install:** `uv add anthropic`

**Why:** Mandated by dual-track architecture. Track A uses Anthropic Messages API (`client.messages.create()`). Track B uses Anthropic Managed Agent Sessions (when Research Preview access arrives). Both tracks implement the `AgentRuntime` ABC (`async def execute(self, task: AgentTask) -> AsyncGenerator[AgentEvent, None]`). Model routing — `claude-haiku-*` for triage, `claude-sonnet-*` for deep analysis — runs through this SDK.

No alternatives evaluated — this is a hard architectural requirement.

---

### D9: Event Bus

**Decision:** asyncio event system + PostgreSQL `event_log` table + `LISTEN/NOTIFY`
**Install:** No external dependency

**Why:** Entity write + event log entry happen in the same SQLAlchemy transaction — no events dropped on process crash. `LISTEN/NOTIFY` handles cross-process fan-out (e.g., CLI polling SSE from a separate API process) without Redis pub/sub. Avoids a queue broker in V1 while maintaining transactional durability.

| Alternative | Why rejected |
|-------------|--------------|
| Celery + Redis | Heavier job model than V1 needs; revisit for V2 extraction job scheduling |
| aio-pika (RabbitMQ) | External broker adds operational complexity; no transactional guarantees |

---

### D10: Concurrency Control

**Decision:** PostgreSQL advisory locks via SQLAlchemy
**Install:** No external dependency — uses `text()` escape hatch in SQLAlchemy

**Why:** Prevents duplicate simultaneous extraction for the same repo. Lock key: `hashtext(repo_id + ':' + revision_sha)`. Transaction-scoped → auto-released on commit or rollback, eliminating lock-leak bugs.

```python
await session.execute(
    text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
    {"key": f"{repo_id}:{revision_sha}"}
)
```

| Alternative | Why rejected |
|-------------|--------------|
| Redis SETNX | Requires correct TTL expiry; adds Redis round-trip; outage breaks extraction |
| Redlock | Multi-node Redis algorithm; overkill for V1 single-instance |

---

### D11: Vector Search

**Decision:** `pgvector` (Python client) + HNSW index
**Install:** `uv add pgvector`
**DB extension:** `CREATE EXTENSION IF NOT EXISTS vector;` in initial Alembic migration

**Why:** HNSW over IVFFlat: better recall, no reindexing as dataset grows, consistent query time. The review (M4) explicitly flagged IVFFlat degradation above ~10K vectors. Embedding dimension: **1536** (text-embedding-3-small compatible). Kept in V1 per architect decision.

```sql
CREATE INDEX ON embeddings USING hnsw (embedding vector_cosine_ops);
```

| Alternative | Why rejected |
|-------------|--------------|
| pgvector IVFFlat | Degrades above ~10K vectors; requires periodic re-clustering |
| Pinecone | External SaaS; data residency concern for RESTRICTED repos |
| Weaviate | Adds another Docker service; revisit if pgvector hits limits in V2 |

---

### D12: HTTP Client

**Decision:** `httpx`
**Install:** `uv add httpx`

**Why:** Async-first HTTP client designed for FastAPI/asyncio ecosystems. Supports `AsyncClient` for non-blocking requests. Handles streaming responses for Anthropic SSE forwarding. All adapter HTTP calls (GitHub API, Jira REST API) fit within httpx's capabilities. `httpx.AsyncClient` is the recommended client in FastAPI docs.

| Alternative | Why rejected |
|-------------|--------------|
| requests | Synchronous only; blocks the event loop |
| aiohttp | More complex API; httpx has better compatibility with httpcore |

---

### D13: Validation — REQUIRED

**Decision:** Pydantic v2 + pydantic-settings
**Install:** `uv add pydantic pydantic-settings`

**Why:** Already required — FastAPI uses Pydantic natively for request/response validation. `BaseModel` subclasses eliminate separate interface declarations. `pydantic-settings` handles environment variable loading for `BifrostConfig`. `model_validator` and custom validators handle LLM response parsing and confidence gate logic.

No alternatives evaluated — already required by FastAPI dependency.

---

### D14: Container — REQUIRED

**Decision:** Docker + Docker Compose v2
**Install:** Docker Desktop (developer) or Docker Engine + Compose plugin (CI/CD)

**Why:** Reproducible local development with PostgreSQL 16 + pgvector, Redis 7, and the Bifrost API. Production image: Python 3.13-slim base, multi-stage build, non-root user.

**`docker-compose.yml`:**
```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: bifrost
      POSTGRES_USER: bifrost
      POSTGRES_PASSWORD: bifrost
    ports: ["5432:5432"]
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  api:
    build: .
    ports: ["8000:8000"]
    environment:
      DATABASE_URL: postgresql+asyncpg://bifrost:bifrost_dev@postgres:5432/bifrost
      REDIS_URL: redis://redis:6379
      BIFROST_TRACK: "A"
    depends_on: [postgres, redis]

volumes:
  postgres_data:
```

---

### D15: RESTRICTED Repos

**Decision:** RESTRICTED repos use the same Claude runtime as every other repo (the `claude -p` CLI under your Claude login)
**V1 behavior:** no blocking 403

**Why:** The original design (ollama with 403 Forbidden) was rejected. Your Claude plan's data handling terms decide whether code may be sent to the model, so V1 ships with full RESTRICTED repo support on the same runtime. A self-hosted LLM (ollama) is deferred to V2 if ever needed.

---

### D16: Linting

**Decision:** `ruff`
**Install:** `uv add --dev ruff`

**Why:** Single tool replaces flake8 + isort + black. 10-100x faster than flake8. Configured via `pyproject.toml` — `[tool.ruff]` section.

---

### D17: Type Checking

**Decision:** `pyright`
**Install:** `uv add --dev pyright`

**Why:** Best-in-class inference for Pydantic v2 models and SQLAlchemy 2.0 typed columns. Strict mode catches implicit `Any` propagation from LLM response parsing. Configured via `pyrightconfig.json`.

---

## Package Layout

```
bifrost/
├── src/
│   └── bifrost/
│       ├── core/          # Domain models, ABCs, shared types
│       ├── server/        # FastAPI app, routes, middleware
│       ├── cli/           # Typer CLI commands
│       ├── db/            # SQLAlchemy models, Alembic migrations
│       ├── adapters/      # Source adapters (GitHub, Jira, local)
│       └── agents/        # AgentRuntime implementations (Track A + B)
├── tests/
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── pyrightconfig.json
├── docker-compose.yml
└── Dockerfile
```

---

## Consequences

**Positive:**
- Single language across all layers — no FFI, no type drift between services
- Mainstream stack: FastAPI + SQLAlchemy + asyncpg + pgvector + pytest
- FastAPI generates OpenAPI docs natively — no extra plugin needed
- Pydantic v2 is the validation layer AND the type system — zero duplication
- RESTRICTED repos use the same runtime in V1 — no 403 blocking, no ollama setup

**Risks to watch:**
- SQLAlchemy 2.0 async mode has subtleties — always use `async with AsyncSession` context managers, never raw `Session`
- Pydantic v2 vs v1 has breaking changes — ensure all dependencies use v2 (FastAPI 0.100+ is required)
- `uv` is newer than pip/Poetry — some CI systems may need `uv` installation step added
- HNSW index must be created AFTER initial data load for best build performance

# Bifrost dev thin wrappers. All real logic lives in scripts/dev.sh.
# Run `make help` to see everything.

SHELL := /bin/bash
DEV := ./scripts/dev.sh

.PHONY: help up start run down stop restart reload backend ui status ps \
        logs logs-backend logs-ui health ping migrate test

help:
	@$(DEV) help

# ── Start ────────────────────────────────────────────────────────
up start run:
	@$(DEV) up

# ── Stop ─────────────────────────────────────────────────────────
down stop:
	@$(DEV) down

# ── Restart (the one you want 99% of the time) ──────────────────
restart reload:
	@$(DEV) restart

# ── Scoped ──────────────────────────────────────────────────────
backend:
	@$(DEV) backend

ui:
	@$(DEV) ui

# ── Observability ───────────────────────────────────────────────
status ps:
	@$(DEV) status

logs:
	@$(DEV) logs

logs-backend:
	@$(DEV) logs backend

logs-ui:
	@$(DEV) logs ui

health ping:
	@$(DEV) health

# ── Database and tests ──────────────────────────────────────────
migrate:
	uv run alembic upgrade head

test:
	uv run --extra dev pytest tests/ -q --ignore=tests/e2e

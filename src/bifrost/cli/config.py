"""Application configuration via pydantic-settings.

Reads from environment variables with BIFROST_ prefix where applicable.
Provides sensible defaults for local development.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar, Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from bifrost.cli.config_sections import (
    ApiTokenConfig,
    ConventionsConfig,
    IdentityConfig,
    LinkingConfig,
    ManifestConfig,
    McpConfig,
    OrgConfig,
    SourcesConfig,
)
from bifrost.errors import ConfigError

DEFAULT_CONFIG_FILE = "bifrost.yaml"


def resolve_config_path() -> Path | None:
    """Locate the bifrost.yaml to load.

    ``BIFROST_CONFIG`` unset: ``./bifrost.yaml`` if it exists. Set to ``""``: no
    file. Set to a path: that file, which must exist.
    """
    configured = os.environ.get("BIFROST_CONFIG")
    if configured is None:
        default = Path(DEFAULT_CONFIG_FILE)
        return default if default.is_file() else None
    if configured == "":
        return None
    path = Path(configured).expanduser()
    if not path.is_file():
        raise ConfigError(f"BIFROST_CONFIG points at {str(path)!r}, which is not a file")
    return path


class BifrostSettings(BaseSettings):
    """Central configuration for the Bifrost platform."""

    model_config = SettingsConfigDict(
        env_prefix="BIFROST_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Precedence: init kwargs > env > .env > secrets > bifrost.yaml > defaults."""
        sources: tuple[PydanticBaseSettingsSource, ...] = (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
        path = resolve_config_path()
        if path is None:
            return sources
        return (*sources, YamlConfigSettingsSource(settings_cls, yaml_file=path))

    # Company-specific values live in bifrost.yaml; see bifrost.example.yaml.
    org: OrgConfig = Field(default_factory=OrgConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    manifest: ManifestConfig = Field(default_factory=ManifestConfig)
    linking: LinkingConfig = Field(default_factory=LinkingConfig)
    conventions: ConventionsConfig = Field(default_factory=ConventionsConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    api_tokens: list[ApiTokenConfig] = Field(default_factory=list)

    # Track selection: "A" = TypeScript+PostgreSQL, "B" = Managed Agents
    track: Literal["A", "B"] = "A"

    # Connect/read/write/pool timeout for every outbound HTTP client.
    http_timeout_seconds: float = Field(default=30.0, gt=0)

    # Database
    database_url: str = "postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost"

    # API server
    api_base_url: str = "http://localhost:8000"

    # Logging. log_level is applied by configure_logging. log_format picks
    # the renderer:
    #   json    — one JSON object per line (prod / log shippers)
    #   console — colorized human-readable (local dev)
    #   auto    — console on a TTY, json otherwise (default)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console", "auto"] = "auto"

    # Claude Code CLI — all LLM calls go through `claude -p` subprocess, which
    # uses whatever auth Claude Code is logged in with (run `claude auth login`).
    # No API keys, no gateway URLs.
    claude_cli_path: str = "claude"
    claude_cli_timeout_seconds: int = 600  # per-call wall clock

    # Process-wide cap on concurrent `claude -p` spawns. The chat fan-out
    # (N repos × up to 3 agents each) and the extraction worker all share one
    # Claude session and event loop. At 6 the semaphore bottlenecked multi-repo
    # queries — 5 repos × 3 agents = 15 calls needed, but only 6 ran at once,
    # forcing 2-3 serial waves. Raised to 20 to allow a full 5-repo fan-out
    # (code + jira + external per repo = 15) to run concurrently. The upper
    # bound of 64 guards against OS fd exhaustion on smaller hosts. If the
    # Claude API rate-limits at high concurrency, lower via env var.
    # Env: BIFROST_MAX_CONCURRENT_CLI_CALLS.
    max_concurrent_cli_calls: int = Field(default=20, ge=1, le=64)

    # Content-addressed LLM response cache (cost control). When on, an EXACT
    # re-invocation of claude -p (same prompt+model+schema) returns the stored
    # response for $0 instead of re-burning tokens — targets re-extraction,
    # repeated triage, benchmark re-runs. Stored in knowledge_text_cache.
    # Per-call ``force_refresh=True`` always bypasses. Defensive: any cache
    # error falls through to a live call.
    llm_cache_enabled: bool = True

    # Models — configurable so you can retune without code changes. Defaults
    # track the latest publicly available Claude models. Override via env:
    #   BIFROST_TRIAGE_MODEL, BIFROST_EXTRACTION_MODEL,
    #   BIFROST_AGENT_MODEL, BIFROST_REASONING_MODEL.
    # Pass either an alias ("sonnet", "haiku", "opus") or a full model id.
    triage_model: str = "claude-opus-4-8"  # quick action, matching, slack escalation
    extraction_model: str = "claude-sonnet-4-6"  # extraction phases D–I (quality)
    agent_model: str = "claude-sonnet-4-6"  # per-source research agents, action agents
    reasoning_model: str = "claude-opus-4-8"  # orchestrator, planner, router, clarify,
    # intent_router, synthesis — the steps that
    # decide the entire run shape and produce
    # the final answer. Worth premium model cost.

    # Scope deepening: after a research wave, a reasoning-tier model decides
    # whether the answer is complete or whether specific NOT-yet-walked repos
    # hold the load-bearing logic, and triggers another wave on them. This is
    # the "follow the trail across repos" intelligence that turns a hedged
    # one-turn answer into a complete, code-grounded one.
    enable_scope_deepening: bool = True

    # Extraction worker
    max_concurrent_extractions: int = 1
    checkpoint_dir: str = "/tmp/bifrost-checkpoints"
    clone_dir: str = "/tmp/bifrost-clones"
    extraction_dry_run: bool = False  # True = mock LLM calls for testing

    # Handover generation — pre-flight cost ceiling.
    # When a tree decomposition would cost more than this in Sonnet
    # calls, the API 409s the generate request unless the caller passes
    # ``?force=true``. Default $50 = roughly 500 child Sonnet calls,
    # which covers the largest realistic domain/squad scopes.
    handover_max_cost_usd: float = 50.0

    # Handover generation — per-requester rate limit.
    # Caps how many ``POST .../generate`` calls a single cookie-session
    # requester can issue per minute, so a runaway client can't fan
    # out N concurrent generates and burn the cost ceiling N times in
    # parallel. ``0`` disables the gate entirely. The 30/min default
    # accommodates typical interactive use (regenerate a handover
    # every ~2s if needed) while clamping bursts.
    handover_rate_limit_per_minute: int = 30

    # Bearer token the CLI sends (``Authorization: Bearer``); mint one with
    # ``bifrost token new`` and add its digest to ``api_tokens`` on the server.
    token: str | None = None

    # Fernet key(s) for encrypting ``_secret.*`` values stored in JSONB columns.
    # Comma-separated for rotation: the first key encrypts, all decrypt.
    credential_key: str | None = None

    # Slack escalation post: on only when BOTH are set. The channel (name like
    # ``#eng-handovers`` or id) is fixed here; the bot token needs chat:write.
    slack_escalation_channel: str | None = None
    slack_bot_token: SecretStr | None = None

    # CORS allowed origins.
    # Comma-separated list of origins permitted to make credentialed
    # cross-origin requests. The default covers the local Vite dev
    # server. In production, set
    # ``BIFROST_CORS_ORIGINS=https://bifrost.example.com``. Setting
    # this to ``*`` is no longer supported — Starlette's
    # CORSMiddleware echoes origins when ``allow_credentials=True``
    # is on, which lets any non-browser client land credentialed
    # requests with arbitrary Origin headers. The allowlist is
    # exact-match.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Cookie signing key.
    # Used to HMAC-sign the ``bifrost_session`` cookie so a client
    # can't spoof another requester's session id (which is the
    # rate-limit bucket and audit ``actor`` field). When unset
    # (local dev), an ephemeral random secret is generated at
    # startup; cookies survive only the lifetime of one process.
    # In production, set this to a stable random string (e.g.
    # ``python -c "import secrets; print(secrets.token_urlsafe(32))"``)
    # so cookies survive restarts.
    session_signing_key: str | None = None

    # ── Identity layer ─────────────────────────────────────────────
    # Provider selection and its settings live under ``identity`` (see
    # ``IdentityConfig``); resolution order is documented in
    # ``bifrost/api/auth/__init__.py``.

    # When True and ``identity.provider`` is ``none``, every request
    # without an operator env override resolves to the demo principal
    # (``identity.demo_email``). Local dev and demos only.
    demo_mode: bool = False

    # With ``identity.provider=none``, the local operator's email and
    # comma-separated groups. Without an email the demo principal applies.
    operator_email: str | None = None
    operator_groups: str | None = None

    # Production-strict startup mode: refuse to boot unless the
    # prod-required secrets and explicit config are present.
    production_mode: bool = False

    # ── Research / cache tuning knobs ──────────────────────────────
    # Folded in from scattered ``os.getenv`` reads (research/coordinator,
    # research/agent_cache, collaboration/loopback). Raw parsing silently
    # clamped or fell back on bad input — a typo'd
    # ``BIFROST_CACHE_SIM_THRESHOLD=2`` quietly became 1.0 and nobody knew.
    # As bounded fields they now fail fast at boot (via validate_startup)
    # instead of silently mis-tuning retrieval.

    # First-touch auto-extraction wait budget, seconds. When a query is the
    # first to reach a manifest-only repo, the chat blocks up to this long for
    # deep extraction before falling back to manifest metadata.
    auto_extract_wait_s: float = Field(default=90.0, ge=0.0)

    # Semantic answer-cache cosine-similarity floor (pgvector ``<=>``).
    cache_sim_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # Loopback doubt-clustering similarity floor.
    doubt_cluster_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # Research DEPTH tier (NOT a discovery cap — every relevant repo is always
    # found). Top-N most-relevant repos get the full agent fan-out; any beyond
    # are DEFERRED and surfaced to the user ("found, not deep-searched"), never
    # silently dropped. Per-conversation ``research_full_depth`` overrides this
    # to research everything. Default 8 covers typical scopes; only unusually
    # broad questions defer. Bounds the pathological all-repos fan-out on cost.
    research_max_repos: int = Field(default=8, ge=1, le=50)

    # Server-side ceiling on a conversation's walk_breadth_soft_cap; members
    # cannot raise the walk above it through PATCH /settings or the plan.
    research_walk_breadth_ceiling: int = Field(default=200, ge=20, le=2000)

    @field_validator("api_tokens")
    @classmethod
    def _unique_token_names(cls, value: list[ApiTokenConfig]) -> list[ApiTokenConfig]:
        names = [t.name for t in value]
        duplicated = sorted({n for n in names if names.count(n) > 1})
        if duplicated:
            raise ValueError(f"api_tokens names must be unique; repeated: {duplicated}")
        return value

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse the comma-separated allowlist into a Python list.

        Empty strings are filtered out. Whitespace around entries is
        stripped. Returns an empty list if the setting is empty,
        which CORSMiddleware will treat as "no cross-origin allowed"
        — same-origin requests still work.
        """
        return [origin.strip() for origin in (self.cors_origins or "").split(",") if origin.strip()]

    # Dev-default sentinels — the validator below uses these to
    # detect "this setting was never overridden from its local-dev
    # default" without having to compare against the literal string
    # in two places. ClassVar tells pydantic this isn't a field.
    _DEV_DATABASE_URL_DEFAULT: ClassVar[str] = (
        "postgresql+asyncpg://bifrost:bifrost_dev@localhost:5432/bifrost"
    )
    _DEV_CORS_ORIGINS_DEFAULT: ClassVar[str] = "http://localhost:5173,http://127.0.0.1:5173"

    def validate_production_secrets(self) -> list[str]:
        """Return a list of human-readable errors for prod-misconfig.

        Returns an empty list when ``production_mode`` is False — the
        caller can short-circuit without parsing the result. The
        intent is to fail loud at startup rather than silently boot a
        misconfigured service:

          * ``session_signing_key`` must be set (or every restart
            invalidates all cookies and rate-limit buckets become
            spoofable).
          * ``database_url`` must not be the dev localhost default
            (silent connection to the wrong db is the most common
            ``it works on my machine`` failure).
          * ``cors_origins`` must not be the dev localhost default.
          * An identity provider that can actually verify callers:
            ``header`` with ``trusted_proxy`` or ``oidc_jwt``.
        """
        if not self.production_mode:
            return []
        errors: list[str] = []
        if not self.session_signing_key:
            errors.append(
                "BIFROST_SESSION_SIGNING_KEY is required in production "
                "mode. Generate with: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if self.database_url == self._DEV_DATABASE_URL_DEFAULT:
            errors.append(
                "BIFROST_DATABASE_URL is still the local-dev default "
                f"({self._DEV_DATABASE_URL_DEFAULT!r}). Set it to your "
                "production Postgres URL."
            )
        if not (self.credential_key or "").strip():
            errors.append(
                "BIFROST_CREDENTIAL_KEY is required in production mode "
                "(encrypts stored repo credentials). Generate with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        if self.cors_origins == self._DEV_CORS_ORIGINS_DEFAULT:
            errors.append(
                "BIFROST_CORS_ORIGINS is still the local-dev default. "
                "Set it to your production frontend origin(s), e.g. "
                "'https://bifrost.example.com'."
            )
        ident = self.identity
        header_ok = ident.provider == "header" and ident.trusted_proxy
        if not header_ok and ident.provider != "oidc_jwt":
            errors.append(
                "Production mode needs a trustable identity check: set "
                "identity.provider=header with identity.trusted_proxy=true "
                "or identity.provider=oidc_jwt."
            )
        if ident.provider == "oidc_jwt" and not ident.oidc.jwks_url:
            errors.append(
                "identity.provider=oidc_jwt but identity.oidc.jwks_url is unset. "
                "Verification cannot fetch the signing keys."
            )
        if self.demo_mode:
            errors.append(
                "BIFROST_DEMO_MODE=true is incompatible with "
                "production mode. Demo mode bypasses identity entirely."
            )
        return errors


# Module-level singleton — import and call get_settings() instead of constructing directly.
_settings: BifrostSettings | None = None


def get_settings() -> BifrostSettings:
    """Return the cached settings singleton."""
    global _settings  # noqa: PLW0603
    if _settings is None:
        _settings = BifrostSettings()
    return _settings


def reset_settings() -> None:
    """Reset cached settings — useful in tests."""
    global _settings  # noqa: PLW0603
    _settings = None


def validate_startup() -> BifrostSettings:
    """Eagerly construct + validate settings so misconfig aborts boot.

    pydantic validates every field's type and bounds at construction; calling
    this once at startup turns a would-be late, mid-request 500 into a loud
    boot failure that lists *all* problems at once. Raises
    :class:`~bifrost.errors.ConfigError` (the config-error taxonomy) rather than a raw
    ``ValidationError`` so logging/HTTP treat it as a config fault, not a 500.

    This is the single "config gate" — call it at every entrypoint (app
    factory, CLI callback, benchmark runner) before doing real work. It also
    rejects BIFROST settings that nothing reads (see ``unknown_settings``).
    """
    from bifrost.cli.unknown_settings import check_unknown_settings

    check_unknown_settings()
    try:
        return get_settings()
    except ValidationError as exc:
        lines = [
            f"  - {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigError(
            "Invalid Bifrost configuration — fix the BIFROST_* environment "
            "variables and restart:\n" + "\n".join(lines),
            details={"errors": exc.errors()},
        ) from exc


def resolve_migration_database_url() -> str | None:
    """URL for alembic: process env, then ./.env, else None (alembic.ini applies)."""
    from_env = os.environ.get("DATABASE_URL") or os.environ.get("BIFROST_DATABASE_URL")
    if from_env:
        return from_env
    return dotenv_values(".env").get("BIFROST_DATABASE_URL") or None

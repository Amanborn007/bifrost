"""Per-conversation chat settings.

The user controls research behaviour from the chat sidebar. This
schema is the single source of truth for the knob shape; the route
layer, the storage layer, and the UI all consume it.

Knobs map onto the architecture stages:

  - Stage 3 (blast-radius walk):
      walk_depth, walk_breadth_soft_cap, edge_directions, edge_types,
      confidence_floor
  - Stage 6 (Bifrost status / auto-extract):
      auto_extract_missing, auto_extract_wait_s
  - Stage 7 (agent dispatch):
      external_sources
  - Stage 8 (multi-code-agent sharding):
      agents_per_repo_max
  - Stage 10 (synthesis):
      code_first_strict

The research flow reads it via ``EntityRepository.get_chat_settings``
and lets it shape the actual research flow.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Canonical edge types. Mirrors the values used in
# ``EntityRelationship.rel_type`` across extraction. Settings that
# reference unknown rel_types are dropped silently at merge time
# rather than rejected — the user shouldn't see a 422 because the
# extraction pipeline added a new rel_type.
_KNOWN_EDGE_TYPES: tuple[str, ...] = (
    "calls",
    "depends_on",
    "owns",
    "produces_event",
    "consumes_event",
    "implements",
    "extends",
    "references",
    "imports",
    "calls_service",
    "publishes_to",
    "shares_topic",
)

_EXTERNAL_SOURCES = ("jira", "confluence", "github", "slack", "gdocs")


class ChatSettings(BaseModel):
    """Resolved settings — every field has a value.

    Returned from ``GET /conversations/{id}/settings``. Field defaults
    here ARE the application defaults; nothing else supplies them.
    """

    walk_depth: int = Field(
        default=2,
        ge=1,
        le=6,
        description="BFS hops out from seed entities. 1 = direct callers/callees only.",
    )
    walk_breadth_soft_cap: int = Field(
        default=200,
        ge=20,
        le=2000,
        description=(
            "Soft cap on nodes per blast-radius walk. NOT a hard truncation — "
            "exceeding it emits a warning event so the user can re-scope, "
            "but all results still come back."
        ),
    )
    confidence_floor: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum edge confidence to follow during walk. 0.55 is the "
            "T4 cutoff (docs-only / fuzzy edges below this); 0.85 keeps "
            "only T1+T2 (code-verified / config-declared) edges."
        ),
    )
    edge_directions: Literal["upstream", "downstream", "both"] = Field(
        default="both",
        description="Which BFS direction to walk from seeds.",
    )
    edge_types: list[str] = Field(
        default_factory=lambda: list(_KNOWN_EDGE_TYPES),
        description="Allowlist of EntityRelationship.rel_type values to follow.",
    )
    code_first_strict: bool = Field(
        default=True,
        description=(
            "If true, the synthesis prompt's HARD RULE 1 (code is ground "
            "truth) is enforced; external-source claims that don't have "
            "code backing must be hedged. If false, external sources are "
            "weighted equally — only useful when chasing doc-level questions."
        ),
    )
    agents_per_repo_max: int = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "When >1, large repos (>200 components) get sharded across "
            "this many parallel code agents. Default of 1 keeps cost "
            "predictable; bump to 4 for deep dives into monorepos."
        ),
    )
    external_sources: list[Literal["jira", "confluence", "github", "slack", "gdocs"]] = Field(
        default_factory=lambda: list(_EXTERNAL_SOURCES),
        description="Which external agents to fan out to (per-repo, when manifest metadata exists).",
    )
    auto_extract_missing: bool = Field(
        default=True,
        description=(
            "When the blast-radius walk picks up a manifest_only repo (no "
            "deep extraction yet), auto-trigger extraction. Disable to "
            "speed up answers at the cost of breadth."
        ),
    )
    auto_extract_wait_s: float = Field(
        default=90.0,
        ge=0.0,
        le=600.0,
        description=(
            "Seconds to wait for auto-extractions before answering. Repos "
            "that finish in time are promoted to deep-indexed for this "
            "turn; the rest run in the background and surface in follow-ups. "
            "0 = don't block at all."
        ),
    )
    research_include_slack: bool = Field(
        default=False,
        description=(
            "Opt-in: run the per-repo Slack agent during fan-out. Off by "
            "default because Slack has ~0-5% citation rate and adds 10-40s "
            "per query. Enable when you want to surface recent channel "
            "discussions alongside code evidence."
        ),
    )
    research_include_github: bool = Field(
        default=False,
        description=(
            "Opt-in: run the per-repo GitHub agent (PRs/issues) during "
            "fan-out. Off by default because GitHub has ~0-5% citation rate "
            "and adds 10-15 agent calls per query. Enable when you want to "
            "trace recent PR activity or open issues alongside code evidence."
        ),
    )
    research_include_confluence: bool = Field(
        default=False,
        description=(
            "Opt-in: run the per-repo Confluence agent during fan-out. Off "
            "by default because the org-wide ExternalAgent already searches "
            "Confluence and per-repo coverage overlaps with it. Enable when "
            "you need deep, space-scoped Confluence search for a specific repo."
        ),
    )
    research_full_depth: bool = Field(
        default=False,
        description=(
            "Deep-search EVERY relevant repo the router finds, with no depth "
            "tier. Off (default): the top BIFROST_RESEARCH_MAX_REPOS are "
            "deep-searched and any others are surfaced as 'found, not "
            "deep-searched' (never dropped). On: full in-depth coverage — "
            "higher cost + latency, maximum completeness."
        ),
    )

    @model_validator(mode="after")
    def _filter_unknown_edge_types(self) -> ChatSettings:
        """Drop unknown edge_types silently rather than 422.

        The extraction pipeline can add a new rel_type tomorrow, and
        users with stored settings shouldn't see route-level errors
        because their stored value mentions an obsolete name.
        """
        self.edge_types = [t for t in self.edge_types if t in _KNOWN_EDGE_TYPES]
        return self


class PartialChatSettings(BaseModel):
    """All fields optional — used by ``PATCH /settings``.

    Pydantic model_dump(exclude_unset=True) gives us only the fields
    the user actually sent, which we merge into the stored JSONB and
    re-validate as a full ``ChatSettings``.
    """

    walk_depth: int | None = Field(default=None, ge=1, le=6)
    walk_breadth_soft_cap: int | None = Field(default=None, ge=20, le=2000)
    confidence_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    edge_directions: Literal["upstream", "downstream", "both"] | None = None
    edge_types: list[str] | None = None
    code_first_strict: bool | None = None
    agents_per_repo_max: int | None = Field(default=None, ge=1, le=8)
    external_sources: list[Literal["jira", "confluence", "github", "slack", "gdocs"]] | None = None
    auto_extract_missing: bool | None = None
    auto_extract_wait_s: float | None = Field(default=None, ge=0.0, le=600.0)
    research_include_slack: bool | None = None
    research_include_github: bool | None = None
    research_include_confluence: bool | None = None


def merge_settings(
    stored: dict,
    patch: PartialChatSettings,
) -> ChatSettings:
    """Merge a PATCH body into stored JSONB and return a validated
    ``ChatSettings``. Unknown / dropped fields in ``stored`` fall
    back to defaults. Fields the user didn't send in ``patch`` keep
    their stored value.
    """
    merged = dict(stored or {})
    for field, value in patch.model_dump(exclude_unset=True).items():
        merged[field] = value
    return ChatSettings(**merged)


def settings_from_stored(stored: dict | None) -> ChatSettings:
    """Build a ``ChatSettings`` from a possibly-empty stored dict.

    Empty dict → all defaults. Partial dict → merged with defaults.
    Used at GET time and by the coordinator at the top of every
    research turn.
    """
    return ChatSettings(**(stored or {}))

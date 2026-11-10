"""Structural contracts: body scrubbing, admin/api router split,
background-task strong refs, h2 demotion in user content, and
Pydantic max_length.
"""

from __future__ import annotations

import pydantic
import pytest

from bifrost.api.routes import admin_router_top, api_router
from bifrost.api.routes.handover_docs import (
    _BACKGROUND_TASKS,
    _append_qa_entry,
    _demote_h2_lines,
    _normalise_question,
    _scrub_body,
    _spawn_background,
)
from bifrost.api.schemas.collaboration import (
    AppendCollabMessageRequest,
    CreateCollabSessionRequest,
    PatchHandoverBodyRequest,
    PromoteCandidateRequest,
)

# ── body scrubbing ──────────────────────────────────────────────


def test_scrub_redacts_github_token() -> None:
    src = "Hi\nGITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890\nbye"
    out = _scrub_body(src, sink="test")
    assert "ghp_abcdefghij" not in out
    assert "REDACTED" in out


def test_scrub_redacts_aws_key() -> None:
    src = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE in the body"
    out = _scrub_body(src, sink="test")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "REDACTED" in out


def test_scrub_passes_clean_text() -> None:
    src = "## Section\n\nThis is regular handover content."
    assert _scrub_body(src, sink="test") == src


def test_scrub_handles_empty_input() -> None:
    assert _scrub_body("", sink="test") == ""
    assert _scrub_body(None, sink="test") == ""


# ── admin/api router split ──────────────────────────────────────


def test_admin_router_has_admin_dep_only() -> None:
    """admin_router_top is mounted SEPARATELY at /api/v1 in app.py and
    carries exactly one dependency: the admin role gate."""
    assert len(admin_router_top.dependencies) == 1
    gate = admin_router_top.dependencies[0].dependency
    assert getattr(gate, "required_role", None) == "admin"


def test_api_router_has_no_router_level_gate() -> None:
    """api_router has no router-level dependency: IdentityMiddleware
    authenticates every request, and role gates sit on the mutating
    routes themselves (see tests/test_route_role_inventory.py)."""
    assert api_router.dependencies == []


# ── background-task strong refs ─────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_background_holds_strong_ref() -> None:
    """A spawned task must be in _BACKGROUND_TASKS while running and
    automatically removed when complete — proves the GC-prevention
    contract holds."""
    import asyncio

    started = asyncio.Event()
    finished = asyncio.Event()

    async def coro() -> None:
        started.set()
        await asyncio.sleep(0.01)
        finished.set()

    task = _spawn_background(coro())
    await started.wait()
    assert task in _BACKGROUND_TASKS, "task should be in registry while running"
    await finished.wait()
    # Yield once for the done callback to fire.
    await asyncio.sleep(0)
    assert task not in _BACKGROUND_TASKS, "task should be removed after completion"


# ── h2 demotion in user content ─────────────────────────────────


def test_demote_h2_at_line_start() -> None:
    src = "## Header injection\nbody\n## More"
    out = _demote_h2_lines(src)
    assert "### Header injection" in out
    assert "### More" in out
    # Original h2 lines must be gone
    assert "\n## " not in "\n" + out


def test_demote_preserves_h3() -> None:
    src = "### Already h3\n## becomes h3"
    out = _demote_h2_lines(src)
    assert "### Already h3" in out
    assert "### becomes h3" in out


def test_demote_preserves_inline_double_hash() -> None:
    """A `##` in the middle of a line (e.g., a comment in code) is
    not a heading — leave it alone."""
    src = "regular text with ## in the middle"
    assert _demote_h2_lines(src) == src


def test_normalise_question_collapses_newlines() -> None:
    out = _normalise_question("Why does\nthis happen?\nWith embedded ## inject")
    assert "\n" not in out
    # The `##` is no longer at line-start after collapse, so it's
    # textually neutralised even before _demote_h2_lines runs.
    assert "## inject" in out  # appears as text but not as a heading


def test_append_qa_strips_h2_in_answer() -> None:
    body = "## Common questions\n\n### Q: existing\n\nanswer\n"
    out = _append_qa_entry(
        body,
        question="What about X?",
        answer_md="The answer.\n## Sneaky h2\nMore",
    )
    # The injected h2 must be demoted to h3 — check at line-start
    # specifically, since `### Sneaky h2` would substring-match a
    # naïve `## Sneaky h2` check.
    assert "\n## Sneaky h2" not in out
    assert "\n### Sneaky h2" in out


# ── Pydantic max_length ─────────────────────────────────────────


def test_patch_body_rejects_oversize() -> None:
    PatchHandoverBodyRequest(body_md="x" * 200_000)  # exactly at cap
    with pytest.raises(pydantic.ValidationError):
        PatchHandoverBodyRequest(body_md="x" * 200_001)


def test_promote_question_capped() -> None:
    PromoteCandidateRequest(question="x" * 4_000, answer_md="ok")
    with pytest.raises(pydantic.ValidationError):
        PromoteCandidateRequest(question="x" * 4_001, answer_md="ok")


def test_promote_answer_capped() -> None:
    PromoteCandidateRequest(question="q", answer_md="x" * 50_000)
    with pytest.raises(pydantic.ValidationError):
        PromoteCandidateRequest(question="q", answer_md="x" * 50_001)


def test_collab_message_capped() -> None:
    AppendCollabMessageRequest(content_md="x" * 50_000)
    with pytest.raises(pydantic.ValidationError):
        AppendCollabMessageRequest(content_md="x" * 50_001)


def test_collab_session_requirement_capped() -> None:
    CreateCollabSessionRequest(requirement_text="x" * 20_000)
    with pytest.raises(pydantic.ValidationError):
        CreateCollabSessionRequest(requirement_text="x" * 20_001)

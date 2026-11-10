"""Unit tests for the Bifrost-task-state → Jira-workflow-state map."""

from __future__ import annotations

from bifrost.actions.jira_state_map import (
    TASK_STATE_TO_JIRA,
    WORKFLOW_OVERRIDES,
    JiraTargetState,
    jira_state_for,
)
from bifrost.actions.task_lifecycle import TaskState


def test_canonical_mapping_covers_lifecycle_states() -> None:
    """Every TaskState has an entry in the map (None or a target)."""
    for state in TaskState:
        assert state in TASK_STATE_TO_JIRA, f"{state} missing"


def test_local_only_states_have_no_jira_action() -> None:
    """Pre-plan states should not move Jira."""
    assert jira_state_for(TaskState.DRAFT_INTENT) is None
    assert jira_state_for(TaskState.DISCUSSING) is None
    assert jira_state_for(TaskState.DRAFTING_PLAN) is None


def test_executing_maps_to_in_progress() -> None:
    assert jira_state_for(TaskState.EXECUTING) == "In Progress"


def test_success_maps_to_in_review() -> None:
    assert jira_state_for(TaskState.SUCCESS) == "In Review"


def test_pr_merged_maps_to_done() -> None:
    assert jira_state_for(TaskState.PR_MERGED) == "Done"


def test_cancelled_maps_to_wont_do() -> None:
    assert jira_state_for(TaskState.CANCELLED) == "Won't Do"


def test_blocked_does_not_move_state() -> None:
    """Blocked is a comment-only event; keep current Jira state."""
    assert jira_state_for(TaskState.BLOCKED) is None


def test_per_project_override_applies(monkeypatch) -> None:
    """When a project has an override, the canonical name is replaced."""
    monkeypatch.setitem(
        WORKFLOW_OVERRIDES,
        "OPS",
        {JiraTargetState.IN_PROGRESS: "Doing"},
    )
    try:
        assert jira_state_for(TaskState.EXECUTING, project_key="OPS") == "Doing"
        # Default (no override) still returns canonical
        assert jira_state_for(TaskState.EXECUTING, project_key="DLVR") == "In Progress"
    finally:
        WORKFLOW_OVERRIDES.pop("OPS", None)


def test_unknown_project_falls_back_to_canonical() -> None:
    """An override for project X doesn't affect project Y."""
    assert jira_state_for(TaskState.SUCCESS, project_key="UNKNOWN") == "In Review"

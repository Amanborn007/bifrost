"""Map Bifrost task states to Jira workflow state names.

Different Jira projects have different workflows:

  * SHIP:  "To Do" → "In Progress" → "In Review" → "Done"
  * INV:   "Backlog" → "Started" → "Code Review" → "Closed"
  * OPS:   "New" → "Doing" → "Reviewing" → "Done"

Hardcoding "In Progress" everywhere fails on projects with a custom workflow, such as INV or OPS. So this module
publishes a CANONICAL set of Bifrost-task-states (5 of them) and a
configurable per-project override map. When a project doesn't have
an explicit override, we use the default (most common) names; the
``transition_issue`` MCP wrapper then does fuzzy matching over the
project's actual transitions list.

Canonical Bifrost states that drive Jira moves (subset of TaskState):

  TODO         → ticket exists, not yet worked
  IN_PROGRESS  → executing
  IN_REVIEW    → PR opened
  DONE         → PR merged
  WONT_DO      → cancelled

Rolling out per-project overrides happens via configuration, not code
change — the override map is read from the ``bifrost_jira_workflow``
table when present (added in a future migration). For now the map is
defined inline; the TODO at the bottom flags the path forward.
"""

from __future__ import annotations

from enum import StrEnum

from bifrost.actions.task_lifecycle import TaskState


class JiraTargetState(StrEnum):
    """Canonical state names Bifrost requests against Jira. The
    ``jira_writer.transition_issue`` MCP wrapper does fuzzy match
    over the issue's actual transition list, so these values are
    the BEST-GUESS default names — workflow variance is tolerated.
    """

    TODO = "To Do"
    IN_PROGRESS = "In Progress"
    IN_REVIEW = "In Review"
    DONE = "Done"
    WONT_DO = "Won't Do"


# Bifrost-state → JiraTargetState mapping. Drives the lifecycle hook.
TASK_STATE_TO_JIRA: dict[TaskState, JiraTargetState | None] = {
    TaskState.DRAFT_INTENT: None,  # local-only
    TaskState.DISCUSSING: None,
    TaskState.DISCUSS_DONE: None,
    TaskState.DRAFTING_PLAN: None,
    TaskState.PLAN_READY: JiraTargetState.TODO,  # ticket born here
    TaskState.EXECUTING: JiraTargetState.IN_PROGRESS,
    TaskState.VERIFYING: None,  # stay In Progress
    TaskState.SUCCESS: JiraTargetState.IN_REVIEW,  # PR open
    TaskState.PR_MERGED: JiraTargetState.DONE,  # merged
    TaskState.BLOCKED: None,  # comment, don't move
    TaskState.CANCELLED: JiraTargetState.WONT_DO,
}


# Per-project workflow overrides. Empty by default — the MCP layer's
# fuzzy matcher handles most workflows automatically. Add entries
# here only when fuzzy match misbehaves on a specific project.
#
# Example:
#   "OPS": {JiraTargetState.IN_PROGRESS: "Doing"}
WORKFLOW_OVERRIDES: dict[str, dict[JiraTargetState, str]] = {}


def jira_state_for(
    task_state: TaskState,
    *,
    project_key: str = "",
) -> str | None:
    """Return the Jira workflow state name (or ``None`` for "no Jira
    move"). Honors per-project overrides; falls back to the canonical
    name."""
    canonical = TASK_STATE_TO_JIRA.get(task_state)
    if canonical is None:
        return None
    pk = (project_key or "").upper()
    overrides = WORKFLOW_OVERRIDES.get(pk, {})
    return overrides.get(canonical, canonical.value)

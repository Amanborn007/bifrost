"""Placeholder-team detection and convention-derived identifiers."""

from __future__ import annotations

import re
from collections.abc import Iterable

from bifrost.cli.config_sections import ConventionsConfig
from bifrost.manifest.model import ServiceManifest

_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    v.lower()
    for v in (
        "your-squad-here",
        "your_squad_here",
        "your-team-here",
        "your_team_here",
        "todo",
        "tbd",
        "changeme",
        "change-me",
        "change_me",
        "fixme",
        "fix-me",
        "n/a",
        "na",
        "none",
        "null",
        "xxx",
        "placeholder",
        "template",
        "example",
        "sample",
        "<squad>",
        "<team>",
        "{squad}",
        "{team}",
        "unknown",
    )
)

# Bracketed single tokens, "your-..." prefixes and fix-me/change-me/todo-style prefixes.
_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[\[\{<][^\]\}>]*[\]\}>]$"),
    re.compile(r"^your[-_ ]"),
    re.compile(r"^(fix[-_ ]?me|fixme)[-_ ]"),
    re.compile(r"^(change[-_ ]?me|changeme)[-_ ]"),
    re.compile(r"^(todo|tbd|xxx|placeholder)[-_ ]"),
)

_JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")
_CONFLUENCE_KEY = re.compile(r"^[A-Za-z0-9]{2,255}$")


def is_placeholder_team(value: str | None, extra: Iterable[str] = ()) -> bool:
    """True when ``value`` is a template/placeholder, not a real team."""
    if value is None:
        return True
    v = value.strip().lower()
    if not v:
        return True
    if v in _PLACEHOLDER_VALUES or v in {e.strip().lower() for e in extra}:
        return True
    return any(p.search(v) for p in _PLACEHOLDER_PATTERNS)


def _render(template: str, team: str) -> str | None:
    if not template:
        return None
    slug = re.sub(r"\s+", "-", team.strip())
    return template.replace("{TEAM}", slug.upper()).replace("{team}", slug.lower())


def apply_conventions(md: ServiceManifest, cfg: ConventionsConfig) -> ServiceManifest:
    """Fill jira/confluence/slack identifiers from the team where not already set."""
    if not md.team:
        return md
    if not md.jira_project_key:
        key = _render(cfg.jira_project_key, md.team)
        md.jira_project_key = key if key and _JIRA_KEY.match(key) else None
    if not md.confluence_space_key:
        space = _render(cfg.confluence_space_key, md.team)
        md.confluence_space_key = space if space and _CONFLUENCE_KEY.match(space) else None
    if not md.slack_channel:
        md.slack_channel = _render(cfg.slack_channel, md.team)
    return md

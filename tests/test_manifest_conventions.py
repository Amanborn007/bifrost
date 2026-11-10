"""Placeholder-team detection and convention-derived identifiers."""

from __future__ import annotations

import pytest

from bifrost.cli.config_sections import ConventionsConfig
from bifrost.manifest.conventions import apply_conventions, is_placeholder_team
from bifrost.manifest.model import ServiceManifest


@pytest.mark.parametrize(
    "value",
    [
        "your-squad-here",
        "your_team_here",
        "<squad>",
        "{team}",
        "[team]",
        "[fill-in-later]",
        "todo",
        "tbd",
        "changeme",
        "fix-me",
        "n/a",
        "none",
        "null",
        "xxx",
        "placeholder",
        "template",
        "example",
        "sample",
        "unknown",
        "YOUR-SQUAD-HERE",
        "TODO",
        "Tbd",
        "your-platform-team",
        "fix-me-squad",
        "change-me-later",
        "tbd-team",
        "<FILL_IN_PHASE_1>",
        "",
        "   ",
        None,
    ],
)
def test_catches_placeholders(value: str | None) -> None:
    assert is_placeholder_team(value) is True


@pytest.mark.parametrize(
    "value",
    ["orders-platform", "webhook-api", "DLVR", "Platform", "fnd", "ai", "dlvr"],
)
def test_does_not_catch_real_teams(value: str) -> None:
    assert is_placeholder_team(value) is False


def test_extra_placeholders_are_case_insensitive() -> None:
    assert is_placeholder_team("Legacy-Team", extra=["legacy-team"]) is True
    assert is_placeholder_team("legacy-team") is False
    assert is_placeholder_team("dlvr", extra=("legacy-team",)) is False


def test_templates_fill_from_team() -> None:
    md = ServiceManifest(team="dlvr")
    apply_conventions(md, ConventionsConfig())
    assert md.jira_project_key == "DLVR"
    assert md.confluence_space_key == "DLVR"
    assert md.slack_channel == "#dlvr"


def test_custom_templates() -> None:
    cfg = ConventionsConfig(
        jira_project_key="X{TEAM}", confluence_space_key="{team}wiki", slack_channel="#eng-{team}"
    )
    md = ServiceManifest(team="plat")
    apply_conventions(md, cfg)
    assert md.jira_project_key == "XPLAT"
    assert md.confluence_space_key == "platwiki"
    assert md.slack_channel == "#eng-plat"


def test_explicit_values_are_not_overwritten() -> None:
    md = ServiceManifest(team="dlvr", jira_project_key="SHIP", slack_channel="#custom")
    apply_conventions(md, ConventionsConfig())
    assert md.jira_project_key == "SHIP"
    assert md.slack_channel == "#custom"
    assert md.confluence_space_key == "DLVR"


def test_no_team_fills_nothing() -> None:
    md = ServiceManifest()
    apply_conventions(md, ConventionsConfig())
    assert md.jira_project_key is None
    assert md.confluence_space_key is None
    assert md.slack_channel is None


@pytest.mark.parametrize("team", ["orders-platform", "a", "Growth Engineering", "1abc", "x" * 20])
def test_invalid_jira_key_becomes_none(team: str) -> None:
    md = ServiceManifest(team=team)
    apply_conventions(md, ConventionsConfig())
    assert md.jira_project_key is None


def test_empty_template_skips_field() -> None:
    md = ServiceManifest(team="dlvr")
    apply_conventions(md, ConventionsConfig(jira_project_key="", slack_channel=""))
    assert md.jira_project_key is None
    assert md.slack_channel is None
    assert md.confluence_space_key == "DLVR"

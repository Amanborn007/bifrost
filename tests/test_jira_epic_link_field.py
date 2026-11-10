"""The Jira chain prompt asks for the standard ``parent`` field; a custom Epic Link
field is added only when ``sources.jira.epic_link_field`` is set."""

from __future__ import annotations

import pytest

from bifrost.cli.config import BifrostSettings
from bifrost.cli.config_sections import JiraSourceConfig
from bifrost.errors import ConfigError
from bifrost.research.coordinator import _jira_chain_system


def _settings(**jira: object) -> BifrostSettings:
    return BifrostSettings(sources={"jira": jira})


def test_default_asks_for_parent_and_no_custom_field() -> None:
    prompt = _jira_chain_system(_settings())
    assert '"parent"' in prompt
    assert "customfield_" not in prompt
    assert "{mcp:" not in prompt


def test_configured_field_is_added_to_fields_and_instructions() -> None:
    prompt = _jira_chain_system(_settings(epic_link_field="customfield_10008"))
    assert '"parent"' in prompt
    assert '"customfield_10008"' in prompt
    assert prompt.count("customfield_10008") == 2


def test_configured_field_leaves_the_rest_of_the_prompt_alone() -> None:
    base = _jira_chain_system(_settings())
    custom = _jira_chain_system(_settings(epic_link_field="customfield_10008"))
    assert custom != base
    assert custom.startswith(base.split("3. ")[0])


def test_default_is_none() -> None:
    assert JiraSourceConfig().epic_link_field is None


@pytest.mark.parametrize(
    "bad", ["10014", "customfield_", "customfield_abc", "epic", " customfield_1"]
)
def test_field_name_must_match_customfield_pattern(bad: str) -> None:
    with pytest.raises(ConfigError, match="customfield_"):
        JiraSourceConfig(epic_link_field=bad)


def test_env_name_is_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_SOURCES__JIRA__EPIC_LINK_FIELD", "customfield_10008")
    assert BifrostSettings().sources.jira.epic_link_field == "customfield_10008"

"""Jira URL matching follows ``sources.jira.base_url`` (plus Atlassian Cloud by default)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bifrost.cli.config_sections import JiraSourceConfig
from bifrost.research.jira_discovery import discover_jira_project_key

CLOUD = JiraSourceConfig()
SELF_HOSTED = JiraSourceConfig(base_url="https://jira.acme.com")
CONTEXT_PATH = JiraSourceConfig(base_url="https://acme.com/jira")


def _readme(tmp_path: Path, body: str, name: str = "repo") -> str:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text(body, encoding="utf-8")
    return str(repo)


def _refs(host_url: str, key: str = "ABC", n: int = 2) -> str:
    return "\n".join(f"See {host_url}/browse/{key}-{i}" for i in range(1, n + 1))


# ── jira_discovery ──────────────────────────────────────────────────────


def test_atlassian_cloud_matches_by_default(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://acme.atlassian.net"))
    key, counts = discover_jira_project_key(clone)
    assert key == "ABC"
    assert counts == {"ABC": 2}


def test_atlassian_cloud_matches_when_a_base_url_is_also_configured(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://acme.atlassian.net"))
    assert discover_jira_project_key(clone, jira=SELF_HOSTED)[0] == "ABC"


def test_self_hosted_is_ignored_by_default(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://jira.acme.com"))
    assert discover_jira_project_key(clone) == (None, {})


def test_self_hosted_matches_once_base_url_is_set(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://jira.acme.com"))
    key, counts = discover_jira_project_key(clone, jira=SELF_HOSTED)
    assert key == "ABC"
    assert counts == {"ABC": 2}


def test_self_hosted_does_not_match_another_host(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://jira.other.com"))
    assert discover_jira_project_key(clone, jira=SELF_HOSTED) == (None, {})


def test_dots_in_the_configured_host_are_literal(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://jiraXacmeXcom"))
    assert discover_jira_project_key(clone, jira=SELF_HOSTED) == (None, {})


def test_context_path_base_url_works(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://acme.com/jira"))
    assert discover_jira_project_key(clone, jira=CONTEXT_PATH)[0] == "ABC"


def test_context_path_base_url_ignores_other_paths_on_the_host(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://acme.com/wiki"))
    assert discover_jira_project_key(clone, jira=CONTEXT_PATH) == (None, {})


def test_noise_keys_are_ignored(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://acme.atlassian.net", key="XXX", n=4))
    assert discover_jira_project_key(clone)[0] is None


def test_a_single_reference_is_not_enough(tmp_path: Path) -> None:
    clone = _readme(tmp_path, _refs("https://acme.atlassian.net", n=1))
    key, counts = discover_jira_project_key(clone)
    assert key is None
    assert counts == {"ABC": 1}


def test_most_referenced_key_wins(tmp_path: Path) -> None:
    body = (
        _refs("https://acme.atlassian.net", key="ABC", n=2)
        + "\n"
        + _refs("https://acme.atlassian.net", key="DEF", n=3)
    )
    assert discover_jira_project_key(_readme(tmp_path, body))[0] == "DEF"


def test_two_configs_back_to_back_do_not_share_a_compiled_pattern(tmp_path: Path) -> None:
    cloud_repo = _readme(tmp_path, _refs("https://acme.atlassian.net"), name="cloud")
    hosted_repo = _readme(tmp_path, _refs("https://jira.acme.com"), name="hosted")

    assert discover_jira_project_key(hosted_repo, jira=SELF_HOSTED)[0] == "ABC"
    assert discover_jira_project_key(hosted_repo, jira=CLOUD) == (None, {})
    assert discover_jira_project_key(cloud_repo, jira=CLOUD)[0] == "ABC"
    assert discover_jira_project_key(cloud_repo, jira=SELF_HOSTED)[0] == "ABC"
    assert discover_jira_project_key(hosted_repo, jira=SELF_HOSTED)[0] == "ABC"


def test_unset_jira_argument_reads_the_settings_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bifrost.cli.config import reset_settings

    clone = _readme(tmp_path, _refs("https://jira.acme.com"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIFROST_CONFIG", "")
    monkeypatch.setenv("BIFROST_SOURCES__JIRA__BASE_URL", "https://jira.acme.com")
    reset_settings()
    try:
        assert discover_jira_project_key(clone)[0] == "ABC"
    finally:
        monkeypatch.undo()
        reset_settings()


# ── JiraSourceConfig.host_pattern ───────────────────────────────────────


def test_host_pattern_default_is_the_cloud_fragment_only() -> None:
    assert CLOUD.host_pattern() == r"(?:[\w-]+\.atlassian\.net)"


def test_host_pattern_escapes_and_includes_the_configured_host() -> None:
    pat = SELF_HOSTED.host_pattern()
    assert r"jira\.acme\.com" in pat
    assert r"[\w-]+\.atlassian\.net" in pat


def test_host_pattern_keeps_each_callers_default_fragment() -> None:
    assert SELF_HOSTED.host_pattern(r"[\w.-]+\.atlassian\.net").endswith(
        r"[\w.-]+\.atlassian\.net)"
    )


# ── the other three call sites keep their own defaults ──────────────────

LONG_PAD = "x" * 400  # bare-key fallbacks only run on short text


def _hosted_url(key: str = "AB1-12", base: str = "https://jira.acme.com") -> str:
    return f"{LONG_PAD} {base}/browse/{key} {LONG_PAD}"


class TestIntentRouter:
    def test_cloud_url_by_default(self) -> None:
        from bifrost.actions.intent_router import _extract_jira_key

        assert _extract_jira_key("see https://acme.atlassian.net/browse/AB1-12") == "AB1-12"

    def test_self_hosted_ignored_by_default(self) -> None:
        from bifrost.actions.intent_router import _extract_jira_key

        assert _extract_jira_key(_hosted_url()) is None

    def test_self_hosted_matched_with_base_url(self) -> None:
        from bifrost.actions.intent_router import _extract_jira_key

        assert _extract_jira_key(_hosted_url(), jira=SELF_HOSTED) == "AB1-12"

    def test_context_path_base(self) -> None:
        from bifrost.actions.intent_router import _extract_jira_key

        text = _hosted_url(base="https://acme.com/jira")
        assert _extract_jira_key(text, jira=CONTEXT_PATH) == "AB1-12"
        assert _extract_jira_key(text) is None

    def test_configs_do_not_leak_between_calls(self) -> None:
        from bifrost.actions.intent_router import _extract_jira_key

        assert _extract_jira_key(_hosted_url(), jira=SELF_HOSTED) == "AB1-12"
        assert _extract_jira_key(_hosted_url(), jira=CLOUD) is None


class TestJiraEnrich:
    def test_cloud_url_by_default(self) -> None:
        from bifrost.actions.jira_enrich import extract_related_jira_keys

        got = extract_related_jira_keys("dup of https://sub.domain.atlassian.net/browse/AB1-12")
        assert got == ["AB1-12"]

    def test_self_hosted_ignored_by_default(self) -> None:
        from bifrost.actions.jira_enrich import extract_related_jira_keys

        assert extract_related_jira_keys(_hosted_url()) == []

    def test_self_hosted_matched_with_base_url(self) -> None:
        from bifrost.actions.jira_enrich import extract_related_jira_keys

        assert extract_related_jira_keys(_hosted_url(), jira=SELF_HOSTED) == ["AB1-12"]

    def test_skip_still_applies(self) -> None:
        from bifrost.actions.jira_enrich import extract_related_jira_keys

        assert extract_related_jira_keys(_hosted_url(), skip="AB1-12", jira=SELF_HOSTED) == []

    def test_context_path_base(self) -> None:
        from bifrost.actions.jira_enrich import extract_related_jira_keys

        text = _hosted_url(base="https://acme.com/jira")
        assert extract_related_jira_keys(text, jira=CONTEXT_PATH) == ["AB1-12"]
        assert extract_related_jira_keys(text) == []


class TestCoordinator:
    def test_cloud_url_by_default(self) -> None:
        from bifrost.research.coordinator import _extract_jira_keys

        assert _extract_jira_keys("look at https://acme.atlassian.net/browse/ab1-12") == {"AB1-12"}

    def test_bare_keys_still_found(self) -> None:
        from bifrost.research.coordinator import _extract_jira_keys

        assert _extract_jira_keys("what about ABC-123?") == {"ABC-123"}

    def test_self_hosted_ignored_by_default(self) -> None:
        from bifrost.research.coordinator import _extract_jira_keys

        assert _extract_jira_keys("look at https://jira.acme.com/browse/AB1-12") == set()

    def test_self_hosted_matched_with_base_url(self) -> None:
        from bifrost.research.coordinator import _extract_jira_keys

        got = _extract_jira_keys("look at https://jira.acme.com/browse/AB1-12", jira=SELF_HOSTED)
        assert got == {"AB1-12"}

    def test_context_path_base(self) -> None:
        from bifrost.research.coordinator import _extract_jira_keys

        text = "look at https://acme.com/jira/browse/AB1-12"
        assert _extract_jira_keys(text, jira=CONTEXT_PATH) == {"AB1-12"}
        assert _extract_jira_keys(text) == set()

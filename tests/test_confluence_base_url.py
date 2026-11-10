"""Confluence links match Atlassian Cloud plus ``sources.confluence.base_url``."""

from __future__ import annotations

import pytest

from bifrost.actions.jira_enrich import extract_confluence_links
from bifrost.cli.config import BifrostSettings
from bifrost.cli.config_sections import ConfluenceSourceConfig
from bifrost.errors import ConfigError

SELF_HOSTED = ConfluenceSourceConfig(base_url="https://wiki.acme.com")
PAGE = "https://wiki.acme.com/display/ENG/Design"
CLOUD_PAGE = "https://acmeco.atlassian.net/wiki/spaces/ENG/pages/1/a"


def test_default_is_none() -> None:
    assert ConfluenceSourceConfig().base_url is None


def test_self_hosted_ignored_without_base_url() -> None:
    assert extract_confluence_links(f"See {PAGE}", confluence=ConfluenceSourceConfig()) == []


def test_self_hosted_matched_with_base_url() -> None:
    assert extract_confluence_links(f"See {PAGE} now", confluence=SELF_HOSTED) == [PAGE]


def test_cloud_still_matches_when_base_url_is_set() -> None:
    desc = f"{CLOUD_PAGE} and {PAGE}"
    assert extract_confluence_links(desc, confluence=SELF_HOSTED) == [CLOUD_PAGE, PAGE]


def test_context_path_base_url_and_trailing_slash() -> None:
    cfg = ConfluenceSourceConfig(base_url="https://acme.com/confluence/")
    url = "https://acme.com/confluence/pages/viewpage.action?pageId=42"
    assert extract_confluence_links(f"x {url}", confluence=cfg) == [url]


def test_base_url_is_regex_escaped() -> None:
    cfg = ConfluenceSourceConfig(base_url="https://wiki.acme.com")
    assert extract_confluence_links("https://wikiXacme.com/display/A/B", confluence=cfg) == []


def test_other_host_with_same_prefix_is_not_matched() -> None:
    assert extract_confluence_links("https://wiki.acme.com.evil.io/x", confluence=SELF_HOSTED) == []


def test_default_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    s = BifrostSettings(sources={"confluence": {"base_url": "https://wiki.acme.com"}})
    monkeypatch.setattr("bifrost.actions.jira_enrich.get_settings", lambda: s)
    assert extract_confluence_links(f"See {PAGE}") == [PAGE]


def test_section_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        ConfluenceSourceConfig(host="x")  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", ["", "wiki.acme.com", "ftp://wiki.acme.com", "https://"])
def test_base_url_needs_an_http_scheme_and_host(bad: str) -> None:
    with pytest.raises(ConfigError, match="sources.confluence.base_url"):
        ConfluenceSourceConfig(base_url=bad)

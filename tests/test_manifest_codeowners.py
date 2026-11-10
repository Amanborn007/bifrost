"""CODEOWNERS provider."""

from __future__ import annotations

from bifrost.cli.config_sections import CodeownersConfig
from bifrost.manifest.files import InMemoryRepoFiles
from bifrost.manifest.providers.codeowners import CodeownersProvider, parse_codeowners


def test_team_is_first_owner_of_last_star_rule() -> None:
    text = "* @acme/first\n/docs/ @acme/docs\n* @acme/second @acme/third\n"
    md = parse_codeowners(text, strip_org_prefix=True)
    assert md is not None
    assert md.team == "second"
    assert md.owners == ["second", "third"]


def test_only_star_rule_counts_not_path_rules() -> None:
    text = "* @acmeco/dlvr\n/config/ @acmeco/dlvr @acmeco/plat\n"
    md = parse_codeowners(text, strip_org_prefix=True)
    assert md is not None
    assert md.team == "dlvr"


def test_strip_org_prefix_false_keeps_org() -> None:
    md = parse_codeowners("* @acmeco/dlvr\n", strip_org_prefix=False)
    assert md is not None
    assert md.team == "acmeco/dlvr"


def test_comments_ignored() -> None:
    text = "# * @acme/commented\n* @acme/real # trailing note\n"
    md = parse_codeowners(text, strip_org_prefix=True)
    assert md is not None
    assert md.team == "real"
    assert md.owners == ["real"]


def test_email_owners_ignored() -> None:
    md = parse_codeowners("* dev@acme.com @acme/real\n", strip_org_prefix=True)
    assert md is not None
    assert md.team == "real"
    assert md.owners == ["real"]


def test_only_email_owners_gives_no_team() -> None:
    md = parse_codeowners("* dev@acme.com\n", strip_org_prefix=True)
    assert md is None or md.team is None


def test_user_handle_without_org() -> None:
    md = parse_codeowners("* @jdoe\n", strip_org_prefix=True)
    assert md is not None
    assert md.team == "jdoe"


def test_no_star_rule_returns_none() -> None:
    assert parse_codeowners("/config/ @acme/plat\n", strip_org_prefix=True) is None


def test_empty_returns_none() -> None:
    assert parse_codeowners("", strip_org_prefix=True) is None
    assert parse_codeowners("# only a comment\n", strip_org_prefix=True) is None


def test_crlf_and_tabs() -> None:
    md = parse_codeowners("*\t@acme/dlvr\r\n", strip_org_prefix=True)
    assert md is not None
    assert md.team == "dlvr"


async def test_provider_probes_configured_paths_in_order() -> None:
    files = InMemoryRepoFiles(
        {".github/CODEOWNERS": "* @acme/gh\n", "docs/CODEOWNERS": "* @acme/docs\n"}
    )
    md = await CodeownersProvider(CodeownersConfig()).load(files, repo_name="svc")
    assert md is not None
    assert md.team == "gh"
    assert md.source_files == [".github/CODEOWNERS"]
    assert md.providers == ["codeowners"]


async def test_provider_none_without_file() -> None:
    assert (
        await CodeownersProvider(CodeownersConfig()).load(InMemoryRepoFiles({}), repo_name="s")
        is None
    )

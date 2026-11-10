"""Repo-hint patterns come from linking.internal_package_prefixes, not hardcoded constants."""

from __future__ import annotations

import pytest

from bifrost.research.cross_repo_linker import _dep_target_repo_hint, _repo_hint_patterns
from tests import leak_terms

ACME = ["com.acmeco.", "@acmeco/", "github.com/acmeco/"]


@pytest.mark.parametrize(
    ("dep", "expected"),
    [
        ("com.acmeco.dispatcher.client.Gateway", "dispatcher"),
        ("com.acmeco.delivery-gateway.Retry", "deliverygateway"),
        ("@acmeco/signing-keys/sdk", "signingkeys"),
        ("github.com/acmeco/Usage-Metering/pkg/roll", "usagemetering"),
    ],
)
def test_acme_prefixes_yield_hint(dep: str, expected: str) -> None:
    assert _dep_target_repo_hint(dep, _repo_hint_patterns(ACME)) == expected


def test_unrelated_dep_gets_no_hint() -> None:
    assert _dep_target_repo_hint("org.other.thing.Klass", _repo_hint_patterns(ACME)) is None


def test_empty_prefixes_give_no_patterns_and_no_hint() -> None:
    assert _repo_hint_patterns([]) == ()
    assert _dep_target_repo_hint("com.othervendor.x.Y", _repo_hint_patterns([])) is None


def test_no_banned_prefix_is_hardcoded() -> None:
    for term in leak_terms.terms():
        for prefixes in ([], ACME):
            assert _dep_target_repo_hint(f"com.{term}.x.Y", _repo_hint_patterns(prefixes)) is None


def test_prefix_is_escaped() -> None:
    patterns = _repo_hint_patterns(["com.a.b."])
    assert _dep_target_repo_hint("comXaXb.svc.Y", patterns) is None
    assert _dep_target_repo_hint("com.a.b.svc.Y", patterns) == "svc"


def test_default_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost.cli import config as cfg_mod

    cfg_mod.reset_settings()
    monkeypatch.setattr(
        cfg_mod,
        "_settings",
        cfg_mod.BifrostSettings(linking={"internal_package_prefixes": ["com.acmeco."]}),
    )
    try:
        assert _dep_target_repo_hint("com.acmeco.dispatcher.X") == "dispatcher"
    finally:
        cfg_mod.reset_settings()

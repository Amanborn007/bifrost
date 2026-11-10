"""Provider-set validation for the identity and manifest config sections."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bifrost.cli.config_sections import IdentityConfig, ManifestConfig


def test_identity_defaults_to_none() -> None:
    assert IdentityConfig().provider == "none"


@pytest.mark.parametrize("provider", ["none", "header", "oidc_jwt"])
def test_identity_accepts_known_providers(provider: str) -> None:
    assert IdentityConfig(provider=provider).provider == provider


def test_identity_rejects_unknown_provider() -> None:
    with pytest.raises(ValidationError):
        IdentityConfig(provider="vendor_sso")


def test_manifest_accepts_none_alone() -> None:
    assert ManifestConfig(providers=["none"]).providers == ["none"]


def test_manifest_rejects_none_mixed_with_other_providers() -> None:
    with pytest.raises(ValidationError, match="none"):
        ManifestConfig(providers=["none", "backstage"])

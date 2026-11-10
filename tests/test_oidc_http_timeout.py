"""The OIDC JWKS fetch honours the one outbound ``http_timeout_seconds`` setting."""

from __future__ import annotations

from typing import Any

import pytest

from bifrost.api.auth.providers import OidcJwtProvider, build_identity_provider
from bifrost.cli.config import BifrostSettings, reset_settings

OIDC = {
    "jwks_url": "https://idp.example.com/.well-known/jwks.json",
    "issuer": "https://idp.example.com/",
    "audience": "bifrost",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    for key in ("BIFROST_DEMO_MODE", "BIFROST_HTTP_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    reset_settings()
    yield
    reset_settings()


@pytest.mark.parametrize("seconds", [12.5, 1.0])
def test_oidc_verifier_receives_configured_timeout(seconds: float) -> None:
    settings = BifrostSettings(
        _env_file=None,  # type: ignore[call-arg]
        http_timeout_seconds=seconds,
        identity={"provider": "oidc_jwt", "oidc": OIDC, "roles": {"admin": []}},
    )
    provider = build_identity_provider(settings)
    assert isinstance(provider, OidcJwtProvider)
    assert provider._verifier.http_timeout_seconds == seconds

"""Claim-to-principal mapping for the oidc_jwt provider. The verifier is
stubbed: signature/issuer/audience checks are covered in
test_identity_providers.py and test_jwt_verifier.py."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request

from bifrost.api.auth.principal import SOURCE_OIDC_JWT
from bifrost.api.auth.providers import InvalidCredentials, OidcJwtProvider


class _StubVerifier:
    def __init__(self, claims: dict[str, Any]) -> None:
        self._claims = claims

    def verify_and_decode(self, _token: str) -> dict[str, Any]:
        return self._claims


def _provider(claims: dict[str, Any], groups_claim: str = "groups") -> OidcJwtProvider:
    return OidcJwtProvider(verifier=_StubVerifier(claims), groups_claim=groups_claim)  # type: ignore[arg-type]


def _request() -> Request:
    headers = [(b"authorization", b"Bearer tok")]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


async def test_email_uses_sub_when_email_missing() -> None:
    principal = await _provider({"sub": "a@example.com", "groups": ["squad-a"]}).resolve(_request())
    assert principal is not None
    assert principal.email == "a@example.com"
    assert "squad-a" in principal.groups


async def test_uuid_sub_without_email_is_rejected() -> None:
    with pytest.raises(InvalidCredentials):
        await _provider({"sub": "uuid-style-sub", "groups": []}).resolve(_request())


async def test_email_preferred_over_preferred_username() -> None:
    claims = {"email": "primary@example.com", "preferred_username": "other@example.com"}
    principal = await _provider(claims).resolve(_request())
    assert principal is not None
    assert principal.email == "primary@example.com"


async def test_preferred_username_used_when_email_missing() -> None:
    principal = await _provider({"preferred_username": "pu@example.com"}).resolve(_request())
    assert principal is not None
    assert principal.email == "pu@example.com"


async def test_groups_accept_comma_string_and_dedupe() -> None:
    principal = await _provider({"email": "a@example.com", "groups": "eng, ops,eng"}).resolve(
        _request()
    )
    assert principal is not None
    assert principal.groups == ("eng", "ops")


async def test_groups_claim_name_is_configurable() -> None:
    claims = {"email": "a@example.com", "roles": ["admin"], "groups": ["ignored"]}
    principal = await _provider(claims, groups_claim="roles").resolve(_request())
    assert principal is not None
    assert principal.groups == ("admin",)


async def test_expiry_is_carried_onto_the_principal() -> None:
    principal = await _provider({"email": "a@example.com", "exp": 4102444800}).resolve(_request())
    assert principal is not None
    assert principal.source == SOURCE_OIDC_JWT
    assert principal.expires_at is not None
    assert principal.expires_at.year == 2100

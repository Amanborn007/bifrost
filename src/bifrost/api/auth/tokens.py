"""``bft_`` API tokens: a principal source for non-browser clients.

Only the sha256 of each token is configured (``api_tokens`` in
bifrost.yaml); several can be active at once, which is how rotation works.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Sequence

from bifrost.api.auth.principal import SOURCE_API_TOKEN, Principal
from bifrost.api.auth.roles import canonical_roles
from bifrost.cli.config_sections import ApiTokenConfig

TOKEN_PREFIX = "bft_"


def generate_token() -> tuple[str, str]:
    """A new token and its sha256 hex digest. The token is shown once."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, digest_of(token)


def digest_of(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ApiTokenAuthenticator:
    def __init__(self, tokens: Sequence[ApiTokenConfig]) -> None:
        self._tokens = tuple(tokens)

    def authenticate(self, presented: str) -> Principal | None:
        digest = digest_of(presented)
        matched: ApiTokenConfig | None = None
        # Compare against every digest so timing does not reveal which
        # configured token (if any) matched.
        for token in self._tokens:
            if hmac.compare_digest(digest, token.sha256):
                matched = token
        if matched is None:
            return None
        return Principal(
            email=f"token:{matched.name}",
            source=SOURCE_API_TOKEN,
            roles=canonical_roles(matched.roles),
        )

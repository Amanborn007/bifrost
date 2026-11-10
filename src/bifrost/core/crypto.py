"""Per-key Fernet encryption for credentials stored inside a JSONB column.

Convention: a top-level key starting with ``_secret.`` holds a credential. On
write its string value is encrypted and stored as ``enc:v1:<fernet token>``;
on read it is decrypted. Every other key is stored as-is, so a column keeps
its normal JSON shape and the alembic baseline stays JSONB.

``BIFROST_CREDENTIAL_KEY`` holds one Fernet key, or a comma-separated list
for rotation: the first key encrypts, every key can decrypt.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

RESERVED_PREFIX = "_secret."
CIPHERTEXT_PREFIX = "enc:v1:"
REDACTED = "***"

_MISSING_KEY = (
    "BIFROST_CREDENTIAL_KEY is not set, so credentials cannot be encrypted or decrypted. "
    'Generate one with: python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)

# "pat" only counts as a whole word, otherwise it would flag path, patch and pattern.
_TOKEN_SHAPED = re.compile(r"token|secret|password|passwd|credential|api_?key", re.IGNORECASE)
_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


class CredentialKeyError(ValueError):
    """The credential key is missing or cannot decrypt a stored value."""


def is_secret_key(key: str) -> bool:
    return key.startswith(RESERVED_PREFIX)


def is_token_shaped(key: str) -> bool:
    """True for names like ``github_token`` or ``apiKey`` that suggest a credential."""
    if _TOKEN_SHAPED.search(key.replace("-", "_")):
        return True
    return any(w.lower() == "pat" for w in _WORD_SPLIT.split(key))


def misplaced_secret_keys(config: dict[str, Any]) -> list[str]:
    """Token-shaped keys that sit outside the encrypted namespace."""
    return sorted(k for k in config if not is_secret_key(k) and is_token_shaped(k))


def redact_secrets(config: dict[str, Any] | None) -> dict[str, Any]:
    """Copy of ``config`` with every reserved value replaced by ``***``."""
    return {k: (REDACTED if is_secret_key(k) else v) for k, v in (config or {}).items()}


@lru_cache(maxsize=8)
def _fernet(keys: str) -> MultiFernet:
    try:
        return MultiFernet([Fernet(k.strip()) for k in keys.split(",") if k.strip()])
    except (ValueError, TypeError) as exc:
        raise CredentialKeyError(
            f"BIFROST_CREDENTIAL_KEY is not a valid Fernet key list: {exc}"
        ) from exc


def _settings_keys() -> str | None:
    from bifrost.cli.config import get_settings

    return get_settings().credential_key


class SecretJSON(TypeDecorator[dict[str, Any]]):
    """JSONB that encrypts the values of ``_secret.*`` top-level keys."""

    impl = JSONB
    cache_ok = True

    def __init__(self, keys: Callable[[], str | None] | None = None) -> None:
        super().__init__()
        self._keys = keys or _settings_keys

    def _cipher(self) -> MultiFernet:
        keys = self._keys()
        if not keys or not keys.strip():
            raise CredentialKeyError(_MISSING_KEY)
        return _fernet(keys)

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if not isinstance(value, dict):
            return value
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not (isinstance(key, str) and is_secret_key(key)):
                out[key] = item
                continue
            if not isinstance(item, str):
                raise TypeError(f"{key} must be a string, got {type(item).__name__}")
            out[key] = CIPHERTEXT_PREFIX + self._cipher().encrypt(item.encode()).decode()
        return out

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if not isinstance(value, dict):
            return value
        out: dict[str, Any] = {}
        for key, item in value.items():
            if (
                isinstance(key, str)
                and is_secret_key(key)
                and isinstance(item, str)
                and item.startswith(CIPHERTEXT_PREFIX)
            ):
                try:
                    out[key] = (
                        self._cipher().decrypt(item[len(CIPHERTEXT_PREFIX) :].encode()).decode()
                    )
                except InvalidToken as exc:
                    raise CredentialKeyError(
                        f"{key} was not encrypted with any key in BIFROST_CREDENTIAL_KEY"
                    ) from exc
            else:
                out[key] = item
        return out

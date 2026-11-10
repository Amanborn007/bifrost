"""HMAC-signed session cookies.

The ``bifrost_session`` cookie is the rate-limit bucket key
(``handover_docs.py:127-129``) and the audit ``actor`` field
(``handover_audits.actor``). Before this module landed, the cookie
was a plain ``uuid4().hex`` that the server accepted at face value:
a client could send any string and have it recorded as the actor or
land in any rate bucket.

Wire format
-----------

A signed cookie looks like::

    <session_id>.<sig>

where:

  - ``session_id`` is a ``uuid4().hex`` (32 lowercase hex chars).
  - ``sig`` is the urlsafe-base64 SHA-256 HMAC of ``session_id``
    under the server's signing key, with trailing ``=`` padding
    stripped.

On read, ``parse_signed_cookie`` recomputes the HMAC over the prefix
and constant-time-compares against the suffix. Tampered, stale-key or
unsigned values return ``None`` and the route mints a fresh cookie via
``mint_signed_cookie``.

Key management
--------------

The signing key comes from ``BIFROST_SESSION_SIGNING_KEY``. If
unset, ``_get_signing_key`` lazily generates a random ephemeral key
at first use (``secrets.token_bytes(32)``). Ephemeral keys mean
cookies survive only as long as the process — this is acceptable
for local dev where there's a single uvicorn worker. Production
deployments MUST set the env var so cookies survive restarts and
are stable across multiple workers.

The key is cached process-locally in ``_KEY_CACHE`` to avoid
re-deriving it on every request. ``reset_signing_key()`` is exposed
for tests so they can deterministically rotate keys.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Final

from bifrost.cli.config import get_settings

logger = logging.getLogger(__name__)


# Separator between the session id and the signature in the cookie
# value. We picked ``.`` (not URL-encoded by browsers) so the cookie
# stays printable.
_COOKIE_SEPARATOR: Final[str] = "."


# Process-local cache for the signing key. ``_KEY_CACHE[0]`` is the
# resolved key bytes; a sentinel ``None`` means "not yet resolved".
# A list is used so ``reset_signing_key`` can rebind the slot
# without rebinding a module-level name.
_KEY_CACHE: list[bytes | None] = [None]


def _key_file_path() -> Path:
    """Where we persist the auto-generated signing key.

    Default: ``~/.bifrost/session-key``. Override via
    ``BIFROST_SESSION_KEY_FILE`` env (mostly for tests). The file is
    created with mode 0600 to avoid group-readable secrets.
    """
    override = os.environ.get("BIFROST_SESSION_KEY_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bifrost" / "session-key"


def _load_or_create_persistent_key() -> bytes:
    """Read the signing key from disk, generating + persisting it
    on first run.

    An ephemeral per-process key would be minted again on every
    ``--reload`` or crash-and-relaunch, invalidating every signed
    cookie in flight and orphaning the conversations they owned.

    Persisting the key to disk gives signed cookies a stable
    verification surface across restarts. The file is mode 0600.
    """
    path = _key_file_path()
    try:
        if path.exists():
            data = path.read_bytes().strip()
            # base64-decode if the file contains the urlsafe form
            # we wrote ourselves; otherwise treat as raw bytes for
            # operator-supplied keys.
            try:
                key_bytes = base64.urlsafe_b64decode(data)
            except Exception:
                key_bytes = data
            if len(key_bytes) >= 16:
                return key_bytes
    except OSError as exc:
        logger.warning(
            "bifrost.session_key_file_read_failed path=%s err=%s",
            path,
            exc,
        )

    # File missing or unusable — generate + persist.
    key_bytes = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(key_bytes)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        try:
            path.chmod(0o600)
        except OSError:
            pass  # filesystem may not support chmod (e.g., FAT)
        logger.info(
            "bifrost.session_key_generated path=%s",
            path,
        )
    except OSError as exc:
        logger.warning(
            "bifrost.session_key_file_write_failed path=%s err=%s; "
            "falling back to in-process key (cookies WILL be invalidated "
            "on next restart)",
            path,
            exc,
        )
    return key_bytes


def _get_signing_key() -> bytes:
    """Return the cached signing key, resolving it on first call.

    Order of resolution:
      1. ``BIFROST_SESSION_SIGNING_KEY`` env (via settings) — primary
         path for production / multi-worker deployments where the key
         must be sourced from a secret manager.
      2. Persistent key file at ``~/.bifrost/session-key`` (or
         ``BIFROST_SESSION_KEY_FILE`` override) — auto-generated on
         first run, survives restarts. This is the dev-friendly path
         that fixes the 40-orphaned-sessions bug.
    """
    if _KEY_CACHE[0] is not None:
        return _KEY_CACHE[0]

    configured = (get_settings().session_signing_key or "").strip()
    key_bytes = configured.encode("utf-8") if configured else _load_or_create_persistent_key()
    _KEY_CACHE[0] = key_bytes
    return key_bytes


def reset_signing_key() -> None:
    """Reset the cached signing key (test hook).

    Subsequent calls to ``_get_signing_key`` will re-resolve from
    env. Useful when a test mutates ``BIFROST_SESSION_SIGNING_KEY``
    via ``reset_settings``.
    """
    _KEY_CACHE[0] = None


def _sign(session_id: str) -> str:
    """Compute the urlsafe-base64 HMAC-SHA256 of ``session_id``."""
    key = _get_signing_key()
    digest = hmac.new(key, session_id.encode("utf-8"), hashlib.sha256).digest()
    # Strip ``=`` padding so the cookie value stays compact and
    # within the safe-char set browsers accept without quoting.
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def mint_signed_cookie() -> str:
    """Generate a fresh ``<session_id>.<sig>`` value."""
    session_id = uuid.uuid4().hex
    return f"{session_id}{_COOKIE_SEPARATOR}{_sign(session_id)}"


def sign_session_cookie(session_id: str) -> str:
    """Signed cookie value for an existing session id.

    For trusted in-process clients (the benchmark runner) that act as a
    known session; never fed from request input.
    """
    return f"{session_id}{_COOKIE_SEPARATOR}{_sign(session_id)}"


def parse_signed_cookie(value: str | None) -> str | None:
    """Return the session id from a signed cookie, or ``None``.

    ``None`` means the caller must mint a fresh session: the value is
    empty, a bare id with no signature, malformed, or carries a signature
    that does not validate under the current key. A forged id is never
    accepted, so rotating the signing key starts every client a new session.
    """
    if not value:
        return None
    session_id, sep, sig = value.partition(_COOKIE_SEPARATOR)
    if not sep or not sig:
        return None
    if len(session_id) != 32 or any(c not in "0123456789abcdef" for c in session_id):
        return None
    if not hmac.compare_digest(_sign(session_id).encode(), sig.encode()):
        return None
    return session_id

"""Cookie signing tests.

The ``bifrost_session`` cookie is the rate-limit bucket key and the
audit ``actor``. These tests pin the contract that:

  - Minted cookies round-trip through ``parse_signed_cookie``.
  - Tampered cookies are rejected.
  - Unsigned legacy values are rejected (no shared-prefix collision
    against the 32-hex session-id format).
  - Bad-signature and stale-key cookies are rejected, never recovered.
  - Cookies signed under one key don't validate under another.
"""

from __future__ import annotations

import pytest

from bifrost.api import _cookie_signing as cs
from bifrost.cli.config import reset_settings


@pytest.fixture(autouse=True)
def _reset_signing() -> None:
    """Force re-resolution of the signing key for each test."""
    reset_settings()
    cs.reset_signing_key()
    yield
    reset_settings()
    cs.reset_signing_key()


def test_round_trip_with_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_SESSION_SIGNING_KEY", "test-key-deterministic")
    reset_settings()
    cs.reset_signing_key()

    minted = cs.mint_signed_cookie()
    assert "." in minted
    sid = cs.parse_signed_cookie(minted)
    assert sid is not None
    assert len(sid) == 32
    assert all(c in "0123456789abcdef" for c in sid)


def test_tampered_signature_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signed cookie whose signature does not validate is not a
    session: the route mints a fresh one rather than trusting the id."""
    monkeypatch.setenv("BIFROST_SESSION_SIGNING_KEY", "k1")
    reset_settings()
    cs.reset_signing_key()

    minted = cs.mint_signed_cookie()
    sid_part, sig_part = minted.split(".", 1)
    flipped = sig_part[:-1] + ("a" if sig_part[-1] != "a" else "b")
    assert cs.parse_signed_cookie(f"{sid_part}.{flipped}") is None


def test_tampered_session_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cookie whose ID half isn't 32-hex shape is rejected
    (recovery only kicks in when the ID is shape-correct)."""
    monkeypatch.setenv("BIFROST_SESSION_SIGNING_KEY", "k1")
    reset_settings()
    cs.reset_signing_key()

    minted = cs.mint_signed_cookie()
    sid_part, sig_part = minted.split(".", 1)
    # Replace one hex char with a non-hex char so the shape check rejects it.
    flipped_sid = "z" + sid_part[1:]
    tampered = f"{flipped_sid}.{sig_part}"
    assert cs.parse_signed_cookie(tampered) is None


def test_unsigned_garbage_cookie_rejected() -> None:
    # Anything that's not 32-char hex AND not signed must be rejected.
    # (A bare 32-char hex is rejected too; see test_session_hardening.py.)
    assert cs.parse_signed_cookie("abc123def456") is None
    assert cs.parse_signed_cookie("g" * 32) is None  # non-hex
    assert cs.parse_signed_cookie("a" * 31) is None  # too short
    assert cs.parse_signed_cookie("a" * 33) is None  # too long


def test_unsigned_legacy_uuid_hex_rejected() -> None:
    # A bare 32-char hex is forgeable by any client, so it is never a session.
    assert cs.parse_signed_cookie("0123456789abcdef0123456789abcdef") is None


def test_empty_or_none_rejected() -> None:
    assert cs.parse_signed_cookie(None) is None
    assert cs.parse_signed_cookie("") is None
    assert cs.parse_signed_cookie(".") is None
    assert cs.parse_signed_cookie("a.") is None
    assert cs.parse_signed_cookie(".b") is None


def test_key_rotation_invalidates_old_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the signing key rotates, cookies minted under the old key are
    rejected (fresh session), and a new mint validates under the new key."""
    monkeypatch.setenv("BIFROST_SESSION_SIGNING_KEY", "first-key")
    reset_settings()
    cs.reset_signing_key()
    minted_under_k1 = cs.mint_signed_cookie()
    sid_part = minted_under_k1.split(".", 1)[0]

    monkeypatch.setenv("BIFROST_SESSION_SIGNING_KEY", "second-key")
    reset_settings()
    cs.reset_signing_key()
    assert cs.parse_signed_cookie(minted_under_k1) is None
    assert cs.parse_signed_cookie(cs.sign_session_cookie(sid_part)) == sid_part


def test_ephemeral_key_used_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When BIFROST_SESSION_SIGNING_KEY is unset, an ephemeral key
    is generated and cookies still round-trip within one process."""
    monkeypatch.delenv("BIFROST_SESSION_SIGNING_KEY", raising=False)
    reset_settings()
    cs.reset_signing_key()

    minted = cs.mint_signed_cookie()
    sid = cs.parse_signed_cookie(minted)
    assert sid is not None

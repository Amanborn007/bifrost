"""Secret rotation with an overlap window so customers can switch verifiers."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from signing_keys.config import Settings
from signing_keys.store import KeyStore, SigningKey


class KeyRotator:
    def __init__(self, store: KeyStore, settings: Settings) -> None:
        self._store = store
        self._grace = timedelta(hours=settings.rotation_grace_hours)

    def rotate(self, subscription_id: str, now: datetime) -> SigningKey:
        """Create a new secret; the previous key stays valid for the grace period."""
        for old in self._store.keys_for(subscription_id):
            if old.expires_at is None:
                old.expires_at = now + self._grace
        new_key = SigningKey(
            subscription_id=subscription_id,
            secret=secrets.token_hex(32),
            created_at=now,
        )
        self._store.add(new_key)
        return new_key

"""HMAC-SHA256 payload signing."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

from signing_keys.store import KeyStore


def compute_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Signer:
    def __init__(self, store: KeyStore) -> None:
        self._store = store

    def sign(self, subscription_id: str, body: bytes, now: datetime) -> str:
        """Sign with the newest unexpired key for the subscription."""
        key = self._store.active_key(subscription_id, now)
        return compute_signature(key.secret, body)

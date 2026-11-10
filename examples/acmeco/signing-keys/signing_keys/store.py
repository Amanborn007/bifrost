"""Key storage. Secrets stay in the region that created them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SigningKey:
    subscription_id: str
    secret: str
    created_at: datetime
    expires_at: datetime | None = None


@dataclass
class KeyStore:
    region: str
    _keys: dict[str, list[SigningKey]] = field(default_factory=dict)

    def add(self, key: SigningKey) -> None:
        self._keys.setdefault(key.subscription_id, []).append(key)

    def keys_for(self, subscription_id: str) -> list[SigningKey]:
        return list(self._keys.get(subscription_id, []))

    def active_key(self, subscription_id: str, now: datetime) -> SigningKey:
        """Newest key that has not expired."""
        live = [k for k in self.keys_for(subscription_id) if k.expires_at is None or k.expires_at > now]
        if not live:
            raise KeyError(f"no active key for {subscription_id}")
        return max(live, key=lambda k: k.created_at)

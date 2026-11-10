"""Looks up active subscriptions for an account from the shared subscriptions table."""

from __future__ import annotations

from dataclasses import dataclass, field

from dispatcher.models import Target


@dataclass
class SubscriptionIndex:
    db_url: str
    _by_account: dict[str, list[Target]] = field(default_factory=dict)

    def register(self, account_id: str, target: Target) -> None:
        self._by_account.setdefault(account_id, []).append(target)

    def targets_for(self, account_id: str, event_type: str) -> list[Target]:
        return [t for t in self._by_account.get(account_id, []) if event_type in t.event_types]

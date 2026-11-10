"""Subscription CRUD backed by a repository."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from webhook_api.models import Subscription


@dataclass
class SubscriptionRepository:
    _rows: dict[str, Subscription] = field(default_factory=dict)

    def save(self, sub: Subscription) -> None:
        self._rows[sub.id] = sub

    def get(self, subscription_id: str) -> Subscription | None:
        return self._rows.get(subscription_id)

    def delete(self, subscription_id: str) -> bool:
        return self._rows.pop(subscription_id, None) is not None

    def list_for_account(self, account_id: str) -> list[Subscription]:
        return [s for s in self._rows.values() if s.account_id == account_id]


@dataclass
class SubscriptionService:
    repo: SubscriptionRepository
    region: str

    def create(self, account_id: str, endpoint_url: str, event_types: list[str]) -> Subscription:
        if not endpoint_url.startswith("https://"):
            raise ValueError("endpoint_url must use https")
        sub = Subscription(
            id=str(uuid.uuid4()),
            account_id=account_id,
            endpoint_url=endpoint_url,
            event_types=event_types,
            region=self.region,
        )
        self.repo.save(sub)
        return sub

    def delete(self, subscription_id: str) -> bool:
        return self.repo.delete(subscription_id)

    def list(self, account_id: str) -> list[Subscription]:
        return self.repo.list_for_account(account_id)

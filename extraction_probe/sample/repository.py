"""Order repository — data access layer."""

from __future__ import annotations

from extraction_probe.sample.models import Order


class OrderRepository:
    """In-memory order storage (would be PostgreSQL in production)."""

    def __init__(self) -> None:
        self._store: dict[str, Order] = {}

    async def save(self, order: Order) -> None:
        """Persist an order."""
        self._store[order.id] = order

    async def find_by_id(self, order_id: str) -> Order | None:
        """Retrieve an order by ID."""
        return self._store.get(order_id)

    async def find_by_customer(self, customer_id: str) -> list[Order]:
        """Retrieve all orders for a customer."""
        return [o for o in self._store.values() if o.customer_id == customer_id]

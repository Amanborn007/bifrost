"""Order processing service — business logic layer."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from extraction_probe.sample.repository import OrderRepository
from extraction_probe.sample.events import EventBus
from extraction_probe.sample.models import Order, OrderStatus
from extraction_probe.sample.validator import validate_order_weight


@dataclass
class OrderService:
    """Core order processing service.

    Handles order creation, validation, and lifecycle management.
    Publishes events on state transitions.
    """

    repo: OrderRepository
    event_bus: EventBus

    async def process_order(
        self,
        customer_id: str,
        tenant_plan: str,
        weight_kg: float,
        shipping_method: str = "standard",
    ) -> dict[str, Any]:
        """Process a new order through validation, persistence, and notification."""
        validate_order_weight(weight_kg, shipping_method)
        if weight_kg > 1000:
            raise ValueError(f"Weight {weight_kg} kg exceeds the 1000 kg order limit")

        order = Order(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            tenant_plan=tenant_plan,
            weight_kg=weight_kg,
            shipping_method=shipping_method,
            status=OrderStatus.PENDING,
        )

        await self.repo.save(order)
        await self.event_bus.publish("order.created", {"order_id": order.id})

        order.status = OrderStatus.SHIPPED
        await self.repo.save(order)
        await self.event_bus.publish("order.shipped", {"order_id": order.id})

        return {"id": order.id, "status": order.status.value, "weight_kg": order.weight_kg}

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = await self.repo.find_by_id(order_id)
        if order is None:
            return None
        return {"id": order.id, "status": order.status.value, "weight_kg": order.weight_kg}

    async def return_order(self, order_id: str) -> dict[str, Any]:
        """Return a shipped order."""
        order = await self.repo.find_by_id(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")
        if order.status != OrderStatus.SHIPPED:
            raise ValueError(f"Cannot return order in status {order.status}")

        order.status = OrderStatus.RETURNED
        await self.repo.save(order)
        await self.event_bus.publish("order.returned", {"order_id": order.id})
        return {"id": order.id, "status": order.status.value, "weight_kg": order.weight_kg}

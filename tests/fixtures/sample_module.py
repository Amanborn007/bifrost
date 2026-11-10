"""ShipmentService — books carrier shipments for the checkout flow.

This is a sample fixture for extraction testing. It represents a
realistic service module with dependencies and flow participation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

logger = logging.getLogger(__name__)


class CarrierGateway(Protocol):
    """External carrier gateway interface."""

    async def book(self, weight_g: int, service_level: str, token: str) -> dict: ...
    async def cancel(self, tracking_id: str, weight_g: int) -> dict: ...


@dataclass
class ShipmentResult:
    tracking_id: str
    status: str
    weight_g: int
    service_level: str
    processed_at: datetime


class ShipmentService:
    """Processes orders through the configured gateway.

    Coordinates between the OrderService (upstream) and the
    CarrierGateway (external). Emits events to the NotificationService.
    """

    def __init__(self, gateway: CarrierGateway, db_session, event_bus) -> None:
        self._gateway = gateway
        self._db = db_session
        self._events = event_bus

    async def create_shipment(
        self, order_id: str, weight_g: int, service_level: str, carrier_token: str
    ) -> ShipmentResult:
        """Create a shipment for the given order."""
        logger.info("Creating shipment for order %s: %d %s", order_id, weight_g, service_level)

        # Book through gateway
        response = await self._gateway.book(weight_g, service_level, carrier_token)

        # Store result
        result = ShipmentResult(
            tracking_id=response["tracking_id"],
            status=response["status"],
            weight_g=weight_g,
            service_level=service_level,
            processed_at=datetime.utcnow(),
        )

        # Persist
        await self._db.execute(
            "INSERT INTO orders (order_id, tracking_id, weight_g, service_level, status) "
            "VALUES ($1, $2, $3, $4, $5)",
            order_id,
            result.tracking_id,
            weight_g,
            service_level,
            result.status,
        )

        # Emit event
        await self._events.publish(
            "shipment.created",
            {
                "order_id": order_id,
                "tracking_id": result.tracking_id,
                "weight_g": weight_g,
            },
        )

        return result

    async def cancel_shipment(self, tracking_id: str, weight_g: int) -> ShipmentResult:
        """Cancel a previously created shipment."""
        await self._gateway.cancel(tracking_id, weight_g)
        await self._events.publish(
            "shipment.cancelled",
            {
                "tracking_id": tracking_id,
                "weight_g": weight_g,
            },
        )
        return ShipmentResult(
            tracking_id=tracking_id,
            status="cancelled",
            weight_g=weight_g,
            service_level="standard",
            processed_at=datetime.utcnow(),
        )

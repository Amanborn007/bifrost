"""Domain models for the sample order service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OrderStatus(Enum):
    PENDING = "pending"
    SHIPPED = "shipped"
    RETURNED = "returned"
    FAILED = "failed"


@dataclass
class Order:
    id: str
    customer_id: str
    tenant_plan: str
    weight_kg: float
    shipping_method: str
    status: OrderStatus

"""FastAPI entrypoint: POST /v1/deliver is called by the dispatcher."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from delivery_gateway.circuit_breaker import CircuitBreaker
from delivery_gateway.config import Settings
from delivery_gateway.delivery import DeliveryService
from delivery_gateway.retry import RetryPolicy
from delivery_gateway.signing_client import SigningClient

app = FastAPI(title="delivery-gateway", version="2.1.0")

settings = Settings.from_env()
delivery_service = DeliveryService(
    signer=SigningClient(host=settings.signing_keys_host),
    breaker=CircuitBreaker(
        failure_threshold=settings.breaker_failure_threshold,
        reset_seconds=settings.breaker_reset_seconds,
    ),
    retry=RetryPolicy(max_attempts=settings.max_attempts),
)


class DeliverRequest(BaseModel):
    event_id: str
    subscription_id: str
    endpoint_url: str
    event_type: str
    payload: dict[str, Any]


@app.post("/v1/deliver")
async def deliver(req: DeliverRequest) -> dict[str, bool]:
    return {"delivered": delivery_service.deliver(req.model_dump())}

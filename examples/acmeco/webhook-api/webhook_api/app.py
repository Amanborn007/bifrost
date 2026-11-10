"""FastAPI entrypoint: POST /v1/events and /v1/subscriptions CRUD."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from webhook_api.config import Settings
from webhook_api.ingest import IngestService, PayloadTooLarge
from webhook_api.models import IncomingEvent
from webhook_api.queue_client import QueueClient
from webhook_api.subscriptions import SubscriptionRepository, SubscriptionService

app = FastAPI(title="webhook-api", version="1.4.0")

settings = Settings.from_env()
ingest_service = IngestService(settings=settings, queue=QueueClient())
subscription_service = SubscriptionService(
    repo=SubscriptionRepository(), region=settings.region
)


class EventRequest(BaseModel):
    id: str
    account_id: str
    event_type: str
    payload: dict[str, Any]


class SubscriptionRequest(BaseModel):
    account_id: str
    endpoint_url: str
    event_types: list[str]


@app.post("/v1/events", status_code=202)
async def publish_event(req: EventRequest) -> dict[str, str]:
    """Accept an event and enqueue it on DELIVERY_QUEUE."""
    try:
        event_id = ingest_service.accept(IncomingEvent(**req.model_dump()))
    except PayloadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return {"event_id": event_id}


@app.post("/v1/subscriptions", status_code=201)
async def create_subscription(req: SubscriptionRequest) -> dict[str, Any]:
    try:
        sub = subscription_service.create(req.account_id, req.endpoint_url, req.event_types)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": sub.id, "region": sub.region}


@app.get("/v1/subscriptions")
async def list_subscriptions(account_id: str) -> list[dict[str, Any]]:
    return [
        {"id": s.id, "endpoint_url": s.endpoint_url, "event_types": s.event_types}
        for s in subscription_service.list(account_id)
    ]


@app.delete("/v1/subscriptions/{subscription_id}", status_code=204)
async def delete_subscription(subscription_id: str) -> None:
    if not subscription_service.delete(subscription_id):
        raise HTTPException(status_code=404, detail="subscription not found")

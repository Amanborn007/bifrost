"""FastAPI entrypoint: POST /v1/sign (called by delivery-gateway) and key rotation."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from signing_keys.config import Settings
from signing_keys.hmac_signer import Signer
from signing_keys.rotation import KeyRotator
from signing_keys.store import KeyStore

app = FastAPI(title="signing-keys", version="1.2.0")

settings = Settings.from_env()
store = KeyStore(region=settings.region)
signer = Signer(store)
rotator = KeyRotator(store, settings)


class SignRequest(BaseModel):
    subscription_id: str
    body: str


@app.post("/v1/sign")
async def sign(req: SignRequest) -> dict[str, str]:
    try:
        signature = signer.sign(req.subscription_id, req.body.encode(), datetime.now(timezone.utc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"signature": signature}


@app.post("/v1/keys/{subscription_id}/rotate")
async def rotate(subscription_id: str) -> dict[str, str]:
    key = rotator.rotate(subscription_id, datetime.now(timezone.utc))
    return {"subscription_id": key.subscription_id, "created_at": key.created_at.isoformat()}

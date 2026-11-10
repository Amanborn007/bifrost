"""HTTP client for delivery-gateway, addressed by DELIVERY_GATEWAY_HOST."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import httpx

from dispatcher.models import DeliveryJob


@dataclass
class GatewayClient:
    host: str
    timeout_s: float = 10.0

    def deliver(self, job: DeliveryJob) -> bool:
        """POST the job to delivery-gateway /v1/deliver; True when the gateway accepted it."""
        resp = httpx.post(
            f"https://{self.host}/v1/deliver",
            json=asdict(job),
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        return resp.json()["delivered"]

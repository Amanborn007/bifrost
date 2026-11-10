"""Core delivery path: sign, POST to the customer endpoint, retry with backoff."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from delivery_gateway.circuit_breaker import CircuitBreaker, CircuitOpen
from delivery_gateway.retry import RetryPolicy
from delivery_gateway.signing_client import SigningClient

SIGNATURE_HEADER = "X-Acme-Signature"


@dataclass
class DeliveryService:
    signer: SigningClient
    breaker: CircuitBreaker
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def deliver(self, job: dict[str, Any]) -> bool:
        """Deliver one job; returns True on a 2xx, False once retries are exhausted."""
        url = job["endpoint_url"]
        body = json.dumps(job["payload"], sort_keys=True).encode()
        signature = self.signer.sign(job["subscription_id"], body)
        headers = {SIGNATURE_HEADER: signature, "Content-Type": "application/json"}

        attempt = 0
        while True:
            attempt += 1
            try:
                self.breaker.before_call(url)
            except CircuitOpen:
                return False
            status = self._post(url, body, headers)
            if status is not None and 200 <= status < 300:
                self.breaker.record_success(url)
                return True
            self.breaker.record_failure(url)
            if not self.retry.should_retry(attempt, status):
                return False
            time.sleep(self.retry.delay_for(attempt))

    def _post(self, url: str, body: bytes, headers: dict[str, str]) -> int | None:
        try:
            return httpx.post(url, content=body, headers=headers, timeout=10.0).status_code
        except httpx.TransportError:
            return None

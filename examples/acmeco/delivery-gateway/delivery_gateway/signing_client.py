"""HTTP client for signing-keys, addressed by SIGNING_KEYS_HOST."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class SigningClient:
    host: str

    def sign(self, subscription_id: str, body: bytes) -> str:
        """Ask signing-keys for the HMAC signature of `body` for this subscription."""
        resp = httpx.post(
            f"https://{self.host}/v1/sign",
            json={"subscription_id": subscription_id, "body": body.decode()},
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json()["signature"]

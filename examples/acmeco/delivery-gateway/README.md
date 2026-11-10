# delivery-gateway

Outbound HTTP delivery for AcmeCo webhooks. Owned by team DLVR.

`dispatcher` calls `POST /v1/deliver`. The gateway asks `signing-keys` (`SIGNING_KEYS_HOST`) for an HMAC signature, sends the payload to the customer endpoint with the `X-Acme-Signature` header, and retries failures with exponential backoff (2s base, 5 attempts, 300s cap). A per-endpoint circuit breaker opens after 5 consecutive failures and stays open for 30 seconds.

Per-region settings live in `config/us.env` and `config/eu.env`.

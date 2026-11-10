# dispatcher

Fan-out worker for AcmeCo webhook delivery. Owned by team DLVR.

Consumes events that `webhook-api` publishes to `DELIVERY_QUEUE`, looks up the matching subscriptions, and sends one job per subscription to `delivery-gateway` (`DELIVERY_GATEWAY_HOST`, `POST /v1/deliver`). After each attempt it publishes a usage record to `BILLING_TOPIC`, which `usage-metering` consumes.

Per-region settings live in `config/us.env` and `config/eu.env`.

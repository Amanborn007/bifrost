# webhook-api

Public entrypoint for AcmeCo webhook delivery. Owned by team DLVR.

- `POST /v1/events` validates an event and publishes it to the `DELIVERY_QUEUE` queue. The `dispatcher` service consumes that queue.
- `POST/GET/DELETE /v1/subscriptions` manage customer endpoints.

Each region (`us`, `eu`) runs its own deployment with its own queue; see `config/us.env` and `config/eu.env`. Events never cross regions, so data residency is preserved.

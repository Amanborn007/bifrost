# signing-keys

HMAC secret management for AcmeCo webhooks. Owned by team PLAT.

`delivery-gateway` calls `POST /v1/sign` to get an HMAC-SHA256 signature for each payload, which it sends as the `X-Acme-Signature` header. `POST /v1/keys/{subscription_id}/rotate` creates a new secret; the previous key keeps working for `ROTATION_GRACE_HOURS` (24 by default) so customers can update their verifiers.

Keys are stored per region (`config/us.env`, `config/eu.env`) and never replicated across regions.

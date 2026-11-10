# usage-metering

Usage aggregation for AcmeCo webhook billing (TypeScript). Owned by team PLAT.

`dispatcher` publishes one usage record per delivery attempt to `BILLING_TOPIC`. This service subscribes to that topic, counts attempted and delivered events per account, and every `FLUSH_INTERVAL_SECONDS` publishes per-account rollups to `ROLLUP_TOPIC`.

Per-region settings live in `config/us.env` and `config/eu.env`.

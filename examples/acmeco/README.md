# AcmeCo demo org

AcmeCo is a fictional company that runs a webhook delivery platform in two data-residency regions, `us` and `eu`. Five small services make up the platform. Team DLVR owns `webhook-api`, `dispatcher` and `delivery-gateway`. Team PLAT owns `signing-keys` and `usage-metering`. Each service has a `catalog-info.yaml`, a `CODEOWNERS` file and per-region `config/*.env` files that name the hosts and topics it talks to. `docs/benchmarks/acme.yml` holds questions about this org with expected answers.

## 1. Seed the repos

`scripts/seed_acmeco.py` copies each service into `$TMPDIR/acmeco/<name>`, runs `git init` and replays a fixed commit history with fixed authors and dates, so the SHAs are the same on every machine.

```bash
cp examples/acmeco/bifrost.yaml bifrost.yaml
uv run python scripts/seed_acmeco.py          # seed only
```

## 2. Register and extract

Registering does not extract. `--extract` registers each repo, then runs `bifrost extract run <id>` for it, one repo at a time, and stops with a non-zero exit on the first failure.

```bash
uv run python scripts/seed_acmeco.py --bifrost-cmd "uv run bifrost" --register --extract
```

The same steps by hand, for one repo:

```bash
uv run bifrost repo add "$TMPDIR/acmeco/dispatcher"
curl -s http://localhost:8000/api/v1/repos     # copy the full id of the repo
uv run bifrost extract run <repo-id>
```

## 3. Rebuild the service graph (optional)

Registering a repo already rebuilds the service-level graph in the background (`api/routes/repos.py`). The graph comes from the catalog files and config keys, not from extraction. To force a rebuild, for example after editing a catalog file, call the endpoint below. Under the demo's `identity.provider: none` a loopback call is already admin, so no token is needed:

```bash
curl -X POST http://localhost:8000/api/v1/admin/index-services
```

With `header` or `oidc_jwt` it needs an admin API token (`bifrost token new --role admin`) in `api_tokens`, exported as `BIFROST_TOKEN`. Registration runs the same build and apply as the endpoint, but only over registered repos. The endpoint also reads catalog repos by default.

```bash
curl -X POST http://localhost:8000/api/v1/admin/index-services \
  -H "Authorization: Bearer $BIFROST_TOKEN"
```

## 4. Research and benchmark

Open http://localhost:5173, go to Chat and ask "What services does the AcmeCo webhook delivery platform consist of and how do they connect to each other?". To score the whole question set:

```bash
uv run python -m bifrost.benchmarks --domain acme --label <your-label>
```

## Recovery: a repo that crashed at the end of extraction

If extraction fails at the repo summary step after phases A to I have finished and been checkpointed, a plain re-run resumes from that checkpoint, skips every finished phase and has nothing left to summarise. Re-extract with the checkpoint cleared:

```bash
uv run bifrost extract run <repo-id> --force
```

`--force` sends `force_full=true`. The API call is `POST /api/v1/repos/<repo-id>/extract` with `{"force_full": true}`.

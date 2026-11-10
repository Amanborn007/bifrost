#!/usr/bin/env python3
"""Seed the AcmeCo demo org as real git repos with deterministic history.

Copies each repo under ``examples/acmeco/`` into a target directory,
runs ``git init`` and replays a fixed commit plan (fixed author, fixed
dates, Jira-style keys in the messages) so the resulting SHAs are
reproducible and historical questions have something to find.

Usage: ``uv run python scripts/seed_acmeco.py [TARGET] [--register] [--extract]``.
TARGET defaults to ``$TMPDIR/acmeco``. ``--register`` also runs
``bifrost repo add <abs path>`` for each repo; extraction git-clones the
registered URL, so each path must be (and is) a real git repo. Registering
does not extract: ``--extract`` (needs ``--register``) then runs
``bifrost extract run <id>`` for each repo and fails on the first failure.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples" / "acmeco"

AUTHOR_NAME = "AcmeCo Dev"
AUTHOR_EMAIL = "dev@example.com"


@dataclass(frozen=True)
class Commit:
    date: str
    subject: str
    body: str
    paths: tuple[str, ...]


META = ("CODEOWNERS", "catalog-info.yaml")

PLAN: dict[str, list[Commit]] = {
    "webhook-api": [
        Commit(
            "2026-02-02T10:00:00+0000",
            "DLVR-3: add subscriptions CRUD",
            "Customers register an https endpoint and the event types they want.",
            (
                "webhook_api/__init__.py",
                "webhook_api/models.py",
                "webhook_api/subscriptions.py",
            ),
        ),
        Commit(
            "2026-02-16T10:00:00+0000",
            "DLVR-6: ingest events onto DELIVERY_QUEUE",
            "POST /v1/events validates the payload (413 over 256 KiB) and enqueues it "
            "for the dispatcher. One queue per region keeps data residency intact.",
            (
                "webhook_api/config.py",
                "webhook_api/queue_client.py",
                "webhook_api/ingest.py",
                "webhook_api/app.py",
                "config/us.env",
                "config/eu.env",
            ),
        ),
        Commit(
            "2026-03-02T10:00:00+0000",
            "DLVR-23: add catalog-info, CODEOWNERS and README",
            "",
            ("README.md", *META),
        ),
    ],
    "dispatcher": [
        Commit(
            "2026-02-09T10:00:00+0000",
            "DLVR-8: initial fan-out worker design",
            "Models, subscription lookup and the fan-out step that builds one "
            "delivery job per matching subscription.",
            (
                "dispatcher/__init__.py",
                "dispatcher/models.py",
                "dispatcher/config.py",
                "dispatcher/subscription_index.py",
                "dispatcher/fanout.py",
                "config/us.env",
                "config/eu.env",
            ),
        ),
        Commit(
            "2026-02-23T10:00:00+0000",
            "DLVR-14: call delivery-gateway over DELIVERY_GATEWAY_HOST",
            "The dispatcher no longer sends HTTP itself; retries and circuit "
            "breaking live in delivery-gateway.",
            ("dispatcher/gateway_client.py",),
        ),
        Commit(
            "2026-03-09T10:00:00+0000",
            "DLVR-15: publish usage records to BILLING_TOPIC",
            "One record per delivery attempt so usage-metering can bill accurately.",
            ("dispatcher/billing.py", "dispatcher/worker.py"),
        ),
        Commit(
            "2026-03-16T10:00:00+0000",
            "DLVR-23: add catalog-info, CODEOWNERS and README",
            "",
            ("README.md", *META),
        ),
    ],
    "delivery-gateway": [
        Commit(
            "2026-02-10T10:00:00+0000",
            "DLVR-10: scaffold delivery-gateway and signing client",
            "Settings plus the client for signing-keys (SIGNING_KEYS_HOST).",
            (
                "delivery_gateway/__init__.py",
                "delivery_gateway/config.py",
                "delivery_gateway/signing_client.py",
                "config/us.env",
                "config/eu.env",
            ),
        ),
        Commit(
            "2026-02-24T10:00:00+0000",
            "DLVR-12: add exponential backoff retry for failed deliveries",
            "Customers lost events after a single 503. Retry up to 5 attempts "
            "with a 2s base delay, capped at 300s. 4xx other than 429 is not retried.",
            ("delivery_gateway/retry.py",),
        ),
        Commit(
            "2026-03-10T10:00:00+0000",
            "DLVR-19: add per-endpoint circuit breaker",
            "A dead customer URL kept workers busy retrying. Open the breaker "
            "after 5 consecutive failures and hold it open for 30 seconds.",
            ("delivery_gateway/circuit_breaker.py",),
        ),
        Commit(
            "2026-03-17T10:00:00+0000",
            "DLVR-21: wire delivery path and /v1/deliver endpoint",
            "Sign, POST with X-Acme-Signature, retry and breaker in one path.",
            (
                "delivery_gateway/delivery.py",
                "delivery_gateway/app.py",
                "README.md",
                *META,
            ),
        ),
    ],
    "signing-keys": [
        Commit(
            "2026-01-26T10:00:00+0000",
            "PLAT-3: initial HMAC-SHA256 signer and key store",
            "Per-subscription secrets; payloads signed with HMAC-SHA256.",
            (
                "signing_keys/__init__.py",
                "signing_keys/config.py",
                "signing_keys/store.py",
                "signing_keys/hmac_signer.py",
                "config/us.env",
            ),
        ),
        Commit(
            "2026-02-12T10:00:00+0000",
            "PLAT-7: add key rotation with a 24h grace period",
            "Rotating a secret instantly broke customer verifiers. The previous key "
            "now stays valid for ROTATION_GRACE_HOURS (24) after a rotate.",
            ("signing_keys/rotation.py",),
        ),
        Commit(
            "2026-03-05T10:00:00+0000",
            "PLAT-11: expose sign and rotate endpoints, add eu deployment",
            "Keys stay in the region that created them; eu gets its own config.",
            ("signing_keys/app.py", "config/eu.env", "README.md", *META),
        ),
    ],
    "usage-metering": [
        Commit(
            "2026-02-03T10:00:00+0000",
            "PLAT-5: initial usage meter",
            "Per-account counters for attempted and delivered events.",
            (
                "package.json",
                "tsconfig.json",
                "src/config.ts",
                "src/meter.ts",
                "config/us.env",
                "config/eu.env",
            ),
        ),
        Commit(
            "2026-02-19T10:00:00+0000",
            "PLAT-9: consume usage records from BILLING_TOPIC",
            "The dispatcher publishes one record per delivery attempt.",
            ("src/consumer.ts",),
        ),
        Commit(
            "2026-03-12T10:00:00+0000",
            "PLAT-13: publish per-account rollups to ROLLUP_TOPIC",
            "",
            ("src/publisher.ts",),
        ),
        Commit(
            "2026-03-19T10:00:00+0000",
            "PLAT-14: add catalog-info, CODEOWNERS and README",
            "",
            ("README.md", *META),
        ),
    ],
}


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
    )


def _source_files(src: Path) -> set[str]:
    return {
        p.relative_to(src).as_posix()
        for p in src.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def seed_repo(name: str, target_root: Path) -> Path:
    src = EXAMPLES_DIR / name
    if not src.is_dir():
        raise FileNotFoundError(f"missing example repo: {src}")

    planned = [p for c in PLAN[name] for p in c.paths]
    on_disk = _source_files(src)
    missing, unplanned = set(planned) - on_disk, on_disk - set(planned)
    if missing or unplanned or len(planned) != len(set(planned)):
        raise ValueError(
            f"{name}: commit plan out of sync with files "
            f"(missing={sorted(missing)}, unplanned={sorted(unplanned)})"
        )

    dest = target_root / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _git(dest, "init", "-q", "-b", "main")

    for commit in PLAN[name]:
        for rel in commit.paths:
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, out)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": commit.date,
            "GIT_COMMITTER_DATE": commit.date,
        }
        _git(dest, "add", "-A", env=env)
        message = ["-m", commit.subject] + (["-m", commit.body] if commit.body else [])
        _git(dest, "commit", "-q", *message, env=env)
    return dest


def _repo_ids(names: list[str]) -> dict[str, str]:
    # `bifrost repo add` ignores `-o json`, so read the ids back from the API.
    from bifrost.cli.commands._http import get_client, unwrap

    with get_client() as client:
        registered = {r["name"]: r["id"] for r in unwrap(client.get("/repos"))}
    missing = [n for n in names if n not in registered]
    if missing:
        raise RuntimeError(f"repos not registered, cannot extract: {missing}")
    return {n: registered[n] for n in names}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "acmeco",
        help="directory to create the repos in (default: $TMPDIR/acmeco)",
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="run `bifrost repo add <abs path>` for each seeded repo",
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help="after --register, run `bifrost extract run <id>` for each repo",
    )
    parser.add_argument(
        "--bifrost-cmd",
        default="bifrost",
        help="bifrost CLI invocation, e.g. 'uv run bifrost' (default: bifrost)",
    )
    args = parser.parse_args(argv)
    if args.extract and not args.register:
        parser.error("--extract requires --register")

    target = args.target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    seeded = [seed_repo(name, target) for name in PLAN]
    for path in seeded:
        print(f"seeded {path}")

    if args.register:
        cmd = args.bifrost_cmd.split()
        for path in seeded:
            subprocess.run([*cmd, "repo", "add", str(path)], check=True)
        if args.extract:
            for name, repo_id in _repo_ids([p.name for p in seeded]).items():
                print(f"extracting {name} ({repo_id})")
                subprocess.run([*cmd, "extract", "run", repo_id], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

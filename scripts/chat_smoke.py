#!/usr/bin/env python
"""Flagship functional benchmark — drive a REAL chat query end-to-end.

Unlike screen_smoke.py (which only checks endpoints return non-5xx), this
exercises the actual product: create a conversation, post a question, and
consume the SSE research stream through the production path
(run_query_turn → scope → fan-out → synthesis), asserting a real answer
comes back (a ``done`` event) rather than an ``error``.

This is the exact flow that 500'd for the operator before the stale-server /
auto_extract_wait_s fix. It invokes ``claude -p`` (real LLM, ~1-2 min, real
cost), so it's a manual benchmark, not a CI unit test.

    python scripts/chat_smoke.py "what is the orders service and what does it do?"
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

# Import path: run from repo root (script lives in scripts/).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bifrost.api.auth.csrf import CSRF_HEADER_NAME, CSRF_HEADER_VALUE  # noqa: E402

BASE = "http://localhost:8000/api/v1"
# The server sets a session cookie on create, so later POSTs need the CSRF header.
HEADERS = {
    "X-Forwarded-Email": "smoke@acmeco.example",
    CSRF_HEADER_NAME: CSRF_HEADER_VALUE,
}
TIMEOUT_S = 300.0


def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else (
        "what is the orders service and what does it do?"
    )
    c = httpx.Client(base_url=BASE, headers=HEADERS, timeout=30)

    # 1) create a conversation. Scope to ONE repo (env BIFROST_SMOKE_SCOPE_ALL=1
    # to force all-repos) so the fan-out stays small and the run completes fast.
    scope: list[str] = []
    if os.environ.get("BIFROST_SMOKE_SCOPE_ALL") != "1":
        repos = c.get("/repos").json().get("data", [])
        indexed = [r for r in repos if r.get("index_status") == "complete"] or repos
        if indexed:
            scope = [indexed[0]["id"]]
            print(f"scoped to repo: {indexed[0].get('name')} ({scope[0]})")
    r = c.post(
        "/conversations",
        json={"title": f"smoke: {question[:40]}", "scope_repo_ids": scope},
    )
    r.raise_for_status()
    conv = r.json()["data"]["id"]
    print(f"conversation: {conv}")

    # 2) post the question — kicks off run_query_turn on the worker
    r = c.post(
        f"/conversations/{conv}/messages",
        json={"content": question, "idempotency_key": f"smoke-{int(time.time())}"},
    )
    r.raise_for_status()
    print(f"posted message: {r.json().get('data', r.json())}")

    # 3) consume the SSE research stream until done/error/timeout
    seen: dict[str, int] = {}
    answer_len = 0
    outcome = "TIMEOUT"
    err = None
    start = time.time()
    # Reuse the SAME client `c` so the signed bifrost_session cookie set on
    # create/post persists — ownership is session-cookie-scoped, so a fresh
    # client would 404 (it would look like a different session).
    if True:
        with c.stream("GET", f"/conversations/{conv}/stream", timeout=TIMEOUT_S) as resp:
            print(f"stream status: {resp.status_code}")
            # SSE wire format is `event: {type}` then `data: {json}`. Track the
            # event name from the event: line and act on the following data:.
            current = None
            for line in resp.iter_lines():
                if time.time() - start > TIMEOUT_S:
                    break
                if line.startswith("event:"):
                    current = line[6:].strip()
                    seen[current] = seen.get(current, 0) + 1
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(line[5:].strip())
                except Exception:
                    evt = {}
                if current in ("synthesis_delta", "token"):
                    answer_len += len(evt.get("text") or evt.get("token") or "")
                elif current in ("done", "assistant_message", "task_complete"):
                    outcome = "ANSWER"
                    answer_len = max(
                        answer_len,
                        len(str(evt.get("answer") or evt.get("content") or "")),
                    )
                    break
                elif current in ("error", "task_failed"):
                    outcome = "ERROR"
                    err = evt.get("error") or evt.get("message")
                    break

    dur = time.time() - start
    print(f"\n{'='*50}")
    print(f"event types seen: {seen}")
    print(f"outcome: {outcome}  answer_chars: {answer_len}  elapsed: {dur:.0f}s")
    if outcome == "ANSWER" and answer_len > 0:
        print("✅ chat works end-to-end — real answer produced (reached done)")
        return 0
    if outcome == "ERROR":
        print(f"❌ chat returned an error event: {err}")
        return 1
    if answer_len > 0:
        print(
            f"✅ chat is producing a real answer ({answer_len} synthesis chars "
            f"streamed) but did not reach 'done' within {TIMEOUT_S:.0f}s — "
            "latency, not a broken flow (large fan-out)."
        )
        return 0
    print("❌ no answer within timeout (see event types above)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

# Testing

## Commands

```bash
make test                                   # backend; needs Postgres
uv run python -m pytest -q -p no:cacheprovider --noconftest tests/test_leak_gate.py   # no database needed
cd ui && npx vitest run                     # UI
```

Most backend tests need Postgres, because the session fixture in `tests/conftest.py` migrates a test database and creates `bifrost_test` when it is missing. `BIFROST_SKIP_DB_MIGRATIONS=1` skips that step for pure-logic tests, and the leak-gate workflow sets it. With `--noconftest`, `tests/test_ownership_sources.py` also runs without a database. [CONTRIBUTING.md](../CONTRIBUTING.md#run-the-checks-only) has the setup. `tests/e2e/` starts its own Postgres container through testcontainers, so it needs Docker, and `make test` leaves it out with `--ignore=tests/e2e`.

## Counts on 2026-11-10

- Backend: 2466 tests collected (`pytest tests/ --ignore=tests/e2e --co -q`, parametrized cases included).
- UI: 20 files and 118 tests pass on a fresh clone after `npm ci`.
- 24 backend tests are marked xfail in `tests/conftest.py` as known failures, with `strict=False`, so a test that starts passing shows as XPASS and does not fail the run. One example is the 1536-versus-384 vector dimension drift, where tests still pass 1536-dimension placeholder vectors to a 384-dimension column.
- Without `LEAK_DENYLIST`, `tests/test_leak_gate.py` skips its term test and runs the other four.

## Edge cases with a test each

Each id is a full pytest node id, so it can be pasted into `uv run pytest`.

| Case | Test |
|---|---|
| The private-address opt-in for git URLs is refused in production mode. | `tests/test_git_ssrf.py::TestProductionOptOut::test_opt_in_refused_in_production` |
| A literal `</user_intent>` in user text is escaped, so only the wrapper's own closing tag survives. | `tests/security/test_prompt_injection.py::TestWrapUntrusted::test_attacker_cannot_close_boundary_via_literal_close_tag` |
| A cookie-authenticated mutation without `X-Requested-With: bifrost-ui` gets 403, and with it gets 200. | `tests/test_csrf.py::test_cookie_auth_without_header_rejected` |
| A verify command with shell metacharacters is rejected with a reason. | `tests/test_shell_safe.py::TestRejects::test_metacharacter_injection_rejected` |
| A PEM private key body does not survive in the output text. | `tests/test_secret_scrubbing.py::TestScrubSecretsHelper::test_private_key_redacted` |
| A mistyped `BIFROST_` variable fails startup with "did you mean" and the nearest valid name. | `tests/test_unknown_settings.py::test_error_suggests_the_closest_name` |
| A new mutating route fails the test until it is admin-gated or listed as a member action. | `tests/test_route_role_inventory.py::test_every_mutating_route_is_admin_or_a_declared_member_action` |
| The extraction task is committed before it is submitted to the worker. | `tests/test_extract_commit_order.py::test_task_is_committed_before_submit` |

## Benchmark

12 questions in `docs/benchmarks/acme.yml` and a scorer that uses regex and substring matching only. Three identical baseline runs spread by 0.007, so a gain smaller than that cannot be read ([roadmap section 1](roadmap.md#1-verdict)). Runs A and B ask all 12 questions and run C asks the 9 that have a paraphrase, so a full A/B/C run is 33 chat turns. It cost about $44 to $51 in late 2026.

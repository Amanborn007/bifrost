# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub security advisories. Open the
repository's Security tab, choose "Report a vulnerability", and describe what
you found and how to reproduce it. Please do not open a public issue or pull
request for a vulnerability.

We aim to acknowledge a report within 5 working days and to tell you what we
plan to do about it within 14 days.

## Supported versions

Only the latest commit on `main` is supported. Bifrost has no tagged releases yet.

## What is in scope

Bifrost clones repositories and runs the `claude` CLI against them, so the
areas where a flaw matters most are:

- Shell and path handling around cloned repositories (`src/bifrost/actions/_shell_safe.py`).
- Secret redaction before file content reaches a prompt (`src/bifrost/extraction/secrets.py`).
- The identity layer, role gates and API tokens (`src/bifrost/api/auth/`).
- Session cookie signing (`src/bifrost/api/_cookie_signing.py`).
- The task agents that run `claude -p` in a worktree (`src/bifrost/actions/executor.py`, `verifier.py`, `debugger.py`).

## Deployment notes

With `identity.provider: none` Bifrost serves loopback clients only and treats
them as an admin. Before exposing it beyond localhost, set
`BIFROST_PRODUCTION_MODE=true`, use `header` or `oidc_jwt`, and set
`identity.roles` so only the groups you name are admins. Give scripts API
tokens (`bifrost token new`) with the lowest role that works. See
[RUNNING.md](RUNNING.md#13-configuration-reference).

### Task execution

Execute, retry, verify, debug and `/quick` run `claude -p` with
`--permission-mode bypassPermissions` in the task worktree. The tool list
includes `Bash(python *)`, so treat these steps as arbitrary code running as
the Bifrost OS user. The subprocess has an empty MCP config, and its
environment carries no Bifrost, Jira or cloud tokens, but `HOME` is passed, so
files the OS user can read are reachable. That includes cloud CLI config, git
and gh credentials, the Claude login, and Bifrost's own `.env` and
`bifrost.yaml`.

The routes are admin-only. Plans can still carry Jira, Slack and PR text, and
that text can try to steer the agent (prompt injection). Run Bifrost as a user
that holds nothing you would not hand the agent, and keep the admin group small.

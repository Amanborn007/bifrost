"""Tests for ``bifrost.extraction.cli_client.build_minimal_env``.

The minimal env is the second-line defence: even if a future
regression weakens verify_cmd validation OR a Bash tool inside an agent
is exploited, there should be no JIRA/AWS/DB credentials in the
subprocess env to exfiltrate.
"""

from __future__ import annotations

import pytest

from bifrost.extraction.cli_client import build_minimal_env


class TestAllowlistedKeys:
    """Keys the subprocess legitimately needs must pass through."""

    def test_path_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
        env = build_minimal_env()
        assert env["PATH"] == "/usr/local/bin:/usr/bin"

    def test_home_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/Users/test")
        env = build_minimal_env()
        assert env["HOME"] == "/Users/test"

    def test_virtualenv_passes_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("VIRTUAL_ENV", "/proj/.venv")
        env = build_minimal_env()
        assert env["VIRTUAL_ENV"] == "/proj/.venv"


class TestSecretsStripped:
    """The whole point of the function. Anything that smells like a
    credential must NOT be in the result, no matter how it was named."""

    @pytest.mark.parametrize(
        "key,value",
        [
            ("JIRA_API_TOKEN", "atlas-secret-12345"),
            ("DATABASE_URL", "postgresql://user:pass@host/db"),
            ("AWS_SECRET_ACCESS_KEY", "AKIA-DEADBEEF"),
            ("AWS_ACCESS_KEY_ID", "AKIATESTKEY"),
            ("BIFROST_ANTHROPIC_API_KEY", "sk-ant-api-test"),
            ("ANTHROPIC_API_KEY", "sk-ant-test"),
            ("GH_TOKEN", "ghp_test_pat"),
            ("GITHUB_TOKEN", "ghp_other_pat"),
            ("SLACK_BOT_TOKEN", "xoxb-test"),
            ("CONFLUENCE_PASSWORD", "test-pw"),
            ("BIFROST_DB_PASSWORD", "test-pg-pw"),
            ("OPENAI_API_KEY", "sk-test"),
        ],
    )
    def test_secret_key_not_in_minimal_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key: str,
        value: str,
    ) -> None:
        monkeypatch.setenv(key, value)
        env = build_minimal_env()
        assert key not in env, f"{key} (a known credential-shape var) leaked into minimal env"
        # And the value MUST not appear in any other key either, in case
        # someone adds a future allowlist entry that interpolates it.
        assert all(value not in v for v in env.values()), (
            f"value of {key} appeared somewhere in minimal env"
        )


class TestExtra:
    """The ``extra`` parameter is how the executor adds its own keys."""

    def test_extra_overlay(self) -> None:
        env = build_minimal_env({"CLAUDE_CODE_NO_SESSION_PERSISTENCE": "1"})
        assert env["CLAUDE_CODE_NO_SESSION_PERSISTENCE"] == "1"

    def test_extra_can_override_passthrough(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PATH", "/system")
        env = build_minimal_env({"PATH": "/custom"})
        assert env["PATH"] == "/custom"


class TestNoLeakOfWholeEnv:
    """If somebody types ``env={**os.environ, …}`` instead of using the
    helper, this test will not catch it — but if the helper itself
    regresses to the old blocklist behaviour, the result will contain
    far more keys than the allowlist allows."""

    def test_result_only_contains_allowlisted_keys_plus_extra(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Inject a clearly-non-allowlisted key
        monkeypatch.setenv("ABSOLUTELY_NOT_ALLOWLISTED_TEST_KEY", "x")
        env = build_minimal_env({"CLAUDE_CODE_NO_SESSION_PERSISTENCE": "1"})

        # _SAFE_ENV_KEYS is private; assert by negative — the synthetic
        # key cannot be in env.
        assert "ABSOLUTELY_NOT_ALLOWLISTED_TEST_KEY" not in env

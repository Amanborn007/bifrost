"""Startup rejects BIFROST settings that nothing reads.

pydantic-settings ignores unknown keys, so a typo (or a name from an older
release) silently leaves a gate off. ``validate_startup`` scans the process
environment, the ``.env`` file and bifrost.yaml and fails on any unknown name.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from starlette.requests import Request
from typer.testing import CliRunner

from bifrost.api.auth.providers import build_identity_provider
from bifrost.cli.config import BifrostSettings, reset_settings, validate_startup
from bifrost.cli.unknown_settings import PROCESS_ENV_ONLY, collect_known_names, is_known_name
from bifrost.errors import ConfigError
from tests.auth_helpers import boot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(BifrostSettings.model_config, "env_file", None)
    monkeypatch.setenv("BIFROST_CONFIG", "")
    reset_settings()
    yield
    reset_settings()


def _dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> None:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setitem(BifrostSettings.model_config, "env_file", str(path))


def _yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bifrost.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONFIG", str(path))
    return path


# ── environment ─────────────────────────────────────────────────────


def test_unknown_env_name_fails_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_NOT_A_SETTING", "1")
    with pytest.raises(ConfigError, match="BIFROST_NOT_A_SETTING"):
        validate_startup()


def test_lowercase_prefix_is_checked_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("bifrost_not_a_setting", "1")
    with pytest.raises(ConfigError, match="(?i)bifrost_not_a_setting"):
        validate_startup()


def test_empty_value_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_NOT_A_SETTING", "")
    with pytest.raises(ConfigError, match="BIFROST_NOT_A_SETTING"):
        validate_startup()


def test_plain_field_with_nested_suffix_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_DEMO_MODE__X", "1")
    with pytest.raises(ConfigError, match="BIFROST_DEMO_MODE__X"):
        validate_startup()


@pytest.mark.parametrize(
    ("typo", "expected"),
    [
        ("BIFROST_DEMO_MOD", "BIFROST_DEMO_MODE"),
        ("BIFROST_IDENTITY__TRUSTED_PROXI", "BIFROST_IDENTITY__TRUSTED_PROXY"),
    ],
)
def test_error_suggests_the_closest_name(
    monkeypatch: pytest.MonkeyPatch, typo: str, expected: str
) -> None:
    monkeypatch.setenv(typo, "1")
    with pytest.raises(ConfigError, match=f"did you mean {expected}"):
        validate_startup()


def test_every_unknown_name_is_listed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_FIRST_BAD", "1")
    monkeypatch.setenv("BIFROST_SECOND_BAD", "1")
    with pytest.raises(ConfigError) as exc:
        validate_startup()
    assert "BIFROST_FIRST_BAD" in str(exc.value)
    assert "BIFROST_SECOND_BAD" in str(exc.value)


def test_known_names_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_DEMO_MODE", "false")
    monkeypatch.setenv("BIFROST_IDENTITY__PROVIDER", "none")
    monkeypatch.setenv("BIFROST_SOURCES__JIRA__BASE_URL", "https://jira.example.com")
    validate_startup()


@pytest.mark.parametrize("name", sorted(set(PROCESS_ENV_ONLY) - {"BIFROST_CONFIG"}))
def test_allowlisted_process_env_names_boot(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "x")
    validate_startup()


def test_known_name_matching() -> None:
    assert is_known_name("BIFROST_DEMO_MODE")
    assert is_known_name("bifrost_demo_mode")
    assert is_known_name("BIFROST_IDENTITY__OIDC__JWKS_URL")
    assert is_known_name("BIFROST_IDENTITY__ROLES__ADMIN")
    assert not is_known_name("BIFROST_IDENTITY__PROVIDER__X")
    assert not is_known_name("BIFROST_NOT_A_SETTING")


def test_builder_raises_when_a_field_gains_an_alias() -> None:
    class Aliased(BaseModel):
        value: int = Field(default=0, alias="other")

    with pytest.raises(ConfigError, match="alias"):
        collect_known_names(Aliased)


# ── .env ────────────────────────────────────────────────────────────


def test_unknown_dotenv_name_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _dotenv(monkeypatch, tmp_path, "BIFROST_NOT_A_SETTING=1\n")
    with pytest.raises(ConfigError, match=r"BIFROST_NOT_A_SETTING.*\.env"):
        validate_startup()


def test_non_bifrost_dotenv_lines_are_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _dotenv(monkeypatch, tmp_path, "OTHER_TOOL_TOKEN=abc\nBIFROST_DEMO_MODE=false\n")
    validate_startup()


def test_renamed_jira_dotenv_line_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _dotenv(monkeypatch, tmp_path, "JIRA_API_TOKEN=abc\nBIFROST_DEMO_MODE=false\n")
    with pytest.raises(ConfigError, match="BIFROST_SOURCES__JIRA__API_TOKEN"):
        validate_startup()


def test_process_env_only_name_in_dotenv_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _dotenv(monkeypatch, tmp_path, "BIFROST_HANDOVERS_DIR=/tmp/h\n")
    with pytest.raises(ConfigError, match="process environment"):
        validate_startup()


def test_operator_email_and_groups_are_read_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BIFROST_OPERATOR_EMAIL", raising=False)
    monkeypatch.delenv("BIFROST_OPERATOR_GROUPS", raising=False)
    _dotenv(
        monkeypatch,
        tmp_path,
        "BIFROST_OPERATOR_EMAIL=dev@example.com\nBIFROST_OPERATOR_GROUPS=eng, ops\n",
    )
    settings = validate_startup()
    assert settings.operator_email == "dev@example.com"
    assert settings.operator_groups == "eng, ops"


async def test_operator_identity_resolves_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BIFROST_OPERATOR_EMAIL", raising=False)
    _dotenv(
        monkeypatch,
        tmp_path,
        "BIFROST_OPERATOR_EMAIL=dev@example.com\nBIFROST_OPERATOR_GROUPS=eng, ops\n",
    )
    provider = build_identity_provider(validate_startup())
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    principal = await provider.resolve(Request({**scope, "client": ("127.0.0.1", 50000)}))
    assert principal is not None
    assert principal.email == "dev@example.com"
    assert principal.groups == ("eng", "ops")


# ── bifrost.yaml ────────────────────────────────────────────────────


def test_top_level_yaml_typo_fails_with_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _yaml(monkeypatch, tmp_path, "demo_mod: true\n")
    with pytest.raises(ConfigError, match="demo_mod") as exc:
        validate_startup()
    assert str(path) in str(exc.value)
    assert "did you mean demo_mode" in str(exc.value)


def test_nested_yaml_typo_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _yaml(monkeypatch, tmp_path, "identity:\n  trusted_proxi: true\n")
    with pytest.raises(ConfigError, match="trusted_proxi"):
        validate_startup()


def test_valid_yaml_boots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _yaml(monkeypatch, tmp_path, "demo_mode: false\nidentity:\n  provider: none\n")
    validate_startup()


# ── a dead gate must not boot, in every source ──────────────────────


def test_unread_gate_name_fails_create_app_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="BIFROST_TRUSTED_PROXY_MODE"):
        boot(
            monkeypatch,
            provider="none",
            demo=True,
            extra_env={"BIFROST_TRUSTED_PROXY_MODE": "true"},
        )


def test_unread_gate_name_fails_create_app_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _dotenv(monkeypatch, tmp_path, "BIFROST_TRUSTED_PROXY_MODE=true\n")
    with pytest.raises(ConfigError, match="BIFROST_TRUSTED_PROXY_MODE"):
        boot(monkeypatch, provider="none", demo=True)


def test_unread_gate_name_fails_create_app_from_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _yaml(monkeypatch, tmp_path, "trusted_proxy_mode: true\n")
    with pytest.raises(ConfigError, match="trusted_proxy_mode"):
        boot(monkeypatch, provider="none", demo=True, extra_env={"BIFROST_CONFIG": str(path)})


# ── entrypoints ─────────────────────────────────────────────────────


def test_cli_callback_runs_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost.cli.app import app as cli_app

    monkeypatch.setenv("BIFROST_NOT_A_SETTING", "1")
    result = CliRunner().invoke(cli_app, ["token", "new"])
    assert isinstance(result.exception, ConfigError)


def test_benchmark_main_runs_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost.benchmarks import runner

    monkeypatch.setenv("BIFROST_NOT_A_SETTING", "1")
    with pytest.raises(ConfigError, match="BIFROST_NOT_A_SETTING"):
        runner.main([])


# ── cleanup guards ──────────────────────────────────────────────────


def test_resolved_identity_is_gone() -> None:
    assert not hasattr(BifrostSettings, "resolved_identity")


def test_build_verifier_from_settings_is_gone() -> None:
    from bifrost.api.auth import jwt_verifier

    assert not hasattr(jwt_verifier, "build_verifier_from_settings")


def test_auto_extract_wait_is_read_from_the_validated_field() -> None:
    from bifrost.research import coordinator

    assert not hasattr(coordinator, "_AUTO_EXTRACT_WAIT_S")


# ── allowlist and drift ─────────────────────────────────────────────


@pytest.mark.parametrize(("name", "reader"), sorted(PROCESS_ENV_ONLY.items()))
def test_allowlist_entry_names_the_file_that_reads_it(name: str, reader: str) -> None:
    path = ROOT / reader
    assert path.is_file(), f"{name}: {reader} does not exist"
    assert name in path.read_text(encoding="utf-8"), f"{name} is not mentioned in {reader}"


_NAME = re.compile(r"(?<![A-Za-z0-9_])BIFROST_[A-Z0-9_]+")
_DRIFT_SKIP = ("docs/", "ui/", "tests/", "uv.lock")


def test_every_bifrost_name_in_the_tree_is_a_setting_or_allowlisted() -> None:
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    unknown: dict[str, list[str]] = {}
    for rel in files:
        if rel.startswith(_DRIFT_SKIP):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for name in _NAME.findall(text):
            if name.endswith("_") or is_known_name(name):
                continue
            unknown.setdefault(name, []).append(rel)
    assert not unknown, "\n".join(f"{n}: {sorted(set(f))}" for n, f in sorted(unknown.items()))

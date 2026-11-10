"""Follow-ups to the unknown-setting check: exact-case process-env names,
value-independence of removed names, and unreadable yaml."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from bifrost.cli.config import BifrostSettings, reset_settings, validate_startup
from bifrost.errors import ConfigError


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(BifrostSettings.model_config, "env_file", None)
    monkeypatch.setenv("BIFROST_CONFIG", "")
    reset_settings()
    yield
    reset_settings()


@pytest.mark.parametrize("name", ["bifrost_config", "Bifrost_Handovers_Dir"])
def test_process_env_only_names_are_matched_in_exact_case(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "/tmp/x")
    with pytest.raises(ConfigError, match=name):
        validate_startup()


REMOVED = [
    ("BIFROST_API_TOKEN", "api_token"),
    ("BIFROST_ADMIN_TOKEN", "admin_token"),
    ("BIFROST_ALLOW_OPEN_ADMIN", "allow_open_admin"),
]
VALUES = ["true", "false", ""]


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize(("env_name", "_key"), REMOVED)
def test_removed_name_in_env_fails_whatever_its_value(
    monkeypatch: pytest.MonkeyPatch, env_name: str, _key: str, value: str
) -> None:
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ConfigError, match=env_name):
        validate_startup()


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize(("env_name", "_key"), REMOVED)
def test_removed_name_in_dotenv_fails_whatever_its_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_name: str, _key: str, value: str
) -> None:
    path = tmp_path / ".env"
    path.write_text(f"{env_name}={value}\n", encoding="utf-8")
    monkeypatch.setitem(BifrostSettings.model_config, "env_file", str(path))
    with pytest.raises(ConfigError, match=env_name):
        validate_startup()


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize(("_env", "key"), REMOVED)
def test_removed_name_in_yaml_fails_whatever_its_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _env: str, key: str, value: str
) -> None:
    path = tmp_path / "bifrost.yaml"
    path.write_text(f'{key}: "{value}"\n', encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONFIG", str(path))
    with pytest.raises(ConfigError, match=key):
        validate_startup()


def test_unparseable_yaml_is_a_config_error_naming_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "bifrost.yaml"
    path.write_text("identity: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONFIG", str(path))
    with pytest.raises(ConfigError, match=str(path)):
        validate_startup()

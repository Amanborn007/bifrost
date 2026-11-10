"""The local-dev database defaults must name one database everywhere."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.engine import make_url

from bifrost.cli.commands.init import DEFAULT_CONFIG_TEMPLATE
from bifrost.cli.config import BifrostSettings

ROOT = Path(__file__).resolve().parent.parent


def _compose_defaults() -> dict[str, str]:
    text = (ROOT / "docker-compose.yml").read_text()
    return dict(re.findall(r"\$\{(\w+):-([^}]*)\}", text))


def _parts(url: str) -> tuple[str, str, str, int, str]:
    u = make_url(url)
    return (u.username, u.password, u.host, u.port, u.database)  # type: ignore[return-value]


def _compose_parts() -> tuple[str, str, str, int, str]:
    d = _compose_defaults()
    return (
        d["POSTGRES_USER"],
        d["POSTGRES_PASSWORD"],
        "localhost",
        int(d["POSTGRES_PORT"]),
        d["POSTGRES_DB"],
    )


def test_settings_default_matches_compose():
    default = BifrostSettings.model_fields["database_url"].default
    assert _parts(default) == _compose_parts()


def test_production_sentinel_equals_settings_default():
    default = BifrostSettings.model_fields["database_url"].default
    assert default == BifrostSettings._DEV_DATABASE_URL_DEFAULT


def test_alembic_ini_matches_compose():
    text = (ROOT / "alembic.ini").read_text()
    url = re.search(r"^sqlalchemy\.url\s*=\s*(\S+)", text, re.M).group(1)
    assert _parts(url) == _compose_parts()


def test_init_template_matches_compose():
    m = re.search(r"BIFROST_DATABASE_URL=(\S+)", DEFAULT_CONFIG_TEMPLATE)
    assert m is not None
    assert _parts(m.group(1)) == _compose_parts()


def test_ci_test_database_urls_use_compose_credentials():
    user, password, _host, _port, _db = _compose_parts()
    for name in ("pr-check.yml", "leak-gate.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        url = re.search(r"TEST_DATABASE_URL:\s*(\S+)", text).group(1)
        assert _parts(url)[:2] == (user, password), name


def test_env_example_matches_compose():
    text = (ROOT / ".env.example").read_text()
    url = re.search(r"^#?\s*BIFROST_DATABASE_URL=(\S+)", text, re.M).group(1)
    assert _parts(url) == _compose_parts()

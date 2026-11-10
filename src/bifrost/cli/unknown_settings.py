"""Startup check for BIFROST settings that nothing reads.

pydantic-settings ignores keys it does not know, so a typo leaves a setting
(possibly a gate) silently at its default. The check scans the process
environment, the ``.env`` file and the top level of bifrost.yaml. Nested
yaml keys are already rejected by the sections (``extra="forbid"``).
"""

from __future__ import annotations

import difflib
import os
import types
import typing
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel

from bifrost.errors import ConfigError

if TYPE_CHECKING:
    from bifrost.cli.config import BifrostSettings

ENV_PREFIX = "BIFROST_"
DELIMITER = "__"

# Names read straight from the process environment, outside the settings model.
# Each value is the file that reads it; a test checks the file still does.
PROCESS_ENV_ONLY: dict[str, str] = {
    "BIFROST_CONFIG": "src/bifrost/cli/config.py",
    "BIFROST_ALLOW_PRIVATE_GIT_URLS": "src/bifrost/adapters/git.py",
    "BIFROST_HANDOVERS_DIR": "src/bifrost/collaboration/handover_generator.py",
    "BIFROST_DISABLE_AUTO_ARCHIVE": "src/bifrost/actions/auto_archive.py",
    "BIFROST_DISABLE_HANDOVER_BACKFILL": "src/bifrost/collaboration/handover_backfill.py",
    "BIFROST_SESSION_KEY_FILE": "src/bifrost/api/_cookie_signing.py",
    "BIFROST_EMBEDDING_MODEL": "src/bifrost/research/embeddings.py",
    "BIFROST_EMBEDDING_CACHE": "src/bifrost/research/embeddings.py",
    "BIFROST_BENCHMARK_OWNER_SESSION_ID": "src/bifrost/benchmarks/runner.py",
    "BIFROST_SKIP_DB_MIGRATIONS": "tests/conftest.py",
    "BIFROST_SYNC_TEST_URL": "tests/test_learning_sync.py",
    "BIFROST_SMOKE_BASE": "scripts/screen_smoke.py",
    "BIFROST_SMOKE_EMAIL": "scripts/screen_smoke.py",
    "BIFROST_SMOKE_SCOPE_ALL": "scripts/chat_smoke.py",
    "BIFROST_API": "scripts/demo_validation.py",
}

# Generic names an older release read straight from the environment; now settings-only.
RENAMED_ENV: dict[str, str] = {
    "JIRA_BASE_URL": "BIFROST_SOURCES__JIRA__BASE_URL",
    "JIRA_EMAIL": "BIFROST_SOURCES__JIRA__EMAIL",
    "JIRA_API_TOKEN": "BIFROST_SOURCES__JIRA__API_TOKEN",
}

_HINT_CUTOFF = 0.8


@dataclass(frozen=True)
class KnownNames:
    """Env names a settings model accepts."""

    exact: frozenset[str]
    free_form_prefixes: tuple[str, ...]
    top_level_fields: frozenset[str]


@dataclass(frozen=True)
class UnknownSetting:
    name: str
    source: str
    reason: str = ""
    hint: str | None = None

    def render(self) -> str:
        text = f"{self.name} ({self.source})"
        if self.reason:
            text += f": {self.reason}"
        if self.hint:
            text += f"{'; ' if self.reason else ': '}did you mean {self.hint}?"
        return text


def _classify(annotation: Any) -> tuple[str, type[BaseModel] | None]:
    """``("model", cls)``, ``("dict", None)`` or ``("plain", None)``."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            return _classify(members[0])
        return "plain", None
    if typing.get_origin(annotation) is dict or annotation is dict:
        return "dict", None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "model", annotation
    return "plain", None


def collect_known_names(model: type[BaseModel], prefix: str = ENV_PREFIX) -> KnownNames:
    """Flatten ``model`` into env names joined by ``__``.

    Model-typed fields recurse; dict-typed fields accept any suffix; every
    other field accepts no suffix.
    """
    exact: set[str] = set()
    free: list[str] = []
    for name, field in model.model_fields.items():
        if field.alias is not None or field.validation_alias is not None:
            raise ConfigError(
                f"{model.__name__}.{name} has an alias; the unknown-setting check "
                "assumes a field's env name is its field name"
            )
        env_name = f"{prefix}{name.upper()}"
        exact.add(env_name)
        kind, nested = _classify(field.annotation)
        if kind == "dict":
            free.append(env_name)
        elif kind == "model" and nested is not None:
            child = collect_known_names(nested, f"{env_name}{DELIMITER}")
            exact |= child.exact
            free.extend(child.free_form_prefixes)
    return KnownNames(frozenset(exact), tuple(free), frozenset(model.model_fields))


@lru_cache(maxsize=1)
def _settings_names() -> KnownNames:
    from bifrost.cli.config import BifrostSettings

    return collect_known_names(BifrostSettings)


def is_known_name(name: str) -> bool:
    """True for a settings field, a free-form dict key or a process-env-only name."""
    upper = name.upper()
    names = _settings_names()
    if upper in names.exact or name in PROCESS_ENV_ONLY:
        return True
    return any(upper.startswith(p + DELIMITER) for p in names.free_form_prefixes)


def _hint(name: str, candidates: Iterable[str]) -> str | None:
    match = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=_HINT_CUTOFF)
    return match[0] if match else None


def _env_candidates() -> list[str]:
    return [*_settings_names().exact, *PROCESS_ENV_ONLY]


def _is_bifrost_name(name: str) -> bool:
    return name.upper().startswith(ENV_PREFIX)


def find_unknown_env_names(environ: Mapping[str, str]) -> list[str]:
    """BIFROST_* names in ``environ`` that nothing reads."""
    return [k for k in environ if _is_bifrost_name(k) and not is_known_name(k)]


def _scan_environ(environ: Mapping[str, str]) -> list[UnknownSetting]:
    return [
        UnknownSetting(k, "environment", hint=_hint(k.upper(), _env_candidates()))
        for k in find_unknown_env_names(environ)
    ]


def _env_files(settings_cls: type[BifrostSettings]) -> list[Path]:
    configured = settings_cls.model_config.get("env_file")
    if configured is None:
        return []
    if isinstance(configured, str | os.PathLike):
        return [Path(configured)]
    return [Path(p) for p in configured]


def _scan_dotenv(settings_cls: type[BifrostSettings]) -> list[UnknownSetting]:
    found: list[UnknownSetting] = []
    encoding = settings_cls.model_config.get("env_file_encoding") or "utf-8"
    for path in _env_files(settings_cls):
        if not path.is_file():
            continue
        source = f".env file {path}"
        for key in dotenv_values(path, encoding=encoding):
            if not _is_bifrost_name(key):
                continue
            if key.upper() in PROCESS_ENV_ONLY:
                found.append(
                    UnknownSetting(
                        key, source, "read only from the process environment, not from .env"
                    )
                )
            elif not is_known_name(key):
                found.append(
                    UnknownSetting(key, source, hint=_hint(key.upper(), _env_candidates()))
                )
    return found


def _scan_yaml(path: Path | None) -> list[UnknownSetting]:
    if path is None:
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        return []
    fields = _settings_names().top_level_fields
    return [
        UnknownSetting(str(key), str(path), hint=_hint(str(key), fields))
        for key in data
        if str(key) not in fields
    ]


def _scan_renamed(
    settings_cls: type[BifrostSettings], environ: Mapping[str, str]
) -> list[tuple[str, str]]:
    """``(name, source)`` for each RENAMED_ENV name set in ``environ`` or the .env file."""
    found = [(name, "environment") for name in RENAMED_ENV if name in environ]
    encoding = settings_cls.model_config.get("env_file_encoding") or "utf-8"
    for path in _env_files(settings_cls):
        if path.is_file():
            found.extend(
                (key, f".env file {path}")
                for key in dotenv_values(path, encoding=encoding)
                if key in RENAMED_ENV
            )
    return found


def check_unknown_settings(environ: Mapping[str, str] | None = None) -> None:
    """Raise ``ConfigError`` listing every unknown name and where it was set."""
    from bifrost.cli.config import BifrostSettings, resolve_config_path

    env = os.environ if environ is None else environ
    renamed = _scan_renamed(BifrostSettings, env)
    if renamed:
        lines = "\n".join(f"  - {n} ({src}): set {RENAMED_ENV[n]} instead" for n, src in renamed)
        raise ConfigError(
            f"Renamed settings; Bifrost no longer reads these names:\n{lines}",
            details={"renamed": [n for n, _ in renamed]},
        )
    unknown = [
        *_scan_environ(env),
        *_scan_dotenv(BifrostSettings),
        *_scan_yaml(resolve_config_path()),
    ]
    if not unknown:
        return
    lines = "\n".join(f"  - {u.render()}" for u in unknown)
    raise ConfigError(
        f"Unknown Bifrost settings; nothing reads them, so they would be silently ignored:\n{lines}",
        details={"unknown": [u.name for u in unknown]},
    )

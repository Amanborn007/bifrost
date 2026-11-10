"""Guard: no module, field, route or attribute carries a banned name, and catalog adapters stay pluggable.

The names come from LEAK_DENYLIST via tests/leak_terms.py; these tests skip when it is unset.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from bifrost.api.schemas.chat_settings import (
    ChatSettings,
    PartialChatSettings,
)
from bifrost.cli.config import BifrostSettings
from bifrost.research import confidence_policy
from tests import leak_terms


def _loaded_modules(import_stmt: str) -> list[str]:
    code = (
        "import os, sys\n"
        "os.environ['BIFROST_SKIP_DB_MIGRATIONS'] = '1'\n"
        f"{import_stmt}\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line and leak_terms.matches(line)]


@pytest.mark.parametrize(
    "import_stmt",
    ["import bifrost.research", "import bifrost.research.coordinator"],
)
def test_importing_research_loads_no_banned_module(import_stmt: str) -> None:
    leak_terms.patterns()
    assert _loaded_modules(import_stmt) == []


@pytest.mark.parametrize("model", [ChatSettings, PartialChatSettings])
def test_chat_settings_have_no_banned_fields(model: type) -> None:
    assert [f for f in model.model_fields if leak_terms.matches(f)] == []


def test_ownership_router_has_no_banned_path() -> None:
    from bifrost.api.routes.ownership import router

    assert [r.path for r in router.routes if leak_terms.matches(r.path)] == []


def test_bifrost_settings_have_no_banned_attributes() -> None:
    assert [f for f in BifrostSettings.model_fields if leak_terms.matches(f)] == []


def test_confidence_policy_knows_manifest_and_no_banned_name() -> None:
    assert confidence_policy.classify("manifest") == ("confirmed", 0.88)
    names = [*confidence_policy._TIER_MAP, *dir(confidence_policy)]
    assert [n for n in names if leak_terms.matches(n)] == []


def test_default_edge_types_include_service_level_edges() -> None:
    edge_types = ChatSettings().edge_types
    for t in ("calls_service", "publishes_to", "shares_topic"):
        assert t in edge_types

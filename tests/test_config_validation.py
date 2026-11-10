"""Tests for the fail-fast config gate.

The gate folds three previously-scattered ``os.getenv`` knobs into bounded
``BifrostSettings`` fields and added ``validate_startup()`` as the single
boot-time config gate. The contract these lock:

  * the new knobs read from the validated surface (with bounds),
  * an out-of-range / non-numeric value fails *fast* (at boot) as a
    ``ConfigError`` — not a silent clamp deep in a request,
  * ``validate_startup()`` aggregates every problem into one message.
"""

from __future__ import annotations

import pytest

from bifrost.cli.config import (
    BifrostSettings,
    get_settings,
    reset_settings,
    validate_startup,
)
from bifrost.errors import ConfigError


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_settings()
    yield
    reset_settings()


# ── New bounded fields ───────────────────────────────────────────────


def test_new_knob_defaults() -> None:
    s = BifrostSettings()
    assert s.auto_extract_wait_s == 90.0
    assert s.cache_sim_threshold == 0.85
    assert s.doubt_cluster_threshold == 0.85


def test_knobs_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("BIFROST_AUTO_EXTRACT_WAIT_S", "30")
    monkeypatch.setenv("BIFROST_CACHE_SIM_THRESHOLD", "0.7")
    monkeypatch.setenv("BIFROST_DOUBT_CLUSTER_THRESHOLD", "0.6")
    s = BifrostSettings()
    assert s.auto_extract_wait_s == 30.0
    assert s.cache_sim_threshold == 0.7
    assert s.doubt_cluster_threshold == 0.6


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("BIFROST_CACHE_SIM_THRESHOLD", "2.0"),  # > 1.0
        ("BIFROST_CACHE_SIM_THRESHOLD", "-0.1"),  # < 0.0
        ("BIFROST_DOUBT_CLUSTER_THRESHOLD", "5"),  # > 1.0
        ("BIFROST_AUTO_EXTRACT_WAIT_S", "-1"),  # < 0.0
    ],
)
def test_out_of_range_rejected(monkeypatch, env, value) -> None:
    """Bounds are enforced — no silent clamp like the old raw os.getenv path."""
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError):  # pydantic ValidationError is a ValueError
        BifrostSettings()


@pytest.mark.parametrize(
    "value",
    ["not-a-number", ""],
)
def test_non_numeric_threshold_rejected(monkeypatch, value) -> None:
    monkeypatch.setenv("BIFROST_CACHE_SIM_THRESHOLD", value)
    with pytest.raises(ValueError):
        BifrostSettings()


# ── validate_startup gate ────────────────────────────────────────────


def test_validate_startup_returns_settings_when_valid() -> None:
    s = validate_startup()
    assert isinstance(s, BifrostSettings)
    # Returns the cached singleton.
    assert s is get_settings()


def test_validate_startup_raises_configerror_on_bad_value(monkeypatch) -> None:
    monkeypatch.setenv("BIFROST_CACHE_SIM_THRESHOLD", "2.0")
    with pytest.raises(ConfigError) as ei:
        validate_startup()
    err = ei.value
    assert err.code == "config.invalid"
    assert err.http_status == 500
    assert err.retryable is False
    # The field name appears in the aggregated message + structured details.
    assert "cache_sim_threshold" in str(err)
    assert err.details.get("errors")


def test_validate_startup_aggregates_multiple_problems(monkeypatch) -> None:
    """All bad fields are reported at once, not just the first."""
    monkeypatch.setenv("BIFROST_CACHE_SIM_THRESHOLD", "2.0")
    monkeypatch.setenv("BIFROST_DOUBT_CLUSTER_THRESHOLD", "9.0")
    with pytest.raises(ConfigError) as ei:
        validate_startup()
    msg = str(ei.value)
    assert "cache_sim_threshold" in msg
    assert "doubt_cluster_threshold" in msg

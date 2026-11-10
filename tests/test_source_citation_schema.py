"""Regression tests for ``SourceCitation`` legacy-data deserialization.

The chat-not-loading bug: ``conversation_messages.sources``
JSONB rows from older runs stored only ``{'urn': 'urn:bifrost:...'}``
without a ``name``. The route layer's ``SourceCitation(**s)`` raised
``ValidationError: name field required`` and returned 500. The fix
makes ``name`` optional with a URN-tail-derived default.

These tests pin that legacy shapes deserialize cleanly and that
explicit names still win over the auto-derivation."""

from __future__ import annotations

import pytest

from bifrost.api.schemas.conversations import SourceCitation


def test_legacy_urn_only_shape_deserializes() -> None:
    """The shape that broke production:
    ``{'urn': 'urn:bifrost:service:OrderService'}`` should round-trip with
    name auto-derived to ``OrderService``."""
    sc = SourceCitation(urn="urn:bifrost:service:OrderService")
    assert sc.urn == "urn:bifrost:service:OrderService"
    assert sc.name == "OrderService"


def test_explicit_name_wins_over_derivation() -> None:
    """When the row carries an explicit ``name``, do NOT overwrite
    it with the URN tail — the coordinator's emitted name is more
    accurate (it knows the human label)."""
    sc = SourceCitation(urn="urn:bifrost:service:foo", name="Foo Service")
    assert sc.name == "Foo Service"


def test_urn_without_separator_falls_back_to_urn() -> None:
    """Defensive: a URN that doesn't have a ``:`` separator still
    renders something the FE can show, not an empty string."""
    sc = SourceCitation(urn="https://example.com/page")
    assert sc.name == "https://example.com/page"


def test_empty_urn_yields_unknown_placeholder() -> None:
    """Belt-and-braces: an empty URN never crashes; FE shows
    ``(unknown)`` so the operator notices the bad row."""
    sc = SourceCitation(urn="")
    assert sc.name == "(unknown)"


def test_multi_segment_urn_uses_last_segment() -> None:
    """``urn:bifrost:repo/OrderService/component/Foo`` → ``component/Foo``
    (the tail after the last ``:``). This is the worst-case shape;
    callers can override with explicit name when they want better."""
    sc = SourceCitation(urn="urn:bifrost:repo:OrderService:component:Bar")
    assert sc.name == "Bar"


def test_legacy_with_relevance_string_still_works() -> None:
    """An older row carries ``relevance`` as a string label
    ('high'/'medium'/'low'). Schema accepts both float and str."""
    sc = SourceCitation(
        urn="urn:bifrost:service:OrderService",
        relevance="high",
    )
    assert sc.relevance == "high"
    assert sc.name == "OrderService"


@pytest.mark.parametrize(
    "raw,expected_name",
    [
        ({"urn": "urn:bifrost:service:OrderService"}, "OrderService"),
        ({"urn": "urn:bifrost:repo:foo", "name": "Foo"}, "Foo"),
        ({"urn": "urn:bifrost:repo:foo", "files": ["a.py"]}, "foo"),
        # extra unknown fields don't break (Pydantic ignores them)
        ({"urn": "urn:bifrost:service:X", "weird": 1}, "X"),
    ],
)
def test_dict_unwrapping_legacy_and_current_shapes(
    raw: dict,
    expected_name: str,
) -> None:
    """End-to-end ``SourceCitation(**dict)`` over both shapes — the
    exact path the route layer takes when deserializing JSONB."""
    sc = SourceCitation(**raw)
    assert sc.name == expected_name

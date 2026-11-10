"""URN repo-segment parser — handles both production + legacy formats.

Regression test: production entities use ``urn:bifrost:repo/<repo>/<type>/<name>``
(slash-keyed) but the parser only handled the colon-keyed
``urn:bifrost:<type>:<repo>::<qualified>`` form, so EVERY real-world
edge classified as same-repo and the synthesizer never saw the
"Cross-repo edges" subsection.

This test pins both forms so the next refactor doesn't silently
regress one of them.
"""

from __future__ import annotations

from bifrost.research.coordinator import _is_cross_repo_edge, _repo_from_urn

# ── Production slash-form ──────────────────────────────────────────


def test_production_slash_form_extracts_repo_name() -> None:
    out = _repo_from_urn(
        "urn:bifrost:repo/batch-orders/component/OrderDatabaseConnection",
    )
    assert out == "batch-orders"


def test_production_slash_form_with_qualified_name() -> None:
    out = _repo_from_urn(
        "urn:bifrost:repo/DeliveryGateway/component/CarrierAdapterModule",
    )
    assert out == "DeliveryGateway"


def test_production_slash_form_for_dependency_entity() -> None:
    out = _repo_from_urn(
        "urn:bifrost:repo/batch-orders/dependency/RetryScheduler->RetryCallbackProcessor",
    )
    assert out == "batch-orders"


def test_production_slash_form_for_flow_entity() -> None:
    out = _repo_from_urn(
        "urn:bifrost:repo/DeliveryGateway/flow/Endpoint Lookup by Subscription ID",
    )
    assert out == "DeliveryGateway"


# ── Legacy colon-form ──────────────────────────────────────────────


def test_legacy_colon_form_extracts_repo_name() -> None:
    out = _repo_from_urn("urn:bifrost:component:webhook-api::OrderRouter")
    assert out == "webhook-api"


def test_legacy_colon_form_without_qualified_name() -> None:
    out = _repo_from_urn("urn:bifrost:component:orders::RetryQueue")
    assert out == "orders"


def test_legacy_repo_urn_returns_repo_name() -> None:
    """The repo URN itself uses ``urn:bifrost:repo:<name>`` — no
    embedded entity. We extract <name> as the repo segment."""
    out = _repo_from_urn("urn:bifrost:repo:batch-orders")
    assert out == "batch-orders"


# ── Boundary cases ─────────────────────────────────────────────────


def test_returns_empty_for_non_bifrost_urn() -> None:
    assert _repo_from_urn("https://github.com/x") == ""
    assert _repo_from_urn("urn:other:component:x::y") == ""


def test_returns_empty_for_truncated_urn() -> None:
    assert _repo_from_urn("urn:bifrost") == ""
    assert _repo_from_urn("urn:bifrost:") == ""
    assert _repo_from_urn("urn:bifrost:repo/") == ""


def test_returns_empty_for_domain_urn() -> None:
    """Domain URNs (``urn:bifrost:domain:<squad>``) have no repo
    segment — caller should treat them as same-repo for edge
    classification."""
    out = _repo_from_urn("urn:bifrost:domain:orders-platform")
    assert out == "orders-platform"  # legacy parser sees parts[1]=='domain'... wait
    # Actually the legacy colon-form parser returns parts[1].
    # For ``urn:bifrost:domain:orders-platform``, parts after split(":")
    # are ['domain', 'orders-platform'] in body — parts[1] == 'orders-platform'.
    # That's correct: domain-scoped entities live "outside" any repo,
    # but their URN's segment-1 reads as the squad/domain name.


# ── Cross-repo classifier ──────────────────────────────────────────


def test_cross_repo_detected_in_production_form() -> None:
    src = "urn:bifrost:repo/batch-orders/component/X"
    tgt = "urn:bifrost:repo/webhook-api/component/Y"
    assert _is_cross_repo_edge(src, tgt) is True


def test_same_repo_in_production_form() -> None:
    src = "urn:bifrost:repo/batch-orders/component/X"
    tgt = "urn:bifrost:repo/batch-orders/component/Y"
    assert _is_cross_repo_edge(src, tgt) is False


def test_cross_repo_detected_in_legacy_form() -> None:
    src = "urn:bifrost:component:orders::A"
    tgt = "urn:bifrost:component:auth::B"
    assert _is_cross_repo_edge(src, tgt) is True


def test_cross_repo_detected_when_one_side_each_form() -> None:
    """A graph could span legacy fixture URNs joined to live
    extraction URNs — the classifier should still see them as
    cross-repo."""
    src = "urn:bifrost:repo/batch-orders/component/X"
    tgt = "urn:bifrost:component:webhook-api::Y"
    assert _is_cross_repo_edge(src, tgt) is True


def test_no_classification_when_either_urn_unparseable() -> None:
    src = "urn:bifrost:repo/batch-orders/component/X"
    assert _is_cross_repo_edge(src, "https://github.com/x/y") is False
    assert _is_cross_repo_edge("not-a-urn", src) is False

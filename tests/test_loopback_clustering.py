"""Unit tests for ``loopback._cluster_doubts`` .

Clustering is embedding-similarity based, replacing normalized-string
clustering, using sentence-transformers/all-MiniLM-L6-v2. These tests
assert the new behavior:

  * Semantically-similar paraphrases cluster together at a high
    threshold (the whole point of the upgrade) — normalized-string
    clustering would never have clustered them.
  * Topically-unrelated questions stay in separate clusters at the
    same threshold.
  * The normalized-string fast-path still works when the embedder is
    unavailable (we monkeypatch ``embed_texts`` to raise).
  * The function never raises — embedding errors degrade gracefully.

DB-free: we hand-build ``CollabDoubt`` instances without any session.

Threshold choice: the test pairs use a 0.6 threshold (set via the
``BIFROST_DOUBT_CLUSTER_THRESHOLD`` env var) which sits above the
unrelated-pair noise floor (~0.13-0.46 measured on real MiniLM
output) and below paraphrase similarity (~0.79-0.94 measured). The
production default is 0.85; the test exercises the mechanism rather
than the production-default tuning so it stays stable across MiniLM
revisions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from bifrost.cli.config import reset_settings
from bifrost.collaboration.loopback import _cluster_doubts
from bifrost.storage.models import CollabDoubt


@pytest.fixture(autouse=True)
def _reset_settings():
    """The cluster threshold comes from the cached settings
    singleton, so tests that set BIFROST_DOUBT_CLUSTER_THRESHOLD must
    reset it (and clean up after) — see each setenv + reset_settings()."""
    reset_settings()
    yield
    reset_settings()


def _make_doubt(
    *,
    question: str,
    session_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    status: str = "unresolved",
) -> CollabDoubt:
    """Build a CollabDoubt without touching the DB."""
    return CollabDoubt(
        id=uuid.uuid4(),
        session_id=session_id or uuid.uuid4(),
        question=question,
        status=status,
        created_at=created_at or datetime.now(UTC),
    )


def test_paraphrased_questions_cluster_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two paraphrases of the same intent cluster into one bucket.

    These two questions ask about the same thing with completely
    disjoint surface forms — normalized-string clustering would have put
    them in two separate clusters. Embedding-similarity clustering merges
    them.

    We use a 0.6 threshold (above the unrelated-pair noise floor,
    well below paraphrase similarity) — this validates the mechanism
    is wired in without depending on MiniLM's specific calibration
    against an arbitrary 0.85 cutoff.
    """
    monkeypatch.setenv("BIFROST_DOUBT_CLUSTER_THRESHOLD", "0.6")
    reset_settings()

    s1, s2 = uuid.uuid4(), uuid.uuid4()
    doubts = [
        _make_doubt(
            question="How are orders retried on failure?",
            session_id=s1,
        ),
        _make_doubt(
            question="What's the retry policy for failed orders?",
            session_id=s2,
        ),
    ]

    clusters = _cluster_doubts(doubts)

    assert len(clusters) == 1, (
        f"expected 1 cluster of paraphrases, got {len(clusters)}: "
        f"{[c.representative_question for c in clusters]}"
    )
    assert clusters[0].occurrence_count == 2
    assert set(clusters[0].session_ids) == {str(s1), str(s2)}


def test_unrelated_questions_do_not_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two unrelated questions on different topics stay in separate
    clusters even at the same threshold that merged the paraphrases
    above. If MiniLM-similarity false-positives unrelated questions,
    the whole feature is worse than the normalized-string baseline.
    """
    monkeypatch.setenv("BIFROST_DOUBT_CLUSTER_THRESHOLD", "0.6")
    reset_settings()

    doubts = [
        _make_doubt(
            question="What is the SLA for reshipments?",
            session_id=uuid.uuid4(),
        ),
        _make_doubt(
            question="Where are Kafka topics declared in this repo?",
            session_id=uuid.uuid4(),
        ),
    ]

    clusters = _cluster_doubts(doubts)

    assert len(clusters) == 2, (
        f"expected 2 separate clusters for unrelated questions, got {len(clusters)}"
    )
    reps = {c.representative_question for c in clusters}
    assert "What is the SLA for reshipments?" in reps
    assert "Where are Kafka topics declared in this repo?" in reps


def test_normalized_fast_path_clusters_identical_text() -> None:
    """Two doubts that normalize-equal must cluster — exercises the
    pre-embedding bucket step. Uses production defaults."""
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    doubts = [
        _make_doubt(
            question="What is the SLA for reshipments?",
            session_id=s1,
        ),
        _make_doubt(
            question="what is the sla for reshipments",  # only case/punct.
            session_id=s2,
        ),
    ]

    clusters = _cluster_doubts(doubts)

    assert len(clusters) == 1
    assert clusters[0].occurrence_count == 2
    assert set(clusters[0].session_ids) == {str(s1), str(s2)}


def test_embedding_failure_falls_back_to_normalized_clustering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``embed_texts`` blows up (model load error, OOM, etc.) we
    fall back to normalized-string clustering rather than raising.
    The function is on the hot handover-detail GET path — a 500 here
    would break the UI for a clustering-quality regression."""

    def _boom(*_a, **_kw):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated model load failure")

    monkeypatch.setattr(
        "bifrost.research.embeddings.embed_texts",
        _boom,
    )

    doubts = [
        _make_doubt(
            question="What is the SLA for reshipments?",
            session_id=uuid.uuid4(),
        ),
        _make_doubt(
            question="How fast does reshipment processing happen?",
            session_id=uuid.uuid4(),
        ),
    ]

    # Should NOT raise — falls back to normalized buckets.
    clusters = _cluster_doubts(doubts)

    # Two distinct normalized strings → two clusters in fallback mode.
    # (Embedding clustering would have merged them at low threshold;
    # we're verifying the fallback explicitly does the normalized-string thing.)
    assert len(clusters) == 2


def test_threshold_env_var_controls_clustering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BIFROST_DOUBT_CLUSTER_THRESHOLD=0.99`` should make even close
    paraphrases stay in separate clusters. Validates the env var is
    actually wired in and read on each invocation."""
    monkeypatch.setenv("BIFROST_DOUBT_CLUSTER_THRESHOLD", "0.99")
    reset_settings()

    doubts = [
        _make_doubt(
            question="How are orders retried on failure?",
            session_id=uuid.uuid4(),
        ),
        _make_doubt(
            question="What's the retry policy for failed orders?",
            session_id=uuid.uuid4(),
        ),
    ]

    clusters = _cluster_doubts(doubts)

    # 0.99 is virtually-identical-only. Paraphrases will not clear it.
    assert len(clusters) == 2

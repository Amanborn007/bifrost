"""A GDocs agent error counts as an evidence gap like every other source."""

from __future__ import annotations

from bifrost.research.coordinator import RepoBundle, _has_evidence_gap_in_bundles
from bifrost.research.gdocs_agent import GDocsReport


def test_gdocs_error_is_an_evidence_gap() -> None:
    gdocs = GDocsReport(
        repo_id="r1",
        repo_name="svc",
        gdoc_folder_url=None,
        relevance="none",
        summary="",
        error="drive search failed",
    )
    bundle = RepoBundle(repo_id="r1", repo_name="svc", gdocs=gdocs)

    gap = _has_evidence_gap_in_bundles([bundle])

    assert gap is not None
    assert "svc.gdocs" in gap

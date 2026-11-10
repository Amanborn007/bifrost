"""Unit tests for ``bifrost.actions.jira_enrich`` parsers.

The three extractors are where real user-visible value lives:
  * ``extract_acceptance_criteria`` — pulls bullet-pointed ACs out of
    a Jira description so tasks get pre-populated acceptance criteria
  * ``extract_confluence_links`` — finds Atlassian Wiki URLs referenced
    by the ticket (surfaced as "linked docs" in the UI)
  * ``extract_related_jira_keys`` — finds other JIRA-KEY references
    so a task knows what tickets it's entangled with

These tests pin parser behavior against realistic Jira descriptions.
Pure-logic tests, no network, no DB.
"""

from __future__ import annotations

from bifrost.actions.jira_enrich import (
    extract_acceptance_criteria,
    extract_confluence_links,
    extract_related_jira_keys,
)


class TestExtractAcceptanceCriteria:
    """ACs can come in ~5 different shapes. All must work."""

    def test_markdown_heading_with_checkbox_bullets(self) -> None:
        desc = """
## Acceptance Criteria
- [ ] First requirement
- [ ] Second requirement
- [x] Done already
"""
        items = extract_acceptance_criteria(desc)
        assert items == [
            "First requirement",
            "Second requirement",
            "Done already",
        ]

    def test_bold_header_with_plain_bullets(self) -> None:
        desc = """
**Acceptance Criteria**
- New table exists
- Dual-write is live
- Reader flip works via flag
"""
        items = extract_acceptance_criteria(desc)
        assert len(items) == 3
        assert "New table exists" in items
        assert "Reader flip works via flag" in items

    def test_ac_shorthand_header(self) -> None:
        desc = """
AC:
- Item one
- Item two
"""
        items = extract_acceptance_criteria(desc)
        assert items == ["Item one", "Item two"]

    def test_definition_of_done_header(self) -> None:
        desc = """
## Definition of Done
- PR merged to main
- Metrics show p99 parity
"""
        items = extract_acceptance_criteria(desc)
        assert len(items) == 2
        assert "PR merged to main" in items

    def test_stops_at_next_heading(self) -> None:
        """ACs shouldn't bleed into the next section."""
        desc = """
## Acceptance Criteria
- Real AC

## Risks
- not an AC
- also not an AC
"""
        items = extract_acceptance_criteria(desc)
        assert items == ["Real AC"]

    def test_numbered_bullets(self) -> None:
        desc = """
## Acceptance Criteria
1. First numbered
2. Second numbered
"""
        items = extract_acceptance_criteria(desc)
        assert items == ["First numbered", "Second numbered"]

    def test_dedupes_identical_items(self) -> None:
        desc = """
## Acceptance Criteria
- same
- same
- different
"""
        items = extract_acceptance_criteria(desc)
        assert items == ["same", "different"]

    def test_respects_max_items(self) -> None:
        """Don't explode memory on a 200-AC description."""
        items_text = "\n".join(f"- item {i}" for i in range(50))
        desc = f"## Acceptance Criteria\n{items_text}\n"
        items = extract_acceptance_criteria(desc, max_items=10)
        assert len(items) == 10

    def test_empty_description_returns_empty_list(self) -> None:
        assert extract_acceptance_criteria("") == []
        # The parser treats None-ish gracefully via falsy check
        assert extract_acceptance_criteria("   ") == []

    def test_description_with_no_ac_header_and_no_checkboxes(self) -> None:
        """Don't invent ACs from a generic bullet list — only AC sections
        or explicit checkbox bullets count."""
        desc = """
Here's a bunch of stuff we did:
- item a
- item b

And some context.
"""
        assert extract_acceptance_criteria(desc) == []

    def test_standalone_checkbox_list_without_header(self) -> None:
        """Fallback: checkbox bullets anywhere count even without a header."""
        desc = """
We need to:
- [ ] do thing one
- [ ] do thing two
"""
        items = extract_acceptance_criteria(desc)
        assert items == ["do thing one", "do thing two"]


class TestExtractConfluenceLinks:
    """Confluence URLs get surfaced as "linked docs" in the task UI."""

    def test_single_wiki_url(self) -> None:
        desc = "See https://acmeco.atlassian.net/wiki/spaces/ENG/pages/12345/Design for context."
        urls = extract_confluence_links(desc)
        assert urls == [
            "https://acmeco.atlassian.net/wiki/spaces/ENG/pages/12345/Design",
        ]

    def test_multiple_unique_urls(self) -> None:
        desc = """
Design: https://acmeco.atlassian.net/wiki/spaces/ENG/pages/1/a
Rollout: https://acmeco.atlassian.net/wiki/spaces/OPS/pages/2/b
"""
        urls = extract_confluence_links(desc)
        assert len(urls) == 2
        assert urls[0].endswith("/1/a")
        assert urls[1].endswith("/2/b")

    def test_dedupes_same_url_mentioned_twice(self) -> None:
        url = "https://acmeco.atlassian.net/wiki/spaces/ENG/pages/1"
        desc = f"{url} - see also {url}"
        assert extract_confluence_links(desc) == [url]

    def test_ignores_non_confluence_urls(self) -> None:
        desc = """
https://github.com/acmeco/repo/pull/1
https://acmeco.atlassian.net/browse/ABC-1
(the second is a Jira URL, not Confluence)
"""
        assert extract_confluence_links(desc) == []

    def test_empty_returns_empty(self) -> None:
        assert extract_confluence_links("") == []


class TestExtractRelatedJiraKeys:
    """Jira keys mentioned in description feed the "related tickets"
    panel. The detector must exclude the task's own key (passed via
    ``skip``)."""

    def test_url_form(self) -> None:
        desc = "Blocked by https://acmeco.atlassian.net/browse/ABC-123"
        keys = extract_related_jira_keys(desc)
        assert keys == ["ABC-123"]

    def test_bare_key_form(self) -> None:
        desc = "Related to PLAT-7788 and DLVR-42"
        keys = extract_related_jira_keys(desc)
        assert set(keys) == {"PLAT-7788", "DLVR-42"}

    def test_skip_excludes_own_key(self) -> None:
        desc = "This ticket is ABC-1. Blocked by ABC-2 and DEF-3."
        keys = extract_related_jira_keys(desc, skip="ABC-1")
        assert "ABC-1" not in keys
        assert "ABC-2" in keys
        assert "DEF-3" in keys

    def test_dedupes_same_key_mentioned_twice(self) -> None:
        desc = "Related to ABC-1 which blocks ABC-1 ↩"
        keys = extract_related_jira_keys(desc)
        assert keys == ["ABC-1"]

    def test_ignores_inline_non_keys(self) -> None:
        """Strings that look like patterns but aren't keys."""
        # Lowercase prefixes, single-digit suffix with no hyphen, etc.
        desc = "abc-123 and Abc-123 and 123-abc should not match"
        keys = extract_related_jira_keys(desc)
        assert keys == []

    def test_url_and_bare_coexist(self) -> None:
        desc = """
See https://acmeco.atlassian.net/browse/ABC-1 for context.
Also PLAT-2 and DLVR-3.
"""
        keys = extract_related_jira_keys(desc)
        # URL-form first (preserves discovery order), then bare keys
        assert keys[0] == "ABC-1"
        assert "PLAT-2" in keys
        assert "DLVR-3" in keys

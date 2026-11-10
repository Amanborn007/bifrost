"""Context builder — assemble relevant entities into bounded LLM context.

Implements Knowledge Index Compression (≤5K tokens) and Capability-Based
Routing (select entity types relevant to the query domain).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Token estimation ──────────────────────────────────────────────────
# Anthropic's tokenizer averages ~3.5 chars per token for English code.
# We use a conservative 4 chars/token to stay within budget.

CHARS_PER_TOKEN = 4

# ── Query-to-entity-type routing ──────────────────────────────────────

_QUERY_TYPE_HINTS: dict[str, list[str]] = {
    "flow": ["flow", "flow_segment"],
    "dependency": ["dependency"],
    "component": ["component"],
    "interface": ["interface"],
    "api": ["component", "interface", "flow"],
    "how does": ["flow", "component"],
    "what depends": ["dependency"],
    "architecture": ["component", "dependency", "flow"],
    "trace": ["flow", "flow_segment"],
    "call": ["flow", "component", "interface"],
    "import": ["dependency"],
    "relationship": ["dependency", "component"],
}


def _infer_relevant_types(query: str) -> list[str] | None:
    """Return entity types most relevant to the query, or None for all types."""
    query_lower = query.lower()
    for keyword, types in _QUERY_TYPE_HINTS.items():
        if keyword in query_lower:
            return types
    return None  # no specific routing — use all types


# ── Relevance scoring ─────────────────────────────────────────────────


def _score_entity(entity: dict[str, Any], query_terms: set[str]) -> float:
    """Score an entity by keyword overlap with query terms.

    Returns a score in [0, 1] range.
    """
    name = (entity.get("name") or "").lower()
    urn = (entity.get("urn") or "").lower()
    entity_type = (entity.get("entity_type") or "").lower()
    summary = (entity.get("summary") or "").lower()
    metadata = str(entity.get("metadata") or entity.get("entity_metadata") or "").lower()

    searchable = f"{name} {urn} {entity_type} {summary} {metadata}"
    if not query_terms:
        return 0.0

    matches = sum(1 for term in query_terms if term in searchable)
    return matches / len(query_terms)


# ── Entity formatting ─────────────────────────────────────────────────


def _format_entity(entity: dict[str, Any]) -> str:
    """Render an entity dict into a compact text block for LLM context."""
    lines = [
        f"[{entity.get('entity_type', 'unknown').upper()}] {entity.get('name', '?')}",
        f"  URN: {entity.get('urn', '?')}",
    ]
    if entity.get("qualified_name"):
        lines.append(f"  Qualified: {entity['qualified_name']}")
    if entity.get("confidence_level"):
        lines.append(f"  Confidence: {entity['confidence_level']}")
    summary = entity.get("summary") or ""
    metadata = entity.get("metadata") or entity.get("entity_metadata")
    if isinstance(metadata, dict):
        purpose = metadata.get("purpose") or metadata.get("description") or ""
        if purpose:
            summary = purpose
    if summary:
        lines.append(f"  Summary: {summary}")
    return "\n".join(lines)


def _format_relationship(rel: dict[str, Any]) -> str:
    """Render a relationship dict into a compact text line."""
    return (
        f"  {rel.get('source_urn', '?')} --[{rel.get('rel_type', '?')}]--> "
        f"{rel.get('target_urn', '?')}"
    )


# ── Context builder ───────────────────────────────────────────────────


@dataclass
class ContextBuilder:
    """Assembles relevant entities into bounded LLM context.

    Usage::

        builder = ContextBuilder(max_tokens=5000)
        context = builder.build_context(
            query="How does order flow work?",
            entities=[...],        # list of entity dicts
            relationships=[...],   # list of relationship dicts
        )
    """

    max_tokens: int = 5000
    _entity_type_filter: list[str] | None = field(default=None, init=False)

    def build_context(
        self,
        query: str,
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
    ) -> str:
        """Build a context string within the token budget.

        1. Route: select entity types relevant to the query
        2. Score: rank entities by keyword relevance
        3. Assemble: format highest-relevance entities first
        4. Truncate: stop when budget is reached
        """
        max_chars = self.max_tokens * CHARS_PER_TOKEN

        # 1. Capability-Based Routing
        relevant_types = _infer_relevant_types(query)
        if relevant_types:
            filtered = [
                e for e in entities if (e.get("entity_type") or "").lower() in relevant_types
            ]
            # If filtering removes everything, fall back to all
            if filtered:
                entities = filtered

        # 2. Relevance scoring
        query_terms = {t.lower() for t in query.split() if len(t) > 2}
        scored = [(e, _score_entity(e, query_terms)) for e in entities]
        scored.sort(key=lambda x: x[1], reverse=True)

        # 3. Assemble within budget
        sections: list[str] = []
        header = f"=== Knowledge Context for: {query} ===\n"
        current_chars = len(header)
        sections.append(header)

        # Add entities in relevance order
        entity_count = 0
        for entity, _score in scored:
            block = _format_entity(entity)
            block_chars = len(block) + 2  # +2 for separator newlines
            if current_chars + block_chars > max_chars:
                break
            sections.append(block)
            current_chars += block_chars
            entity_count += 1

        # Add relationships if budget remains
        if relationships and current_chars < max_chars:
            rel_header = "\n=== Relationships ===\n"
            current_chars += len(rel_header)
            sections.append(rel_header)
            for rel in relationships:
                line = _format_relationship(rel)
                line_chars = len(line) + 1
                if current_chars + line_chars > max_chars:
                    break
                sections.append(line)
                current_chars += line_chars

        context = "\n".join(sections)

        logger.info(
            "Built context: %d entities, %d chars (~%d tokens), budget %d tokens",
            entity_count,
            len(context),
            len(context) // CHARS_PER_TOKEN,
            self.max_tokens,
        )

        return context

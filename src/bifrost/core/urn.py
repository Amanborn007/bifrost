"""URN generation, parsing, and canonicalization.

Canonical form (F1 glossary, section 10):

    bifrost:{scope}:{type}:{id}

NOT canonical (legacy forms that exist in the codebase today and the
slated for consolidation):

    urn:bifrost:repo:{name}           -- routes/repos.py and friends
    bifrost:{adapter}:{instance}:{type}:{id}
                                       -- 5-part adapter URNs
    bifrost:component:repo::Class     -- double-colon bug (prompt
                                          example, since fixed)

Use ``build_urn`` for new emitters. Use ``canonicalize`` to normalize
a URN that may have come in legacy form -- it strips a leading
``urn:`` prefix and refuses ambiguous shapes. ``parse_urn`` continues
to enforce the strict 4-part contract.

Wholesale migration of every emitter to ``build_urn`` is intentionally
out of scope here: the benchmark scorer is tuned to the
``urn:bifrost:`` variant and changing emissions invalidates persisted
URNs. That migration belongs in a deliberate follow-up PR with a
benchmark re-baseline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from bifrost.core.types import URN

URN_PREFIX = "bifrost"
LEGACY_URN_PREFIX = "urn:bifrost"
VALID_TYPES = frozenset(
    {
        "component",
        "interface",
        "flow",
        "dependency",
        "artifact",
        "gap-report",
        "correction",
        "repo",
        "domain",
    }
)


@dataclass(frozen=True)
class ParsedURN:
    """Parsed components of a Bifrost URN."""

    prefix: str  # always "bifrost"
    scope: str  # repo_id, domain_id, or "org"
    type: str  # entity type
    id: str  # entity id


def generate_urn(scope: str, entity_type: str, entity_id: str | uuid.UUID | None = None) -> URN:
    """Generate a Bifrost URN.

    Args:
        scope: The scope identifier (repo_id, domain_id, or "org").
        entity_type: The entity type (e.g., "component", "flow").
        entity_id: The entity ID. If None, generates a new UUID v4.

    Returns:
        A URN string like "bifrost:repo-abc:component:uuid-here".
    """
    if entity_type not in VALID_TYPES:
        raise ValueError(f"Invalid URN type: {entity_type}. Must be one of {VALID_TYPES}")

    if entity_id is None:
        entity_id = str(uuid.uuid4())

    return f"{URN_PREFIX}:{scope}:{entity_type}:{entity_id}"


def parse_urn(urn: URN) -> ParsedURN:
    """Parse a Bifrost URN into its components.

    Args:
        urn: A URN string like "bifrost:scope:type:id".

    Returns:
        ParsedURN with prefix, scope, type, and id fields.

    Raises:
        ValueError: If the URN is malformed.
    """
    parts = urn.split(":", maxsplit=3)
    if len(parts) != 4:
        raise ValueError(f"Malformed URN (expected 4 parts): {urn}")

    prefix, scope, entity_type, entity_id = parts

    if prefix != URN_PREFIX:
        raise ValueError(f"Invalid URN prefix: {prefix} (expected {URN_PREFIX})")

    if entity_type not in VALID_TYPES:
        raise ValueError(f"Invalid URN type: {entity_type}")

    return ParsedURN(prefix=prefix, scope=scope, type=entity_type, id=entity_id)


# ── New canonical surface ─────────────────────────────────────────────


# Alias: ``build_urn`` is the canonical name new code should call.
# ``generate_urn`` remains as a back-compat shim while existing call
# sites migrate.
build_urn = generate_urn


def canonicalize(urn: str) -> str:
    """Normalize a URN to the canonical ``bifrost:scope:type:id`` form.

    Strips a single leading ``urn:`` prefix (the legacy variant used
    by routes/repos.py and friends) and collapses any pair of
    adjacent colons that arose from an empty intermediate segment
    (e.g. ``bifrost:component:repo::Class`` -> a malformed URN that
    the caller probably meant as ``bifrost:repo:component:Class``).

    Returns the input unchanged when it's already canonical. Raises
    ``ValueError`` when normalization can't produce a 4-part URN.
    """
    candidate = urn.strip()
    if candidate.startswith(LEGACY_URN_PREFIX + ":"):
        candidate = candidate[len("urn:") :]
    # Collapse runs of ":" longer than one. This is best-effort; if
    # the empty segment carried meaning the caller should have
    # encoded the value explicitly.
    while "::" in candidate:
        candidate = candidate.replace("::", ":")
    parts = candidate.split(":")
    if len(parts) < 4 or parts[0] != URN_PREFIX:
        raise ValueError(f"Cannot canonicalize URN: {urn!r}")
    # Re-join exactly 4 segments (greedy id tail to preserve colons
    # in IDs the same way parse_urn does).
    return ":".join(parts[:3] + [":".join(parts[3:])])

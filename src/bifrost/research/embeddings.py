"""Local sentence embeddings for entities, Confluence pages, and Jira issues.

Uses sentence-transformers/all-MiniLM-L6-v2 (384-dim, ~80MB, CPU-friendly).
No API keys — respects the project constraint of pure Claude-CLI/local.

Typical usage:

    from bifrost.research.embeddings import embed_texts, embed_one

    vecs = embed_texts(["OrderService", "retry logic for carriers"])
    # returns list[list[float]] each length 384

The model is loaded lazily on first call and cached process-wide.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from threading import Lock

logger = logging.getLogger(__name__)


_MODEL_NAME = os.environ.get(
    "BIFROST_EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)
_CACHE_FOLDER = os.environ.get("BIFROST_EMBEDDING_CACHE", "/tmp/bifrost_st_cache")
EMBEDDING_DIM = 384

_model = None
_model_lock = Lock()


def _get_model():
    """Load MiniLM on first call (~2-3s, then cached)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                # Silence ~10 lines of "UNEXPECTED" warnings from the loader.
                import warnings

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    from sentence_transformers import SentenceTransformer

                    logger.info(
                        "Loading embedding model %s (one-time)",
                        _MODEL_NAME,
                    )
                    _model = SentenceTransformer(
                        _MODEL_NAME,
                        cache_folder=_CACHE_FOLDER,
                    )
    return _model


def embed_texts(
    texts: Iterable[str],
    *,
    batch_size: int = 32,
    normalize: bool = True,
) -> list[list[float]]:
    """Embed a batch of texts. Returns list of 384-float vectors.

    ``normalize=True`` L2-normalizes so cosine-similarity == dot product,
    which matches pgvector's `<->` cosine distance operator when paired
    with ``cosine_distance`` lookup (also stores more efficient indexes).
    """
    items = [t if t else " " for t in texts]  # empty strings confuse the model
    if not items:
        return []
    model = _get_model()
    vecs = model.encode(
        items,
        batch_size=batch_size,
        show_progress_bar=False,
        normalize_embeddings=normalize,
        convert_to_numpy=True,
    )
    return [v.tolist() for v in vecs]


def embed_one(text: str) -> list[float]:
    """Embed a single string."""
    return embed_texts([text])[0]


# ── Canonical text builders for each entity type ──────────────────────


def entity_embed_text(
    *,
    name: str,
    entity_type: str,
    purpose: str | None = None,
    qualified_name: str | None = None,
    source_file: str | None = None,
) -> str:
    """Canonical text to feed the embedder for one entity.

    We concatenate the fields the model is most likely to match against
    in a user's question: name, type, purpose, source file. Keep it
    short (the 128-token limit on MiniLM is real).
    """
    parts = [f"{entity_type}: {name}"]
    if qualified_name and qualified_name != name:
        parts.append(qualified_name)
    if purpose:
        parts.append(purpose[:400])
    if source_file:
        parts.append(f"in {source_file}")
    return "\n".join(parts)


def confluence_embed_text(
    *,
    title: str,
    summary: str | None = None,
    body_excerpt: str | None = None,
) -> str:
    parts = [title]
    if summary:
        parts.append(summary[:400])
    if body_excerpt:
        parts.append(body_excerpt[:600])
    return "\n".join(parts)


def jira_embed_text(
    *,
    issue_key: str,
    title: str,
    summary: str | None = None,
    description_excerpt: str | None = None,
    labels: list[str] | None = None,
) -> str:
    parts = [f"{issue_key}: {title}"]
    if summary:
        parts.append(summary[:300])
    if description_excerpt:
        parts.append(description_excerpt[:500])
    if labels:
        parts.append("labels: " + ", ".join(labels[:10]))
    return "\n".join(parts)

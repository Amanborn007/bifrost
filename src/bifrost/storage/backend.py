"""AgentStateBackend ABC — TRD §2.9.

Track A: implemented by PostgresStateBackend (knowledge_text_cache table).
Track B: implemented by MemoryStoreBackend (Anthropic Memory Stores API).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WritePrecondition:
    """Precondition for AgentStateBackend.write().

    sha: write fails with ConflictError if current content_hash differs.
    must_not_exist: write fails with ConflictError if document already exists.
    """

    sha: str | None = None
    must_not_exist: bool = False


@dataclass(frozen=True)
class DocumentEntry:
    """Lightweight reference returned by AgentStateBackend.list()."""

    path: str
    updated_at: datetime
    content_hash: str


class AgentStateBackend(ABC):
    """Abstract storage interface for agent state.

    Path conventions:
      /repo/{id}/components/{name}.md
      /repo/{id}/flows/{flow-id}.md
      /repo/{id}/dependencies/{dep-id}.md
      /repo/{id}/freshness/state.md
      /repo/{id}/config.md
      /repo/{id}/index.md
    """

    @abstractmethod
    async def read(self, path: str) -> str | None:
        """Read a document by exact path. Returns None if not found."""
        ...

    @abstractmethod
    async def write(
        self,
        path: str,
        content: str,
        precondition: WritePrecondition | None = None,
    ) -> None:
        """Write a document at the given path."""
        ...

    @abstractmethod
    async def list(self, path_prefix: str) -> list[DocumentEntry]:
        """List all document paths with the given prefix."""
        ...

    @abstractmethod
    async def delete(self, path: str) -> None:
        """Delete a document. No-op if not found."""
        ...

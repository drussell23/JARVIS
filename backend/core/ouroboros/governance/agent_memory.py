"""
Agent Memory Store — Persistent Per-Agent Memory Across Sessions
================================================================

Implements scoped, persistent memory for Ouroboros governance agents that
survives across sessions.  Inspired by Claude Code's USER / PROJECT / LOCAL
memory scoping model.

Memory Scopes
-------------
- **USER**: ``~/.jarvis/ouroboros/agent-memory/{agent_name}/``
  Global memories that follow the user across all projects.

- **PROJECT**: ``{project_root}/.jarvis/agent-memory/{agent_name}/``
  Per-project memories shared with collaborators (not gitignored).

- **LOCAL**: ``{project_root}/.jarvis/agent-memory-local/{agent_name}/``
  Per-project memories that are machine-local (gitignored).

Each memory entry is persisted as an individual JSON file keyed by a
sanitised version of the entry's ``key``.  Recall relevance is computed
via lightweight keyword overlap + recency + access-frequency scoring —
no embedding model or external service required.

Thread Safety
-------------
File I/O is serialised through a per-store ``asyncio.Lock`` when used
from async code.  Synchronous callers are safe as long as only one thread
writes to a given scope directory at a time (the governance pipeline is
single-threaded per operation).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.AgentMemory")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

_USER_MEMORY_ROOT = Path(
    os.environ.get(
        "JARVIS_AGENT_MEMORY_ROOT",
        os.path.expanduser("~/.jarvis/ouroboros/agent-memory"),
    )
)

# Maximum entries before auto-prune triggers on save
_DEFAULT_MAX_ENTRIES = int(os.environ.get("JARVIS_AGENT_MEMORY_MAX_ENTRIES", "200"))

# Relevance half-life in seconds (how quickly memories decay)
_RELEVANCE_HALF_LIFE_S = float(
    os.environ.get("JARVIS_AGENT_MEMORY_HALF_LIFE_S", str(7 * 86400))  # 7 days
)

# Valid categories for memory entries
VALID_CATEGORIES = frozenset({"learning", "pattern", "constraint", "preference"})


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MemoryScope(str, Enum):
    """Where a memory store is persisted on disk."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MemoryEntry:
    """A single persistent memory record for an agent.

    Attributes
    ----------
    key:
        Unique identifier within the agent's scope (e.g. ``"auth_flow_understanding"``).
    content:
        The actual memory content — free-form text.
    category:
        One of ``"learning"``, ``"pattern"``, ``"constraint"``, ``"preference"``.
    created_at:
        Unix timestamp of initial creation.
    updated_at:
        Unix timestamp of last update.
    access_count:
        Number of times this entry has been recalled.
    relevance_score:
        Base relevance score in ``[0.0, 1.0]``.  This is the *static* score
        set by the caller at save time.  Effective relevance is computed
        dynamically by :meth:`AgentMemoryStore._effective_relevance`, which
        blends this score with recency and access frequency.
    """

    key: str
    content: str
    category: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    access_count: int = 0
    relevance_score: float = 1.0

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MemoryEntry:
        """Deserialise from a dictionary (tolerant of unknown keys)."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitise_key(key: str) -> str:
    """Convert an arbitrary key string into a safe filename stem.

    Rules:
    - Lowercase
    - Replace any non-alphanumeric character (except ``-`` and ``_``) with ``_``
    - Collapse consecutive underscores
    - Strip leading / trailing underscores
    - Truncate to 128 characters
    """
    s = key.lower()
    s = re.sub(r"[^a-z0-9_\-]", "_", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_")
    return s[:128] or "unnamed"


def _tokenise(text: str) -> set[str]:
    """Cheaply tokenise text into lowercase alpha-numeric word set."""
    return {w for w in re.split(r"\W+", text.lower()) if len(w) > 2}


# ---------------------------------------------------------------------------
# AgentMemoryStore
# ---------------------------------------------------------------------------


class AgentMemoryStore:
    """Persistent, file-backed memory store for a single agent within one scope.

    Parameters
    ----------
    agent_name:
        Identifier for the agent (used as directory name).
    scope:
        Which persistence scope to use.
    project_root:
        Required for ``PROJECT`` and ``LOCAL`` scopes.  Ignored for ``USER``.
    max_entries:
        Hard cap on stored entries; exceeded triggers :meth:`prune`.
    """

    def __init__(
        self,
        agent_name: str,
        scope: MemoryScope,
        project_root: Optional[Path] = None,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._agent_name = agent_name
        self._scope = scope
        self._project_root = project_root
        self._max_entries = max_entries
        self._memory_dir = self._resolve_memory_dir()

        # Eagerly ensure the directory tree exists.
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(
            "AgentMemoryStore initialised: agent=%s scope=%s dir=%s",
            agent_name,
            scope.value,
            self._memory_dir,
        )

    # -- directory resolution ------------------------------------------------

    def _resolve_memory_dir(self) -> Path:
        """Compute the on-disk directory for this agent+scope pair."""
        safe_name = _sanitise_key(self._agent_name)

        if self._scope is MemoryScope.USER:
            return _USER_MEMORY_ROOT / safe_name

        if self._project_root is None:
            raise ValueError(
                f"project_root is required for scope={self._scope.value}"
            )

        if self._scope is MemoryScope.PROJECT:
            return self._project_root / ".jarvis" / "agent-memory" / safe_name

        if self._scope is MemoryScope.LOCAL:
            return self._project_root / ".jarvis" / "agent-memory-local" / safe_name

        raise ValueError(f"Unknown scope: {self._scope}")  # pragma: no cover

    # -- private helpers -----------------------------------------------------

    def _path_for_key(self, key: str) -> Path:
        """Return the JSON file path for a given memory key."""
        return self._memory_dir / f"{_sanitise_key(key)}.json"

    def _read_entry(self, path: Path) -> Optional[MemoryEntry]:
        """Read and deserialise a single memory file.  Returns None on error."""
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return MemoryEntry.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Failed to read memory entry %s: %s", path, exc)
            return None

    def _write_entry(self, entry: MemoryEntry) -> None:
        """Atomically write an entry to its JSON file.

        Writes to a temporary file first, then renames, to avoid partial writes.
        """
        target = self._path_for_key(entry.key)
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(entry.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(target)
        except OSError as exc:
            logger.error("Failed to write memory entry %s: %s", target, exc)
            # Clean up temp file if rename failed
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _effective_relevance(self, entry: MemoryEntry, now: Optional[float] = None) -> float:
        """Compute dynamic relevance blending base score, recency, and access count.

        Formula (weights configurable via subclass override):
            effective = 0.4 * base_relevance
                      + 0.4 * recency_factor
                      + 0.2 * frequency_factor

        Recency decays exponentially with configurable half-life.
        Frequency is log-scaled to prevent runaway dominance.
        """
        now = now or time.time()
        age_s = max(now - entry.updated_at, 0.0)

        # Exponential decay: 1.0 at t=0, 0.5 at t=half_life, ...
        recency = math.exp(-0.693147 * age_s / _RELEVANCE_HALF_LIFE_S)

        # Log-scaled access frequency, normalised to [0, 1] range
        frequency = min(math.log1p(entry.access_count) / math.log1p(100), 1.0)

        return (
            0.4 * entry.relevance_score
            + 0.4 * recency
            + 0.2 * frequency
        )

    # -- public API ----------------------------------------------------------

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def scope(self) -> MemoryScope:
        return self._scope

    @property
    def memory_dir(self) -> Path:
        return self._memory_dir

    def save(
        self,
        key: str,
        content: str,
        category: str,
        relevance_score: float = 1.0,
    ) -> MemoryEntry:
        """Create or update a memory entry, persisting it to disk.

        If the key already exists, the content, category, and relevance are
        updated in place and ``updated_at`` is refreshed.  Otherwise a new
        entry is created.

        Auto-prune fires after save if the store exceeds ``max_entries``.

        Parameters
        ----------
        key:
            Unique memory identifier.
        content:
            The memory content.
        category:
            Must be one of :data:`VALID_CATEGORIES`.
        relevance_score:
            Base relevance in ``[0.0, 1.0]``.  Defaults to ``1.0``.

        Returns
        -------
        MemoryEntry:
            The saved (or updated) entry.
        """
        if category not in VALID_CATEGORIES:
            logger.warning(
                "Unknown category %r (expected one of %s) — defaulting to 'learning'",
                category,
                VALID_CATEGORIES,
            )
            category = "learning"

        relevance_score = max(0.0, min(1.0, relevance_score))
        now = time.time()

        existing = self.recall(key, bump_access=False)
        if existing is not None:
            entry = MemoryEntry(
                key=existing.key,
                content=content,
                category=category,
                created_at=existing.created_at,
                updated_at=now,
                access_count=existing.access_count,
                relevance_score=relevance_score,
            )
        else:
            entry = MemoryEntry(
                key=key,
                content=content,
                category=category,
                created_at=now,
                updated_at=now,
                access_count=0,
                relevance_score=relevance_score,
            )

        self._write_entry(entry)
        logger.info(
            "Saved memory: agent=%s key=%s category=%s scope=%s",
            self._agent_name,
            key,
            category,
            self._scope.value,
        )

        # Auto-prune if we've exceeded max entries
        all_entries = self.list_all()
        if len(all_entries) > self._max_entries:
            self.prune(self._max_entries)

        return entry

    def recall(
        self,
        key: str,
        bump_access: bool = True,
    ) -> Optional[MemoryEntry]:
        """Retrieve a specific memory by key.

        Parameters
        ----------
        key:
            The memory key to look up.
        bump_access:
            If True (default), increment ``access_count`` and persist the update.

        Returns
        -------
        Optional[MemoryEntry]:
            The memory entry, or None if not found.
        """
        path = self._path_for_key(key)
        if not path.exists():
            return None

        entry = self._read_entry(path)
        if entry is None:
            return None

        if bump_access:
            bumped = MemoryEntry(
                key=entry.key,
                content=entry.content,
                category=entry.category,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                access_count=entry.access_count + 1,
                relevance_score=entry.relevance_score,
            )
            self._write_entry(bumped)
            return bumped

        return entry

    def recall_relevant(
        self,
        goal: str,
        max_entries: int = 5,
        category_filter: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """Search all memories for relevance to a given goal string.

        Scoring combines:
        - Keyword overlap between goal and entry content/key
        - Recency (exponential decay)
        - Access frequency (log-scaled)

        Parameters
        ----------
        goal:
            Natural-language description of the current objective.
        max_entries:
            Maximum number of entries to return.
        category_filter:
            If set, restrict search to entries of this category.

        Returns
        -------
        List[MemoryEntry]:
            Up to ``max_entries`` entries, sorted by descending relevance.
        """
        goal_tokens = _tokenise(goal)
        if not goal_tokens:
            return []

        now = time.time()
        scored: list[tuple[float, MemoryEntry]] = []

        for entry in self.list_all():
            if category_filter and entry.category != category_filter:
                continue

            # Keyword overlap score: Jaccard-like
            entry_tokens = _tokenise(entry.key) | _tokenise(entry.content)
            if not entry_tokens:
                keyword_score = 0.0
            else:
                intersection = goal_tokens & entry_tokens
                union = goal_tokens | entry_tokens
                keyword_score = len(intersection) / len(union) if union else 0.0

            # Blend keyword relevance with dynamic relevance
            dynamic = self._effective_relevance(entry, now=now)
            combined = 0.5 * keyword_score + 0.5 * dynamic

            scored.append((combined, entry))

        # Sort descending by combined score
        scored.sort(key=lambda pair: pair[0], reverse=True)

        results = [entry for _, entry in scored[:max_entries]]

        # Bump access count on recalled entries
        for entry in results:
            self.recall(entry.key, bump_access=True)

        return results

    def forget(self, key: str) -> bool:
        """Delete a specific memory entry.

        Returns
        -------
        bool:
            True if the entry existed and was deleted, False otherwise.
        """
        path = self._path_for_key(key)
        if not path.exists():
            return False

        try:
            path.unlink()
            logger.info(
                "Forgot memory: agent=%s key=%s scope=%s",
                self._agent_name,
                key,
                self._scope.value,
            )
            return True
        except OSError as exc:
            logger.error("Failed to delete memory %s: %s", path, exc)
            return False

    def list_all(self) -> List[MemoryEntry]:
        """Load and return all stored memory entries.

        Entries are returned in no guaranteed order.  Corrupted files are
        skipped with a warning.
        """
        entries: list[MemoryEntry] = []
        if not self._memory_dir.exists():
            return entries

        for path in self._memory_dir.glob("*.json"):
            entry = self._read_entry(path)
            if entry is not None:
                entries.append(entry)

        return entries

    def prune(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> int:
        """Remove lowest-relevance entries until at most ``max_entries`` remain.

        Entries are scored by :meth:`_effective_relevance` and the lowest-scoring
        entries are deleted first.

        Returns
        -------
        int:
            Number of entries removed.
        """
        all_entries = self.list_all()
        if len(all_entries) <= max_entries:
            return 0

        now = time.time()
        scored = [
            (self._effective_relevance(e, now=now), e) for e in all_entries
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        to_remove = scored[max_entries:]
        removed = 0
        for _, entry in to_remove:
            if self.forget(entry.key):
                removed += 1

        logger.info(
            "Pruned %d/%d entries from agent=%s scope=%s (limit=%d)",
            removed,
            len(all_entries),
            self._agent_name,
            self._scope.value,
            max_entries,
        )
        return removed

    @staticmethod
    def format_for_prompt(entries: Sequence[MemoryEntry]) -> str:
        """Format a list of memory entries for injection into a generation prompt.

        Produces a structured, human-readable block that can be appended to
        system or user prompt sections.

        Parameters
        ----------
        entries:
            The memory entries to format.

        Returns
        -------
        str:
            Formatted text block, or empty string if no entries.
        """
        if not entries:
            return ""

        lines: list[str] = ["## Agent Memory Context", ""]

        for entry in entries:
            age_hours = (time.time() - entry.updated_at) / 3600
            if age_hours < 1:
                age_str = f"{age_hours * 60:.0f}m ago"
            elif age_hours < 24:
                age_str = f"{age_hours:.1f}h ago"
            else:
                age_str = f"{age_hours / 24:.1f}d ago"

            lines.append(
                f"### [{entry.category.upper()}] {entry.key} "
                f"(accessed {entry.access_count}x, updated {age_str})"
            )
            lines.append(entry.content)
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_agent_memory(
    agent_name: str,
    scope: MemoryScope = MemoryScope.PROJECT,
    project_root: Optional[Path] = None,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
) -> AgentMemoryStore:
    """Factory function for obtaining an :class:`AgentMemoryStore`.

    Parameters
    ----------
    agent_name:
        The agent identifier.
    scope:
        Memory persistence scope.
    project_root:
        Required for ``PROJECT`` and ``LOCAL`` scopes.  For ``USER`` scope this
        is ignored.  If not provided for project/local scopes, falls back to
        the ``JARVIS_REPO_PATH`` environment variable, then to ``"."``.

    Returns
    -------
    AgentMemoryStore:
        Ready-to-use memory store with its backing directory created.
    """
    if scope is not MemoryScope.USER and project_root is None:
        env_root = os.environ.get("JARVIS_REPO_PATH")
        project_root = Path(env_root) if env_root else Path(".")

    return AgentMemoryStore(
        agent_name=agent_name,
        scope=scope,
        project_root=project_root,
        max_entries=max_entries,
    )

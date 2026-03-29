"""
RoadmapSnapshot & SnapshotFragment
====================================

Immutable, hashable value objects that represent a point-in-time view of all
roadmap source material (specs, plans, backlogs, memory files, commit logs,
issues).

Design principles:
- All fields are typed; no ``Any`` at the public surface.
- ``SnapshotFragment`` is a ``frozen`` dataclass — fully immutable after
  construction so it is safe to use as a dict key or in sets.
- ``RoadmapSnapshot`` wraps a tuple of fragments and carries a canonical
  content-hash so callers can detect whether anything changed without
  re-reading every file.
- ``compute_snapshot_hash`` uses a stable, order-independent formula:
  sort-then-hash so that inserting fragments in different orders always
  produces the same digest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from dataclasses import dataclass
from typing import ClassVar, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# SnapshotFragment
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotFragment:
    """A single source document included in a roadmap snapshot.

    Parameters
    ----------
    source_id:
        Stable, human-readable ID for the source.  Examples::

            "spec:ouroboros-daemon-design"
            "git:jarvis:bounded"
            "memory:MEMORY.md"

        The ID must be stable across runs so that the content-hash formula
        (which keyes on ``source_id``) remains meaningful.

    uri:
        Relative file path or shell command used to fetch the content.

    tier:
        Authority tier.  Lower is more authoritative:
        - 0 = authoritative (design specs, architecture docs)
        - 1 = trajectory (plans, roadmaps, backlogs)
        - 2 = external (GitHub issues, third-party docs)
        - 3 = personal (MEMORY.md, user notes)

    content_hash:
        SHA-256 hex digest of the raw file bytes at fetch time.

    fetched_at:
        UTC epoch seconds when the content was fetched.  Use
        ``time.time()`` — NOT ``time.monotonic()`` — so that it survives
        process restarts.

    mtime:
        File modification time as UTC epoch seconds.

    title:
        Document title extracted from frontmatter or the first heading.

    summary:
        First 500 characters of document content (stripped).

    fragment_type:
        Semantic category of the source.  One of:
        ``"spec"``, ``"plan"``, ``"backlog"``, ``"memory"``,
        ``"commit_log"``, ``"issue"``.
    """

    VALID_FRAGMENT_TYPES: ClassVar[frozenset] = frozenset(
        {"spec", "plan", "backlog", "memory", "commit_log", "issue"}
    )

    source_id: str
    uri: str
    tier: int
    content_hash: str
    fetched_at: float
    mtime: float
    title: str
    summary: str
    fragment_type: str

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("source_id must be non-empty")
        if self.tier not in range(4):
            raise ValueError(f"tier must be 0-3, got {self.tier!r}")
        if self.fragment_type not in self.VALID_FRAGMENT_TYPES:
            raise ValueError(
                f"fragment_type must be one of {sorted(self.VALID_FRAGMENT_TYPES)}, "
                f"got {self.fragment_type!r}"
            )


# ---------------------------------------------------------------------------
# Canonical hash formula
# ---------------------------------------------------------------------------

def compute_snapshot_hash(fragments: Tuple[SnapshotFragment, ...]) -> str:
    """Return a stable SHA-256 digest over *fragments*.

    Formula::

        sha256("\\n".join(sorted(
            f"{sf.source_id}\\t{sf.content_hash}"
            for sf in fragments
        )))

    Properties:
    - **Order-independent**: fragments are sorted before hashing.
    - **Collision-resistant**: tab separator prevents ``"a" + "bc"`` from
      colliding with ``"ab" + "c"``.
    - **Deterministic**: only ``source_id`` and ``content_hash`` contribute —
      timestamps and metadata changes do not trigger a version bump.
    """
    lines = sorted(
        f"{sf.source_id}\t{sf.content_hash}"
        for sf in fragments
    )
    payload = "\n".join(lines)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# RoadmapSnapshot
# ---------------------------------------------------------------------------

@dataclass
class RoadmapSnapshot:
    """An immutable-by-convention snapshot of the entire roadmap corpus.

    Use the :meth:`create` classmethod rather than the bare constructor so
    that ``version`` and ``content_hash`` are computed correctly.

    Parameters
    ----------
    version:
        Monotonically-increasing integer.  Increments *only* when
        ``content_hash`` differs from the previous snapshot's hash.

    content_hash:
        Canonical digest produced by :func:`compute_snapshot_hash`.

    created_at:
        UTC epoch seconds when this snapshot was assembled.

    fragments:
        All source fragments in this snapshot, in an arbitrary but stable
        order (the hash formula sorts internally).

    tier_counts:
        Mapping of ``tier`` → count of fragments at that tier.
    """

    version: int
    content_hash: str
    created_at: float
    fragments: Tuple[SnapshotFragment, ...]
    tier_counts: Dict[int, int]

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        fragments: Tuple[SnapshotFragment, ...],
        previous_version: int = 0,
        previous_hash: Optional[str] = None,
    ) -> "RoadmapSnapshot":
        """Construct a snapshot from *fragments*.

        Parameters
        ----------
        fragments:
            Collection of :class:`SnapshotFragment` objects to include.
        previous_version:
            Version number of the most recent snapshot.  Defaults to 0.
        previous_hash:
            Content hash of the most recent snapshot, or ``None`` if this
            is the first snapshot.  When the computed hash matches
            *previous_hash* the version is **not** incremented.

        Returns
        -------
        RoadmapSnapshot
            New snapshot.  ``version`` is ``previous_version + 1`` if the
            content changed, else ``previous_version``.
        """
        new_hash = compute_snapshot_hash(fragments)
        changed = new_hash != previous_hash
        version = previous_version + 1 if changed else previous_version

        tier_counts: Dict[int, int] = {}
        for sf in fragments:
            tier_counts[sf.tier] = tier_counts.get(sf.tier, 0) + 1

        return cls(
            version=version,
            content_hash=new_hash,
            created_at=time.time(),
            fragments=tuple(fragments),
            tier_counts=tier_counts,
        )

"""
RoadmapSensor — Clock 1
========================

Deterministic snapshot refresh with change detection and optional callback
trigger.  Zero model calls are made; all work is pure I/O delegated to the
source crawlers.

Usage
-----
::

    from backend.core.ouroboros.roadmap.sensor import RoadmapSensor, RoadmapSensorConfig

    sensor = RoadmapSensor(
        repo_root=Path("/path/to/repo"),
        config=RoadmapSensorConfig(p1_enabled=True, refresh_interval_s=3600.0),
        on_snapshot_changed=lambda snap: print(f"snapshot v{snap.version} ready"),
    )

    snapshot = sensor.refresh()          # crawl + change-detect
    health   = sensor.health()           # lightweight status dict
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot, SnapshotFragment
from backend.core.ouroboros.roadmap.source_crawlers import (
    crawl_claude_md,
    crawl_backlog,
    crawl_git_log,
    crawl_memory,
    crawl_plans,
    crawl_specs,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RoadmapSensorConfig:
    """Tunable knobs for :class:`RoadmapSensor`.

    Attributes
    ----------
    p1_enabled:
        When ``True`` the sensor includes a bounded ``git log`` fragment
        (tier 1) in each snapshot.
    p1_commit_limit:
        Maximum number of commits passed to :func:`crawl_git_log`.
    p1_days:
        Maximum commit age in days passed to :func:`crawl_git_log`.
    p2_enabled:
        Reserved for future P2 (external / GitHub-issues) crawlers.
        Currently unused.
    p3_enabled:
        Reserved for future P3 crawlers.  Currently unused.
    refresh_interval_s:
        Minimum seconds between automatic refreshes when this sensor is
        driven by a daemon.  :meth:`RoadmapSensor.refresh` itself does
        **not** enforce this interval — the daemon is responsible for
        scheduling.
    """

    p1_enabled: bool = True
    p1_commit_limit: int = 50
    p1_days: int = 30
    p2_enabled: bool = False
    p3_enabled: bool = False
    refresh_interval_s: float = 3600.0


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------

class RoadmapSensor:
    """Clock 1 — deterministic snapshot refresh with change detection.

    The sensor is *stateful*: it remembers the last snapshot and fires the
    optional callback only when the content hash changes.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.  All crawlers receive this
        path.
    config:
        Tunable behaviour flags and limits.
    on_snapshot_changed:
        Optional zero-argument-free callable that is invoked with the new
        :class:`~backend.core.ouroboros.roadmap.snapshot.RoadmapSnapshot`
        whenever ``content_hash`` changes between refreshes.  Called
        synchronously inside :meth:`refresh`.
    """

    def __init__(
        self,
        repo_root: Path,
        config: RoadmapSensorConfig,
        on_snapshot_changed: Optional[Callable[[RoadmapSnapshot], None]] = None,
    ) -> None:
        self._repo_root = repo_root
        self._config = config
        self._on_snapshot_changed = on_snapshot_changed

        self._current_snapshot: Optional[RoadmapSnapshot] = None
        self._last_refresh_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> RoadmapSnapshot:
        """Crawl all enabled sources and return an up-to-date snapshot.

        The method:

        1. Crawls all P0 sources unconditionally.
        2. Crawls P1 (``git log``) when ``config.p1_enabled`` is ``True``.
        3. Assembles a new :class:`RoadmapSnapshot` via
           :meth:`~RoadmapSnapshot.create`, propagating the previous
           ``version`` and ``content_hash`` so the version counter only
           increments on real content changes.
        4. When the content hash changed *and* ``on_snapshot_changed`` was
           supplied, calls the callback with the new snapshot.
        5. Caches the snapshot and returns it.

        Returns
        -------
        RoadmapSnapshot
            The latest snapshot (possibly with the same version/hash as the
            previous call if nothing changed).
        """
        fragments = self._crawl_all()

        prev_version = self._current_snapshot.version if self._current_snapshot else 0
        prev_hash = self._current_snapshot.content_hash if self._current_snapshot else None

        new_snapshot = RoadmapSnapshot.create(
            fragments=tuple(fragments),
            previous_version=prev_version,
            previous_hash=prev_hash,
        )

        changed = new_snapshot.content_hash != (prev_hash or "")
        if changed and self._on_snapshot_changed is not None:
            self._on_snapshot_changed(new_snapshot)

        self._current_snapshot = new_snapshot
        self._last_refresh_at = time.time()
        return new_snapshot

    @property
    def current_snapshot(self) -> Optional[RoadmapSnapshot]:
        """Most recently produced snapshot, or ``None`` before first :meth:`refresh`."""
        return self._current_snapshot

    def health(self) -> Dict[str, object]:
        """Return a lightweight status dictionary.

        Keys
        ----
        snapshot_version:
            Current snapshot version (0 if never refreshed).
        fragment_count:
            Number of fragments in the current snapshot (0 if never refreshed).
        last_refresh_at:
            UTC epoch seconds of the last completed refresh (0.0 if never refreshed).
        content_hash:
            Content hash of the current snapshot (empty string if never refreshed).
        """
        snap = self._current_snapshot
        return {
            "snapshot_version": snap.version if snap is not None else 0,
            "fragment_count": len(snap.fragments) if snap is not None else 0,
            "last_refresh_at": self._last_refresh_at,
            "content_hash": snap.content_hash if snap is not None else "",
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _crawl_all(self) -> list[SnapshotFragment]:
        """Execute all enabled crawlers and return the merged fragment list."""
        fragments: list[SnapshotFragment] = []

        # P0 — always on, authoritative sources
        fragments.extend(crawl_specs(self._repo_root))
        fragments.extend(crawl_plans(self._repo_root))
        fragments.extend(crawl_backlog(self._repo_root))
        fragments.extend(crawl_memory(self._repo_root))
        fragments.extend(crawl_claude_md(self._repo_root))

        # P1 — trajectory (bounded git log)
        if self._config.p1_enabled:
            fragments.extend(
                crawl_git_log(
                    self._repo_root,
                    max_commits=self._config.p1_commit_limit,
                    max_days=self._config.p1_days,
                )
            )

        # P2 / P3 hooks reserved for future crawlers — no hardcoded stubs
        return fragments

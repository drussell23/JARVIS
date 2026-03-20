"""
ScenePartition — L1 hot cache for the Unified Knowledge Fabric.

All storage is pure Python dict; no external dependencies.
Target latency: <5 ms for read and query_nearest.
Default TTL: 5 seconds (FreshnessClass.HOT).
"""

from __future__ import annotations

import math
import time
from typing import Dict, Optional, Tuple


class ScenePartition:
    """In-memory L1 hot cache for scene-partition entities.

    Entities are stored with an expiry timestamp.  Reads check expiry and
    prune stale entries lazily to avoid a background sweep thread.

    Parameters
    ----------
    default_ttl_seconds:
        TTL applied when callers do not supply their own.  Defaults to 5.0 s
        (HOT freshness tier).
    """

    def __init__(self, default_ttl_seconds: float = 5.0) -> None:
        self._default_ttl: float = default_ttl_seconds
        # Payload store: entity_id -> data dict
        self._store: Dict[str, dict] = {}
        # Expiry timestamps: entity_id -> monotonic expiry time (seconds)
        self._expiry: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(
        self,
        entity_id: str,
        data: dict,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Store *data* under *entity_id* with an optional custom TTL.

        If the entity already exists its data and expiry are both refreshed.

        Parameters
        ----------
        entity_id:
            Full ``kg://`` entity ID string.
        data:
            Arbitrary dict payload to store (a shallow copy is kept).
        ttl_seconds:
            Seconds until this entry expires.  ``None`` uses the partition
            default (5 s).
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        self._store[entity_id] = dict(data)
        self._expiry[entity_id] = time.monotonic() + ttl

    def read(self, entity_id: str) -> Optional[dict]:
        """Return the stored data for *entity_id*, or ``None`` on miss/expiry.

        Expired entries are pruned on access (lazy eviction).

        Parameters
        ----------
        entity_id:
            Full ``kg://`` entity ID string.
        """
        if entity_id not in self._store:
            return None
        if time.monotonic() >= self._expiry[entity_id]:
            # Lazy prune
            del self._store[entity_id]
            del self._expiry[entity_id]
            return None
        return self._store[entity_id]

    def query_nearest(
        self,
        coords: Tuple[int, int],
        max_distance: float = 50.0,
    ) -> Optional[dict]:
        """Find the live entity whose ``"position"`` field is closest to *coords*.

        Only entities that have a ``"position"`` key (tuple of two numbers)
        and whose Euclidean distance from *coords* is within *max_distance*
        are considered.  Expired entries are skipped and lazily pruned.

        Parameters
        ----------
        coords:
            ``(x, y)`` query point in screen coordinates.
        max_distance:
            Maximum Euclidean distance (pixels) to consider a match.

        Returns
        -------
        dict or None
            The data dict of the nearest matching entity, or ``None`` if no
            entity qualifies.
        """
        now = time.monotonic()
        qx, qy = coords
        best_dist: float = math.inf
        best_data: Optional[dict] = None
        expired_keys = []

        for eid, data in self._store.items():
            if now >= self._expiry[eid]:
                expired_keys.append(eid)
                continue
            pos = data.get("position")
            if pos is None:
                continue
            px, py = pos
            dist = math.sqrt((px - qx) ** 2 + (py - qy) ** 2)
            if dist <= max_distance and dist < best_dist:
                best_dist = dist
                best_data = data

        # Lazy eviction of expired keys found during scan
        for eid in expired_keys:
            self._store.pop(eid, None)
            self._expiry.pop(eid, None)

        return best_data

    def clear(self) -> None:
        """Remove all entries from the partition."""
        self._store.clear()
        self._expiry.clear()

    # ------------------------------------------------------------------
    # Introspection helpers (useful for debugging / tests)
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Return the number of non-expired entries (O(n) scan)."""
        now = time.monotonic()
        return sum(1 for eid in self._store if now < self._expiry[eid])

    def __repr__(self) -> str:  # pragma: no cover
        return f"ScenePartition(live={len(self)}, total_slots={len(self._store)})"

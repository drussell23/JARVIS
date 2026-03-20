"""
KnowledgeFabric — unified API across scene, semantic, and trinity partitions.

This task wires the local L1 ScenePartition.  Semantic (L2) and trinity (L3)
are remote partitions implemented in later tasks; their slots here are None
stubs so that ``query()`` cleanly returns ``None`` rather than raising.

Architecture
------------
    KnowledgeFabric
        ├── _scene   : ScenePartition  (local, in-process, <5 ms)
        ├── _semantic: None            (remote L2 — future task)
        └── _trinity : None            (remote L3 — future task)

Routing is delegated to :func:`backend.knowledge.fabric_router.route_partition`
which derives the target partition from the ``kg://`` prefix — no hardcoding.
"""

from __future__ import annotations

from typing import Optional, Tuple

from backend.knowledge.fabric_router import route_partition
from backend.knowledge.scene_partition import ScenePartition


class KnowledgeFabric:
    """Unified read/write API for the three-partition Knowledge Graph.

    Parameters
    ----------
    scene_partition:
        Optional pre-built :class:`~backend.knowledge.scene_partition.ScenePartition`
        instance.  Useful for testing.  If ``None`` a default instance is
        created automatically.
    """

    def __init__(
        self,
        scene_partition: Optional[ScenePartition] = None,
    ) -> None:
        self._scene: ScenePartition = (
            scene_partition if scene_partition is not None else ScenePartition()
        )
        # Remote partitions — implemented in future tasks
        self._semantic = None  # L2: J-Prime semantic cache
        self._trinity = None   # L3: trinity audit store

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, entity_id: str, data: dict, **kwargs) -> None:
        """Route a write to the correct partition.

        Parameters
        ----------
        entity_id:
            Full ``kg://`` entity ID.
        data:
            Arbitrary dict payload.
        **kwargs:
            Forwarded to the partition's ``write()`` method (e.g.
            ``ttl_seconds`` for the scene partition).
        """
        partition = route_partition(entity_id)
        if partition == "scene":
            self._scene.write(entity_id, data, **kwargs)
        elif partition == "semantic":
            # Remote partition stub — writes are silently dropped until L2 lands
            pass
        elif partition == "trinity":
            # Remote partition stub — writes are silently dropped until L3 lands
            pass

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, entity_id: str) -> Optional[dict]:
        """Route a point-lookup to the correct partition.

        Parameters
        ----------
        entity_id:
            Full ``kg://`` entity ID.

        Returns
        -------
        dict or None
            The stored payload, or ``None`` on cache miss / expiry / remote
            partition not yet available.
        """
        partition = route_partition(entity_id)
        if partition == "scene":
            return self._scene.read(entity_id)
        # semantic and trinity partitions are remote stubs → always None here
        return None

    # ------------------------------------------------------------------
    # Spatial helpers
    # ------------------------------------------------------------------

    def query_nearest_element(
        self,
        coords: Tuple[int, int],
        max_distance: float = 50.0,
    ) -> Optional[dict]:
        """Find the scene entity closest to *coords* within *max_distance* pixels.

        Delegates directly to the scene partition's spatial index.

        Parameters
        ----------
        coords:
            ``(x, y)`` screen-space query point.
        max_distance:
            Euclidean distance cutoff in pixels.

        Returns
        -------
        dict or None
        """
        return self._scene.query_nearest(coords, max_distance=max_distance)

    # ------------------------------------------------------------------
    # Partition accessors (for testing / advanced consumers)
    # ------------------------------------------------------------------

    @property
    def scene(self) -> ScenePartition:
        """Direct access to the L1 scene partition."""
        return self._scene

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"KnowledgeFabric("
            f"scene={self._scene!r}, "
            f"semantic={'wired' if self._semantic else 'stub'}, "
            f"trinity={'wired' if self._trinity else 'stub'})"
        )

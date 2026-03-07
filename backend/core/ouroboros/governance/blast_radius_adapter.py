# backend/core/ouroboros/governance/blast_radius_adapter.py
"""
Blast Radius Adapter -- Oracle Integration
============================================

Auto-populates :class:`OperationProfile` ``blast_radius`` from the
:class:`CodebaseKnowledgeGraph` when available.  Falls back gracefully
to manual values when the Oracle is unavailable or encounters errors.

For multi-file operations, computes blast radius for each file and
uses the maximum across all files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from backend.core.ouroboros.governance.risk_engine import OperationProfile

logger = logging.getLogger("Ouroboros.BlastRadiusAdapter")


@dataclass(frozen=True)
class BlastRadiusResult:
    """Result of a blast radius computation."""

    total_affected: int
    risk_level: str
    from_oracle: bool


_FALLBACK = BlastRadiusResult(total_affected=1, risk_level="low", from_oracle=False)


class BlastRadiusAdapter:
    """Adapts the CodebaseKnowledgeGraph blast radius API for governance use."""

    def __init__(self, oracle: Optional[Any] = None) -> None:
        self._oracle = oracle

    def compute(self, file_path: str) -> BlastRadiusResult:
        """Compute blast radius for a single file."""
        if self._oracle is None:
            return _FALLBACK

        try:
            nodes = self._oracle.find_nodes_in_file(file_path)
            if not nodes:
                return _FALLBACK

            blast = self._oracle.compute_blast_radius(nodes[0])
            return BlastRadiusResult(
                total_affected=blast.total_affected,
                risk_level=blast.risk_level,
                from_oracle=True,
            )
        except Exception as exc:
            logger.warning(
                "BlastRadiusAdapter: Oracle error for %s: %s",
                file_path, exc,
            )
            return _FALLBACK

    def enrich_profile(self, profile: OperationProfile) -> OperationProfile:
        """Enrich an OperationProfile with Oracle blast radius data.

        Computes blast radius for each file in the profile and uses
        the maximum across all files.
        """
        max_blast = 0
        for fpath in profile.files_affected:
            result = self.compute(str(fpath))
            max_blast = max(max_blast, result.total_affected)

        if max_blast > 0:
            return replace(profile, blast_radius=max_blast)
        return profile

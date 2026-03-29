"""
HypothesisCache — exact-fingerprint cache for FeatureHypothesis lists.
=======================================================================

Persists synthesized hypotheses to disk so the Synthesis Engine can skip
redundant 397B model calls when the input fingerprint has not changed.

Staleness is OR-based (matching :meth:`FeatureHypothesis.is_stale`):
- hash mismatch  — the snapshot content drifted, OR
- age exceeded   — the cache is too old regardless of content.

Files written:
    <cache_dir>/hypotheses.json       — array of serialised hypothesis dicts
    <cache_dir>/hypotheses_meta.json  — {"input_fingerprint", "snapshot_hash",
                                          "saved_at", "count"}
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path.home() / ".jarvis" / "ouroboros" / "roadmap"

# File names are intentionally constants (not config) — they are the
# canonical single-source-of-truth layout for this cache.
_HYPOTHESES_FILE = "hypotheses.json"
_META_FILE = "hypotheses_meta.json"


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _hypothesis_to_dict(h: FeatureHypothesis) -> dict:
    """Serialise a FeatureHypothesis to a plain JSON-safe dict.

    Tuples are stored as lists (JSON has no tuple type).
    hypothesis_fingerprint is NOT stored — it is recomputed by __post_init__
    on deserialisation to guarantee integrity.
    """
    return {
        "hypothesis_id": h.hypothesis_id,
        "description": h.description,
        "evidence_fragments": list(h.evidence_fragments),
        "gap_type": h.gap_type,
        "confidence": h.confidence,
        "confidence_rule_id": h.confidence_rule_id,
        "urgency": h.urgency,
        "suggested_scope": h.suggested_scope,
        "suggested_repos": list(h.suggested_repos),
        "provenance": h.provenance,
        "synthesized_for_snapshot_hash": h.synthesized_for_snapshot_hash,
        "synthesized_at": h.synthesized_at,
        "synthesis_input_fingerprint": h.synthesis_input_fingerprint,
        "status": h.status,
    }


def _dict_to_hypothesis(d: dict) -> FeatureHypothesis:
    """Deserialise a dict back to a FeatureHypothesis.

    Lists from JSON are converted back to tuples as required by the
    dataclass fields.  Raises ValueError / KeyError if the dict is invalid.
    """
    return FeatureHypothesis(
        hypothesis_id=d["hypothesis_id"],
        description=d["description"],
        evidence_fragments=tuple(d["evidence_fragments"]),
        gap_type=d["gap_type"],
        confidence=float(d["confidence"]),
        confidence_rule_id=d["confidence_rule_id"],
        urgency=d["urgency"],
        suggested_scope=d["suggested_scope"],
        suggested_repos=tuple(d["suggested_repos"]),
        provenance=d["provenance"],
        synthesized_for_snapshot_hash=d["synthesized_for_snapshot_hash"],
        synthesized_at=float(d["synthesized_at"]),
        synthesis_input_fingerprint=d["synthesis_input_fingerprint"],
        status=d.get("status", "active"),
    )


# ---------------------------------------------------------------------------
# HypothesisCache
# ---------------------------------------------------------------------------

class HypothesisCache:
    """Exact-fingerprint disk cache for :class:`FeatureHypothesis` lists.

    Parameters
    ----------
    cache_dir:
        Directory where ``hypotheses.json`` and ``hypotheses_meta.json``
        are stored.  Defaults to ``~/.jarvis/ouroboros/roadmap``.
        Created on first :meth:`save` if it does not exist.
    """

    def __init__(self, cache_dir: Path = _DEFAULT_CACHE_DIR) -> None:
        self._cache_dir = Path(cache_dir)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _hypotheses_path(self) -> Path:
        return self._cache_dir / _HYPOTHESES_FILE

    @property
    def _meta_path(self) -> Path:
        return self._cache_dir / _META_FILE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        hypotheses: List[FeatureHypothesis],
        *,
        input_fingerprint: str = "",
        snapshot_hash: str = "",
    ) -> None:
        """Serialise *hypotheses* to disk and write companion meta.

        Parameters
        ----------
        hypotheses:
            The list to persist.  May be empty.
        input_fingerprint:
            Fingerprint of the synthesis input (used by
            :meth:`get_if_valid` for cache-hit detection).
        snapshot_hash:
            ``content_hash`` of the :class:`RoadmapSnapshot` the
            hypotheses were synthesised against (used by
            :meth:`is_stale`).
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        data = [_hypothesis_to_dict(h) for h in hypotheses]
        meta = {
            "input_fingerprint": input_fingerprint,
            "snapshot_hash": snapshot_hash,
            "saved_at": time.time(),
            "count": len(hypotheses),
        }

        # Write data first, then meta — readers check meta for validity,
        # so writing data first keeps the window of inconsistency minimal.
        self._hypotheses_path.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
        self._meta_path.write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        logger.debug(
            "HypothesisCache saved %d hypotheses to %s",
            len(hypotheses),
            self._cache_dir,
        )

    def load(self) -> List[FeatureHypothesis]:
        """Deserialise hypotheses from disk.

        Returns an empty list when:
        - The cache directory does not exist.
        - ``hypotheses.json`` is missing.
        - The file content is not valid JSON.
        - Any individual hypothesis dict fails validation (that entry is
          silently skipped; other valid entries are still returned).
        """
        if not self._hypotheses_path.exists():
            return []

        try:
            raw = json.loads(self._hypotheses_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "HypothesisCache: failed to parse %s — %s", self._hypotheses_path, exc
            )
            return []

        hypotheses: List[FeatureHypothesis] = []
        for i, entry in enumerate(raw):
            try:
                hypotheses.append(_dict_to_hypothesis(entry))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "HypothesisCache: skipping entry %d — %s", i, exc
                )
        return hypotheses

    def get_if_valid(
        self, input_fingerprint: str
    ) -> Optional[List[FeatureHypothesis]]:
        """Return cached hypotheses if the stored input fingerprint matches.

        Parameters
        ----------
        input_fingerprint:
            The fingerprint of the current synthesis input.

        Returns
        -------
        List[FeatureHypothesis]
            The cached list when the fingerprints match.
        None
            When there is no cache, the meta is unreadable, or the
            fingerprints differ.
        """
        stored_meta = self._read_meta()
        if stored_meta is None:
            return None

        if stored_meta.get("input_fingerprint") != input_fingerprint:
            return None

        return self.load()

    def is_stale(
        self,
        current_snapshot_hash: str,
        ttl_s: float,
    ) -> bool:
        """Return ``True`` if the cache should be considered stale.

        Staleness is OR-based:
        - the stored snapshot hash differs from *current_snapshot_hash*, OR
        - the age of the cache exceeds *ttl_s* seconds.

        A missing or unreadable meta file is always treated as stale.

        Parameters
        ----------
        current_snapshot_hash:
            Hash of the current :class:`RoadmapSnapshot` content.
        ttl_s:
            Maximum permitted age of the cache in seconds.
        """
        stored_meta = self._read_meta()
        if stored_meta is None:
            return True

        hash_mismatch = stored_meta.get("snapshot_hash", "") != current_snapshot_hash
        saved_at = stored_meta.get("saved_at", 0.0)
        age_exceeded = (time.time() - float(saved_at)) > ttl_s
        return hash_mismatch or age_exceeded

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_meta(self) -> Optional[dict]:
        """Return the parsed meta dict, or None on any failure."""
        if not self._meta_path.exists():
            return None
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "HypothesisCache: failed to parse %s — %s", self._meta_path, exc
            )
            return None

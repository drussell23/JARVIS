"""
Advanced preservation signals — Slice 4 of Production Integration.
===================================================================

Two deterministic enrichments on top of Slice 2's
:class:`PreservationScorer`:

* :class:`CrossOpIntentTracker` — reads recent per-op
  :class:`IntentTracker` singletons and aggregates path / tool
  signals across ops. Useful when an operator juggles multiple
  nearby ops on the same files: a chunk in op-B that references
  a path op-A recently focused on gets a modest boost.

* :class:`SemanticClusterer` — near-duplicate detection via
  Jaccard similarity over character n-gram shingles. No
  embeddings, no LLM (§5 Tier 0). Used as a post-selection
  dedup pass: when the scorer wants to keep two highly-similar
  chunks, one is demoted to the compact pool so the operator
  isn't looking at near-duplicates.

Both signals are strictly additive: callers opt in by passing
them to :class:`PreservationScorer` or the
:func:`dedupe_preservation_result` helper. Omitting them leaves
Slice 2 behaviour intact.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import (
    Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple,
)

from backend.core.ouroboros.governance.context_intent import (
    ChunkDecision,
    ChunkScore,
    PreservationResult,
    IntentTracker,
    IntentTrackerRegistry,
)

logger = logging.getLogger("Ouroboros.ContextAdvanced")


ADVANCED_SIGNALS_SCHEMA_VERSION: str = "context_advanced.v1"


# ---------------------------------------------------------------------------
# CrossOpIntentTracker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossOpSnapshot:
    """Aggregated signals across recent ops.

    Weights are sums across participating ops. Paths that appear in
    multiple ops carry higher weight; single-op paths stay close to
    their original intensity.
    """

    participating_op_ids: Tuple[str, ...]
    path_scores: Dict[str, float]
    tool_scores: Dict[str, float]
    error_scores: Dict[str, float]
    schema_version: str = ADVANCED_SIGNALS_SCHEMA_VERSION


class CrossOpIntentTracker:
    """Per-process aggregator of :class:`IntentTracker` snapshots.

    Callers supply either an explicit list of ops (``op_ids``) or a
    cap (``max_ops``) applied to
    :class:`IntentTrackerRegistry.get_or_create`-backed registry.
    Aggregation is O(ops × signals) and produces an immutable
    :class:`CrossOpSnapshot`.
    """

    def __init__(
        self,
        *,
        registry: Optional[IntentTrackerRegistry] = None,
        max_ops: int = 8,
        exclude_op_ids: Optional[Sequence[str]] = None,
    ) -> None:
        from backend.core.ouroboros.governance.context_intent import (
            get_default_tracker_registry,
        )
        self._registry = registry or get_default_tracker_registry()
        self._max_ops = max(1, max_ops)
        self._exclude = frozenset(exclude_op_ids or ())

    def snapshot(
        self,
        *,
        op_ids: Optional[Sequence[str]] = None,
    ) -> CrossOpSnapshot:
        """Aggregate across up to ``max_ops`` ops.

        If ``op_ids`` is given, use exactly that set (bounded by
        ``max_ops``). Otherwise read the registry's ``_trackers`` keys.
        """
        pool: List[str]
        if op_ids is not None:
            pool = list(op_ids)
        else:
            # Use the registry's dict — the newest ops are the most
            # recently added keys.
            pool = list(self._registry._trackers.keys())
        pool = [o for o in pool if o not in self._exclude]
        pool = pool[-self._max_ops:]

        agg_path: Dict[str, float] = {}
        agg_tool: Dict[str, float] = {}
        agg_err: Dict[str, float] = {}
        for op_id in pool:
            tracker = self._registry.get(op_id)
            if tracker is None:
                continue
            snap = tracker.current_intent()
            for k, v in snap.weighted_path_scores.items():
                agg_path[k] = agg_path.get(k, 0.0) + v
            for k, v in snap.weighted_tool_scores.items():
                agg_tool[k] = agg_tool.get(k, 0.0) + v
            for k, v in snap.weighted_error_scores.items():
                agg_err[k] = agg_err.get(k, 0.0) + v

        return CrossOpSnapshot(
            participating_op_ids=tuple(pool),
            path_scores=agg_path,
            tool_scores=agg_tool,
            error_scores=agg_err,
        )

    def score_boost_for_chunk(
        self,
        *,
        chunk_text: str,
        cross_op_snap: CrossOpSnapshot,
        path_weight: float = 1.0,
        tool_weight: float = 0.5,
        error_weight: float = 0.8,
    ) -> float:
        """Return a modest additive boost for a chunk.

        Deliberately lower weights than per-op intent — cross-op is a
        weaker signal. Caller adds this to :class:`ChunkScore.total`
        or passes it as an external signal to the scorer's breakdown.
        """
        from backend.core.ouroboros.governance.context_intent import (
            extract_error_terms,
            extract_paths,
            extract_tool_mentions,
        )
        boost = 0.0
        for p in set(extract_paths(chunk_text)):
            w = cross_op_snap.path_scores.get(p, 0.0)
            if w > 0:
                boost += path_weight * w
        for t in set(extract_tool_mentions(chunk_text)):
            w = cross_op_snap.tool_scores.get(t, 0.0)
            if w > 0:
                boost += tool_weight * w
        for term in set(extract_error_terms(chunk_text)):
            w = cross_op_snap.error_scores.get(term, 0.0)
            if w > 0:
                boost += error_weight * w
        return boost


# ---------------------------------------------------------------------------
# SemanticClusterer — deterministic near-duplicate detection
# ---------------------------------------------------------------------------


def _shingles(text: str, *, size: int = 4) -> FrozenSet[str]:
    """Character n-gram shingle set.

    Deterministic, no embeddings. Lowercased + whitespace-normalised
    so trivial formatting differences don't defeat similarity.
    """
    if not text:
        return frozenset()
    normalised = " ".join(text.lower().split())
    if len(normalised) < size:
        return frozenset({normalised})
    return frozenset(
        normalised[i : i + size] for i in range(len(normalised) - size + 1)
    )


def jaccard_similarity(a: FrozenSet[str], b: FrozenSet[str]) -> float:
    """Classic Jaccard |A∩B| / |A∪B|."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)


class SemanticClusterer:
    """Greedy near-duplicate clusterer.

    Walks a sequence of (chunk_id, text) pairs in input order; each
    chunk either joins an existing cluster (Jaccard similarity to
    representative > ``threshold``) or starts a new cluster.

    Cluster representatives are the FIRST chunk that landed in the
    cluster — so caller-supplied ordering (e.g. score-descending)
    determines which representative wins.
    """

    def __init__(
        self,
        *,
        shingle_size: int = 4,
        threshold: float = 0.85,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0.0, 1.0]")
        self._shingle_size = max(2, shingle_size)
        self._threshold = threshold

    def cluster(
        self,
        items: Iterable[Tuple[str, str]],
    ) -> Dict[str, List[str]]:
        """Return {cluster_representative_id: [member_ids_in_input_order]}."""
        clusters: Dict[str, FrozenSet[str]] = {}
        members: Dict[str, List[str]] = {}
        for chunk_id, text in items:
            shingles = _shingles(text, size=self._shingle_size)
            matched_rep: Optional[str] = None
            for rep_id, rep_shingles in clusters.items():
                if jaccard_similarity(shingles, rep_shingles) > self._threshold:
                    matched_rep = rep_id
                    break
            if matched_rep is None:
                clusters[chunk_id] = shingles
                members[chunk_id] = [chunk_id]
            else:
                members[matched_rep].append(chunk_id)
        return members


def dedupe_preservation_result(
    result: PreservationResult,
    *,
    candidate_text_lookup: Dict[str, str],
    threshold: float = 0.85,
    shingle_size: int = 4,
) -> PreservationResult:
    """Post-process a :class:`PreservationResult` to demote duplicates.

    For each cluster of near-identical kept chunks, keep ONE (the
    highest-scoring — which is the cluster representative under our
    score-ordered scan) and demote the rest from ``KEEP`` to
    ``COMPACT``. Pinned chunks never get demoted.

    Returns a new :class:`PreservationResult` with the same
    ``total_chars_before`` and recomputed ``total_chars_after``.
    """
    if not result.kept:
        return result
    # Respect pins — never demote pinned.
    pinned_ids: Set[str] = {
        s.chunk_id for s in result.kept if s.pin_bonus > 0
    }

    # Walk kept in the scorer's score-DESC order. We don't have that
    # order here directly (result.kept is chronological after
    # select_preserved), so we sort by total DESC + index DESC.
    def _key(s: ChunkScore) -> Tuple[float, int, str]:
        return (-s.total, -s.index_in_sequence, s.chunk_id)

    by_score = sorted(result.kept, key=_key)
    # First pass: identify clusters.
    clusterer = SemanticClusterer(
        shingle_size=shingle_size, threshold=threshold,
    )
    items = [
        (s.chunk_id, candidate_text_lookup.get(s.chunk_id, ""))
        for s in by_score
    ]
    clusters = clusterer.cluster(items)

    # Decide: kept-reps stay KEEP; non-rep cluster members demote
    # (unless pinned).
    demoted_ids: Set[str] = set()
    for rep_id, member_ids in clusters.items():
        for member_id in member_ids:
            if member_id == rep_id:
                continue
            if member_id in pinned_ids:
                continue
            demoted_ids.add(member_id)

    if not demoted_ids:
        return result  # no dupes found

    new_kept: List[ChunkScore] = []
    extra_compacted: List[ChunkScore] = []
    total_chars_after = 0
    for s in result.kept:
        if s.chunk_id in demoted_ids:
            extra_compacted.append(ChunkScore(
                **{**s.__dict__, "decision": ChunkDecision.COMPACT},
            ))
        else:
            new_kept.append(s)
            total_chars_after += len(
                candidate_text_lookup.get(s.chunk_id, "")
            )
    new_compacted = tuple(result.compacted) + tuple(extra_compacted)

    # Re-sort kept chronologically (same convention as select_preserved).
    new_kept.sort(key=lambda s: s.index_in_sequence)

    return PreservationResult(
        kept=tuple(new_kept),
        compacted=new_compacted,
        dropped=result.dropped,
        total_chars_before=result.total_chars_before,
        total_chars_after=total_chars_after,
    )


__all__ = [
    "ADVANCED_SIGNALS_SCHEMA_VERSION",
    "CrossOpIntentTracker",
    "CrossOpSnapshot",
    "SemanticClusterer",
    "dedupe_preservation_result",
    "jaccard_similarity",
]

_ = IntentTracker  # keep import for docstring cross-reference

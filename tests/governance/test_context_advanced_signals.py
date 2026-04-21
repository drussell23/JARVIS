"""Slice 4 tests — cross-op intent + semantic clustering."""
from __future__ import annotations

import math
from typing import Dict, List

import pytest

from backend.core.ouroboros.governance.context_advanced_signals import (
    ADVANCED_SIGNALS_SCHEMA_VERSION,
    CrossOpIntentTracker,
    CrossOpSnapshot,
    SemanticClusterer,
    dedupe_preservation_result,
    jaccard_similarity,
)
from backend.core.ouroboros.governance.context_intent import (
    ChunkCandidate,
    IntentTrackerRegistry,
    PreservationScorer,
    TurnSource,
    intent_tracker_for,
    reset_default_tracker_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_default_tracker_registry()
    yield
    reset_default_tracker_registry()


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert ADVANCED_SIGNALS_SCHEMA_VERSION == "context_advanced.v1"


# ===========================================================================
# Jaccard + shingles
# ===========================================================================


def test_jaccard_identical():
    from backend.core.ouroboros.governance.context_advanced_signals import (
        _shingles,
    )
    a = _shingles("hello world")
    b = _shingles("hello world")
    assert jaccard_similarity(a, b) == 1.0


def test_jaccard_empty_pair_is_one():
    assert jaccard_similarity(frozenset(), frozenset()) == 1.0


def test_jaccard_asymmetric_one_empty():
    assert jaccard_similarity(frozenset({"a"}), frozenset()) == 0.0


def test_jaccard_near_duplicate_high():
    from backend.core.ouroboros.governance.context_advanced_signals import (
        _shingles,
    )
    a = _shingles("the quick brown fox")
    b = _shingles("the quick brown fox!")  # trailing punct
    assert jaccard_similarity(a, b) > 0.85


def test_jaccard_different_low():
    from backend.core.ouroboros.governance.context_advanced_signals import (
        _shingles,
    )
    a = _shingles("completely different string alpha")
    b = _shingles("another unrelated string beta omega")
    assert jaccard_similarity(a, b) < 0.5


def test_shingles_normalises_whitespace():
    from backend.core.ouroboros.governance.context_advanced_signals import (
        _shingles,
    )
    a = _shingles("hello world")
    b = _shingles("hello     world")  # extra spaces
    assert a == b


def test_shingles_short_text_fallback():
    from backend.core.ouroboros.governance.context_advanced_signals import (
        _shingles,
    )
    assert _shingles("hi", size=4) == frozenset({"hi"})


# ===========================================================================
# SemanticClusterer
# ===========================================================================


def test_clusterer_groups_identical_chunks():
    c = SemanticClusterer(threshold=0.85)
    clusters = c.cluster([
        ("a", "exact same content"),
        ("b", "exact same content"),
        ("c", "something totally different"),
    ])
    # 'a' and 'b' cluster together; 'c' is its own cluster
    assert len(clusters) == 2
    assert "b" in clusters["a"] or "a" in clusters["b"]


def test_clusterer_keeps_distinct_separate():
    c = SemanticClusterer(threshold=0.85)
    clusters = c.cluster([
        ("a", "apple orange banana"),
        ("b", "frobnicate the cromulent widget"),
        ("c", "totally unrelated poetry"),
    ])
    assert len(clusters) == 3


def test_clusterer_threshold_boundary():
    """Tighter threshold → more singleton clusters."""
    c_loose = SemanticClusterer(threshold=0.3)
    c_strict = SemanticClusterer(threshold=0.99)
    items = [
        ("a", "hello world"),
        ("b", "hello there"),
    ]
    # Loose threshold: 'hello' shared → one cluster
    loose_out = c_loose.cluster(items)
    strict_out = c_strict.cluster(items)
    assert len(loose_out) <= len(strict_out)


def test_clusterer_rejects_bad_threshold():
    with pytest.raises(ValueError):
        SemanticClusterer(threshold=0.0)
    with pytest.raises(ValueError):
        SemanticClusterer(threshold=1.5)


def test_clusterer_input_order_picks_representative():
    """The FIRST chunk seen that starts a cluster becomes representative."""
    c = SemanticClusterer(threshold=0.85)
    clusters = c.cluster([
        ("first", "repeated content repeated content"),
        ("second", "repeated content repeated content"),
    ])
    # 'first' is the representative key
    assert "first" in clusters
    assert "second" not in clusters


# ===========================================================================
# dedupe_preservation_result
# ===========================================================================


def test_dedupe_demotes_near_duplicates():
    tracker = intent_tracker_for("op-d")
    tracker.ingest_turn("focus backend/x.py", source=TurnSource.USER)
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(chunk_id="a", text="backend/x.py edited once",
                       index_in_sequence=0, role="user"),
        ChunkCandidate(chunk_id="b", text="backend/x.py edited once",
                       index_in_sequence=1, role="user"),
        ChunkCandidate(chunk_id="c", text="completely different chunk",
                       index_in_sequence=2, role="user"),
    ]
    result = scorer.select_preserved(
        cands, tracker.current_intent(), max_chunks=3,
    )
    assert len(result.kept) == 3
    text_lookup = {c.chunk_id: c.text for c in cands}
    deduped = dedupe_preservation_result(
        result, candidate_text_lookup=text_lookup,
    )
    # Only one of a/b remains in kept; the other demoted to compacted
    kept_ids = {s.chunk_id for s in deduped.kept}
    assert len(kept_ids) == 2
    assert "c" in kept_ids
    assert "a" in kept_ids or "b" in kept_ids
    # Combined kept + compacted + dropped count equals original input count
    assert (
        len(deduped.kept) + len(deduped.compacted) + len(deduped.dropped)
        == 3
    )


def test_dedupe_never_demotes_pinned():
    tracker = intent_tracker_for("op-d2")
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(chunk_id="pinned", text="same content",
                       index_in_sequence=0, role="tool", pinned=True),
        ChunkCandidate(chunk_id="unpinned", text="same content",
                       index_in_sequence=1, role="tool"),
    ]
    result = scorer.select_preserved(
        cands, tracker.current_intent(), max_chunks=2,
    )
    text_lookup = {c.chunk_id: c.text for c in cands}
    deduped = dedupe_preservation_result(
        result, candidate_text_lookup=text_lookup,
    )
    kept_ids = {s.chunk_id for s in deduped.kept}
    # pinned ALWAYS survives; the unpinned one is the dupe that gets demoted
    assert "pinned" in kept_ids
    # unpinned may be demoted
    assert "unpinned" not in kept_ids


def test_dedupe_no_dupes_passes_through_unchanged():
    tracker = intent_tracker_for("op-d3")
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(chunk_id="a", text="alpha",
                       index_in_sequence=0, role="user"),
        ChunkCandidate(chunk_id="b", text="beta",
                       index_in_sequence=1, role="user"),
    ]
    result = scorer.select_preserved(
        cands, tracker.current_intent(), max_chunks=2,
    )
    text_lookup = {c.chunk_id: c.text for c in cands}
    deduped = dedupe_preservation_result(
        result, candidate_text_lookup=text_lookup,
    )
    # No dupes found → same result object (optimised)
    assert len(deduped.kept) == len(result.kept)


def test_dedupe_handles_empty_result():
    result = PreservationScorer().select_preserved(
        [], intent_tracker_for("op-d4").current_intent(),
    )
    deduped = dedupe_preservation_result(result, candidate_text_lookup={})
    assert deduped.kept == ()


# ===========================================================================
# CrossOpIntentTracker
# ===========================================================================


def test_cross_op_aggregates_paths_across_ops():
    registry = IntentTrackerRegistry()
    a = registry.get_or_create("op-a")
    b = registry.get_or_create("op-b")
    a.ingest_turn("work on backend/shared.py", source=TurnSource.USER)
    b.ingest_turn("also backend/shared.py", source=TurnSource.USER)
    b.ingest_turn("and backend/unique.py", source=TurnSource.USER)

    cross = CrossOpIntentTracker(registry=registry)
    snap = cross.snapshot()
    # backend/shared.py appears in both ops → higher weight than unique
    assert snap.path_scores.get("backend/shared.py", 0) > \
        snap.path_scores.get("backend/unique.py", 0)


def test_cross_op_excludes_op_ids():
    registry = IntentTrackerRegistry()
    a = registry.get_or_create("op-a")
    b = registry.get_or_create("op-b")
    a.ingest_turn("backend/x.py", source=TurnSource.USER)
    b.ingest_turn("backend/y.py", source=TurnSource.USER)
    cross = CrossOpIntentTracker(
        registry=registry, exclude_op_ids=["op-b"],
    )
    snap = cross.snapshot()
    assert "op-b" not in snap.participating_op_ids
    assert "backend/y.py" not in snap.path_scores


def test_cross_op_respects_max_ops_cap():
    registry = IntentTrackerRegistry()
    for i in range(10):
        t = registry.get_or_create(f"op-{i}")
        t.ingest_turn(f"backend/file{i}.py", source=TurnSource.USER)
    cross = CrossOpIntentTracker(registry=registry, max_ops=3)
    snap = cross.snapshot()
    assert len(snap.participating_op_ids) == 3


def test_cross_op_score_boost_path():
    registry = IntentTrackerRegistry()
    a = registry.get_or_create("op-a")
    a.ingest_turn("backend/shared.py", source=TurnSource.USER)
    cross = CrossOpIntentTracker(registry=registry)
    snap = cross.snapshot()
    boost = cross.score_boost_for_chunk(
        chunk_text="we touched backend/shared.py",
        cross_op_snap=snap,
    )
    assert boost > 0


def test_cross_op_score_boost_zero_for_unrelated_chunk():
    registry = IntentTrackerRegistry()
    a = registry.get_or_create("op-a")
    a.ingest_turn("backend/x.py", source=TurnSource.USER)
    cross = CrossOpIntentTracker(registry=registry)
    snap = cross.snapshot()
    boost = cross.score_boost_for_chunk(
        chunk_text="no mention of anything",
        cross_op_snap=snap,
    )
    assert boost == 0


def test_cross_op_score_boost_weights_configurable():
    registry = IntentTrackerRegistry()
    a = registry.get_or_create("op-a")
    for _ in range(5):
        a.ingest_turn("backend/x.py", source=TurnSource.USER)
    cross = CrossOpIntentTracker(registry=registry)
    snap = cross.snapshot()
    low = cross.score_boost_for_chunk(
        chunk_text="backend/x.py",
        cross_op_snap=snap, path_weight=0.1,
    )
    high = cross.score_boost_for_chunk(
        chunk_text="backend/x.py",
        cross_op_snap=snap, path_weight=10.0,
    )
    assert high > low * 10


def test_cross_op_explicit_op_ids_override_registry():
    registry = IntentTrackerRegistry()
    a = registry.get_or_create("op-a")
    b = registry.get_or_create("op-b")
    a.ingest_turn("backend/a.py", source=TurnSource.USER)
    b.ingest_turn("backend/b.py", source=TurnSource.USER)
    cross = CrossOpIntentTracker(registry=registry)
    snap = cross.snapshot(op_ids=["op-a"])
    assert "backend/a.py" in snap.path_scores
    assert "backend/b.py" not in snap.path_scores


def test_cross_op_missing_op_id_silently_skipped():
    registry = IntentTrackerRegistry()
    cross = CrossOpIntentTracker(registry=registry)
    snap = cross.snapshot(op_ids=["op-does-not-exist"])
    assert snap.path_scores == {}
    assert snap.participating_op_ids == ("op-does-not-exist",)


# ===========================================================================
# End-to-end: cross-op boost + dedup used together
# ===========================================================================


def test_end_to_end_cross_op_plus_dedup():
    """Cross-op boost promotes shared-path chunks; dedup removes twins."""
    registry = IntentTrackerRegistry()
    other_op = registry.get_or_create("op-peer")
    other_op.ingest_turn("backend/shared.py", source=TurnSource.USER)
    cross = CrossOpIntentTracker(registry=registry)
    snap = cross.snapshot()

    current_tracker = registry.get_or_create("op-main")
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(
            chunk_id="shared-ref-1",
            text="backend/shared.py touched here",
            index_in_sequence=0, role="user",
        ),
        ChunkCandidate(
            chunk_id="shared-ref-2",
            text="backend/shared.py touched here",
            index_in_sequence=1, role="user",
        ),
        ChunkCandidate(
            chunk_id="unrelated",
            text="completely different content",
            index_in_sequence=2, role="user",
        ),
    ]
    result = scorer.select_preserved(
        cands, current_tracker.current_intent(), max_chunks=3,
    )
    # Dedup the twins
    text_lookup = {c.chunk_id: c.text for c in cands}
    deduped = dedupe_preservation_result(
        result, candidate_text_lookup=text_lookup,
    )
    kept_ids = {s.chunk_id for s in deduped.kept}
    # One of shared-ref-* remains; the other is demoted
    assert len({"shared-ref-1", "shared-ref-2"} & kept_ids) == 1
    assert "unrelated" in kept_ids

    # Cross-op boost works independently of dedup
    boost_ref = cross.score_boost_for_chunk(
        chunk_text="backend/shared.py touched here",
        cross_op_snap=snap,
    )
    boost_other = cross.score_boost_for_chunk(
        chunk_text="completely different content",
        cross_op_snap=snap,
    )
    assert boost_ref > boost_other

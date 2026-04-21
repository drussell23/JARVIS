"""Slice 2 tests — IntentTracker + PreservationScorer.

Pins:
* Path / tool / error-term extraction correctness + false-positive resistance
* §1 authority: only user / orchestrator turns shift intent; assistant + tool
  turns are ignored
* Recency decay: a signal N turns old decays by half_life
* Ledger entries contribute to intent via ingest_ledger_entry()
* PreservationScorer.score produces a total ordering
* select_preserved honours budget, pins, ratio split
* Score-ordered selection beats fixed "last N" for intent-relevant chunks
* ChunkScore.breakdown records every signal's contribution (§8)
* Tracker + Scorer are deterministic (same input → same output, 2 runs)
"""
from __future__ import annotations

import math
from typing import List, Tuple

import pytest

from backend.core.ouroboros.governance.context_intent import (
    ChunkCandidate,
    ChunkDecision,
    IntentTracker,
    IntentTrackerRegistry,
    PreservationScorer,
    TurnSource,
    extract_error_terms,
    extract_paths,
    extract_tool_mentions,
    intent_tracker_for,
    reset_default_tracker_registry,
)


@pytest.fixture(autouse=True)
def _clean_trackers():
    reset_default_tracker_registry()
    yield
    reset_default_tracker_registry()


# ===========================================================================
# Path extraction
# ===========================================================================


def test_extract_paths_finds_canonical_source_paths():
    text = "please look at backend/foo.py and tests/test_auth.py, also src/lib.ts"
    paths = extract_paths(text)
    assert "backend/foo.py" in paths
    assert "tests/test_auth.py" in paths
    assert "src/lib.ts" in paths


def test_extract_paths_finds_bare_source_file():
    text = "the bug is in utils.py"
    paths = extract_paths(text)
    assert "utils.py" in paths


def test_extract_paths_rejects_english_prose():
    text = "I think this.is.not.a.path but file.py is"
    paths = extract_paths(text)
    # file.py IS a path (known suffix); this.is.not.a.path is NOT.
    assert "file.py" in paths
    assert "this.is.not.a.path" not in paths


def test_extract_paths_deduplicates_preserving_order():
    text = "first backend/foo.py then tests/bar.py and backend/foo.py again"
    paths = extract_paths(text)
    assert paths == ["backend/foo.py", "tests/bar.py"]


def test_extract_paths_handles_empty():
    assert extract_paths("") == []


# ===========================================================================
# Tool extraction
# ===========================================================================


def test_extract_tool_mentions_finds_known_tools():
    text = "I ran read_file and then called search_code"
    tools = extract_tool_mentions(text)
    assert tools == ["read_file", "search_code"]


def test_extract_tool_mentions_ignores_unknown_words():
    text = "try calling my_custom_helper or special_fn_x"
    assert extract_tool_mentions(text) == []


def test_extract_tool_mentions_whole_word_only():
    text = "read_file_variant is not a tool but read_file is"
    tools = extract_tool_mentions(text)
    # 'read_file_variant' does NOT match because the suffix breaks word
    # boundary
    assert tools == ["read_file"]


# ===========================================================================
# Error term extraction
# ===========================================================================


def test_extract_error_terms_matches_lexicon():
    text = "got an ImportError in the test, traceback below"
    terms = extract_error_terms(text)
    assert "importerror" in terms
    assert "traceback" in terms


def test_extract_error_terms_ignores_unknown_word():
    text = "everything is fine, working as intended"
    assert extract_error_terms(text) == []


# ===========================================================================
# IntentTracker — authority boundary (§1)
# ===========================================================================


def test_user_turn_shifts_intent():
    tracker = IntentTracker("op-1")
    tracker.ingest_turn("focus on backend/auth.py", source=TurnSource.USER)
    intent = tracker.current_intent()
    assert "backend/auth.py" in intent.recent_paths
    assert intent.turn_count == 1


def test_orchestrator_turn_shifts_intent():
    tracker = IntentTracker("op-1")
    tracker.ingest_turn(
        "pivoting to src/lib.ts", source=TurnSource.ORCHESTRATOR,
    )
    intent = tracker.current_intent()
    assert "src/lib.ts" in intent.recent_paths


def test_assistant_turn_does_not_shift_intent():
    """§1: model claims do not move intent."""
    tracker = IntentTracker("op-1")
    tracker.ingest_turn(
        "I'm going to focus on backend/secrets.py",
        source=TurnSource.ASSISTANT,
    )
    intent = tracker.current_intent()
    assert "backend/secrets.py" not in intent.recent_paths
    # Turn counter also doesn't advance.
    assert intent.turn_count == 0


def test_tool_turn_does_not_shift_intent():
    tracker = IntentTracker("op-1")
    tracker.ingest_turn("read backend/x.py", source=TurnSource.TOOL)
    assert tracker.turn_count == 0
    assert tracker.current_intent().recent_paths == ()


# ===========================================================================
# Recency decay
# ===========================================================================


def test_recency_decay_fades_old_signals():
    tracker = IntentTracker("op-1", half_life_turns=2.0)
    tracker.ingest_turn("start with backend/old.py", source=TurnSource.USER)
    # 10 unrelated turns pass
    for _ in range(10):
        tracker.ingest_turn("just talking", source=TurnSource.USER)
    tracker.ingest_turn("now look at backend/new.py", source=TurnSource.USER)
    intent = tracker.current_intent()
    # new.py must outweigh old.py
    old_w = intent.weighted_path_scores.get("backend/old.py", 0.0)
    new_w = intent.weighted_path_scores.get("backend/new.py", 0.0)
    assert new_w > old_w * 5


def test_stale_signals_drop_below_floor():
    tracker = IntentTracker("op-1", half_life_turns=1.0)
    tracker.ingest_turn("backend/ancient.py once", source=TurnSource.USER)
    for _ in range(20):
        tracker.ingest_turn("more", source=TurnSource.USER)
    assert "backend/ancient.py" not in tracker.current_intent().recent_paths


def test_reinforced_signal_stays_strong():
    tracker = IntentTracker("op-1", half_life_turns=2.0)
    for _ in range(5):
        tracker.ingest_turn("backend/hot.py", source=TurnSource.USER)
    for _ in range(5):
        tracker.ingest_turn("unrelated", source=TurnSource.USER)
    tracker.ingest_turn("backend/hot.py", source=TurnSource.USER)
    intent = tracker.current_intent()
    assert "backend/hot.py" in intent.recent_paths


# ===========================================================================
# Ledger integration
# ===========================================================================


def test_ingest_file_read_ledger_entry_bumps_path_signal():
    tracker = IntentTracker("op-1")
    tracker.ingest_ledger_entry({
        "kind": "file_read",
        "file_path": "backend/auth.py",
    })
    assert "backend/auth.py" in tracker.current_intent().recent_paths


def test_ingest_tool_call_entry_bumps_tool_signal():
    tracker = IntentTracker("op-1")
    tracker.ingest_ledger_entry({
        "kind": "tool_call",
        "tool": "edit_file",
    })
    assert "edit_file" in tracker.current_intent().recent_tools


def test_ingest_error_entry_captures_terms_and_paths():
    tracker = IntentTracker("op-1")
    tracker.ingest_ledger_entry({
        "kind": "error",
        "message": "ImportError: foo",
        "where": "backend/x.py:12",
    })
    intent = tracker.current_intent()
    assert "importerror" in intent.recent_error_terms
    assert "backend/x.py:12" in intent.recent_paths \
        or "backend/x.py" in intent.recent_paths


def test_ingest_question_entry_captures_related_paths_and_tools():
    tracker = IntentTracker("op-1")
    tracker.ingest_ledger_entry({
        "kind": "question",
        "related_paths": ["backend/foo.py"],
        "related_tools": ["edit_file"],
    })
    intent = tracker.current_intent()
    assert "backend/foo.py" in intent.recent_paths
    assert "edit_file" in intent.recent_tools


def test_ingest_ledger_does_not_advance_turn_clock():
    tracker = IntentTracker("op-1")
    for _ in range(5):
        tracker.ingest_ledger_entry({
            "kind": "file_read", "file_path": "x.py",
        })
    assert tracker.turn_count == 0


# ===========================================================================
# PreservationScorer — score ordering
# ===========================================================================


def _score_fixture():
    tracker = IntentTracker("op-1")
    tracker.ingest_turn(
        "let's fix backend/auth.py — getting ImportError",
        source=TurnSource.USER,
    )
    scorer = PreservationScorer()
    intent = tracker.current_intent()
    return intent, scorer


def test_score_intent_match_outweighs_recency():
    intent, scorer = _score_fixture()
    # Chunk 0 (oldest) mentions the focus path; chunk 9 (newest) is noise
    target = ChunkCandidate(
        chunk_id="target", text="we read backend/auth.py earlier",
        index_in_sequence=0, role="user",
    )
    noise = ChunkCandidate(
        chunk_id="noise", text="unrelated chatter here",
        index_in_sequence=9, role="assistant",
    )
    t_score = scorer.score(target, intent, newest_index=9)
    n_score = scorer.score(noise, intent, newest_index=9)
    assert t_score.total > n_score.total, (
        f"intent-match target ({t_score.total:.2f}) should beat "
        f"noise ({n_score.total:.2f})"
    )


def test_score_pin_dominates():
    intent, scorer = _score_fixture()
    pinned_noise = ChunkCandidate(
        chunk_id="pinned", text="zzz", index_in_sequence=0, role="tool",
        pinned=True,
    )
    score = scorer.score(pinned_noise, intent, newest_index=9)
    assert score.total == math.inf


def test_score_breakdown_itemises_every_signal():
    intent, scorer = _score_fixture()
    chunk = ChunkCandidate(
        chunk_id="c1",
        text="ImportError in backend/auth.py when calling read_file",
        index_in_sequence=5, role="user",
    )
    s = scorer.score(chunk, intent, newest_index=9)
    keys = [k for k, _ in s.breakdown]
    assert "base_recency" in keys
    assert "structural_role" in keys
    assert any(k.startswith("intent_path:") for k in keys)
    assert any(k.startswith("intent_error:") for k in keys)


# ===========================================================================
# PreservationScorer — select_preserved
# ===========================================================================


def _chunks(*specs: Tuple[str, str, str]) -> List[ChunkCandidate]:
    return [
        ChunkCandidate(chunk_id=cid, text=txt, index_in_sequence=idx, role=role)
        for idx, (cid, txt, role) in enumerate(specs)
    ]


def test_select_preserved_fills_budget_by_score():
    tracker = IntentTracker("op-1")
    tracker.ingest_turn(
        "focus on backend/hot.py", source=TurnSource.USER,
    )
    intent = tracker.current_intent()
    scorer = PreservationScorer()
    cands = _chunks(
        ("c0", "legacy context blob", "system"),
        ("c1", "mention of backend/hot.py from ages ago", "user"),
        ("c2", "unrelated stuff", "assistant"),
        ("c3", "more noise", "assistant"),
        ("c4", "I touched backend/hot.py again", "user"),
        ("c5", "final turn — fresh", "user"),
    )
    result = scorer.select_preserved(
        cands, intent, max_chunks=3, keep_ratio=0.5,
    )
    kept_ids = {s.chunk_id for s in result.kept}
    # Both hot.py mentions must be kept
    assert "c1" in kept_ids
    assert "c4" in kept_ids
    assert len(result.kept) == 3


def test_select_preserved_respects_pins_even_past_budget():
    tracker = IntentTracker("op-1")
    intent = tracker.current_intent()
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(chunk_id="junk", text="zzz", index_in_sequence=0,
                       role="tool", pinned=True),
        ChunkCandidate(chunk_id="fresh", text="user turn", index_in_sequence=1,
                       role="user"),
    ]
    result = scorer.select_preserved(cands, intent, max_chunks=1)
    kept_ids = {s.chunk_id for s in result.kept}
    # Pin survives even though budget is 1 and junk is older + tool-role
    assert "junk" in kept_ids
    # fresh is forced out of kept, goes to compacted or dropped
    assert "fresh" not in kept_ids


def test_select_preserved_decision_field_set_per_chunk():
    tracker = IntentTracker("op-1")
    intent = tracker.current_intent()
    scorer = PreservationScorer()
    cands = _chunks(
        ("a", "a", "user"), ("b", "b", "user"), ("c", "c", "user"),
    )
    result = scorer.select_preserved(
        cands, intent, max_chunks=1, keep_ratio=0.5,
    )
    all_scores = list(result.kept) + list(result.compacted) + list(result.dropped)
    assert all(s.decision is not None for s in all_scores)
    decisions = {s.decision for s in all_scores}
    assert ChunkDecision.KEEP in decisions


def test_select_preserved_deterministic():
    tracker = IntentTracker("op-1")
    tracker.ingest_turn("backend/x.py", source=TurnSource.USER)
    intent = tracker.current_intent()
    scorer = PreservationScorer()
    cands = _chunks(
        ("c0", "text0 backend/x.py", "user"),
        ("c1", "text1", "assistant"),
        ("c2", "text2 backend/x.py", "user"),
    )
    r1 = scorer.select_preserved(cands, intent, max_chunks=2)
    r2 = scorer.select_preserved(cands, intent, max_chunks=2)
    assert [s.chunk_id for s in r1.kept] == [s.chunk_id for s in r2.kept]


def test_select_preserved_char_budget():
    tracker = IntentTracker("op-1")
    intent = tracker.current_intent()
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(chunk_id=f"c{i}", text="x" * 100,
                       index_in_sequence=i, role="user")
        for i in range(10)
    ]
    # Budget allows 3 chunks of 100 chars each, plus some slack
    result = scorer.select_preserved(cands, intent, max_chars=350)
    assert result.total_chars_after <= 350
    assert len(result.kept) == 3


def test_select_preserved_empty_input():
    tracker = IntentTracker("op-1")
    intent = tracker.current_intent()
    scorer = PreservationScorer()
    result = scorer.select_preserved([], intent, max_chunks=5)
    assert result.kept == ()
    assert result.compacted == ()
    assert result.dropped == ()


def test_select_preserved_ordering_is_chronological_on_output():
    """Kept chunks should be re-sorted by index_in_sequence for emission."""
    tracker = IntentTracker("op-1")
    tracker.ingest_turn("backend/x.py", source=TurnSource.USER)
    intent = tracker.current_intent()
    scorer = PreservationScorer()
    cands = _chunks(
        ("c0", "backend/x.py oldest", "user"),
        ("c1", "chatter", "assistant"),
        ("c2", "backend/x.py newer", "user"),
    )
    result = scorer.select_preserved(cands, intent, max_chunks=3)
    indices = [s.index_in_sequence for s in result.kept]
    assert indices == sorted(indices)


# ===========================================================================
# Comparison against "keep last N" (the problem this slice fixes)
# ===========================================================================


def test_score_selection_preserves_intent_chunk_that_last_N_would_drop():
    """The flagship problem CC's feedback called out.

    If the operator mentioned backend/auth.py 15 turns ago and the agent
    is now doing unrelated reads, naive "keep last 6" drops the
    intent-relevant context. Score-ordered selection preserves it.
    """
    tracker = IntentTracker("op-1", half_life_turns=20.0)  # slow decay
    tracker.ingest_turn(
        "keep focus on backend/auth.py", source=TurnSource.USER,
    )
    intent = tracker.current_intent()
    scorer = PreservationScorer()

    # 15 chunks: chunk 0 is the intent-rich one, chunks 1-14 are noise.
    cands: List[ChunkCandidate] = [
        ChunkCandidate(
            chunk_id="intent_rich",
            text="earlier we examined backend/auth.py in depth",
            index_in_sequence=0, role="user",
        ),
    ]
    for i in range(1, 15):
        cands.append(ChunkCandidate(
            chunk_id=f"noise_{i}",
            text=f"noise {i}",
            index_in_sequence=i, role="assistant",
        ))

    # Budget = 6 chunks (same as O+V's legacy _PRESERVE_RECENT=6)
    result = scorer.select_preserved(cands, intent, max_chunks=6)
    kept_ids = {s.chunk_id for s in result.kept}
    # Legacy "keep last 6" would return {noise_9, noise_10, ..., noise_14}
    # and DROP intent_rich. Intent-aware selection keeps intent_rich.
    assert "intent_rich" in kept_ids, (
        "intent-rich chunk must be preserved over recent noise; "
        "that's the whole point of this arc"
    )


# ===========================================================================
# Registry
# ===========================================================================


def test_registry_isolates_ops():
    r = IntentTrackerRegistry()
    a = r.get_or_create("op-a")
    b = r.get_or_create("op-b")
    a.ingest_turn("backend/a.py", source=TurnSource.USER)
    assert "backend/a.py" not in b.current_intent().recent_paths


def test_registry_singleton_shared_via_default():
    a = intent_tracker_for("op-x")
    b = intent_tracker_for("op-x")
    assert a is b


def test_registry_rejects_empty_op_id():
    r = IntentTrackerRegistry()
    with pytest.raises(ValueError):
        r.get_or_create("")


# ===========================================================================
# Schema version
# ===========================================================================


def test_intent_snapshot_schema_version_stable():
    from backend.core.ouroboros.governance.context_intent import (
        INTENT_TRACKER_SCHEMA_VERSION,
    )
    assert INTENT_TRACKER_SCHEMA_VERSION == "context_intent.v1"
    snap = IntentTracker("op-1").current_intent()
    assert snap.schema_version == "context_intent.v1"

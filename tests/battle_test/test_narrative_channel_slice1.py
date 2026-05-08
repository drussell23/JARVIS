"""Tests for narrative_channel (Gap #6 Slice 1)."""
from __future__ import annotations

import threading

import pytest

from backend.core.ouroboros.battle_test.narrative_channel import (
    BUFFER_SIZE_ENV_VAR,
    FrameState,
    NARRATIVE_CHANNEL_SCHEMA_VERSION,
    NarrativeChannel,
    NarrativeFrame,
    NarrativeKind,
    REF_PREFIX,
    get_default_channel,
    reset_default_channel_for_tests,
)


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(BUFFER_SIZE_ENV_VAR, raising=False)
    reset_default_channel_for_tests()
    yield
    reset_default_channel_for_tests()


# ===========================================================================
# Schema + closed taxonomies
# ===========================================================================


def test_schema_version_pinned():
    assert NARRATIVE_CHANNEL_SCHEMA_VERSION == "narrative_channel.v1"


def test_ref_prefix_is_n_dash():
    assert REF_PREFIX == "n-"


def test_narrative_kind_closed_seven_values():
    """Closed taxonomy extended 2026-05-08 (§38.11-D) — added
    DREAM for DreamEngine speculative-blueprint prose."""
    assert {m.value for m in NarrativeKind} == {
        "intent", "plan_prose", "tool_preamble",
        "thinking", "l2_repair_prose", "postmortem_prose",
        "dream",
    }


def test_frame_state_closed_three_values():
    assert {m.value for m in FrameState} == {
        "buffering", "committed", "discarded",
    }


@pytest.mark.parametrize("state,terminal", [
    (FrameState.BUFFERING, False),
    (FrameState.COMMITTED, True),
    (FrameState.DISCARDED, True),
])
def test_frame_state_is_terminal(state, terminal):
    assert state.is_terminal is terminal


@pytest.mark.parametrize("raw,expected", [
    ("intent", NarrativeKind.INTENT),
    ("PLAN_PROSE", NarrativeKind.PLAN_PROSE),
    ("  thinking  ", NarrativeKind.THINKING),
    (NarrativeKind.POSTMORTEM_PROSE, NarrativeKind.POSTMORTEM_PROSE),
])
def test_kind_coerce_recognized(raw, expected):
    assert NarrativeKind.coerce(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "garbage", 42])
def test_kind_coerce_unknowns_default_to_thinking(raw):
    assert NarrativeKind.coerce(raw) is NarrativeKind.THINKING


# ===========================================================================
# Capacity construction
# ===========================================================================


def test_default_capacity_is_200():
    assert NarrativeChannel().capacity == 200


def test_explicit_capacity():
    assert NarrativeChannel(capacity=10).capacity == 10


def test_env_capacity(monkeypatch):
    monkeypatch.setenv(BUFFER_SIZE_ENV_VAR, "75")
    assert NarrativeChannel().capacity == 75


def test_capacity_clamped(monkeypatch):
    monkeypatch.setenv(BUFFER_SIZE_ENV_VAR, "0")
    assert NarrativeChannel().capacity == 1
    monkeypatch.setenv(BUFFER_SIZE_ENV_VAR, "999999")
    assert NarrativeChannel().capacity == 10_000


def test_env_garbage_falls_back():
    import os
    os.environ[BUFFER_SIZE_ENV_VAR] = "not-int"
    try:
        assert NarrativeChannel().capacity == 200
    finally:
        del os.environ[BUFFER_SIZE_ENV_VAR]


# ===========================================================================
# start_frame + ref allocation
# ===========================================================================


def test_start_frame_allocates_n_dash():
    ch = NarrativeChannel(capacity=10)
    frame = ch.start_frame(
        op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE,
        provider="claude",
    )
    assert isinstance(frame, NarrativeFrame)
    assert frame.ref == "n-1"
    assert frame.state is FrameState.BUFFERING
    assert frame.prose == ""
    assert frame.kind is NarrativeKind.PLAN_PROSE


def test_start_frame_idempotent_for_same_composite():
    ch = NarrativeChannel(capacity=10)
    a = ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    b = ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    assert a.ref == b.ref


def test_start_frame_distinct_for_different_kinds_same_op():
    """Multiple parallel frames per op are supported as long as kind
    differs (matches the real-world case of THINKING + PLAN_PROSE
    interleaved)."""
    ch = NarrativeChannel(capacity=10)
    a = ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    b = ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.THINKING)
    assert a.ref != b.ref


def test_refs_monotonic():
    ch = NarrativeChannel(capacity=10)
    refs = [
        ch.start_frame(op_id=f"op-{i}", phase="PLAN", kind=NarrativeKind.PLAN_PROSE).ref
        for i in range(5)
    ]
    assert refs == ["n-1", "n-2", "n-3", "n-4", "n-5"]


# ===========================================================================
# append_token
# ===========================================================================


def test_append_token_accumulates():
    ch = NarrativeChannel(capacity=10)
    ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    assert ch.append_token(
        op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE,
        token="I'll start ",
    ) is True
    assert ch.append_token(
        op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE,
        token="by reading the file.",
    ) is True
    frames = ch.find_by_op_id("op-x")
    assert frames[0].prose == "I'll start by reading the file."


def test_append_to_unstarted_returns_false():
    ch = NarrativeChannel(capacity=10)
    assert ch.append_token(
        op_id="never", phase="PLAN", kind=NarrativeKind.PLAN_PROSE,
        token="x",
    ) is False


def test_append_after_commit_returns_false():
    ch = NarrativeChannel(capacity=10)
    ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    ch.commit(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    assert ch.append_token(
        op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE,
        token="late",
    ) is False


def test_append_empty_token_returns_false():
    ch = NarrativeChannel(capacity=10)
    ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    assert ch.append_token(
        op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE,
        token="",
    ) is False


def test_append_token_handles_non_string_input():
    ch = NarrativeChannel(capacity=10)
    ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    # Coerced to string via _safe_str
    assert ch.append_token(
        op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE,
        token=42,
    ) is True
    assert ch.find_by_op_id("op-x")[0].prose == "42"


# ===========================================================================
# commit / discard
# ===========================================================================


def test_commit_transitions_state():
    ch = NarrativeChannel(capacity=10)
    ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    ch.append_token(
        op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE,
        token="done",
    )
    out = ch.commit(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    assert out.state is FrameState.COMMITTED
    assert out.terminal_at > 0.0


def test_discard_transitions_state():
    ch = NarrativeChannel(capacity=10)
    ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    out = ch.discard(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    assert out.state is FrameState.DISCARDED


def test_commit_unknown_returns_none():
    ch = NarrativeChannel(capacity=10)
    assert ch.commit(op_id="never", phase="PLAN", kind=NarrativeKind.PLAN_PROSE) is None


def test_commit_releases_active_key():
    """After commit, the same composite key can start a fresh frame."""
    ch = NarrativeChannel(capacity=10)
    a = ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    ch.commit(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    b = ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    assert a.ref != b.ref


# ===========================================================================
# emit_complete — one-shot helper
# ===========================================================================


def test_emit_complete_round_trip():
    ch = NarrativeChannel(capacity=10)
    out = ch.emit_complete(
        op_id="op-x", phase="GENERATE", kind=NarrativeKind.TOOL_PREAMBLE,
        prose="I need to read auth.py to understand JWT validation",
        provider="synthesized",
    )
    assert out is not None
    assert out.state is FrameState.COMMITTED
    assert "JWT validation" in out.prose


def test_emit_complete_empty_prose_returns_none():
    ch = NarrativeChannel(capacity=10)
    assert ch.emit_complete(
        op_id="op-x", phase="GENERATE", kind=NarrativeKind.TOOL_PREAMBLE,
        prose="",
    ) is None


# ===========================================================================
# Eviction — drop-oldest
# ===========================================================================


def test_eviction_drops_oldest():
    ch = NarrativeChannel(capacity=3)
    refs = [
        ch.emit_complete(
            op_id=f"op-{i}", phase="PLAN",
            kind=NarrativeKind.PLAN_PROSE, prose=f"prose-{i}",
        ).ref
        for i in range(5)
    ]
    assert ch.lookup(refs[0]) is None
    assert ch.lookup(refs[1]) is None
    assert ch.lookup(refs[2]) is not None


def test_eviction_prunes_active_key_for_buffering_frame():
    ch = NarrativeChannel(capacity=2)
    # Both still BUFFERING
    ch.start_frame(op_id="op-1", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    ch.start_frame(op_id="op-2", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    # Push capacity over
    ch.start_frame(op_id="op-3", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    # op-1 evicted; subsequent append must NOT misroute
    keys_active = ch.active_keys()
    keys_set = {k for k in keys_active}
    assert ("op-1", "PLAN", "plan_prose") not in keys_set


def test_clear_keeps_seq_counter():
    ch = NarrativeChannel(capacity=10)
    ch.start_frame(op_id="op-x", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    ch.clear()
    nxt = ch.start_frame(op_id="op-y", phase="PLAN", kind=NarrativeKind.PLAN_PROSE)
    assert nxt.ref == "n-2"


# ===========================================================================
# Snapshot
# ===========================================================================


def test_snapshot_counts_states():
    ch = NarrativeChannel(capacity=10)
    ch.emit_complete(
        op_id="o-1", phase="P", kind=NarrativeKind.PLAN_PROSE, prose="committed",
    )
    ch.start_frame(op_id="o-2", phase="P", kind=NarrativeKind.PLAN_PROSE)
    ch.start_frame(op_id="o-3", phase="P", kind=NarrativeKind.PLAN_PROSE)
    ch.discard(op_id="o-3", phase="P", kind=NarrativeKind.PLAN_PROSE)
    snap = ch.snapshot()
    assert snap.committed_count == 1
    assert snap.buffering_count == 1
    assert snap.discarded_count == 1


def test_snapshot_utilization():
    ch = NarrativeChannel(capacity=4)
    ch.start_frame(op_id="o-1", phase="P", kind=NarrativeKind.PLAN_PROSE)
    ch.start_frame(op_id="o-2", phase="P", kind=NarrativeKind.PLAN_PROSE)
    assert ch.snapshot().utilization == 0.5


# ===========================================================================
# Query API
# ===========================================================================


def test_find_by_kind():
    ch = NarrativeChannel(capacity=10)
    ch.emit_complete(
        op_id="o-1", phase="P", kind=NarrativeKind.INTENT, prose="i1",
    )
    ch.emit_complete(
        op_id="o-2", phase="P", kind=NarrativeKind.PLAN_PROSE, prose="p1",
    )
    ch.emit_complete(
        op_id="o-3", phase="P", kind=NarrativeKind.INTENT, prose="i2",
    )
    intents = ch.find_by_kind(NarrativeKind.INTENT)
    assert len(intents) == 2
    plans = ch.find_by_kind(NarrativeKind.PLAN_PROSE)
    assert len(plans) == 1


def test_list_recent_newest_first():
    ch = NarrativeChannel(capacity=10)
    for i in range(3):
        ch.emit_complete(
            op_id=f"o-{i}", phase="P",
            kind=NarrativeKind.PLAN_PROSE, prose=f"#{i}",
        )
    recent = ch.list_recent(limit=2)
    assert tuple(f.prose for f in recent) == ("#2", "#1")


# ===========================================================================
# Frozen + projection
# ===========================================================================


def test_frame_is_frozen():
    ch = NarrativeChannel(capacity=10)
    f = ch.start_frame(op_id="x", phase="P", kind=NarrativeKind.PLAN_PROSE)
    with pytest.raises(Exception):
        f.prose = "tampered"  # type: ignore[misc]


def test_to_dict_omits_prose_by_default():
    ch = NarrativeChannel(capacity=10)
    f = ch.emit_complete(
        op_id="x", phase="P", kind=NarrativeKind.PLAN_PROSE,
        prose="huge content " * 100,
    )
    d = f.to_dict()
    assert "prose" not in d
    assert d["char_count"] > 0


def test_to_dict_includes_prose_when_requested():
    ch = NarrativeChannel(capacity=10)
    f = ch.emit_complete(
        op_id="x", phase="P", kind=NarrativeKind.PLAN_PROSE, prose="hello",
    )
    d = f.to_dict(include_prose=True)
    assert d["prose"] == "hello"


# ===========================================================================
# Thread safety
# ===========================================================================


def test_concurrent_frames_unique_refs():
    ch = NarrativeChannel(capacity=10_000)
    n = 8
    per = 25
    refs: list = []
    refs_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def _worker(tid: int):
        barrier.wait()
        local = []
        for i in range(per):
            f = ch.emit_complete(
                op_id=f"op-{tid}-{i}", phase="P",
                kind=NarrativeKind.PLAN_PROSE, prose=f"#{i}",
            )
            local.append(f.ref)
        with refs_lock:
            refs.extend(local)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(refs) == n * per
    assert len(set(refs)) == len(refs)


# ===========================================================================
# Singleton
# ===========================================================================


def test_singleton_stable():
    a = get_default_channel()
    b = get_default_channel()
    assert a is b


def test_reset_drops_singleton():
    a = get_default_channel()
    reset_default_channel_for_tests()
    b = get_default_channel()
    assert a is not b

"""Tests for op_block_buffer (Gap #3 Slice 2)."""
from __future__ import annotations

import threading

import pytest

from backend.core.ouroboros.battle_test.op_block_buffer import (
    BUFFER_SIZE_ENV_VAR,
    OP_BLOCK_BUFFER_SCHEMA_VERSION,
    OpBlock,
    OpBlockBuffer,
    OpBlockState,
    REF_PREFIX,
    get_default_buffer,
    reset_default_buffer_for_tests,
)


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(BUFFER_SIZE_ENV_VAR, raising=False)
    reset_default_buffer_for_tests()
    yield
    reset_default_buffer_for_tests()


# ===========================================================================
# Schema + constants
# ===========================================================================


def test_schema_version_pinned():
    assert OP_BLOCK_BUFFER_SCHEMA_VERSION == "op_block_buffer.v1"


def test_ref_prefix_is_o_dash():
    assert REF_PREFIX == "o-"


def test_op_block_state_closed_taxonomy():
    assert {m.value for m in OpBlockState} == {
        "buffering", "committed", "expanded",
    }


@pytest.mark.parametrize("state,terminal", [
    (OpBlockState.BUFFERING, False),
    (OpBlockState.COMMITTED, True),
    (OpBlockState.EXPANDED, True),
])
def test_state_is_terminal(state, terminal):
    assert state.is_terminal is terminal


# ===========================================================================
# Capacity construction
# ===========================================================================


def test_default_capacity_is_50():
    assert OpBlockBuffer().capacity == 50


def test_explicit_capacity_used():
    assert OpBlockBuffer(capacity=7).capacity == 7


def test_env_capacity(monkeypatch):
    monkeypatch.setenv(BUFFER_SIZE_ENV_VAR, "12")
    assert OpBlockBuffer().capacity == 12


def test_capacity_clamped(monkeypatch):
    monkeypatch.setenv(BUFFER_SIZE_ENV_VAR, "0")
    assert OpBlockBuffer().capacity == 1
    monkeypatch.setenv(BUFFER_SIZE_ENV_VAR, "999999")
    assert OpBlockBuffer().capacity == 5_000


# ===========================================================================
# start_op + ref allocation
# ===========================================================================


def test_start_op_allocates_o_dash_ref():
    buf = OpBlockBuffer(capacity=10)
    block = buf.start_op("op-x")
    assert isinstance(block, OpBlock)
    assert block.ref == "o-1"
    assert block.state is OpBlockState.BUFFERING
    assert block.lines == ()


def test_start_op_idempotent_for_active_op():
    buf = OpBlockBuffer(capacity=10)
    a = buf.start_op("op-x")
    b = buf.start_op("op-x")
    assert a is not None and b is not None
    assert a.ref == b.ref


def test_start_op_after_commit_issues_fresh_ref():
    buf = OpBlockBuffer(capacity=10)
    a = buf.start_op("op-x")
    buf.commit("op-x", "summary")
    b = buf.start_op("op-x")
    assert a.ref == "o-1"
    assert b.ref == "o-2"


def test_start_op_empty_id_returns_none():
    buf = OpBlockBuffer(capacity=10)
    assert buf.start_op("") is None
    assert buf.start_op(None) is None


def test_refs_monotonic():
    buf = OpBlockBuffer(capacity=10)
    refs = [buf.start_op(f"op-{i}").ref for i in range(5)]
    assert refs == ["o-1", "o-2", "o-3", "o-4", "o-5"]


# ===========================================================================
# append
# ===========================================================================


def test_append_to_active_block():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    assert buf.append("op-x", "line 1") is True
    assert buf.append("op-x", "line 2") is True
    block = buf.find_by_op_id("op-x")[0]
    assert block.lines == ("line 1", "line 2")


def test_append_to_unknown_op_returns_false():
    buf = OpBlockBuffer(capacity=10)
    assert buf.append("never-started", "x") is False


def test_append_after_commit_returns_false():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.commit("op-x", "summary")
    assert buf.append("op-x", "should-be-rejected") is False


def test_append_coerces_non_string_inputs():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    assert buf.append("op-x", None) is True  # None coerces to ""
    block = buf.find_by_op_id("op-x")[0]
    assert block.lines == ("",)


# ===========================================================================
# commit
# ===========================================================================


def test_commit_transitions_to_committed():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.append("op-x", "a")
    out = buf.commit("op-x", "summary line")
    assert out is not None
    assert out.state is OpBlockState.COMMITTED
    assert out.summary_line == "summary line"
    assert out.committed_at > 0.0
    assert out.duration_s >= 0.0


def test_commit_unknown_op_returns_none():
    buf = OpBlockBuffer(capacity=10)
    assert buf.commit("never", "x") is None


def test_commit_already_committed_idempotent():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.commit("op-x", "first")
    second = buf.commit("op-x", "second")
    assert second is None  # active-index already cleaned up


def test_commit_releases_active_index():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.commit("op-x", "summary")
    assert "op-x" not in buf.active_op_ids()


# ===========================================================================
# mark_expanded
# ===========================================================================


def test_mark_expanded_after_commit():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    block = buf.commit("op-x", "summary")
    out = buf.mark_expanded(block.ref)
    assert out is not None
    assert out.state is OpBlockState.EXPANDED


def test_mark_expanded_on_buffering_no_op():
    """Don't downgrade BUFFERING → EXPANDED (would lose state)."""
    buf = OpBlockBuffer(capacity=10)
    block = buf.start_op("op-x")
    out = buf.mark_expanded(block.ref)
    assert out is not None
    assert out.state is OpBlockState.BUFFERING  # unchanged


def test_mark_expanded_unknown_ref_none():
    buf = OpBlockBuffer(capacity=10)
    assert buf.mark_expanded("o-999") is None
    assert buf.mark_expanded(None) is None


# ===========================================================================
# discard_active
# ===========================================================================


def test_discard_active_removes_buffering_block():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.append("op-x", "line")
    out = buf.discard_active("op-x")
    assert out is not None
    assert "op-x" not in buf.active_op_ids()
    assert buf.find_by_op_id("op-x") == ()


def test_discard_active_unknown_returns_none():
    buf = OpBlockBuffer(capacity=10)
    assert buf.discard_active("never") is None


# ===========================================================================
# Eviction — drop-oldest
# ===========================================================================


def test_eviction_drops_oldest():
    buf = OpBlockBuffer(capacity=3)
    refs = []
    for i in range(5):
        b = buf.start_op(f"op-{i}")
        buf.commit(f"op-{i}", f"summary-{i}")
        refs.append(b.ref)
    # o-1, o-2 evicted; o-3, o-4, o-5 retained.
    assert buf.lookup("o-1") is None
    assert buf.lookup("o-2") is None
    assert buf.lookup("o-3") is not None
    assert buf.lookup("o-4") is not None
    assert buf.lookup("o-5") is not None


def test_eviction_prunes_active_index_for_buffering_blocks():
    """If an evicted block was still BUFFERING, the active-index
    must be cleaned up too — otherwise append() would silently
    misroute to the evicted slot."""
    buf = OpBlockBuffer(capacity=2)
    buf.start_op("op-1")  # ref o-1, BUFFERING (never committed)
    buf.start_op("op-2")  # ref o-2, BUFFERING
    buf.start_op("op-3")  # ref o-3 — pushes o-1 out
    # op-1 should NOT be in active_op_ids
    assert "op-1" not in buf.active_op_ids()
    # And append to op-1 should fail.
    assert buf.append("op-1", "x") is False


def test_clear_drops_state_but_not_seq():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.clear()
    assert len(buf) == 0
    next_block = buf.start_op("op-y")
    assert next_block.ref == "o-2"  # counter not reset


# ===========================================================================
# Snapshot
# ===========================================================================


def test_snapshot_counts_states():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-1")  # BUFFERING
    buf.start_op("op-2")
    buf.commit("op-2", "x")  # COMMITTED
    block3 = buf.start_op("op-3")
    buf.commit("op-3", "y")
    buf.mark_expanded(block3.ref)  # EXPANDED
    snap = buf.snapshot()
    assert snap.buffering_count == 1
    assert snap.committed_count == 1
    assert snap.expanded_count == 1
    assert snap.size == 3


def test_snapshot_utilization():
    buf = OpBlockBuffer(capacity=4)
    buf.start_op("op-1")
    buf.start_op("op-2")
    assert buf.snapshot().utilization == 0.5


# ===========================================================================
# Query API
# ===========================================================================


def test_find_by_op_id_returns_all_matching():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.commit("op-x", "first")
    buf.start_op("op-x")  # second life of op-x
    matches = buf.find_by_op_id("op-x")
    assert len(matches) == 2


def test_find_by_op_id_unknown_empty():
    buf = OpBlockBuffer(capacity=10)
    assert buf.find_by_op_id("never") == ()
    assert buf.find_by_op_id("") == ()


def test_list_recent_newest_first():
    buf = OpBlockBuffer(capacity=10)
    for i in range(3):
        buf.start_op(f"op-{i}")
    recent = buf.list_recent(limit=2)
    assert tuple(b.op_id for b in recent) == ("op-2", "op-1")


# ===========================================================================
# Frozen + projection
# ===========================================================================


def test_op_block_frozen():
    buf = OpBlockBuffer(capacity=10)
    block = buf.start_op("op-x")
    with pytest.raises(Exception):
        block.lines = ("tampered",)  # type: ignore[misc]


def test_to_dict_omits_lines_by_default():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.append("op-x", "huge line " * 100)
    block = buf.find_by_op_id("op-x")[0]
    d = block.to_dict()
    assert "lines" not in d
    assert d["line_count"] == 1


def test_to_dict_includes_lines_when_requested():
    buf = OpBlockBuffer(capacity=10)
    buf.start_op("op-x")
    buf.append("op-x", "L1")
    buf.append("op-x", "L2")
    block = buf.find_by_op_id("op-x")[0]
    d = block.to_dict(include_lines=True)
    assert d["lines"] == ["L1", "L2"]


# ===========================================================================
# Thread safety
# ===========================================================================


def test_concurrent_starts_produce_unique_refs():
    buf = OpBlockBuffer(capacity=10_000)
    n_threads = 8
    per = 25
    refs: list = []
    refs_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def _worker(tid: int):
        barrier.wait()
        local = []
        for i in range(per):
            b = buf.start_op(f"op-{tid}-{i}")
            local.append(b.ref)
            buf.append(f"op-{tid}-{i}", f"line {i}")
            buf.commit(f"op-{tid}-{i}", "summary")
        with refs_lock:
            refs.extend(local)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(refs) == n_threads * per
    assert len(set(refs)) == len(refs)


# ===========================================================================
# Singleton
# ===========================================================================


def test_singleton_stable():
    a = get_default_buffer()
    b = get_default_buffer()
    assert a is b


def test_reset_drops_singleton():
    a = get_default_buffer()
    reset_default_buffer_for_tests()
    b = get_default_buffer()
    assert a is not b

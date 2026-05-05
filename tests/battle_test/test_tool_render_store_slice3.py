"""Tests for tool_render_store (Gap #2 Slice 3).

Validates the bounded body store:
  • Schema-versioned + frozen records
  • Drop-oldest eviction at capacity
  • Monotonic ref allocation (no reuse, no reset)
  • Thread-safe (concurrent stores produce distinct, valid refs)
  • Defensive coercion on every input
  • Singleton + reset hook for test isolation
  • Env-driven capacity with bounds clamping
"""
from __future__ import annotations

import threading

import pytest

from backend.core.ouroboros.battle_test.tool_render_store import (
    BoundedBodyStore,
    REF_PREFIX,
    STORE_SIZE_ENV_VAR,
    StoreSnapshot,
    StoredBody,
    TOOL_RENDER_STORE_SCHEMA_VERSION,
    get_default_store,
    reset_default_store_for_tests,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def clean_env_and_singleton(monkeypatch: pytest.MonkeyPatch):
    """Strip the size env var and reset the singleton for every test."""
    monkeypatch.delenv(STORE_SIZE_ENV_VAR, raising=False)
    reset_default_store_for_tests()
    yield
    reset_default_store_for_tests()


# ===========================================================================
# Schema + constants
# ===========================================================================


def test_schema_version_pinned():
    assert TOOL_RENDER_STORE_SCHEMA_VERSION == "tool_render_store.v1"


def test_ref_prefix_is_t_dash():
    # Stable across slices — ``/expand`` parser depends on it.
    assert REF_PREFIX == "t-"


# ===========================================================================
# StoredBody — frozen + projection
# ===========================================================================


def test_stored_body_to_dict_omits_full_body():
    """``to_dict`` should expose ``body_chars`` (count) but NOT the
    raw body — keeps SSE / observability payloads bounded."""
    s = StoredBody(
        ref="t-1", op_id="op-x", round_index=0, tool_name="bash",
        body="huge body content " * 100, summary="3 lines of output",
        lexer="bash", inserted_at=1.0,
    )
    d = s.to_dict()
    assert d["ref"] == "t-1"
    assert d["body_chars"] == len("huge body content " * 100)
    assert "body" not in d
    assert d["schema_version"] == TOOL_RENDER_STORE_SCHEMA_VERSION


def test_stored_body_is_frozen():
    s = StoredBody(
        ref="t-1", op_id="x", round_index=0, tool_name="bash",
        body="b", summary="s", lexer=None, inserted_at=0.0,
    )
    with pytest.raises(Exception):
        s.body = "tampered"  # type: ignore[misc]


# ===========================================================================
# Capacity construction
# ===========================================================================


def test_explicit_capacity_used():
    store = BoundedBodyStore(capacity=7)
    assert store.capacity == 7


def test_env_capacity_used_when_unset(monkeypatch):
    monkeypatch.setenv(STORE_SIZE_ENV_VAR, "12")
    store = BoundedBodyStore()
    assert store.capacity == 12


def test_env_capacity_default_when_blank():
    store = BoundedBodyStore()
    assert store.capacity == 50


def test_env_capacity_clamped_to_min(monkeypatch):
    monkeypatch.setenv(STORE_SIZE_ENV_VAR, "0")
    store = BoundedBodyStore()
    assert store.capacity == 1  # min clamp


def test_env_capacity_clamped_to_max(monkeypatch):
    monkeypatch.setenv(STORE_SIZE_ENV_VAR, "999999")
    store = BoundedBodyStore()
    assert store.capacity == 10_000  # max clamp


def test_env_capacity_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv(STORE_SIZE_ENV_VAR, "not-an-int")
    store = BoundedBodyStore()
    assert store.capacity == 50


def test_capacity_kwarg_clamps_too():
    s1 = BoundedBodyStore(capacity=0)
    assert s1.capacity == 1
    s2 = BoundedBodyStore(capacity=99_999)
    assert s2.capacity == 10_000


# ===========================================================================
# Store → ref allocation + retrieval
# ===========================================================================


def test_store_returns_stored_body_with_ref():
    store = BoundedBodyStore(capacity=10)
    out = store.store(
        op_id="op-a", round_index=0, tool_name="bash",
        body="line1\nline2", summary="2 lines of output", lexer="bash",
    )
    assert isinstance(out, StoredBody)
    assert out.ref == "t-1"
    assert out.body == "line1\nline2"
    assert out.tool_name == "bash"
    assert out.lexer == "bash"


def test_lookup_resolves_ref():
    store = BoundedBodyStore(capacity=10)
    s = store.store(
        op_id="op", round_index=0, tool_name="read_file",
        body="content", lexer="python",
    )
    assert store.lookup(s.ref) is s


def test_lookup_unknown_ref_returns_none():
    store = BoundedBodyStore(capacity=10)
    assert store.lookup("t-999") is None
    assert store.lookup("garbage") is None
    assert store.lookup(None) is None
    assert store.lookup(42) is None


def test_refs_monotonic():
    store = BoundedBodyStore(capacity=10)
    refs = [
        store.store(op_id="x", round_index=i, tool_name="bash", body=str(i)).ref
        for i in range(5)
    ]
    assert refs == ["t-1", "t-2", "t-3", "t-4", "t-5"]


# ===========================================================================
# Eviction — drop-oldest at capacity
# ===========================================================================


def test_eviction_drops_oldest_first():
    store = BoundedBodyStore(capacity=3)
    refs = [
        store.store(
            op_id="o", round_index=i, tool_name="bash", body=f"#{i}",
        ).ref
        for i in range(5)
    ]
    # Capacity 3, inserted 5 — refs t-1 and t-2 should be evicted.
    assert store.lookup(refs[0]) is None  # t-1 evicted
    assert store.lookup(refs[1]) is None  # t-2 evicted
    assert store.lookup(refs[2]) is not None  # t-3 retained
    assert store.lookup(refs[3]) is not None  # t-4 retained
    assert store.lookup(refs[4]) is not None  # t-5 retained
    assert len(store) == 3


def test_eviction_preserves_monotonic_seq_after_overflow():
    """Ref counter never resets — even after eviction. This is the
    safety contract: a ref printed in the operator's terminal will
    NEVER point to a different body than the one originally
    referenced; it can only be evicted (lookup → None)."""
    store = BoundedBodyStore(capacity=2)
    refs = []
    for i in range(6):
        refs.append(store.store(
            op_id="o", round_index=i, tool_name="bash", body=f"#{i}",
        ).ref)
    assert refs == ["t-1", "t-2", "t-3", "t-4", "t-5", "t-6"]
    snap = store.snapshot()
    assert snap.next_seq == 7  # counter advanced past all inserts
    assert snap.size == 2
    assert snap.capacity == 2


def test_clear_drops_bodies_but_not_seq():
    store = BoundedBodyStore(capacity=10)
    r1 = store.store(op_id="o", round_index=0, tool_name="bash", body="x").ref
    store.clear()
    assert store.lookup(r1) is None
    assert len(store) == 0
    # Counter MUST NOT reset — the next ref is t-2, never reusing t-1.
    r2 = store.store(op_id="o", round_index=1, tool_name="bash", body="y").ref
    assert r2 == "t-2"


# ===========================================================================
# Snapshot — utilization math
# ===========================================================================


def test_snapshot_utilization_zero_when_empty():
    store = BoundedBodyStore(capacity=10)
    assert store.snapshot().utilization == 0.0


def test_snapshot_utilization_at_capacity():
    store = BoundedBodyStore(capacity=4)
    for i in range(4):
        store.store(op_id="o", round_index=i, tool_name="bash", body="x")
    assert store.snapshot().utilization == 1.0


def test_snapshot_utilization_partial():
    store = BoundedBodyStore(capacity=4)
    for i in range(2):
        store.store(op_id="o", round_index=i, tool_name="bash", body="x")
    assert store.snapshot().utilization == 0.5


def test_snapshot_caps_utilization_at_one():
    """Defensive — should never report > 1.0 even under
    pathological state."""
    snap = StoreSnapshot(capacity=2, size=999, next_seq=1000)
    assert snap.utilization == 1.0


# ===========================================================================
# Defensive coercion — non-string / non-int inputs
# ===========================================================================


def test_store_coerces_non_string_inputs():
    store = BoundedBodyStore(capacity=10)
    # Pass garbage in every slot — must NOT raise.
    s = store.store(
        op_id=None, round_index=None, tool_name=42,
        body=object(), summary=None, lexer=None,
    )
    assert isinstance(s, StoredBody)
    assert s.op_id == ""
    assert s.round_index == 0
    # tool_name coerced from int → str
    assert s.tool_name == "42"


def test_store_lexer_empty_string_normalizes_to_none():
    store = BoundedBodyStore(capacity=10)
    s = store.store(
        op_id="o", round_index=0, tool_name="bash", body="x", lexer="",
    )
    assert s.lexer is None


def test_store_lexer_passes_through_when_set():
    store = BoundedBodyStore(capacity=10)
    s = store.store(
        op_id="o", round_index=0, tool_name="bash", body="x", lexer="bash",
    )
    assert s.lexer == "bash"


def test_store_bool_round_index_coerced_to_zero():
    """bool is a subclass of int — defensive helper rejects it so a
    stray True/False kwarg doesn't poison the index field."""
    store = BoundedBodyStore(capacity=10)
    s = store.store(
        op_id="o", round_index=True, tool_name="bash", body="x",
    )
    assert s.round_index == 0


# ===========================================================================
# Thread safety — concurrent stores produce unique, valid refs
# ===========================================================================


def test_concurrent_stores_produce_unique_refs():
    store = BoundedBodyStore(capacity=10_000)
    n_threads = 8
    inserts_per_thread = 50
    barrier = threading.Barrier(n_threads)
    refs: list = []
    refs_lock = threading.Lock()

    def _worker(tid: int):
        barrier.wait()
        local: list = []
        for i in range(inserts_per_thread):
            s = store.store(
                op_id=f"op-{tid}", round_index=i, tool_name="bash",
                body=f"thread {tid} insert {i}",
            )
            local.append(s.ref)
        with refs_lock:
            refs.extend(local)

    threads = [
        threading.Thread(target=_worker, args=(t,)) for t in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All refs unique
    assert len(refs) == n_threads * inserts_per_thread
    assert len(set(refs)) == len(refs)
    # All refs resolvable
    for ref in refs:
        assert store.lookup(ref) is not None


# ===========================================================================
# Singleton + reset
# ===========================================================================


def test_singleton_is_stable():
    a = get_default_store()
    b = get_default_store()
    assert a is b


def test_reset_drops_singleton():
    a = get_default_store()
    reset_default_store_for_tests()
    b = get_default_store()
    assert a is not b


def test_singleton_reads_env_at_first_construction(monkeypatch):
    monkeypatch.setenv(STORE_SIZE_ENV_VAR, "7")
    reset_default_store_for_tests()
    s = get_default_store()
    assert s.capacity == 7


# ===========================================================================
# all_refs ordering
# ===========================================================================


def test_all_refs_returns_oldest_to_newest():
    store = BoundedBodyStore(capacity=10)
    for i in range(3):
        store.store(op_id="o", round_index=i, tool_name="bash", body=str(i))
    assert store.all_refs() == ("t-1", "t-2", "t-3")


def test_all_refs_after_eviction():
    store = BoundedBodyStore(capacity=2)
    for i in range(4):
        store.store(op_id="o", round_index=i, tool_name="bash", body=str(i))
    # t-1, t-2 evicted; t-3, t-4 retained.
    assert store.all_refs() == ("t-3", "t-4")

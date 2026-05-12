"""Regression spine for Treefinement Phase 4 Slice 4a+4b+4c —
TreeArchive ring + §33.4 JSONL persistence + SSE producer-bridge.

Pins the canonical ring shape (mirrors permission_decision_archive
v2.89), drop-oldest eviction, monotonic b-N refs, dual master-flag
gating (ring + persistence independent), composition of the
canonical flock primitive, and best-effort SSE publication.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.repair_tree import (
    BranchOutcome,
    LayerVerdict,
    PruningReason,
    RepairBranch,
    RepairTreeLayer,
    RepairTreeResult,
)
from backend.core.ouroboros.governance.repair_tree_archive import (
    ARCHIVE_MASTER_FLAG_ENV_VAR,
    ARCHIVE_SIZE_ENV_VAR,
    PERSISTENCE_MASTER_FLAG_ENV_VAR,
    PERSISTENCE_PATH_ENV_VAR,
    REPAIR_TREE_ARCHIVE_SCHEMA_VERSION,
    ArchivedBranch,
    ArchiveSnapshot,
    TreeArchive,
    archive_enabled,
    get_default_archive,
    maybe_archive_tree_result,
    persist_tree_result,
    persistence_enabled,
    register_flags,
    reset_default_archive_for_tests,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagType,
)


def _make_branch(
    *, bid: str, score: float = 0.5,
    outcome: BranchOutcome = BranchOutcome.PROMOTED,
    prune_reason: PruningReason = None,  # type: ignore[assignment]
    layer_index: int = 0,
    hypothesis: str = "rename",
) -> RepairBranch:
    return RepairBranch(
        branch_id=bid,
        parent_branch_id=None,
        layer_index=layer_index,
        failure_class="test",
        fix_hypothesis=hypothesis,
        diff="--- a\n+++ b\n",
        validator_score=score,
        outcome=outcome,
        prune_reason=prune_reason,
        worktree_id="unit-x",
        cost_usd=0.001,
        validation_runs_consumed=1,
    )


def _make_result(
    *, op_id: str = "op-1",
    branches_per_layer: int = 2,
    layer_count: int = 1,
    bid_prefix: str = "b",
    score: float = 0.5,
    outcome: BranchOutcome = BranchOutcome.PROMOTED,
) -> RepairTreeResult:
    layers: List[RepairTreeLayer] = []
    for li in range(layer_count):
        branches = tuple(
            _make_branch(
                bid=f"{bid_prefix}-{li}-{i}",
                score=score,
                outcome=outcome,
                layer_index=li,
            )
            for i in range(branches_per_layer)
        )
        layers.append(RepairTreeLayer(
            layer_index=li,
            branches=branches,
            verdict=LayerVerdict.EXPANDED,
            wall_ms=10.0,
            parallel_units_actual=branches_per_layer,
        ))
    return RepairTreeResult(
        root_op_id=op_id,
        layers=tuple(layers),
        winning_branch_path=(),
        final_status=None,
    )


@pytest.fixture(autouse=True)
def _isolate_archive(monkeypatch):
    """Reset singleton + master flags between tests."""
    monkeypatch.setenv(ARCHIVE_MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.delenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(PERSISTENCE_PATH_ENV_VAR, raising=False)
    reset_default_archive_for_tests()
    yield
    reset_default_archive_for_tests()


# ===========================================================================
# Master flag accessors
# ===========================================================================


def test_archive_enabled_default_false(monkeypatch):
    monkeypatch.delenv(ARCHIVE_MASTER_FLAG_ENV_VAR, raising=False)
    assert archive_enabled() is False, (
        "Archive master flag MUST default FALSE per §33.1 — "
        "graduation contract"
    )


def test_persistence_enabled_default_false(monkeypatch):
    monkeypatch.delenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, raising=False)
    assert persistence_enabled() is False


def test_archive_and_persistence_are_independent(monkeypatch):
    """Operator may want one without the other."""
    monkeypatch.setenv(ARCHIVE_MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.delenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, raising=False)
    assert archive_enabled() is True
    assert persistence_enabled() is False


# ===========================================================================
# Ring semantics — mirrors permission_decision_archive
# ===========================================================================


def test_archive_records_branches_with_monotonic_b_refs():
    a = TreeArchive(capacity=10)
    result = _make_result(branches_per_layer=3)
    archived = a.record_result(result)
    assert len(archived) == 3
    # b-N refs are monotonic from 1
    assert [e.ref for e in archived] == ["b-1", "b-2", "b-3"]


def test_archive_drop_oldest_eviction():
    a = TreeArchive(capacity=2)
    a.record_result(_make_result(op_id="op-A", branches_per_layer=2))
    # b-1, b-2 occupied
    assert len(a) == 2
    a.record_result(_make_result(op_id="op-B", branches_per_layer=2))
    # b-1, b-2 evicted; b-3, b-4 occupy
    assert len(a) == 2
    assert a.get_by_ref("b-1") is None  # evicted
    assert a.get_by_ref("b-2") is None  # evicted
    assert a.get_by_ref("b-3") is not None
    assert a.get_by_ref("b-4") is not None


def test_archive_b_counter_never_rewinds_after_eviction():
    a = TreeArchive(capacity=1)
    a.record_result(_make_result(op_id="op-A", branches_per_layer=1))
    a.record_result(_make_result(op_id="op-B", branches_per_layer=1))
    a.record_result(_make_result(op_id="op-C", branches_per_layer=1))
    snap = a.snapshot()
    # 3 branches archived → next_seq = 4 (b-1..b-3 issued)
    assert snap.next_seq == 4
    assert snap.size == 1


def test_archive_master_off_returns_no_op():
    """When master flag is FALSE, record_result is a no-op."""
    a = TreeArchive(capacity=10)
    import os
    old = os.environ.pop(ARCHIVE_MASTER_FLAG_ENV_VAR, None)
    try:
        archived = a.record_result(_make_result())
        assert archived == ()
        assert len(a) == 0
    finally:
        if old is not None:
            os.environ[ARCHIVE_MASTER_FLAG_ENV_VAR] = old


def test_archive_lookup_by_ref():
    a = TreeArchive(capacity=10)
    a.record_result(_make_result(branches_per_layer=2))
    e = a.get_by_ref("b-1")
    assert e is not None
    assert isinstance(e, ArchivedBranch)
    assert e.ref == "b-1"


def test_archive_lookup_by_ref_unknown_returns_none():
    a = TreeArchive(capacity=10)
    a.record_result(_make_result(branches_per_layer=1))
    assert a.get_by_ref("b-999") is None
    assert a.get_by_ref("not-a-ref") is None
    assert a.get_by_ref("") is None


def test_archive_lookup_by_branch_id():
    a = TreeArchive(capacity=10)
    a.record_result(_make_result(branches_per_layer=1, bid_prefix="X"))
    e = a.get_by_branch_id("X-0-0")
    assert e is not None
    assert e.branch.branch_id == "X-0-0"


def test_archive_by_op_returns_all_branches():
    a = TreeArchive(capacity=10)
    a.record_result(_make_result(op_id="op-A", branches_per_layer=2))
    a.record_result(_make_result(op_id="op-B", branches_per_layer=3))
    branches_a = a.by_op("op-A")
    branches_b = a.by_op("op-B")
    assert len(branches_a) == 2
    assert len(branches_b) == 3


def test_archive_by_op_unknown_returns_empty():
    a = TreeArchive(capacity=10)
    assert a.by_op("unknown-op") == ()


def test_archive_recent_returns_newest_first():
    a = TreeArchive(capacity=10)
    a.record_result(_make_result(op_id="op-A", branches_per_layer=1, bid_prefix="A"))
    a.record_result(_make_result(op_id="op-B", branches_per_layer=1, bid_prefix="B"))
    a.record_result(_make_result(op_id="op-C", branches_per_layer=1, bid_prefix="C"))
    recent = a.recent(limit=10)
    # Newest first: C, B, A
    assert [e.op_id for e in recent] == ["op-C", "op-B", "op-A"]


def test_archive_snapshot_projection():
    a = TreeArchive(capacity=5)
    a.record_result(_make_result(branches_per_layer=2))
    snap = a.snapshot()
    assert isinstance(snap, ArchiveSnapshot)
    assert snap.capacity == 5
    assert snap.size == 2
    assert snap.next_seq == 3
    payload = snap.to_dict()
    assert payload["utilization"] == 2 / 5


def test_archived_branch_to_dict_round_trip():
    a = TreeArchive(capacity=10)
    archived = a.record_result(_make_result(branches_per_layer=1))
    e = archived[0]
    payload = e.to_dict()
    assert payload["schema_version"] == REPAIR_TREE_ARCHIVE_SCHEMA_VERSION
    assert payload["ref"] == "b-1"
    assert "branch" in payload
    assert "archived_at_unix" in payload


def test_archive_capacity_clamped(monkeypatch):
    monkeypatch.setenv(ARCHIVE_SIZE_ENV_VAR, "999999")
    a = TreeArchive()
    assert a.capacity == 10_000  # ceiling
    monkeypatch.setenv(ARCHIVE_SIZE_ENV_VAR, "0")
    a2 = TreeArchive()
    assert a2.capacity == 1  # floor


def test_archive_capacity_garbage_falls_back(monkeypatch):
    monkeypatch.setenv(ARCHIVE_SIZE_ENV_VAR, "elephant")
    a = TreeArchive()
    assert a.capacity == 30  # default


# ===========================================================================
# Eviction maintains secondary indices
# ===========================================================================


def test_eviction_cleans_op_index():
    a = TreeArchive(capacity=2)
    a.record_result(_make_result(op_id="op-A", branches_per_layer=2))
    # op-A has b-1, b-2
    assert len(a.by_op("op-A")) == 2
    a.record_result(_make_result(op_id="op-B", branches_per_layer=2))
    # b-1, b-2 evicted
    assert a.by_op("op-A") == ()
    assert len(a.by_op("op-B")) == 2


def test_eviction_cleans_branch_id_index():
    a = TreeArchive(capacity=1)
    a.record_result(_make_result(bid_prefix="A", branches_per_layer=1))
    assert a.get_by_branch_id("A-0-0") is not None
    a.record_result(_make_result(bid_prefix="B", branches_per_layer=1))
    # A-0-0 evicted
    assert a.get_by_branch_id("A-0-0") is None
    assert a.get_by_branch_id("B-0-0") is not None


# ===========================================================================
# Default singleton
# ===========================================================================


def test_get_default_archive_returns_same_instance():
    a1 = get_default_archive()
    a2 = get_default_archive()
    assert a1 is a2


def test_reset_default_archive_returns_new_instance():
    a1 = get_default_archive()
    reset_default_archive_for_tests()
    a2 = get_default_archive()
    assert a1 is not a2


# ===========================================================================
# Thread safety
# ===========================================================================


def test_archive_thread_safe_under_concurrent_records():
    a = TreeArchive(capacity=1000)
    errors: List[Exception] = []

    def _worker(i: int):
        try:
            a.record_result(_make_result(
                op_id=f"op-{i}", branches_per_layer=3,
                bid_prefix=f"T{i}",
            ))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(i,))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(a) == 60  # 20 workers × 3 branches each


# ===========================================================================
# §33.4 JSONL persistence — composes flock_append_line
# ===========================================================================


def test_persist_master_off_returns_false(monkeypatch, tmp_path):
    monkeypatch.delenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, raising=False)
    result = _make_result()
    assert persist_tree_result(
        result, path=tmp_path / "out.jsonl",
    ) is False


def test_persist_writes_one_line_per_branch(monkeypatch, tmp_path):
    monkeypatch.setenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, "true")
    target = tmp_path / "trees.jsonl"
    result = _make_result(branches_per_layer=3, layer_count=2)
    ok = persist_tree_result(result, path=target)
    assert ok is True
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 6  # 3 branches × 2 layers
    # Each line is a valid JSON record
    for line in lines:
        parsed = json.loads(line)
        assert parsed["schema_version"] == REPAIR_TREE_ARCHIVE_SCHEMA_VERSION
        assert "branch" in parsed
        assert "op_id" in parsed
        assert "archived_at_unix" in parsed


def test_persist_creates_parent_directory(monkeypatch, tmp_path):
    monkeypatch.setenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, "true")
    target = tmp_path / "deep" / "nested" / "trees.jsonl"
    result = _make_result(branches_per_layer=1)
    ok = persist_tree_result(result, path=target)
    assert ok is True
    assert target.exists()


def test_persist_appends_across_calls(monkeypatch, tmp_path):
    monkeypatch.setenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, "true")
    target = tmp_path / "trees.jsonl"
    persist_tree_result(_make_result(op_id="op-A", branches_per_layer=1), path=target)
    persist_tree_result(_make_result(op_id="op-B", branches_per_layer=1), path=target)
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    op_ids = [json.loads(line)["op_id"] for line in lines]
    assert op_ids == ["op-A", "op-B"]


def test_persist_path_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom-path.jsonl"
    monkeypatch.setenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(PERSISTENCE_PATH_ENV_VAR, str(custom))
    result = _make_result(branches_per_layer=1)
    ok = persist_tree_result(result)  # no explicit path
    assert ok is True
    assert custom.exists()


def test_persist_never_raises_on_garbage_path(monkeypatch):
    monkeypatch.setenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(PERSISTENCE_PATH_ENV_VAR, "/proc/cant-write-here.jsonl")
    result = _make_result()
    # MUST NOT raise
    ok = persist_tree_result(result)
    # Returns False since write fails
    assert ok is False


# ===========================================================================
# Producer-bridge — composes record + persist + SSE
# ===========================================================================


def test_maybe_archive_runs_ring_and_persistence(monkeypatch, tmp_path):
    monkeypatch.setenv(ARCHIVE_MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(PERSISTENCE_PATH_ENV_VAR, str(tmp_path / "out.jsonl"))
    result = _make_result(branches_per_layer=2)
    archived = maybe_archive_tree_result(result)
    assert len(archived) == 2
    # Persistence ran
    assert (tmp_path / "out.jsonl").exists()


def test_maybe_archive_master_off_returns_empty():
    import os
    old_archive = os.environ.pop(ARCHIVE_MASTER_FLAG_ENV_VAR, None)
    old_persist = os.environ.pop(PERSISTENCE_MASTER_FLAG_ENV_VAR, None)
    try:
        result = _make_result()
        archived = maybe_archive_tree_result(result)
        assert archived == ()
    finally:
        if old_archive is not None:
            os.environ[ARCHIVE_MASTER_FLAG_ENV_VAR] = old_archive
        if old_persist is not None:
            os.environ[PERSISTENCE_MASTER_FLAG_ENV_VAR] = old_persist


def test_maybe_archive_persistence_off_still_records(monkeypatch):
    """Independent gating — ring works even if persistence is off."""
    monkeypatch.setenv(ARCHIVE_MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.delenv(PERSISTENCE_MASTER_FLAG_ENV_VAR, raising=False)
    result = _make_result(branches_per_layer=2)
    archived = maybe_archive_tree_result(result)
    assert len(archived) == 2


def test_maybe_archive_never_raises_on_internal_failure(monkeypatch):
    """Producer-bridge MUST NOT propagate internal exceptions into
    the runner path. Force a failure by passing a malformed result."""
    monkeypatch.setenv(ARCHIVE_MASTER_FLAG_ENV_VAR, "true")

    class _BrokenResult:
        @property
        def root_op_id(self):
            raise RuntimeError("malformed")

        @property
        def layers(self):
            raise RuntimeError("malformed")

    # Should NOT raise
    archived = maybe_archive_tree_result(_BrokenResult())  # type: ignore[arg-type]
    assert archived == ()


# ===========================================================================
# SSE producer-bridge composition
# ===========================================================================


def test_publish_branch_lifecycle_events_uses_canonical_constants():
    """Verify the SSE events reference the canonical constants
    registered in _VALID_EVENT_TYPES."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
        EVENT_TYPE_REPAIR_BRANCH_PROMOTED,
        EVENT_TYPE_REPAIR_BRANCH_PRUNED,
        EVENT_TYPE_REPAIR_LAYER_COMPLETED,
        EVENT_TYPE_REPAIR_TREE_WON,
    )
    assert EVENT_TYPE_REPAIR_BRANCH_PROMOTED in _VALID_EVENT_TYPES
    assert EVENT_TYPE_REPAIR_BRANCH_PRUNED in _VALID_EVENT_TYPES
    assert EVENT_TYPE_REPAIR_LAYER_COMPLETED in _VALID_EVENT_TYPES
    assert EVENT_TYPE_REPAIR_TREE_WON in _VALID_EVENT_TYPES


# ===========================================================================
# FlagRegistry seed
# ===========================================================================


def test_register_flags_installs_four_specs():
    reg = FlagRegistry()
    count = register_flags(reg)
    assert count == 4, f"expected 4 archive flags, got {count}"


def test_archive_flags_have_correct_shapes():
    reg = FlagRegistry()
    register_flags(reg)
    spec = reg.get_spec(ARCHIVE_MASTER_FLAG_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.BOOL
    assert spec.default is False  # §33.1
    assert spec.category == Category.SAFETY

    spec = reg.get_spec(PERSISTENCE_MASTER_FLAG_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.BOOL
    assert spec.default is False  # §33.1
    assert spec.category == Category.SAFETY


def test_register_flags_never_raises_on_malformed_registry():
    class _BrokenRegistry:
        def register(self, _spec):
            raise RuntimeError("registry broke")
    count = register_flags(_BrokenRegistry())
    assert count == 0  # fail-open


# ===========================================================================
# Auto-discovery via canonical seed walker
# ===========================================================================


def test_auto_discovery_picks_up_archive_flags():
    from backend.core.ouroboros.governance.flag_registry import (
        ensure_seeded, reset_default_registry,
    )
    reset_default_registry()
    try:
        registry = ensure_seeded()
        for env_var in (
            ARCHIVE_MASTER_FLAG_ENV_VAR,
            ARCHIVE_SIZE_ENV_VAR,
            PERSISTENCE_MASTER_FLAG_ENV_VAR,
            PERSISTENCE_PATH_ENV_VAR,
        ):
            spec = registry.get_spec(env_var)
            assert spec is not None, (
                f"{env_var} MUST be auto-discovered via §33.3 walker"
            )
    finally:
        reset_default_registry()

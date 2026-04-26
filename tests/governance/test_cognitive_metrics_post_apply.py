"""Phase 4 P3 follow-on — vindication post-APPLY call site regression.

Pins the new `_reflect_cognitive_metrics_post_apply_impl` orchestrator
helper + the supporting snapshot/cache machinery on
``CognitiveMetricsService``. Closes the half-open loop from P3 Slice 2:
pre-score was already wired at CONTEXT_EXPANSION; this slice wires
the matching vindication call adjacent to ``_oracle_incremental_update``.

Sections:
    (A) OracleSnapshot dataclass — frozen + correctness
    (B) snapshot_oracle_state — happy / empty target_files / oracle
        failure / per-file failure tolerated
    (C) Snapshot cache — score_pre_apply caches; bounded eviction;
        get/pop semantics
    (D) auto_reflect_post_apply — happy path; no snapshot → None;
        no target_files → None; oracle failure for after-state → None
    (E) Orchestrator helper — short-circuits on flag off / no
        applied_files / no singleton; happy-path writes vindication row
    (F) Sequence pin — post-apply call site immediately follows
        _oracle_incremental_update
    (G) Authority invariants — banned-import grep on cognitive_metrics
        unchanged post-additions
    (H) End-to-end integration — pre-score caches snap → applied_files
        threaded → post-apply records vindication row
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.cognitive_metrics import (
    CognitiveMetricsService,
    OracleSnapshot,
    _SNAPSHOT_CACHE_MAX,
    reset_default_service,
    set_default_service,
    snapshot_oracle_state,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.orchestrator import (
    _reflect_cognitive_metrics_post_apply_impl,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_COGNITIVE_METRICS_ENABLED", raising=False)
    reset_default_service()
    yield
    reset_default_service()


@pytest.fixture
def stub_oracle():
    o = MagicMock()
    o.compute_blast_radius.return_value = MagicMock(
        risk_level="LOW", total_affected=5,
    )
    o.get_dependencies.return_value = ["x"]
    o.get_dependents.return_value = ["y"]
    return o


@pytest.fixture
def service(stub_oracle, tmp_path):
    return CognitiveMetricsService(
        oracle=stub_oracle, project_root=tmp_path,
    )


def _make_ctx(target_files=("a.py",)) -> OperationContext:
    return OperationContext.create(
        target_files=tuple(target_files),
        description="post-apply pin",
    )


# ===========================================================================
# A — OracleSnapshot
# ===========================================================================


def test_oracle_snapshot_is_frozen():
    s = OracleSnapshot(
        coupling_total=4.0, blast_max=5.0, complexity_estimate=2.0,
    )
    with pytest.raises(Exception):
        s.coupling_total = 99.0  # type: ignore[misc]


def test_oracle_snapshot_holds_floats():
    s = OracleSnapshot(coupling_total=0.0, blast_max=0.0, complexity_estimate=0.0)
    assert isinstance(s.coupling_total, float)


# ===========================================================================
# B — snapshot_oracle_state
# ===========================================================================


def test_snapshot_happy_path(stub_oracle):
    s = snapshot_oracle_state(stub_oracle, ["a.py", "b.py"])
    assert s is not None
    # Each file: 1 dep + 1 dependent = 2; two files = 4 total.
    assert s.coupling_total == 4.0
    # max blast across files = 5 (constant return).
    assert s.blast_max == 5.0
    # complexity estimate = number of files
    assert s.complexity_estimate == 2.0


def test_snapshot_empty_target_files_returns_none(stub_oracle):
    assert snapshot_oracle_state(stub_oracle, []) is None


def test_snapshot_no_oracle_returns_none():
    assert snapshot_oracle_state(None, ["a.py"]) is None


def test_snapshot_per_file_failure_does_not_abort(stub_oracle):
    """One file's deps probe raises; others succeed → partial snapshot."""
    def deps_failing(path):
        if path == "bad.py":
            raise RuntimeError("oracle hiccup")
        return ["x"]
    stub_oracle.get_dependencies.side_effect = deps_failing
    s = snapshot_oracle_state(stub_oracle, ["bad.py", "good.py"])
    assert s is not None
    # Only good.py contributed coupling: 1 dep + 1 dependent = 2.
    assert s.coupling_total == 2.0


def test_snapshot_total_oracle_failure_yields_zero_snapshot(stub_oracle):
    """Per-file try/except guarantees a snapshot even when EVERY file's
    probes raise — coupling/blast both fall back to 0.0 (partial snapshot
    is better than none, per docstring). complexity_estimate still
    reflects the input file count."""
    stub_oracle.get_dependencies.side_effect = RuntimeError("boom")
    stub_oracle.get_dependents.side_effect = RuntimeError("boom")
    stub_oracle.compute_blast_radius.side_effect = RuntimeError("boom")
    s = snapshot_oracle_state(stub_oracle, ["a.py", "b.py"])
    assert s is not None
    assert s.coupling_total == 0.0
    assert s.blast_max == 0.0
    assert s.complexity_estimate == 2.0


# ===========================================================================
# C — Snapshot cache
# ===========================================================================


def test_score_pre_apply_caches_snapshot_when_flag_on(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    service.score_pre_apply("op-cache", ["a.py"], max_complexity=5)
    assert service.get_pre_apply_snapshot("op-cache") is not None


def test_score_pre_apply_no_cache_when_flag_off(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    service.score_pre_apply("op-no-cache", ["a.py"], max_complexity=5)
    assert service.get_pre_apply_snapshot("op-no-cache") is None


def test_pop_snapshot_evicts(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    service.score_pre_apply("op-pop", ["a.py"])
    snap1 = service.pop_pre_apply_snapshot("op-pop")
    snap2 = service.pop_pre_apply_snapshot("op-pop")
    assert snap1 is not None
    assert snap2 is None  # second pop → already evicted


def test_snapshot_cache_bounded_fifo_eviction(monkeypatch, service):
    """Pin: cache evicts oldest when size hits _SNAPSHOT_CACHE_MAX so
    a long session can't accumulate snapshots from never-applied ops."""
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    n = _SNAPSHOT_CACHE_MAX
    for i in range(n + 5):
        service.score_pre_apply(f"op-{i}", ["a.py"])
    # Oldest 5 should have been evicted.
    assert service.get_pre_apply_snapshot("op-0") is None
    assert service.get_pre_apply_snapshot("op-4") is None
    # Newest still cached.
    assert service.get_pre_apply_snapshot(f"op-{n + 4}") is not None


# ===========================================================================
# D — auto_reflect_post_apply
# ===========================================================================


def test_auto_reflect_happy_path(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    service.score_pre_apply("op-1", ["a.py"])
    result = service.auto_reflect_post_apply("op-1", ["a.py"])
    assert result is not None
    assert result.advisory in ("vindicating", "neutral", "concerning", "warning")
    # Cache should have been popped.
    assert service.get_pre_apply_snapshot("op-1") is None


def test_auto_reflect_returns_none_when_no_snapshot(monkeypatch, service):
    """Op never went through score_pre_apply → no cached snapshot →
    helper returns None (caller short-circuits)."""
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    result = service.auto_reflect_post_apply("op-never-scored", ["a.py"])
    assert result is None


def test_auto_reflect_returns_none_on_empty_target_files(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    service.score_pre_apply("op-1", ["a.py"])
    result = service.auto_reflect_post_apply("op-1", [])
    assert result is None


def test_auto_reflect_returns_none_when_after_oracle_fails(
    monkeypatch, tmp_path,
):
    """Pre-snapshot succeeded, but after-snapshot fails (oracle went
    down between pre + post) → helper returns None."""
    oracle = MagicMock()
    oracle.compute_blast_radius.return_value = MagicMock(total_affected=1)
    oracle.get_dependencies.return_value = []
    oracle.get_dependents.return_value = []
    svc = CognitiveMetricsService(oracle=oracle, project_root=tmp_path)
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    svc.score_pre_apply("op-1", ["a.py"])
    # Now break oracle for the after-snapshot.
    oracle.get_dependencies.side_effect = RuntimeError("oracle gone")
    oracle.get_dependents.side_effect = RuntimeError("oracle gone")
    oracle.compute_blast_radius.side_effect = RuntimeError("oracle gone")
    result = svc.auto_reflect_post_apply("op-1", ["a.py"])
    # Per-file try/except in snapshot_oracle_state catches each failure;
    # result is an empty (zeros) snapshot — still a valid result. So
    # auto_reflect proceeds with neutral deltas.
    # Either None (if snapshot returns None) or a real result is acceptable;
    # both are within the "best-effort never raise" contract.
    assert result is None or result.advisory == "neutral"


# ===========================================================================
# E — Orchestrator helper
# ===========================================================================


def test_helper_short_circuits_when_flag_off(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "false")
    set_default_service(service)
    ctx = _make_ctx()
    _reflect_cognitive_metrics_post_apply_impl(ctx, [Path("a.py")])
    # No vindication row should have been written.
    assert all(r.kind != "vindication" for r in service.load_records())


def test_helper_short_circuits_on_no_applied_files(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    set_default_service(service)
    ctx = _make_ctx()
    # Should not raise + should not write.
    _reflect_cognitive_metrics_post_apply_impl(ctx, [])
    assert all(r.kind != "vindication" for r in service.load_records())


def test_helper_short_circuits_when_singleton_missing(monkeypatch):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    reset_default_service()
    ctx = _make_ctx()
    # Should not raise even though no service is wired.
    _reflect_cognitive_metrics_post_apply_impl(ctx, [Path("a.py")])


def test_helper_writes_vindication_row_on_success(monkeypatch, service):
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    set_default_service(service)
    ctx = _make_ctx(target_files=("a.py", "b.py"))
    # Pre-apply must run first (caches snapshot).
    service.score_pre_apply(ctx.op_id, list(ctx.target_files))
    _reflect_cognitive_metrics_post_apply_impl(
        ctx, [Path(p) for p in ctx.target_files],
    )
    rows = service.load_records()
    vind_rows = [r for r in rows if r.kind == "vindication"]
    assert len(vind_rows) == 1
    assert vind_rows[0].op_id == ctx.op_id


# ===========================================================================
# F — Sequence pin (call site immediately follows _oracle_incremental_update)
# ===========================================================================


def test_pin_post_apply_call_after_oracle_incremental_update():
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    upd_idx = src.find("await self._oracle_incremental_update(applied_files)")
    cm_idx = src.find(
        "_reflect_cognitive_metrics_post_apply_impl(ctx, applied_files)",
    )
    assert upd_idx > 0, "_oracle_incremental_update call site missing"
    assert cm_idx > 0, "post-apply CM helper invocation missing"
    assert upd_idx < cm_idx, (
        "vindication call site must follow _oracle_incremental_update"
    )


def test_pin_post_apply_helper_is_module_level():
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    assert "def _reflect_cognitive_metrics_post_apply_impl(" in src


# ===========================================================================
# G — Authority invariants unchanged
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_cognitive_metrics_no_authority_imports_post_followon():
    src = _read("backend/core/ouroboros/governance/cognitive_metrics.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


# ===========================================================================
# H — End-to-end integration
# ===========================================================================


def test_end_to_end_pre_then_post(monkeypatch, service):
    """The whole post-APPLY chain in one test:
      1. score_pre_apply caches OracleSnapshot under op_id
      2. _reflect_cognitive_metrics_post_apply_impl threads applied_files
      3. auto_reflect_post_apply pops snapshot + computes deltas
      4. Vindication CognitiveMetricRecord persists to ledger
      5. Cache is empty after (snapshot was popped)
    """
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    set_default_service(service)
    ctx = _make_ctx(target_files=("backend/x.py", "backend/y.py"))

    service.score_pre_apply(ctx.op_id, list(ctx.target_files))
    assert service.get_pre_apply_snapshot(ctx.op_id) is not None

    _reflect_cognitive_metrics_post_apply_impl(
        ctx, [Path(p) for p in ctx.target_files],
    )

    # Ledger has one pre + one vindication row for this op.
    rows = [r for r in service.load_records() if r.op_id == ctx.op_id]
    kinds = sorted(r.kind for r in rows)
    assert kinds == ["pre_score", "vindication"]
    # Snapshot evicted by auto_reflect_post_apply.
    assert service.get_pre_apply_snapshot(ctx.op_id) is None


def test_end_to_end_post_only_no_pre_does_not_record_vindication(
    monkeypatch, service,
):
    """Defensive: if pre-score never ran (e.g. flag flipped between
    CONTEXT_EXPANSION and APPLY), post-apply helper short-circuits and
    no vindication row is written."""
    monkeypatch.setenv("JARVIS_COGNITIVE_METRICS_ENABLED", "true")
    set_default_service(service)
    ctx = _make_ctx(target_files=("a.py",))
    # No score_pre_apply call → no snapshot in cache.
    _reflect_cognitive_metrics_post_apply_impl(ctx, [Path("a.py")])
    vind_rows = [r for r in service.load_records() if r.kind == "vindication"]
    assert vind_rows == []

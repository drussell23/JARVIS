"""Tests for the L2 Repair Engine completion — multi-file (Phase 1) + divergence/progress (Phase 2/3).

Covers `repair_multifile.py` + `repair_progress.py`:
1. Multi-file extraction (files[] shape + legacy single fallback).
2. Topological ordering by dependency direction (dependency before dependent; cycle-safe).
3. Divergence detection (identical fail-sig OR identical patch-sig over window).
4. Stochastic escalation ladder (paradigm switch + cone bump, budget-bounded).
5. Granular progress v1.1 (sig-set narrowing) + Operational Velocity Score + memory-throttle signal.
6. Flag gating (all default OFF).
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.repair_multifile import (
    extract_candidate_files,
    l2_multifile_enabled,
    topo_sort_files,
)
from backend.core.ouroboros.governance.repair_progress import (
    RepairProgressTracker,
    diverge_escape_enabled,
    max_escalations,
    next_escalation,
    progress_v11_enabled,
)


# --------------------------------------------------------------------------- multi-file extract
class TestExtractCandidateFiles:
    def test_files_shape(self) -> None:
        cand = {"files": [
            {"file_path": "a.py", "full_content": "x", "rationale": "r"},
            {"file_path": "b.py", "full_content": "y"},
        ]}
        assert extract_candidate_files(cand) == [("a.py", "x"), ("b.py", "y")]

    def test_legacy_single(self) -> None:
        cand = {"file_path": "a.py", "full_content": "x"}
        assert extract_candidate_files(cand) == [("a.py", "x")]

    def test_skips_empty(self) -> None:
        cand = {"files": [
            {"file_path": "", "full_content": "x"},
            {"file_path": "b.py", "full_content": ""},
            {"file_path": "c.py", "full_content": "z"},
        ]}
        assert extract_candidate_files(cand) == [("c.py", "z")]

    def test_non_dict(self) -> None:
        assert extract_candidate_files(None) == []


# --------------------------------------------------------------------------- topo sort
class TestTopoSortFiles:
    def test_dependency_before_dependent(self) -> None:
        files = [("app.py", "1"), ("util.py", "2")]
        # app depends on util → util must come first
        def dep(a: str, b: str) -> bool:
            return a == "app.py" and b == "util.py"
        ordered = topo_sort_files(files, dep)
        assert [p for p, _ in ordered] == ["util.py", "app.py"]

    def test_no_provider_keeps_order(self) -> None:
        files = [("a.py", "1"), ("b.py", "2")]
        assert topo_sort_files(files, None) == files

    def test_cycle_degrades_gracefully(self) -> None:
        files = [("a.py", "1"), ("b.py", "2")]
        # mutual dependency (cycle) → stable original order, no drop, no raise
        def dep(a: str, b: str) -> bool:
            return True
        ordered = topo_sort_files(files, dep)
        assert {p for p, _ in ordered} == {"a.py", "b.py"}
        assert len(ordered) == 2

    def test_provider_raise_is_safe(self) -> None:
        files = [("a.py", "1"), ("b.py", "2")]
        def dep(a: str, b: str) -> bool:
            raise RuntimeError("graph down")
        ordered = topo_sort_files(files, dep)
        assert len(ordered) == 2  # no edges resolved → original order, no raise


# --------------------------------------------------------------------------- divergence
class TestDivergence:
    def test_identical_fail_sig_diverges(self) -> None:
        t = RepairProgressTracker()
        for _ in range(2):
            t.record(fail_sig="F", patch_sig="p%d" % _, failing_sigs=frozenset({"t1"}), diff_lines=5)
        assert t.is_diverged(window=2) is True

    def test_identical_patch_sig_diverges(self) -> None:
        t = RepairProgressTracker()
        t.record(fail_sig="F1", patch_sig="P", failing_sigs=frozenset({"t1"}), diff_lines=5)
        t.record(fail_sig="F2", patch_sig="P", failing_sigs=frozenset({"t1"}), diff_lines=5)
        assert t.is_diverged(window=2) is True

    def test_distinct_no_diverge(self) -> None:
        t = RepairProgressTracker()
        t.record(fail_sig="F1", patch_sig="P1", failing_sigs=frozenset({"t1"}), diff_lines=5)
        t.record(fail_sig="F2", patch_sig="P2", failing_sigs=frozenset(), diff_lines=2)
        assert t.is_diverged(window=2) is False

    def test_needs_full_window(self) -> None:
        t = RepairProgressTracker()
        t.record(fail_sig="F", patch_sig="P", failing_sigs=frozenset({"t1"}), diff_lines=5)
        assert t.is_diverged(window=2) is False


# --------------------------------------------------------------------------- escalation ladder
class TestEscalation:
    def test_levels_escalate(self) -> None:
        e1 = next_escalation(1)
        e2 = next_escalation(2)
        assert e1 is not None and e2 is not None
        assert e1.level == 1 and e2.level == 2
        assert e2.cone_depth_bump > e1.cone_depth_bump
        assert "ESCALATION" in e1.paradigm and e1.paradigm != e2.paradigm

    def test_budget_exhaustion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_L2_MAX_ESCALATIONS", "2")
        assert next_escalation(1) is not None
        assert next_escalation(2) is not None
        assert next_escalation(3) is None  # budget spent → terminal stop

    def test_invalid_count(self) -> None:
        assert next_escalation(0) is None


# --------------------------------------------------------------------------- progress v1.1
class TestProgressV11:
    def test_sig_set_narrowing(self) -> None:
        t = RepairProgressTracker()
        t.record(fail_sig="F1", patch_sig="P1", failing_sigs=frozenset({"t1", "t2"}), diff_lines=10)
        t.record(fail_sig="F2", patch_sig="P2", failing_sigs=frozenset({"t1"}), diff_lines=8)
        assert t.sig_set_narrowed() is True

    def test_sig_set_not_narrowed_when_disjoint(self) -> None:
        t = RepairProgressTracker()
        t.record(fail_sig="F1", patch_sig="P1", failing_sigs=frozenset({"t1"}), diff_lines=10)
        t.record(fail_sig="F2", patch_sig="P2", failing_sigs=frozenset({"t2"}), diff_lines=10)
        assert t.sig_set_narrowed() is False  # different failure, not a subset

    def test_velocity_positive_when_converging(self) -> None:
        t = RepairProgressTracker()
        t.record(fail_sig="F1", patch_sig="P1", failing_sigs=frozenset({"t1", "t2", "t3"}), diff_lines=30)
        t.record(fail_sig="F2", patch_sig="P2", failing_sigs=frozenset({"t1", "t2"}), diff_lines=20)
        t.record(fail_sig="F3", patch_sig="P3", failing_sigs=frozenset({"t1"}), diff_lines=10)
        assert t.velocity_score() > 0
        assert t.should_throttle_memory() is False

    def test_velocity_nonpositive_when_thrashing(self) -> None:
        t = RepairProgressTracker()
        for i in range(3):
            t.record(fail_sig="F", patch_sig="P%d" % i, failing_sigs=frozenset({"t1", "t2"}), diff_lines=20 + i * 5)
        assert t.velocity_score() <= 0
        assert t.should_throttle_memory() is True  # errors persist + no velocity

    def test_no_throttle_when_no_failures(self) -> None:
        t = RepairProgressTracker()
        t.record(fail_sig="F", patch_sig="P", failing_sigs=frozenset(), diff_lines=0)
        assert t.should_throttle_memory() is False


# --------------------------------------------------------------------------- flags
class TestFlags:
    def test_defaults_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("JARVIS_L2_MULTIFILE_ENABLED", "JARVIS_L2_DIVERGE_ESCAPE_ENABLED",
                  "JARVIS_L2_PROGRESS_V11_ENABLED"):
            monkeypatch.delenv(v, raising=False)
        assert l2_multifile_enabled() is False
        assert diverge_escape_enabled() is False
        assert progress_v11_enabled() is False

    def test_max_escalations_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_L2_MAX_ESCALATIONS", raising=False)
        assert max_escalations() == 2

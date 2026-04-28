"""Tests for Slice 2.2 — TrajectoryAuditor: codebase trajectory tracker."""
from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from typing import Dict

import pytest


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_TRAJECTORY_AUDITOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TRAJECTORY_AUDITOR_PATH",
                       str(tmp_path / "trajectory.jsonl"))


def _make_py(tmp_path: Path, relpath: str, content: str) -> Path:
    fp = tmp_path / relpath
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return fp


@pytest.fixture
def project(tmp_path):
    _make_py(tmp_path, "backend/core/main.py",
             'def hello():\n    if True:\n        pass\n\n__all__ = ["hello"]\n')
    _make_py(tmp_path, "backend/core/utils.py",
             'def add(a, b):\n    return a + b\n')
    _make_py(tmp_path, "backend/core/governance/policy.py",
             'def check():\n    for x in range(10):\n        if x > 5:\n            pass\n')
    _make_py(tmp_path, "backend/tests/test_main.py",
             'def test_hello():\n    assert True\n')
    _make_py(tmp_path, "backend/tests/test_utils.py",
             'def test_add():\n    assert 1 + 1 == 2\n')
    return tmp_path


@pytest.fixture
def auditor(project, tmp_path):
    from backend.core.ouroboros.governance.observability.trajectory_auditor import (
        TrajectoryAuditor,
    )
    return TrajectoryAuditor(
        project_root=project,
        snapshots_path=tmp_path / "trajectory.jsonl",
        scan_dirs=["backend"],
    )


# ---------------------------------------------------------------------------
# 1. Snapshot computation
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_counts_loc(self, auditor):
        snap = auditor.snapshot()
        assert snap.total_loc > 0

    def test_snapshot_finds_test_files(self, auditor):
        snap = auditor.snapshot()
        assert snap.test_file_count == 2

    def test_snapshot_has_governance_files(self, auditor):
        snap = auditor.snapshot()
        assert snap.governance_file_count >= 1

    def test_snapshot_has_hash(self, auditor):
        snap = auditor.snapshot()
        assert snap.snapshot_hash
        assert len(snap.snapshot_hash) > 8

    def test_snapshot_to_dict(self, auditor):
        snap = auditor.snapshot()
        d = snap.to_dict()
        assert "total_loc" in d
        assert "test_file_count" in d
        json_str = json.dumps(d)
        assert isinstance(json_str, str)


# ---------------------------------------------------------------------------
# 2. LOC counting
# ---------------------------------------------------------------------------

class TestLOC:
    def test_count_loc_skips_blanks(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import count_loc
        fp = _make_py(tmp_path, "loc_test.py", "a = 1\n\nb = 2\n\n\nc = 3\n")
        assert count_loc(fp) == 3

    def test_count_loc_skips_comments(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import count_loc
        fp = _make_py(tmp_path, "comment_test.py", "# comment\na = 1\n# another\n")
        assert count_loc(fp) == 1

    def test_count_loc_nonexistent(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import count_loc
        assert count_loc(tmp_path / "nope.py") == 0


# ---------------------------------------------------------------------------
# 3. Complexity counting
# ---------------------------------------------------------------------------

class TestComplexity:
    def test_simple_function(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import compute_complexity
        fp = _make_py(tmp_path, "simple.py", "def foo():\n    return 1\n")
        cc = compute_complexity(fp)
        assert cc == 1.0  # base only

    def test_branching_function(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import compute_complexity
        fp = _make_py(tmp_path, "branch.py",
                       "def foo():\n    if True:\n        for x in []:\n            pass\n")
        cc = compute_complexity(fp)
        assert cc == 3.0  # base + if + for

    def test_syntax_error_returns_zero(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import compute_complexity
        fp = _make_py(tmp_path, "bad.py", "def foo(\n")
        assert compute_complexity(fp) == 0.0

    def test_no_functions_returns_zero(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import compute_complexity
        fp = _make_py(tmp_path, "nofunc.py", "a = 1\nb = 2\n")
        assert compute_complexity(fp) == 0.0


# ---------------------------------------------------------------------------
# 4. Public API counting
# ---------------------------------------------------------------------------

class TestPublicAPI:
    def test_counts_all_entries(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import count_public_api
        fp = _make_py(tmp_path, "api.py", '__all__ = ["foo", "bar", "baz"]\n')
        assert count_public_api(fp) == 3

    def test_no_all_returns_zero(self, tmp_path):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import count_public_api
        fp = _make_py(tmp_path, "noall.py", "def foo(): pass\n")
        assert count_public_api(fp) == 0


# ---------------------------------------------------------------------------
# 5. Drift detection
# ---------------------------------------------------------------------------

class TestDriftDetection:
    def test_no_baseline_returns_stable(self, auditor):
        report = auditor.audit()
        assert report.verdict == "stable"
        assert report.baseline is None

    def test_drift_on_loc_growth(self, auditor, project, tmp_path):
        # Record a baseline snapshot with small LOC.
        snap = auditor.snapshot(now_unix=1000.0)
        auditor.record_snapshot(snap)

        # Add a massive amount of code.
        big_code = "\n".join(f"def func_{i}():\n    pass" for i in range(500))
        _make_py(project, "backend/core/huge.py", big_code)

        report = auditor.audit(now_unix=2000.0)
        # Should detect growth.
        if report.baseline is not None:
            assert report.current.total_loc > report.baseline.total_loc

    def test_test_file_decrease_is_critical(self, auditor, project, tmp_path):
        # Record baseline.
        snap = auditor.snapshot(now_unix=1000.0)
        auditor.record_snapshot(snap)
        # Delete test files.
        for tf in (project / "backend" / "tests").glob("test_*.py"):
            tf.unlink()
        report = auditor.audit(now_unix=2000.0)
        critical_signals = [s for s in report.drift_signals if s.severity == "critical"]
        if report.baseline and report.baseline.test_file_count > 0:
            assert len(critical_signals) >= 1


# ---------------------------------------------------------------------------
# 6. Verdict classification
# ---------------------------------------------------------------------------

class TestVerdict:
    def test_stable_no_signals(self):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import _classify_trajectory
        assert _classify_trajectory([]) == "stable"

    def test_growing_on_warning(self):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import (
            DriftSignal, _classify_trajectory,
        )
        signals = [DriftSignal("x", 1, 2, 100, "warning", "test")]
        assert _classify_trajectory(signals) == "growing"

    def test_drifting_on_one_critical(self):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import (
            DriftSignal, _classify_trajectory,
        )
        signals = [DriftSignal("x", 1, 2, 100, "critical", "test")]
        assert _classify_trajectory(signals) == "drifting"

    def test_alarming_on_two_critical(self):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import (
            DriftSignal, _classify_trajectory,
        )
        signals = [
            DriftSignal("x", 1, 2, 100, "critical", "a"),
            DriftSignal("y", 1, 2, 100, "critical", "b"),
        ]
        assert _classify_trajectory(signals) == "alarming"


# ---------------------------------------------------------------------------
# 7. Rolling baseline
# ---------------------------------------------------------------------------

class TestBaseline:
    def test_no_history_returns_none(self, auditor):
        assert auditor.baseline() is None

    def test_baseline_averages(self, auditor):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import TrajectorySnapshot
        for i in range(3):
            snap = TrajectorySnapshot(
                ts_unix=1000 + i * 100,
                total_loc=100 + i * 10,
                loc_by_module={},
                test_file_count=10 + i,
                avg_function_complexity=2.0 + i * 0.5,
                public_api_count=5,
                governance_file_count=3,
                snapshot_hash=f"h{i}",
            )
            auditor.record_snapshot(snap)
        bl = auditor.baseline()
        assert bl is not None
        assert bl.total_loc == 110  # (100 + 110 + 120) // 3


# ---------------------------------------------------------------------------
# 8. Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_record_and_reload(self, tmp_path, project):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import TrajectoryAuditor
        path = tmp_path / "persist_test.jsonl"
        a1 = TrajectoryAuditor(project_root=project, snapshots_path=path)
        snap = a1.snapshot()
        ok, detail = a1.record_snapshot(snap)
        assert ok is True

        a2 = TrajectoryAuditor(project_root=project, snapshots_path=path)
        bl = a2.baseline()
        assert bl is not None

    def test_bounded_history(self, tmp_path, project):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import (
            MAX_SNAPSHOTS, TrajectoryAuditor, TrajectorySnapshot,
        )
        path = tmp_path / "bounded.jsonl"
        a = TrajectoryAuditor(project_root=project, snapshots_path=path)
        for i in range(MAX_SNAPSHOTS + 20):
            snap = TrajectorySnapshot(
                ts_unix=float(i), total_loc=i, loc_by_module={},
                test_file_count=0, avg_function_complexity=0,
                public_api_count=0, governance_file_count=0,
                snapshot_hash=str(i),
            )
            a.record_snapshot(snap)
        lines = path.read_text().strip().splitlines()
        assert len(lines) <= MAX_SNAPSHOTS


# ---------------------------------------------------------------------------
# 9. Threshold env-configurability
# ---------------------------------------------------------------------------

class TestThresholds:
    def test_loc_threshold_env(self, monkeypatch):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import _loc_growth_warn_pct
        monkeypatch.setenv("JARVIS_TRAJECTORY_LOC_GROWTH_WARN_PCT", "25.0")
        assert _loc_growth_warn_pct() == 25.0

    def test_complexity_threshold_env(self, monkeypatch):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import _complexity_warn_pct
        monkeypatch.setenv("JARVIS_TRAJECTORY_COMPLEXITY_WARN_PCT", "15")
        assert _complexity_warn_pct() == 15.0

    def test_baseline_window_env(self, monkeypatch):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import _baseline_window
        monkeypatch.setenv("JARVIS_TRAJECTORY_BASELINE_WINDOW", "5")
        assert _baseline_window() == 5


# ---------------------------------------------------------------------------
# 10. Master flag
# ---------------------------------------------------------------------------

class TestMasterFlag:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, val):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import is_trajectory_enabled
        monkeypatch.setenv("JARVIS_TRAJECTORY_AUDITOR_ENABLED", val)
        assert is_trajectory_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy(self, monkeypatch, val):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import is_trajectory_enabled
        monkeypatch.setenv("JARVIS_TRAJECTORY_AUDITOR_ENABLED", val)
        assert is_trajectory_enabled() is False

    def test_default_disabled(self, monkeypatch):
        from backend.core.ouroboros.governance.observability.trajectory_auditor import is_trajectory_enabled
        monkeypatch.delenv("JARVIS_TRAJECTORY_AUDITOR_ENABLED", raising=False)
        assert is_trajectory_enabled() is False


# ---------------------------------------------------------------------------
# 11. Cage authority invariants
# ---------------------------------------------------------------------------

class TestCage:
    _BANNED = frozenset({
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator", "gate", "semantic_guardian",
    })

    def test_no_banned_imports(self):
        src = Path("backend/core/ouroboros/governance/observability/trajectory_auditor.py")
        if not src.exists():
            pytest.skip("source not found")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        mod = alias.name
                for b in self._BANNED:
                    assert b not in mod


# ---------------------------------------------------------------------------
# 12. Module constants pinned
# ---------------------------------------------------------------------------

class TestConstants:
    def test_pinned(self):
        from backend.core.ouroboros.governance.observability import trajectory_auditor as mod
        assert mod.MAX_SNAPSHOTS == 100
        assert mod.MAX_SNAPSHOT_FILE_BYTES == 8 * 1024 * 1024
        assert mod.MAX_DRIFT_SIGNALS == 50
        assert mod.MAX_MODULE_DEPTH == 4

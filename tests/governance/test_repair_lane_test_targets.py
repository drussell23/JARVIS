"""Slice 9 — the L2 lane's three diagnosed defects, pinned.

(1) Test scoping: L2 scoped pytest to the CANDIDATE file (a source file,
zero tests). With attributed scope the failing TEST is in
ctx.target_files — thread it. (2) backend/pytest.ini hijack: scoping
under backend/ picks up addopts (--cov/-n) the runtime doesn't have →
rc=4 usage error. Pin the config to the sandbox root's pytest.ini.
(3) 'FAILED (unknown)': getattr on a field SandboxValidationResult never
has — log rc + output tail instead."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.repair_engine import (
    _resolve_l2_test_targets,
)


class _Ctx:
    def __init__(self, targets):
        self.target_files = targets


class TestResolveL2TestTargets:
    def test_attributed_scope_threads_the_test_locus(self):
        ctx = _Ctx((
            "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py",
            "tests/governance/a1_ignition_vector/test_leaf_predicates.py",
        ))
        assert _resolve_l2_test_targets(ctx, ("fallback.py",)) == (
            "tests/governance/a1_ignition_vector/test_leaf_predicates.py",
        )

    def test_no_test_shaped_targets_falls_back(self):
        ctx = _Ctx(("backend/x/a.py",))
        assert _resolve_l2_test_targets(ctx, ("backend/x/a.py",)) == (
            "backend/x/a.py",
        )

    def test_missing_target_files_falls_back(self):
        assert _resolve_l2_test_targets(object(), ("f.py",)) == ("f.py",)

    def test_kill_switch_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_L2_TEST_TARGET_THREADING_ENABLED", "false")
        ctx = _Ctx(("src.py", "tests/test_src.py"))
        assert _resolve_l2_test_targets(ctx, ("src.py",)) == ("src.py",)


def test_run_tests_pins_sandbox_pytest_ini(tmp_path):
    """The pytest cmd must carry -c <sandbox>/pytest.ini when it exists —
    structurally pinned by inspecting the constructed argv (source-level
    assertion; the subprocess behavior is covered by Task 2's e2e)."""
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/repair_sandbox.py"
    ).read_text()
    assert '"-c"' in src and 'pytest.ini' in src, (
        "run_tests does not pin the pytest config — backend/pytest.ini "
        "hijack (rc=4 usage error) is live"
    )


def test_no_phantom_failure_class_label(tmp_path):
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/repair_engine.py"
    ).read_text()
    assert "getattr(svr, \"failure_class\"" not in src.replace("'", '"'), (
        "the phantom failure_class getattr still labels every L2 failure "
        "'(unknown)'"
    )

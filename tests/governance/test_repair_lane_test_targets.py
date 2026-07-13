"""Slice 9 — the L2 lane's three diagnosed defects, pinned.

(1) Test scoping: L2 scoped pytest to the CANDIDATE file (a source file,
zero tests). With attributed scope the failing TEST is in
ctx.target_files — thread it. (2) backend/pytest.ini hijack: scoping
under backend/ picks up addopts (--cov/-n) the runtime doesn't have →
rc=4 usage error. Pin the config to the sandbox root's pytest.ini.
(3) 'FAILED (unknown)': getattr on a field SandboxValidationResult never
has — log rc + output tail instead."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.repair_engine import (
    _resolve_l2_test_targets,
)


class _Ctx:
    def __init__(self, targets, evidence_json=""):
        self.target_files = targets
        self.intake_evidence_json = evidence_json


def _resolved_evidence(test_locus):
    return json.dumps({
        "attribution": {"status": "resolved", "test_locus": test_locus},
    })


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

    # -- Finding B: attribution-first (a source module named test_*.py
    # must never be handed to pytest when attribution names the test) ----

    def test_resolved_attribution_excludes_test_named_source_locus(self):
        ctx = _Ctx(
            (
                "backend/core/ouroboros/governance/test_runner.py",
                "tests/governance/test_foo.py",
            ),
            evidence_json=_resolved_evidence("tests/governance/test_foo.py"),
        )
        targets = _resolve_l2_test_targets(ctx, ("fallback.py",))
        assert targets == ("tests/governance/test_foo.py",)
        assert "backend/core/ouroboros/governance/test_runner.py" not in targets

    def test_malformed_evidence_falls_through_to_heuristic(self):
        ctx = _Ctx(
            ("src/a.py", "tests/test_a.py"),
            evidence_json="{not json",
        )
        assert _resolve_l2_test_targets(ctx, ("src/a.py",)) == (
            "tests/test_a.py",
        )

    def test_unresolved_op_under_singular_test_dir_matches(self, monkeypatch):
        # test_runner.py defaults JARVIS_TEST_DIR_NAMES to "tests,test" —
        # the L2 resolver must not drift to "tests" only.
        monkeypatch.delenv("JARVIS_TEST_DIR_NAMES", raising=False)
        ctx = _Ctx(("src/a.py", "test/regression_suite.py"))
        assert _resolve_l2_test_targets(ctx, ("src/a.py",)) == (
            "test/regression_suite.py",
        )

    # -- Finding A: batch coherence — changed sibling test files must be
    # exercised, not silently discarded with the fallback ----------------

    def test_multi_file_changed_sibling_test_is_unioned(self):
        ctx = _Ctx((
            "backend/x/a.py",
            "tests/test_a.py",
        ))
        targets = _resolve_l2_test_targets(
            ctx, ("backend/x/a.py", "tests/test_bar.py"),
        )
        assert targets == ("tests/test_a.py", "tests/test_bar.py")

    def test_changed_source_siblings_stay_excluded(self):
        ctx = _Ctx(("backend/x/a.py", "tests/test_a.py"))
        targets = _resolve_l2_test_targets(
            ctx, ("backend/x/a.py", "backend/x/b.py"),
        )
        assert targets == ("tests/test_a.py",)

    def test_attributed_lane_still_unions_changed_sibling(self):
        ctx = _Ctx(
            ("src/a.py", "tests/test_a.py"),
            evidence_json=_resolved_evidence("tests/test_a.py"),
        )
        targets = _resolve_l2_test_targets(
            ctx, ("src/a.py", "tests/test_bar.py", "tests/test_a.py"),
        )
        assert targets == ("tests/test_a.py", "tests/test_bar.py")


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

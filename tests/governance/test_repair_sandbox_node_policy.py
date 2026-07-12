"""Slice 8 sweep: the L2 repair lane's sandbox test runs must survive the
node's no-/tmp sandbox-prefix policy. RepairSandbox materializes a FULL
tree (git worktree/rsync) in a mkdtemp dir; if any path gate anchored at
the MAIN repo_root judges files inside it, the L2 lane inherits the
Run #18 class (masked as 'unknown' failures)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_NODE_POLICY = "/nonexistent-sandbox-prefix"


@pytest.fixture
def node_policy(monkeypatch):
    monkeypatch.setenv("JARVIS_SANDBOX_PREFIXES", _NODE_POLICY)


def test_testrunner_anchored_at_sandbox_root_survives_node_policy(
    node_policy, tmp_path
):
    """A TestRunner whose repo_root IS the sandbox tree (how the repair
    lane must anchor) treats in-sandbox tests as safe — no vacuous pass,
    no skip — even under node policy."""
    from backend.core.ouroboros.governance.test_runner import _is_safe_path

    sandbox_tree = tmp_path / "jarvis_repair_sandbox_x"
    test_file = sandbox_tree / "tests" / "test_ok.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_ok():\n    assert True\n")
    assert _is_safe_path(test_file, sandbox_tree) is True


def test_repair_engine_test_invocation_is_sandbox_anchored(node_policy):
    """Structural pin: grep-level assertion that the repair engine's test
    execution anchors its runner/pytest cwd at the SANDBOX root, not the
    main repo root judging sandbox paths. Read the source and assert the
    anchoring expression exists; if this fails, the L2 lane shares the
    Run #18 class and the minimal fix (anchor the runner at
    sandbox.sandbox_root) is in scope for this task."""
    src = (
        _REPO / "backend" / "core" / "ouroboros" / "governance"
        / "repair_engine.py"
    ).read_text(encoding="utf-8")
    assert "sandbox_root" in src, (
        "repair_engine.py never references sandbox_root — its test runs "
        "cannot be sandbox-anchored; L2 inherits the Run #18 class"
    )


async def test_repair_sandbox_run_tests_executes_under_node_policy(
    node_policy, tmp_path
):
    """Behavioral pin: RepairSandbox.run_tests is self-anchored — pytest
    runs with cwd=sandbox_root and never consults the sandbox-prefix
    policy, so a mkdtemp-style sandbox under node policy still EXECUTES
    tests (a real failure surfaces as failure; no vacuous pass, no skip).
    This is the live-lane counterpart of the structural pins above."""
    from backend.core.ouroboros.governance.repair_sandbox import RepairSandbox

    sandbox_tree = tmp_path / "jarvis_repair_sandbox_live"
    test_file = sandbox_tree / "tests" / "test_probe.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_pass():\n    assert True\n\n"
        "def test_fail():\n    assert False\n"
    )

    sb = RepairSandbox(repo_root=_REPO, test_timeout_s=60.0)
    # Anchor the sandbox directly at the pre-built tree — the full
    # worktree/rsync materialization is out of scope for this pin.
    sb._sandbox_dir = sandbox_tree
    try:
        result = await sb.run_tests(("tests/test_probe.py",), timeout_s=60.0)
    finally:
        sb._sandbox_dir = None  # never let teardown touch tmp_path

    # The run must have genuinely executed: the deliberate failure is
    # visible in the result. A path-gate skip would yield a vacuous pass
    # or an empty run — both rejected here.
    assert result.passed is False
    assert result.returncode not in (0, -1), result.stderr
    assert "test_fail" in result.stdout or "1 failed" in result.stdout

from __future__ import annotations
from backend.core.ouroboros.governance import autonomous_workspace as aw

def test_lock_disabled_is_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "false")
    assert aw._deterministic_force(root="/x", is_primary=True, container=False, autonomous=True) is False

def test_lock_forces_in_primary_autonomous_noncontainer(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert aw._deterministic_force(root="/x", is_primary=True, container=False, autonomous=True) is True

def test_lock_noop_in_container(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert aw._deterministic_force(root="/x", is_primary=True, container=True, autonomous=True) is False

def test_lock_noop_outside_primary(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert aw._deterministic_force(root="/x", is_primary=False, container=False, autonomous=True) is False

def test_lock_noop_when_operator_present(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert aw._deterministic_force(root="/x", is_primary=True, container=False, autonomous=False) is False

def test_arm_dual_arms_both_flags(monkeypatch):
    monkeypatch.delenv("JARVIS_FILE_ISOLATION_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_EXECUTION_BOUNDARY_ENABLED", raising=False)
    aw._arm_boundary_flags()
    import os
    assert os.environ["JARVIS_FILE_ISOLATION_ENABLED"] == "true"
    assert os.environ["JARVIS_EXECUTION_BOUNDARY_ENABLED"] == "true"


def test_forced_flow_routes_to_worktree_and_arms_both(monkeypatch, tmp_path):
    """Integration test: forced path arms both flags and routes to the worktree.

    Exercises the full resolve_loop_project_root forced flow (LR-A lock enabled,
    primary checkout, non-container, autonomous) — the load-bearing path the
    unit tests above don't cover end-to-end.
    """
    import asyncio
    import os
    from pathlib import Path
    from backend.core.ouroboros.governance import execution_context as ec

    # Lock on, both isolation flags absent.
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    monkeypatch.delenv("JARVIS_FILE_ISOLATION_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_EXECUTION_BOUNDARY_ENABLED", raising=False)

    # Patch the execution_context functions so both call-sites in
    # resolve_loop_project_root (the direct import in the first try-block and
    # the module-level ec.is_autonomous in the second) see the forced values.
    monkeypatch.setattr(ec, "is_primary_checkout", lambda *a, **k: True)
    monkeypatch.setattr(ec, "_is_cloud_container", lambda *a, **k: False)
    monkeypatch.setattr(ec, "is_autonomous", lambda *a, **k: True)

    # Fake WorktreeManager: create() matches the real signature
    # (async def create(self, branch_name: str) -> Path).
    wt = tmp_path / "wt"

    class _FakeWM:
        async def create(self, branch_name: str) -> Path:
            return wt

    result = asyncio.run(
        aw.resolve_loop_project_root(
            str(tmp_path),
            session_id="s1",
            worktree_manager=_FakeWM(),
        )
    )

    # (a) routed to the worktree, NOT the primary root.
    assert result == wt, f"Expected worktree {wt}, got {result}"
    # (b) both boundary flags are armed in the environment.
    assert os.environ.get("JARVIS_FILE_ISOLATION_ENABLED") == "true"
    assert os.environ.get("JARVIS_EXECUTION_BOUNDARY_ENABLED") == "true"

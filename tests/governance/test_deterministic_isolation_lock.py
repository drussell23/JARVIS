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

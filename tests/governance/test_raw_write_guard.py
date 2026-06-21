from __future__ import annotations
from backend.core.ouroboros.governance import tool_executor as te

def test_raw_write_denied_in_primary_autonomous(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert te._deny_primary_raw_write(is_primary=True, autonomous=True) is True

def test_raw_write_allowed_in_worktree(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert te._deny_primary_raw_write(is_primary=False, autonomous=True) is False

def test_raw_write_allowed_for_operator(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert te._deny_primary_raw_write(is_primary=True, autonomous=False) is False

def test_raw_write_guard_off_when_lock_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "false")
    assert te._deny_primary_raw_write(is_primary=True, autonomous=True) is False

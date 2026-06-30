from __future__ import annotations
import backend.core.ouroboros.governance.pre_apply_exec_lock as lock


def test_lock_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_SANDBOX_LOCK_ENABLED", raising=False)
    assert lock.lock_enabled() is False


def test_lock_enabled_when_flagged(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_SANDBOX_LOCK_ENABLED", "true")
    assert lock.lock_enabled() is True

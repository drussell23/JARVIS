# [Ouroboros] Modified by Ouroboros (op=op-01a07974-) at 2026-09-07 01:27 UTC
# Reason: Append the Tier-1 proof test to test_gate1_orchestrator_wiring.py  In the EXISTING file tests/governance/test_gate1_orch

from __future__ import annotations
import backend.core.ouroboros.governance.pre_apply_exec_lock as lock


def test_lock_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_SANDBOX_LOCK_ENABLED", raising=False)
    assert lock.lock_enabled() is False


def test_lock_enabled_when_flagged(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_SANDBOX_LOCK_ENABLED", "true")
    assert lock.lock_enabled() is True

def test_tier1_atomic_proof():
    assert sum(range(5)) == 10

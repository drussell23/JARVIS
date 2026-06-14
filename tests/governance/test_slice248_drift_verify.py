"""Slice 248 — harden re-alignment with an APPLY-time verification pass.

Slice 247 detects drift at GENERATE entry and injects re-read feedback, then
TRUSTS the model to regenerate against the new state. But the model could ignore
the instruction, or the disk could drift AGAIN during regeneration. The legacy
APPLY-time hash check (orchestrator ~8549) detected this but was LOG-ONLY — it
then blindly applied the stale candidate, corrupting the file.

This slice converts that into a deterministic, zero-LLM VERIFICATION GATE: before
APPLY, re-hash the targets; if the candidate's baseline no longer matches disk
(provably stale), BLOCK the apply (fail-safe -> POSTMORTEM, no corruption) rather
than blind-applying. The op then re-runs fresh on its next sensor trigger,
generating against current disk — an eventual re-alignment. Routing APPLY ->
GENERATE_RETRY in-line is unsafe (past the retry walk), so fail-safe is the
correct action. Reuses the Slice 247 detect_drift + the existing LiveWorkSensor
abort pattern (record FAILED -> advance POSTMORTEM -> publish outcome).
"""
from __future__ import annotations

import hashlib
import inspect

import pytest

from backend.core.ouroboros.governance import state_drift as sd


def _write(p, content: str) -> str:
    p.write_text(content)
    return hashlib.sha256(content.encode()).hexdigest()


class TestVerifyGate:
    def test_enabled_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_STATE_DRIFT_VERIFY_ENABLED", raising=False)
        assert sd.state_drift_verify_enabled() is True

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("JARVIS_STATE_DRIFT_VERIFY_ENABLED", "0")
        assert sd.state_drift_verify_enabled() is False


class TestUnreconciledConstant:
    def test_terminal_reason_code_present(self):
        assert sd.STATE_DRIFT_UNRECONCILED == "state_drift_unreconciled"


class TestShouldBlockApply:
    def test_blocks_when_drift_and_gate_on(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_STATE_DRIFT_VERIFY_ENABLED", raising=False)
        h = _write(tmp_path / "a.py", "v1\n")
        (tmp_path / "a.py").write_text("v2-human-edit\n")  # drifted
        block, drifted = sd.should_block_apply([("a.py", h)], tmp_path)
        assert block is True
        assert drifted == ["a.py"]

    def test_no_block_when_aligned(self, tmp_path):
        h = _write(tmp_path / "a.py", "stable\n")
        block, drifted = sd.should_block_apply([("a.py", h)], tmp_path)
        assert block is False
        assert drifted == []

    def test_gate_off_detects_but_does_not_block(self, tmp_path, monkeypatch):
        """Kill switch → legacy log-and-apply: drift still surfaced for the
        ledger, but the apply is NOT blocked (byte-identical to pre-248)."""
        monkeypatch.setenv("JARVIS_STATE_DRIFT_VERIFY_ENABLED", "0")
        h = _write(tmp_path / "a.py", "v1\n")
        (tmp_path / "a.py").write_text("v2\n")
        block, drifted = sd.should_block_apply([("a.py", h)], tmp_path)
        assert block is False, "kill switch must not block"
        assert drifted == ["a.py"], "but drift is still reported for telemetry"

    def test_never_raises(self, tmp_path):
        block, drifted = sd.should_block_apply(None, None)
        assert block is False and drifted == []


class TestOrchestratorWiring:
    def test_apply_seam_blocks_on_unreconciled_drift(self):
        from backend.core.ouroboros.governance import orchestrator as orch
        src = inspect.getsource(orch)
        assert "should_block_apply" in src, "APPLY seam must use the verification gate"
        assert "state_drift_unreconciled" in src or "STATE_DRIFT_UNRECONCILED" in src
        # on a verification failure it must abort to POSTMORTEM, NOT blind-apply
        i_block = src.find("if _block_apply:")
        assert i_block > 0, "must branch on the block decision"
        seg = src[i_block:i_block + 1600]
        assert "POSTMORTEM" in seg, "blocked apply must route to POSTMORTEM (fail-safe)"
        assert "return ctx" in seg, "blocked apply must return (not fall through to apply)"

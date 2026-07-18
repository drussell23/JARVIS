"""Stateful pipeline suspend/resume — the registration-activation root-cause fix.

The FSM suspend/resume machinery (fsm_checkpoint: capture_inflight → signed
checkpoint → list_pending → build_resume_envelope → hydrate) was fully built and
wired (harness suspend both paths; intake-router _hydrate_fsm_checkpoints on
boot), but INERT: its data source, the in_flight_registry, is populated only when
JARVIS_IN_FLIGHT_REGISTRY_ENABLED is on — and that defaults FALSE (§33.1, the
*reaper's* flag). So a default-ON suspend feature was transitively starved:
bt-2026-07-17-234137 hit session exhaustion with 7 ops mid-GENERATE and
capture_inflight checkpointed 0 ("in-flight registry empty").

The fix decouples registry POPULATION from the reaper flag: register whenever
EITHER consumer needs it (reaper OR FSM checkpointing, default-on). These tests
pin the decoupling AND the full serialize→(second boot)→hydrate cycle.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import fsm_checkpoint as ckpt
from backend.core.ouroboros.governance import in_flight_registry as R


@pytest.fixture(autouse=True)
def _clean_registry():
    R.reset_default_registry()
    yield
    R.reset_default_registry()


class _FakeCtx:
    """Minimal op context mirroring what capture_from_context reads."""

    def __init__(self, op_id, phase="GENERATE"):
        self.op_id = op_id
        self.description = "optimize the hot loop"
        self.target_files = ["backend/a.py"]
        self.provider_route = "complex"
        self.intake_evidence_json = '{"sensor": "opportunity_miner"}'
        self.phase = phase


# ---------------------------------------------------------------------------
# Part A — the decoupling: registration active when EITHER consumer wants it
# ---------------------------------------------------------------------------


def test_both_consumers_off_is_inert(monkeypatch):
    monkeypatch.setenv("JARVIS_IN_FLIGHT_REGISTRY_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "false")
    assert R.registration_active() is False
    assert R.register_op_safely("op-1", ctx_ref=_FakeCtx("op-1")) is False
    assert len(R.get_default_registry().snapshot()) == 0


def test_checkpoint_on_activates_registration_even_with_reaper_off(monkeypatch):
    # THE fix: the reaper stays advisory-off (§33.1) but the default-ON suspend
    # feature now populates the registry.
    monkeypatch.setenv("JARVIS_IN_FLIGHT_REGISTRY_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
    assert R.master_enabled() is False          # reaper NOT made load-bearing
    assert R.registration_active() is True
    assert R.register_op_safely("op-1", ctx_ref=_FakeCtx("op-1"),
                                last_phase_name="GENERATE") is True
    assert len(R.get_default_registry().snapshot()) == 1


def test_reaper_on_also_activates(monkeypatch):
    monkeypatch.setenv("JARVIS_IN_FLIGHT_REGISTRY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "false")
    assert R.registration_active() is True
    assert R.register_op_safely("op-1", ctx_ref=_FakeCtx("op-1")) is True


def test_checkpoint_default_is_on(monkeypatch):
    # Prove the real production default: with NOTHING set, checkpointing (and thus
    # registration) is active — this is the state the soak actually runs in.
    monkeypatch.delenv("JARVIS_IN_FLIGHT_REGISTRY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_FSM_CHECKPOINT_ENABLED", raising=False)
    assert R.registration_active() is True


# ---------------------------------------------------------------------------
# Part B — SUSPEND: capture_inflight now checkpoints registered ops
# (the exact path that captured 0 in the soak)
# ---------------------------------------------------------------------------


def test_capture_inflight_checkpoints_registered_ops(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_IN_FLIGHT_REGISTRY_ENABLED", "false")
    base = str(tmp_path)

    # Two ops mid-pipeline, registered as the live loop would.
    R.register_op_safely("op-aaa", ctx_ref=_FakeCtx("op-aaa"),
                         last_phase_name="GENERATE")
    R.register_op_safely("op-bbb", ctx_ref=_FakeCtx("op-bbb", phase="VALIDATE"),
                         last_phase_name="VALIDATE")

    n = ckpt.capture_inflight(base_dir=base, reason="session_exhausted")
    assert n == 2                                   # was 0 in the soak
    pending = {cp.op_id: cp for cp in ckpt.list_pending(base_dir=base)}
    assert set(pending) == {"op-aaa", "op-bbb"}
    assert pending["op-aaa"].phase == "GENERATE"    # halted phase preserved
    assert pending["op-bbb"].phase == "VALIDATE"
    assert pending["op-aaa"].resume_reason == "session_exhausted"


def test_empty_registry_still_captures_zero_gracefully(monkeypatch, tmp_path):
    # The pre-fix behavior is still correct when there genuinely is nothing.
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
    assert ckpt.capture_inflight(base_dir=str(tmp_path), reason="idle") == 0


# ---------------------------------------------------------------------------
# Part C — RESUME: a SECOND boot hydrates + resumes at the halted phase
# ---------------------------------------------------------------------------


def test_second_boot_hydrates_and_resumes_at_halted_phase(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
    base = str(tmp_path)

    # ---- BOOT 1: op runs to mid-GENERATE, session exhausts, suspend ----
    R.register_op_safely("op-resume", ctx_ref=_FakeCtx("op-resume"),
                         last_phase_name="GENERATE")
    assert ckpt.capture_inflight(base_dir=base, reason="wall_clock_cap") == 1

    # ---- BOOT 2: fresh process — registry empty, checkpoint on disk ----
    R.reset_default_registry()
    assert len(R.get_default_registry().snapshot()) == 0   # nothing in memory

    pending = ckpt.list_pending(base_dir=base)             # HMAC verifies (same key)
    assert len(pending) == 1
    cp = pending[0]

    env = ckpt.build_resume_envelope(cp)
    # The resume envelope re-enters intake carrying the halted phase, so the
    # pipeline FAST-FORWARDS past already-completed steps (Triage/Plan) instead
    # of restarting clean.
    assert env["op_id"] == "op-resume"
    assert env["resume"] is True
    assert env["resume_phase"] == "GENERATE"
    assert env["source"] == "fsm_resume"
    assert env["target_files"] == ["backend/a.py"]
    assert env["provider_route"] == "complex"
    # intake_evidence_json is preserved across the boot (the known WAL-resume gap
    # this path does NOT have).
    assert "opportunity_miner" in env["intake_evidence_json"]


def test_hmac_key_stable_across_boots_same_base(monkeypatch, tmp_path):
    # Same base_dir → same persisted key → resume verifies. This is why a normal
    # host resumes: .ouroboros/checkpoint_key persists across ignitions.
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CHECKPOINT_HMAC_SECRET", raising=False)
    base = str(tmp_path)
    R.register_op_safely("op-k", ctx_ref=_FakeCtx("op-k"), last_phase_name="GENERATE")
    assert ckpt.capture_inflight(base_dir=base, reason="wall_clock_cap") == 1
    assert len(ckpt.list_pending(base_dir=base)) == 1      # verifies


def test_checkpoint_written_under_a_different_key_is_rejected(monkeypatch, tmp_path):
    # Explains the soak's Jul-3 orphan REJECTs: a checkpoint signed with one key
    # (e.g. a driver-provisioned JARVIS_CHECKPOINT_HMAC_SECRET) fails verify under
    # a different key → clean boot, never a silent corrupt resume.
    base = str(tmp_path)
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "key-from-session-1")
    R.register_op_safely("op-x", ctx_ref=_FakeCtx("op-x"), last_phase_name="GENERATE")
    assert ckpt.capture_inflight(base_dir=base, reason="wall_clock_cap") == 1
    assert len(ckpt.list_pending(base_dir=base)) == 1      # verifies with same key

    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "key-from-session-2")
    # Different key → HMAC verify fails → excluded from resume (clean boot).
    assert len(ckpt.list_pending(base_dir=base)) == 0

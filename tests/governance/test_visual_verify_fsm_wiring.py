"""Regression spine for the orchestrator FSM wiring (Task 22 handoff #4).

Covers:

* ``OperationPhase.VISUAL_VERIFY`` enum present.
* FSM transitions: VERIFY → VISUAL_VERIFY is allowed (progress);
  VISUAL_VERIFY → VALIDATE_RETRY allowed (L2 dispatch on fail);
  terminal-reachability invariant still holds (all terminal escapes
  auto-injected for both phases).
* ``run_post_verify`` single-call driver: composes deterministic +
  advisory + ledger recording into one entry point; I4 asymmetry
  preserved; back-compat path (master switch off → ran=False);
  advisory only runs on deterministic pass.
* ``OperationContext.advance(VERIFY → VISUAL_VERIFY)`` succeeds (FSM
  accepts the transition).
* Production code uses the new driver — structural guard.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.op_context import (
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    Attachment,
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.visual_verify import (
    ADVISORY_ALIGNED,
    ADVISORY_REGRESSED,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_SKIPPED,
    AdvisoryLedger,
    AdvisoryVerdict,
    VisualVerifyDispatchOutcome,
    run_post_verify,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Slice 3 master switch OFF by default — most tests set it per-case.
    monkeypatch.delenv("JARVIS_VISION_VERIFY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", raising=False)
    yield


def _pre_post(tmp_path, app_id="com.apple.Terminal"):
    pre_path = tmp_path / "pre.png"
    pre_path.write_bytes(b"\x89PNG" + b"\x00" * 64)
    post_path = tmp_path / "post.png"
    post_path.write_bytes(b"\x89PNG" + b"\xff" * 64)
    pre = Attachment.from_file(str(pre_path), kind="pre_apply", app_id=app_id)
    post = Attachment.from_file(str(post_path), kind="post_apply", app_id=app_id)
    return pre, post


# ---------------------------------------------------------------------------
# Enum + FSM transitions
# ---------------------------------------------------------------------------


def test_visual_verify_phase_enum_present():
    assert OperationPhase.VISUAL_VERIFY is not None
    assert OperationPhase.VISUAL_VERIFY.name == "VISUAL_VERIFY"


def test_verify_can_transition_to_visual_verify():
    assert OperationPhase.VISUAL_VERIFY in PHASE_TRANSITIONS[OperationPhase.VERIFY]


def test_visual_verify_can_transition_to_validate_retry_for_l2():
    assert (
        OperationPhase.VALIDATE_RETRY
        in PHASE_TRANSITIONS[OperationPhase.VISUAL_VERIFY]
    )


def test_visual_verify_has_all_terminal_escapes():
    """Terminal-reachability invariant: every non-terminal phase must
    be able to reach CANCELLED / EXPIRED / POSTMORTEM / COMPLETE.
    """
    targets = PHASE_TRANSITIONS[OperationPhase.VISUAL_VERIFY]
    for terminal in TERMINAL_PHASES:
        assert terminal in targets, (
            f"VISUAL_VERIFY missing terminal escape to {terminal.name}"
        )


def test_verify_still_reaches_complete_back_compat():
    """Back-compat: VERIFY can still go directly to COMPLETE (orch-
    estrators that don't know about Visual VERIFY keep working).
    """
    assert OperationPhase.COMPLETE in PHASE_TRANSITIONS[OperationPhase.VERIFY]


def test_visual_verify_not_terminal():
    """VISUAL_VERIFY is a progress phase — it has outgoing transitions.
    Adding it to TERMINAL_PHASES by mistake would break the FSM.
    """
    assert OperationPhase.VISUAL_VERIFY not in TERMINAL_PHASES


# ---------------------------------------------------------------------------
# OperationContext.advance accepts the new transition
# ---------------------------------------------------------------------------


def test_context_advance_verify_to_visual_verify():
    ctx = OperationContext.create(
        target_files=("src/Button.tsx",),
        description="test",
    )
    # Walk the FSM up to VERIFY.
    ctx = ctx.advance(OperationPhase.ROUTE)
    ctx = ctx.advance(OperationPhase.GENERATE)
    ctx = ctx.advance(OperationPhase.VALIDATE)
    ctx = ctx.advance(OperationPhase.GATE)
    ctx = ctx.advance(OperationPhase.APPLY)
    ctx = ctx.advance(OperationPhase.VERIFY)
    # Now transition to VISUAL_VERIFY — must not raise.
    ctx = ctx.advance(OperationPhase.VISUAL_VERIFY)
    assert ctx.phase == OperationPhase.VISUAL_VERIFY


def test_context_advance_visual_verify_to_complete():
    """Pass path: VISUAL_VERIFY → COMPLETE via terminal reachability."""
    ctx = OperationContext.create(
        target_files=("src/Button.tsx",), description="test",
    )
    for phase in (
        OperationPhase.ROUTE,
        OperationPhase.GENERATE,
        OperationPhase.VALIDATE,
        OperationPhase.GATE,
        OperationPhase.APPLY,
        OperationPhase.VERIFY,
        OperationPhase.VISUAL_VERIFY,
        OperationPhase.COMPLETE,
    ):
        ctx = ctx.advance(phase)
    assert ctx.phase == OperationPhase.COMPLETE


def test_context_advance_visual_verify_to_validate_retry():
    """Fail path: VISUAL_VERIFY → VALIDATE_RETRY for L2 dispatch."""
    ctx = OperationContext.create(
        target_files=("src/Button.tsx",), description="test",
    )
    for phase in (
        OperationPhase.ROUTE,
        OperationPhase.GENERATE,
        OperationPhase.VALIDATE,
        OperationPhase.GATE,
        OperationPhase.APPLY,
        OperationPhase.VERIFY,
        OperationPhase.VISUAL_VERIFY,
        OperationPhase.VALIDATE_RETRY,
    ):
        ctx = ctx.advance(phase)
    assert ctx.phase == OperationPhase.VALIDATE_RETRY


# ---------------------------------------------------------------------------
# run_post_verify driver
# ---------------------------------------------------------------------------


def test_run_post_verify_skipped_when_master_switch_off(tmp_path):
    pre, post = _pre_post(tmp_path)
    out = run_post_verify(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        op_id="op-1",
        op_description="restyle",
    )
    assert out.ran is False
    assert out.skipped_reason == "master_switch_off"


def test_run_post_verify_skipped_on_backend_op(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", "true")
    pre, post = _pre_post(tmp_path)
    out = run_post_verify(
        target_files=("backend/server.py",),       # backend → not UI
        attachments=(pre, post),
        op_id="op-1",
        op_description="refactor",
    )
    assert out.ran is False
    assert out.skipped_reason == "not_ui_affected"


def test_run_post_verify_deterministic_pass_without_advisory(tmp_path, monkeypatch):
    """Slice 3 on, Slice 4 off → deterministic runs, advisory skipped."""
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", "true")
    pre, post = _pre_post(tmp_path)
    out = run_post_verify(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        op_id="op-1",
        op_description="restyle",
        hash_distance_fn=lambda a, b: 0.5,
    )
    assert out.ran is True
    assert out.result.verdict == VERDICT_PASS
    assert out.advisory is None     # advisory disabled
    assert out.l2_triggered is False


def test_run_post_verify_deterministic_fail_does_not_run_advisory(
    tmp_path, monkeypatch,
):
    """Advisory only runs on deterministic pass — a fail routes directly
    to L2 via the deterministic verdict without advisory noise."""
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", "true")
    pre, post = _pre_post(tmp_path)

    advisory_calls = []

    def _advisory(pre_b, post_b, intent):
        advisory_calls.append(intent)
        return {
            "verdict": ADVISORY_REGRESSED, "confidence": 0.9,
            "model": "test", "reasoning": "",
        }

    out = run_post_verify(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        op_id="op-1",
        op_description="restyle",
        hash_distance_fn=lambda a, b: 0.0,   # hash_unchanged fail
        advisory_fn=_advisory,
    )
    assert out.ran is True
    assert out.result.verdict == VERDICT_FAIL
    # Advisory never called.
    assert advisory_calls == []
    assert out.advisory is None


def test_run_post_verify_advisory_regressed_above_threshold_triggers_l2(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", "true")
    pre, post = _pre_post(tmp_path)
    ledger = AdvisoryLedger(path=str(tmp_path / "advisory.json"))

    def _advisory(pre_b, post_b, intent):
        return {
            "verdict": ADVISORY_REGRESSED, "confidence": 0.9,
            "model": "test", "reasoning": "UI regressed",
        }

    out = run_post_verify(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        op_id="op-1",
        op_description="restyle",
        hash_distance_fn=lambda a, b: 0.5,   # deterministic pass
        advisory_fn=_advisory,
        ledger=ledger,
    )
    assert out.ran is True
    assert out.result.verdict == VERDICT_PASS
    assert out.advisory is not None
    assert out.advisory.verdict == ADVISORY_REGRESSED
    assert out.l2_triggered is True
    # Advisory recorded in ledger.
    assert len(ledger.entries) == 1
    assert ledger.entries[0]["op_id"] == "op-1"


def test_run_post_verify_advisory_aligned_no_l2(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", "true")
    pre, post = _pre_post(tmp_path)

    out = run_post_verify(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        op_id="op-1",
        op_description="restyle",
        hash_distance_fn=lambda a, b: 0.5,
        advisory_fn=lambda p, q, i: {
            "verdict": ADVISORY_ALIGNED, "confidence": 0.95,
            "model": "test",
        },
    )
    assert out.ran is True
    assert out.advisory.verdict == ADVISORY_ALIGNED
    assert out.l2_triggered is False


def test_run_post_verify_i4_clamp_preserved(tmp_path, monkeypatch):
    """TestRunner red + deterministic pass → clamp to fail (I4)."""
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", "true")
    pre, post = _pre_post(tmp_path)
    out = run_post_verify(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        op_id="op-1",
        op_description="restyle",
        hash_distance_fn=lambda a, b: 0.5,
        test_runner_result="failed",
    )
    assert out.result.verdict == VERDICT_FAIL
    assert "I4 asymmetry" in out.result.reasoning


def test_run_post_verify_reasoning_combines_det_and_advisory(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_VISION_VERIFY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", "true")
    pre, post = _pre_post(tmp_path)
    out = run_post_verify(
        target_files=("src/Button.tsx",),
        attachments=(pre, post),
        op_id="op-1",
        op_description="restyle",
        hash_distance_fn=lambda a, b: 0.5,
        advisory_fn=lambda p, q, i: {
            "verdict": ADVISORY_REGRESSED, "confidence": 0.85,
            "model": "test", "reasoning": "",
        },
    )
    # Combined reasoning has both det= and advisory= tokens for operator logs.
    assert "det=" in out.reasoning
    assert "advisory=" in out.reasoning


def test_dispatch_outcome_frozen():
    out = VisualVerifyDispatchOutcome(
        ran=False, result=None, advisory=None,
        l2_triggered=False, reasoning="",
    )
    with pytest.raises(Exception):
        out.ran = True   # type: ignore[misc]


# ---------------------------------------------------------------------------
# Production-source structural guards
# ---------------------------------------------------------------------------


def test_op_context_adds_visual_verify_enum():
    repo = Path(__file__).resolve().parents[2]
    src = (
        repo / "backend/core/ouroboros/governance/op_context.py"
    ).read_text(encoding="utf-8")
    assert "VISUAL_VERIFY = auto()" in src


def test_op_context_wires_verify_to_visual_verify_transition():
    repo = Path(__file__).resolve().parents[2]
    src = (
        repo / "backend/core/ouroboros/governance/op_context.py"
    ).read_text(encoding="utf-8")
    # VERIFY's progress set contains VISUAL_VERIFY.
    verify_block_start = src.find("OperationPhase.VERIFY: {")
    verify_block_end = src.find("}", verify_block_start)
    verify_block = src[verify_block_start:verify_block_end]
    assert "OperationPhase.VISUAL_VERIFY" in verify_block


def test_run_post_verify_exposes_dispatch_outcome_dataclass():
    """Regression guard: operator orchestrator code calls
    ``run_post_verify`` and expects a ``VisualVerifyDispatchOutcome``
    with exactly these fields. A rename would silently break the
    wiring.
    """
    import dataclasses
    fields = {f.name for f in dataclasses.fields(VisualVerifyDispatchOutcome)}
    assert fields == {
        "ran", "result", "advisory", "l2_triggered",
        "reasoning", "skipped_reason",
    }

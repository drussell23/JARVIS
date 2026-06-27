"""Anti-Venom Task 7 — orchestrator fail-closed guardian + noop/baseline guards
+ shielded apply.

These tests cover the three brain-stem hardening edits in
``backend/core/ouroboros/governance/orchestrator.py``:

* **Lock A — fail-CLOSED guardian.** A ``SemanticGuardian`` crash used to fail
  OPEN (empty findings → no tier floor → SAFE_AUTO auto-applies). It now injects
  a hard sentinel finding and forces ``APPROVAL_REQUIRED``.
* **S2 — noop in-loop write guard.** ``is_noop`` + non-empty
  ``venom_edit_history`` ⇒ the op is CANCELLED (in-loop writes that never
  passed the guardian must not silently COMPLETE).
* **S2 — guardian git-HEAD baseline.** Venom-edited paths are baselined from
  ``git show HEAD:<path>`` so the guardian compares original→candidate.
* **C2 — asyncio.shield the apply.** The two APPLY call sites are
  ``asyncio.shield``-wrapped: the write + ledger commit completes even if the
  op task is cancelled, while ``CancelledError`` still propagates so the stop is
  honored.

The APPLY/GATE/noop logic lives inline in the ~10k-line
``GovernedOrchestrator._run_pipeline`` with no isolatable seam, so these tests
combine (a) **behavioral** tests of the exact mechanisms the edits rely on
(the real ``Detection``/``recommend_tier_floor`` contract, the real
``OperationContext.advance`` FSM, real ``asyncio.shield`` cancellation
semantics) with (b) **structural** assertions read from the live
``inspect.getsource`` of ``_run_pipeline`` proving each mechanism is wired at
the right place. No mocking of the unit under test.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone

import pytest

from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    _SENTINEL_GUARDIAN_CRASH,
)
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.semantic_guardian import (
    Detection,
    recommend_tier_floor,
)


def _pipeline_src() -> str:
    return inspect.getsource(GovernedOrchestrator._run_pipeline)


# ---------------------------------------------------------------------------
# (a) Lock A — fail-CLOSED guardian
# ---------------------------------------------------------------------------


def test_sentinel_is_hard_detection():
    """The crash sentinel is a real, non-empty, HARD Detection."""
    assert isinstance(_SENTINEL_GUARDIAN_CRASH, Detection)
    assert _SENTINEL_GUARDIAN_CRASH.severity == "hard"
    assert _SENTINEL_GUARDIAN_CRASH.pattern == "guardian_crashed"
    assert _SENTINEL_GUARDIAN_CRASH.message  # non-empty human-readable message


def test_sentinel_finding_arms_approval_required_floor():
    """A guardian crash → one hard sentinel → recommend_tier_floor demands
    approval_required (the same path any real hard pattern takes). This proves
    the fail-CLOSED finding is not SAFE_AUTO."""
    floor = recommend_tier_floor([_SENTINEL_GUARDIAN_CRASH])
    assert floor == "approval_required"


def test_guardian_except_branch_fails_closed_in_source():
    """The guardian ``except`` no longer fails OPEN. It must set
    APPROVAL_REQUIRED + the sentinel finding, and the old fail-open
    ``SemanticGuardian skipped`` debug-swallow must be gone."""
    src = _pipeline_src()
    assert "SemanticGuardian skipped" not in src, (
        "fail-OPEN debug-swallow still present — guardian crash would "
        "auto-apply SAFE_AUTO"
    )
    assert "risk_tier = RiskTier.APPROVAL_REQUIRED" in src
    assert "_guardian_findings = [_SENTINEL_GUARDIAN_CRASH]" in src
    assert "FAILING" in src  # the fail-closed warning marker


# ---------------------------------------------------------------------------
# (b) S2 — noop + in-loop write guard
# ---------------------------------------------------------------------------


def _make_ctx(phase: OperationPhase = OperationPhase.GENERATE) -> OperationContext:
    now = datetime.now(tz=timezone.utc)
    return OperationContext(
        op_id="op-task7-test",
        created_at=now,
        phase=phase,
        phase_entered_at=now,
        context_hash="h0",
        previous_hash="",
        target_files=("a.py",),
    )


def test_advance_to_cancelled_with_inloop_reason_is_terminal():
    """The exact FSM mechanism the noop guard uses: advancing to CANCELLED with
    the in-loop reason code yields a terminal context carrying that code."""
    ctx = _make_ctx()
    cancelled = ctx.advance(
        OperationPhase.CANCELLED,
        terminal_reason_code="noop_inloop_write_guard",
    )
    assert cancelled.phase is OperationPhase.CANCELLED
    assert cancelled.terminal_reason_code == "noop_inloop_write_guard"


def test_noop_inloop_guard_wired_in_source():
    """The is_noop block CANCELS on non-empty venom_edit_history BEFORE the
    legacy noop COMPLETE handling, with a FAILED ledger record."""
    src = _pipeline_src()
    assert "if generation.is_noop:" in src
    # The guard reads venom_edit_history and cancels with the dedicated code.
    noop_idx = src.index("if generation.is_noop:")
    after = src[noop_idx:]
    assert 'venom_edit_history' in after
    assert 'terminal_reason_code="noop_inloop_write_guard"' in after
    assert 'OperationState.FAILED' in after
    # The cancel-return must precede the legacy read-only/COMPLETE handling so
    # an in-loop-write noop never reaches the silent COMPLETE fast-path.
    guard_pos = after.index('noop_inloop_write_guard')
    complete_pos = after.index("OperationPhase.COMPLETE")
    assert guard_pos < complete_pos


# ---------------------------------------------------------------------------
# (b2) S2 — guardian git-HEAD baseline
# ---------------------------------------------------------------------------


def test_guardian_git_head_baseline_wired_in_source():
    """For venom-edited paths the guardian baselines _old from git show
    HEAD:<path> (so it compares original→candidate, not post-write→candidate),
    fail-soft to empty on git error."""
    src = _pipeline_src()
    assert 'git' in src and 'show' in src
    assert 'f"HEAD:{_rel}"' in src
    assert "venom_edit_history" in src
    # The git-baseline path is gated on the path being in the venom edit set.
    assert "_venom_paths" in src


# ---------------------------------------------------------------------------
# (c) C2 — asyncio.shield the apply
# ---------------------------------------------------------------------------


def test_both_apply_sites_are_shield_wrapped_in_source():
    """Both APPLY call sites (multi-file + single-file) are asyncio.shield-
    wrapped."""
    src = _pipeline_src()
    assert src.count("asyncio.shield") >= 2
    assert "asyncio.shield(\n                            self._apply_multi_file_candidate(" in src
    assert "asyncio.shield(\n                            self._stack.change_engine.execute(" in src


def test_shield_completes_write_but_propagates_cancel():
    """Behavioral proof of the property the C2 edit relies on: when the outer
    task is cancelled while awaiting a shielded coroutine, (1) the inner
    coroutine still runs to completion (atomic write + ledger), and (2)
    CancelledError still propagates to the outer awaiter so the stop is
    honored."""

    async def _scenario():
        committed: dict = {}

        async def _apply_and_commit():
            # Simulate the apply: file-write then APPLIED-ledger commit. A yield
            # point in the middle is where an unshielded cancel would tear it.
            committed["write"] = True
            await asyncio.sleep(0.05)
            committed["ledger"] = True
            return "applied"

        async def _op_task():
            # Mirrors the orchestrator: await asyncio.shield(apply()).
            return await asyncio.shield(_apply_and_commit())

        task = asyncio.ensure_future(_op_task())
        await asyncio.sleep(0.01)  # let it enter the shielded apply
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task  # (2) stop is honored — cancel propagates
        # (1) the shielded apply still finished atomically despite the cancel.
        await asyncio.sleep(0.1)
        return committed

    committed = asyncio.run(_scenario())
    assert committed.get("write") is True
    assert committed.get("ledger") is True, (
        "ledger commit was torn by cancellation — shield did not protect the "
        "write+ledger atomicity"
    )

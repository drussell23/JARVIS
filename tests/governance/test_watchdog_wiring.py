"""T3: Convergence watchdog wired into _decompose_block_or_legacy funnel.

Tests that after a stalled decompose lineage (2 consecutive passes where
the largest child >= 95% of parent), the watchdog:
  - replaces _all_subs with ONE fitting sub-goal (payload <= compression_target)
  - fires emit_sovereign_yield

Also tests the reducible-pass (no intervention) and the watchdog-disabled
(byte-identical) paths.

These are structural assertions against the LIVE _decompose_block_or_legacy
funnel (orchestrator.py, defined at ~line 2073) -- the same funnel used
by the egress-overweight re-chunk path and the Advisor-BLOCK path.  The
funnel is exercised via monkeypatching orchestrator module seams so the
test never instantiates the full governed stack.
"""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import orchestrator as orch_mod
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance import recursion_dedup as dedup
from backend.core.ouroboros.governance.operation_advisor import (
    Advisory,
    AdvisoryDecision,
)
from backend.core.ouroboros.governance.goal_decomposition_planner import (
    SubGoal,
    SubGoalKind,
)
from backend.core.ouroboros.governance import convergence_watchdog as cw_mod


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

@dataclass
class _FakeStack:
    governed_loop_service: Any = None


@dataclass
class _FakeGLS:
    _intake_router: Any = None


def _make_orch() -> GovernedOrchestrator:
    """Build a GovernedOrchestrator without running its heavy __init__."""
    o = object.__new__(GovernedOrchestrator)
    o._stack = _FakeStack(governed_loop_service=_FakeGLS(_intake_router=None))
    return o


def _make_ctx(
    op_id: str = "op-watchdog-t3",
    description: str = "Refactor the entire monolith",
    target_files: Tuple[str, ...] = ("backend/core/ouroboros/governance/orchestrator.py",),
) -> OperationContext:
    return OperationContext.create(
        op_id=op_id,
        target_files=target_files,
        description=description,
    )


def _block_advisory() -> Advisory:
    return Advisory(
        decision=AdvisoryDecision.BLOCK,
        reasons=["blast_radius too high"],
        blast_radius=99,
        test_coverage=1.0,
        chronic_entropy=0.0,
        risk_score=0.9,
    )


def _make_sub(
    sub_id: str,
    parent_id: str,
    description: str,
    depends_on: Tuple[str, ...] = (),
) -> SubGoal:
    return SubGoal(
        sub_goal_id=sub_id,
        parent_goal_id=parent_id,
        title=sub_id,
        description=description,
        kind=SubGoalKind.ATOMIC,
        target_files=(),
        depends_on_sub_ids=depends_on,
        estimated_complexity="moderate",
        boundary_crossed=False,
        scoped_symbols=(),
    )


def _run_seam(
    orch: GovernedOrchestrator,
    ctx: OperationContext,
    advisory: Advisory,
    compression_target: Optional[int] = None,
):
    return asyncio.get_event_loop().run_until_complete(
        orch._decompose_block_or_legacy(ctx, advisory, compression_target=compression_target)
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_env_and_ledger(monkeypatch):
    """Enable chunking + watchdog; reset process-global singletons per test."""
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CONVERGENCE_WATCHDOG_ENABLED", "true")
    # Reset ledger singleton so no cross-test dedup contamination.
    dedup._ATTEMPT_LEDGER_SINGLETON = None  # type: ignore[attr-defined]
    # Reset watchdog tracker singleton so lineage history is clean.
    cw_mod._REDUCTION_TRACKER_SINGLETON = None  # type: ignore[attr-defined]
    yield
    dedup._ATTEMPT_LEDGER_SINGLETON = None  # type: ignore[attr-defined]
    cw_mod._REDUCTION_TRACKER_SINGLETON = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helper: common fake advance_orchestration (returns emitted_count=1)
# ---------------------------------------------------------------------------

async def _fake_advance_emit_one(plan, *, router=None, **kw):
    from types import SimpleNamespace
    return SimpleNamespace(emitted_count=1)


# ---------------------------------------------------------------------------
# Test 1: stalled lineage -> ONE shed sub-goal + emit_sovereign_yield fires
# ---------------------------------------------------------------------------

def test_stalled_lineage_yields_one_shed_sub_goal_and_emits(monkeypatch):
    """After 2 passes where largest child >= 95% of parent, watchdog intervenes:
    _all_subs becomes exactly ONE sub-goal whose description length <=
    compression_target, and emit_sovereign_yield is called.

    Exercises the LIVE _decompose_block_or_legacy funnel in orchestrator.py.
    """
    compression_target = 500
    parent_desc = "x" * 2000  # heavy parent description

    ctx = _make_ctx(description=parent_desc)
    op_id = ctx.op_id

    # A heavy sub-goal: description is 1900 chars (95% of parent 2000 chars).
    heavy_sub = _make_sub("sub-1", op_id, "y" * 1900)
    stall_subs: List[SubGoal] = [heavy_sub]

    emit_calls: List[dict] = []

    # Monkeypatch decompose_for_block to return stall_subs every call.
    monkeypatch.setattr(orch_mod, "decompose_for_block", lambda *a, **kw: tuple(stall_subs))

    # Monkeypatch estimate_subgoal_payload_chars to return len(description).
    monkeypatch.setattr(
        orch_mod,
        "estimate_subgoal_payload_chars",
        lambda s: len(str(getattr(s, "description", "") or "")),
    )

    # Monkeypatch shed_to_fit to truncate to target_chars.
    def _fake_shed(source: str, target_chars: int):
        return source[:target_chars], "tier3"

    monkeypatch.setattr(orch_mod, "shed_to_fit", _fake_shed)

    # Monkeypatch emit_sovereign_yield to capture calls.
    def _fake_emit(op_id, *, lineage_id, ratio, consecutive_stalls, parent_chars, child_chars, tier):
        emit_calls.append(dict(
            op_id=op_id, lineage_id=lineage_id, ratio=ratio,
            consecutive_stalls=consecutive_stalls,
            parent_chars=parent_chars, child_chars=child_chars, tier=tier,
        ))

    monkeypatch.setattr(orch_mod, "emit_sovereign_yield", _fake_emit)

    # advance_orchestration always emits 1 so we get "decomposed" terminal.
    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance_emit_one)

    orch = _make_orch()

    # Pass 1: stall #1 (ratio 0.95) -- watchdog records but threshold is 2.
    _run_seam(orch, ctx, _block_advisory(), compression_target=compression_target)
    # After pass 1 the tracker has 1 stall -- not yet tripped.
    # emit_sovereign_yield must NOT have fired yet.
    assert len(emit_calls) == 0, "emit should NOT fire on the first stall pass"

    # Reset dedup ledger so the second call isn't treated as duplicate.
    dedup._ATTEMPT_LEDGER_SINGLETON = None  # type: ignore[attr-defined]

    # Pass 2: stall #2 (ratio still 0.95) -- watchdog NOW trips.
    captured_subs: List[Tuple[SubGoal, ...]] = []

    async def _fake_advance_capture(plan, *, router=None, **kw):
        captured_subs.append(plan.sub_goals)
        from types import SimpleNamespace
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance_capture)

    _run_seam(orch, ctx, _block_advisory(), compression_target=compression_target)

    # Watchdog MUST have fired emit_sovereign_yield.
    assert len(emit_calls) == 1, f"emit_sovereign_yield must fire once on stall; got {emit_calls}"

    # The emitted plan must carry exactly ONE sub-goal.
    assert len(captured_subs) == 1, "advance_orchestration must be called once"
    subs_on_stall = captured_subs[0]
    assert len(subs_on_stall) == 1, (
        f"stall -> ONE fitting sub-goal; got {len(subs_on_stall)}"
    )

    # That one sub-goal's description must fit within compression_target.
    shed_desc = subs_on_stall[0].description
    assert len(shed_desc) <= compression_target, (
        f"shed description len {len(shed_desc)} must be <= compression_target {compression_target}"
    )


# ---------------------------------------------------------------------------
# Test 2: reducible pass -> _all_subs unchanged, no emit
# ---------------------------------------------------------------------------

def test_reducible_pass_does_not_intervene(monkeypatch):
    """A decompose pass where largest child < 95% of parent should not trigger
    the watchdog at all -- _all_subs passes through unchanged, no yield emitted.
    """
    compression_target = 500
    parent_desc = "x" * 2000

    ctx = _make_ctx(description=parent_desc)
    op_id = ctx.op_id

    # A SMALL sub-goal: description is only 400 chars (~20% of parent).
    small_sub = _make_sub("sub-small", op_id, "z" * 400)

    captured_subs: List[Tuple[SubGoal, ...]] = []
    emit_calls: List[dict] = []

    monkeypatch.setattr(orch_mod, "decompose_for_block", lambda *a, **kw: (small_sub,))
    monkeypatch.setattr(
        orch_mod,
        "estimate_subgoal_payload_chars",
        lambda s: len(str(getattr(s, "description", "") or "")),
    )

    def _fake_shed(source: str, target_chars: int):
        return source[:target_chars], "tier2"  # should never be called

    monkeypatch.setattr(orch_mod, "shed_to_fit", _fake_shed)

    def _fake_emit(**kw):
        emit_calls.append(kw)

    monkeypatch.setattr(orch_mod, "emit_sovereign_yield", _fake_emit)

    async def _fake_advance_capture(plan, *, router=None, **kw):
        captured_subs.append(plan.sub_goals)
        from types import SimpleNamespace
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance_capture)

    _run_seam(_make_orch(), ctx, _block_advisory(), compression_target=compression_target)

    # No yield should have been emitted.
    assert emit_calls == [], f"emit_sovereign_yield must NOT fire on reducible pass; got {emit_calls}"

    # The sub-goals passed to advance_orchestration should still include our sub.
    assert len(captured_subs) == 1
    assert any(s.sub_goal_id == "sub-small" for s in captured_subs[0]), (
        "original sub-small sub-goal must pass through unchanged"
    )


# ---------------------------------------------------------------------------
# Test 3: watchdog_enabled() == False -> byte-identical (no watchdog logic)
# ---------------------------------------------------------------------------

def test_watchdog_disabled_is_byte_identical(monkeypatch):
    """When JARVIS_CONVERGENCE_WATCHDOG_ENABLED=false, the seam must behave
    exactly as before T3 -- _all_subs comes from decompose_for_block unchanged,
    emit_sovereign_yield is never called, even after multiple stall passes.
    """
    monkeypatch.setenv("JARVIS_CONVERGENCE_WATCHDOG_ENABLED", "false")

    compression_target = 500
    parent_desc = "x" * 2000

    ctx = _make_ctx(description=parent_desc)
    op_id = ctx.op_id

    heavy_sub1 = _make_sub("sub-h1", op_id, "y" * 1900)
    heavy_sub2 = _make_sub("sub-h2", op_id, "y" * 1850)
    stall_subs = (heavy_sub1, heavy_sub2)

    emit_calls: List[dict] = []
    captured_subs: List[Tuple[SubGoal, ...]] = []

    monkeypatch.setattr(orch_mod, "decompose_for_block", lambda *a, **kw: stall_subs)
    monkeypatch.setattr(
        orch_mod,
        "estimate_subgoal_payload_chars",
        lambda s: len(str(getattr(s, "description", "") or "")),
    )
    monkeypatch.setattr(orch_mod, "shed_to_fit", lambda src, tgt: (src[:tgt], "tier3"))

    def _fake_emit(**kw):
        emit_calls.append(kw)

    monkeypatch.setattr(orch_mod, "emit_sovereign_yield", _fake_emit)

    async def _fake_advance_capture(plan, *, router=None, **kw):
        captured_subs.append(plan.sub_goals)
        from types import SimpleNamespace
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance_capture)

    orch = _make_orch()

    # Simulate two "stall" passes. Watchdog is OFF so nothing should change.
    _run_seam(orch, ctx, _block_advisory(), compression_target=compression_target)
    dedup._ATTEMPT_LEDGER_SINGLETON = None  # type: ignore[attr-defined]
    _run_seam(orch, ctx, _block_advisory(), compression_target=compression_target)

    # emit_sovereign_yield must NEVER fire when watchdog is disabled.
    assert emit_calls == [], "emit_sovereign_yield must NOT fire when watchdog disabled"

    # Both calls should have passed through all original sub-goals unchanged.
    for subs in captured_subs:
        assert len(subs) == len(stall_subs), (
            f"sub-goal count must be unchanged when disabled; got {len(subs)}, want {len(stall_subs)}"
        )


# ---------------------------------------------------------------------------
# Test 4: watchdog_enabled() False + no compression_target -> byte-identical
# ---------------------------------------------------------------------------

def test_no_compression_target_skips_watchdog(monkeypatch):
    """compression_target=None means the egress re-chunk path is NOT active.
    The watchdog guard requires compression_target is not None, so it must
    be completely skipped (record_pass never called).
    """
    monkeypatch.setenv("JARVIS_CONVERGENCE_WATCHDOG_ENABLED", "true")

    parent_desc = "x" * 2000
    ctx = _make_ctx(description=parent_desc)
    op_id = ctx.op_id

    heavy_sub = _make_sub("sub-h", op_id, "y" * 1900)
    emit_calls: List[dict] = []
    record_pass_calls: List[Any] = []

    monkeypatch.setattr(orch_mod, "decompose_for_block", lambda *a, **kw: (heavy_sub,))
    monkeypatch.setattr(
        orch_mod,
        "estimate_subgoal_payload_chars",
        lambda s: len(str(getattr(s, "description", "") or "")),
    )
    monkeypatch.setattr(orch_mod, "shed_to_fit", lambda src, tgt: (src[:tgt], "tier3"))

    def _fake_emit(**kw):
        emit_calls.append(kw)

    monkeypatch.setattr(orch_mod, "emit_sovereign_yield", _fake_emit)

    # Monkeypatch get_reduction_tracker to detect if record_pass is called.
    class _SpyTracker:
        def record_pass(self, lineage_id, parent_chars, max_child_chars):
            record_pass_calls.append((lineage_id, parent_chars, max_child_chars))
            # Return stalled=True to force intervention IF it were reached.
            return cw_mod.WatchdogVerdict(stalled=True, ratio=0.99, consecutive_stalls=2, passes=2)

    monkeypatch.setattr(orch_mod, "get_reduction_tracker", lambda: _SpyTracker())

    async def _fake_advance(plan, *, router=None, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    # Call WITHOUT compression_target (None = legacy path).
    _run_seam(_make_orch(), ctx, _block_advisory(), compression_target=None)

    assert record_pass_calls == [], (
        "record_pass must NOT be called when compression_target is None"
    )
    assert emit_calls == [], (
        "emit_sovereign_yield must NOT fire when compression_target is None"
    )


# ---------------------------------------------------------------------------
# Test 5: fail-soft -- watchdog error -> legacy slice path (no crash)
# ---------------------------------------------------------------------------

def test_watchdog_error_is_failsoft(monkeypatch):
    """An exception inside the watchdog block must be silently swallowed so
    the outer chunking seam continues to its decomposed terminal.  The op
    must NEVER be lost due to watchdog internals.
    """
    compression_target = 500
    parent_desc = "x" * 2000
    ctx = _make_ctx(description=parent_desc)

    monkeypatch.setattr(orch_mod, "decompose_for_block", lambda *a, **kw: ())

    # Make estimate_subgoal_payload_chars explode.
    def _boom(s):
        raise RuntimeError("intentional test bomb")

    monkeypatch.setattr(orch_mod, "estimate_subgoal_payload_chars", _boom)
    monkeypatch.setattr(orch_mod, "shed_to_fit", lambda src, tgt: (src[:tgt], "tier3"))
    monkeypatch.setattr(orch_mod, "emit_sovereign_yield", lambda *a, **kw: None)

    async def _fake_advance(plan, *, router=None, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    # Must not raise -- fail-soft inside the try/except.
    out = _run_seam(_make_orch(), ctx, _block_advisory(), compression_target=compression_target)
    # Either decomposed or advisor_blocked -- but no exception.
    assert out.phase == OperationPhase.CANCELLED

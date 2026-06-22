"""Sovereign Ledger-Watchdog Composition.

Proves the Autonomous Convergence Watchdog actually ENGAGES on the live
recursive chain instead of being pre-empted by the de-dup ledger. Three
structural changes are validated here:

  Part 1 -- deep-payload shed helper (shed_block_goal_to_fit): reads the FULL
            target-file source, sheds it, and inlines the result with
            scoped_symbols CLEARED so the egress ruler measures <= target.
  Part 2 -- invariant lineage (subgoal_hash(target_files, ()) is stable across
            re-injection -> the tracker accumulates stalls).
  Part 3 -- FUNNEL INVERSION: a DUPLICATE GOAL on the egress path gives the
            watchdog a shed-and-CONTINUE chance (decomposed) before the legacy
            advisor_blocked hard-fail; a FIXPOINT (shed cannot reduce further)
            yields to advisor_blocked -- the de-dup ledger is the final
            mathematical backstop, so the loop is BOUNDED.

TERMINATION PROOF: tests below prove (a) invariant lineage accumulates stalls,
(b) one self-heal hop emits a sub-goal <= target, (c) a fixpoint lineage falls
to advisor_blocked (at most one self-heal hop per lineage). Together: the loop
cannot run forever.

Fakes only: monkeypatched advance_orchestration, fake router/stack. No live
advisor / network is touched.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from backend.core.ouroboros.governance import orchestrator as orch_mod
from backend.core.ouroboros.governance import convergence_watchdog as cw_mod
from backend.core.ouroboros.governance import recursion_dedup as dedup
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.goal_decomposition_planner import (
    SubGoalKind,
    estimate_subgoal_payload_chars,
    shed_block_goal_to_fit,
)
from backend.core.ouroboros.governance.recursion_dedup import subgoal_hash
from backend.core.ouroboros.governance.operation_advisor import (
    Advisory,
    AdvisoryDecision,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class _FakeStack:
    governed_loop_service: Any = None


@dataclass
class _FakeGLS:
    _intake_router: Any = None


class _FakeRouter:
    def __init__(self) -> None:
        self.ingested: List[Any] = []


def _make_orch(router: Any = None) -> GovernedOrchestrator:
    o = object.__new__(GovernedOrchestrator)
    o._stack = _FakeStack(governed_loop_service=_FakeGLS(_intake_router=router))
    return o


def _make_ctx(
    *,
    op_id: str = "op-comp-1",
    description: str = "Implement the widget feature",
    target_files: Tuple[str, ...] = ("backend/widget.py",),
) -> OperationContext:
    return OperationContext.create(
        op_id=op_id,
        target_files=target_files,
        description=description,
    )


def _block_advisory(*, coverage: float = 1.0, reasons=None) -> Advisory:
    return Advisory(
        decision=AdvisoryDecision.BLOCK,
        reasons=reasons if reasons is not None else ["blast_radius too high"],
        blast_radius=99,
        test_coverage=coverage,
        chronic_entropy=0.0,
        risk_score=0.9,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# Heavy source the shedder must reduce (tier3 truncation for tight budgets).
_HEAVY = (
    '"""Module docstring padding padding padding padding padding."""\n'
    "import os\n"
    "def a(x):\n"
    "    return x * 2 + sum(range(x)) + len(str(x)) + 1234567890\n"
    "def b(y):\n"
    "    return [i for i in range(y) if i % 2 == 0 and i > 3 and i < 99]\n"
) * 40


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    """Reset both singletons + chunking/watchdog env per test."""
    dedup._ATTEMPT_LEDGER_SINGLETON = None  # type: ignore[attr-defined]
    cw_mod._REDUCTION_TRACKER_SINGLETON = None  # type: ignore[attr-defined]
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CONVERGENCE_WATCHDOG_ENABLED", raising=False)
    yield
    dedup._ATTEMPT_LEDGER_SINGLETON = None  # type: ignore[attr-defined]
    cw_mod._REDUCTION_TRACKER_SINGLETON = None  # type: ignore[attr-defined]


# ===========================================================================
# Test 1 -- INVARIANT LINEAGE: stable across differing op descriptions.
# ===========================================================================

def test_invariant_lineage_is_stable_across_reinjection():
    """subgoal_hash(target_files, ()) is the SAME for the same files even when
    the per-op description changes -> the tracker accumulates stalls across
    re-injections. The OLD lineage (ctx.op_id) was per-op and never tripped.
    """
    tf = ("backend/widget.py", "backend/other.py")
    lin_a = subgoal_hash(tf, ())
    lin_b = subgoal_hash(tf, ())  # different "op", same files
    assert lin_a == lin_b, "lineage must be invariant across re-injection"
    # And distinct files give a distinct lineage.
    assert subgoal_hash(("x.py",), ()) != lin_a

    # The tracker accumulates stalls under the invariant lineage.
    tracker = cw_mod.get_reduction_tracker()
    v1 = tracker.record_pass(lin_a, 1000, 990)
    assert v1.consecutive_stalls == 1 and v1.stalled is False
    v2 = tracker.record_pass(lin_b, 1000, 990)  # SAME lineage, "2nd op"
    assert v2.consecutive_stalls == 2 and v2.stalled is True


# ===========================================================================
# Test 2 -- FUNNEL INVERSION: duplicate + stalled -> decomposed (NOT blocked).
# ===========================================================================

def test_funnel_inversion_duplicate_stalled_self_heals(monkeypatch, tmp_path):
    """A DUPLICATE goal on the egress path (compression_target set, watchdog
    enabled, lineage already stalled) -> _watchdog_self_heal returns a
    `decomposed` ctx and emits ONE sub-goal whose estimated payload <= target.
    """
    # Write a real heavy target file so the deep shed measures real source.
    target_file = tmp_path / "heavy.py"
    target_file.write_text(_HEAVY)
    target_files = (str(target_file),)
    compression_target = 400

    captured: dict = {}

    async def _fake_advance(plan, *, router=None, **kw):
        captured["plan"] = plan
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    # Pre-stall the lineage so the very next self-heal call sees stalled=True
    # (the funnel-inversion path needs an already-stalled lineage).
    lineage = subgoal_hash(target_files, ())
    tracker = cw_mod.get_reduction_tracker()
    tracker.record_pass(lineage, 1000, 1000)  # stall #1 (full stall, ratio 1.0)

    # Pre-mark the GOAL hash so it is a DUPLICATE -> funnel inversion fires.
    ctx = _make_ctx(description="rechunk me", target_files=target_files)
    h = subgoal_hash(target_files, ctx.description or "")
    dedup.get_attempt_ledger().mark(h)

    orch = _make_orch(router=_FakeRouter())
    out = _run(
        orch._decompose_block_or_legacy(
            ctx, _block_advisory(), compression_target=compression_target,
        )
    )

    assert out.phase == OperationPhase.CANCELLED
    assert out.terminal_reason_code == "decomposed", (
        "duplicate + stalled must self-heal (decomposed), NOT advisor_blocked"
    )
    plan = captured["plan"]
    assert plan is not None
    assert plan.diagnostic == "watchdog_self_heal_reinject"
    subs = tuple(plan.sub_goals)
    assert len(subs) == 1, "self-heal emits exactly ONE fitting sub-goal"
    sub = subs[0]
    # Deep-payload invariant: the shed sub-goal fits the egress ceiling.
    assert estimate_subgoal_payload_chars(sub) <= compression_target, (
        "self-healed sub-goal MUST fit compression_target so the loop breaks"
    )


# ===========================================================================
# Test 3 -- FIXPOINT TERMINATION: unshrinkable shed -> advisor_blocked.
# ===========================================================================

def test_fixpoint_shed_already_marked_falls_to_advisor_blocked(
    monkeypatch, tmp_path,
):
    """When the shed result's hash is ALREADY a duplicate (the shed cannot
    reduce further -> fixpoint), _watchdog_self_heal returns None and the caller
    falls to advisor_blocked. BOUNDED: at most one self-heal hop per lineage.
    """
    target_file = tmp_path / "small.py"
    target_file.write_text("def f():\n    return 1\n")  # already tiny
    target_files = (str(target_file),)
    compression_target = 50_000  # huge -> shed is a no-op (tier 'none')

    called: list = []

    async def _fake_advance(plan, *, router=None, **kw):
        called.append(plan)
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    # Stall the lineage.
    lineage = subgoal_hash(target_files, ())
    cw_mod.get_reduction_tracker().record_pass(lineage, 100, 100)

    ctx = _make_ctx(description="fixpoint goal", target_files=target_files)
    ledger = dedup.get_attempt_ledger()
    # Mark the GOAL hash (duplicate -> inversion fires).
    ledger.mark(subgoal_hash(target_files, ctx.description or ""))
    # Pre-mark the SHED hash so the fixpoint guard trips.
    _sub, _ = shed_block_goal_to_fit(
        target_files, ctx.description, compression_target, ctx.op_id,
    )
    assert _sub is not None
    ledger.mark(subgoal_hash(_sub.target_files, _sub.description))

    orch = _make_orch(router=_FakeRouter())
    out = _run(
        orch._decompose_block_or_legacy(
            ctx, _block_advisory(), compression_target=compression_target,
        )
    )

    assert out.terminal_reason_code == "advisor_blocked", (
        "fixpoint (unshrinkable shed) MUST yield to the de-dup backstop"
    )
    assert called == [], "fixpoint must NOT re-inject -> loop terminates"


# ===========================================================================
# Test 4 -- PLAIN BLOCK path (compression_target=None) -> byte-identical.
# ===========================================================================

def test_plain_block_path_never_invokes_self_heal(monkeypatch):
    """compression_target=None (plain BLOCK, not the egress re-chunk path) ->
    the funnel inversion is NEVER entered; a duplicate falls straight through to
    legacy advisor_blocked exactly as before.
    """
    self_heal_calls: list = []

    async def _spy_self_heal(*a, **k):
        self_heal_calls.append((a, k))
        return None

    monkeypatch.setattr(
        GovernedOrchestrator, "_watchdog_self_heal", _spy_self_heal,
    )

    called: list = []

    async def _fake_advance(plan, *, router=None, **kw):
        called.append(plan)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    ctx = _make_ctx()
    # Duplicate goal so legacy path would otherwise short-circuit.
    dedup.get_attempt_ledger().mark(
        subgoal_hash(tuple(ctx.target_files), ctx.description or "")
    )
    orch = _make_orch(router=_FakeRouter())
    out = _run(
        orch._decompose_block_or_legacy(ctx, _block_advisory())  # no target
    )

    assert out.terminal_reason_code == "advisor_blocked"
    assert self_heal_calls == [], (
        "plain BLOCK (compression_target=None) must NOT invoke self-heal"
    )


# ===========================================================================
# Test 5 -- MASTER OFF (watchdog disabled) -> byte-identical, no self-heal.
# ===========================================================================

def test_master_off_never_invokes_self_heal(monkeypatch):
    """JARVIS_CONVERGENCE_WATCHDOG_ENABLED=false -> the funnel inversion guard
    is False; a duplicate on the egress path falls straight to advisor_blocked.
    """
    monkeypatch.setenv("JARVIS_CONVERGENCE_WATCHDOG_ENABLED", "false")

    self_heal_calls: list = []

    async def _spy_self_heal(*a, **k):
        self_heal_calls.append((a, k))
        return None

    monkeypatch.setattr(
        GovernedOrchestrator, "_watchdog_self_heal", _spy_self_heal,
    )

    async def _fake_advance(plan, *, router=None, **kw):
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    ctx = _make_ctx()
    dedup.get_attempt_ledger().mark(
        subgoal_hash(tuple(ctx.target_files), ctx.description or "")
    )
    orch = _make_orch(router=_FakeRouter())
    out = _run(
        orch._decompose_block_or_legacy(
            ctx, _block_advisory(), compression_target=400,
        )
    )

    assert out.terminal_reason_code == "advisor_blocked"
    assert self_heal_calls == [], "master OFF must NOT invoke self-heal"


# ===========================================================================
# Test 6 -- DEEP-PAYLOAD shed: clears scoped_symbols + inlines source <= target.
# ===========================================================================

def test_deep_payload_shed_fits_target_and_clears_symbols(tmp_path):
    """shed_block_goal_to_fit reads the FULL file source, sheds it, inlines it
    into description, and CLEARS scoped_symbols -> estimate_subgoal_payload_chars
    measures <= target for a large target file. Fail-soft on a missing file.
    """
    target_file = tmp_path / "big.py"
    target_file.write_text(_HEAVY)
    target_files = (str(target_file),)
    compression_target = 300

    sub, tier = shed_block_goal_to_fit(
        target_files, "shed this goal", compression_target, "op-deep-1",
    )
    assert sub is not None
    assert sub.scoped_symbols == (), "scoped_symbols MUST be cleared (inline shed)"
    assert sub.kind == SubGoalKind.ATOMIC
    assert sub.target_files == target_files
    assert estimate_subgoal_payload_chars(sub) <= compression_target, (
        f"deep shed must fit target; got {estimate_subgoal_payload_chars(sub)}"
    )
    assert tier in ("none", "tier1", "tier2", "tier3")

    # Fail-soft: a non-existent file must NOT crash (reader returns "").
    sub2, tier2 = shed_block_goal_to_fit(
        ("/nonexistent/path/xyz.py",), "missing", 100, "op-deep-2",
    )
    # The reader yields "" for the missing file; the shed still returns a sub.
    assert sub2 is not None
    assert estimate_subgoal_payload_chars(sub2) <= 100


# ===========================================================================
# Test 7 -- BOUNDEDNESS: a second arrival of an irreducible lineage terminates.
# ===========================================================================

def test_reinjected_shed_reaches_fixpoint_and_terminates(
    monkeypatch, tmp_path,
):
    """End-to-end boundedness via the REAL re-injection mechanism.

    In the live loop the self-healed sub-goal carries the shed text AS its new
    op description. So on the next stall the self-heal receives ``description ==
    prior shed text``. shed(shed_text + source) truncated to target converges to
    a FIXPOINT (once the shed text fills the budget, re-shedding it returns the
    same bytes). At the fixpoint the shed-hash repeats -> already marked -> the
    self-heal yields to advisor_blocked. This proves the chain TERMINATES: the
    self-heal hops are bounded by the number of distinct shed payloads, which is
    finite because each hop is a monotone non-growing AST reduction toward a
    fixpoint. Here we drive that re-injection loop directly and assert it stops.
    """
    target_file = tmp_path / "irr.py"
    target_file.write_text(_HEAVY)
    target_files = (str(target_file),)
    compression_target = 350

    advance_calls: list = []

    async def _fake_advance(plan, *, router=None, **kw):
        advance_calls.append(plan)
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    lineage = subgoal_hash(target_files, ())
    tracker = cw_mod.get_reduction_tracker()
    # Pre-stall the lineage so every arrival is treated as stalled.
    tracker.record_pass(lineage, 1000, 1000)

    ledger = dedup.get_attempt_ledger()
    orch = _make_orch(router=_FakeRouter())

    # Drive the re-injection loop: each iteration feeds the PRIOR shed text back
    # as the new op description (exactly what the multi-step emitter does live).
    description = "irreducible heavy goal"
    terminated = False
    for hop in range(25):  # hard cap: must terminate well before this
        ctx = _make_ctx(
            op_id=f"op-irr-{hop}",
            description=description,
            target_files=target_files,
        )
        # The arriving GOAL is a duplicate (it has been attempted) -> inversion.
        ledger.mark(subgoal_hash(target_files, description))
        out = _run(
            orch._decompose_block_or_legacy(
                ctx, _block_advisory(),
                compression_target=compression_target,
            )
        )
        if out.terminal_reason_code == "advisor_blocked":
            terminated = True
            break
        assert out.terminal_reason_code == "decomposed"
        # Re-injection: the emitted sub-goal's description becomes the next op's
        # description (the live multi-step emitter carries it forward).
        plan = advance_calls[-1]
        description = plan.sub_goals[0].description

    assert terminated, (
        "the re-injected shed chain MUST reach a fixpoint and terminate "
        "advisor_blocked -- the loop cannot run forever"
    )

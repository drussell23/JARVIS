"""
Task #98 spine — Universal phase-local sub-budgeting (CLASSIFY/ROUTE/CTX/PLAN).

v14-rev15 graduation soak proved Task #97 PlanGenerator phase-local
budget worked correctly, and immediately surfaced the "onion peel"
problem: CLASSIFY / ROUTE / CTX consumed ~316s of the op budget
BEFORE PLAN ever ran, leaving op_remaining=43.6s — below the
GENERATE reserve.

Task #98 universalizes Task #97's defense across every pre-GENERATE
phase via the new ``phase_budget`` module (single source of truth
for the math kernel; ``plan_generator.py`` delegates to it).

This spine pins:

  * Universal math kernel ``compute_phase_budget_s(op_remaining,
    phase_name)`` shape — decision-table parametrized per phase.
  * Per-phase fraction resolver reads ``JARVIS_PHASE_BUDGET_FRACTION_
    <NAME>`` with safe defaults summing to < 1.0 (GENERATE always
    gets a meaningful remainder).
  * ``dispatch_phase_with_budget`` correctly:
      - passes through on master-off (byte-identical legacy)
      - passes through on op_deadline=None (no behavior change
        without explicit deadline)
      - returns graceful skip when budget below floor
      - hard-cancels via asyncio.wait_for when runner exceeds budget
      - returns graceful skip with structured reason on timeout
  * Task #97 helpers remain importable + behaviorally identical
    (back-compat — its spine still green).
  * Orchestrator dispatch sites for CLASSIFY/ROUTE/CTX/PLAN are wrapped
    with dispatch_phase_with_budget (AST scan).
  * FlagRegistry seeds present.

No live network — fully deterministic via stubbed PhaseRunner +
monkeypatched env.
"""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest


_PHASE_BUDGET_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "phase_budget.py"
)
_ORCH_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "orchestrator.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_kernel():
    from backend.core.ouroboros.governance.phase_budget import (
        compute_phase_budget_s,
        resolve_phase_fraction,
        resolve_min_generate_reserve_s,
        resolve_phase_min_budget_s,
        PHASE_FRACTION_DEFAULTS,
    )
    return (
        compute_phase_budget_s,
        resolve_phase_fraction,
        resolve_min_generate_reserve_s,
        resolve_phase_min_budget_s,
        PHASE_FRACTION_DEFAULTS,
    )


# ---------------------------------------------------------------------------
# Math kernel — decision table per phase
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase,op_rem,expected", [
    # CLASSIFY @ default 0.05:
    #   300s × 0.05 = 15; 300 - 60 = 240; min = 15
    ("CLASSIFY", 300.0, 15.0),
    # ROUTE @ default 0.05: same shape as CLASSIFY
    ("ROUTE", 300.0, 15.0),
    # CONTEXT_EXPANSION @ default 0.20:
    #   300 × 0.20 = 60; min(60, 240) = 60
    ("CONTEXT_EXPANSION", 300.0, 60.0),
    # PLAN @ default 0.30:
    #   300 × 0.30 = 90; min(90, 240) = 90
    ("PLAN", 300.0, 90.0),
    # Unknown phase → 0.10 conservative default
    ("UNKNOWN_PHASE", 300.0, 30.0),
    # Reserve dominates when op_remaining is tight:
    #   PLAN @ 0.30, op_rem=70 → 70×0.30=21, 70-60=10, min=10
    ("PLAN", 70.0, 10.0),
    # All phases yield 0 when op_remaining <= reserve floor (60):
    ("CLASSIFY", 60.0, 0.0),
    ("PLAN", 60.0, 0.0),
    # Sum of defaults < 1.0 → GENERATE always has runway:
    # CLASSIFY+ROUTE+CTX+PLAN = 0.05+0.05+0.20+0.30 = 0.60 → 40% min for GEN
])
def test_kernel_decision_table(phase, op_rem, expected):
    fn, _, _, _, _ = _import_kernel()
    assert fn(op_rem, phase) == pytest.approx(expected, abs=0.01)


def test_kernel_defaults_sum_under_one():
    """Load-bearing invariant: CLASSIFY + ROUTE + CTX + PLAN default
    fractions MUST sum to < 1.0 so GENERATE has guaranteed runway."""
    _, _, _, _, defaults = _import_kernel()
    pre_gen_sum = sum([
        defaults["CLASSIFY"], defaults["ROUTE"],
        defaults["CONTEXT_EXPANSION"], defaults["PLAN"],
    ])
    assert pre_gen_sum < 1.0, (
        f"Pre-GENERATE phase fractions sum to {pre_gen_sum} ≥ 1.0 — "
        "GENERATE would get no runway in the limit!"
    )
    # Reasonable margin — at least 0.30 reserved for GENERATE
    assert pre_gen_sum <= 0.70, (
        f"Pre-GENERATE sum {pre_gen_sum} > 0.70 leaves less than 30% "
        "for GENERATE; raise the bar"
    )


def test_invariant_phase_budget_never_exceeds_op_remaining():
    fn, _, _, _, defaults = _import_kernel()
    for phase in defaults.keys():
        for op_rem in [0.0, 30.0, 60.0, 100.0, 500.0, 3600.0]:
            budget = fn(op_rem, phase)
            assert budget <= op_rem + 1e-9


# ---------------------------------------------------------------------------
# Resolver — invalid env fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase,env_val,expected", [
    ("CLASSIFY", "0.10", 0.10),
    ("ROUTE", "0.07", 0.07),
    ("CONTEXT_EXPANSION", "0.25", 0.25),
    ("PLAN", "0.40", 0.40),
    # Invalid → default
    ("CLASSIFY", "0.0", 0.05),     # 0.0 rejected
    ("CLASSIFY", "1.5", 0.05),     # out of range
    ("CLASSIFY", "abc", 0.05),     # garbage
    ("CLASSIFY", "", 0.05),        # unset
])
def test_phase_fraction_resolver(phase, env_val, expected, monkeypatch):
    env_key = f"JARVIS_PHASE_BUDGET_FRACTION_{phase}"
    if env_val:
        monkeypatch.setenv(env_key, env_val)
    else:
        monkeypatch.delenv(env_key, raising=False)
    _, fn, _, _, _ = _import_kernel()
    assert fn(phase) == pytest.approx(expected, abs=0.01)


def test_shared_min_generate_reserve_resolver(monkeypatch):
    """Universal reserve MUST share the same env knob as Task #97 —
    single source of truth, no parallel knobs."""
    _, _, fn, _, _ = _import_kernel()
    # Default
    monkeypatch.delenv("JARVIS_PLAN_PHASE_MIN_GENERATE_RESERVE_S", raising=False)
    assert fn() == 60.0
    # Operator override
    monkeypatch.setenv("JARVIS_PLAN_PHASE_MIN_GENERATE_RESERVE_S", "120.0")
    assert fn() == 120.0


# ---------------------------------------------------------------------------
# dispatch_phase_with_budget — behavioral
# ---------------------------------------------------------------------------


class _StubRunner:
    """Minimal PhaseRunner stub — invokes a coroutine that we control
    for testing wait_for behavior."""

    def __init__(self, *, delay_s: float = 0.0, return_phase=None):
        self.delay_s = delay_s
        self.return_phase = return_phase
        self.ran = False

    async def run(self, ctx):
        from backend.core.ouroboros.governance.phase_runner import PhaseResult
        self.ran = True
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return PhaseResult(
            next_ctx=ctx, next_phase=self.return_phase, status="ok",
        )


@pytest.mark.asyncio
async def test_dispatch_master_off_passes_through(monkeypatch):
    """Master switch false → byte-identical legacy: just await runner.run."""
    from backend.core.ouroboros.governance.phase_budget import (
        dispatch_phase_with_budget,
    )
    monkeypatch.setenv("JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED", "false")
    runner = _StubRunner()
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=300)
    result = await dispatch_phase_with_budget(
        runner, ctx=None,
        phase_name="CLASSIFY", op_deadline=deadline,
    )
    assert runner.ran
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_dispatch_no_deadline_passes_through(monkeypatch):
    """op_deadline=None → legacy pass-through (no invented budget)."""
    from backend.core.ouroboros.governance.phase_budget import (
        dispatch_phase_with_budget,
    )
    monkeypatch.setenv("JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED", "true")
    runner = _StubRunner()
    result = await dispatch_phase_with_budget(
        runner, ctx=None, phase_name="CLASSIFY", op_deadline=None,
    )
    assert runner.ran
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_dispatch_skips_when_budget_below_floor(monkeypatch):
    """When phase_budget < min_budget_s floor, runner MUST NOT run —
    graceful skip with structured reason."""
    from backend.core.ouroboros.governance.phase_budget import (
        dispatch_phase_with_budget,
    )
    monkeypatch.setenv("JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED", "true")
    runner = _StubRunner()
    # 50s remaining < 60s reserve → budget = max(0, 50-60) = 0 (clamp)
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=50)
    result = await dispatch_phase_with_budget(
        runner, ctx=None,
        phase_name="CLASSIFY", op_deadline=deadline,
    )
    assert not runner.ran, (
        "Runner MUST NOT run when phase_budget below floor — graceful "
        "skip preserves the op budget for downstream phases"
    )
    assert result.status == "skip"
    assert "phase_budget_exhausted" in (result.reason or "")
    assert "classify" in (result.reason or "")
    assert "insufficient_budget" in (result.reason or "")


@pytest.mark.asyncio
async def test_dispatch_hard_cancels_on_timeout(monkeypatch):
    """Slow runner exceeds phase budget → asyncio.wait_for fires hard
    cancel → graceful skip with structured reason."""
    from backend.core.ouroboros.governance.phase_budget import (
        dispatch_phase_with_budget,
    )
    monkeypatch.setenv("JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED", "true")
    # CTX @ 0.20 × 100s = 20s budget; +1s grace = 21s wait_for cap.
    # But we set fraction to 0.02 so budget = 100×0.02 = 2.0s, well
    # above 0.0 but below the typical floor.  Use a custom small
    # floor via env so the test is deterministic on a fast budget.
    monkeypatch.setenv("JARVIS_PLAN_PHASE_MIN_BUDGET_S", "0.5")
    monkeypatch.setenv("JARVIS_PHASE_BUDGET_FRACTION_CONTEXT_EXPANSION", "0.02")
    # 100s × 0.02 = 2s budget; +1s grace = 3s wait_for cap.
    # Runner sleeps 10s — wait_for kills it well before.
    runner = _StubRunner(delay_s=10.0)
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=100)
    t0 = asyncio.get_event_loop().time()
    result = await dispatch_phase_with_budget(
        runner, ctx=None,
        phase_name="CONTEXT_EXPANSION", op_deadline=deadline,
    )
    elapsed = asyncio.get_event_loop().time() - t0
    # Hard cancel should fire near 3s (budget + grace), well before 10s
    assert elapsed < 8.0, (
        f"Hard cancel should fire well before runner.delay=10s; "
        f"observed elapsed={elapsed:.2f}s"
    )
    assert result.status == "skip"
    assert "phase_budget_exhausted" in (result.reason or "")
    assert "hard_timeout_after" in (result.reason or "")
    assert "context_expansion" in (result.reason or "")


@pytest.mark.asyncio
async def test_dispatch_passes_through_on_normal_completion(monkeypatch):
    """Fast runner within budget → runs to completion, returns its
    own PhaseResult unchanged."""
    from backend.core.ouroboros.governance.phase_budget import (
        dispatch_phase_with_budget,
    )
    monkeypatch.setenv("JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED", "true")
    runner = _StubRunner(delay_s=0.05)
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=300)
    result = await dispatch_phase_with_budget(
        runner, ctx=None,
        phase_name="ROUTE", op_deadline=deadline,
    )
    assert runner.ran
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# AST pins — orchestrator dispatch sites wired
# ---------------------------------------------------------------------------


def test_ast_pin_orchestrator_imports_dispatch_helper():
    """Orchestrator MUST import dispatch_phase_with_budget."""
    src = _ORCH_SRC.read_text(encoding="utf-8")
    assert "from backend.core.ouroboros.governance.phase_budget import" in src
    assert "dispatch_phase_with_budget," in src


def test_ast_pin_orchestrator_wraps_classify_runner():
    src = _ORCH_SRC.read_text(encoding="utf-8")
    # Look for the wrapper call with CLASSIFY phase_name
    assert 'phase_name="CLASSIFY"' in src, (
        "Orchestrator MUST wrap CLASSIFYRunner dispatch with "
        "dispatch_phase_with_budget(phase_name='CLASSIFY', ...)"
    )


def test_ast_pin_orchestrator_wraps_route_runner():
    src = _ORCH_SRC.read_text(encoding="utf-8")
    assert 'phase_name="ROUTE"' in src


def test_ast_pin_orchestrator_wraps_ctx_runner():
    src = _ORCH_SRC.read_text(encoding="utf-8")
    assert 'phase_name="CONTEXT_EXPANSION"' in src


def test_ast_pin_orchestrator_wraps_plan_runner():
    src = _ORCH_SRC.read_text(encoding="utf-8")
    assert 'phase_name="PLAN"' in src


def test_ast_pin_dispatch_uses_pipeline_deadline():
    """All wraps MUST consult ctx.pipeline_deadline via getattr (defensive
    against pipeline_deadline=None on legacy code paths)."""
    src = _ORCH_SRC.read_text(encoding="utf-8")
    assert 'getattr(ctx, "pipeline_deadline", None)' in src, (
        "Wraps MUST pass op_deadline=getattr(ctx, 'pipeline_deadline', "
        "None) — defensive against ctx without pipeline_deadline"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_seed_master_switch_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED" in src
    idx = src.find("JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED")
    window = src[idx:idx + 1500]
    assert "default=True" in window or "default=true" in window
    assert "Category.SAFETY" in window
    assert "phase_budget.py" in window


@pytest.mark.parametrize("phase,default", [
    ("CLASSIFY", "0.05"),
    ("ROUTE", "0.05"),
    ("CONTEXT_EXPANSION", "0.20"),
    ("PLAN", "0.30"),
])
def test_seed_per_phase_fraction_present(phase, default):
    src = _SEED_SRC.read_text(encoding="utf-8")
    env_name = f"JARVIS_PHASE_BUDGET_FRACTION_{phase}"
    assert env_name in src
    idx = src.find(env_name)
    window = src[idx:idx + 1500]
    assert f"default={default}" in window
    assert "Category.TUNING" in window


# ---------------------------------------------------------------------------
# Back-compat — Task #97 spine still passes (smoke check)
# ---------------------------------------------------------------------------


def test_task97_resolvers_still_work(monkeypatch):
    """Task #97's plan_generator helpers MUST still be importable
    and return correct values after Task #98 refactor."""
    from backend.core.ouroboros.governance.plan_generator import (
        _resolve_plan_phase_fraction,
        _resolve_plan_phase_min_generate_reserve_s,
        _resolve_plan_phase_min_budget_s,
        _compute_plan_phase_budget_s,
    )
    monkeypatch.delenv("JARVIS_PLAN_PHASE_BUDGET_FRACTION", raising=False)
    monkeypatch.delenv("JARVIS_PHASE_BUDGET_FRACTION_PLAN", raising=False)
    assert _resolve_plan_phase_fraction() == pytest.approx(0.30, abs=0.01)
    assert _resolve_plan_phase_min_generate_reserve_s() == 60.0
    assert _resolve_plan_phase_min_budget_s() == 5.0
    # Math: op_rem=300 → fraction_bound=90, reserve_bound=240 → min=90
    assert _compute_plan_phase_budget_s(300.0) == pytest.approx(90.0, abs=0.01)


def test_task97_legacy_env_knob_still_honored(monkeypatch):
    """Operators with the legacy JARVIS_PLAN_PHASE_BUDGET_FRACTION env
    knob set MUST retain their override — back-compat invariant."""
    from backend.core.ouroboros.governance.plan_generator import (
        _resolve_plan_phase_fraction,
    )
    monkeypatch.setenv("JARVIS_PLAN_PHASE_BUDGET_FRACTION", "0.50")
    assert _resolve_plan_phase_fraction() == pytest.approx(0.50, abs=0.01)

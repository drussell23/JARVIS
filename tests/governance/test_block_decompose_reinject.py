"""Task B5 -- BLOCK -> decompose -> re-inject seam.

Tests the KEYSTONE seam at the OperationAdvisor BLOCK site: instead of
unconditionally terminating, the orchestrator decomposes the GOAL into
AST-symbol-scoped + test-first sub-goals and re-injects them (gated by the
B3 governor + B4 de-dup). The parent op terminates ``decomposed`` (not
``advisor_blocked``). Fail-soft to legacy ``advisor_blocked`` on ANY error /
chunking-off / governor-not-allowed / duplicate -- the op is NEVER lost.

Fakes only: a fake Advisory, monkeypatched ``advance_orchestration``, fake
router, fake stack. No live advisor / decomposition wiring is exercised.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List, Tuple

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

    async def ingest(self, env: Any) -> None:  # pragma: no cover - dry path
        self.ingested.append(env)


def _make_orch(router: Any = None) -> GovernedOrchestrator:
    """Build a GovernedOrchestrator without running its heavy __init__."""
    o = object.__new__(GovernedOrchestrator)
    o._stack = _FakeStack(governed_loop_service=_FakeGLS(_intake_router=router))
    return o


def _make_ctx(
    *,
    op_id: str = "op-b5-1",
    description: str = "Implement the widget feature",
    target_files: Tuple[str, ...] = ("backend/widget.py",),
    intake_evidence_json: str = "",
) -> OperationContext:
    return OperationContext.create(
        op_id=op_id,
        target_files=target_files,
        description=description,
        intake_evidence_json=intake_evidence_json,
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


@pytest.fixture(autouse=True)
def _fresh_ledger_and_env(monkeypatch):
    """Reset the process-global attempt ledger + chunking env per test."""
    # Reset the singleton so a prior test's marks don't leak.
    dedup._ATTEMPT_LEDGER_SINGLETON = None  # type: ignore[attr-defined]
    monkeypatch.delenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", raising=False)
    yield
    dedup._ATTEMPT_LEDGER_SINGLETON = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helper: drive the seam
# ---------------------------------------------------------------------------

def _run_seam(orch: GovernedOrchestrator, ctx: OperationContext, advisory: Advisory):
    return asyncio.get_event_loop().run_until_complete(
        orch._decompose_block_or_legacy(ctx, advisory)
    )


# ---------------------------------------------------------------------------
# OFF byte-identical (chunking disabled -> legacy)
# ---------------------------------------------------------------------------

def test_disabled_falls_through_to_legacy_advisor_blocked(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "false")
    called = []
    monkeypatch.setattr(
        orch_mod, "advance_orchestration",
        lambda *a, **k: called.append((a, k)),
    )
    orch = _make_orch(router=_FakeRouter())
    out = _run_seam(orch, _make_ctx(), _block_advisory())
    assert out.phase == OperationPhase.CANCELLED
    assert out.terminal_reason_code == "advisor_blocked"
    assert called == [], "advance_orchestration must NOT be called when disabled"


# ---------------------------------------------------------------------------
# ON + budget allowed + not dup -> decomposed + re-inject
# ---------------------------------------------------------------------------

def test_enabled_decomposes_and_reinjects(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "true")
    captured = {}

    async def _fake_advance(plan, *, router=None, **kw):
        captured["plan"] = plan
        captured["router"] = router
        # I1: the seam returns "decomposed" only when >=1 sub-goal was emitted.
        from types import SimpleNamespace
        return SimpleNamespace(emitted_count=1)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    router = _FakeRouter()
    orch = _make_orch(router=router)
    # zero coverage -> test-first sub-goal prepended
    out = _run_seam(orch, _make_ctx(), _block_advisory(coverage=0.0))

    assert out.phase == OperationPhase.CANCELLED
    assert out.terminal_reason_code == "decomposed"

    plan = captured["plan"]
    assert plan is not None
    assert captured["router"] is router
    subs = tuple(plan.sub_goals)
    assert len(subs) >= 2, "test-first sub-goal + at least one mutation sub-goal"
    # First sub-goal is the test-gen one: no dependencies.
    assert subs[0].depends_on_sub_ids == ()
    # A later (mutation) sub-goal depends on the test sub-goal.
    assert any(subs[0].sub_goal_id in s.depends_on_sub_ids for s in subs[1:])


def test_enabled_marks_ledger_after_reinject(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "true")

    async def _fake_advance(plan, *, router=None, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(emitted_count=1)  # I1: >=1 emit -> decomposed

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)
    orch = _make_orch(router=_FakeRouter())
    ctx = _make_ctx()
    h = dedup.subgoal_hash(tuple(ctx.target_files), ctx.description or "")
    ledger = dedup.get_attempt_ledger()
    assert not ledger.seen(h)
    _run_seam(orch, ctx, _block_advisory())
    assert ledger.seen(h), "hash must be marked after successful re-inject"


# ---------------------------------------------------------------------------
# Duplicate -> legacy (no re-inject, op not lost)
# ---------------------------------------------------------------------------

def test_duplicate_falls_through_to_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "true")
    called = []

    async def _fake_advance(plan, *, router=None, **kw):
        called.append(plan)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)
    orch = _make_orch(router=_FakeRouter())
    ctx = _make_ctx()
    # Pre-mark the hash so it is a duplicate.
    h = dedup.subgoal_hash(tuple(ctx.target_files), ctx.description or "")
    dedup.get_attempt_ledger().mark(h)

    out = _run_seam(orch, ctx, _block_advisory())
    assert out.terminal_reason_code == "advisor_blocked"
    assert called == [], "duplicate must NOT re-inject"


# ---------------------------------------------------------------------------
# Governor not-allowed -> legacy
# ---------------------------------------------------------------------------

def test_governor_not_allowed_falls_through_to_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "true")
    called = []

    async def _fake_advance(plan, *, router=None, **kw):
        called.append(plan)

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)

    # Force the governor to deny.
    from backend.core.ouroboros.governance.adaptive_recursion_governor import Budget

    def _deny(**kw):
        return Budget(allowed=False, max_fanout=1, reason="denied_test")

    monkeypatch.setattr(orch_mod, "recursion_budget", _deny)

    orch = _make_orch(router=_FakeRouter())
    out = _run_seam(orch, _make_ctx(), _block_advisory())
    assert out.terminal_reason_code == "advisor_blocked"
    assert called == [], "governor-denied must NOT re-inject"


# ---------------------------------------------------------------------------
# advance_orchestration raises -> legacy (op NEVER lost)
# ---------------------------------------------------------------------------

def test_reinject_raises_falls_through_to_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "true")

    async def _boom(plan, *, router=None, **kw):
        raise RuntimeError("router exploded")

    monkeypatch.setattr(orch_mod, "advance_orchestration", _boom)
    orch = _make_orch(router=_FakeRouter())
    out = _run_seam(orch, _make_ctx(), _block_advisory())
    assert out.phase == OperationPhase.CANCELLED
    assert out.terminal_reason_code == "advisor_blocked", "op must survive as legacy block"


# ---------------------------------------------------------------------------
# Structural: the seam consults decompose_for_block under the gate
# ---------------------------------------------------------------------------

def test_seam_calls_decompose_for_block_under_gate(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSIVE_CHUNKING_ENABLED", "true")
    seen = {}

    real = orch_mod.decompose_for_block

    def _spy(goal, *, zero_coverage, **kw):
        seen["zero_coverage"] = zero_coverage
        seen["goal_id"] = getattr(goal, "goal_id", None)
        return real(goal, zero_coverage=zero_coverage, **kw)

    monkeypatch.setattr(orch_mod, "decompose_for_block", _spy)

    async def _fake_advance(plan, *, router=None, **kw):
        return None

    monkeypatch.setattr(orch_mod, "advance_orchestration", _fake_advance)
    orch = _make_orch(router=_FakeRouter())
    ctx = _make_ctx(op_id="op-spy")
    _run_seam(orch, ctx, _block_advisory(coverage=0.0))
    assert seen.get("zero_coverage") is True
    assert seen.get("goal_id") == "op-spy"

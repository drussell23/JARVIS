"""Parity tests for :class:`ROUTERunner` (Wave 2 (5) Slice 3).

Verbatim transcription of orchestrator.py ROUTE block (~2048-2141/2257).
Tests pin the observable-trace parity that graduation of
``JARVIS_PHASE_RUNNER_ROUTE_EXTRACTED`` must preserve.

Parity contract:

1. Telemetry host-bind runs for remote routes (split-brain guard)
2. UrgencyRouter classify + ``provider_route`` stamp + CommProtocol
   emit_decision
3. CostGovernor.start called with route + complexity + is_read_only
4. Transition dispatch — ``CONTEXT_EXPANSION`` when enabled else ``PLAN``
5. PreActionNarrator.narrate fires BEFORE advance(CTX) on enabled path
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.phase_runners.route_runner import (
    ROUTERunner,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSerpent:
    def __init__(self) -> None:
        self.updates: List[str] = []

    def update_phase(self, phase: str) -> None:
        self.updates.append(phase)


class _FakeComm:
    def __init__(self) -> None:
        self.decisions: List[Dict[str, Any]] = []

    async def emit_decision(self, **kwargs) -> None:
        self.decisions.append(kwargs)


class _FakeCostGovernor:
    def __init__(self) -> None:
        self.starts: List[Dict[str, Any]] = []

    def start(self, **kwargs) -> None:
        self.starts.append(kwargs)


class _FakeNarrator:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    async def narrate_phase(self, phase: str, payload: Dict[str, Any]) -> None:
        self.calls.append((phase, payload))


class _FakeStack:
    def __init__(self, comm: _FakeComm) -> None:
        self.comm = comm


@dataclass
class _FakeConfig:
    project_root: Path
    context_expansion_enabled: bool = True


@dataclass
class _FakeOrchestrator:
    _stack: _FakeStack
    _config: _FakeConfig
    _cost_governor: _FakeCostGovernor
    _pre_action_narrator: Optional[_FakeNarrator] = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _route_ctx(tmp_path: Path) -> OperationContext:
    """Advance a fresh ctx to ROUTE phase."""
    (tmp_path / "x.py").write_text("pass\n")
    ctx = OperationContext.create(
        target_files=(str(tmp_path / "x.py"),),
        description="route parity",
    )
    return ctx.advance(
        OperationPhase.ROUTE, risk_tier=RiskTier.SAFE_AUTO,
    )


@pytest.fixture
def ctx(tmp_path):
    return _route_ctx(tmp_path)


def _orch(
    tmp_path: Path, *, expansion_enabled: bool = True, narrator: bool = False,
) -> _FakeOrchestrator:
    cfg = _FakeConfig(
        project_root=tmp_path,
        context_expansion_enabled=expansion_enabled,
    )
    return _FakeOrchestrator(
        _stack=_FakeStack(_FakeComm()),
        _config=cfg,
        _cost_governor=_FakeCostGovernor(),
        _pre_action_narrator=_FakeNarrator() if narrator else None,
    )


# ---------------------------------------------------------------------------
# (1) Class wiring
# ---------------------------------------------------------------------------


def test_route_runner_is_phase_runner():
    assert issubclass(ROUTERunner, PhaseRunner)
    assert ROUTERunner.phase is OperationPhase.ROUTE


# ---------------------------------------------------------------------------
# (2) Happy path — expansion enabled → CONTEXT_EXPANSION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_advances_to_context_expansion(ctx, tmp_path):
    orch = _orch(tmp_path, expansion_enabled=True, narrator=True)
    serpent = _FakeSerpent()

    result = await ROUTERunner(orch, serpent).run(ctx)

    assert result.status == "ok"
    assert result.reason == "routed"
    assert result.next_phase is OperationPhase.CONTEXT_EXPANSION
    assert result.next_ctx.phase is OperationPhase.CONTEXT_EXPANSION

    # Serpent saw the CTX transition update
    assert "CONTEXT_EXPANSION" in serpent.updates

    # PreActionNarrator fired for CONTEXT_EXPANSION
    assert orch._pre_action_narrator.calls
    assert orch._pre_action_narrator.calls[0][0] == "CONTEXT_EXPANSION"

    # CostGovernor started with route + complexity
    assert orch._cost_governor.starts
    assert orch._cost_governor.starts[0]["op_id"] == ctx.op_id


@pytest.mark.asyncio
async def test_expansion_disabled_advances_to_plan(ctx, tmp_path):
    orch = _orch(tmp_path, expansion_enabled=False)
    result = await ROUTERunner(orch, None).run(ctx)

    assert result.next_phase is OperationPhase.PLAN
    assert result.next_ctx.phase is OperationPhase.PLAN


@pytest.mark.asyncio
async def test_expansion_disabled_does_not_call_narrator(ctx, tmp_path):
    orch = _orch(tmp_path, expansion_enabled=False, narrator=True)
    await ROUTERunner(orch, None).run(ctx)
    # Narrator is only called pre-CTX-advance, which doesn't happen
    assert orch._pre_action_narrator.calls == []


# ---------------------------------------------------------------------------
# (3) UrgencyRouter + CommProtocol telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_urgency_router_stamps_provider_route(ctx, tmp_path):
    orch = _orch(tmp_path)
    result = await ROUTERunner(orch, None).run(ctx)
    # provider_route should be set on ctx (via object.__setattr__ in runner)
    assert getattr(result.next_ctx, "provider_route", "") != ""


@pytest.mark.asyncio
async def test_comm_emit_decision_fires(ctx, tmp_path):
    orch = _orch(tmp_path)
    await ROUTERunner(orch, None).run(ctx)
    assert orch._stack.comm.decisions
    d = orch._stack.comm.decisions[0]
    assert d["op_id"] == ctx.op_id
    assert "route" in d


# ---------------------------------------------------------------------------
# (4) CostGovernor start plumbs is_read_only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_governor_start_plumbs_is_read_only(tmp_path):
    # Build a read-only ctx
    (tmp_path / "y.py").write_text("pass\n")
    ctx = OperationContext.create(
        target_files=(str(tmp_path / "y.py"),), description="read only op",
        is_read_only=True,
    ).advance(OperationPhase.ROUTE, risk_tier=RiskTier.SAFE_AUTO)

    orch = _orch(tmp_path)
    await ROUTERunner(orch, None).run(ctx)
    assert orch._cost_governor.starts[0]["is_read_only"] is True


# ---------------------------------------------------------------------------
# (5) None-serpent on both paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_serpent_expansion_enabled(ctx, tmp_path):
    orch = _orch(tmp_path, expansion_enabled=True)
    result = await ROUTERunner(orch, None).run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_none_serpent_expansion_disabled(ctx, tmp_path):
    orch = _orch(tmp_path, expansion_enabled=False)
    result = await ROUTERunner(orch, None).run(ctx)
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# (6) Exception swallow invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_governor_raise_is_swallowed(ctx, tmp_path):
    orch = _orch(tmp_path)

    def _raise(**kwargs):
        raise RuntimeError("boom")

    orch._cost_governor.start = _raise  # type: ignore[method-assign]
    result = await ROUTERunner(orch, None).run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_emit_decision_raise_is_swallowed(ctx, tmp_path):
    orch = _orch(tmp_path)

    async def _raise(**kwargs):
        raise RuntimeError("comm boom")

    orch._stack.comm.emit_decision = _raise  # type: ignore[method-assign]
    result = await ROUTERunner(orch, None).run(ctx)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_narrator_raise_is_swallowed(ctx, tmp_path):
    orch = _orch(tmp_path, narrator=True)

    async def _raise(phase, payload):
        raise RuntimeError("narrator boom")

    orch._pre_action_narrator.narrate_phase = _raise  # type: ignore[method-assign]
    result = await ROUTERunner(orch, None).run(ctx)
    assert result.status == "ok"
    # Even with exception, transition still happened
    assert result.next_phase is OperationPhase.CONTEXT_EXPANSION


# ---------------------------------------------------------------------------
# (7) Authority invariant
# ---------------------------------------------------------------------------


def test_route_runner_bans_execution_authority_imports():
    import inspect
    from backend.core.ouroboros.governance.phase_runners import route_runner

    src = inspect.getsource(route_runner)
    for banned in ("candidate_generator", "iron_gate", "change_engine", "gate"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert banned not in s, (
                    f"route_runner.py must not import {banned}: {s}"
                )


__all__ = []

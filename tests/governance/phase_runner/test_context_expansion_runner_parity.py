"""Parity tests for :class:`ContextExpansionRunner` (Wave 2 (5) Slice 3).

Verbatim transcription of orchestrator.py CONTEXT_EXPANSION block
(~2143-2254). Pins the observable-trace parity.

Parity contract:

1. ``ContextExpander.expand(ctx, deadline)`` is awaited via wait_for
2. Optional ExplorationFleet + Oracle dependency summary injections
3. Broad try/except wraps — expansion failure is a WARNING, not fatal
4. Unconditional advance to PLAN at the end (``next_phase=PLAN``)
5. Resolves ``ContextExpander`` through orchestrator module for test patching
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.phase_runners.context_expansion_runner import (
    ContextExpansionRunner,
)


class _FakeComm:
    async def emit_decision(self, **kwargs):  # unused here but mirrors real shape
        pass


class _FakeStack:
    def __init__(self):
        self.comm = _FakeComm()
        self.oracle = None


@dataclass
class _FakeConfig:
    project_root: Path
    context_expansion_timeout_s: float = 10.0


@dataclass
class _FakeOrchestrator:
    _stack: _FakeStack
    _config: _FakeConfig
    _generator: Any = None
    _dialogue_store: Any = None
    _exploration_fleet: Any = None

    def _build_dependency_summary(self, oracle, target_files):
        return ""


def _ctx_at_ctx_phase(tmp_path: Path) -> OperationContext:
    (tmp_path / "a.py").write_text("pass\n")
    ctx = OperationContext.create(
        target_files=(str(tmp_path / "a.py"),), description="ctx parity",
    )
    return ctx.advance(OperationPhase.ROUTE).advance(OperationPhase.CONTEXT_EXPANSION)


@pytest.fixture
def ctx(tmp_path):
    return _ctx_at_ctx_phase(tmp_path)


@pytest.fixture
def orch(tmp_path):
    return _FakeOrchestrator(
        _stack=_FakeStack(),
        _config=_FakeConfig(project_root=tmp_path),
    )


# ---------------------------------------------------------------------------
# (1) Class wiring
# ---------------------------------------------------------------------------


def test_ctx_runner_is_phase_runner():
    assert issubclass(ContextExpansionRunner, PhaseRunner)
    assert ContextExpansionRunner.phase is OperationPhase.CONTEXT_EXPANSION


# ---------------------------------------------------------------------------
# (2) Happy path — patches orchestrator.ContextExpander + verifies expand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_called_and_advances_to_plan(ctx, orch):
    expand_called = []

    async def _fake_expand(the_ctx, deadline):
        expand_called.append(True)
        return the_ctx

    with patch("backend.core.ouroboros.governance.orchestrator.ContextExpander") as MockExp:
        inst = MagicMock()
        inst.expand = AsyncMock(side_effect=_fake_expand)
        MockExp.return_value = inst

        result = await ContextExpansionRunner(orch, None).run(ctx)

    assert expand_called == [True]
    assert result.status == "ok"
    assert result.reason == "expanded"
    assert result.next_phase is OperationPhase.PLAN
    assert result.next_ctx.phase is OperationPhase.PLAN


# ---------------------------------------------------------------------------
# (3) Expansion failure is swallowed — still advances to PLAN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expansion_failure_still_advances_to_plan(ctx, orch):
    async def _raise(the_ctx, deadline):
        raise RuntimeError("expander boom")

    with patch("backend.core.ouroboros.governance.orchestrator.ContextExpander") as MockExp:
        inst = MagicMock()
        inst.expand = AsyncMock(side_effect=_raise)
        MockExp.return_value = inst

        result = await ContextExpansionRunner(orch, None).run(ctx)

    assert result.status == "ok"
    assert result.next_ctx.phase is OperationPhase.PLAN


# ---------------------------------------------------------------------------
# (4) Hash chain advances
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hash_chain_advances(ctx, orch):
    before = ctx.context_hash
    with patch("backend.core.ouroboros.governance.orchestrator.ContextExpander") as MockExp:
        inst = MagicMock()
        inst.expand = AsyncMock(side_effect=lambda c, d: c)
        MockExp.return_value = inst

        result = await ContextExpansionRunner(orch, None).run(ctx)
    assert result.next_ctx.context_hash != before


# ---------------------------------------------------------------------------
# (5) Authority invariant
# ---------------------------------------------------------------------------


def test_ctx_runner_bans_execution_authority_imports():
    import inspect
    from backend.core.ouroboros.governance.phase_runners import context_expansion_runner

    src = inspect.getsource(context_expansion_runner)
    for banned in ("candidate_generator", "iron_gate", "change_engine", "gate"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert banned not in s, (
                    f"context_expansion_runner.py must not import {banned}: {s}"
                )


__all__ = []

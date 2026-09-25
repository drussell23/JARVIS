"""CONTEXT_EXPANSION's two dead injections (2026-09-24).

1. The Oracle dependency summary never reached the prompt under the isolated
   Oracle: the sync builder called the async proxy through ``.raw``, got an
   un-awaited coroutine, and died on ``.get`` outside its own try
   ("'coroutine' object has no attribute 'get'", DEBUG-logged, every op).
2. The ExplorationFleet ran 8 agents per op and its output was discarded
   (``ctx.expanded_files`` does not exist). It is goal-independent -- the same
   77 files every soak -- so it is removed from this phase, not wired in.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
from backend.core.ouroboros.governance.phase_runners.context_expansion_runner import (
    ContextExpansionRunner,
)
from backend.core.ouroboros.oracle_adapter import IsolatedOracleAdapter

_INFO = {
    "found": True,
    "risk_assessment": {"risk_level": "high", "total_affected": 4},
    "dependents": [{"file_path": "backend/consumer_a.py"}, {"file_path": "backend/consumer_b.py"}],
    "related_files": [],
}


class _AsyncProxy:
    """The shape of AsyncOracleProxy: the IPC method is a coroutine."""

    def __init__(self) -> None:
        self.calls: list = []
        self.is_ready = True

    async def get_context_for_improvement(self, target: Any, max_depth: int = 2) -> dict:
        self.calls.append(target)
        return dict(_INFO)


class _SyncOracle:
    def get_context_for_improvement(self, target: Any, max_depth: int = 2) -> dict:
        return dict(_INFO)


@pytest.mark.asyncio
async def test_isolated_adapter_yields_a_summary():
    proxy = _AsyncProxy()
    summary = await GovernedOrchestrator._build_dependency_summary(
        IsolatedOracleAdapter(proxy), ("backend/target.py",),
    )
    assert proxy.calls == ["backend/target.py"]
    assert "backend/consumer_a.py" in summary
    assert "risk=high" in summary


@pytest.mark.asyncio
async def test_bare_sync_oracle_still_works():
    summary = await GovernedOrchestrator._build_dependency_summary(
        _SyncOracle(), ("backend/target.py",),
    )
    assert "backend/consumer_b.py" in summary


@pytest.mark.asyncio
async def test_non_dict_reply_degrades_to_empty():
    class _Weird:
        async def get_context_for_improvement(self, target, max_depth=2):
            return None

    assert await GovernedOrchestrator._build_dependency_summary(_Weird(), ("x.py",)) == ""


# ---------------------------------------------------------------------------
# Through the runner: the summary lands on ctx, the fleet is never deployed
# ---------------------------------------------------------------------------


@dataclass
class _Stack:
    oracle: Any
    comm: Any = None


@dataclass
class _Config:
    project_root: Path
    context_expansion_timeout_s: float = 10.0


@dataclass
class _Orch:
    _stack: _Stack
    _config: _Config
    _exploration_fleet: Any
    _generator: Any = None
    _dialogue_store: Any = None
    _build_dependency_summary = staticmethod(GovernedOrchestrator._build_dependency_summary)


@pytest.mark.asyncio
async def test_runner_injects_summary_and_skips_fleet(tmp_path):
    (tmp_path / "a.py").write_text("pass\n")
    ctx = OperationContext.create(
        target_files=(str(tmp_path / "a.py"),), description="dep summary",
    ).advance(OperationPhase.ROUTE).advance(OperationPhase.CONTEXT_EXPANSION)

    fleet = MagicMock()
    fleet.deploy = AsyncMock()
    orch = _Orch(
        _stack=_Stack(oracle=IsolatedOracleAdapter(_AsyncProxy())),
        _config=_Config(project_root=tmp_path),
        _exploration_fleet=fleet,
    )
    with patch("backend.core.ouroboros.governance.orchestrator.ContextExpander") as MockExp:
        inst = MagicMock()
        inst.expand = AsyncMock(side_effect=lambda c, d: c)
        MockExp.return_value = inst
        result = await ContextExpansionRunner(orch, None).run(ctx)

    assert result.status == "ok"
    assert "backend/consumer_a.py" in result.next_ctx.dependency_summary
    fleet.deploy.assert_not_called()

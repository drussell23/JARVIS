"""MEM-2: ContextExpansionRunner wires ModuleContextRouter (gated default-OFF).

TDD contract (3 cases):
  1. flag ON  → routed section appended to strategic_memory_prompt
  2. flag OFF → byte-identical, no router call
  3. router raises → pipeline unaffected, original prompt intact

Mirror the setup from test_context_expansion_runner_parity.py.
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
from backend.core.ouroboros.governance.phase_runners.context_expansion_runner import (
    ContextExpansionRunner,
)


# ---------------------------------------------------------------------------
# Fakes mirroring test_context_expansion_runner_parity.py
# ---------------------------------------------------------------------------


class _FakeComm:
    async def emit_decision(self, **kwargs):
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
        target_files=(str(tmp_path / "a.py"),), description="mem-2 routing test",
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
# Helper — run the runner with ContextExpander patched to be a no-op
# ---------------------------------------------------------------------------


async def _run_with_noop_expander(
    orch: _FakeOrchestrator,
    ctx: OperationContext,
) -> OperationContext:
    """Run ContextExpansionRunner with ContextExpander as identity (no-op)."""
    with patch(
        "backend.core.ouroboros.governance.orchestrator.ContextExpander"
    ) as MockExp:
        inst = MagicMock()
        inst.expand = AsyncMock(side_effect=lambda c, d: c)
        MockExp.return_value = inst

        result = await ContextExpansionRunner(orch, None).run(ctx)

    return result.next_ctx


# ---------------------------------------------------------------------------
# Test 1 — flag ON: routed section is appended to strategic_memory_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_appended_when_flag_on(ctx, orch, monkeypatch):
    """When JARVIS_MEMORY_ROUTING_ENABLED=1, the routed section is appended."""
    monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "1")

    _ROUTED_SECTION = "## Relevant Architecture Memory\n\n### Orchestrator\nsome content"

    # Patch routing_enabled (imported lazily in the runner)
    with patch(
        "backend.core.ouroboros.governance.module_routing.routing_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance.module_routing.ModuleContextRouter"
    ) as MockRouter:
        _fake_result = MagicMock()
        _fake_result.section = _ROUTED_SECTION
        _fake_result.topics = ("topic-a",)
        MockRouter.return_value.route.return_value = _fake_result

        result_ctx = await _run_with_noop_expander(orch, ctx)

    prompt = result_ctx.strategic_memory_prompt
    assert _ROUTED_SECTION in prompt, (
        f"Expected routed section in strategic_memory_prompt; got: {prompt!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — flag OFF: byte-identical, no router instantiated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_skipped_when_flag_off(ctx, orch, monkeypatch):
    """When JARVIS_MEMORY_ROUTING_ENABLED is absent/false, prompt is unchanged."""
    monkeypatch.delenv("JARVIS_MEMORY_ROUTING_ENABLED", raising=False)

    prompt_before = ctx.strategic_memory_prompt  # "" (empty at CONTEXT_EXPANSION entry)

    with patch(
        "backend.core.ouroboros.governance.module_routing.ModuleContextRouter"
    ) as MockRouter:
        result_ctx = await _run_with_noop_expander(orch, ctx)

    # Router must never have been instantiated
    MockRouter.assert_not_called()

    # strategic_memory_prompt unchanged (byte-identical)
    assert result_ctx.strategic_memory_prompt == prompt_before


# ---------------------------------------------------------------------------
# Test 3 — router raises: pipeline unaffected, original prompt intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_exception_is_swallowed(ctx, orch, monkeypatch):
    """If ModuleContextRouter.route() raises, the pipeline continues unaffected."""
    monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "1")

    prompt_before = ctx.strategic_memory_prompt

    with patch(
        "backend.core.ouroboros.governance.module_routing.routing_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance.module_routing.ModuleContextRouter"
    ) as MockRouter:
        MockRouter.return_value.route.side_effect = RuntimeError("router exploded")

        # Must not raise
        result_ctx = await _run_with_noop_expander(orch, ctx)

    # Phase still advances to PLAN
    assert result_ctx.phase is OperationPhase.PLAN

    # strategic_memory_prompt unchanged after the crash
    assert result_ctx.strategic_memory_prompt == prompt_before


__all__ = []

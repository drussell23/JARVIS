"""Infrastructure tests for the Wave 2 (5) Slice 6a phase dispatcher.

Covers:
* PhaseContext slot semantics + artifact merge
* PhaseRunnerRegistry register/get/phases
* Loud-fail contract — registry miss, unknown phase, malformed runner,
  malformed PhaseResult, iteration cap
* Dispatcher loop mechanics — dispatch_phase tracking distinct from
  ctx.phase (key for GENERATE→VALIDATE handoff)
* Flag gate default + runtime flip
* Dispatcher-on ≡ dispatcher-off for happy path (single test that
  drives a real orchestrator through both paths, pinning the final
  ctx.phase + terminal_reason_code)
* Dispatcher-on ≡ dispatcher-off for one high-signal terminal
  (user_cancelled pre-APPLY — stresses cross-phase state threading
  because cancel check reads orch._is_cancel_requested after CLASSIFY
  has stamped risk_tier/advisory/consciousness_bridge into pctx)

6b owns the per-phase terminal matrix + artifact propagation edge
cases. 6a proves the scaffolding is sound.

Authority invariant: no candidate_generator / iron_gate / change_engine.
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
from backend.core.ouroboros.governance.phase_dispatcher import (
    PhaseContext,
    PhaseContextError,
    PhaseDispatchError,
    PhaseRunnerRegistry,
    PhaseRunnerRegistryError,
    build_default_registry,
    dispatch_pipeline,
    dispatcher_enabled,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)


# ===========================================================================
# PhaseContext
# ===========================================================================


def test_phase_context_defaults_are_none_or_empty():
    pctx = PhaseContext()
    assert pctx.advisory is None
    assert pctx.consciousness_bridge is None
    assert pctx.risk_tier is None
    assert pctx.best_candidate is None
    assert pctx.best_validation is None
    assert pctx.generation is None
    assert pctx.episodic_memory is None
    assert pctx.generate_retries_remaining is None
    assert pctx.t_apply == 0.0
    assert pctx.extras == {}


def test_phase_context_merge_artifacts_populates_declared_slots():
    pctx = PhaseContext()
    pctx.merge_artifacts({
        "advisory": "A",
        "consciousness_bridge": "CB",
        "risk_tier": "RT",
        "best_candidate": {"id": "c0"},
        "generation": "GEN",
        "episodic_memory": "EM",
        "generate_retries_remaining": 3,
        "t_apply": 12.5,
    })
    assert pctx.advisory == "A"
    assert pctx.consciousness_bridge == "CB"
    assert pctx.risk_tier == "RT"
    assert pctx.best_candidate == {"id": "c0"}
    assert pctx.generation == "GEN"
    assert pctx.episodic_memory == "EM"
    assert pctx.generate_retries_remaining == 3
    assert pctx.t_apply == 12.5


def test_phase_context_merge_artifacts_unknown_keys_land_in_extras():
    pctx = PhaseContext()
    pctx.merge_artifacts({"custom_key": "xyz"})
    assert pctx.extras == {"custom_key": "xyz"}


def test_phase_context_merge_artifacts_none_is_noop():
    pctx = PhaseContext()
    pctx.merge_artifacts(None)  # must not raise
    assert pctx.advisory is None


def test_phase_context_merge_artifacts_rejects_non_mapping():
    pctx = PhaseContext()
    with pytest.raises(PhaseContextError, match="Mapping"):
        pctx.merge_artifacts([("a", 1), ("b", 2)])  # list, not Mapping


def test_phase_context_extras_key_stays_extras_not_slot():
    """Prevent accidental 'extras' slot overwrite — it's a dict, not a slot."""
    pctx = PhaseContext()
    pctx.merge_artifacts({"extras": "bad"})
    # 'extras' key routed to pctx.extras["extras"], not pctx.extras = "bad"
    assert pctx.extras == {"extras": "bad"}
    assert isinstance(pctx.extras, dict)


# ===========================================================================
# PhaseRunnerRegistry
# ===========================================================================


def _dummy_factory(orch, serpent, pctx, ctx):
    class _Dummy(PhaseRunner):
        phase = OperationPhase.CLASSIFY

        async def run(self, c):
            return PhaseResult(next_ctx=c, next_phase=None, status="ok")
    return _Dummy()


def test_registry_register_and_get():
    reg = PhaseRunnerRegistry()
    reg.register(OperationPhase.CLASSIFY, _dummy_factory)
    assert reg.get(OperationPhase.CLASSIFY) is _dummy_factory


def test_registry_miss_raises_with_descriptive_error():
    reg = PhaseRunnerRegistry()
    reg.register(OperationPhase.CLASSIFY, _dummy_factory)
    with pytest.raises(
        PhaseRunnerRegistryError,
        match="no runner factory registered for phase ROUTE",
    ) as exc_info:
        reg.get(OperationPhase.ROUTE)
    # Error must list registered phases for operator diagnosis
    assert "CLASSIFY" in str(exc_info.value)


def test_registry_register_rejects_non_phase():
    reg = PhaseRunnerRegistry()
    with pytest.raises(PhaseRunnerRegistryError, match="OperationPhase"):
        reg.register("CLASSIFY", _dummy_factory)  # type: ignore[arg-type]


def test_registry_register_rejects_non_callable():
    reg = PhaseRunnerRegistry()
    with pytest.raises(PhaseRunnerRegistryError, match="callable"):
        reg.register(OperationPhase.CLASSIFY, "not_a_factory")  # type: ignore[arg-type]


def test_registry_overwrite_wins():
    reg = PhaseRunnerRegistry()
    reg.register(OperationPhase.CLASSIFY, _dummy_factory)
    new_factory = lambda o, s, p, c: _dummy_factory(o, s, p, c)
    reg.register(OperationPhase.CLASSIFY, new_factory)
    assert reg.get(OperationPhase.CLASSIFY) is new_factory


def test_default_registry_covers_all_nine_extracted_phases():
    """Canonical registry must wire every runner Slices 1-5 extracted."""
    reg = build_default_registry()
    covered = set(reg.phases())
    expected = {
        OperationPhase.CLASSIFY,
        OperationPhase.ROUTE,
        OperationPhase.CONTEXT_EXPANSION,
        OperationPhase.PLAN,
        OperationPhase.GENERATE,
        OperationPhase.VALIDATE,
        OperationPhase.GATE,
        OperationPhase.APPROVE,
        OperationPhase.COMPLETE,
    }
    assert covered == expected


# ===========================================================================
# Flag gate
# ===========================================================================


def test_dispatcher_enabled_default_true_post_graduation(monkeypatch):
    """Post-#8 FINAL (commit 203856371e, 2026-04-23): dispatcher_enabled()
    default flipped false → true. Unset env → True. Original pre-graduation
    name kept for git blame continuity via the truthy/falsey variant tests
    below (which cover explicit env values in both directions)."""
    monkeypatch.delenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", raising=False)
    assert dispatcher_enabled() is True


def test_dispatcher_enabled_truthy_variants(monkeypatch):
    for val in ("true", "1", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", val)
        assert dispatcher_enabled() is True


def test_dispatcher_enabled_falsey_variants(monkeypatch):
    for val in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", val)
        assert dispatcher_enabled() is False


# ===========================================================================
# Dispatcher loop — loud-fail contract
# ===========================================================================


def _make_ctx(tmp_path: Path, phase: OperationPhase = OperationPhase.CLASSIFY) -> OperationContext:
    (tmp_path / "a.py").write_text("pass\n")
    ctx = OperationContext.create(
        target_files=(str(tmp_path / "a.py"),),
        description="dispatcher test",
    )
    # Advance to the requested phase if not CLASSIFY
    from backend.core.ouroboros.governance.risk_engine import RiskTier
    if phase is not OperationPhase.CLASSIFY:
        ctx = ctx.advance(OperationPhase.ROUTE, risk_tier=RiskTier.SAFE_AUTO)
        if phase is not OperationPhase.ROUTE:
            # Walk forward — test only covers the happy chain for the
            # dispatcher-can-start-at-any-phase invariant
            pass
    return ctx


@pytest.mark.asyncio
async def test_dispatcher_registry_miss_raises(tmp_path):
    """ctx.phase with no registered factory → loud fail."""
    reg = PhaseRunnerRegistry()  # empty registry
    ctx = _make_ctx(tmp_path)
    with pytest.raises(PhaseRunnerRegistryError, match="no runner factory"):
        await dispatch_pipeline(MagicMock(), None, ctx, registry=reg)


@pytest.mark.asyncio
async def test_dispatcher_malformed_runner_raises(tmp_path):
    """Factory returns non-PhaseRunner → loud fail."""
    reg = PhaseRunnerRegistry()
    reg.register(
        OperationPhase.CLASSIFY,
        lambda o, s, p, c: "not_a_runner",  # type: ignore[return-value]
    )
    ctx = _make_ctx(tmp_path)
    with pytest.raises(PhaseDispatchError, match="not a PhaseRunner"):
        await dispatch_pipeline(MagicMock(), None, ctx, registry=reg)


@pytest.mark.asyncio
async def test_dispatcher_malformed_result_raises(tmp_path):
    """Runner returns non-PhaseResult → loud fail."""
    class _BadRunner(PhaseRunner):
        phase = OperationPhase.CLASSIFY

        async def run(self, c):
            return "not_a_phase_result"

    reg = PhaseRunnerRegistry()
    reg.register(OperationPhase.CLASSIFY, lambda o, s, p, c: _BadRunner())
    ctx = _make_ctx(tmp_path)
    with pytest.raises(PhaseDispatchError, match="not a PhaseResult"):
        await dispatch_pipeline(MagicMock(), None, ctx, registry=reg)


@pytest.mark.asyncio
async def test_dispatcher_factory_exception_wraps_as_context_error(tmp_path):
    """Factory raising unexpected error → wrapped as PhaseContextError."""
    reg = PhaseRunnerRegistry()

    def _crashy_factory(o, s, p, c):
        raise ValueError("my slot is empty")

    reg.register(OperationPhase.CLASSIFY, _crashy_factory)
    ctx = _make_ctx(tmp_path)
    with pytest.raises(PhaseContextError, match="my slot is empty"):
        await dispatch_pipeline(MagicMock(), None, ctx, registry=reg)


@pytest.mark.asyncio
async def test_dispatcher_factory_phase_context_error_passes_through(tmp_path):
    """Factory raising PhaseContextError is re-raised as-is (no wrapping)."""
    reg = PhaseRunnerRegistry()

    def _factory_missing_slot(o, s, p, c):
        raise PhaseContextError("explicit missing slot message")

    reg.register(OperationPhase.CLASSIFY, _factory_missing_slot)
    ctx = _make_ctx(tmp_path)
    with pytest.raises(PhaseContextError, match="explicit missing slot message"):
        await dispatch_pipeline(MagicMock(), None, ctx, registry=reg)


@pytest.mark.asyncio
async def test_dispatcher_iteration_cap_raises(tmp_path):
    """Infinite loop (runner returns its own phase as next_phase) → iteration cap."""
    class _SelfLoopRunner(PhaseRunner):
        phase = OperationPhase.CLASSIFY

        async def run(self, c):
            return PhaseResult(
                next_ctx=c,
                next_phase=OperationPhase.CLASSIFY,
                status="ok",
            )

    reg = PhaseRunnerRegistry()
    reg.register(OperationPhase.CLASSIFY, lambda o, s, p, c: _SelfLoopRunner())
    ctx = _make_ctx(tmp_path)
    with pytest.raises(PhaseDispatchError, match="max_iterations"):
        await dispatch_pipeline(MagicMock(), None, ctx, registry=reg, max_iterations=5)


# ===========================================================================
# Dispatcher loop — dispatch_phase tracks next_phase, not ctx.phase
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatcher_follows_next_phase_not_ctx_phase(tmp_path):
    """Key invariant for GENERATE→VALIDATE handoff: when a runner returns
    ``next_phase=X`` but leaves ``next_ctx.phase`` at the current phase,
    the dispatcher must follow next_phase for the next factory lookup
    (not ctx.phase), preventing an infinite self-loop on the runner
    whose body expects the NEXT runner to do the advance."""
    class _NoAdvanceRunner(PhaseRunner):
        phase = OperationPhase.CLASSIFY

        async def run(self, c):
            # Return next_phase=ROUTE but leave ctx in CLASSIFY.
            return PhaseResult(
                next_ctx=c, next_phase=OperationPhase.ROUTE, status="ok",
            )

    class _TerminalRouteRunner(PhaseRunner):
        phase = OperationPhase.ROUTE

        async def run(self, c):
            return PhaseResult(
                next_ctx=c, next_phase=None, status="ok", reason="done",
            )

    reg = PhaseRunnerRegistry()
    reg.register(OperationPhase.CLASSIFY, lambda o, s, p, c: _NoAdvanceRunner())
    reg.register(OperationPhase.ROUTE, lambda o, s, p, c: _TerminalRouteRunner())

    ctx = _make_ctx(tmp_path)
    # Should dispatch to CLASSIFY → ROUTE → terminate. If dispatch used
    # ctx.phase it would loop on CLASSIFY forever.
    result = await dispatch_pipeline(MagicMock(), None, ctx, registry=reg, max_iterations=5)
    # Dispatcher exited via ROUTE's next_phase=None; result is the ctx
    # returned by _TerminalRouteRunner (still CLASSIFY phase since
    # neither runner advanced).
    assert result.phase is OperationPhase.CLASSIFY  # neither runner advanced


# ===========================================================================
# Dispatcher-on ≡ dispatcher-off parity (happy + terminal)
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatcher_parity_happy_path(monkeypatch, tmp_path):
    """Drive a real orchestrator through dispatcher-off path, record the
    final ctx.phase. Then drive the same setup through dispatcher-on.
    Final ctx.phase must match. This is the bedrock parity invariant —
    if this test fails, the dispatcher has diverged from the inline FSM."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, AsyncMock

    from backend.core.ouroboros.governance.op_context import GenerationResult
    from backend.core.ouroboros.governance.risk_engine import (
        RiskClassification, RiskTier,
    )
    from backend.core.ouroboros.governance.orchestrator import (
        GovernedOrchestrator, OrchestratorConfig,
    )

    def _build_stack():
        stack = MagicMock()
        stack.can_write.return_value = (True, "ok")
        stack.risk_engine.classify.return_value = RiskClassification(
            tier=RiskTier.SAFE_AUTO, reason_code="default_safe",
        )
        stack.ledger = MagicMock()
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine = AsyncMock()
        stack.change_engine.execute = AsyncMock(return_value=MagicMock(
            success=True, rolled_back=False, op_id="op-parity",
        ))
        stack.governed_loop_service.is_cancel_requested.return_value = False
        stack.learning_bridge = MagicMock()
        stack.learning_bridge.publish = AsyncMock(return_value=None)
        stack.security_reviewer = MagicMock()
        stack.security_reviewer.review = AsyncMock(return_value=None)
        return stack

    def _build_gen():
        gen = MagicMock()
        gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=(
                {
                    "candidate_id": "c1",
                    "file_path": "backend/core/utils.py",
                    "full_content": "def hello():\n    pass\n",
                    "rationale": "parity stub",
                },
            ),
            provider_name="mock-provider",
            generation_duration_s=1.5,
            tool_execution_records=(),
        ))
        return gen

    def _build_cfg():
        return OrchestratorConfig(
            project_root=Path("/tmp/test-project-dispatcher"),
            generation_timeout_s=5.0,
            validation_timeout_s=5.0,
            approval_timeout_s=5.0,
            max_generate_retries=1,
            max_validate_retries=2,
            benchmark_enabled=False,
        )

    def _build_ctx(op_id: str):
        return OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="parity",
            op_id=op_id,
            _timestamp=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
        )

    # --- dispatcher OFF baseline ---
    monkeypatch.delenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")  # simplify spine
    orch_off = GovernedOrchestrator(
        stack=_build_stack(), generator=_build_gen(),
        approval_provider=None, config=_build_cfg(),
    )
    out_off = await orch_off.run(_build_ctx("op-parity-off"))
    final_phase_off = out_off.phase
    final_reason_off = out_off.terminal_reason_code

    # --- dispatcher ON ---
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
    orch_on = GovernedOrchestrator(
        stack=_build_stack(), generator=_build_gen(),
        approval_provider=None, config=_build_cfg(),
    )
    out_on = await orch_on.run(_build_ctx("op-parity-on"))
    final_phase_on = out_on.phase
    final_reason_on = out_on.terminal_reason_code

    # Parity on terminal state
    assert final_phase_on is final_phase_off, (
        f"dispatcher-on final phase {final_phase_on} != "
        f"dispatcher-off final phase {final_phase_off}"
    )
    assert final_reason_on == final_reason_off, (
        f"dispatcher-on reason {final_reason_on!r} != "
        f"dispatcher-off reason {final_reason_off!r}"
    )


@pytest.mark.asyncio
async def test_dispatcher_parity_cancel_terminal(monkeypatch, tmp_path):
    """High-signal terminal: user_cancelled pre-APPLY. Stresses context
    threading — classification stamps advisory + consciousness_bridge +
    risk_tier into pctx, then the cancel check fires AFTER multiple
    phases have run. Both paths must land the same terminal state."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, AsyncMock

    from backend.core.ouroboros.governance.op_context import GenerationResult
    from backend.core.ouroboros.governance.risk_engine import (
        RiskClassification, RiskTier,
    )
    from backend.core.ouroboros.governance.orchestrator import (
        GovernedOrchestrator, OrchestratorConfig,
    )

    def _build_stack():
        stack = MagicMock()
        stack.can_write.return_value = (True, "ok")
        stack.risk_engine.classify.return_value = RiskClassification(
            tier=RiskTier.SAFE_AUTO, reason_code="default_safe",
        )
        stack.ledger = MagicMock()
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine = AsyncMock()
        stack.change_engine.execute = AsyncMock(return_value=MagicMock(
            success=True, rolled_back=False,
        ))
        # User cancels pre-APPLY
        stack.governed_loop_service.is_cancel_requested.return_value = True
        stack.learning_bridge = MagicMock()
        stack.learning_bridge.publish = AsyncMock(return_value=None)
        stack.security_reviewer = MagicMock()
        stack.security_reviewer.review = AsyncMock(return_value=None)
        return stack

    def _build_gen():
        gen = MagicMock()
        gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=(
                {
                    "candidate_id": "c1",
                    "file_path": "backend/core/utils.py",
                    "full_content": "def hello():\n    pass\n",
                },
            ),
            provider_name="mock",
            generation_duration_s=1.0,
            tool_execution_records=(),
        ))
        return gen

    def _build_cfg():
        return OrchestratorConfig(
            project_root=Path("/tmp/test-project-dispatcher-cancel"),
            generation_timeout_s=5.0, validation_timeout_s=5.0,
            approval_timeout_s=5.0,
            max_generate_retries=1, max_validate_retries=2,
            benchmark_enabled=False,
        )

    def _build_ctx(op_id):
        return OperationContext.create(
            target_files=("backend/core/utils.py",),
            description="parity cancel",
            op_id=op_id,
            _timestamp=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")

    # dispatcher OFF
    monkeypatch.delenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", raising=False)
    orch_off = GovernedOrchestrator(
        stack=_build_stack(), generator=_build_gen(),
        approval_provider=None, config=_build_cfg(),
    )
    out_off = await orch_off.run(_build_ctx("op-cancel-off"))

    # dispatcher ON
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")
    orch_on = GovernedOrchestrator(
        stack=_build_stack(), generator=_build_gen(),
        approval_provider=None, config=_build_cfg(),
    )
    out_on = await orch_on.run(_build_ctx("op-cancel-on"))

    # Both paths must hit the SAME terminal phase + reason
    assert out_on.phase is out_off.phase
    assert out_on.terminal_reason_code == out_off.terminal_reason_code


# ===========================================================================
# Authority invariant
# ===========================================================================


def test_phase_dispatcher_bans_execution_authority_imports():
    import inspect
    from backend.core.ouroboros.governance import phase_dispatcher

    src = inspect.getsource(phase_dispatcher)
    for banned in ("candidate_generator", "iron_gate", "change_engine"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert banned not in s, (
                    f"phase_dispatcher.py must not import {banned}: {s}"
                )


__all__ = []

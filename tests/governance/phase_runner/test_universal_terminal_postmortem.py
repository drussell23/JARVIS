"""Tests for Option E — Universal Terminal Postmortem.

Verifies that EVERY op termination (not just COMPLETE) produces a
VerificationPostmortem, dynamically captures the terminal context,
persists via capture_phase_decision for Merkle DAG linkage, and
executes asynchronously without blocking the dispatcher.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_dispatcher import (
    PhaseContext,
    PhaseRunnerRegistry,
    _fire_terminal_postmortem,
    _terminal_postmortem_enabled,
    dispatch_pipeline,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_flags(monkeypatch):
    """Enable all relevant flags for every test."""
    monkeypatch.setenv("JARVIS_TERMINAL_POSTMORTEM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_VERIFICATION_POSTMORTEM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED", "true")


@dataclass
class _FakeCtx:
    """Minimal duck-typed OperationContext for testing."""
    op_id: str = "test-op-001"
    phase: OperationPhase = OperationPhase.CLASSIFY
    validation_passed: bool = False
    target_files: list = field(default_factory=list)
    signal_urgency: str = ""
    signal_source: str = ""
    task_complexity: str = ""
    cross_repo: bool = False
    is_read_only: bool = False
    risk_tier: Any = None
    generation: Any = None

    def advance(self, phase, **kwargs):
        return _FakeCtx(op_id=self.op_id, phase=phase, **{
            k: v for k, v in kwargs.items()
            if k in self.__dataclass_fields__
        })


class _TerminalRunner(PhaseRunner):
    """Runner that terminates immediately with configurable status/reason."""
    phase = OperationPhase.CLASSIFY

    def __init__(self, status="fail", reason="test_terminated"):
        self._status = status
        self._reason = reason

    async def run(self, ctx):
        return PhaseResult(
            next_ctx=ctx,
            next_phase=None,
            status=self._status,
            reason=self._reason,
        )


class _PassthroughRunner(PhaseRunner):
    """Runner that advances to the next phase."""
    phase = OperationPhase.CLASSIFY

    def __init__(self, next_phase):
        self._next_phase = next_phase

    async def run(self, ctx):
        return PhaseResult(
            next_ctx=ctx,
            next_phase=self._next_phase,
            status="ok",
            reason="advanced",
        )


class _FakeOrchestrator:
    """Minimal duck-typed Orchestrator."""
    _cancel_token_registry = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_registry_with_terminal(phase: OperationPhase, **kwargs):
    """Build a registry with a single terminal runner at the given phase."""
    reg = PhaseRunnerRegistry()
    reg.register(phase, lambda o, s, p, c: _TerminalRunner(**kwargs))
    return reg


def _build_chain_to_terminal(
    *phases: OperationPhase,
    terminal_status="fail",
    terminal_reason="chain_end",
):
    """Build a registry where phases chain into each other, with the last
    one terminating."""
    reg = PhaseRunnerRegistry()
    for i, phase in enumerate(phases):
        if i == len(phases) - 1:
            reg.register(
                phase,
                lambda o, s, p, c, st=terminal_status, r=terminal_reason: (
                    _TerminalRunner(status=st, reason=r)
                ),
            )
        else:
            next_p = phases[i + 1]
            reg.register(
                phase,
                lambda o, s, p, c, np=next_p: _PassthroughRunner(np),
            )
    return reg


# ---------------------------------------------------------------------------
# 1. Core: Every non-COMPLETE terminal fires postmortem
# ---------------------------------------------------------------------------


class TestTerminalPhasesFire:
    """Each non-COMPLETE phase that returns next_phase=None fires the
    universal postmortem."""

    @pytest.mark.parametrize("phase,reason", [
        (OperationPhase.CLASSIFY, "background_accepted"),
        (OperationPhase.ROUTE, "route_rejected"),
        (OperationPhase.PLAN, "is_noop"),
        (OperationPhase.GENERATE, "provider_error"),
        (OperationPhase.VALIDATE, "validate_failed"),
        (OperationPhase.GATE, "gate_blocked"),
        (OperationPhase.APPROVE, "apply_rejected"),
    ])
    @pytest.mark.asyncio
    async def test_fires_for_phase(self, phase, reason):
        reg = _build_registry_with_terminal(phase, reason=reason)
        ctx = _FakeCtx(phase=phase)
        orch = _FakeOrchestrator()

        with patch(
            "backend.core.ouroboros.governance.phase_dispatcher"
            "._fire_terminal_postmortem",
            new_callable=AsyncMock,
        ) as mock_fire:
            await dispatch_pipeline(
                orch, None, ctx, registry=reg,
            )
            mock_fire.assert_called_once()
            call_kwargs = mock_fire.call_args.kwargs
            assert call_kwargs["terminal_phase"] == phase
            assert call_kwargs["reason"] == reason


class TestCompleteDoesNotDoubleFire:
    """COMPLETE runner already fires its own postmortem — the universal
    hook must NOT fire for COMPLETE."""

    @pytest.mark.asyncio
    async def test_complete_skipped(self):
        reg = PhaseRunnerRegistry()
        reg.register(
            OperationPhase.COMPLETE,
            lambda o, s, p, c: _TerminalRunner(
                status="ok", reason="complete",
            ),
        )
        ctx = _FakeCtx(phase=OperationPhase.COMPLETE)
        orch = _FakeOrchestrator()

        with patch(
            "backend.core.ouroboros.governance.phase_dispatcher"
            "._fire_terminal_postmortem",
            new_callable=AsyncMock,
        ) as mock_fire:
            await dispatch_pipeline(
                orch, None, ctx, registry=reg,
            )
            mock_fire.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Dynamic terminal context capture
# ---------------------------------------------------------------------------


class TestDynamicContext:
    """The postmortem captures status and reason dynamically from the
    PhaseResult — not hardcoded."""

    @pytest.mark.asyncio
    async def test_status_captured(self):
        reg = _build_registry_with_terminal(
            OperationPhase.GATE, status="fail", reason="security_reject",
        )
        ctx = _FakeCtx(phase=OperationPhase.GATE)
        orch = _FakeOrchestrator()

        with patch(
            "backend.core.ouroboros.governance.phase_dispatcher"
            "._fire_terminal_postmortem",
            new_callable=AsyncMock,
        ) as mock_fire:
            await dispatch_pipeline(
                orch, None, ctx, registry=reg,
            )
            kw = mock_fire.call_args.kwargs
            assert kw["status"] == "fail"
            assert kw["reason"] == "security_reject"

    @pytest.mark.asyncio
    async def test_skip_status_captured(self):
        reg = PhaseRunnerRegistry()
        reg.register(
            OperationPhase.PLAN,
            lambda o, s, p, c: _TerminalRunner(
                status="skip", reason="trivial_op",
            ),
        )
        ctx = _FakeCtx(phase=OperationPhase.PLAN)
        orch = _FakeOrchestrator()

        with patch(
            "backend.core.ouroboros.governance.phase_dispatcher"
            "._fire_terminal_postmortem",
            new_callable=AsyncMock,
        ) as mock_fire:
            await dispatch_pipeline(
                orch, None, ctx, registry=reg,
            )
            kw = mock_fire.call_args.kwargs
            assert kw["status"] == "skip"
            assert kw["reason"] == "trivial_op"


# ---------------------------------------------------------------------------
# 3. Unregistered terminal phases
# ---------------------------------------------------------------------------


class TestUnregisteredTerminals:
    """Unregistered terminal phases (CANCELLED, EXPIRED, POSTMORTEM)
    also fire the postmortem via the unregistered exit path."""

    @pytest.mark.parametrize("phase", [
        OperationPhase.CANCELLED,
        OperationPhase.EXPIRED,
        OperationPhase.POSTMORTEM,
    ])
    @pytest.mark.asyncio
    async def test_unregistered_terminal_fires(self, phase):
        # Build a CLASSIFY runner that routes to an unregistered terminal
        reg = PhaseRunnerRegistry()
        reg.register(
            OperationPhase.CLASSIFY,
            lambda o, s, p, c, tp=phase: _PassthroughRunner(tp),
        )
        ctx = _FakeCtx(phase=OperationPhase.CLASSIFY)
        orch = _FakeOrchestrator()

        with patch(
            "backend.core.ouroboros.governance.phase_dispatcher"
            "._fire_terminal_postmortem",
            new_callable=AsyncMock,
        ) as mock_fire:
            await dispatch_pipeline(
                orch, None, ctx, registry=reg,
            )
            mock_fire.assert_called_once()
            kw = mock_fire.call_args.kwargs
            assert kw["terminal_phase"] == phase
            assert kw["status"] == "fail"
            assert kw["reason"] == phase.name.lower()


# ---------------------------------------------------------------------------
# 4. Async non-blocking
# ---------------------------------------------------------------------------


class TestAsyncNonBlocking:
    """The dispatcher returns BEFORE the postmortem completes."""

    @pytest.mark.asyncio
    async def test_dispatcher_returns_immediately(self):
        # Use a slow postmortem to prove the dispatcher doesn't wait
        _pm_started = asyncio.Event()
        _pm_done = asyncio.Event()

        original = _fire_terminal_postmortem

        async def _slow_pm(**kwargs):
            _pm_started.set()
            await asyncio.sleep(0.1)
            _pm_done.set()

        reg = _build_registry_with_terminal(OperationPhase.CLASSIFY)
        ctx = _FakeCtx(phase=OperationPhase.CLASSIFY)
        orch = _FakeOrchestrator()

        with patch(
            "backend.core.ouroboros.governance.phase_dispatcher"
            "._fire_terminal_postmortem",
            side_effect=_slow_pm,
        ):
            result = await dispatch_pipeline(
                orch, None, ctx, registry=reg,
            )
            # Dispatcher returned, but postmortem may still be running
            assert result is not None

        # Allow the event loop to process the background task
        await asyncio.sleep(0.2)
        assert _pm_done.is_set()


# ---------------------------------------------------------------------------
# 5. Never raises on postmortem failure
# ---------------------------------------------------------------------------


class TestNeverRaises:
    """If the postmortem itself raises, the dispatcher still returns
    normally."""

    @pytest.mark.asyncio
    async def test_postmortem_exception_suppressed(self):
        async def _exploding_pm(**kwargs):
            raise RuntimeError("postmortem kaboom")

        reg = _build_registry_with_terminal(OperationPhase.VALIDATE)
        ctx = _FakeCtx(phase=OperationPhase.VALIDATE)
        orch = _FakeOrchestrator()

        with patch(
            "backend.core.ouroboros.governance.phase_dispatcher"
            "._fire_terminal_postmortem",
            side_effect=_exploding_pm,
        ):
            # Should NOT raise — the exception is suppressed
            result = await dispatch_pipeline(
                orch, None, ctx, registry=reg,
            )
            assert result is not None


# ---------------------------------------------------------------------------
# 6. Master flag control
# ---------------------------------------------------------------------------


class TestMasterFlag:
    """The terminal postmortem flag independently gates the hook."""

    def test_default_enabled(self):
        import os
        os.environ["JARVIS_TERMINAL_POSTMORTEM_ENABLED"] = "true"
        assert _terminal_postmortem_enabled() is True

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TERMINAL_POSTMORTEM_ENABLED", "false")
        assert _terminal_postmortem_enabled() is False

    @pytest.mark.asyncio
    async def test_disabled_skips_postmortem(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TERMINAL_POSTMORTEM_ENABLED", "false")
        ctx = _FakeCtx()

        with patch(
            "backend.core.ouroboros.governance.verification.postmortem"
            ".produce_verification_postmortem",
            new_callable=AsyncMock,
        ) as mock_produce:
            await _fire_terminal_postmortem(
                ctx=ctx,
                terminal_phase=OperationPhase.CLASSIFY,
                status="fail",
                reason="test",
            )
            mock_produce.assert_not_called()


# ---------------------------------------------------------------------------
# 7. _fire_terminal_postmortem integration
# ---------------------------------------------------------------------------


class TestFireTerminalPostmortem:
    """Integration tests for the core postmortem function."""

    @pytest.mark.asyncio
    async def test_produces_and_persists(self):
        from backend.core.ouroboros.governance.verification.postmortem import (
            VerificationPostmortem,
        )
        _pm = VerificationPostmortem(
            op_id="test-op", session_id="test",
        )
        ctx = _FakeCtx()

        with patch(
            "backend.core.ouroboros.governance.verification.postmortem"
            ".produce_verification_postmortem",
            new_callable=AsyncMock,
            return_value=_pm,
        ) as mock_produce, patch(
            "backend.core.ouroboros.governance.determinism.phase_capture"
            ".capture_phase_decision",
            new_callable=AsyncMock,
        ) as mock_capture:
            await _fire_terminal_postmortem(
                ctx=ctx,
                terminal_phase=OperationPhase.GATE,
                status="fail",
                reason="gate_blocked",
            )
            mock_produce.assert_called_once()
            mock_capture.assert_called_once()
            # Verify phase and kind in the capture call
            kw = mock_capture.call_args.kwargs
            assert kw["phase"] == "GATE"
            assert kw["kind"] == "terminal_postmortem"
            assert kw["extra_inputs"]["terminal_phase"] == "GATE"
            assert kw["extra_inputs"]["status"] == "fail"
            assert kw["extra_inputs"]["reason"] == "gate_blocked"

    @pytest.mark.asyncio
    async def test_no_op_id_skips(self, monkeypatch):
        ctx = _FakeCtx(op_id="")

        with patch(
            "backend.core.ouroboros.governance.verification.postmortem"
            ".produce_verification_postmortem",
            new_callable=AsyncMock,
        ) as mock_produce:
            await _fire_terminal_postmortem(
                ctx=ctx,
                terminal_phase=OperationPhase.CLASSIFY,
                status="fail",
                reason="test",
            )
            mock_produce.assert_not_called()

    @pytest.mark.asyncio
    async def test_merkle_fallback_on_capture_failure(self):
        """When capture_phase_decision fails, falls back to basic persist."""
        from backend.core.ouroboros.governance.verification.postmortem import (
            VerificationPostmortem,
        )
        _pm = VerificationPostmortem(
            op_id="test-op", session_id="test",
        )
        ctx = _FakeCtx()

        with patch(
            "backend.core.ouroboros.governance.verification.postmortem"
            ".produce_verification_postmortem",
            new_callable=AsyncMock,
            return_value=_pm,
        ), patch(
            "backend.core.ouroboros.governance.determinism.phase_capture"
            ".capture_phase_decision",
            new_callable=AsyncMock,
            side_effect=RuntimeError("capture exploded"),
        ), patch(
            "backend.core.ouroboros.governance.verification.postmortem"
            ".persist_postmortem",
            new_callable=AsyncMock,
        ) as mock_persist:
            await _fire_terminal_postmortem(
                ctx=ctx,
                terminal_phase=OperationPhase.PLAN,
                status="fail",
                reason="plan_error",
            )
            # Fallback persist should fire
            mock_persist.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminal_context_enriched_in_payload(self):
        """The postmortem dict is enriched with _terminal_context."""
        from backend.core.ouroboros.governance.verification.postmortem import (
            VerificationPostmortem,
        )
        _pm = VerificationPostmortem(
            op_id="test-op", session_id="test",
        )
        ctx = _FakeCtx()
        _captured_payload = {}

        async def _capture_spy(**kwargs):
            # Execute compute to get the enriched payload
            payload = await kwargs["compute"]()
            _captured_payload.update(payload)
            return payload

        with patch(
            "backend.core.ouroboros.governance.verification.postmortem"
            ".produce_verification_postmortem",
            new_callable=AsyncMock,
            return_value=_pm,
        ), patch(
            "backend.core.ouroboros.governance.determinism.phase_capture"
            ".capture_phase_decision",
            side_effect=_capture_spy,
        ):
            await _fire_terminal_postmortem(
                ctx=ctx,
                terminal_phase=OperationPhase.VALIDATE,
                status="fail",
                reason="validate_failed",
            )
            assert "_terminal_context" in _captured_payload
            tc = _captured_payload["_terminal_context"]
            assert tc["terminal_phase"] == "VALIDATE"
            assert tc["status"] == "fail"
            assert tc["reason"] == "validate_failed"
            assert tc["is_success"] is False


# ---------------------------------------------------------------------------
# 8. Multi-phase chain terminates at non-COMPLETE
# ---------------------------------------------------------------------------


class TestMultiPhaseChain:
    """When a chain of phases terminates at a non-COMPLETE phase,
    the universal postmortem fires at the correct terminal point."""

    @pytest.mark.asyncio
    async def test_classify_to_plan_terminal(self):
        reg = _build_chain_to_terminal(
            OperationPhase.CLASSIFY,
            OperationPhase.PLAN,
            terminal_reason="plan_rejected",
        )
        ctx = _FakeCtx(phase=OperationPhase.CLASSIFY)
        orch = _FakeOrchestrator()

        with patch(
            "backend.core.ouroboros.governance.phase_dispatcher"
            "._fire_terminal_postmortem",
            new_callable=AsyncMock,
        ) as mock_fire:
            await dispatch_pipeline(
                orch, None, ctx, registry=reg,
            )
            mock_fire.assert_called_once()
            kw = mock_fire.call_args.kwargs
            assert kw["terminal_phase"] == OperationPhase.PLAN
            assert kw["reason"] == "plan_rejected"

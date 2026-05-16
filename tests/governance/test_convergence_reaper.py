"""Regression spine for the Convergence Reaper (P2 Slice 2).

Closes the operator's load-bearing requirement: "a synthetic op
that never self-terminates is force-converged to ``FAILED`` by
the reaper within deadline + emits the ``operation_terminal`` SSE".

Coverage axes:

  * Master gate (§33.1 default-FALSE)
  * Env knobs (tick interval + ceiling, clamps + garbage handling)
  * Closed 3-value :class:`ForcedTerminalReason` taxonomy
  * :class:`_ForcedTerminalCtxView` adapter — overrides only
    ``terminal_reason_code``/``phase``, delegates everything
    else, does NOT mutate the wrapped ctx
  * Classification: deadline_exceeded vs ceiling_exceeded vs
    in-flight (returns None)
  * Force-converge composes the canonical publisher + emits the
    SSE event with the correct reason_code
  * Force-converge with no ctx_ref (Fix-A class: fire-and-forget)
    still emits via the :class:`_MinimalCtxShim`
  * Force-converge NEVER raises on publisher failure
  * **Load-bearing**: synthetic op past deadline gets converged
    via :meth:`tick_once` AND the SSE event fires with reason
    "deadline_exceeded"
  * Async start/stop lifecycle (idempotent, cancellable)
  * 4 AST pins validate
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from backend.core.ouroboros.governance.convergence_reaper import (
    CONVERGENCE_REAPER_SCHEMA_VERSION,
    ConvergenceReaper,
    ForcedTerminalReason,
    ReaperTickResult,
    _DEFAULT_CEILING_S as _CEILING_DEFAULT,
    _DEFAULT_TICK_S as _TICK_DEFAULT,
    _ForcedTerminalCtxView,
    _MinimalCtxShim,
    default_ceiling_s,
    get_default_reaper,
    reaper_enabled,
    register_shipped_invariants,
    reset_default_reaper,
    tick_interval_s,
)
from backend.core.ouroboros.governance.in_flight_registry import (
    InFlightRegistry,
)


_MASTER_FLAG = "JARVIS_CONVERGENCE_REAPER_ENABLED"
_TICK_ENV = "JARVIS_CONVERGENCE_REAPER_TICK_S"
_CEILING_ENV = "JARVIS_CONVERGENCE_REAPER_DEFAULT_CEILING_S"


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    saved = {
        flag: os.environ.pop(flag, None)
        for flag in (_MASTER_FLAG, _TICK_ENV, _CEILING_ENV)
    }
    try:
        yield
    finally:
        for flag, prev in saved.items():
            if prev is None:
                os.environ.pop(flag, None)
            else:
                os.environ[flag] = prev
    reset_default_reaper()


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MASTER_FLAG, "true")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeCtx:
    op_id: str
    phase: Any = None
    phase_entered_at: Any = None
    terminal_reason_code: str = ""


@dataclass
class _FakeState:
    value: str


class _RecordingPublisher:
    """Spy that records every publish_operation_terminal call —
    composes the publisher's read pattern (op_id, phase, state,
    terminal_reason_code) into a typed event list."""

    def __init__(self) -> None:
        self.events: List[dict] = []

    def __call__(self, ctx: Any, state: Any) -> str:
        self.events.append({
            "op_id": getattr(ctx, "op_id", ""),
            "phase": getattr(ctx, "phase", None),
            "state_value": getattr(state, "value", ""),
            "terminal_reason_code": getattr(
                ctx, "terminal_reason_code", "",
            ),
            "phase_entered_at": getattr(
                ctx, "phase_entered_at", None,
            ),
        })
        return f"evt-{len(self.events)}"


# ---------------------------------------------------------------------------
# Master gate
# ---------------------------------------------------------------------------


class TestMasterGate:
    def test_default_false(self):
        assert reaper_enabled() is False

    def test_on(self, monkeypatch):
        _enable(monkeypatch)
        assert reaper_enabled() is True


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_tick_default(self):
        assert tick_interval_s() == _TICK_DEFAULT

    def test_tick_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv(_TICK_ENV, "not-a-number")
        assert tick_interval_s() == _TICK_DEFAULT

    def test_tick_under_min_clamps(self, monkeypatch):
        monkeypatch.setenv(_TICK_ENV, "0.1")
        assert tick_interval_s() == _TICK_DEFAULT

    def test_tick_above_max_clamps(self, monkeypatch):
        monkeypatch.setenv(_TICK_ENV, "9999")
        assert tick_interval_s() == _TICK_DEFAULT

    def test_tick_valid(self, monkeypatch):
        monkeypatch.setenv(_TICK_ENV, "5")
        assert tick_interval_s() == 5.0

    def test_ceiling_default(self):
        assert default_ceiling_s() == _CEILING_DEFAULT

    def test_ceiling_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv(_CEILING_ENV, "bad")
        assert default_ceiling_s() == _CEILING_DEFAULT

    def test_ceiling_under_min_clamps(self, monkeypatch):
        monkeypatch.setenv(_CEILING_ENV, "10")
        assert default_ceiling_s() == _CEILING_DEFAULT


# ---------------------------------------------------------------------------
# ForcedTerminalReason taxonomy
# ---------------------------------------------------------------------------


class TestForcedTerminalReason:
    def test_three_values(self):
        assert {r.value for r in ForcedTerminalReason} == {
            "deadline_exceeded",
            "ceiling_exceeded",
            "registry_purged",
        }


# ---------------------------------------------------------------------------
# _ForcedTerminalCtxView
# ---------------------------------------------------------------------------


class TestForcedTerminalCtxView:
    def test_overrides_reason_code_only(self):
        real = _FakeCtx(
            op_id="op-x", phase="GENERATE",
            phase_entered_at="2026-05-16T15:00:00",
            terminal_reason_code="original",
        )
        view = _ForcedTerminalCtxView(
            real, reason_code="deadline_exceeded",
        )
        # Reason overridden.
        assert view.terminal_reason_code == "deadline_exceeded"
        # Everything else delegated.
        assert view.op_id == "op-x"
        assert view.phase_entered_at == "2026-05-16T15:00:00"

    def test_phase_inherited_from_real_by_default(self):
        real = _FakeCtx(op_id="x", phase="APPLY")
        view = _ForcedTerminalCtxView(
            real, reason_code="x",
        )
        assert view.phase == "APPLY"

    def test_phase_override(self):
        real = _FakeCtx(op_id="x", phase="APPLY")
        view = _ForcedTerminalCtxView(
            real, reason_code="x", phase_override="POSTMORTEM",
        )
        assert view.phase == "POSTMORTEM"

    def test_does_not_mutate_real_ctx(self):
        real = _FakeCtx(
            op_id="x", terminal_reason_code="original",
        )
        _ForcedTerminalCtxView(
            real, reason_code="overridden",
        )
        # Real ctx untouched.
        assert real.terminal_reason_code == "original"


# ---------------------------------------------------------------------------
# Minimal ctx shim (no ctx_ref path)
# ---------------------------------------------------------------------------


class TestMinimalCtxShim:
    def test_minimal_attributes(self):
        shim = _MinimalCtxShim(op_id="shim-1")
        assert shim.op_id == "shim-1"
        assert shim.phase is None
        assert shim.phase_entered_at is None
        assert shim.terminal_reason_code == ""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassification:
    def test_in_flight_returns_none(self, monkeypatch):
        _enable(monkeypatch)
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )
        # Op with future deadline.
        registry.register(
            "op-fresh",
            deadline_monotonic=time.monotonic() + 100,
        )
        result = r.tick_once()
        assert result.converged_count == 0
        assert pub.events == []

    def test_deadline_exceeded(self, monkeypatch):
        _enable(monkeypatch)
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )
        registry.register(
            "op-stuck",
            deadline_monotonic=time.monotonic() - 5,
            ctx_ref=_FakeCtx(op_id="op-stuck"),
        )
        result = r.tick_once()
        assert result.converged_count == 1
        assert result.converged_op_ids == ("op-stuck",)
        assert (
            ForcedTerminalReason.DEADLINE_EXCEEDED
            in result.reasons
        )
        assert len(pub.events) == 1
        assert (
            pub.events[0]["terminal_reason_code"]
            == "deadline_exceeded"
        )

    def test_ceiling_exceeded(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv(_CEILING_ENV, "60")
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )
        # Op registered "70s ago" via time-travel — no explicit
        # deadline. Reaper falls back to ceiling.
        now = time.monotonic()
        registry.register(
            "op-old", ctx_ref=_FakeCtx(op_id="op-old"),
        )
        # Mock the registered record by pulling it + overwriting
        # started_at via registering a synthetic with same op_id
        # is messy; instead, tick with a manipulated now value.
        result = r.tick_once(now_monotonic=now + 70)
        assert result.converged_count == 1
        assert (
            ForcedTerminalReason.CEILING_EXCEEDED
            in result.reasons
        )
        assert (
            pub.events[0]["terminal_reason_code"]
            == "ceiling_exceeded"
        )

    def test_deadline_takes_priority_over_ceiling(
        self, monkeypatch,
    ):
        """An op with both an explicit deadline AND age past
        ceiling reports deadline_exceeded (the more specific
        reason)."""
        _enable(monkeypatch)
        monkeypatch.setenv(_CEILING_ENV, "60")
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )
        now = time.monotonic()
        registry.register(
            "op-both",
            deadline_monotonic=now - 5,  # past
            ctx_ref=_FakeCtx(op_id="op-both"),
        )
        result = r.tick_once(now_monotonic=now + 70)
        assert result.reasons == (
            ForcedTerminalReason.DEADLINE_EXCEEDED,
        )


# ---------------------------------------------------------------------------
# Force-converge details
# ---------------------------------------------------------------------------


class TestForceConverge:
    def test_publish_uses_failed_state(self, monkeypatch):
        _enable(monkeypatch)
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        failed = _FakeState(value="failed")
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=failed,
        )
        registry.register(
            "op-x",
            deadline_monotonic=time.monotonic() - 1,
            ctx_ref=_FakeCtx(op_id="op-x"),
        )
        r.tick_once()
        assert pub.events[0]["state_value"] == "failed"

    def test_op_unregistered_after_converge(self, monkeypatch):
        _enable(monkeypatch)
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )
        registry.register(
            "op-x",
            deadline_monotonic=time.monotonic() - 1,
            ctx_ref=_FakeCtx(op_id="op-x"),
        )
        r.tick_once()
        # Unregistered → next tick is a no-op.
        assert registry.lookup("op-x") is None
        result2 = r.tick_once()
        assert result2.converged_count == 0

    def test_no_ctx_ref_uses_minimal_shim(self, monkeypatch):
        """Fix-A class: fire-and-forget ops register without a
        ctx_ref. The reaper must still emit the SSE so observers
        see convergence."""
        _enable(monkeypatch)
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )
        registry.register(
            "op-shimmed",
            deadline_monotonic=time.monotonic() - 1,
            # No ctx_ref.
        )
        r.tick_once()
        assert len(pub.events) == 1
        assert pub.events[0]["op_id"] == "op-shimmed"

    def test_publisher_raising_does_not_break_reaper(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        registry = InFlightRegistry()

        def _boom(ctx, state):  # noqa: ARG001
            raise RuntimeError("publisher crashed")

        r = ConvergenceReaper(
            registry=registry,
            publish_fn=_boom,
            operation_state_failed=_FakeState(value="failed"),
        )
        registry.register(
            "op-boom",
            deadline_monotonic=time.monotonic() - 1,
            ctx_ref=_FakeCtx(op_id="op-boom"),
        )
        # MUST NOT raise.
        result = r.tick_once()
        # Convergence "failed" — count stays 0; op stays
        # registered for retry on the next tick.
        assert result.converged_count == 0
        assert registry.lookup("op-boom") is not None

    def test_master_off_skips_tick(self):
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )
        registry.register(
            "op-x",
            deadline_monotonic=time.monotonic() - 1,
            ctx_ref=_FakeCtx(op_id="op-x"),
        )
        result = r.tick_once()  # master OFF
        assert result.skipped_master_off is True
        assert result.converged_count == 0
        assert pub.events == []


# ---------------------------------------------------------------------------
# LOAD-BEARING: synthetic-op end-to-end (operator's invariant)
# ---------------------------------------------------------------------------


class TestLoadBearingConvergence:
    """The operator's exact invariant: "a synthetic op which
    never self-terminates is force-converged to failed by the
    reaper within deadline + emits the SSE"."""

    def test_synthetic_hang_converges_within_deadline(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )

        # Synthetic op with a deadline that's already in the
        # past — simulates "stuck for >deadline seconds".
        now = time.monotonic()
        synthetic_op_id = "synthetic-hung-op-001"
        registry.register(
            synthetic_op_id,
            deadline_monotonic=now - 0.001,
            ctx_ref=_FakeCtx(
                op_id=synthetic_op_id,
                phase="GENERATE",
                phase_entered_at="2026-05-16T15:00:00",
                terminal_reason_code="",
            ),
            last_phase_name="generate",
            metadata={"source": "synthetic_test"},
        )
        assert registry.size() == 1

        # Reaper tick — single sweep is the same path the async
        # loop runs.
        result = r.tick_once()

        # Convergence happened.
        assert result.converged_count == 1
        assert result.converged_op_ids == (synthetic_op_id,)
        assert result.reasons == (
            ForcedTerminalReason.DEADLINE_EXCEEDED,
        )

        # SSE event emitted (the load-bearing observability
        # claim).
        assert len(pub.events) == 1
        evt = pub.events[0]
        assert evt["op_id"] == synthetic_op_id
        assert evt["state_value"] == "failed"
        assert evt["terminal_reason_code"] == "deadline_exceeded"

        # Registry purged — the op cannot re-converge.
        assert registry.lookup(synthetic_op_id) is None


# ---------------------------------------------------------------------------
# Async lifecycle
# ---------------------------------------------------------------------------


class TestAsyncLifecycle:
    @pytest.mark.asyncio
    async def test_start_when_master_off_is_silent_noop(self):
        registry = InFlightRegistry()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=_RecordingPublisher(),
            operation_state_failed=_FakeState(value="failed"),
        )
        r.start()  # master OFF
        assert r.is_running() is False
        await r.stop()  # idempotent

    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv(_TICK_ENV, "1")
        registry = InFlightRegistry()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=_RecordingPublisher(),
            operation_state_failed=_FakeState(value="failed"),
        )
        r.start()
        assert r.is_running()
        # Re-start is silent.
        r.start()
        assert r.is_running()
        await r.stop()
        assert not r.is_running()
        # Re-stop is silent.
        await r.stop()

    @pytest.mark.asyncio
    async def test_background_loop_converges(self, monkeypatch):
        """Schedule the background task and prove it converges
        an aged op without an explicit tick_once call."""
        _enable(monkeypatch)
        # Aggressive tick so the test finishes fast.
        monkeypatch.setenv(_TICK_ENV, "1")
        registry = InFlightRegistry()
        pub = _RecordingPublisher()
        r = ConvergenceReaper(
            registry=registry,
            publish_fn=pub,
            operation_state_failed=_FakeState(value="failed"),
        )
        registry.register(
            "op-bg",
            deadline_monotonic=time.monotonic() - 1,
            ctx_ref=_FakeCtx(op_id="op-bg"),
        )
        r.start()
        try:
            # Wait long enough for one tick.
            for _ in range(30):
                if registry.lookup("op-bg") is None:
                    break
                await asyncio.sleep(0.1)
            assert registry.lookup("op-bg") is None, (
                "background reaper failed to converge in 3s"
            )
            assert len(pub.events) == 1
        finally:
            await r.stop()


# ---------------------------------------------------------------------------
# ReaperTickResult shape
# ---------------------------------------------------------------------------


class TestReaperTickResult:
    def test_frozen(self):
        r = ReaperTickResult(
            inspected_count=1, converged_count=1,
        )
        with pytest.raises(Exception):
            r.inspected_count = 9  # type: ignore[misc]

    def test_carries_schema_version(self):
        r = ReaperTickResult(
            inspected_count=0, converged_count=0,
        )
        assert r.schema_version == (
            CONVERGENCE_REAPER_SCHEMA_VERSION
        )


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_returns_same_instance(self):
        a = get_default_reaper()
        b = get_default_reaper()
        assert a is b

    def test_reset_drops(self):
        a = get_default_reaper()
        reset_default_reaper()
        b = get_default_reaper()
        assert a is not b


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_returns_four_pins(self):
        pins = register_shipped_invariants()
        names = {p.invariant_name for p in pins}
        assert names == {
            "convergence_reaper_master_default_false",
            "convergence_reaper_reason_taxonomy_closed",
            "convergence_reaper_single_convergence_seam",
            "convergence_reaper_composes_canonical_publisher",
        }

    def test_pins_pass_on_current_source(self):
        import ast
        pins = register_shipped_invariants()
        src = Path(
            "backend/core/ouroboros/governance/"
            "convergence_reaper.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for pin in pins:
            violations = pin.validate(tree, src)
            assert violations == (), (
                f"{pin.invariant_name} drift: {violations}"
            )

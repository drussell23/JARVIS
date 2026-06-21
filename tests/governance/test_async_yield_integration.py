"""Layer-2 cross-component integration tests — Sovereign Async Yield Matrix (Task 9).

Covers the full cross-component contract across:
  - mutation_critical_section  (LR-B drain gate)
  - operator_presence          (edge-trigger detection)
  - operator_yield_bridge      (suspend flag + resume dispatch)
  - op_park_store              (should_park_for_route)
  - sensor_governor            (operator hard-zero)

All tests are deterministic.  No real bus, no real event loop scheduling
beyond the coroutines under test.  Fakes are local (or reused from the
bridge test surface in _FakeCtx / _FakeBgOp / _FakePool).

OFF byte-identical note (Test 5)
---------------------------------
When JARVIS_OPERATOR_YIELD_ENABLED is false (the default):
  * operator_suspended() → always False, even after set_operator_active()
  * _operator_hard_zero(True) → False
  * should_park_for_route(..., operator_suspended=False) == legacy decision
  * OperatorPresenceWatcher.run() returns immediately (no-op)

Layer 1's deterministic mutation lock (mutation_section / maybe_mutation_section)
is separate and stays active regardless of this env flag.  That path is fully
tested in test_mutation_critical_section.py; we do not duplicate it here.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, List, Tuple

import pytest

import backend.core.ouroboros.governance.operator_yield_bridge as bridge
import backend.core.ouroboros.governance.sensor_governor as sg
from backend.core.ouroboros.governance.mutation_critical_section import (
    drain,
    is_mutating,
    mutation_section,
)
from backend.core.ouroboros.governance.op_park_store import should_park_for_route
from backend.core.ouroboros.governance.operator_presence import (
    OperatorPresenceWatcher,
    _is_present,
)
from backend.core.ouroboros.governance.sensor_governor import (
    BudgetDecision,
    SensorBudgetSpec,
    SensorGovernor,
    Urgency,
    reset_default_governor,
)


# ---------------------------------------------------------------------------
# Shared fakes (mirror test_operator_yield_bridge.py surface)
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self, op_id: str, route: str = "background") -> None:
        self.op_id = op_id
        self.provider_route = route


class _FakeBgOp:
    def __init__(self, op_id: str, status: str, context: Any) -> None:
        self.op_id = op_id
        self.status = status
        self.context = context
        self.park_attempt_seq = 1


class _FakePool:
    """Minimal stand-in exposing the surface the bridge needs."""

    def __init__(self, parked: List[_FakeBgOp]) -> None:
        self._parked = parked
        self._resumed_ops: dict = {}
        self.submitted: List[Tuple[str, int]] = []

    def list_all(self):
        return list(self._parked)

    def is_resumed_dispatch(self, ctx_op_id: str) -> bool:
        return ctx_op_id in self._resumed_ops

    async def submit_for_resume(self, ctx, *, attempt_seq: int) -> str:
        op_id = str(getattr(ctx, "op_id", "") or "")
        self.submitted.append((op_id, attempt_seq))
        self._resumed_ops[op_id] = attempt_seq
        return f"pool-{op_id}"


# ---------------------------------------------------------------------------
# Module-level autouse — always reset the bridge suspend flag
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bridge(monkeypatch):
    """Clear the module suspend flag before and after each test."""
    bridge.set_operator_idle()
    yield
    bridge.set_operator_idle()


@pytest.fixture(autouse=True)
def _reset_governor():
    """Reset governor singleton so state doesn't leak between tests."""
    reset_default_governor()
    yield
    reset_default_governor()


# ---------------------------------------------------------------------------
# Test 1: Drain-gates-park (LR-B)
# ---------------------------------------------------------------------------


class TestDrainGatesPark:
    """While an active mutation_section is held, drain() returns False
    (would-abandon), which means the yield path must NOT park.  Once the
    section exits, drain() returns True — safe to park."""

    def test_drain_returns_false_while_mutating(self):
        """drain() times out (returns False) when a mutation_section is active."""

        async def _run():
            op_id = "op-drain-test-001"
            async with mutation_section(op_id):
                # Confirm is_mutating reflects the active section
                assert is_mutating(op_id) is True
                # drain with a tiny timeout: should NOT wait long enough for
                # the section to exit, so it returns False (wedged → abandon)
                result = await drain(op_id, timeout=0.05, poll_s=0.01)
                assert result is False, "drain must return False while section active"
                # is_mutating still True inside the section
                assert is_mutating(op_id) is True
            return True

        result = asyncio.run(_run())
        assert result is True

    def test_drain_returns_true_after_mutation_section_exits(self):
        """drain() returns True immediately when no mutation_section is held."""

        async def _run():
            op_id = "op-drain-test-002"
            async with mutation_section(op_id):
                pass  # enter and immediately exit
            # After the section exits is_mutating must be False
            assert is_mutating(op_id) is False
            result = await drain(op_id, timeout=0.5, poll_s=0.01)
            assert result is True, "drain must return True when no section active"

        asyncio.run(_run())

    def test_drain_safe_to_park_after_section_exits(self):
        """Once drain() returns True, should_park_for_route can proceed.

        This composes the drain guard with the park decision: the yield
        path first awaits drain(); only when it returns True is it safe
        to evaluate should_park_for_route with operator_suspended=True.
        """

        async def _run():
            op_id = "op-drain-test-003"
            os.environ["JARVIS_BG_PARK_ENABLED"] = "true"
            os.environ["JARVIS_OPERATOR_YIELD_ENABLED"] = "true"
            try:
                async with mutation_section(op_id):
                    # Would-abandon path: drain inside section → False → don't park
                    drained = await drain(op_id, timeout=0.05, poll_s=0.01)
                    assert drained is False
                # After section exits: drain → True → safe to evaluate park
                drained = await drain(op_id, timeout=0.5, poll_s=0.01)
                assert drained is True
                # With operator suspended and a park-supported route, should park
                decision = should_park_for_route(
                    "background",
                    queue_pressure=False,
                    operator_suspended=True,
                )
                assert decision is True, (
                    "should_park_for_route must return True on supported route "
                    "when operator_suspended=True and park enabled"
                )
            finally:
                os.environ.pop("JARVIS_BG_PARK_ENABLED", None)
                os.environ.pop("JARVIS_OPERATOR_YIELD_ENABLED", None)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 2: Presence → suspend → park decision
# ---------------------------------------------------------------------------


class TestPresenceSuspendPark:
    """set_operator_active() (yield enabled) → operator_suspended() True
    → should_park_for_route returns True even without queue pressure.
    set_operator_idle() → operator_suspended() False → legacy decision."""

    def test_active_suspends_and_parks(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

        bridge.set_operator_active()
        assert bridge.operator_suspended() is True

        # Park-supported route (background), no queue pressure — still parks
        # because operator_suspended trumps queue pressure
        result = should_park_for_route(
            "background",
            queue_pressure=False,
            operator_suspended=bridge.operator_suspended(),
        )
        assert result is True, "must park on background route when operator active"

    def test_active_parks_on_standard_route(self, monkeypatch):
        """standard is in _PARK_SUPPORTED_ROUTES (batch-capable) so parks."""
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

        bridge.set_operator_active()
        result = should_park_for_route(
            "standard",
            queue_pressure=False,
            operator_suspended=bridge.operator_suspended(),
        )
        assert result is True

    def test_idle_reverts_to_legacy_no_park_without_pressure(self, monkeypatch):
        """After set_operator_idle() operator_suspended() → False.
        Without queue pressure, should_park_for_route reverts to False (legacy)."""
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

        bridge.set_operator_active()
        assert bridge.operator_suspended() is True

        bridge.set_operator_idle()
        assert bridge.operator_suspended() is False

        result = should_park_for_route(
            "background",
            queue_pressure=False,
            operator_suspended=bridge.operator_suspended(),
        )
        assert result is False, "must NOT park when operator idle + no queue pressure"

    def test_immediate_route_never_parks_even_when_suspended(self, monkeypatch):
        """IMMEDIATE is not in _PARK_SUPPORTED_ROUTES → never parks."""
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")

        bridge.set_operator_active()
        result = should_park_for_route(
            "immediate",
            queue_pressure=True,
            operator_suspended=bridge.operator_suspended(),
        )
        assert result is False, "IMMEDIATE route must never park"

    def test_presence_is_pure_deterministic(self):
        """_is_present is a pure function: injected monotonic timestamps."""
        # Operator present when elapsed < idle_threshold (default 45s)
        assert _is_present(last_input_monotonic=100.0, now=140.0) is True
        # Operator absent when elapsed >= idle_threshold
        assert _is_present(last_input_monotonic=100.0, now=146.0) is False

    def test_presence_liveness_overrides_stale_timestamp(self):
        """A truthy liveness probe overrides a stale timestamp."""
        # Elapsed 200s → would be absent, but liveness returns True
        assert _is_present(
            last_input_monotonic=100.0,
            now=300.0,
            liveness=lambda: True,
        ) is True

    def test_presence_liveness_exception_is_fail_soft(self):
        """A crashing liveness probe is swallowed; falls through to timestamp."""
        def _bad_liveness():
            raise RuntimeError("boom")

        # elapsed 200s → absent timestamp; probe raises → still absent
        assert _is_present(
            last_input_monotonic=100.0,
            now=300.0,
            liveness=_bad_liveness,
        ) is False


# ---------------------------------------------------------------------------
# Test 3: Governor hard-zero on active
# ---------------------------------------------------------------------------


class TestGovernorHardZeroOnActive:
    """SensorGovernor(operator_active_fn=lambda: True) (yield enabled)
    denies a budget request. With lambda: False it does not."""

    def _make_governor(self, active_fn) -> SensorGovernor:
        g = SensorGovernor(operator_active_fn=active_fn)
        g.register(SensorBudgetSpec(sensor_name="TestSensor", base_cap_per_hour=100))
        return g

    def test_hard_zero_denies_when_active(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")

        gov = self._make_governor(lambda: True)
        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is False
        assert decision.reason_code == "governor.operator_active_yield"

    def test_hard_zero_not_triggered_when_inactive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")

        gov = self._make_governor(lambda: False)
        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        # Operator inactive → hard-zero does not fire; budget should be allowed
        assert decision.allowed is True

    def test_hard_zero_not_triggered_when_yield_disabled(self, monkeypatch):
        """Even with operator_active_fn returning True, if yield flag is off
        the hard-zero never fires (byte-identical pre-Task-7 behavior)."""
        monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")

        gov = self._make_governor(lambda: True)
        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is True, (
            "hard-zero must not fire when JARVIS_OPERATOR_YIELD_ENABLED is off"
        )

    def test_hard_zero_pure_helper_on(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        assert sg._operator_hard_zero(True) is True

    def test_hard_zero_pure_helper_off_by_flag(self, monkeypatch):
        monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)
        assert sg._operator_hard_zero(True) is False

    def test_hard_zero_pure_helper_inactive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        assert sg._operator_hard_zero(False) is False

    def test_hard_zero_no_fn_never_fires(self, monkeypatch):
        """No operator_active_fn → hard-zero path not reached."""
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")

        gov = SensorGovernor()  # no operator_active_fn
        gov.register(SensorBudgetSpec(sensor_name="TestSensor", base_cap_per_hour=100))
        decision = gov.request_budget("TestSensor")
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Test 4: Resume on idle
# ---------------------------------------------------------------------------


class TestResumeOnIdle:
    """on_operator_idle with a fake pool (parked ops) calls submit_for_resume
    for each parked op."""

    def test_on_operator_idle_resumes_parked_ops(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        ctx_a = _FakeCtx("op-a")
        ctx_b = _FakeCtx("op-b")
        parked_ops = [
            _FakeBgOp("op-a", "parked", ctx_a),
            _FakeBgOp("op-b", "parked", ctx_b),
        ]
        pool = _FakePool(parked_ops)

        async def _run():
            await bridge.on_operator_idle(pool=pool)

        asyncio.run(_run())

        submitted_op_ids = {op_id for op_id, _ in pool.submitted}
        assert "op-a" in submitted_op_ids
        assert "op-b" in submitted_op_ids
        assert len(pool.submitted) == 2

    def test_on_operator_idle_skips_non_parked(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        ctx_a = _FakeCtx("op-running")
        ctx_b = _FakeCtx("op-parked")
        ops = [
            _FakeBgOp("op-running", "running", ctx_a),
            _FakeBgOp("op-parked", "parked", ctx_b),
        ]
        pool = _FakePool(ops)

        async def _run():
            await bridge.on_operator_idle(pool=pool)

        asyncio.run(_run())

        submitted_op_ids = {op_id for op_id, _ in pool.submitted}
        assert "op-parked" in submitted_op_ids
        assert "op-running" not in submitted_op_ids

    def test_on_operator_idle_skips_already_resumed(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        ctx_a = _FakeCtx("op-already-resumed")
        ops = [_FakeBgOp("op-already-resumed", "parked", ctx_a)]
        pool = _FakePool(ops)
        # Pre-mark as already in resume dispatch
        pool._resumed_ops["op-already-resumed"] = 1

        async def _run():
            await bridge.on_operator_idle(pool=pool)

        asyncio.run(_run())

        assert len(pool.submitted) == 0, (
            "should not double-dispatch an op already in resume"
        )

    def test_on_operator_idle_clears_suspend_flag(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        bridge.set_operator_active()
        assert bridge.operator_suspended() is True

        pool = _FakePool([])

        async def _run():
            await bridge.on_operator_idle(pool=pool)

        asyncio.run(_run())
        assert bridge.operator_suspended() is False

    def test_on_operator_idle_noop_when_yield_disabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)

        # Set raw flag but yield is off, so on_operator_idle is a no-op
        bridge.set_operator_active()

        ctx = _FakeCtx("op-x")
        pool = _FakePool([_FakeBgOp("op-x", "parked", ctx)])

        async def _run():
            await bridge.on_operator_idle(pool=pool)

        asyncio.run(_run())

        # No submissions — bridge is inactive
        assert len(pool.submitted) == 0


# ---------------------------------------------------------------------------
# Test 5: OFF byte-identical (CRITICAL)
# ---------------------------------------------------------------------------


class TestOffByteIdentical:
    """With JARVIS_OPERATOR_YIELD_ENABLED=false, all Layer-2 yield surfaces
    are dormant: operator_suspended() is always False, _operator_hard_zero
    never fires, should_park_for_route behaves as legacy, and
    OperatorPresenceWatcher.run() is a no-op.

    NOTE: Layer 1's deterministic mutation lock (mutation_section /
    maybe_mutation_section) is separate and stays active regardless of this
    flag — tested in test_mutation_critical_section.py.
    """

    @pytest.fixture(autouse=True)
    def _yield_disabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)

    def test_operator_suspended_always_false_when_disabled(self):
        # Even after set_operator_active(), operator_suspended() returns False
        bridge.set_operator_active()
        assert bridge.operator_suspended() is False, (
            "operator_suspended() must return False when yield is disabled"
        )

    def test_set_operator_active_then_idle_both_safe_when_disabled(self):
        # Should not raise; just no-op from the gate perspective
        bridge.set_operator_active()
        bridge.set_operator_idle()
        assert bridge.operator_suspended() is False

    def test_operator_hard_zero_false_when_disabled(self):
        # _operator_hard_zero(True) → False when yield flag is off
        assert sg._operator_hard_zero(True) is False

    def test_should_park_for_route_legacy_when_disabled(self, monkeypatch):
        """should_park_for_route with operator_suspended=False (the only value
        possible when yield is off) falls through to legacy behavior.
        Legacy with park disabled → always False."""
        monkeypatch.delenv("JARVIS_BG_PARK_ENABLED", raising=False)
        # operator_suspended() is False (feature off); pass that value
        result = should_park_for_route(
            "background",
            queue_pressure=True,
            operator_suspended=bridge.operator_suspended(),  # False
        )
        assert result is False, (
            "should_park_for_route must respect legacy decision (park disabled)"
        )

    def test_should_park_for_route_legacy_with_park_enabled(self, monkeypatch):
        """With park enabled but operator_suspended=False (yield off), park
        returns True only when route is eligible AND queue_pressure is True."""
        monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
        # operator_suspended is False (yield disabled)
        result = should_park_for_route(
            "background",
            queue_pressure=True,
            operator_suspended=False,
        )
        # background is in the default eligible routes — must park under pressure
        assert result is True, (
            "legacy path: eligible route + queue_pressure=True should park"
        )
        result_no_pressure = should_park_for_route(
            "background",
            queue_pressure=False,
            operator_suspended=False,
        )
        assert result_no_pressure is False, (
            "legacy path: no queue pressure → no park"
        )

    def test_presence_watcher_run_is_noop_when_disabled(self):
        """OperatorPresenceWatcher.run() returns immediately when yield is off."""

        async def _run():
            watcher = OperatorPresenceWatcher()
            # run() is gated on JARVIS_OPERATOR_YIELD_ENABLED; with it off,
            # it should return without entering the while loop.
            import asyncio
            try:
                await asyncio.wait_for(watcher.run(bus=None), timeout=0.2)
            except asyncio.TimeoutError:
                pytest.fail(
                    "OperatorPresenceWatcher.run() should return immediately "
                    "when JARVIS_OPERATOR_YIELD_ENABLED is false"
                )

        asyncio.run(_run())

    def test_on_operator_active_noop_when_disabled(self):
        """on_operator_active is a no-op when yield is disabled."""

        async def _run():
            await bridge.on_operator_active()

        asyncio.run(_run())
        # Suspend flag should remain False
        assert bridge.operator_suspended() is False

    def test_governor_hard_zero_not_fired_when_disabled(self, monkeypatch):
        """SensorGovernor with operator_active_fn=lambda: True does NOT
        deny budget when JARVIS_OPERATOR_YIELD_ENABLED is off."""
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        # yield flag is delenv'd by the autouse fixture

        gov = SensorGovernor(operator_active_fn=lambda: True)
        gov.register(SensorBudgetSpec(sensor_name="TestSensor", base_cap_per_hour=100))
        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is True, (
            "governor must not hard-zero when JARVIS_OPERATOR_YIELD_ENABLED is off"
        )

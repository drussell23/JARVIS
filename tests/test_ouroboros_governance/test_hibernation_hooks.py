"""Tests for the HIBERNATION hook bridge — HIBERNATION_MODE step 6.5.

Covers:
- Registration / unregistration / dedup semantics.
- Hooks fire AFTER the mode flip (observe new state).
- Sync and async hooks both work.
- Multiple hooks fire in registration order.
- One failing hook does not prevent others from firing.
- Per-hook timeout enforcement via _hook_timeout_s.
- Transition return value is unchanged by hook outcomes.
- No-op transitions (already hibernating / not hibernating) do NOT re-fire.
- emergency_stop from HIBERNATION fires the wake hooks.
- stop() clears hooks so rebuilt fixtures start clean.
- Env var JARVIS_HIBERNATION_HOOK_TIMEOUT_S resolution.
- Observability snapshot.
- End-to-end bridge: real controller + real BackgroundAgentPool +
  real IdleWatchdog, hibernation pauses/freezes both, wake restores.
"""
from __future__ import annotations

import asyncio
from typing import List

import pytest

from backend.core.ouroboros.governance.supervisor_controller import (
    _DEFAULT_HOOK_TIMEOUT_S,
    _ENV_HOOK_TIMEOUT,
    AutonomyMode,
    SupervisorOuroborosController,
    _call_maybe_async,
    _resolve_hook_timeout,
)


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------


class TestResolveHookTimeout:
    def test_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv(_ENV_HOOK_TIMEOUT, raising=False)
        assert _resolve_hook_timeout() == _DEFAULT_HOOK_TIMEOUT_S

    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv(_ENV_HOOK_TIMEOUT, "2.5")
        assert _resolve_hook_timeout() == 2.5

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv(_ENV_HOOK_TIMEOUT, "oops")
        assert _resolve_hook_timeout() == _DEFAULT_HOOK_TIMEOUT_S

    def test_non_positive_falls_back(self, monkeypatch):
        monkeypatch.setenv(_ENV_HOOK_TIMEOUT, "0")
        assert _resolve_hook_timeout() == _DEFAULT_HOOK_TIMEOUT_S
        monkeypatch.setenv(_ENV_HOOK_TIMEOUT, "-3.5")
        assert _resolve_hook_timeout() == _DEFAULT_HOOK_TIMEOUT_S


# ---------------------------------------------------------------------------
# _call_maybe_async helper
# ---------------------------------------------------------------------------


class TestCallMaybeAsync:
    @pytest.mark.asyncio
    async def test_sync_callable(self):
        calls = []

        def hook(*, reason: str) -> str:
            calls.append(reason)
            return "sync-result"

        result = await _call_maybe_async(hook, reason="ok")
        assert result == "sync-result"
        assert calls == ["ok"]

    @pytest.mark.asyncio
    async def test_async_def_callable(self):
        calls = []

        async def hook(*, reason: str) -> str:
            calls.append(reason)
            return "async-result"

        result = await _call_maybe_async(hook, reason="ok")
        assert result == "async-result"
        assert calls == ["ok"]

    @pytest.mark.asyncio
    async def test_sync_returning_coroutine(self):
        """A lambda that defers to an async impl — common adapter pattern."""
        calls = []

        async def _impl(reason: str) -> str:
            calls.append(reason)
            return "lambda-result"

        hook = lambda *, reason: _impl(reason)  # noqa: E731
        result = await _call_maybe_async(hook, reason="ok")
        assert result == "lambda-result"
        assert calls == ["ok"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_stores_hooks(self):
        ctrl = SupervisorOuroborosController()
        on_h = lambda *, reason: None  # noqa: E731
        on_w = lambda *, reason: None  # noqa: E731
        ctrl.register_hibernation_hooks(
            on_hibernate=on_h, on_wake=on_w, name="test",
        )
        snap = ctrl.hibernation_hook_snapshot()
        assert snap["hibernate_hooks_registered"] == 1
        assert snap["wake_hooks_registered"] == 1

    @pytest.mark.asyncio
    async def test_register_is_idempotent(self):
        """Same callable registered twice should only fire once."""
        ctrl = SupervisorOuroborosController()
        calls = []

        def on_h(*, reason: str) -> None:
            calls.append(reason)

        ctrl.register_hibernation_hooks(on_hibernate=on_h, name="first")
        ctrl.register_hibernation_hooks(on_hibernate=on_h, name="dedup")
        assert ctrl.hibernation_hook_snapshot()["hibernate_hooks_registered"] == 1

        await ctrl.start()
        await ctrl.enter_hibernation("outage")
        assert calls == ["outage"]  # fired once, not twice

    @pytest.mark.asyncio
    async def test_register_multiple_distinct_hooks(self):
        ctrl = SupervisorOuroborosController()
        seen: List[str] = []

        def h1(*, reason: str) -> None:
            seen.append(f"h1:{reason}")

        def h2(*, reason: str) -> None:
            seen.append(f"h2:{reason}")

        def h3(*, reason: str) -> None:
            seen.append(f"h3:{reason}")

        ctrl.register_hibernation_hooks(on_hibernate=h1, name="a")
        ctrl.register_hibernation_hooks(on_hibernate=h2, name="b")
        ctrl.register_hibernation_hooks(on_hibernate=h3, name="c")
        await ctrl.start()
        await ctrl.enter_hibernation("dw down")
        assert seen == ["h1:dw down", "h2:dw down", "h3:dw down"]

    @pytest.mark.asyncio
    async def test_unregister_removes(self):
        ctrl = SupervisorOuroborosController()
        calls = []
        on_h = lambda *, reason: calls.append(reason)  # noqa: E731
        ctrl.register_hibernation_hooks(on_hibernate=on_h, name="t")
        ctrl.unregister_hibernation_hooks(on_hibernate=on_h)
        assert ctrl.hibernation_hook_snapshot()["hibernate_hooks_registered"] == 0

        await ctrl.start()
        await ctrl.enter_hibernation("outage")
        assert calls == []

    @pytest.mark.asyncio
    async def test_unregister_missing_is_noop(self):
        ctrl = SupervisorOuroborosController()
        never_registered = lambda *, reason: None  # noqa: E731
        ctrl.unregister_hibernation_hooks(on_hibernate=never_registered)

    @pytest.mark.asyncio
    async def test_clear_drops_all(self):
        ctrl = SupervisorOuroborosController()
        ctrl.register_hibernation_hooks(
            on_hibernate=lambda *, reason: None,
            on_wake=lambda *, reason: None,
            name="t",
        )
        ctrl.clear_hibernation_hooks()
        snap = ctrl.hibernation_hook_snapshot()
        assert snap["hibernate_hooks_registered"] == 0
        assert snap["wake_hooks_registered"] == 0


# ---------------------------------------------------------------------------
# Hook fire semantics
# ---------------------------------------------------------------------------


class TestHookFire:
    @pytest.mark.asyncio
    async def test_hooks_fire_after_mode_flip(self):
        """Hooks observe the NEW mode, proving they run after the flip."""
        ctrl = SupervisorOuroborosController()
        observed_modes: List[AutonomyMode] = []

        def on_h(*, reason: str) -> None:
            observed_modes.append(ctrl.mode)

        ctrl.register_hibernation_hooks(on_hibernate=on_h, name="t")
        await ctrl.start()
        await ctrl.enter_hibernation("outage")
        assert observed_modes == [AutonomyMode.HIBERNATION]

    @pytest.mark.asyncio
    async def test_wake_hooks_fire_after_restore(self):
        ctrl = SupervisorOuroborosController()
        observed_modes: List[AutonomyMode] = []

        async def on_w(*, reason: str) -> None:
            observed_modes.append(ctrl.mode)

        ctrl.register_hibernation_hooks(on_wake=on_w, name="t")
        await ctrl.start()  # → SANDBOX
        await ctrl.enter_hibernation("outage")
        await ctrl.wake_from_hibernation(reason="back")
        # Pre-hibernation mode was SANDBOX → restored to SANDBOX.
        assert observed_modes == [AutonomyMode.SANDBOX]

    @pytest.mark.asyncio
    async def test_already_hibernating_does_not_refire(self):
        ctrl = SupervisorOuroborosController()
        calls: List[str] = []
        ctrl.register_hibernation_hooks(
            on_hibernate=lambda *, reason: calls.append(reason),
            name="t",
        )
        await ctrl.start()
        assert await ctrl.enter_hibernation("first") is True
        assert await ctrl.enter_hibernation("second") is False
        assert calls == ["first"]  # second was no-op, no re-fire

    @pytest.mark.asyncio
    async def test_wake_when_not_hibernating_does_not_fire(self):
        ctrl = SupervisorOuroborosController()
        calls: List[str] = []
        ctrl.register_hibernation_hooks(
            on_wake=lambda *, reason: calls.append(reason),
            name="t",
        )
        await ctrl.start()
        assert await ctrl.wake_from_hibernation(reason="phantom") is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_mixed_sync_and_async_hooks(self):
        ctrl = SupervisorOuroborosController()
        order: List[str] = []

        def sync_hook(*, reason: str) -> None:
            order.append("sync")

        async def async_hook(*, reason: str) -> None:
            await asyncio.sleep(0)
            order.append("async")

        ctrl.register_hibernation_hooks(on_hibernate=sync_hook, name="s")
        ctrl.register_hibernation_hooks(on_hibernate=async_hook, name="a")
        await ctrl.start()
        await ctrl.enter_hibernation("outage")
        assert order == ["sync", "async"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestHookFailures:
    @pytest.mark.asyncio
    async def test_one_hook_raises_others_still_fire(self):
        ctrl = SupervisorOuroborosController()
        seen: List[str] = []

        def broken(*, reason: str) -> None:
            raise RuntimeError("bad hook")

        def healthy(*, reason: str) -> None:
            seen.append(reason)

        ctrl.register_hibernation_hooks(on_hibernate=broken, name="broken")
        ctrl.register_hibernation_hooks(on_hibernate=healthy, name="healthy")

        await ctrl.start()
        result = await ctrl.enter_hibernation("outage")
        assert result is True  # transition still succeeded
        assert seen == ["outage"]  # healthy hook still ran

        snap = ctrl.hibernation_hook_snapshot()
        assert snap["hibernate_fires"] == 1
        assert snap["hibernate_hook_failures"] == 1

    @pytest.mark.asyncio
    async def test_hook_timeout_enforced(self, monkeypatch):
        monkeypatch.setenv(_ENV_HOOK_TIMEOUT, "0.05")
        ctrl = SupervisorOuroborosController()
        fired: List[str] = []

        async def slow(*, reason: str) -> None:
            await asyncio.sleep(1.0)  # well beyond 0.05s budget
            fired.append("slow")

        def fast(*, reason: str) -> None:
            fired.append("fast")

        ctrl.register_hibernation_hooks(on_hibernate=slow, name="slow")
        ctrl.register_hibernation_hooks(on_hibernate=fast, name="fast")

        await ctrl.start()
        result = await ctrl.enter_hibernation("outage")
        assert result is True  # transition survived the timeout
        assert fired == ["fast"]  # slow was cancelled, fast still ran

        snap = ctrl.hibernation_hook_snapshot()
        assert snap["hibernate_hook_failures"] == 1

    @pytest.mark.asyncio
    async def test_async_hook_raising_swallowed(self):
        ctrl = SupervisorOuroborosController()

        async def broken(*, reason: str) -> None:
            raise ValueError("async bad")

        ctrl.register_hibernation_hooks(on_hibernate=broken, name="broken")
        await ctrl.start()
        result = await ctrl.enter_hibernation("outage")
        assert result is True
        assert ctrl.mode is AutonomyMode.HIBERNATION


# ---------------------------------------------------------------------------
# Emergency stop unwind
# ---------------------------------------------------------------------------


class TestEmergencyStopUnwind:
    @pytest.mark.asyncio
    async def test_emergency_stop_fires_wake_hooks_when_hibernating(self):
        ctrl = SupervisorOuroborosController()
        wakes: List[str] = []
        ctrl.register_hibernation_hooks(
            on_wake=lambda *, reason: wakes.append(reason),
            name="t",
        )
        await ctrl.start()
        await ctrl.enter_hibernation("outage")
        await ctrl.emergency_stop("operator halt")

        assert ctrl.mode is AutonomyMode.EMERGENCY_STOP
        assert len(wakes) == 1
        assert "emergency_stop" in wakes[0]
        assert "operator halt" in wakes[0]

    @pytest.mark.asyncio
    async def test_emergency_stop_while_not_hibernating_does_not_fire_wake(self):
        ctrl = SupervisorOuroborosController()
        wakes: List[str] = []
        ctrl.register_hibernation_hooks(
            on_wake=lambda *, reason: wakes.append(reason),
            name="t",
        )
        await ctrl.start()
        await ctrl.emergency_stop("halt")
        assert wakes == []


# ---------------------------------------------------------------------------
# Stop clears hooks
# ---------------------------------------------------------------------------


class TestStopClearsHooks:
    @pytest.mark.asyncio
    async def test_stop_clears_registered_hooks(self):
        ctrl = SupervisorOuroborosController()
        ctrl.register_hibernation_hooks(
            on_hibernate=lambda *, reason: None,
            on_wake=lambda *, reason: None,
            name="t",
        )
        await ctrl.start()
        await ctrl.stop()
        snap = ctrl.hibernation_hook_snapshot()
        assert snap["hibernate_hooks_registered"] == 0
        assert snap["wake_hooks_registered"] == 0


# ---------------------------------------------------------------------------
# Observability snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_tracks_fires_and_elapsed(self):
        ctrl = SupervisorOuroborosController()
        ctrl.register_hibernation_hooks(
            on_hibernate=lambda *, reason: None,
            on_wake=lambda *, reason: None,
            name="t",
        )
        await ctrl.start()
        await ctrl.enter_hibernation("outage")
        await ctrl.wake_from_hibernation(reason="back")

        snap = ctrl.hibernation_hook_snapshot()
        assert snap["hibernate_fires"] == 1
        assert snap["wake_fires"] == 1
        assert snap["hibernate_hook_failures"] == 0
        assert snap["wake_hook_failures"] == 0
        assert isinstance(snap["last_hibernate_elapsed_ms"], float)
        assert isinstance(snap["last_wake_elapsed_ms"], float)
        assert snap["hook_timeout_s"] == _DEFAULT_HOOK_TIMEOUT_S


# ---------------------------------------------------------------------------
# End-to-end bridge against real pool + watchdog
# ---------------------------------------------------------------------------


class TestEndToEndBridge:
    @pytest.mark.asyncio
    async def test_real_pool_and_watchdog_pause_on_hibernation(self):
        """The decisive integration test: real controller, real BG pool,
        real IdleWatchdog — hibernate pauses/freezes both, wake restores.
        """
        from backend.core.ouroboros.governance.background_agent_pool import (
            BackgroundAgentPool,
        )
        from backend.core.ouroboros.battle_test.idle_watchdog import (
            IdleWatchdog,
        )

        class _OrchestratorStub:
            async def run_operation(self, *args, **kwargs) -> None:
                return None

        pool = BackgroundAgentPool(orchestrator=_OrchestratorStub())
        await pool.start()

        watchdog = IdleWatchdog(timeout_s=60.0)
        watchdog.idle_event = asyncio.Event()
        await watchdog.start()

        try:
            ctrl = SupervisorOuroborosController()

            # Mirror governed_loop_service.py wiring.
            def _hibernate(*, reason: str) -> None:
                pool.pause(reason=reason)
                watchdog.freeze(reason=reason)

            def _wake(*, reason: str) -> None:
                watchdog.unfreeze(reason=reason)
                pool.resume(reason=reason)

            ctrl.register_hibernation_hooks(
                on_hibernate=_hibernate,
                on_wake=_wake,
                name="bridge",
            )

            await ctrl.start()
            assert pool.is_paused is False
            assert watchdog.is_frozen is False

            await ctrl.enter_hibernation("dw down")
            assert ctrl.mode is AutonomyMode.HIBERNATION
            assert pool.is_paused is True
            assert watchdog.is_frozen is True

            await ctrl.wake_from_hibernation(reason="dw back")
            assert ctrl.mode is AutonomyMode.SANDBOX
            assert pool.is_paused is False
            assert watchdog.is_frozen is False
        finally:
            await pool.stop()
            watchdog.stop()

    @pytest.mark.asyncio
    async def test_real_bridge_survives_emergency_stop(self):
        """Emergency stop from HIBERNATION must unwind pool + watchdog
        so the operator's inspection tools aren't stranded."""
        from backend.core.ouroboros.governance.background_agent_pool import (
            BackgroundAgentPool,
        )
        from backend.core.ouroboros.battle_test.idle_watchdog import (
            IdleWatchdog,
        )

        class _OrchestratorStub:
            async def run_operation(self, *args, **kwargs) -> None:
                return None

        pool = BackgroundAgentPool(orchestrator=_OrchestratorStub())
        await pool.start()
        watchdog = IdleWatchdog(timeout_s=60.0)
        watchdog.idle_event = asyncio.Event()
        await watchdog.start()

        try:
            ctrl = SupervisorOuroborosController()
            ctrl.register_hibernation_hooks(
                on_hibernate=lambda *, reason: (
                    pool.pause(reason=reason),
                    watchdog.freeze(reason=reason),
                ),
                on_wake=lambda *, reason: (
                    watchdog.unfreeze(reason=reason),
                    pool.resume(reason=reason),
                ),
                name="bridge",
            )

            await ctrl.start()
            await ctrl.enter_hibernation("outage")
            assert pool.is_paused and watchdog.is_frozen

            await ctrl.emergency_stop("operator halt")
            assert ctrl.mode is AutonomyMode.EMERGENCY_STOP
            assert pool.is_paused is False
            assert watchdog.is_frozen is False
        finally:
            await pool.stop()
            watchdog.stop()

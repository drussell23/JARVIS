"""Tests for HibernationProber — HIBERNATION_MODE step 6.

Covers:
- Idempotent start/stop semantics.
- Single-healthy-provider wake path.
- All-down backoff progression and max_duration abort.
- Swallowing provider and wake errors.
- Env-var resolution for initial/max/duration.
- Observability snapshot.
- End-to-end integration with ProviderExhaustionWatcher so entering
  hibernation arms the prober and a flipping provider drives wake.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

import pytest

from backend.core.ouroboros.governance.hibernation_prober import (
    HibernationProber,
    _resolve_float,
)
from backend.core.ouroboros.governance.provider_exhaustion_watcher import (
    ProviderExhaustionWatcher,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Provider stub with a configurable health_probe sequence.

    Each probe pops the next value off ``responses`` (defaulting to the
    last value once exhausted). Raising is simulated by seeding an
    Exception instance into the list.
    """

    def __init__(
        self,
        name: str,
        responses: Optional[List[object]] = None,
    ) -> None:
        self.provider_name = name
        self._responses: List[object] = list(responses or [False])
        self.probe_calls = 0

    async def health_probe(self) -> bool:
        self.probe_calls += 1
        if not self._responses:
            return False
        if len(self._responses) == 1:
            value = self._responses[0]
        else:
            value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return bool(value)


class _FakeController:
    """Minimal controller with recorded enter/wake calls."""

    def __init__(self) -> None:
        self.enter_calls: List[str] = []
        self.wake_calls: List[str] = []
        self._hibernating = False

    async def enter_hibernation(self, *, reason: str) -> bool:
        self.enter_calls.append(reason)
        if self._hibernating:
            return False
        self._hibernating = True
        return True

    async def wake_from_hibernation(self, *, reason: str) -> bool:
        self.wake_calls.append(reason)
        if not self._hibernating:
            return False
        self._hibernating = False
        return True


# ---------------------------------------------------------------------------
# _resolve_float
# ---------------------------------------------------------------------------


class TestResolveFloat:
    def test_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("FOO", "42")
        assert _resolve_float("FOO", 5.0, 9.0) == 9.0

    def test_env_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("FOO", "12.5")
        assert _resolve_float("FOO", 5.0, None) == 12.5

    def test_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        assert _resolve_float("FOO", 5.0, None) == 5.0

    def test_garbage_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("FOO", "nope")
        assert _resolve_float("FOO", 5.0, None) == 5.0

    def test_non_positive_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("FOO", "0")
        assert _resolve_float("FOO", 5.0, None) == 5.0

    def test_explicit_non_positive_rejected(self):
        with pytest.raises(ValueError):
            _resolve_float("FOO", 5.0, -1.0)


# ---------------------------------------------------------------------------
# Start / stop lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_refused_when_no_providers(self):
        ctrl = _FakeController()
        prober = HibernationProber(
            controller=ctrl,
            providers=[],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=0.5,
        )
        assert await prober.start() is False
        assert prober.is_probing is False

    @pytest.mark.asyncio
    async def test_none_providers_filtered(self):
        ctrl = _FakeController()
        provider = _FakeProvider("primary", [True])
        prober = HibernationProber(
            controller=ctrl,
            providers=[None, provider, None],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=0.5,
        )
        assert await prober.start() is True
        # Wait for probe loop to wake the controller and exit.
        for _ in range(50):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)
        assert prober.is_probing is False
        assert prober.wake_count == 1

    @pytest.mark.asyncio
    async def test_start_idempotent_while_running(self):
        ctrl = _FakeController()
        provider = _FakeProvider("primary", [False])
        prober = HibernationProber(
            controller=ctrl,
            providers=[provider],
            initial_delay_s=0.05,
            max_delay_s=0.1,
            max_duration_s=2.0,
        )
        assert await prober.start() is True
        assert await prober.start() is False  # still running
        await prober.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        ctrl = _FakeController()
        prober = HibernationProber(
            controller=ctrl,
            providers=[_FakeProvider("primary")],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=0.5,
        )
        assert await prober.stop() is False

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task_without_waking(self):
        ctrl = _FakeController()
        provider = _FakeProvider("primary", [False])
        prober = HibernationProber(
            controller=ctrl,
            providers=[provider],
            initial_delay_s=0.1,
            max_delay_s=0.2,
            max_duration_s=5.0,
        )
        await prober.start()
        await asyncio.sleep(0.02)  # let the task enter its sleep
        assert await prober.stop() is True
        assert prober.is_probing is False
        assert ctrl.wake_calls == []  # stop must NOT wake


# ---------------------------------------------------------------------------
# Probe semantics
# ---------------------------------------------------------------------------


class TestProbeSemantics:
    @pytest.mark.asyncio
    async def test_single_healthy_provider_triggers_wake(self):
        ctrl = _FakeController()
        ctrl._hibernating = True  # pretend the controller is already asleep
        provider = _FakeProvider("primary", [True])
        prober = HibernationProber(
            controller=ctrl,
            providers=[provider],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=1.0,
        )
        await prober.start()
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        assert prober.wake_count == 1
        assert len(ctrl.wake_calls) == 1
        assert "primary" in ctrl.wake_calls[0]

    @pytest.mark.asyncio
    async def test_first_healthy_wins_short_circuits(self):
        ctrl = _FakeController()
        ctrl._hibernating = True
        tier0 = _FakeProvider("tier0", [True])
        primary = _FakeProvider("primary", [True])
        prober = HibernationProber(
            controller=ctrl,
            providers=[tier0, primary],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=1.0,
        )
        await prober.start()
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        assert tier0.probe_calls == 1
        assert primary.probe_calls == 0  # short-circuited after tier0 succeeded
        assert "tier0" in ctrl.wake_calls[0]

    @pytest.mark.asyncio
    async def test_raising_probe_swallowed_and_skipped(self):
        ctrl = _FakeController()
        ctrl._hibernating = True
        broken = _FakeProvider("broken", [RuntimeError("boom"), True])
        healthy = _FakeProvider("healthy", [True])
        prober = HibernationProber(
            controller=ctrl,
            providers=[broken, healthy],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=1.0,
        )
        await prober.start()
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        # First iteration: broken raises, healthy returns True → wake.
        assert "healthy" in ctrl.wake_calls[0]

    @pytest.mark.asyncio
    async def test_provider_without_health_probe_skipped(self):
        ctrl = _FakeController()
        ctrl._hibernating = True

        class _NoProbe:
            provider_name = "legacy"

        healthy = _FakeProvider("healthy", [True])
        prober = HibernationProber(
            controller=ctrl,
            providers=[_NoProbe(), healthy],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=1.0,
        )
        await prober.start()
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        assert prober.wake_count == 1
        assert "healthy" in ctrl.wake_calls[0]

    @pytest.mark.asyncio
    async def test_budget_exhausted_aborts_without_wake(self):
        ctrl = _FakeController()
        ctrl._hibernating = True
        provider = _FakeProvider("primary", [False])
        prober = HibernationProber(
            controller=ctrl,
            providers=[provider],
            initial_delay_s=0.01,
            max_delay_s=0.01,
            max_duration_s=0.05,
        )
        await prober.start()
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        assert prober.wake_count == 0
        assert ctrl.wake_calls == []
        snap = prober.snapshot()
        assert snap["last_result"] == "budget_exhausted"

    @pytest.mark.asyncio
    async def test_backoff_caps_at_max_delay(self):
        """max_delay_s < initial gets coerced up, so delay never shrinks."""
        ctrl = _FakeController()
        prober = HibernationProber(
            controller=ctrl,
            providers=[_FakeProvider("p", [False])],
            initial_delay_s=0.2,
            max_delay_s=0.05,  # intentionally lower — expect bump
            max_duration_s=1.0,
        )
        # Constructor logs a warning and sets max = initial.
        assert prober.snapshot()["max_delay_s"] == prober.snapshot()["initial_delay_s"]


# ---------------------------------------------------------------------------
# Wake failure modes
# ---------------------------------------------------------------------------


class TestWakeFailures:
    @pytest.mark.asyncio
    async def test_controller_without_wake_method_logged(self):
        class _Bare:
            pass

        bare: object = _Bare()
        provider = _FakeProvider("primary", [True])
        prober = HibernationProber(
            controller=bare,
            providers=[provider],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=1.0,
        )
        await prober.start()
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        # Prober counted the "wake" path even though controller lacked method.
        assert prober.wake_count == 1
        # No crash — loop exited cleanly.
        assert prober.is_probing is False

    @pytest.mark.asyncio
    async def test_wake_raise_swallowed(self):
        class _RaisingCtrl:
            async def wake_from_hibernation(self, *, reason: str) -> bool:
                raise RuntimeError("already awake")

        provider = _FakeProvider("primary", [True])
        prober = HibernationProber(
            controller=_RaisingCtrl(),
            providers=[provider],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=1.0,
        )
        await prober.start()
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        assert prober.is_probing is False
        assert prober.wake_count == 1  # the prober committed before calling


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_before_start(self):
        prober = HibernationProber(
            controller=_FakeController(),
            providers=[_FakeProvider("a"), _FakeProvider("b")],
            initial_delay_s=1.0,
            max_delay_s=2.0,
            max_duration_s=5.0,
        )
        snap = prober.snapshot()
        assert snap["is_probing"] is False
        assert snap["probe_attempts"] == 0
        assert snap["wake_count"] == 0
        assert snap["providers"] == ["a", "b"]
        assert snap["elapsed_s"] is None
        assert snap["last_result"] is None


# ---------------------------------------------------------------------------
# Integration with watcher
# ---------------------------------------------------------------------------


class TestWatcherIntegration:
    @pytest.mark.asyncio
    async def test_watcher_attach_prober_triggers_start_on_hibernate(self):
        """ProviderExhaustionWatcher must call prober.start() after the
        controller actually accepts the hibernation transition, and the
        prober must then drive the wake when a provider reports healthy."""
        ctrl = _FakeController()

        # Provider flips healthy on first probe → prober wakes immediately.
        provider = _FakeProvider("primary", [True])
        prober = HibernationProber(
            controller=ctrl,
            providers=[provider],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=1.0,
        )

        watcher = ProviderExhaustionWatcher(
            controller=ctrl, threshold=1, prober=prober,
        )
        triggered = await watcher.record_exhaustion(reason="dw down")

        assert triggered is True
        assert len(ctrl.enter_calls) == 1

        # Give the probe loop a beat to run the first iteration.
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        assert prober.wake_count == 1
        assert len(ctrl.wake_calls) == 1

    @pytest.mark.asyncio
    async def test_attach_prober_after_construction(self):
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=1)
        provider = _FakeProvider("primary", [True])
        prober = HibernationProber(
            controller=ctrl,
            providers=[provider],
            initial_delay_s=0.01,
            max_delay_s=0.02,
            max_duration_s=1.0,
        )
        watcher.attach_prober(prober)

        await watcher.record_exhaustion(reason="outage")
        for _ in range(100):
            if not prober.is_probing:
                break
            await asyncio.sleep(0.02)

        assert ctrl.wake_calls and "primary" in ctrl.wake_calls[0]

    @pytest.mark.asyncio
    async def test_prober_start_failure_does_not_break_hibernation(self):
        """If prober.start() raises, watcher still reports hibernation as
        having happened — the controller transition already succeeded."""

        class _BrokenProber:
            async def start(self) -> bool:
                raise RuntimeError("prober construction bug")

        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(
            controller=ctrl, threshold=1, prober=_BrokenProber(),
        )
        result = await watcher.record_exhaustion(reason="outage")
        assert result is True
        assert watcher.hibernations_triggered == 1

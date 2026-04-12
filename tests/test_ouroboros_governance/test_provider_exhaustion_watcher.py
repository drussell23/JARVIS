"""Tests for ProviderExhaustionWatcher — HIBERNATION_MODE step 5.

Covers:
- Threshold-triggered controller.enter_hibernation() invocation.
- Reset-on-success semantics (a single success clears the streak).
- Env-var threshold resolution (`JARVIS_HIBERNATION_TRIGGER_THRESHOLD`).
- Idempotence when the controller is already hibernating.
- Non-zero threshold guard at construction.
- Swallowing controller failures (RuntimeError from EMERGENCY_STOP).
- Observability snapshot.
"""
from __future__ import annotations

import asyncio
from typing import List

import pytest

from backend.core.ouroboros.governance.provider_exhaustion_watcher import (
    ProviderExhaustionWatcher,
    _resolve_threshold,
)


class _FakeController:
    """Minimal stand-in for SupervisorOuroborosController.

    Tracks enter_hibernation calls and can be configured to (a) return
    True once then False (real idempotence), (b) always return True, or
    (c) raise RuntimeError to mimic EMERGENCY_STOP refusal.
    """

    def __init__(
        self,
        *,
        raise_on_enter: bool = False,
        always_succeed: bool = True,
    ) -> None:
        self.calls: List[str] = []
        self._raise_on_enter = raise_on_enter
        self._always_succeed = always_succeed
        self._entered_once = False

    async def enter_hibernation(self, *, reason: str) -> bool:
        self.calls.append(reason)
        if self._raise_on_enter:
            raise RuntimeError(
                "Cannot hibernate from EMERGENCY_STOP — clear the emergency first"
            )
        if self._always_succeed:
            return True
        # Idempotent semantics: first True, subsequent False.
        if self._entered_once:
            return False
        self._entered_once = True
        return True


# ---------------------------------------------------------------------------
# Threshold resolution
# ---------------------------------------------------------------------------


class TestResolveThreshold:
    def test_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HIBERNATION_TRIGGER_THRESHOLD", "7")
        assert _resolve_threshold(2) == 2

    def test_env_var_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HIBERNATION_TRIGGER_THRESHOLD", "5")
        assert _resolve_threshold(None) == 5

    def test_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HIBERNATION_TRIGGER_THRESHOLD", raising=False,
        )
        assert _resolve_threshold(None) == 3

    def test_garbage_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HIBERNATION_TRIGGER_THRESHOLD", "not-a-number")
        assert _resolve_threshold(None) == 3

    def test_non_positive_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HIBERNATION_TRIGGER_THRESHOLD", "0")
        assert _resolve_threshold(None) == 3

    def test_explicit_non_positive_rejected(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HIBERNATION_TRIGGER_THRESHOLD", raising=False,
        )
        with pytest.raises(ValueError, match="threshold"):
            _resolve_threshold(0)


# ---------------------------------------------------------------------------
# Core exhaustion counting
# ---------------------------------------------------------------------------


class TestExhaustionCounting:
    @pytest.mark.asyncio
    async def test_below_threshold_does_not_trigger(self):
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=3)

        assert await watcher.record_exhaustion(reason="dw down") is False
        assert await watcher.record_exhaustion(reason="dw down") is False
        assert watcher.consecutive == 2
        assert watcher.total_exhaustions == 2
        assert ctrl.calls == []

    @pytest.mark.asyncio
    async def test_at_threshold_triggers_hibernation(self):
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=3)

        await watcher.record_exhaustion(reason="dw down")
        await watcher.record_exhaustion(reason="dw down")
        triggered = await watcher.record_exhaustion(reason="claude down")

        assert triggered is True
        assert len(ctrl.calls) == 1
        assert "consecutive_exhaustion=3" in ctrl.calls[0]
        assert "claude down" in ctrl.calls[0]
        assert watcher.hibernations_triggered == 1

    @pytest.mark.asyncio
    async def test_threshold_one_triggers_immediately(self):
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=1)

        triggered = await watcher.record_exhaustion(reason="outage")

        assert triggered is True
        assert len(ctrl.calls) == 1

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_counter(self):
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=3)

        await watcher.record_exhaustion(reason="dw down")
        await watcher.record_exhaustion(reason="dw down")
        assert watcher.consecutive == 2

        await watcher.record_success()
        assert watcher.consecutive == 0
        # Total exhaustions is NOT reset — only the consecutive run.
        assert watcher.total_exhaustions == 2

        # Hibernation should not be triggered at 2 + 1 = 3 because the
        # reset wiped the streak; the next exhaustion starts fresh.
        await watcher.record_exhaustion(reason="dw down")
        assert watcher.consecutive == 1
        assert ctrl.calls == []

    @pytest.mark.asyncio
    async def test_success_noop_when_already_zero(self):
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=3)

        # Lots of successes with no exhaustions.
        for _ in range(5):
            await watcher.record_success()
        assert watcher.consecutive == 0
        assert watcher.total_exhaustions == 0


# ---------------------------------------------------------------------------
# Idempotence & controller failure modes
# ---------------------------------------------------------------------------


class TestHibernationIdempotence:
    @pytest.mark.asyncio
    async def test_subsequent_exhaustions_above_threshold_still_try_controller(
        self,
    ):
        """Once over threshold, every new exhaustion keeps asking the
        controller (which handles idempotence itself). The watcher's
        consecutive counter climbs past the threshold and
        hibernations_triggered reflects only transitions the controller
        actually returned True for.
        """
        ctrl = _FakeController(always_succeed=False)
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=2)

        # Streak builds; no controller call yet at n=1 (below threshold).
        below = await watcher.record_exhaustion(reason="dw")
        assert below is False
        assert len(ctrl.calls) == 0

        # First crossing — transition accepted.
        first = await watcher.record_exhaustion(reason="claude")
        assert first is True
        assert watcher.hibernations_triggered == 1
        assert len(ctrl.calls) == 1

        # Second report — controller returns False (already hibernating),
        # but the watcher still forwards the notification so the
        # controller can count/log it if it cares.
        second = await watcher.record_exhaustion(reason="still down")
        assert second is False
        assert watcher.hibernations_triggered == 1
        assert watcher.consecutive == 3  # counter keeps climbing
        assert len(ctrl.calls) == 2  # both over-threshold calls forwarded

    @pytest.mark.asyncio
    async def test_controller_runtime_error_swallowed(self):
        """If the controller raises (EMERGENCY_STOP), the watcher
        logs and returns False — it must never propagate the error
        back to CandidateGenerator."""
        ctrl = _FakeController(raise_on_enter=True)
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=1)

        result = await watcher.record_exhaustion(reason="outage")

        assert result is False
        assert watcher.hibernations_triggered == 0
        assert len(ctrl.calls) == 1

    @pytest.mark.asyncio
    async def test_missing_enter_hibernation_on_controller(self):
        """Controller without enter_hibernation method — watcher
        degrades gracefully rather than crashing the pipeline."""

        class _BareController:
            pass

        watcher = ProviderExhaustionWatcher(
            controller=_BareController(), threshold=1,
        )
        result = await watcher.record_exhaustion(reason="outage")
        assert result is False
        assert watcher.hibernations_triggered == 0


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_reflects_state(self):
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=3)

        s0 = watcher.snapshot()
        assert s0["threshold"] == 3
        assert s0["consecutive"] == 0
        assert s0["total_exhaustions"] == 0
        assert s0["hibernations_triggered"] == 0
        assert s0["last_reason"] is None

        await watcher.record_exhaustion(reason="dw down")
        s1 = watcher.snapshot()
        assert s1["consecutive"] == 1
        assert s1["total_exhaustions"] == 1
        assert s1["last_reason"] == "dw down"

        await watcher.record_success()
        s2 = watcher.snapshot()
        assert s2["consecutive"] == 0
        assert s2["last_reason"] is None
        assert s2["total_successes"] == 1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_exhaustions_serialize_under_lock(self):
        """Three workers racing record_exhaustion() — the counter
        must end at exactly N regardless of interleaving."""
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=100)

        async def hit() -> None:
            await watcher.record_exhaustion(reason="race")

        await asyncio.gather(*(hit() for _ in range(20)))

        assert watcher.consecutive == 20
        assert watcher.total_exhaustions == 20
        assert watcher.hibernations_triggered == 0

    @pytest.mark.asyncio
    async def test_reset_hard_clears_state(self):
        ctrl = _FakeController()
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=10)

        for _ in range(4):
            await watcher.record_exhaustion(reason="x")
        assert watcher.consecutive == 4

        await watcher.reset()
        assert watcher.consecutive == 0
        assert watcher.snapshot()["last_reason"] is None


# ---------------------------------------------------------------------------
# Integration with real SupervisorOuroborosController
# ---------------------------------------------------------------------------


class TestRealControllerIntegration:
    """Smoke test against the actual controller to prove the watcher
    + controller idempotence cycle works end-to-end without a fake.
    """

    @pytest.mark.asyncio
    async def test_watcher_drives_real_controller_into_hibernation(self):
        from backend.core.ouroboros.governance.supervisor_controller import (
            AutonomyMode,
            SupervisorOuroborosController,
        )

        ctrl = SupervisorOuroborosController()
        await ctrl.start()  # → SANDBOX
        watcher = ProviderExhaustionWatcher(controller=ctrl, threshold=2)

        assert ctrl.mode is AutonomyMode.SANDBOX
        await watcher.record_exhaustion(reason="dw down")
        assert ctrl.mode is AutonomyMode.SANDBOX  # still SANDBOX
        await watcher.record_exhaustion(reason="claude down")
        assert ctrl.mode is AutonomyMode.HIBERNATION

        # And wake restores SANDBOX + success clears the streak.
        await ctrl.wake_from_hibernation(reason="providers back")
        assert ctrl.mode is AutonomyMode.SANDBOX
        await watcher.record_success()
        assert watcher.consecutive == 0

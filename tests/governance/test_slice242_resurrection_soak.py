"""Slice 242 Phase 3 — end-to-end resurrection injection soak.

We do not assume the hibernation persistence loop works — we prove it against a
controllable, injected outage→recovery cycle (a live container soak cannot force
a deterministic DW outage; this mock harness can). The flow proven here mirrors
the production HIBERNATION_MODE step 6:

  1. Inject a catastrophic grid outage  → every provider health_probe DOWN.
  2. The Grid Sentinel (HibernationProber) backs off and keeps probing at ~$0.
  3. Inject grid recovery               → a provider reports UP.
  4. The prober autonomously fires controller.wake_from_hibernation AND records
     the observed dark-window duration into the statistical prior.
  5. On the NEXT outage, the prior (now holding history) times the first probe
     from the recorded durations instead of the static default.

Uses tiny real delays (no monkeypatched clock) so the asyncio backoff path is
exercised exactly as in production, just compressed.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import dw_transport_recovery as rec
from backend.core.ouroboros.governance.hibernation_prober import HibernationProber


class _FakeController:
    """Structurally-typed wake target — records the autonomous resurrection."""

    def __init__(self) -> None:
        self.woken = False
        self.wake_reason = None
        self.wake_calls = 0

    async def wake_from_hibernation(self, reason: str) -> None:
        self.woken = True
        self.wake_reason = reason
        self.wake_calls += 1


class _GridProvider:
    """Simulated DW endpoint. ``up_after`` probes return DOWN, then UP — i.e. the
    dark window lasts ``up_after`` probe cycles before the grid recovers."""

    provider_name = "doubleword-397b"

    def __init__(self, up_after: int) -> None:
        self._up_after = up_after
        self.probe_count = 0

    async def health_probe(self) -> bool:
        self.probe_count += 1
        return self.probe_count > self._up_after


@pytest.fixture(autouse=True)
def _isolate_prior():
    rec.reset_recovery_prior()
    yield
    rec.reset_recovery_prior()


class TestResurrectionSoak:
    async def test_outage_then_autonomous_wake_records_duration(self):
        """Inject a 2-cycle dark window → prober probes, recovers, wakes the
        controller, and banks the outage duration into the prior."""
        controller = _FakeController()
        provider = _GridProvider(up_after=2)  # DOWN, DOWN, UP
        prober = HibernationProber(
            controller=controller,
            providers=[provider],
            initial_delay_s=0.02,
            max_delay_s=0.05,
            max_duration_s=10.0,
        )

        assert await prober.start() is True
        # wait for the loop to probe through the dark window and wake
        for _ in range(200):
            if controller.woken:
                break
            await asyncio.sleep(0.02)
        await prober.stop()

        assert controller.woken is True, "grid recovered but controller was never woken"
        assert controller.wake_calls == 1
        assert provider.probe_count >= 3, "should have probed through the dark window"
        assert prober.wake_count == 1
        # the dark-window duration is now banked for next time
        assert rec.get_recovery_prior().sample_count() == 1

    async def test_prior_times_next_outage_from_history(self):
        """After enough recorded outages, the NEXT first-probe interval is
        derived from the dark-window history — not the static default."""
        prior = rec.get_recovery_prior()
        # bank a history of ~120s outages (min_samples default = 3)
        for _ in range(5):
            prior.record(120.0)

        controller = _FakeController()
        provider = _GridProvider(up_after=0)  # already UP
        prober = HibernationProber(
            controller=controller,
            providers=[provider],
            initial_delay_s=5.0,   # static default would wait 5s
            max_delay_s=300.0,
            max_duration_s=10.0,
        )
        # the adaptive first-probe delay is sourced from the prior (p25 ≈ 120s),
        # proving we no longer blindly use the static 5s
        derived = prober._first_probe_delay()
        assert derived != 5.0, "prior should override the static default"
        assert 100.0 <= derived <= 140.0, f"expected ~p25 of history, got {derived}"

    async def test_gate_off_is_byte_identical_static(self, monkeypatch):
        """Kill switch (=0) → the prober ignores the prior and uses the static
        default, even with a rich outage history present."""
        monkeypatch.setenv("JARVIS_RECOVERY_PRIOR_ENABLED", "0")
        prior = rec.get_recovery_prior()
        for _ in range(5):
            prior.record(120.0)

        prober = HibernationProber(
            controller=_FakeController(),
            providers=[_GridProvider(up_after=0)],
            initial_delay_s=5.0,
            max_delay_s=300.0,
            max_duration_s=10.0,
        )
        assert prober._first_probe_delay() == 5.0  # static, prior ignored
        # and the duration is NOT recorded when gated off
        prior.reset()
        prober._record_outage_duration(99.0)
        assert prior.sample_count() == 0

    async def test_full_two_outage_cycle_history_accumulates(self):
        """Two successive inject→recover cycles each bank a duration — the prior
        accumulates across hibernation cycles within the process."""
        rec.reset_recovery_prior()
        for cycle in range(2):
            controller = _FakeController()
            provider = _GridProvider(up_after=1)
            prober = HibernationProber(
                controller=controller,
                providers=[provider],
                initial_delay_s=0.02,
                max_delay_s=0.05,
                max_duration_s=10.0,
            )
            await prober.start()
            for _ in range(200):
                if controller.woken:
                    break
                await asyncio.sleep(0.02)
            await prober.stop()
            assert controller.woken is True

        assert rec.get_recovery_prior().sample_count() == 2

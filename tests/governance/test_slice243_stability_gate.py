"""Slice 243 — Adaptive Grid Stability Matrix & micro-streaming flap mitigation.

The HibernationProber used to wake the controller on a single successful health
ping. But DW's primary failure mode is a *flapping* grid: a basic 200-OK ping
succeeds while streaming sockets drop mid-flight. Waking the heavy PLAN-EXPLOIT
DAG against a flapping grid → immediate live_transport ruptures + system
thrashing.

This adds a Stability Confidence Gate: on a successful ping the prober enters a
VERIFYING_STABILITY phase and runs a Micro-Streaming Load Test against the
provider (a lightweight multi-token stream). wake_from_hibernation is gated
behind 100% successful stream verification. A mid-flight rupture →
FLAPPING_GRID_DETECTED, wake aborted, seamlessly back to the
RecoveryDurationPrior backoff loop — never disturbing the hibernated WAL intent,
never banking a false outage duration.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import dw_transport_recovery as rec
from backend.core.ouroboros.governance import hibernation_prober as hp
from backend.core.ouroboros.governance.hibernation_prober import HibernationProber


class _Controller:
    def __init__(self) -> None:
        self.woken = False
        self.wake_calls = 0

    async def wake_from_hibernation(self, reason: str) -> None:
        self.woken = True
        self.wake_calls += 1


class _FlapProvider:
    """Pings UP, but its micro-stream behaves per ``stream_results``: True =
    clean multi-token stream, False = empty/incomplete, Exception = mid-flight
    socket rupture. Pops per call; sticks on the last value once exhausted."""

    provider_name = "doubleword-397b"

    def __init__(self, stream_results):
        self._stream = list(stream_results)
        self.ping_calls = 0
        self.stream_calls = 0

    async def health_probe(self) -> bool:
        self.ping_calls += 1
        return True  # the grid always *pings* up — that's the flap trap

    async def stream_health_probe(self) -> bool:
        self.stream_calls += 1
        val = self._stream[0] if len(self._stream) == 1 else self._stream.pop(0)
        if isinstance(val, Exception):
            raise val
        return bool(val)


class _PingOnlyProvider:
    """No stream_health_probe — legacy contract (Prime/Claude)."""

    provider_name = "j-prime"

    def __init__(self):
        self.ping_calls = 0

    async def health_probe(self) -> bool:
        self.ping_calls += 1
        return True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("JARVIS_GRID_STABILITY_GATE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_GRID_STABILITY_STREAM_CHECKS", raising=False)
    rec.reset_recovery_prior()
    yield
    rec.reset_recovery_prior()


def _prober(controller, providers):
    return HibernationProber(
        controller=controller,
        providers=providers,
        initial_delay_s=0.02,
        max_delay_s=0.05,
        max_duration_s=3.0,
    )


async def _run_until(prober, controller, *, max_cycles=120):
    await prober.start()
    for _ in range(max_cycles):
        if controller.woken:
            break
        await asyncio.sleep(0.02)
    await prober.stop()


class TestStabilityGate:
    async def test_flapping_grid_suppresses_wake(self, caplog):
        """Ping UP but stream ruptures → wake aborted, grid stays dark."""
        controller = _Controller()
        provider = _FlapProvider([RuntimeError("live_transport rupture")])
        prober = _prober(controller, [provider])
        with caplog.at_level("WARNING"):
            await _run_until(prober, controller, max_cycles=30)

        assert controller.woken is False, "flapping grid must NOT wake the DAG"
        assert controller.wake_calls == 0
        assert prober.wake_count == 0
        assert provider.stream_calls >= 1, "must run the micro-streaming load test"
        assert any("FLAPPING_GRID_DETECTED" in r.message for r in caplog.records)
        # the false outage was NOT banked into the prior
        assert rec.get_recovery_prior().sample_count() == 0

    async def test_stable_grid_with_stream_verification_wakes(self):
        controller = _Controller()
        provider = _FlapProvider([True])
        prober = _prober(controller, [provider])
        await _run_until(prober, controller)

        assert controller.woken is True
        assert prober.wake_count == 1
        assert provider.stream_calls >= 1
        # a genuine resurrection banks the outage duration (Slice 242 intact)
        assert rec.get_recovery_prior().sample_count() == 1

    async def test_provider_without_stream_probe_wakes_on_ping(self):
        """Legacy providers (no stream contract) keep waking on the ping."""
        controller = _Controller()
        provider = _PingOnlyProvider()
        prober = _prober(controller, [provider])
        await _run_until(prober, controller)

        assert controller.woken is True
        assert prober.wake_count == 1

    async def test_gate_off_wakes_on_ping_even_if_stream_would_flap(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GRID_STABILITY_GATE_ENABLED", "0")
        controller = _Controller()
        provider = _FlapProvider([RuntimeError("rupture")])
        prober = _prober(controller, [provider])
        await _run_until(prober, controller)

        assert controller.woken is True, "kill switch → legacy wake-on-ping"
        assert provider.stream_calls == 0, "gate off → no micro-stream test"

    async def test_dynamic_confidence_requires_all_checks(self, monkeypatch):
        """N consecutive clean streams required; a flap inside the window aborts."""
        monkeypatch.setenv("JARVIS_GRID_STABILITY_STREAM_CHECKS", "3")
        controller = _Controller()
        # clean, then a persistent flap (sticks on rupture) — the grid never
        # yields 3 consecutive clean streams, so the window never closes.
        provider = _FlapProvider([True, RuntimeError("rupture")])
        prober = _prober(controller, [provider])
        await _run_until(prober, controller, max_cycles=20)

        assert controller.woken is False
        assert provider.stream_calls >= 2

    async def test_flap_then_recover_eventually_wakes_once(self):
        """Grid flaps, prober stays in $0 backoff, then streaming stabilises →
        wakes exactly once with no thrashing."""
        controller = _Controller()
        provider = _FlapProvider([RuntimeError("rupture"), RuntimeError("rupture"), True])
        prober = _prober(controller, [provider])
        await _run_until(prober, controller)

        assert controller.woken is True
        assert controller.wake_calls == 1, "must wake exactly once — no thrash"
        assert provider.stream_calls >= 3


class TestStabilityGateSourcePins:
    def test_verifying_stability_state_and_stream_call(self):
        import inspect
        src = inspect.getsource(hp)
        assert "VERIFYING_STABILITY" in src
        assert "stream_health_probe" in src, "must run the micro-streaming load test"
        assert "FLAPPING_GRID_DETECTED" in src
        assert "_verify_grid_stability" in src

    def test_env_knobs(self, monkeypatch):
        assert hp._stability_gate_enabled() is True  # graduated default-ON
        monkeypatch.setenv("JARVIS_GRID_STABILITY_GATE_ENABLED", "0")
        assert hp._stability_gate_enabled() is False
        monkeypatch.delenv("JARVIS_GRID_STABILITY_STREAM_CHECKS", raising=False)
        assert hp._stability_stream_checks() >= 1
        monkeypatch.setenv("JARVIS_GRID_STABILITY_STREAM_CHECKS", "4")
        assert hp._stability_stream_checks() == 4

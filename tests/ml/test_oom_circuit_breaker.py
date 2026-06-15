"""Zero-Trust OOM Circuit Breaker — the headline integration proof.

Slice 250.2b Phase 3. Proves that under unified-memory / Metal OOM during
ECAPA extraction, the BiometricExecutionMatrix:

  1. SYNCHRONOUSLY returns the secure-lock verdict (REJECTED, oom_fail_secure)
     — never delayed by telemetry, never ACCEPTED.
  2. Records the failure on the injected breaker.
  3. Fires async RESOURCE_PRESSURE telemetry to the event sink AND dispatches
     the Slice-254 diagnostic swarm — non-blocking.
  4. Trips the breaker after N sustained OOMs, after which authenticate
     short-circuits to circuit_open_locked WITHOUT invoking the embedder.
"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest

from tests.ml.biometric_execution_matrix import (
    BiometricExecutionMatrix,
    LocalOOMCircuitBreaker,
    Verdict,
)


# --------------------------------------------------------------------------- #
# Spies
# --------------------------------------------------------------------------- #
class _SpyBreaker:
    def __init__(self) -> None:
        self.failures: list = []
        self.successes = 0

    def can_execute(self) -> bool:
        return True

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self, error=None) -> None:
        self.failures.append(error)


class _EventSpy:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def __call__(self, event_type: str, payload: dict) -> None:
        with self.lock:
            self.events.append((event_type, payload))


class _SwarmSpy:
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, payload: dict):
        with self.lock:
            self.payloads.append(payload)


def _oom_embedder(_x: np.ndarray) -> np.ndarray:
    raise MemoryError("Metal/unified-memory OOM")


# --------------------------------------------------------------------------- #
# OOM during extraction — sync REJECT + record_failure + async telemetry
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_oom_sync_reject_and_async_telemetry() -> None:
    breaker = _SpyBreaker()
    sink = _EventSpy()
    swarm = _SwarmSpy()
    matrix = BiometricExecutionMatrix(
        embedder=_oom_embedder,
        baseline_embedding=np.ones(4, dtype=np.float64),
        accept_threshold=0.5,
        breaker=breaker,
        event_sink=sink,
        swarm_trigger=swarm,
    )

    res = matrix.authenticate(np.zeros(16, dtype=np.float64))

    # 1. Secure-lock verdict, synchronously.
    assert res.verdict is Verdict.REJECTED
    assert res.reason == "oom_fail_secure"
    assert res.score == 0.0

    # 2. Failure recorded on the breaker.
    assert len(breaker.failures) == 1
    assert isinstance(breaker.failures[0], MemoryError)

    # 3. Let the async telemetry tasks run.
    for _ in range(5):
        await asyncio.sleep(0)

    assert len(sink.events) == 1
    event_type, payload = sink.events[0]
    assert event_type == "resource_pressure"
    assert payload["component"] == "ecapa"
    assert "MemoryError" in payload["error_class"]
    assert "rss_hint" in payload

    assert len(swarm.payloads) == 1
    assert swarm.payloads[0]["component"] == "ecapa"
    assert "MemoryError" in swarm.payloads[0]["error_class"]


# --------------------------------------------------------------------------- #
# Non-blocking proof: a SLOW sink must NOT delay the secure-lock verdict.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_verdict_not_blocked_by_slow_telemetry() -> None:
    sink_completed_at: list[float] = []
    slow_delay = 0.30

    async def _slow_sink(event_type: str, payload: dict) -> None:
        await asyncio.sleep(slow_delay)
        sink_completed_at.append(time.monotonic())

    swarm_completed_at: list[float] = []

    async def _slow_swarm(payload: dict) -> None:
        await asyncio.sleep(slow_delay)
        swarm_completed_at.append(time.monotonic())

    matrix = BiometricExecutionMatrix(
        embedder=_oom_embedder,
        baseline_embedding=np.ones(4, dtype=np.float64),
        accept_threshold=0.5,
        breaker=_SpyBreaker(),
        event_sink=_slow_sink,
        swarm_trigger=_slow_swarm,
    )

    t0 = time.monotonic()
    res = matrix.authenticate(np.zeros(16, dtype=np.float64))
    verdict_at = time.monotonic()

    # The verdict returns essentially immediately — well before the slow sink.
    assert res.verdict is Verdict.REJECTED
    assert res.reason == "oom_fail_secure"
    verdict_latency = verdict_at - t0
    assert verdict_latency < slow_delay, (
        f"verdict took {verdict_latency:.4f}s — should be << slow sink {slow_delay}s"
    )

    # Now drain the loop so the slow telemetry actually completes.
    await asyncio.sleep(slow_delay + 0.1)
    assert sink_completed_at, "slow sink should eventually complete"
    assert swarm_completed_at, "slow swarm should eventually complete"

    # Ordering: verdict returned strictly before the slow sink completed.
    assert verdict_at < sink_completed_at[0]
    assert verdict_at < swarm_completed_at[0]


# --------------------------------------------------------------------------- #
# No running loop: telemetry still fires best-effort on a daemon thread.
# --------------------------------------------------------------------------- #
def test_oom_telemetry_fires_without_running_loop() -> None:
    breaker = _SpyBreaker()
    sink = _EventSpy()
    swarm = _SwarmSpy()
    matrix = BiometricExecutionMatrix(
        embedder=_oom_embedder,
        baseline_embedding=np.ones(4, dtype=np.float64),
        accept_threshold=0.5,
        breaker=breaker,
        event_sink=sink,
        swarm_trigger=swarm,
    )

    # Called from a plain sync context (no running event loop).
    res = matrix.authenticate(np.zeros(16, dtype=np.float64))
    assert res.verdict is Verdict.REJECTED
    assert res.reason == "oom_fail_secure"

    # Give the daemon thread a brief moment to fire best-effort.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not sink.events:
        time.sleep(0.01)

    assert len(sink.events) == 1
    assert sink.events[0][0] == "resource_pressure"
    assert len(swarm.payloads) == 1


# --------------------------------------------------------------------------- #
# Telemetry failure NEVER changes/delays the verdict.
# --------------------------------------------------------------------------- #
def test_telemetry_failure_does_not_affect_verdict() -> None:
    def _explode_sink(event_type: str, payload: dict) -> None:
        raise RuntimeError("telemetry backend down")

    def _explode_swarm(payload: dict):
        raise RuntimeError("swarm dispatch down")

    matrix = BiometricExecutionMatrix(
        embedder=_oom_embedder,
        baseline_embedding=np.ones(4, dtype=np.float64),
        accept_threshold=0.5,
        breaker=_SpyBreaker(),
        event_sink=_explode_sink,
        swarm_trigger=_explode_swarm,
    )

    res = matrix.authenticate(np.zeros(16, dtype=np.float64))
    assert res.verdict is Verdict.REJECTED
    assert res.reason == "oom_fail_secure"
    # Let any best-effort thread run; it must swallow the telemetry error.
    time.sleep(0.1)


# --------------------------------------------------------------------------- #
# Breaker trips after N sustained OOMs -> subsequent calls short-circuit.
# --------------------------------------------------------------------------- #
def test_breaker_trips_after_n_ooms() -> None:
    threshold = 3
    breaker = LocalOOMCircuitBreaker(failure_threshold=threshold, reset_timeout_s=60.0)

    calls = {"n": 0}

    def _counting_oom(_x: np.ndarray) -> np.ndarray:
        calls["n"] += 1
        raise MemoryError("OOM")

    matrix = BiometricExecutionMatrix(
        embedder=_counting_oom,
        baseline_embedding=np.ones(4, dtype=np.float64),
        accept_threshold=0.5,
        breaker=breaker,
    )

    # First `threshold` calls hit the embedder and OOM.
    for i in range(threshold):
        res = matrix.authenticate(np.zeros(16, dtype=np.float64))
        assert res.verdict is Verdict.REJECTED
        assert res.reason == "oom_fail_secure"

    assert calls["n"] == threshold
    assert breaker.can_execute() is False, "breaker should be open after N OOMs"

    # Subsequent calls short-circuit: embedder NOT invoked, circuit_open_locked.
    res = matrix.authenticate(np.zeros(16, dtype=np.float64))
    assert res.verdict is Verdict.REJECTED
    assert res.reason == "circuit_open_locked"
    assert calls["n"] == threshold, "embedder MUST NOT be called once breaker open"


# --------------------------------------------------------------------------- #
# LocalOOMCircuitBreaker standalone unit behavior
# --------------------------------------------------------------------------- #
def test_local_breaker_resets_after_timeout() -> None:
    breaker = LocalOOMCircuitBreaker(failure_threshold=2, reset_timeout_s=0.05)
    assert breaker.can_execute() is True
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.can_execute() is False
    time.sleep(0.07)
    # After reset timeout, half-open: can_execute allows a trial again.
    assert breaker.can_execute() is True


def test_local_breaker_success_resets_failures() -> None:
    breaker = LocalOOMCircuitBreaker(failure_threshold=2, reset_timeout_s=60.0)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    # One failure pre-reset + one post-reset = still under threshold of 2.
    assert breaker.can_execute() is True

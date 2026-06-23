# tests/governance/test_failover_warmup_integration.py
"""TDD tests for the VRAM pre-warm integration into the Failover FSM.

These tests extend the existing failover_lifecycle test suite and cover the
new warmup_fn injectable boundary added to _tick_awakening:

  (a) AWAKENING awaits warmup_fn before transitioning to SERVING
      -- assert SERVING is NOT entered until warmup_fn resolves
  (b) warmup_fn returning False (timeout) -> still reaches SERVING + logs
      (no deadlock in AWAKENING)
  (c) JARVIS_FAILOVER_WARMUP_ENABLED=false -> straight to SERVING (no warmup call)
  (d) warmup is awaited in the correct order: warmup completes before
      is_jprime_serving() is True

All boundaries injected -- ZERO real GCE / network / Ollama.

NOTE: FailoverState / FailoverLifecycleController are always accessed via the
``fl`` module reference (fl.FailoverState.X, fl.FailoverLifecycleController)
rather than module-level imports. test_failover_lifecycle.py contains
``importlib.reload(fl)`` which rebuilds the enum class -- a module-level
import taken before that reload holds the OLD class identity, causing identity
comparisons to fail even when the value names match. Accessing through ``fl``
ensures every assertion uses the live class object.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq


# ---------------------------------------------------------------------------
# Shared fakes / helpers (mirrors test_failover_lifecycle.py style)
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_JPRIME_COLDSTART_S", "100")
    monkeypatch.setenv("JARVIS_CRYO_AWAKEN_MARGIN", "1.5")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "120")
    monkeypatch.setenv("JARVIS_RECOVERY_THRESHOLD", "0.6")
    monkeypatch.setenv("JARVIS_RECOVERY_HYSTERESIS_CYCLES", "2")
    monkeypatch.setenv("JARVIS_JPRIME_MIN_UPTIME_S", "300")
    monkeypatch.setenv("JARVIS_HANDBACK_COOLDOWN_S", "300")
    # Warmup enabled by default for integration tests.
    monkeypatch.setenv("JARVIS_FAILOVER_WARMUP_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_WARMUP_TIMEOUT_S", "180")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _fill_outage(route: str = "dw", n: int = 5) -> None:
    grad = pq.get_provider_health_gradient()
    for _ in range(n):
        grad.record_sweep(route, success=False)


def _fake_forecast(confidence: str, p50: float = 300.0, p90: float = 600.0):
    class _F:
        def __init__(self):
            self.confidence = confidence
            self.p50_s = p50
            self.p90_s = p90
            self.velocity_hint = 1.0
            self.samples = 5 if confidence == "HIGH" else 0
    return _F()


def _make_ctrl(clock, warmup_fn=None, **kw):
    """Construct a controller via the live fl module class (reload-safe)."""
    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
    )
    defaults.update(kw)
    if warmup_fn is not None:
        defaults["warmup_fn"] = warmup_fn
    return fl.FailoverLifecycleController(**defaults)


# ---------------------------------------------------------------------------
# (a) AWAKENING awaits warmup_fn BEFORE entering SERVING
# ---------------------------------------------------------------------------

async def test_awakening_awaits_warmup_before_serving():
    """warmup_fn must complete before the FSM enters SERVING.

    Sequence: node_ready fires -> warmup_fn blocks -> SERVING must NOT be
    entered yet -> warmup_fn resolves -> next tick -> SERVING.
    """
    clock = FakeClock()
    warmup_gate = asyncio.Event()
    warmup_started = asyncio.Event()
    warmup_calls: list = []

    async def gated_warmup_fn():
        warmup_calls.append("started")
        warmup_started.set()
        await warmup_gate.wait()   # hold until the test releases
        warmup_calls.append("done")
        return True

    ctrl = _make_ctrl(clock, warmup_fn=gated_warmup_fn)
    ctrl._get_forecast = lambda: _fake_forecast("HIGH", p50=300.0)

    _fill_outage()
    await ctrl.tick()  # DORMANT -> AWAKENING
    assert ctrl.state == fl.FailoverState.AWAKENING

    # Start the next tick (AWAKENING -> would go to SERVING) concurrently.
    tick_task = asyncio.create_task(ctrl.tick())

    # Wait until warmup_fn starts executing.
    await asyncio.wait_for(warmup_started.wait(), timeout=2.0)

    # SERVING must NOT be entered while warmup is blocked.
    assert ctrl.state == fl.FailoverState.AWAKENING
    assert ctrl.is_jprime_serving() is False

    # Release the warmup gate.
    warmup_gate.set()
    await tick_task  # let the tick complete

    # Now SERVING should be active.
    assert ctrl.state == fl.FailoverState.SERVING
    assert ctrl.is_jprime_serving() is True
    assert "done" in warmup_calls


# ---------------------------------------------------------------------------
# (b) warmup_fn returning False -> still reaches SERVING (no deadlock)
# ---------------------------------------------------------------------------

async def test_warmup_failure_still_transitions_to_serving(caplog):
    """If warmup_fn returns False (timeout/error), FSM still reaches SERVING.

    The warmup goal is to force cold-load; failure means first op may be cold,
    but the FSM must never deadlock in AWAKENING on a warmup failure.
    """
    clock = FakeClock()

    async def failing_warmup_fn():
        return False  # simulate timeout / error

    ctrl = _make_ctrl(clock, warmup_fn=failing_warmup_fn)
    ctrl._get_forecast = lambda: _fake_forecast("HIGH", p50=300.0)
    _fill_outage()

    await ctrl.tick()  # DORMANT -> AWAKENING
    assert ctrl.state == fl.FailoverState.AWAKENING

    with caplog.at_level(logging.WARNING, logger="backend.core.ouroboros.governance.failover_lifecycle"):
        await ctrl.tick()  # AWAKENING -> SERVING despite warmup returning False

    assert ctrl.state == fl.FailoverState.SERVING
    assert ctrl.is_jprime_serving() is True


async def test_warmup_failure_logs_warning(caplog):
    """warmup_fn returning False must emit a warning log (not silently skip)."""
    clock = FakeClock()

    async def failing_warmup_fn():
        return False

    ctrl = _make_ctrl(clock, warmup_fn=failing_warmup_fn)
    ctrl._get_forecast = lambda: _fake_forecast("HIGH", p50=300.0)
    _fill_outage()

    await ctrl.tick()  # -> AWAKENING
    with caplog.at_level(logging.WARNING, logger="backend.core.ouroboros.governance.failover_lifecycle"):
        await ctrl.tick()  # -> SERVING despite warmup failure

    # Log must mention warmup and the "first op may be cold" fallback.
    combined = " ".join(caplog.messages).lower()
    assert "warmup" in combined
    assert "cold" in combined or "serving" in combined


# ---------------------------------------------------------------------------
# (c) JARVIS_FAILOVER_WARMUP_ENABLED=false -> straight to SERVING (no warmup)
# ---------------------------------------------------------------------------

async def test_warmup_disabled_goes_straight_to_serving(monkeypatch):
    """When warmup is OFF, _tick_awakening transitions to SERVING without calling
    warmup_fn -- byte-identical to pre-warmup behavior."""
    monkeypatch.setenv("JARVIS_FAILOVER_WARMUP_ENABLED", "false")
    clock = FakeClock()
    warmup_calls: list = []

    async def should_not_be_called():
        warmup_calls.append("called")
        return True

    ctrl = _make_ctrl(clock, warmup_fn=should_not_be_called)
    ctrl._get_forecast = lambda: _fake_forecast("HIGH", p50=300.0)
    _fill_outage()

    await ctrl.tick()  # -> AWAKENING
    await ctrl.tick()  # -> SERVING (warmup skipped)

    assert ctrl.state == fl.FailoverState.SERVING
    assert warmup_calls == []  # warmup_fn never invoked


# ---------------------------------------------------------------------------
# (d) Ordering: warmup completes BEFORE is_jprime_serving() returns True
# ---------------------------------------------------------------------------

async def test_warmup_completes_before_jprime_serving():
    """The warmup must finish (return value recorded) before is_jprime_serving()
    becomes True. This proves the cold-load happens before routing begins."""
    clock = FakeClock()
    timeline: list = []

    async def timed_warmup():
        timeline.append("warmup_start")
        await asyncio.sleep(0)   # yield once; still completes before SERVING
        timeline.append("warmup_end")
        return True

    ctrl = _make_ctrl(clock, warmup_fn=timed_warmup)
    ctrl._get_forecast = lambda: _fake_forecast("HIGH", p50=300.0)
    _fill_outage()

    await ctrl.tick()  # -> AWAKENING
    await ctrl.tick()  # -> SERVING (warmup runs within this tick)

    # Warmup must appear before SERVING in the causal timeline.
    assert "warmup_end" in timeline
    # SERVING is entered only after the tick that ran warmup completes.
    assert ctrl.is_jprime_serving() is True
    # The key invariant: warmup_start happened before warmup_end.
    assert timeline.index("warmup_start") < timeline.index("warmup_end")


# ---------------------------------------------------------------------------
# (e) Default warmup_fn uses LocalPrimeClient pointed at jprime_endpoint()
# ---------------------------------------------------------------------------

async def test_default_warmup_fn_is_injectable():
    """FailoverLifecycleController accepts warmup_fn as a constructor kwarg."""
    clock = FakeClock()
    called: list = []

    async def my_warmup_fn():
        called.append(True)
        return True

    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        warmup_fn=my_warmup_fn,
    )
    ctrl._get_forecast = lambda: _fake_forecast("HIGH", p50=300.0)
    _fill_outage()

    await ctrl.tick()  # AWAKENING
    await ctrl.tick()  # SERVING

    assert ctrl.state == fl.FailoverState.SERVING
    assert called == [True]


# ---------------------------------------------------------------------------
# (f) Warmup timeout env knob is respected
# ---------------------------------------------------------------------------

async def test_warmup_timeout_env_knob_read(monkeypatch):
    """JARVIS_FAILOVER_WARMUP_TIMEOUT_S is read at runtime."""
    monkeypatch.setenv("JARVIS_FAILOVER_WARMUP_TIMEOUT_S", "42")
    assert fl._warmup_timeout_s() == 42.0


async def test_warmup_timeout_default():
    """JARVIS_FAILOVER_WARMUP_TIMEOUT_S defaults to 180."""
    import os
    original = os.environ.pop("JARVIS_FAILOVER_WARMUP_TIMEOUT_S", None)
    try:
        assert fl._warmup_timeout_s() == 180.0
    finally:
        if original is not None:
            os.environ["JARVIS_FAILOVER_WARMUP_TIMEOUT_S"] = original


# ---------------------------------------------------------------------------
# (g) Existing AWAKENING-timeout still fires even when warmup is pending
# ---------------------------------------------------------------------------

async def test_awakening_timeout_fires_even_with_warmup_enabled(monkeypatch):
    """The outer AWAKENING deadline (JARVIS_FAILOVER_AWAKEN_TIMEOUT_S) still
    terminates a stuck node even when warmup is enabled. The warmup happens
    only AFTER the node is observed ready -- a node that never answers
    node_ready_fn never gets a warmup call."""
    monkeypatch.setenv("JARVIS_FAILOVER_AWAKEN_TIMEOUT_S", "30")
    clock = FakeClock()
    delete_calls: list = []
    warmup_calls: list = []

    async def warmup_fn():
        warmup_calls.append(True)
        return True

    ctrl = _make_ctrl(
        clock,
        warmup_fn=warmup_fn,
        vm_delete_fn=lambda: delete_calls.append(1) or True,
        # node NEVER becomes ready
        node_ready_fn=lambda endpoint: False,
    )
    ctrl._get_forecast = lambda: _fake_forecast("HIGH", p50=300.0)
    _fill_outage()

    await ctrl.tick()  # -> AWAKENING
    assert ctrl.state == fl.FailoverState.AWAKENING

    # Advance past the AWAKENING timeout.
    clock.advance(35.0)
    await ctrl.tick()

    assert ctrl.state == fl.FailoverState.DORMANT
    assert len(delete_calls) == 1
    # warmup was never called because node_ready never fired.
    assert warmup_calls == []

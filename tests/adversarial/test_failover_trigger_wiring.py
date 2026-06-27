"""tests/adversarial/test_failover_trigger_wiring.py -- Task 4 proof + fix.

Proves (and fixes) that FailoverLifecycleController's awaken trigger
consumes the REAL per-route generation-failure signal
(ProviderHealthGradient.is_global_outage via record_sweep), NOT just the
DWHeartbeat.is_degrading HeavyProbe signal.

Run-#11/#12 bug (documented)
-----------------------------
DW's cheap GET /models HeavyProbe returned HEALTHY (partial single-token OK),
so DWHeartbeat.is_degrading was False. BUT the BACKGROUND *generation* route
collapsed (candidate_generator.record_sweep("background", success=False) × N),
driving is_global_outage("background") == True over a FULL window
(``dw_severed_queued`` → ``fallback_tolerance=queue``).

The FSM watched ONLY the configured JARVIS_FAILOVER_ROUTE key (default "dw") —
a key candidate_generator.record_sweep NEVER populates (it uses urgency-routing
keys: "background", "standard", "complex", "realtime") — so _real_outage()
always returned False and J-Prime never awoke (0 awaken / 0 instances.insert).

The fix
-------
Flip JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED default from "false" → "true" in
failover_lifecycle._any_route_outage_enabled(). The any-route logic and the fix
code path already existed behind the sub-gate (proven in
tests/governance/test_failover_trigger_signal.py). Task 4 proves the default
was wrong and flips it.

Adversary note (per plan spec Task 4)
--------------------------------------
The HTTP-level probe-healthy/gen-fail independence is already proven by
tests/adversarial/test_synthetic_adversary.py::TestIndependentPaths::
test_models_healthy_while_chat_fails_live_transport (Task 3 deliverable).
This file proves the FSM-level trigger wiring: that the gradient state
produced by such HTTP failures drives the awaken correctly.

Reuse chain:
  scripts/chaos_injector.FakeClock          — zero-sleep deterministic clock
  provider_quarantine.ProviderHealthGradient — the REAL gradient (no mock)
  failover_lifecycle.FailoverLifecycleController — the REAL FSM

asyncio_mode = auto (pytest.ini), no explicit @pytest.mark.asyncio needed.
ZERO real GCE / network — all boundaries are injected fakes.
"""
from __future__ import annotations

import os
import sys

import pytest

# Add repo root to sys.path so scripts/ is importable without touching pkg resolution.
# Uses APPEND (not insert-0) to avoid shadowing the existing pkg resolution order
# and prevent the double-import FailoverState identity failure seen when multiple
# failover test files are combined (the pre-existing contamination vector).
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.append(_REPO)  # append, not insert(0)

from scripts.chaos_injector import FakeClock  # reused: T3 / Task-4 spec FakeClock
import backend.core.ouroboros.governance.failover_lifecycle as fl
import backend.core.ouroboros.governance.provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
    FailoverState,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WINDOW = 5  # mirrors JARVIS_QUARANTINE_WINDOW default


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Set env vars and reset controller singleton; failover master ON.

    We do NOT rely on the pq gradient singleton here because
    test_provider_quarantine.py uses importlib.import_module to re-import
    provider_quarantine, which creates a SECOND module copy in sys.modules.
    After that, failover_lifecycle._gradient()'s lazy-import may resolve to
    the second copy's empty singleton, making our fills invisible to the FSM.

    Instead, we create a fresh ProviderHealthGradient instance per test and
    directly patch _gradient() on each controller object at construction time
    (see _make_ctrl below). This completely sidesteps the singleton resolution.
    """
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", str(_WINDOW))
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FAILOVER_WARMUP_ENABLED", "false")
    monkeypatch.setenv("JARVIS_JPRIME_COLDSTART_S", "100")
    monkeypatch.setenv("JARVIS_CRYO_AWAKEN_MARGIN", "1.5")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "120")
    yield
    fl._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _high_conf_forecast(p50: float = 300.0):
    """Fake HIGH-confidence slow forecast: R=p50 >> C*margin → cost gate says AWAKEN."""

    class _F:
        confidence = "HIGH"
        p50_s = p50
        p90_s = p50 * 2
        velocity_hint = 1.0
        samples = 10

    return _F()


def _make_ctrl(
    clock: FakeClock,
    awaken_calls: list,
    gradient: "pq.ProviderHealthGradient",
) -> FailoverLifecycleController:
    """Controller with injected fakes (zero real GCE / network).

    is_degrading_fn=False: simulates the HeavyProbe-healthy run-#11 mode —
    the cheap GET /models succeeds, so the probe-based heartbeat is NOT degrading.
    dw_probe_fn=True: the DW surface health probe returns OK (healthy).

    gradient is directly bound to ctrl._gradient() to bypass the singleton
    resolution, making tests robust against the pre-existing double-import
    contamination from test_provider_quarantine.py's importlib.import_module use.
    """

    async def _awaken(*, startup_script: str = "") -> bool:
        awaken_calls.append("awaken")
        return True

    ctrl = FailoverLifecycleController(
        vm_awaken_fn=_awaken,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: True,        # HeavyProbe: always HEALTHY
        node_ready_fn=lambda ep: True,
        clock_fn=clock,
        route="dw",                       # the FSM's configured route key
        on_serving_fn=lambda: None,
        warmup_fn=None,
        is_degrading_fn=lambda: False,    # heartbeat: NOT degrading (probe OK)
    )
    # ISOLATION: bind the gradient directly on the controller instance so that
    # _gradient()'s lazy import resolution doesn't reach the module singleton
    # (which may be the wrong copy after test_provider_quarantine.py re-imports).
    # Using a closure over the local `gradient` variable guarantees the FSM
    # reads exactly the same gradient object that _fill_gen_failures writes to.
    ctrl._gradient = lambda: gradient
    return ctrl


def _fresh_gradient() -> "pq.ProviderHealthGradient":
    """Create a fresh ProviderHealthGradient instance (not the singleton).

    Used by tests to get an isolated gradient that is passed explicitly to
    _make_ctrl (which binds it to ctrl._gradient). This sidesteps any singleton
    double-import contamination from test_provider_quarantine.py.
    """
    return pq.ProviderHealthGradient()


def _fill_gen_failures(
    gradient: "pq.ProviderHealthGradient",
    route: str,
    n: int = _WINDOW,
) -> None:
    """Simulate candidate_generator.record_sweep(route, success=False) × n.

    This is the REAL production signal: every time all DW models for a given
    urgency-routing route exhaust (dw_severed_queued → fallback_tolerance=queue),
    candidate_generator.py:4826 records one failed sweep for that route key.
    The gradient accumulates these into a rate-based outage deduction.

    Takes the gradient explicitly (not the singleton) for isolation robustness.
    """
    for _ in range(n):
        gradient.record_sweep(route, success=False)


# ---------------------------------------------------------------------------
# Pre-condition: gradient correctly signals outage on the background route
# ---------------------------------------------------------------------------

class TestGradientPreconditions:
    """Verify the ProviderHealthGradient produces the correct outage signal
    for the exact condition seen in run-#11: 'background' route collapses
    while the 'dw' configured-route key stays empty.

    These tests use _fresh_gradient() (bypassing the singleton) so they are
    immune to test-ordering contamination from test_provider_quarantine.py's
    importlib.import_module re-import.
    """

    def test_background_route_reaches_outage_after_full_window(self, monkeypatch):
        """record_sweep fills the window; is_global_outage fires at rate==0."""
        monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
        grad = _fresh_gradient()

        # Partial window: NOT an outage (fail-CLOSED).
        for _ in range(3):
            grad.record_sweep("background", success=False)
        assert grad.is_global_outage("background") is False, (
            "Partial window (3/5) must not declare outage"
        )

        # Full window, rate==0: outage.
        for _ in range(2):
            grad.record_sweep("background", success=False)
        assert grad.is_global_outage("background") is True, (
            "Full window (5/5), rate==0 must declare outage"
        )

    def test_configured_dw_route_never_populated_by_gen_sweeps(self, monkeypatch):
        """The FSM's 'dw' route key is NEVER populated by candidate_generator.

        candidate_generator uses urgency-routing keys (background / standard /
        complex / realtime). 'dw' is the JARVIS_FAILOVER_ROUTE default — a key
        that only the FSM itself reads during SERVING recovery probes.
        This is the structural mismatch that produced the run-#11 blindspot.
        """
        grad = _fresh_gradient()
        _fill_gen_failures(grad, "background", n=_WINDOW)
        assert grad.is_global_outage("background") is True
        assert grad.is_global_outage("dw") is False, (
            "The 'dw' key was never written by generation sweeps — "
            "is_global_outage('dw') must stay False"
        )

    def test_probe_does_not_write_to_gradient(self):
        """DWHeartbeat.is_degrading (probe) is independent of the gradient.

        The HeavyProbe writes to SurfaceHealthLedger, NOT to
        ProviderHealthGradient.record_sweep. A healthy probe leaves the gradient
        empty — the run-#11 condition: probe OK, gradient shows 'background' outage.

        Note: is_global_outage() lazily creates an empty deque for the queried route
        (a side effect of _get_window). We check tracked_routes() on a fresh
        ProviderHealthGradient instance to avoid that side-effect polluting the
        assertion.
        """
        # Fresh instance: assert truly-zero tracked routes (no side effects yet).
        fresh = _fresh_gradient()
        assert fresh.tracked_routes() == [], (
            "A fresh gradient (no sweeps recorded) must have no tracked routes"
        )

        # A second fresh instance to confirm is_global_outage returns False (conservative).
        grad = _fresh_gradient()
        assert grad.is_global_outage("background") is False
        assert grad.is_global_outage("dw") is False


# ---------------------------------------------------------------------------
# RED: proves the pre-fix bug
# ---------------------------------------------------------------------------

class TestPreFixBug:
    """Prove the documented run-#11 bug: with JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED
    forced OFF (the old default), probe-healthy + background-route-outage → awaken
    stays DORMANT."""

    async def test_probe_healthy_gen_fail_stays_dormant_with_gate_off(
        self, monkeypatch
    ):
        """RED: sub-gate=false (old default) → FSM watches 'dw' (unpopulated) →
        stays DORMANT even when 'background' route is at full outage (rate==0).

        This is the exact documented run-#11 behavior: J-Prime never awakened.
        """
        monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "false")
        gradient = _fresh_gradient()
        clock = FakeClock(start=1000.0)
        awaken_calls: list = []
        ctrl = _make_ctrl(clock, awaken_calls, gradient)
        monkeypatch.setattr(ctrl, "_get_forecast",
                            lambda: _high_conf_forecast(p50=300.0))

        # Pre-conditions: probe healthy, gen fails drive 'background' to outage.
        assert ctrl._is_degrading() is False, "HeavyProbe is healthy"
        _fill_gen_failures(gradient, "background", n=_WINDOW)
        assert gradient.is_global_outage("background") is True, (
            "Pre-condition: background outage confirmed"
        )
        assert gradient.is_global_outage("dw") is False, (
            "Pre-condition: 'dw' key never populated"
        )

        # Bug: _real_outage() checks is_global_outage("dw") → False → DORMANT.
        state = await ctrl.tick()
        assert state.name == FailoverState.DORMANT.name, (
            "Bug confirmed: FSM must stay DORMANT (sub-gate=false watches 'dw' "
            f"which is never populated by real gen sweeps); got {state!r}"
        )
        assert awaken_calls == [], "No awaken must have fired"


# ---------------------------------------------------------------------------
# GREEN: proves the fix works
# ---------------------------------------------------------------------------

class TestFix:
    """With JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED=true (fixed default),
    the FSM consumes any_route_in_outage() — the AUTHORITATIVE real-generation-
    failure signal — and awakens when any tracked generation route is at outage."""

    async def test_probe_healthy_gen_fail_awakens_with_gate_on(self, monkeypatch):
        """GREEN (gate explicitly on): same probe-healthy + background-outage
        condition → awaken fires."""
        monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
        gradient = _fresh_gradient()
        clock = FakeClock(start=1000.0)
        awaken_calls: list = []
        ctrl = _make_ctrl(clock, awaken_calls, gradient)
        monkeypatch.setattr(ctrl, "_get_forecast",
                            lambda: _high_conf_forecast(p50=300.0))

        assert ctrl._is_degrading() is False, "HeavyProbe still healthy"
        _fill_gen_failures(gradient, "background", n=_WINDOW)
        assert gradient.is_global_outage("background") is True

        state = await ctrl.tick()
        assert state.name == FailoverState.AWAKENING.name, (
            "Fix: FSM must awaken when any tracked generation route "
            "('background') hits full-window rate==0 outage "
            f"(got {state!r})"
        )
        assert awaken_calls == ["awaken"], "Awaken must have fired exactly once"

    async def test_default_gate_awakens_on_background_outage(self, monkeypatch):
        """GREEN (no env override): the DEFAULT is now 'true' — no env var set,
        so the hardcoded default in _any_route_outage_enabled() is exercised.

        This is the definitive proof of the fix. If this test fails, the default
        was not flipped.
        """
        # Remove any inherited env var so only the hardcoded default is used.
        monkeypatch.delenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", raising=False)
        gradient = _fresh_gradient()
        clock = FakeClock(start=1000.0)
        awaken_calls: list = []
        ctrl = _make_ctrl(clock, awaken_calls, gradient)
        monkeypatch.setattr(ctrl, "_get_forecast",
                            lambda: _high_conf_forecast(p50=300.0))

        _fill_gen_failures(gradient, "background", n=_WINDOW)

        state = await ctrl.tick()
        assert state.name == FailoverState.AWAKENING.name, (
            "Post-fix: default must awaken on real gen-failure signal "
            f"(no env var override needed); got {state!r}"
        )
        assert awaken_calls == ["awaken"]

    @pytest.mark.parametrize("route", ["background", "standard", "complex", "realtime"])
    async def test_all_candidate_generator_route_keys_trigger_awaken(
        self, monkeypatch, route
    ):
        """All urgency-routing keys that candidate_generator.record_sweep
        uses must trigger awaken — closing the route-key mismatch."""
        monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
        gradient = _fresh_gradient()
        clock = FakeClock(start=1000.0)
        awaken_calls: list = []
        ctrl = _make_ctrl(clock, awaken_calls, gradient)
        monkeypatch.setattr(ctrl, "_get_forecast",
                            lambda: _high_conf_forecast(p50=300.0))

        _fill_gen_failures(gradient, route, n=_WINDOW)
        assert gradient.is_global_outage(route) is True

        state = await ctrl.tick()
        assert state.name == FailoverState.AWAKENING.name, (
            f"Route '{route}': awaken must fire (run-#11 fix); got {state!r}"
        )
        assert "awaken" in awaken_calls


# ---------------------------------------------------------------------------
# DORMANT-SAFE: single blip / partial window must NOT awaken
# ---------------------------------------------------------------------------

class TestDormantSafe:
    """Prove that a transient blip (window not full / rate > 0) never awakens
    J-Prime. Fail-CLOSED: identical threshold to the quarantine Cryo-DLQ seal."""

    async def test_single_blip_does_not_awaken(self, monkeypatch):
        """A single generation failure (1/5 window) must NOT awaken J-Prime."""
        monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
        gradient = _fresh_gradient()
        clock = FakeClock(start=1000.0)
        awaken_calls: list = []
        ctrl = _make_ctrl(clock, awaken_calls, gradient)
        monkeypatch.setattr(ctrl, "_get_forecast",
                            lambda: _high_conf_forecast(p50=300.0))

        _fill_gen_failures(gradient, "background", n=1)
        assert gradient.is_global_outage("background") is False

        state = await ctrl.tick()
        assert state.name == FailoverState.DORMANT.name, (
            f"Single blip (1 of window=5) must NOT awaken J-Prime; got {state!r}"
        )
        assert awaken_calls == []

    async def test_partial_window_3_of_5_no_awaken(self, monkeypatch):
        """Three failures in a window of 5 → partial fill → NOT outage → DORMANT."""
        monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
        gradient = _fresh_gradient()
        clock = FakeClock(start=1000.0)
        awaken_calls: list = []
        ctrl = _make_ctrl(clock, awaken_calls, gradient)
        monkeypatch.setattr(ctrl, "_get_forecast",
                            lambda: _high_conf_forecast(p50=300.0))

        _fill_gen_failures(gradient, "background", n=3)

        state = await ctrl.tick()
        assert state.name == FailoverState.DORMANT.name, f"got {state!r}"
        assert awaken_calls == []

    async def test_full_window_one_success_no_awaken(self, monkeypatch):
        """Full window (5/5) with one success → rate > 0 → NOT outage → DORMANT.

        The gradient is a RATE (velocity), not a failure counter. One success in
        a window of 5 failures prevents the outage declaration. This is the
        fail-CLOSED guarantee: a brief recovery prevents spurious J-Prime spin-up.
        """
        monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
        gradient = _fresh_gradient()
        clock = FakeClock(start=1000.0)
        awaken_calls: list = []
        ctrl = _make_ctrl(clock, awaken_calls, gradient)
        monkeypatch.setattr(ctrl, "_get_forecast",
                            lambda: _high_conf_forecast(p50=300.0))

        for _ in range(4):
            gradient.record_sweep("background", success=False)
        gradient.record_sweep("background", success=True)  # one success: rate > 0
        assert gradient.is_global_outage("background") is False

        state = await ctrl.tick()
        assert state.name == FailoverState.DORMANT.name, f"got {state!r}"
        assert awaken_calls == []

    async def test_gate_off_with_background_outage_stays_dormant(self, monkeypatch):
        """When the sub-gate is explicitly OFF, even a full background outage
        does not awaken — byte-identical legacy behavior preserved."""
        monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "false")
        gradient = _fresh_gradient()
        clock = FakeClock(start=1000.0)
        awaken_calls: list = []
        ctrl = _make_ctrl(clock, awaken_calls, gradient)
        monkeypatch.setattr(ctrl, "_get_forecast",
                            lambda: _high_conf_forecast(p50=300.0))

        _fill_gen_failures(gradient, "background", n=_WINDOW)
        assert gradient.is_global_outage("background") is True

        state = await ctrl.tick()
        assert state.name == FailoverState.DORMANT.name, (
            f"Gate=false must preserve byte-identical legacy (single 'dw' check); "
            f"got {state!r}"
        )
        assert awaken_calls == []

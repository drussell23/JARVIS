"""Wire the live J-Prime in-flight count into the failover controller's drain.

The Zero-Drop Drain's ``in_flight_fn`` was injectable but defaulted to 0. The
PRECISE in-flight signal is the ``PrimeProvider.generate`` scope itself -- a
generation-scoped counter on the hoisted PrimeProviderState (reload-surviving),
incremented for the duration of each J-Prime generation. The controller resolves
it lazily (symmetric with how it resolves the DW heartbeat) -- no GLS poking, no
duplication of the existing in_flight_registry (which is op-lifecycle-scoped and
stamps provider at intake, before routing resolves).
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.providers as providers
import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    # Reset the hoisted counter.
    try:
        from backend.core.ouroboros.governance._governance_state import (
            get_prime_provider_state,
        )
        get_prime_provider_state().inflight = 0
    except Exception:
        pass
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def test_inflight_count_zero_when_idle():
    assert providers.get_jprime_inflight_count() == 0


def test_guard_increments_for_duration():
    assert providers.get_jprime_inflight_count() == 0
    with providers._jprime_inflight_guard():
        assert providers.get_jprime_inflight_count() == 1
        with providers._jprime_inflight_guard():
            assert providers.get_jprime_inflight_count() == 2  # nested generations
        assert providers.get_jprime_inflight_count() == 1
    assert providers.get_jprime_inflight_count() == 0  # drained on exit


def test_guard_decrements_on_exception():
    try:
        with providers._jprime_inflight_guard():
            assert providers.get_jprime_inflight_count() == 1
            raise RuntimeError("generation failed")
    except RuntimeError:
        pass
    assert providers.get_jprime_inflight_count() == 0  # never leaks a phantom op


def test_controller_inflight_defaults_to_jprime_count(monkeypatch):
    """With NO injected in_flight_fn, the controller reads the live J-Prime
    generation count from the providers module (lazy default)."""
    monkeypatch.setattr(providers, "get_jprime_inflight_count", lambda: 3)
    ctrl = FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
        # in_flight_fn intentionally omitted -> lazy default
    )
    assert ctrl._inflight_count() == 3


def test_injected_in_flight_fn_still_wins(monkeypatch):
    monkeypatch.setattr(providers, "get_jprime_inflight_count", lambda: 99)
    ctrl = FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
        in_flight_fn=lambda: 1,
    )
    assert ctrl._inflight_count() == 1  # explicit injection overrides the default

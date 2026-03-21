# tests/governance/test_gap6_gls_race.py
"""Structural tests: GovernedLoopService wires UserSignalBus and races in submit()."""
import inspect
import pytest
from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus


def test_gls_has_user_signal_bus_attribute():
    """GLS __init__ must declare _user_signal_bus attribute."""
    source = inspect.getsource(GovernedLoopService.__init__)
    assert "_user_signal_bus" in source, "_user_signal_bus must be initialized in __init__"


def test_gls_submit_references_user_signal_bus():
    """submit() must reference _user_signal_bus for the race path."""
    source = inspect.getsource(GovernedLoopService.submit)
    assert "_user_signal_bus" in source


def test_gls_submit_uses_asyncio_wait():
    """submit() must use asyncio.wait for the race (not just shielded_wait_for)."""
    source = inspect.getsource(GovernedLoopService.submit)
    assert "asyncio.wait" in source


def test_gls_submit_fires_ev_preempt():
    """submit() must fire EV_PREEMPT through the FSM on user stop."""
    source = inspect.getsource(GovernedLoopService.submit)
    assert "EV_PREEMPT" in source


def test_gls_submit_resets_bus_after_stop():
    """submit() must reset the bus after a stop so next op is not pre-stopped."""
    source = inspect.getsource(GovernedLoopService.submit)
    assert ".reset()" in source

"""Tests for ProactiveDrive.on_eligible() callback registration and firing."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from backend.core.topology.idle_verifier import LittlesLawVerifier, ProactiveDrive


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_always_idle_verifier(repo: str = "test") -> LittlesLawVerifier:
    """Return a LittlesLawVerifier whose is_idle() always returns True.

    We patch is_idle() because building a real idle signal requires samples
    spread over time — not practical in a unit test.
    """
    v = LittlesLawVerifier(repo, max_queue_depth=100)
    v.is_idle = lambda: (True, f"{repo}: mocked idle")
    return v


def _make_always_busy_verifier(repo: str = "test") -> LittlesLawVerifier:
    """Return a LittlesLawVerifier whose is_idle() always returns False."""
    v = LittlesLawVerifier(repo, max_queue_depth=100)
    v.is_idle = lambda: (False, f"{repo}: mocked busy")
    return v


def _make_drive(
    cooldown: float = 1.0,
    min_eligible: float = 0.001,
) -> ProactiveDrive:
    """Build a ProactiveDrive where idle transitions are achievable.

    min_eligible must be >0 so the `or` fallback in ProactiveDrive.__init__
    doesn't replace it with the environment default.  0.001 seconds (1ms) is
    effectively instant but avoids the truthiness trap.
    Uses mocked verifiers so the test doesn't depend on timing of sample ingestion.
    """
    return ProactiveDrive(
        jarvis_verifier=_make_always_idle_verifier("jarvis"),
        prime_verifier=_make_always_idle_verifier("prime"),
        reactor_verifier=_make_always_idle_verifier("reactor"),
        cooldown_seconds=cooldown,
        min_eligible_seconds=min_eligible,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOnEligibleRegisters:
    def test_on_eligible_registers_callback(self):
        """on_eligible() appends the callable to the internal list."""
        drive = _make_drive()
        cb = MagicMock()
        drive.on_eligible(cb)
        assert cb in drive._eligible_callbacks

    def test_multiple_callbacks_all_registered(self):
        """Multiple on_eligible() calls all register their callbacks."""
        drive = _make_drive()
        cb1, cb2, cb3 = MagicMock(), MagicMock(), MagicMock()
        drive.on_eligible(cb1)
        drive.on_eligible(cb2)
        drive.on_eligible(cb3)
        assert len(drive._eligible_callbacks) == 3


class TestCallbackFiresOnTransition:
    def test_callback_fires_on_transition_to_eligible(self):
        """Callback fires exactly once when state transitions to ELIGIBLE."""
        drive = _make_drive()  # min_eligible=0.001s
        cb = MagicMock()
        drive.on_eligible(cb)

        # First tick: all idle, _eligible_since is None → sets it, stays MEASURING
        state, _ = drive.tick()
        assert state == "MEASURING"
        cb.assert_not_called()

        # Sleep so the eligibility window elapses
        time.sleep(0.01)

        # Second tick: eligible_since is set and window has elapsed → ELIGIBLE
        state, _ = drive.tick()
        assert state == "ELIGIBLE"
        cb.assert_called_once()

    def test_callback_does_not_fire_when_already_eligible(self):
        """Callback fires only on the TRANSITION — subsequent ticks in ELIGIBLE don't re-fire."""
        drive = _make_drive()
        cb = MagicMock()
        drive.on_eligible(cb)

        drive.tick()          # MEASURING (sets eligible_since)
        time.sleep(0.01)
        drive.tick()          # ELIGIBLE + fires callback
        assert cb.call_count == 1

        # Additional ticks while ELIGIBLE — state machine returns ELIGIBLE
        # but the callback must not fire again.
        drive.tick()
        drive.tick()
        assert cb.call_count == 1

    def test_callback_not_fired_when_not_idle(self):
        """No callback when system is not idle."""
        drive = ProactiveDrive(
            jarvis_verifier=_make_always_busy_verifier("jarvis"),
            prime_verifier=_make_always_busy_verifier("prime"),
            reactor_verifier=_make_always_busy_verifier("reactor"),
            cooldown_seconds=1.0,
            min_eligible_seconds=0.001,
        )
        cb = MagicMock()
        drive.on_eligible(cb)

        drive.tick()
        time.sleep(0.01)
        drive.tick()
        drive.tick()
        cb.assert_not_called()

    def test_multiple_callbacks_all_fire_on_transition(self):
        """All registered callbacks fire when transitioning to ELIGIBLE."""
        drive = _make_drive()
        cb1, cb2 = MagicMock(), MagicMock()
        drive.on_eligible(cb1)
        drive.on_eligible(cb2)

        drive.tick()   # MEASURING
        time.sleep(0.01)
        drive.tick()   # ELIGIBLE

        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_no_callbacks_registered_does_not_error(self):
        """tick() reaching ELIGIBLE with no callbacks registered is safe."""
        drive = _make_drive()
        drive.tick()           # MEASURING
        time.sleep(0.01)
        state, _ = drive.tick()  # ELIGIBLE
        assert state == "ELIGIBLE"

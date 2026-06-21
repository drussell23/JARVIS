"""Tests for deterministic operator-presence detector + edge-triggered event watcher (spec §5.3)."""
from __future__ import annotations

import asyncio
import os
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance import operator_presence as op


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeBus:
    """Minimal fake bus that captures published events without requiring a running loop."""

    def __init__(self) -> None:
        self.published: List[Any] = []
        self.should_raise: bool = False

    async def publish(self, event: Any, persist: bool = False) -> str:
        if self.should_raise:
            raise RuntimeError("bus unavailable")
        self.published.append(event)
        return getattr(event, "event_id", "fake-id")


# ---------------------------------------------------------------------------
# Pure detection logic
# ---------------------------------------------------------------------------

def test_present_when_recent_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "45")
    # last input 5s ago → within threshold → present
    assert op._is_present(last_input_monotonic=100.0, now=105.0) is True


def test_idle_when_stale_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "45")
    # last input 100s ago → beyond 45s threshold → idle
    assert op._is_present(last_input_monotonic=100.0, now=200.0) is False


def test_exactly_at_threshold_is_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "45")
    # gap == threshold → NOT strictly less-than → idle
    assert op._is_present(last_input_monotonic=100.0, now=145.0) is False


def test_just_under_threshold_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "45")
    assert op._is_present(last_input_monotonic=100.0, now=144.9) is True


def test_present_via_liveness_probe() -> None:
    # Even if input is stale, an active liveness probe means present
    assert op._is_present(last_input_monotonic=0.0, now=9999.0, liveness=lambda: True) is True


def test_idle_when_liveness_probe_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "45")
    assert op._is_present(last_input_monotonic=0.0, now=9999.0, liveness=lambda: False) is False


def test_liveness_probe_exception_is_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "45")
    # A probe that raises must not propagate — treat as absent
    def _bad_probe() -> bool:
        raise RuntimeError("probe broken")

    result = op._is_present(last_input_monotonic=0.0, now=9999.0, liveness=_bad_probe)
    assert result is False


def test_default_idle_threshold_is_45s(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var set → default of 45 seconds applies."""
    monkeypatch.delenv("JARVIS_OPERATOR_IDLE_S", raising=False)
    assert op._is_present(last_input_monotonic=100.0, now=144.0) is True
    assert op._is_present(last_input_monotonic=100.0, now=146.0) is False


# ---------------------------------------------------------------------------
# Edge-trigger logic
# ---------------------------------------------------------------------------

def test_edge_trigger_emits_only_on_transition() -> None:
    """_transition() returns the topic string only on state change, else None."""
    w = op.OperatorPresenceWatcher()
    # Initially no state recorded — first call with present=True → transition to active
    assert w._transition(present=True) == op.EVENT_OPERATOR_ACTIVE
    # Same state again → no-op
    assert w._transition(present=True) is None
    # Flip to idle
    assert w._transition(present=False) == op.EVENT_OPERATOR_IDLE
    # Same idle state → no-op
    assert w._transition(present=False) is None
    # Flip back to active
    assert w._transition(present=True) == op.EVENT_OPERATOR_ACTIVE


def test_edge_trigger_starts_from_idle() -> None:
    """A fresh watcher that first sees idle → emits operator.idle."""
    w = op.OperatorPresenceWatcher()
    assert w._transition(present=False) == op.EVENT_OPERATOR_IDLE
    assert w._transition(present=False) is None


# ---------------------------------------------------------------------------
# module-level note_human_input + operator_present convenience
# ---------------------------------------------------------------------------

def test_note_human_input_stamps_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """note_human_input() makes operator_present() return True immediately."""
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "30")
    op.note_human_input()
    assert op.operator_present() is True


# ---------------------------------------------------------------------------
# async run_once with injected fake bus
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_once_publishes_active_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "45")

    bus = _FakeBus()
    w = op.OperatorPresenceWatcher()

    # Inject a present liveness probe
    await w.run_once(bus=bus, liveness=lambda: True)

    assert len(bus.published) == 1
    assert bus.published[0].topic == op.EVENT_OPERATOR_ACTIVE


@pytest.mark.asyncio
async def test_run_once_publishes_idle_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPERATOR_IDLE_S", "1")

    bus = _FakeBus()
    w = op.OperatorPresenceWatcher()

    # Force stale: last_input far in the past, no liveness probe
    op._last_input = 0.0
    await w.run_once(bus=bus, liveness=lambda: False)

    assert len(bus.published) == 1
    assert bus.published[0].topic == op.EVENT_OPERATOR_IDLE


@pytest.mark.asyncio
async def test_run_once_no_spam_on_unchanged_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

    bus = _FakeBus()
    w = op.OperatorPresenceWatcher()

    liveness = lambda: True  # always present
    await w.run_once(bus=bus, liveness=liveness)
    await w.run_once(bus=bus, liveness=liveness)
    await w.run_once(bus=bus, liveness=liveness)

    # Only one event on the first call (idle→active), rest are no-ops
    assert len(bus.published) == 1


@pytest.mark.asyncio
async def test_run_once_fail_soft_when_bus_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

    bus = _FakeBus()
    bus.should_raise = True
    w = op.OperatorPresenceWatcher()

    # Must not raise even if bus.publish() raises
    await w.run_once(bus=bus, liveness=lambda: True)


@pytest.mark.asyncio
async def test_run_returns_immediately_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "false")

    bus = _FakeBus()
    w = op.OperatorPresenceWatcher()

    # run() should return immediately (no loop) when master switch is off
    await asyncio.wait_for(w.run(bus=bus, liveness=lambda: True, interval_s=0.01), timeout=1.0)

    assert len(bus.published) == 0


@pytest.mark.asyncio
async def test_run_publishes_and_loops_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

    bus = _FakeBus()
    w = op.OperatorPresenceWatcher()

    # Run for just enough to fire a few iterations then cancel
    task = asyncio.create_task(w.run(bus=bus, liveness=lambda: True, interval_s=0.01))
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # First call must have published exactly one event (idle→active);
    # subsequent calls for unchanged state produce no extra events.
    assert len(bus.published) == 1
    assert bus.published[0].topic == op.EVENT_OPERATOR_ACTIVE


# ---------------------------------------------------------------------------
# Published event sanity (TrinityEvent fields)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_published_event_has_correct_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

    bus = _FakeBus()
    w = op.OperatorPresenceWatcher()
    await w.run_once(bus=bus, liveness=lambda: True)

    event = bus.published[0]
    assert event.topic == op.EVENT_OPERATOR_ACTIVE
    assert event.source is not None      # RepoType enum
    assert event.payload is not None     # dict
    assert isinstance(event.payload, dict)

"""Gap #4 — IntentDiscoverySensor event-source migration regression spine.

Pins the contract introduced by JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED.

Unlike the other gap-#4 sensors, this one is NOT an fs.changed.* consumer
— it subscribes to ConversationBridge turn events, and every trigger is a
DW inference call (real dollar cost). The test spine therefore focuses on:

  * Flag gating (shadow default off).
  * ConversationBridge observer registration / unregistration.
  * Silence window: no fire mid-sentence; fire only after silence.
  * Inference cooldown: hard floor between two DW calls.
  * Hourly cap: absolute ceiling regardless of silence + cooldown.
  * Counter invariant (evaluations === sum of sw_* counters).
  * Telemetry distinguishes conversation_silence_window vs fallback_poll.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    intent_discovery_sensor as m,
)
from backend.core.ouroboros.governance.intake.sensors.intent_discovery_sensor import (
    IntentDiscoverySensor,
)


class _SpyRouter:
    def __init__(self) -> None:
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


class _FakeBridge:
    """Records observer register/unregister + exposes configurable failure."""

    def __init__(self, fail_register: bool = False) -> None:
        self.observers: List[Any] = []
        self._fail = fail_register

    def register_turn_observer(self, observer: Any) -> None:
        if self._fail:
            raise RuntimeError("simulated bridge failure")
        self.observers.append(observer)

    def unregister_turn_observer(self, observer: Any) -> None:
        try:
            self.observers.remove(observer)
        except ValueError:
            pass


def _sensor(tmp_path: Path | None = None) -> IntentDiscoverySensor:
    return IntentDiscoverySensor(
        gls=None,
        router=_SpyRouter(),
        repo="jarvis",
        project_root=tmp_path or Path("."),
    )


# ---------------------------------------------------------------------------
# Flag helper + init
# ---------------------------------------------------------------------------

def test_events_enabled_reads_env_fresh(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    assert m.events_enabled() is True

    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "false")
    assert m.events_enabled() is False

    # Graduated 2026-04-20 — default is now "true" (event-primary).
    monkeypatch.delenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", raising=False)
    assert m.events_enabled() is True


def test_init_captures_events_mode(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    sensor = _sensor()
    assert sensor._events_mode is True
    assert sensor._events_received == 0
    assert sensor._last_turn_ts == 0.0
    assert sensor._last_inference_ts == 0.0
    assert sensor._hourly_fires == []

    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "false")
    sensor = _sensor()
    assert sensor._events_mode is False


# ---------------------------------------------------------------------------
# subscribe_to_bridge — flag gated
# ---------------------------------------------------------------------------

def test_subscribe_to_bridge_noop_when_flag_off(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "false")
    sensor = _sensor()
    bridge = _FakeBridge()

    sensor.subscribe_to_bridge(bridge)

    assert bridge.observers == [], (
        "flag off must NOT register an observer"
    )
    assert sensor._bridge_ref is None


def test_subscribe_to_bridge_registers_when_flag_on(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    sensor = _sensor()
    bridge = _FakeBridge()

    sensor.subscribe_to_bridge(bridge)

    assert len(bridge.observers) == 1
    assert bridge.observers[0] == sensor._on_turn
    assert sensor._bridge_ref is bridge


def test_subscribe_to_bridge_failure_is_non_fatal(monkeypatch: Any) -> None:
    """bridge.register_turn_observer raising must not propagate."""
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    sensor = _sensor()
    bridge = _FakeBridge(fail_register=True)

    # Must not raise
    sensor.subscribe_to_bridge(bridge)

    assert sensor._bridge_ref is None  # failed registration → no ref


# ---------------------------------------------------------------------------
# _on_turn — observer side (cheap, non-raising)
# ---------------------------------------------------------------------------

def test_on_turn_updates_last_turn_ts_and_counter(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    sensor = _sensor()

    assert sensor._last_turn_ts == 0.0
    sensor._on_turn(None)  # payload shape doesn't matter for the observer
    assert sensor._last_turn_ts > 0.0
    assert sensor._events_received == 1

    first_ts = sensor._last_turn_ts
    time.sleep(0.01)  # monotonic must advance
    sensor._on_turn(None)
    assert sensor._last_turn_ts >= first_ts
    assert sensor._events_received == 2


# ---------------------------------------------------------------------------
# _evaluate_silence_window — three-constraint storm guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_silence_window_rejects_when_no_turn_yet(monkeypatch: Any) -> None:
    """Constraint 1: no turn seen → no_turn_yet counter bumps, no fire."""
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    sensor = _sensor()

    async def _no_scan() -> list:
        raise AssertionError("scan_once must not fire before any turn")

    sensor.scan_once = _no_scan  # type: ignore[assignment]

    fired = await sensor._evaluate_silence_window()
    assert fired is False
    assert sensor._sw_no_turn_yet == 1
    assert sensor._sw_fires == 0


@pytest.mark.asyncio
async def test_silence_window_rejects_mid_sentence(monkeypatch: Any) -> None:
    """Constraint 2: turn just happened → silence_not_met, no fire."""
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    monkeypatch.setattr(m, "_INTENT_SILENCE_S", 30.0)
    sensor = _sensor()

    async def _no_scan() -> list:
        raise AssertionError("scan_once must not fire mid-sentence")

    sensor.scan_once = _no_scan  # type: ignore[assignment]

    sensor._on_turn(None)  # Turn just arrived — monotonic now.
    fired = await sensor._evaluate_silence_window()
    assert fired is False
    assert sensor._sw_silence_not_met == 1
    assert sensor._sw_fires == 0


@pytest.mark.asyncio
async def test_silence_window_fires_after_silence(monkeypatch: Any) -> None:
    """All constraints pass → fire; ledger + cooldown timestamp updated."""
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    # Zero silence window so we don't have to sleep in the test.
    monkeypatch.setattr(m, "_INTENT_SILENCE_S", 0.0)
    monkeypatch.setattr(m, "_INTENT_COOLDOWN_S", 300.0)
    monkeypatch.setattr(m, "_INTENT_HOURLY_CAP", 10)

    sensor = _sensor()

    scanned = {"n": 0}

    async def _fake_scan() -> list:
        scanned["n"] += 1
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    sensor._on_turn(None)
    fired = await sensor._evaluate_silence_window()

    assert fired is True
    assert scanned["n"] == 1
    assert sensor._sw_fires == 1
    assert sensor._last_inference_ts > 0.0
    assert len(sensor._hourly_fires) == 1


@pytest.mark.asyncio
async def test_silence_window_respects_inference_cooldown(monkeypatch: Any) -> None:
    """Constraint 3: just-fired → cooldown_active blocks the next fire."""
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    monkeypatch.setattr(m, "_INTENT_SILENCE_S", 0.0)
    monkeypatch.setattr(m, "_INTENT_COOLDOWN_S", 1000.0)  # huge cooldown
    monkeypatch.setattr(m, "_INTENT_HOURLY_CAP", 10)

    sensor = _sensor()

    async def _fake_scan() -> list:
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    sensor._on_turn(None)
    first = await sensor._evaluate_silence_window()
    assert first is True
    assert sensor._sw_fires == 1

    # Immediate second evaluation — cooldown must block it.
    sensor._on_turn(None)
    second = await sensor._evaluate_silence_window()
    assert second is False
    assert sensor._sw_cooldown_active == 1
    assert sensor._sw_fires == 1  # unchanged


@pytest.mark.asyncio
async def test_silence_window_respects_hourly_cap(monkeypatch: Any) -> None:
    """Constraint 4: hourly cap hit → hourly_cap_hit blocks further fires.

    Set silence=0 and cooldown=0 so the ONLY constraint that can stop us
    is the hourly cap (cap=2). Fire 4 times, expect 2 fires + 2 cap hits.
    """
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    monkeypatch.setattr(m, "_INTENT_SILENCE_S", 0.0)
    monkeypatch.setattr(m, "_INTENT_COOLDOWN_S", 0.0)
    monkeypatch.setattr(m, "_INTENT_HOURLY_CAP", 2)

    sensor = _sensor()

    async def _fake_scan() -> list:
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    for _ in range(4):
        sensor._on_turn(None)
        await sensor._evaluate_silence_window()

    assert sensor._sw_fires == 2
    assert sensor._sw_hourly_cap_hit == 2
    assert len(sensor._hourly_fires) == 2


@pytest.mark.asyncio
async def test_silence_window_counter_invariant(monkeypatch: Any) -> None:
    """Evaluations = sum of all _sw_* counters (no silent paths)."""
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    monkeypatch.setattr(m, "_INTENT_SILENCE_S", 100.0)  # never satisfied
    monkeypatch.setattr(m, "_INTENT_COOLDOWN_S", 0.0)
    monkeypatch.setattr(m, "_INTENT_HOURLY_CAP", 10)

    sensor = _sensor()

    async def _fake_scan() -> list:
        return []

    sensor.scan_once = _fake_scan  # type: ignore[assignment]

    # 1 evaluation before any turn → no_turn_yet
    await sensor._evaluate_silence_window()
    # 3 evaluations with a turn but silence never satisfied → silence_not_met
    for _ in range(3):
        sensor._on_turn(None)
        await sensor._evaluate_silence_window()

    total_evaluations = 4
    total_counters = (
        sensor._sw_fires
        + sensor._sw_no_turn_yet
        + sensor._sw_silence_not_met
        + sensor._sw_cooldown_active
        + sensor._sw_hourly_cap_hit
    )
    assert total_counters == total_evaluations


# ---------------------------------------------------------------------------
# Interval gate
# ---------------------------------------------------------------------------

def test_poll_interval_legacy_when_flag_off(monkeypatch: Any) -> None:
    """Flag off preserves the constructor-provided 15min poll interval."""
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "false")
    sensor = IntentDiscoverySensor(
        gls=None, router=_SpyRouter(),
        repo="jarvis", poll_interval_s=900.0,
    )
    assert sensor._events_mode is False
    assert sensor._poll_interval_s == 900.0


def test_init_events_mode_enables_fallback(monkeypatch: Any) -> None:
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")
    sensor = IntentDiscoverySensor(
        gls=None, router=_SpyRouter(),
        repo="jarvis", poll_interval_s=900.0,
    )
    assert sensor._events_mode is True
    assert m._INTENT_FALLBACK_INTERVAL_S > 0.0


# ---------------------------------------------------------------------------
# ConversationBridge integration smoke (real bridge instance)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_conversation_bridge_observer_fan_out(
    monkeypatch: Any,
) -> None:
    """A real ConversationBridge.record_turn → sensor._on_turn → counter."""
    # Enable the bridge master switch + our sensor's flag.
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "true")

    from backend.core.ouroboros.governance.conversation_bridge import (
        ConversationBridge,
    )

    bridge = ConversationBridge()
    sensor = _sensor()
    sensor.subscribe_to_bridge(bridge)

    assert sensor._bridge_ref is bridge
    assert sensor._events_received == 0

    bridge.record_turn(role="user", text="test turn", source="tui_user")

    # Give the observer a tick to run (it's synchronous so this is belt-
    # and-braces for future async upgrades).
    await asyncio.sleep(0)

    assert sensor._events_received == 1
    assert sensor._last_turn_ts > 0.0

    # Unregister on stop, should survive subsequent turns silently.
    sensor.stop()
    bridge.record_turn(role="user", text="after stop", source="tui_user")
    await asyncio.sleep(0)
    assert sensor._events_received == 1  # unchanged after stop

"""Task #12 — reconnect Karen's voice control-plane.

Karen had every downstream organ (tier table, cooldown/coalescing, canonical
TTS pipeline, /voice REPL) but nothing UPSTREAM ever fed her: `record_event`
had zero callers, so she was a fully-built voice that never heard the
organism. `run_karen_against_broker` is the missing control-plane — the
sibling of the Discord bridge tap — that subscribes to the canonical
StreamEventBroker and forwards every event to `record_event`.

Proof obligations:
  * Disabled master → the tap no-ops (never subscribes).
  * Enabled → the tap subscribes and forwards broker events to record_event
    (event_type / op_id / payload), then releases its subscription.
  * The stop event terminates the tap cleanly.
  * A record_event fault never stops the tap (fail-soft).
  * The harness boot arms the tap (guard against re-severing).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.karen_voice_announcer import (
    get_default_announcer,
    reset_announcer_for_tests,
    run_karen_against_broker,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    get_default_broker,
    reset_default_broker,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_TAP_POLL_S", "0.05")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    reset_announcer_for_tests()
    reset_default_broker()
    yield
    reset_announcer_for_tests()
    reset_default_broker()


async def _wait_for_subscriber(broker, *, timeout=2.0):
    """Poll until the tap has registered its subscription (avoids a
    publish-before-subscribe race)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if broker.subscriber_count >= 1:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("tap never subscribed")


@pytest.mark.asyncio
async def test_tap_noops_when_disabled(clean, monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "false")
    broker = get_default_broker()
    stop = asyncio.Event()
    # Returns immediately without subscribing.
    await asyncio.wait_for(run_karen_against_broker(stop=stop), timeout=2.0)
    assert broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_tap_forwards_broker_events_to_karen(clean, monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    broker = get_default_broker()
    karen = get_default_announcer()

    seen = []
    monkeypatch.setattr(
        karen, "record_event",
        lambda **kw: seen.append(kw),
    )

    stop = asyncio.Event()
    task = asyncio.create_task(run_karen_against_broker(stop=stop))
    try:
        await _wait_for_subscriber(broker)
        broker.publish("task_completed", "op-42", {"status": "ok"})
        broker.publish("circuit_breaker_tripped", "op-99", {"reason": "5xx"})
        # Let the tap drain.
        for _ in range(40):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    types = {e["event_type"] for e in seen}
    assert "task_completed" in types
    assert "circuit_breaker_tripped" in types
    hit = next(e for e in seen if e["event_type"] == "task_completed")
    assert hit["op_id"] == "op-42"
    assert hit["payload"].get("status") == "ok"
    # Subscription released on exit.
    assert broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_tap_stops_on_stop_event(clean, monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    broker = get_default_broker()
    stop = asyncio.Event()
    task = asyncio.create_task(run_karen_against_broker(stop=stop))
    await _wait_for_subscriber(broker)
    stop.set()
    # Must terminate promptly (poll is 0.05s).
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    assert broker.subscriber_count == 0


@pytest.mark.asyncio
async def test_tap_is_failsoft_on_record_event_error(clean, monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "true")
    broker = get_default_broker()
    karen = get_default_announcer()

    calls = {"n": 0}

    def _boom(**kw):
        calls["n"] += 1
        raise RuntimeError("record_event blew up")

    monkeypatch.setattr(karen, "record_event", _boom)
    stop = asyncio.Event()
    task = asyncio.create_task(run_karen_against_broker(stop=stop))
    try:
        await _wait_for_subscriber(broker)
        broker.publish("task_completed", "op-1", {})
        broker.publish("task_completed", "op-2", {})
        for _ in range(40):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    # The tap kept forwarding despite each call raising — it never died.
    assert calls["n"] >= 2
    assert task.done() and task.exception() is None


def test_harness_arms_the_karen_tap():
    """Guard against re-severing: the harness boot must import + arm the
    tap, gated on Karen's master flag."""
    src = (_REPO / "backend/core/ouroboros/battle_test/harness.py").read_text()
    assert "run_karen_against_broker" in src
    assert "master_enabled as _karen_enabled" in src
    assert "_karen_voice_task" in src

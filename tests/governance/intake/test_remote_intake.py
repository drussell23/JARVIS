# -*- coding: utf-8 -*-
"""Remote intake path (Stage-2 Task 3): Body shim + Brain bridge.

Proves the wire contract between ``RemoteIntakeRouter`` (the router-shaped
shim Body sensors call on the Mac) and ``RemoteIntakeBridge`` (the Brain-side
subscriber that feeds the real ``UnifiedIntakeRouter``):

  (a) ROUND-TRIP: a real envelope published through the shim on the Mac-side
      TrinityEventBus crosses the Task-1 TrinityBusBridge pair (in-proc pump)
      and arrives at a recording fake router on the Brain side as an
      ``IntentEnvelope`` whose ``to_dict()`` equals the original's.
  (b) REPLAY-DEDUP HONESTY: the SAME payload delivered twice reaches the
      router TWICE with equal ``dedup_key`` -- dedup is the REAL router's
      job (``_is_duplicate``); the bridge documents, not re-implements, it.
  (c) MALFORMED PAYLOAD: ``{"garbage": True}`` is logged-and-dropped --
      never raises, router never called (fail-soft, never crash the bus).
  (d) SHIM FAIL-SOFT: a publish failure surfaces as the valid router
      verdict ``"backpressure"`` instead of crashing the sensor.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, List

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)
from backend.core.ouroboros.governance.intake.remote_intake import (
    TOPIC_REMOTE_SIGNAL,
    RemoteIntakeBridge,
    RemoteIntakeRouter,
)
from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (
    TRINITY_OP_PREFIX,
    TrinityBusBridge,
)
from backend.core.trinity_event_bus import RepoType, TrinityEvent, TrinityEventBus


async def _mk_bus() -> TrinityEventBus:
    # Deviation (deliberate, documented): two REAL TrinityEventBus instances
    # in one process stand in for two genuinely separate hosts (Mac + Brain
    # VM). Suppress the in-process UDP multicast shortcut so the buses can
    # ONLY see each other through the TrinityBusBridge under test. Precedent
    # + full rationale: tests/governance/transport/
    # test_trinity_bus_bridge.py::_mk_bus.
    prev = os.environ.get("TRINITY_MULTICAST_ENABLED")
    os.environ["TRINITY_MULTICAST_ENABLED"] = "false"
    try:
        return await TrinityEventBus.create(local_repo=RepoType.JARVIS)
    finally:
        if prev is None:
            os.environ.pop("TRINITY_MULTICAST_ENABLED", None)
        else:
            os.environ["TRINITY_MULTICAST_ENABLED"] = prev


async def _pump(src: StreamEventBroker, dst: StreamEventBroker, stop: asyncio.Event,
                 seen: set) -> None:
    """In-proc stand-in for the WS pair -- lifted verbatim from Task 1's
    test file (tests/governance/transport/test_trinity_bus_bridge.py::_pump,
    see its docstring for the (op_id, origin) shared-seen-set rationale)."""
    sub = src.subscribe()
    while not stop.is_set():
        try:
            ev = await asyncio.wait_for(sub.queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        if not ev.op_id.startswith(TRINITY_OP_PREFIX):
            continue
        key = (ev.op_id, ev.payload.get("origin"))
        if key in seen:
            continue
        seen.add(key)
        dst.publish(ev.event_type, ev.op_id, dict(ev.payload))


def _mk_envelope() -> IntentEnvelope:
    return make_envelope(
        source="test_failure",
        description="remote intake round-trip probe",
        target_files=("backend/core/example.py",),
        repo="jarvis",
        confidence=0.9,
        urgency="high",
        evidence={"signature": "stage2-task3-remote-intake"},
        requires_human_ack=False,
    )


class _RecordingRouter:
    """Fake real-router: async def ingest(env) records and accepts."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.calls.append(envelope)
        return "enqueued"


# --------------------------------------------------------------------------- #
# (a) full round-trip over two real buses + Task-1 bridge pair + pump
# --------------------------------------------------------------------------- #
def test_round_trip_body_shim_to_brain_router():
    async def scenario():
        mac_bus, brain_bus = await _mk_bus(), await _mk_bus()
        mac_broker, brain_broker = StreamEventBroker(), StreamEventBroker()
        stop = asyncio.Event()
        pump_seen: set = set()
        pumps = [
            asyncio.ensure_future(_pump(mac_broker, brain_broker, stop, pump_seen)),
            asyncio.ensure_future(_pump(brain_broker, mac_broker, stop, pump_seen)),
        ]
        mac = TrinityBusBridge(mac_bus, mac_broker,
                               outbound_topics=["intake.remote_signal.*"],
                               source_id="mac")
        brain = TrinityBusBridge(brain_bus, brain_broker,
                                 outbound_topics=["actuation.*"],
                                 source_id="brain")
        await mac.start(); await brain.start()

        router = _RecordingRouter()
        bridge = RemoteIntakeBridge(brain_bus, router)
        await bridge.start()

        shim = RemoteIntakeRouter(mac_bus)
        env = _mk_envelope()
        verdict = await shim.ingest(env)
        await asyncio.sleep(1.0)

        stop.set()
        for p in pumps:
            p.cancel()
        await bridge.stop()
        await mac.stop(); await brain.stop()
        return verdict, env, router.calls

    verdict, env, calls = asyncio.get_event_loop().run_until_complete(scenario())
    assert verdict == "enqueued"
    assert len(calls) == 1, f"exactly one envelope must arrive: {calls!r}"
    got = calls[0]
    assert isinstance(got, IntentEnvelope)
    assert got.to_dict() == env.to_dict()


# --------------------------------------------------------------------------- #
# (b) replay-dedup honesty: dedup belongs to the REAL router, not the bridge
# --------------------------------------------------------------------------- #
def test_same_payload_twice_reaches_router_twice_with_equal_dedup_key():
    async def scenario():
        router = _RecordingRouter()
        bridge = RemoteIntakeBridge(trinity_bus=None, router=router)
        payload = _mk_envelope().to_dict()
        await bridge._on_signal(
            TrinityEvent(topic=TOPIC_REMOTE_SIGNAL, payload=dict(payload)))
        await bridge._on_signal(
            TrinityEvent(topic=TOPIC_REMOTE_SIGNAL, payload=dict(payload)))
        return router.calls

    calls = asyncio.get_event_loop().run_until_complete(scenario())
    assert len(calls) == 2, "bridge must NOT dedup -- the real router owns it"
    assert calls[0].dedup_key == calls[1].dedup_key


# --------------------------------------------------------------------------- #
# (c) malformed payload: log-and-drop, never raise, router untouched
# --------------------------------------------------------------------------- #
def test_malformed_payload_dropped_without_raising():
    async def scenario():
        router = _RecordingRouter()
        bridge = RemoteIntakeBridge(trinity_bus=None, router=router)
        await bridge._on_signal(
            TrinityEvent(topic=TOPIC_REMOTE_SIGNAL, payload={"garbage": True}))
        return router.calls

    calls = asyncio.get_event_loop().run_until_complete(scenario())
    assert calls == []


# --------------------------------------------------------------------------- #
# (d) shim fail-soft: publish failure -> "backpressure", never a raise
# --------------------------------------------------------------------------- #
def test_shim_returns_backpressure_when_publish_fails():
    class _ExplodingBus:
        async def publish_raw(self, topic: str, data: dict) -> str:
            raise RuntimeError("wire down")

    async def scenario():
        shim = RemoteIntakeRouter(_ExplodingBus())
        return await shim.ingest(_mk_envelope())

    verdict = asyncio.get_event_loop().run_until_complete(scenario())
    assert verdict == "backpressure"

# -*- coding: utf-8 -*-
"""TrinityBusBridge: topic-allowlisted mirroring with origin-tagged loop safety.

Two REAL TrinityEventBus instances linked through two REAL StreamEventBrokers
and an in-proc pump (the WS bridge's proven contract) -- publish on one side
is observed by subscribers on the other, exactly once, allowlist enforced.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (
    TRINITY_OP_PREFIX,
    TrinityBusBridge,
)
from backend.core.trinity_event_bus import TrinityEventBus, RepoType


async def _mk_bus() -> TrinityEventBus:
    # Deviation (deliberate, documented): two REAL TrinityEventBus instances
    # constructed with the SAME local_repo in one process are, in this test,
    # standing in for two genuinely separate hosts (Mac + Brain VM) that are
    # NOT on a shared broadcast domain in the real Stage-2 topology. Without
    # this, CrossRepoTransport's own UDP multicast (an existing, unrelated
    # channel -- see backend/core/trinity_event_bus.py:551-560) lets the two
    # buses see each other directly on localhost, bypassing the
    # TrinityBusBridge under test entirely and producing a storm that has
    # nothing to do with the bridge's origin-tag loop guard. Established
    # precedent for suppressing this in-process artifact:
    # backend/core/ouroboros/battle_test/ov_smoke.py:456-458
    # ("TRINITY_MULTICAST_ENABLED=false  # suppress UDP socket").
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
    """In-proc stand-in for the WS pair: mirror trinity-prefixed broker events
    once, by LOGICAL identity (the real wire dedups + suppresses reflections).

    Deviation from the brief's literal sketch (documented): the original
    keyed ``seen`` by ``ev.event_id`` and scoped it per-direction (one set
    per one-way pump). ``StreamEventBroker.publish()`` mints a brand-new
    sequential id on every hop (no way to preserve one across a relay -- see
    its signature, no event_id parameter), so a per-direction event_id-keyed
    set can NEVER catch a reflection: mac -> brain -> mac each leg gets a
    fresh id and looks unseen forever, which is exactly the "Stage-1
    reflection-storm class" this test's docstring names. The REAL WS bridge
    (bus_bridge_client.py:216-220, bus_bridge_server.py:59-62/118-119, fixed
    2026-07-04 for this identical failure mode) instead tracks ids it
    RE-MINTED from an inbound peer event and has its OUTBOUND pump skip
    exactly those ids. This is the in-proc analog: dedup by the STABLE
    (op_id, origin) pair carried unchanged in the payload across every hop,
    with ONE ``seen`` set SHARED by both directions of a bidirectional pump
    pair -- so a logical event crosses the link at most once total, not once
    per direction.
    """
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


def test_publish_crosses_once_and_only_allowlisted_topics():
    async def scenario():
        mac_bus, brain_bus = await _mk_bus(), await _mk_bus()
        mac_broker, brain_broker = StreamEventBroker(), StreamEventBroker()
        stop = asyncio.Event()
        pump_seen: set = set()
        pumps = [asyncio.ensure_future(_pump(mac_broker, brain_broker, stop, pump_seen)),
                 asyncio.ensure_future(_pump(brain_broker, mac_broker, stop, pump_seen))]
        mac = TrinityBusBridge(mac_bus, mac_broker,
                               outbound_topics=["intake.remote_signal.*"],
                               source_id="mac")
        brain = TrinityBusBridge(brain_bus, brain_broker,
                                 outbound_topics=["actuation.*"],
                                 source_id="brain")
        await mac.start(); await brain.start()

        got: List[Dict[str, Any]] = []

        async def handler(ev):
            got.append({"topic": ev.topic, "payload": dict(ev.payload)})

        await brain_bus.subscribe("intake.remote_signal.*", handler)
        await mac_bus.publish_raw("intake.remote_signal.voice", {"k": 1})
        await mac_bus.publish_raw("fs.changed.src", {"k": 2})  # NOT allowlisted
        await asyncio.sleep(1.0)

        stop.set()
        for p in pumps:
            p.cancel()
        await mac.stop(); await brain.stop()
        return got

    got = asyncio.get_event_loop().run_until_complete(scenario())
    assert len(got) == 1, f"exactly the allowlisted topic, exactly once: {got!r}"
    assert got[0]["topic"] == "intake.remote_signal.voice"
    assert got[0]["payload"]["k"] == 1


def test_no_ping_pong_amplification_at_trinity_layer():
    """An imported event must NEVER be re-forwarded even when its topic matches
    the local outbound allowlist (the Stage-1 reflection-storm class)."""
    async def scenario():
        mac_bus, brain_bus = await _mk_bus(), await _mk_bus()
        mac_broker, brain_broker = StreamEventBroker(), StreamEventBroker()
        stop = asyncio.Event()
        pump_seen: set = set()
        pumps = [asyncio.ensure_future(_pump(mac_broker, brain_broker, stop, pump_seen)),
                 asyncio.ensure_future(_pump(brain_broker, mac_broker, stop, pump_seen))]
        # SAME topic allowlisted on BOTH sides -- the storm setup.
        mac = TrinityBusBridge(mac_bus, mac_broker,
                               outbound_topics=["shared.topic"], source_id="mac")
        brain = TrinityBusBridge(brain_bus, brain_broker,
                                 outbound_topics=["shared.topic"], source_id="brain")
        await mac.start(); await brain.start()

        await mac_bus.publish_raw("shared.topic", {"n": 1})
        await asyncio.sleep(1.5)  # long enough for any storm

        mac_n = len([e for e in mac_broker.recent_history(limit=500)
                     if e.op_id.startswith(TRINITY_OP_PREFIX)])
        brain_n = len([e for e in brain_broker.recent_history(limit=500)
                       if e.op_id.startswith(TRINITY_OP_PREFIX)])
        stop.set()
        for p in pumps:
            p.cancel()
        await mac.stop(); await brain.stop()
        return mac_n, brain_n

    mac_n, brain_n = asyncio.get_event_loop().run_until_complete(scenario())
    assert mac_n == 1, f"mac broker must hold exactly the original: {mac_n}"
    assert brain_n == 1, f"brain broker must hold exactly the mirror: {brain_n}"

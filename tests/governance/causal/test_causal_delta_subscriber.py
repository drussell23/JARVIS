# -*- coding: utf-8 -*-
"""Domain-1 Staging-1 Task 2 -- CausalDeltaSubscriber (Brain receiver).

Proves the five brief contracts against a REAL ``TrinityEventBus``
(``TRINITY_MULTICAST_ENABLED=false`` to suppress the in-process UDP shortcut;
precedent: tests/governance/transport/test_trinity_bus_bridge.py::_mk_bus):

  (a) three deltas (jarvis/prime/reactor) are observed, and the source repo is
      read REFLECTIVELY from the IN-PAYLOAD ``lineage.repo`` (Mandate 2: the
      RepoType rides as reflective metadata inside the data payload, NOT typed
      on the transport) -- an event whose TOPIC says ``causal.delta.jarvis`` AND
      whose ``event.source`` enum is ``RepoType.REACTOR`` but whose payload
      ``lineage.repo`` is ``"prime"`` is recorded as PRIME (payload is
      authoritative; neither topic-string nor event.source routing);
  (b) the SAME delta published twice within the 60s window is observed once
      (the bus's own fingerprint dedup -- the subscriber adds no dedup algo);
  (c) out-of-order ``emit_seq`` within one repo -> ``observed()`` for that repo
      is in ``emit_seq`` order (the Lamport guarantee);
  (d) a malformed envelope (missing lineage) is dropped, no raise into the bus;
      a delta whose in-payload ``lineage.repo`` is BROADCAST is dropped (a
      causal delta needs a concrete source);
  (e) organism_bus_host constructs + starts the subscriber when the host serves
      (recording fake via monkeypatched lazy import) and is byte-identical
      (subscriber never constructed) when the master flag is off.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, List, Optional

from backend.core.trinity_event_bus import RepoType, TrinityEvent, TrinityEventBus


# ---------------------------------------------------------------------------
# Real-bus fixture (multicast suppressed) -- precedent: _mk_bus in
# tests/governance/transport/test_trinity_bus_bridge.py.
# ---------------------------------------------------------------------------

async def _mk_bus() -> TrinityEventBus:
    prev = os.environ.get("TRINITY_MULTICAST_ENABLED")
    os.environ["TRINITY_MULTICAST_ENABLED"] = "false"
    try:
        bus = await TrinityEventBus.create(local_repo=RepoType.JARVIS)
    finally:
        if prev is None:
            os.environ.pop("TRINITY_MULTICAST_ENABLED", None)
        else:
            os.environ["TRINITY_MULTICAST_ENABLED"] = prev
    # The real transport file-syncs non-local-source events to a shared
    # ~/.jarvis/trinity/bus_sync/<source>_events.jsonl and a fresh bus REPLAYS
    # them on boot (it skips only the local repo). Since these tests publish
    # PRIME/REACTOR-sourced deltas, clear any stale sync files so one test's
    # events never bleed into the next. (Publishing with target=local_repo --
    # see _publish -- also skips the write, so this only self-heals leftovers.)
    try:
        for stale in bus._transport._sync_dir.glob("*_events.jsonl"):
            stale.unlink()
    except Exception:  # noqa: BLE001 -- best-effort isolation
        pass
    return bus


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _envelope(repo: str, emit_seq: int, head_sha: str = "h",
              parent_sha: str = "p", merge_base: str = "m") -> dict:
    """A content-free causal-delta envelope (shape of ``stamp_delta``)."""
    return {
        "delta": {"repo": repo, "file_level_churn": True},
        "lineage": {
            "repo": repo,
            "head_sha": head_sha,
            "parent_sha": parent_sha,
            "merge_base": merge_base,
            "emit_seq": emit_seq,
        },
    }


async def _publish(bus: TrinityEventBus, topic: str, source: RepoType,
                   envelope: dict) -> None:
    # target=local_repo (JARVIS) keeps publish() from writing the event to the
    # shared file-sync transport (publish only calls transport.send when
    # target is BROADCAST or != local_repo) -- so cross-instance/cross-test
    # replay can't happen. Local delivery is unaffected (the event still lands
    # on the local queue and reaches subscribers). ``source`` is independent of
    # ``target`` -- the reflective read under test is of ``event.source``.
    await bus.publish(TrinityEvent(
        topic=topic, source=source, target=RepoType.JARVIS, payload=envelope))


async def _settle(sub: Any, expected: int, timeout: float = 3.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if sub.observed_count() >= expected:
            return
        await asyncio.sleep(0.02)


def _import_subscriber():
    from backend.core.ouroboros.governance.causal.causal_delta_subscriber import (
        CausalDeltaSubscriber,
    )
    return CausalDeltaSubscriber


# ---------------------------------------------------------------------------
# (a) three deltas observed; repo read reflectively from event.source
# ---------------------------------------------------------------------------
def test_three_deltas_observed_repo_read_reflectively():
    CausalDeltaSubscriber = _import_subscriber()

    async def scenario():
        bus = await _mk_bus()
        sub = CausalDeltaSubscriber(bus)
        await sub.start()
        try:
            # jarvis + reactor: honest topic/source/payload.
            await _publish(bus, "causal.delta.jarvis", RepoType.JARVIS,
                           _envelope("jarvis", 1, head_sha="jh"))
            await _publish(bus, "causal.delta.reactor", RepoType.REACTOR,
                           _envelope("reactor", 1, head_sha="rh"))
            # DOUBLE LIE: topic says jarvis AND event.source says REACTOR, but
            # the in-payload lineage.repo says "prime". The PAYLOAD wins ->
            # recorded as prime. Proves neither topic-string nor event.source
            # routing -- Mandate-2 in-payload identity is authoritative.
            await _publish(bus, "causal.delta.jarvis", RepoType.REACTOR,
                           _envelope("prime", 1, head_sha="ph"))
            await _settle(sub, 3)
            return sub.observed()
        finally:
            await sub.stop()
            await bus.stop()

    observed = _run(scenario())
    repos = {repo for (repo, _seq, _sha) in observed}
    assert repos == {"jarvis", "prime", "reactor"}, observed
    # the double-lying event is recorded under its PAYLOAD repo (prime), and its
    # head_sha proves it is the same event.
    prime = [rec for rec in observed if rec[0] == "prime"]
    assert prime == [("prime", 1, "ph")], observed


# ---------------------------------------------------------------------------
# (b) same delta twice within 60s -> observed once (bus fingerprint dedup)
# ---------------------------------------------------------------------------
def test_duplicate_within_window_observed_once():
    CausalDeltaSubscriber = _import_subscriber()

    async def scenario():
        bus = await _mk_bus()
        sub = CausalDeltaSubscriber(bus)
        await sub.start()
        try:
            env = _envelope("jarvis", 5, head_sha="dup")
            await _publish(bus, "causal.delta.jarvis", RepoType.JARVIS, env)
            await _publish(bus, "causal.delta.jarvis", RepoType.JARVIS, env)
            await _settle(sub, 1)
            await asyncio.sleep(0.2)  # give any second delivery a chance
            return sub.observed()
        finally:
            await sub.stop()
            await bus.stop()

    observed = _run(scenario())
    assert observed == [("jarvis", 5, "dup")], observed


def test_belt_and_suspenders_idempotency_outside_window():
    """The seen-set guards a REPLAY outside the bus's 60s window (the bus dedup
    would have expired). Drive the handler directly to simulate that replay."""
    CausalDeltaSubscriber = _import_subscriber()

    async def scenario():
        sub = CausalDeltaSubscriber(trinity_bus=object())
        ev = TrinityEvent(topic="causal.delta.jarvis", source=RepoType.JARVIS,
                          payload=_envelope("jarvis", 9, head_sha="rep"))
        await sub._on_delta(ev)
        await sub._on_delta(ev)  # replay -- seen-set must drop it
        return sub.observed()

    observed = _run(scenario())
    assert observed == [("jarvis", 9, "rep")], observed


# ---------------------------------------------------------------------------
# (c) out-of-order emit_seq within one repo -> observed() is emit_seq-ordered
# ---------------------------------------------------------------------------
def test_out_of_order_emit_seq_ordered_per_repo():
    CausalDeltaSubscriber = _import_subscriber()

    async def scenario():
        bus = await _mk_bus()
        sub = CausalDeltaSubscriber(bus)
        await sub.start()
        try:
            # arrive out of order: 5 then 2 then 8 then 1
            for seq, sha in ((5, "s5"), (2, "s2"), (8, "s8"), (1, "s1")):
                await _publish(bus, "causal.delta.prime", RepoType.PRIME,
                               _envelope("prime", seq, head_sha=sha))
            await _settle(sub, 4)
            return sub.observed()
        finally:
            await sub.stop()
            await bus.stop()

    observed = _run(scenario())
    prime_seqs = [seq for (repo, seq, _sha) in observed if repo == "prime"]
    assert prime_seqs == [1, 2, 5, 8], observed


# ---------------------------------------------------------------------------
# (d) malformed envelope dropped; BROADCAST source dropped -- no raise
# ---------------------------------------------------------------------------
def test_malformed_and_broadcast_dropped_no_raise():
    CausalDeltaSubscriber = _import_subscriber()

    async def scenario():
        bus = await _mk_bus()
        sub = CausalDeltaSubscriber(bus)
        await sub.start()
        try:
            # missing lineage entirely
            await _publish(bus, "causal.delta.jarvis", RepoType.JARVIS,
                           {"delta": {"repo": "jarvis"}})
            # lineage present but missing required keys
            await _publish(bus, "causal.delta.jarvis", RepoType.JARVIS,
                           {"delta": {}, "lineage": {"repo": "jarvis"}})
            # in-payload lineage.repo == BROADCAST: a TARGET semantic, never a
            # valid causal SOURCE (concrete event.source is irrelevant now --
            # the payload is authoritative).
            await _publish(bus, "causal.delta.jarvis", RepoType.JARVIS,
                           _envelope(RepoType.BROADCAST.value, 3))
            # in-payload lineage.repo not a RepoType at all -> reflection drops
            await _publish(bus, "causal.delta.jarvis", RepoType.JARVIS,
                           _envelope("not-a-trinity-repo", 4))
            # a good one to prove the bus + handler are still alive after drops
            await _publish(bus, "causal.delta.reactor", RepoType.REACTOR,
                           _envelope("reactor", 1, head_sha="ok"))
            await _settle(sub, 1)
            await asyncio.sleep(0.2)
            return sub.observed()
        finally:
            await sub.stop()
            await bus.stop()

    observed = _run(scenario())
    assert observed == [("reactor", 1, "ok")], observed


def test_handler_never_raises_on_garbage_payload():
    """Direct-drive the handler with hostile payloads -- it must swallow every
    error (fail-soft: never raise into the bus delivery loop)."""
    CausalDeltaSubscriber = _import_subscriber()

    async def scenario():
        sub = CausalDeltaSubscriber(trinity_bus=object())
        # payload not a dict
        await sub._on_delta(TrinityEvent(topic="causal.delta.jarvis",
                                         source=RepoType.JARVIS, payload=None))  # type: ignore[arg-type]
        # emit_seq not an int
        bad = _envelope("jarvis", 1)
        bad["lineage"]["emit_seq"] = "not-an-int"
        await sub._on_delta(TrinityEvent(topic="causal.delta.jarvis",
                                         source=RepoType.JARVIS, payload=bad))
        # in-payload lineage.repo not a RepoType at all -> reflection drops it
        # (event.source is a perfectly valid RepoType.JARVIS -- irrelevant now)
        bad_repo = _envelope("jarvis", 1)
        bad_repo["lineage"]["repo"] = 12345  # non-str, non-RepoType
        await sub._on_delta(TrinityEvent(topic="causal.delta.jarvis",
                                         source=RepoType.JARVIS, payload=bad_repo))
        return sub.observed_count()

    assert _run(scenario()) == 0


def test_on_delta_callback_fires_and_is_failsoft():
    CausalDeltaSubscriber = _import_subscriber()
    seen: List[dict] = []

    def _cb(payload: dict) -> None:
        seen.append(payload)
        raise RuntimeError("callback boom -- must not propagate")

    async def scenario():
        sub = CausalDeltaSubscriber(trinity_bus=object(), on_delta=_cb)
        await sub._on_delta(TrinityEvent(
            topic="causal.delta.jarvis", source=RepoType.JARVIS,
            payload=_envelope("jarvis", 1, head_sha="cb")))
        return sub.observed()

    observed = _run(scenario())
    assert observed == [("jarvis", 1, "cb")], observed
    assert len(seen) == 1  # callback fired despite raising


# ---------------------------------------------------------------------------
# (e) organism_bus_host wiring: constructed when armed, byte-identical off
# ---------------------------------------------------------------------------
def test_organism_bus_host_constructs_subscriber_when_armed(monkeypatch):
    import backend.core.ouroboros.governance.causal.causal_delta_subscriber as mod
    from backend.core.ouroboros.governance.transport.organism_bus_host import (
        OrganismBusHost,
    )

    recorded: dict = {}

    class _RecordingSubscriber:
        def __init__(self, trinity_bus: Any,
                     *, on_delta: Optional[Any] = None) -> None:
            recorded["bus"] = trinity_bus
            recorded["started"] = False

        async def start(self) -> None:
            recorded["started"] = True

        async def stop(self) -> None:
            recorded["stopped"] = True

    monkeypatch.setattr(mod, "CausalDeltaSubscriber", _RecordingSubscriber)

    host = OrganismBusHost()
    sentinel_bus = object()
    _run(host._start_causal_subscriber(sentinel_bus))

    assert recorded.get("bus") is sentinel_bus
    assert recorded.get("started") is True
    assert isinstance(host._causal_subscriber, _RecordingSubscriber)

    _run(host.stop())
    assert recorded.get("stopped") is True
    assert host._causal_subscriber is None


def test_organism_bus_host_byte_identical_when_master_off(monkeypatch):
    from backend.core.ouroboros.governance.transport import organism_bus_host as obh
    from backend.core.ouroboros.governance.transport.organism_bus_host import (
        OrganismBusHost,
    )

    for key in ("JARVIS_DISTRIBUTED_BUS_ENABLED", "JARVIS_BRAIN_WS_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(obh, "bus_host_enabled", lambda: False)

    host = OrganismBusHost()
    assert _run(host.start()) is False
    assert host._causal_subscriber is None
    _run(host.stop())  # no-op teardown, never raises
    assert host._causal_subscriber is None

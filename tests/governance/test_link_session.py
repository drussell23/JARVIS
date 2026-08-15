"""Regression spine for the Body/Engine session loop.

Two loops are driven against each other in-process, so a full handshake →
resume → partition → reconnect cycle runs deterministically with no sockets
and no sleeps. Every test names the distributed failure it prevents.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import link_protocol as proto
from backend.core.ouroboros.governance import link_session as ls
from backend.core.ouroboros.governance import link_transport as tx


def _loop(node, session="s-1", **kw):
    applied = []
    loop = ls.LinkSessionLoop(
        ls.SessionConfig(node_id=node, session_id=session, **kw),
        on_frame=applied.append,
    )
    loop.applied = applied  # type: ignore[attr-defined]
    return loop


# ---------------------------------------------------------------------------
# Handshake collision — two dials, one winner, decided identically
# ---------------------------------------------------------------------------


def test_both_ends_compute_opposite_answers():
    """THE split-brain requirement. A rule that could return 'win' to both
    recreates exactly what it was meant to prevent."""
    a, b = (7, "body"), (7, "engine")
    assert ls.resolve_collision(a, b) != ls.resolve_collision(b, a)


def test_lamport_precedence_beats_node_id():
    """A node that has observed more of the shared history is further along;
    its view should survive."""
    assert ls.resolve_collision((9, "aaa"), (3, "zzz")) is True


def test_node_id_breaks_a_genuine_tie():
    assert ls.resolve_collision((5, "zzz"), (5, "aaa")) is True
    assert ls.resolve_collision((5, "aaa"), (5, "zzz")) is False


def test_a_peer_advertising_our_own_identity_is_refused():
    """There is no correct winner between a node and itself — a duplicated
    identity or a loopback misconfiguration."""
    with pytest.raises(ls.HandshakeError):
        ls.resolve_collision((5, "same"), (5, "same"))


def test_the_losing_side_abandons_its_own_handshake_not_the_socket():
    """Severing here would drop the connection that just won."""
    body = _loop("body")
    body.build_hello()                       # ours is in flight
    with pytest.raises(ls.CollisionLost):
        body.on_hello({"node_id": "engine", "lamport": 9_999,
                       "session_id": "s-1"})
    assert body.state is not ls.SessionState.CLOSED


def test_the_winning_side_proceeds_to_resume():
    engine = _loop("engine")
    engine.build_hello()
    welcome = engine.on_hello({"node_id": "aaa", "lamport": 1,
                               "session_id": "s-1"})
    assert welcome["kind"] == tx.KIND_WELCOME
    assert engine.state is ls.SessionState.RESUMING


def test_simultaneous_dials_converge_on_one_session():
    """Both ends dial; exactly one handshake survives and both agree which."""
    body, engine = _loop("body"), _loop("engine")
    h_body, h_engine = body.build_hello(), engine.build_hello()

    body_wins = ls.resolve_collision(
        (h_body["lamport"], "body"), (h_engine["lamport"], "engine"))
    loser, winner_hello = ((engine, h_body) if body_wins
                           else (body, h_engine))
    with pytest.raises(ls.CollisionLost):
        loser.on_hello(winner_hello)


# ---------------------------------------------------------------------------
# Handshake + negotiation
# ---------------------------------------------------------------------------


def test_the_handshake_negotiates_the_smaller_ceiling(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_MAX_FRAME_BYTES", "65536")
    engine = _loop("engine")
    engine.on_hello({"node_id": "body", "lamport": 1, "session_id": "s-1",
                     "max_frame_bytes": 8192})
    assert engine._limits is not None
    assert engine._limits.max_frame_bytes == 8192


def test_hello_carries_the_watermark_the_peer_needs():
    body = _loop("body")
    hello = body.build_hello()
    assert "last_applied" in hello and hello["seq"] >= 1


def test_sequences_are_one_indexed_because_zero_is_reserved():
    body = _loop("body")
    assert body.next_seq() == 1


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_a_reconnect_resumes_the_same_session_not_a_new_one():
    body = _loop("body")
    body.on_welcome({"node_id": "engine", "lamport": 5, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    first = body._session
    body.park("wifi drop")
    body.on_welcome({"node_id": "engine", "lamport": 9, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    assert body._session is first, "reconnection allocated new state"
    assert body.state is ls.SessionState.ACTIVE


def test_a_gap_past_retention_triggers_resync_not_a_partial_replay():
    body = _loop("body")
    body.on_welcome({"node_id": "engine", "lamport": 1, "session_id": "s-1",
                     "source_latest": 900, "oldest_retained": 400})
    assert body._resyncs == 1
    assert body.reassembly.next_expected == 901


def test_an_incoherent_peer_is_refused_at_the_handshake():
    body = _loop("body")
    body._session, _ = body.registry.attach("s-1", "body")
    for s in range(1, 6):
        body._session.ledger.apply_once(
            proto.LinkFrame(seq=s, lamport=s, node_id="e", kind="verdict"),
            lambda f: None)
    with pytest.raises(ls.HandshakeError):
        body.on_welcome({"node_id": "engine", "lamport": 1,
                         "session_id": "s-1", "source_latest": 2,
                         "oldest_retained": 1})


# ---------------------------------------------------------------------------
# Park — a partition is a pause, never a rejection
# ---------------------------------------------------------------------------


def test_a_partition_parks_and_keeps_everything(tmp_path):
    """§26.6 crossing the network: nothing infers a verdict from silence."""
    body = _loop("body", spill_path=tmp_path / "o.log")
    body.on_welcome({"node_id": "engine", "lamport": 1, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    body.send_verdict({"op": "op-1"})
    queued = body.outbox.stats().queued
    body.park("wifi dropped")
    assert body.state is ls.SessionState.PARKED
    assert body.outbox.stats().queued == queued, "parking discarded work"
    assert body._session is not None, "parking destroyed the session"


def test_parking_detaches_but_does_not_expire_the_session():
    body = _loop("body")
    body.on_welcome({"node_id": "engine", "lamport": 1, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    body.park("drop")
    assert body.registry.snapshot()["sessions"] == 1


def test_reconnect_backoff_grows_then_resets_on_success():
    body = _loop("body")
    delays = [body.reconnect_delay_s() for _ in range(5)]
    assert delays == sorted(delays)
    body.on_established()
    assert body.reconnect_delay_s() <= delays[0] * 2


# ---------------------------------------------------------------------------
# Re-keying — no special path, by construction
# ---------------------------------------------------------------------------


def test_a_rekey_is_a_reconnect_and_costs_the_queue_nothing(tmp_path):
    """Certificate rotation must not flush unacknowledged work. It does not,
    and not by special-casing: session state lives in the registry, never in
    the connection."""
    body = _loop("body", spill_path=tmp_path / "o.log")
    body.on_welcome({"node_id": "engine", "lamport": 1, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    for i in range(5):
        body.send_verdict({"op": f"op-{i}"})
    before = body.outbox.stats().queued
    watermark = body._last_applied()

    body.park("certificate rotation")          # the re-key
    body.on_welcome({"node_id": "engine", "lamport": 7, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})

    assert body.outbox.stats().queued == before
    assert body._last_applied() == watermark
    assert body.state is ls.SessionState.ACTIVE


def test_the_state_machine_cannot_distinguish_a_rekey_from_a_wifi_drop():
    """A dedicated hot-swap path would be a second way to do what resume
    already does correctly."""
    import inspect
    src = inspect.getsource(ls.LinkSessionLoop)
    assert "rekey" not in src.lower().replace("re-key", "")


# ---------------------------------------------------------------------------
# Priority — verdicts outrank telemetry, nothing blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verdict_overtakes_queued_telemetry():
    q = ls.PriorityWriteQueue()
    for i in range(5):
        q.put_low({"kind": tx.KIND_TELEMETRY, "seq": i + 1})
    q.put_high({"kind": tx.KIND_VERDICT, "seq": 99})
    first = await q.get()
    assert first.record["kind"] == tx.KIND_VERDICT


@pytest.mark.asyncio
async def test_telemetry_drops_oldest_rather_than_blocking(monkeypatch):
    """Blocking a producer to preserve stale telemetry converts a display
    problem into a stall."""
    monkeypatch.setenv("JARVIS_LINK_TELEMETRY_DEPTH", "8")
    q = ls.PriorityWriteQueue()
    for i in range(50):
        q.put_low({"kind": tx.KIND_TELEMETRY, "seq": i + 1})
    assert q.snapshot()["low_queued"] <= 8
    assert q.snapshot()["telemetry_dropped"] > 0
    nxt = await q.get()
    assert nxt.record["seq"] > 1, "the oldest should have been displaced"


@pytest.mark.asyncio
async def test_verdicts_are_never_dropped_under_any_depth():
    q = ls.PriorityWriteQueue()
    for i in range(1000):
        q.put_high({"kind": tx.KIND_VERDICT, "seq": i + 1})
    assert q.snapshot()["high_queued"] == 1000


@pytest.mark.asyncio
async def test_a_verdict_flood_still_lets_telemetry_through(monkeypatch):
    """Strict priority starves. An operator watching a blank deck cannot
    tell a busy Engine from a dead one."""
    monkeypatch.setenv("JARVIS_LINK_STARVATION_QUANTUM", "4")
    q = ls.PriorityWriteQueue()
    for i in range(100):
        q.put_high({"kind": tx.KIND_VERDICT, "seq": i + 1})
    q.put_low({"kind": tx.KIND_TELEMETRY, "seq": 1})
    kinds = [(await q.get()).record["kind"] for _ in range(10)]
    assert tx.KIND_TELEMETRY in kinds, "telemetry was starved"


@pytest.mark.asyncio
async def test_the_queue_blocks_only_when_genuinely_empty():
    q = ls.PriorityWriteQueue()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.05)
    q.put_high({"kind": tx.KIND_VERDICT, "seq": 1})
    assert (await asyncio.wait_for(q.get(), timeout=1.0)) is not None


def test_no_lock_is_held_across_an_await():
    """The single discipline that makes deadlock-under-backpressure
    unreachable rather than merely unlikely."""
    import inspect
    import re
    src = inspect.getsource(ls)
    for block in re.findall(r"with self\._lock:(.*?)(?=\n    [a-zA-Z@]|\Z)",
                            src, re.S):
        assert "await" not in block, "a lock is held across an await"


# ---------------------------------------------------------------------------
# Inbound dispatch
# ---------------------------------------------------------------------------


def test_frames_apply_in_contiguous_order_only():
    body = _loop("body")
    body.on_welcome({"node_id": "engine", "lamport": 1, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    assert body.dispatch({"kind": tx.KIND_VERDICT, "seq": 3,
                          "node_id": "e", "lamport": 3}) == []
    ready = body.dispatch({"kind": tx.KIND_VERDICT, "seq": 1,
                           "node_id": "e", "lamport": 1})
    assert [r["seq"] for r in ready] == [1]


def test_a_redelivered_frame_applies_exactly_once():
    body = _loop("body")
    body.on_welcome({"node_id": "engine", "lamport": 1, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    frame = {"kind": tx.KIND_VERDICT, "seq": 1, "node_id": "e", "lamport": 1,
             "command_id": "c-1"}
    body.dispatch(frame)
    body.reassembly.reset(next_expected=1)
    body.dispatch(frame)
    assert len(body.applied) == 1


def test_an_unknown_kind_is_dropped_not_dispatched():
    body = _loop("body")
    body.on_welcome({"node_id": "engine", "lamport": 1, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    assert body.dispatch({"kind": "exec_arbitrary", "seq": 1,
                          "node_id": "e"}) == []
    assert body.applied == []


def test_inbound_traffic_advances_the_logical_clock():
    body = _loop("body")
    body.on_welcome({"node_id": "engine", "lamport": 1, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})
    before = body._session.clock.peek()
    body.dispatch({"kind": tx.KIND_TELEMETRY, "seq": 1, "node_id": "e",
                   "lamport": 5_000})
    assert body._session.clock.peek() > before


# ---------------------------------------------------------------------------
# End to end — the chaos scenario, in process
# ---------------------------------------------------------------------------


def test_the_pull_the_plug_cycle_loses_nothing(tmp_path):
    """Engine completes work while the Body is gone; the Body reconnects and
    receives every verdict exactly once, in order."""
    engine = _loop("engine", spill_path=tmp_path / "engine.log")
    body = _loop("body")

    engine.on_hello({"node_id": "body", "lamport": 1, "session_id": "s-1"})
    body.on_welcome({"node_id": "engine", "lamport": 2, "session_id": "s-1",
                     "last_applied": 0, "oldest_retained": 1})

    engine.send_verdict({"op": "op-1"})          # delivered before the drop
    delivered = engine.outbox.drain(1)
    engine.outbox.ack(len(delivered))
    from backend.core.ouroboros.battle_test.transcript_log import decode_frame
    rec, _ = decode_frame(delivered[0].rstrip(b"\n"))
    body.dispatch(rec)

    body.park("the barista turned on the blender")
    for i in range(2, 6):                         # work continues regardless
        engine.send_verdict({"op": f"op-{i}"})

    assert engine.outbox.stats().queued == 4, "work was lost while parked"

    # The Engine states its TRUE position on reconnect. Understating it is
    # what `plan_resume` calls an incoherent peer, and it refuses to serve
    # one rather than papering over the disagreement.
    body.on_welcome({"node_id": "engine", "lamport": 9, "session_id": "s-1",
                     "source_latest": 5, "oldest_retained": 1})
    for raw in engine.outbox.drain(10):
        rec, _ = decode_frame(raw.rstrip(b"\n"))
        body.dispatch(rec)

    assert [f["seq"] for f in body.applied] == [1, 2, 3, 4, 5]


def test_verdicts_survive_an_engine_restart_mid_outage(tmp_path):
    from backend.core.ouroboros.governance.link_outbox import LinkOutbox
    path = tmp_path / "engine.log"
    engine = _loop("engine", spill_path=path)
    import os
    os.environ["JARVIS_LINK_OUTBOX_MEM_RECORDS"] = "8"
    try:
        for i in range(40):
            engine.send_verdict({"op": f"op-{i}"})
        spilled = engine.outbox.stats().on_disk
        assert spilled > 0
        assert LinkOutbox(spill_path=path).recover() == spilled
    finally:
        os.environ.pop("JARVIS_LINK_OUTBOX_MEM_RECORDS", None)


# ---------------------------------------------------------------------------
# SSE bridge + observability
# ---------------------------------------------------------------------------


def test_the_sse_bridge_never_raises_into_its_producer():
    body = _loop("body")
    bridge = ls.SseTelemetryBridge(body)
    bridge.on_event(object())                    # not serialisable
    assert bridge.snapshot()["suppressed"] == 1


def test_the_sse_bridge_forwards_onto_the_lossy_lane():
    body = _loop("body")
    bridge = ls.SseTelemetryBridge(body)
    bridge.on_event({"event_type": "phase", "op_id": "op-1"})
    assert bridge.snapshot()["forwarded"] == 1
    assert body.queue.snapshot()["low_queued"] == 1


def test_the_snapshot_is_serialisable_and_names_every_subsystem(tmp_path):
    import json
    body = _loop("body", spill_path=tmp_path / "o.log")
    snap = body.snapshot()
    json.dumps(snap)
    for key in ("state", "queue", "outbox", "liveness", "reassembly",
                "registry", "breaker"):
        assert key in snap

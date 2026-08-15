"""Regression spine for the Body/Engine transport and outbox.

Every network failure is simulated deterministically — no sleeps, no real
sockets except one loopback pair that proves the codec survives a stream.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from backend.core.ouroboros.governance import link_outbox as lo
from backend.core.ouroboros.governance import link_transport as lt


# ---------------------------------------------------------------------------
# Handshake — limits are agreed, never assumed
# ---------------------------------------------------------------------------


def test_the_smaller_frame_ceiling_wins():
    """A ceiling one side cannot honour is not a limit, it is a fault
    waiting for a large frame."""
    n = lt.negotiate({"max_frame_bytes": 1 << 20},
                     {"max_frame_bytes": 65536, "node_id": "engine"})
    assert n.max_frame_bytes == 65536


def test_the_faster_heartbeat_wins():
    """Liveness is the one axis where the more demanding party should win:
    a peer that wants proof of life every second is not harmed by it."""
    n = lt.negotiate({"heartbeat_s": 5.0}, {"heartbeat_s": 2.0})
    assert n.heartbeat_s == 2.0


def test_a_garbage_advertisement_falls_back_to_local_defaults():
    n = lt.negotiate({"max_frame_bytes": 65536},
                     {"max_frame_bytes": "banana", "heartbeat_s": -3})
    assert n.max_frame_bytes == 65536
    assert n.heartbeat_s > 0


def test_an_absent_advertisement_does_not_crash_the_handshake():
    n = lt.negotiate({}, {})
    assert n.max_frame_bytes > 0 and n.heartbeat_s > 0


# ---------------------------------------------------------------------------
# Dead-peer detection — the half-open socket
# ---------------------------------------------------------------------------


def test_the_deadline_tracks_measured_rtt_not_a_constant(monkeypatch):
    """A constant is wrong in both directions: too tight on a café link
    (false deaths) and too loose on a fast one (minutes of silence).

    The floor is lowered here so the ADAPTATION is observable; in
    production the floor is what keeps a LAN-fast link from heartbeating
    itself to death."""
    monkeypatch.setenv("JARVIS_LINK_HEARTBEAT_S", "0.5")
    est = lt.RttEstimator()
    for _ in range(10):
        est.observe(0.005)
    fast = est.deadline_s()

    slow_est = lt.RttEstimator()
    for _ in range(10):
        slow_est.observe(0.9)
    slow = slow_est.deadline_s()

    assert slow > fast


def test_the_deadline_never_drops_below_the_floor():
    est = lt.RttEstimator()
    for _ in range(20):
        est.observe(0.0001)
    assert est.deadline_s() >= lt.heartbeat_interval_s()


def test_a_jittery_link_widens_the_deadline_via_variance(monkeypatch):
    """4*rttvar is what keeps a normally-jittery path from being declared
    dead — the classic RTO margin."""
    monkeypatch.setenv("JARVIS_LINK_HEARTBEAT_S", "0.01")
    steady = lt.RttEstimator()
    jittery = lt.RttEstimator()
    for i in range(16):
        steady.observe(0.1)
        jittery.observe(0.02 if i % 2 else 0.30)
    assert jittery.deadline_s() > steady.deadline_s()


def test_one_missed_heartbeat_is_not_a_death(monkeypatch):
    """Wi-Fi loses single packets constantly; killing the session on one
    would produce the reconnect storm FlapBreaker exists to survive."""
    monkeypatch.setenv("JARVIS_LINK_HEARTBEAT_MISSES", "3")
    mon = lt.LivenessMonitor()
    assert mon.note_miss() is False
    assert mon.note_miss() is False
    assert mon.note_miss() is True


def test_any_inbound_traffic_proves_liveness(monkeypatch):
    """Demanding a specific frame type as proof would declare a busy peer
    dead while it is actively sending."""
    monkeypatch.setenv("JARVIS_LINK_HEARTBEAT_MISSES", "2")
    mon = lt.LivenessMonitor()
    mon.note_miss()
    mon.note_inbound()          # telemetry arrived — it is alive
    assert mon.note_miss() is False


def test_an_ack_feeds_the_estimator_and_clears_the_miss_count():
    mon = lt.LivenessMonitor()
    mon.note_miss()
    mon.note_ack(sent_mono=__import__("time").monotonic() - 0.05)
    assert mon.snapshot()["consecutive_misses"] == 0
    assert mon.rtt.srtt is not None


# ---------------------------------------------------------------------------
# Reassembly — contiguity restored before anything is applied
# ---------------------------------------------------------------------------


def test_frames_arriving_ahead_are_held_until_the_hole_fills():
    buf = lt.ReassemblyBuffer(next_expected=1)
    assert buf.offer(3, "c").ready == []
    assert buf.offer(2, "b").ready == []
    res = buf.offer(1, "a")
    assert res.ready == ["a", "b", "c"], "the run must flush atomically"
    assert buf.next_expected == 4


def test_a_contiguous_frame_flushes_immediately():
    buf = lt.ReassemblyBuffer(next_expected=1)
    assert buf.offer(1, "a").ready == ["a"]


def test_a_duplicate_from_a_replay_is_not_an_error():
    buf = lt.ReassemblyBuffer(next_expected=5)
    res = buf.offer(2, "old")
    assert res.ready == [] and res.gap_unrecoverable is False
    assert res.reason == "duplicate"


def test_an_unclosable_gap_declares_resync_rather_than_buffering_forever(monkeypatch):
    """A gap that cannot close within capacity is a lost range, not a
    buffering problem to solve with more memory."""
    monkeypatch.setenv("JARVIS_LINK_REASSEMBLY_CAPACITY", "8")
    buf = lt.ReassemblyBuffer(next_expected=1)
    last = None
    for seq in range(2, 20):
        last = buf.offer(seq, f"f{seq}")
    assert last is not None and last.gap_unrecoverable is True
    assert "resync" in last.reason


def test_reset_after_a_resync_clears_held_frames():
    buf = lt.ReassemblyBuffer(next_expected=1)
    buf.offer(5, "e")
    buf.reset(next_expected=10)
    assert buf.snapshot()["held"] == 0
    assert buf.offer(10, "j").ready == ["j"]


# ---------------------------------------------------------------------------
# Adaptive batching — the congestion signal that exists above TCP
# ---------------------------------------------------------------------------


def test_slow_drains_shrink_the_batch(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_DRAIN_TARGET_S", "0.05")
    b = lt.AdaptiveBatcher()
    start = b.size
    for _ in range(6):
        b.observe_drain(0.5)
    assert b.size < start


def test_fast_drains_grow_the_batch_but_not_without_bound(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_BATCH_MAX", "32")
    b = lt.AdaptiveBatcher()
    for _ in range(500):
        b.observe_drain(0.0001)
    assert b.size == 32


# ---------------------------------------------------------------------------
# Security posture
# ---------------------------------------------------------------------------


def test_the_default_bind_is_loopback_never_all_interfaces():
    """0.0.0.0 would listen on the home LAN and any café Wi-Fi the host
    joins. One end of this link runs bash."""
    assert lt.bind_host() == "127.0.0.1"


def test_missing_mtls_material_refuses_rather_than_downgrading(monkeypatch, tmp_path):
    """A link that quietly ran unauthenticated would be worse than one that
    did not run."""
    monkeypatch.setenv("JARVIS_LINK_TLS_DIR", str(tmp_path / "absent"))
    assert lt.build_ssl_context(server_side=True) is None
    assert lt.build_ssl_context(server_side=False) is None


def test_an_unknown_frame_kind_is_not_dispatched():
    """A newer peer must not be able to drive an older one down a path it
    does not have."""
    assert lt.is_known_kind(lt.KIND_VERDICT) is True
    assert lt.is_known_kind("exec_arbitrary") is False


def test_no_hardcoded_endpoints_in_transport():
    """Checked against the resolved VALUES, not the prose. The module
    legitimately discusses 0.0.0.0 in the docstring explaining why it is
    not the default, and a substring match cannot tell an explanation
    from a use — the same lesson as the earlier DRY audit."""
    assert lt.bind_host() != "0.0.0.0"
    assert lt.bind_port() == 0, "default port must be OS-assigned"
    import inspect
    src = inspect.getsource(lt.bind_host)
    assert '"0.0.0.0"' not in src.split('"""')[-1], "0.0.0.0 in executable code"


# ---------------------------------------------------------------------------
# Framed stream I/O over a real socket pair
# ---------------------------------------------------------------------------


def _limits(max_bytes=1 << 20):
    return lt.NegotiatedLimits(max_frame_bytes=max_bytes, heartbeat_s=1.0,
                               protocol_version="t.1", peer_node_id="peer")


@pytest.mark.socket
@pytest.mark.asyncio
async def test_frames_round_trip_over_a_real_stream():
    """Proves the transcript codec frames a STREAM, not just a file — the
    reason it could be reused instead of writing a second codec.

    Synchronised on an Event, not a sleep: a test that waits a fixed
    duration for I/O is a flaky test on a loaded CI box."""
    got = []
    done = asyncio.Event()

    async def _serve(reader, writer):
        try:
            for _ in range(3):
                rec = await lt.read_frame(reader, limits=_limits(),
                                          timeout_s=5.0)
                if rec is None:
                    break
                got.append(rec)
        finally:
            done.set()
            writer.close()

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    for i in range(3):
        await lt.write_frame(writer, {"kind": lt.KIND_TELEMETRY, "seq": i + 1},
                             limits=_limits())
    await asyncio.wait_for(done.wait(), timeout=5.0)
    writer.close()
    server.close()
    await server.wait_closed()
    assert [r["seq"] for r in got] == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_frame_over_the_negotiated_ceiling_is_refused_before_the_wire():
    """Refusing at the writer keeps the fault on this side of the socket,
    where it can still be handled."""
    class _W:
        def write(self, b): raise AssertionError("must not reach the socket")
        async def drain(self): pass
    with pytest.raises(lt.FrameTooLarge):
        await lt.write_frame(_W(), {"kind": "x", "seq": 1, "blob": "y" * 5000},
                             limits=_limits(max_bytes=1024))


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_stalled_peer_times_out_rather_than_hanging_forever():
    """THE half-open regression. No FIN arrives; the OS would wait hours."""
    async def _serve(reader, writer):
        await asyncio.sleep(30)          # peer is 'powered off'

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    with pytest.raises(asyncio.TimeoutError):
        await lt.read_frame(reader, limits=_limits(), timeout_s=0.2)
    writer.close()
    server.close()
    await server.wait_closed()


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_corrupt_frame_is_reported_not_parsed():
    """CRC before payload: a torn frame is rejected by arithmetic, not by a
    JSON parser's opinion of it."""
    async def _serve(reader, writer):
        writer.write(b"deadbeef\t{\"kind\":\"telemetry\"}\n")  # bad CRC
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    rec = await lt.read_frame(reader, limits=_limits(), timeout_s=5.0)
    assert rec is not None and rec.get("kind") == "__rejected__"
    writer.close()
    server.close()
    await server.wait_closed()


# ---------------------------------------------------------------------------
# Outbox — an extended outage must not OOM the Engine
# ---------------------------------------------------------------------------


def test_records_spill_to_disk_past_the_memory_bound(monkeypatch, tmp_path):
    """THE extended-outage regression: hours of undelivered verdicts must
    not become an OOM on the machine holding the accelerator."""
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_MEM_RECORDS", "16")
    box = lo.LinkOutbox(spill_path=tmp_path / "outbox.log")
    for i in range(200):
        box.put({"kind": "verdict", "seq": i + 1})
    s = box.stats()
    assert s.in_memory <= 16, "memory was not bounded"
    assert s.on_disk > 0, "nothing spilled"
    assert s.queued == 200, "records were lost, not spilled"


def test_fifo_order_holds_across_the_disk_boundary(monkeypatch, tmp_path):
    """Out-of-order delivery is a verdict the peer's contiguous watermark
    will refuse to advance past."""
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_MEM_RECORDS", "8")
    box = lo.LinkOutbox(spill_path=tmp_path / "outbox.log")
    for i in range(50):
        box.put({"kind": "verdict", "seq": i + 1})
    frames = box.drain(50)
    import json
    seqs = [json.loads(f.split(b"\t", 1)[1])["seq"] for f in frames]
    assert seqs == sorted(seqs) == list(range(1, 51))


def test_drained_frames_survive_the_socket_dying_before_ack(monkeypatch, tmp_path):
    """Drain must not remove: the window between drain and send is exactly
    what this module exists to cover."""
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_MEM_RECORDS", "8")
    box = lo.LinkOutbox(spill_path=tmp_path / "outbox.log")
    for i in range(20):
        box.put({"kind": "verdict", "seq": i + 1})
    first = box.drain(10)
    assert box.stats().queued == 20, "drain removed before acknowledgement"
    again = box.drain(10)
    assert [f for f in again] == [f for f in first]


def test_ack_removes_exactly_what_was_delivered(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_MEM_RECORDS", "8")
    box = lo.LinkOutbox(spill_path=tmp_path / "outbox.log")
    for i in range(20):
        box.put({"kind": "verdict", "seq": i + 1})
    box.ack(len(box.drain(10)))
    assert box.stats().queued == 10


def test_the_disk_ceiling_sheds_loudly_rather_than_growing(monkeypatch, tmp_path):
    """Silence is what must never happen — a dropped verdict is
    indistinguishable from an op that never ran."""
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_MEM_RECORDS", "8")
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_DISK_RECORDS", "64")
    box = lo.LinkOutbox(spill_path=tmp_path / "outbox.log")
    for i in range(500):
        box.put({"kind": "verdict", "seq": i + 1})
    s = box.stats()
    assert s.shed_total > 0, "unbounded growth"
    assert s.on_disk <= 64 + 8


def test_undelivered_verdicts_survive_an_engine_restart(monkeypatch, tmp_path):
    """The Engine restarting is not a reason to lose verdicts the Body has
    not seen — that is why they are on disk."""
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_MEM_RECORDS", "8")
    path = tmp_path / "outbox.log"
    box = lo.LinkOutbox(spill_path=path)
    for i in range(40):
        box.put({"kind": "verdict", "seq": i + 1})
    spilled = box.stats().on_disk
    assert spilled > 0

    reborn = lo.LinkOutbox(spill_path=path)      # new process
    assert reborn.recover() == spilled


def test_an_unencodable_record_fails_at_the_caller_not_at_the_socket(tmp_path):
    box = lo.LinkOutbox(spill_path=tmp_path / "outbox.log")
    assert box.put({"kind": "verdict", "seq": 1, "bad": object()}) is False
    assert box.stats().queued == 0


def test_without_a_spill_path_it_bounds_in_memory_and_says_so(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_MEM_RECORDS", "16")
    box = lo.LinkOutbox(spill_path=None)
    for i in range(100):
        box.put({"kind": "verdict", "seq": i + 1})
    s = box.stats()
    assert s.in_memory <= 16
    assert s.shed_total > 0, "an unbounded queue on the accelerator host"


def test_memory_pressure_lowers_the_high_water_mark(monkeypatch):
    """Holding 512 records is harmless on an idle Engine and reckless on one
    whose RAM is already the constraint.

    Both levels are FORCED. Reading the real host's pressure as a baseline
    made this test pass or fail depending on what else was running — and on
    this machine it was already 'high', so the comparison was 64 < 64."""
    monkeypatch.setenv("JARVIS_LINK_OUTBOX_MEM_RECORDS", "512")
    import backend.core.ouroboros.governance.memory_pressure_gate as mpg

    def _at(level):
        class _Gate:
            def pressure(self):
                class _L:
                    value = level
                return _L()
        monkeypatch.setattr(mpg, "get_default_gate", lambda: _Gate())

    box = lo.LinkOutbox(spill_path=None)
    _at("ok")
    calm = box._effective_high_water()
    _at("warn")
    warned = box._effective_high_water()
    _at("high")
    pressed = box._effective_high_water()

    assert calm == 512
    assert warned < calm
    assert pressed < warned


def test_stats_are_serialisable_for_the_operator(tmp_path):
    import json
    box = lo.LinkOutbox(spill_path=tmp_path / "o.log")
    box.put({"kind": "verdict", "seq": 1})
    json.dumps(box.stats().to_dict())


# ---------------------------------------------------------------------------
# DRY — one codec, not two
# ---------------------------------------------------------------------------


def test_the_wire_and_the_spill_share_one_encoding():
    """A verdict spilled to disk and the same verdict on the socket must be
    byte-identical — no serialise/deserialise seam where schema can drift."""
    import pathlib
    for mod in (lt, lo):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "transcript_log" in src
        assert "json.dumps" not in src, "a second encoder appeared"


def test_seq_zero_is_refused_at_the_caller_not_after_a_restart(tmp_path):
    """seq=0 is reserved for "nothing yet". It would encode cleanly, spill
    cleanly, and be rejected by recover_log after a restart — losing the
    verdicts this module exists to preserve, at the moment they matter."""
    box = lo.LinkOutbox(spill_path=tmp_path / "o.log")
    assert box.put({"kind": "verdict", "seq": 0}) is False
    assert box.put({"kind": "verdict", "seq": -3}) is False
    assert box.put({"kind": "verdict", "seq": "1"}) is False
    assert box.put({"kind": "verdict", "seq": 1}) is True

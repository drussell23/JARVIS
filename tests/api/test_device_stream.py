"""Device-Aware SSE Multiplexer + Dead-Stream Pruning spine.

Mandate 4 verbatim (2026-07-19): two native clients connect with
distinct device_ids; a silent network drop (unhandled write-buffer
exception) on Device A → the pruning logic evicts A from the active
dict while Device B keeps receiving telemetry uninterrupted.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.api import device_stream_manager as dsm
from backend.api.device_stream_manager import DeviceStreamManager


@pytest.fixture(autouse=True)
def _reset():
    dsm.reset_device_manager()
    yield
    dsm.reset_device_manager()


async def _drain(agen, out, *, stop_after=None):
    try:
        i = 0
        async for frame in agen:
            out.append(frame)
            i += 1
            if stop_after is not None and i >= stop_after:
                break
    except Exception as e:  # noqa: BLE001
        out.append(f"<ERR:{type(e).__name__}>")


class TestDeviceMultiplexing:
    async def test_two_devices_routed_independently(self):
        mgr = DeviceStreamManager()

        async def _feed(tag):
            for i in range(3):
                yield f"data: {tag}-{i}\n\n"
                await asyncio.sleep(0)

        a_out, b_out = [], []
        await _drain(mgr.device_stream("devA", _feed("A")), a_out)
        await _drain(mgr.device_stream("devB", _feed("B")), b_out)
        assert any("A-0" in f for f in a_out)
        assert any("B-0" in f for f in b_out)
        assert mgr.stats["connects"] == 2

    async def test_silent_drop_on_A_evicts_A_B_uninterrupted(self):
        """MANDATE 4 VERBATIM: Device A's stream raises a write fault
        (silent native drop); A is evicted, B keeps streaming."""
        mgr = DeviceStreamManager()

        async def _dying_feed():
            yield "data: A-first\n\n"
            # Silent native drop — the write buffer raises with NO FIN:
            raise ConnectionResetError("native socket vanished")

        async def _healthy_feed():
            for i in range(4):
                yield f"data: B-{i}\n\n"
                await asyncio.sleep(0)

        a_out, b_out = [], []
        # Both connect + stream concurrently:
        await asyncio.gather(
            _drain(mgr.device_stream("devA", _dying_feed()), a_out),
            _drain(mgr.device_stream("devB", _healthy_feed()), b_out),
        )
        # Device A evicted by the write fault:
        assert "devA" not in mgr.active_devices
        assert mgr.stats["drops_write_fault"] == 1
        assert any("A-first" in f for f in a_out)     # got the pre-drop frame
        # Device B UNINTERRUPTED — all 4 frames:
        assert sum(1 for f in b_out if f.startswith("data: B-")) == 4
        assert "devB" not in mgr.active_devices        # clean stream_end

    async def test_broken_pipe_also_prunes(self):
        mgr = DeviceStreamManager()

        async def _feed():
            yield "data: x\n\n"
            raise BrokenPipeError("EPIPE")

        out = []
        await _drain(mgr.device_stream("devC", _feed()), out)
        assert "devC" not in mgr.active_devices
        assert mgr.stats["drops_write_fault"] == 1


class TestZombieGuard:
    async def test_heartbeat_timeout_prunes_half_open(self, monkeypatch):
        clock = [1000.0]
        mgr = DeviceStreamManager(clock=lambda: clock[0], heartbeat_timeout_s=30.0)

        async def _stalled_feed():
            # Never yields again after the first — a half-open socket.
            yield "data: hello\n\n"
            await asyncio.sleep(3600)

        out = []
        gen = mgr.device_stream("devZ", _stalled_feed(), heartbeat_interval_s=0.05)
        agen = gen.__aiter__()
        assert (await agen.__anext__()).startswith("data: hello")  # first frame
        # First keepalive window: not yet expired → keepalive emitted.
        ka = await agen.__anext__()
        assert ka == ": keepalive\n\n"
        # Advance the clock past the heartbeat timeout → next window prunes.
        clock[0] += 40.0
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
        assert "devZ" not in mgr.active_devices
        assert mgr.stats["drops_heartbeat"] == 1

    async def test_reconnect_replaces_stale_registration(self):
        mgr = DeviceStreamManager()
        mgr.register("devR")
        mgr.register("devR")                          # reconnect
        assert mgr.stats["reconnects"] == 1
        assert len(mgr.active_devices) == 1           # not duplicated
        mgr.deregister("devR")
        mgr.deregister("devR")                        # idempotent
        assert "devR" not in mgr.active_devices


class TestBackendWiring:
    def test_device_endpoint_added_legacy_intact_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] /
               "backend/api/unified_websocket.py").read_text()
        assert '@router.get("/api/stream/{device_id}")' in src   # native path
        assert '@router.get("/api/stream/sse")' in src           # legacy KEPT
        assert '@router.post("/api/stream/command")' in src      # command KEPT
        body = src[src.index('"/api/stream/{device_id}"'):][:1400]
        assert "get_device_manager" in body                      # multiplexer
        assert "es.sse_stream" in body                           # DRY reuse


# ---------------------------------------------------------------------------
# Circular Event Rehydration Buffer (2026-07-19)
# ---------------------------------------------------------------------------


def _idframe(seq, payload="x"):
    return f"id: {seq}\ndata: {payload}-{seq}\n\n"


class TestRehydrationBuffer:
    def test_replay_after_yields_delta(self):
        from backend.api.device_stream_manager import RehydrationBuffer
        buf = RehydrationBuffer(cap=100)
        for i in range(1, 11):                       # events 1..10
            buf.append(_idframe(i))
        frames, too_old = buf.replay_after(5)
        assert too_old is False
        seqs = [int(f.split("id: ")[1].split("\n")[0]) for f in frames]
        assert seqs == [6, 7, 8, 9, 10]              # strictly > 5
        assert buf.newest_seq() == 10 and buf.oldest_seq() == 1

    def test_cursor_fallen_out_is_too_old(self):
        from backend.api.device_stream_manager import RehydrationBuffer
        buf = RehydrationBuffer(cap=5)               # holds only last 5
        for i in range(1, 21):                       # 1..20; buffer=16..20
            buf.append(_idframe(i))
        # Client cursor at 5 — long gone from the buffer:
        frames, too_old = buf.replay_after(5)
        assert too_old is True and frames == []

    def test_keepalives_not_cached(self):
        from backend.api.device_stream_manager import RehydrationBuffer
        buf = RehydrationBuffer()
        buf.append(": keepalive\n\n")                # no id
        buf.append(_idframe(1))
        assert buf.newest_seq() == 1


class TestMandate4Rehydration:
    async def test_reconnect_at_5_replays_6_9_bridges_live_then_prunes(self):
        """MANDATE 4 VERBATIM: disconnect at Event 5, reconnect with
        Last-Event-ID: 5 → replay 6,7,8,9 instantly, bridge to live,
        then a heartbeat timeout drops cleanly."""
        clock = [1000.0]
        mgr = DeviceStreamManager(clock=lambda: clock[0],
                                  heartbeat_timeout_s=30.0)
        # Pre-seed the circular buffer with events 1..9 (the frames the
        # client missed while offline live in the shared buffer):
        for i in range(1, 10):
            mgr._rehydration.append(_idframe(i))

        # Live inner stream resumes at event 10, then STALLS (half-open
        # native socket after the handoff).
        async def _live():
            yield _idframe(10)
            await asyncio.sleep(3600)

        out = []
        gen = mgr.device_stream("devMobile", _live(),
                                heartbeat_interval_s=0.05, last_event_id=5)
        agen = gen.__aiter__()
        # Catch-up replay: 6,7,8,9 yielded INSTANTLY from the buffer:
        for expected in (6, 7, 8, 9):
            f = await agen.__anext__()
            assert f"id: {expected}" in f
        # Bridges to the LIVE generator — event 10 (no duplicate of 6-9):
        f10 = await agen.__anext__()
        assert "id: 10" in f10
        assert mgr.stats["replayed_frames"] == 4
        # Live stalls → keepalive, then heartbeat timeout prunes cleanly:
        ka = await agen.__anext__()
        assert ka == ": keepalive\n\n"
        clock[0] += 40.0
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
        assert "devMobile" not in mgr.active_devices
        assert mgr.stats["drops_heartbeat"] == 1

    async def test_stale_cursor_emits_state_reset(self):
        mgr = DeviceStreamManager()
        # Buffer only holds recent events; client cursor is ancient.
        for i in range(100, 120):
            mgr._rehydration.append(_idframe(i))

        async def _live():
            yield _idframe(120)

        out = []
        async for f in mgr.device_stream("devStale", _live(),
                                         last_event_id=5):
            out.append(f)
        assert any("STATE_RESET" in f for f in out)   # full-refresh instruction
        assert mgr.stats["state_resets"] == 1
        assert any("id: 120" in f for f in out)       # then live resumes

    async def test_no_duplicate_when_live_overlaps_replay(self):
        mgr = DeviceStreamManager()
        for i in range(1, 8):                          # buffer has 1..7
            mgr._rehydration.append(_idframe(i))

        # Live re-emits 6,7 (overlap) then 8 — the replay already sent 6,7.
        async def _live():
            for i in (6, 7, 8):
                yield _idframe(i)

        seen = []
        async for f in mgr.device_stream("devDup", _live(), last_event_id=5):
            s = f.split("id: ")[1].split("\n")[0] if "id: " in f else None
            if s:
                seen.append(int(s))
        # 6,7 come ONCE (from replay); the live re-emits are skipped; 8 new:
        assert seen.count(6) == 1 and seen.count(7) == 1
        assert 8 in seen

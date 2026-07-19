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

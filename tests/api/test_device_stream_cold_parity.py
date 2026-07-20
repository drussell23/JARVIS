"""Slice I — HTTP SSE Replay Parity (cold-boot reconciliation + cursor).

#69970 already gave the device SSE stream Last-Event-ID reconnect delta +
zombie eviction. Slice I closes the PARITY gap with the UDS bridge: a cold-boot
(or cursor-expired) HTTP client is reconciled from the TrinityEventBus
get_replay_snapshot() history — the critical DAG-hydration lifecycle that fired
before any client attached.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.api.device_stream_manager import (
    DeviceStreamManager, RehydrationBuffer,
)


def _frame(seq: int, body: str = "x") -> str:
    return f"id: {seq}\ndata: {body}\n\n"


# ---------------------------------------------------------------------------
# MANDATE 4 — Last-Event-ID at the 50th of 100 → yields ONLY 51..100
# ---------------------------------------------------------------------------

def test_cursor_at_50_of_100_yields_51_through_100():
    buf = RehydrationBuffer(cap=100)
    for i in range(1, 101):
        buf.append(_frame(i))
    frames, too_old = buf.replay_after(50)
    assert not too_old
    seqs = [int(f.split("id: ")[1].split("\n")[0]) for f in frames]
    assert seqs == list(range(51, 101))          # exactly 51..100, in order
    assert len(seqs) == 50


# ---------------------------------------------------------------------------
# Slice I — cold boot (no Last-Event-ID) reconciles from the bus history
# ---------------------------------------------------------------------------

async def _collect(agen, limit):
    out = []
    async for f in agen:
        out.append(f)
        if len(out) >= limit:
            break
    return out


@pytest.mark.asyncio
async def test_cold_boot_flushes_bus_lifecycle_before_live():
    mgr = DeviceStreamManager()
    # The bus lifecycle history (fired before this client connected).
    cold = [
        "event:daemon\ndata:{\"lifecycle\":\"SYSTEM_HYDRATING\"}\n\n",
        "event:daemon\ndata:{\"lifecycle\":\"SYSTEM_READY\"}\n\n",
    ]

    async def _live():
        yield _frame(1, "live-1")
        # keep the stream open briefly so the generator doesn't end first
        await asyncio.sleep(0.05)

    out = await _collect(
        mgr.device_stream("devCold", _live(), heartbeat_interval_s=0.2,
                          last_event_id=None,           # COLD BOOT
                          cold_start_frames=lambda: cold),
        limit=3)

    # The two historical lifecycle frames were flushed BEFORE the live frame.
    assert "SYSTEM_HYDRATING" in out[0]
    assert "SYSTEM_READY" in out[1]
    assert "live-1" in out[2]
    mgr.deregister("devCold")


@pytest.mark.asyncio
async def test_cold_frames_carry_no_id_so_cursor_is_untouched():
    """Reconciliation frames must NOT advance the Last-Event-ID cursor —
    they carry no id: line (idempotent in the HUD state machine)."""
    mgr = DeviceStreamManager()
    cold = ["event:daemon\ndata:{\"lifecycle\":\"SYSTEM_READY\"}\n\n"]

    async def _live():
        yield _frame(7, "live")
        await asyncio.sleep(0.05)

    out = await _collect(
        mgr.device_stream("devNoId", _live(), heartbeat_interval_s=0.2,
                          cold_start_frames=lambda: cold),
        limit=2)
    assert "id:" not in out[0] and "id: " not in out[0]   # no cursor movement
    mgr.deregister("devNoId")


@pytest.mark.asyncio
async def test_reconnect_with_cursor_does_not_cold_flush():
    """A normal reconnect (valid Last-Event-ID) uses the frame delta only —
    NOT the bus cold flush (that would duplicate)."""
    mgr = DeviceStreamManager()
    for i in range(1, 6):
        mgr._rehydration.append(_frame(i))
    cold_called = {"n": 0}

    def _cold():
        cold_called["n"] += 1
        return ["event:daemon\ndata:{\"lifecycle\":\"SYSTEM_READY\"}\n\n"]

    async def _live():
        yield _frame(6, "live")
        await asyncio.sleep(0.05)

    out = await _collect(
        mgr.device_stream("devRe", _live(), heartbeat_interval_s=0.2,
                          last_event_id=3, cold_start_frames=_cold),
        limit=3)
    # Replayed 4,5 (delta) then live 6 — no cold flush.
    assert cold_called["n"] == 0
    assert any("id: 4" in f for f in out) and any("id: 5" in f for f in out)
    mgr.deregister("devRe")


@pytest.mark.asyncio
async def test_expired_cursor_triggers_full_cold_reconcile():
    """A cursor that fell out of the ring → STATE_RESET THEN the bus cold
    flush (full reconciliation), then live."""
    mgr = DeviceStreamManager()
    for i in range(100, 120):            # ring holds 100..119
        mgr._rehydration.append(_frame(i))
    cold = ["event:daemon\ndata:{\"lifecycle\":\"SYSTEM_READY\"}\n\n"]

    async def _live():
        yield _frame(120, "live")
        await asyncio.sleep(0.05)

    out = await _collect(
        mgr.device_stream("devExp", _live(), heartbeat_interval_s=0.2,
                          last_event_id=5,             # long expired
                          cold_start_frames=lambda: cold),
        limit=3)
    assert "STATE_RESET" in out[0]
    assert "SYSTEM_READY" in out[1]        # full bus reconciliation follows
    mgr.deregister("devExp")


# ---------------------------------------------------------------------------
# MANDATE 2 — zombie eviction on abrupt CancelledError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_abrupt_cancel_evicts_subscriber():
    mgr = DeviceStreamManager()

    async def _live():
        yield _frame(1, "a")
        await asyncio.sleep(5)           # hang so we can cancel mid-stream

    async def _drain():
        async for _ in mgr.device_stream("devZombie", _live(),
                                         heartbeat_interval_s=5):
            pass

    task = asyncio.get_event_loop().create_task(_drain())
    await asyncio.sleep(0.05)
    assert "devZombie" in mgr.active_devices     # registered + live
    task.cancel()                                   # abrupt client drop
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Instantly evicted — no leak in the routing table.
    assert "devZombie" not in mgr.active_devices

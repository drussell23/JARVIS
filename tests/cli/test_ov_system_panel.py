"""``ov system`` — System Observability Panel connection resilience (Slice G)."""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.cli import ov_system_panel as sp


# ---------------------------------------------------------------------------
# fake UDS stream — a reader that yields frames then faults mid-stream
# ---------------------------------------------------------------------------

class _FakeReader:
    """Yields queued newline-JSON frames, then applies a terminal behaviour:
    ``eof`` (empty ModuleNotFound-style close), ``reset`` (ConnectionReset),
    ``exc`` (raise EOFError mid-payload), or ``block`` (stay attached)."""
    def __init__(self, frames, then="exc"):
        self._frames = list(frames)
        self._then = then

    async def readline(self):
        if self._frames:
            return self._frames.pop(0)
        if self._then == "exc":
            raise EOFError("stream faulted mid-payload")
        if self._then == "reset":
            raise ConnectionResetError("peer reset")
        if self._then == "eof":
            return b""                       # clean EOF
        if self._then == "block":
            await asyncio.Event().wait()     # stay attached indefinitely
        return b""


class _FakeWriter:
    def __init__(self): self.closed = False
    def close(self): self.closed = True
    def is_closing(self): return self.closed


def _frame(**kw) -> bytes:
    return (json.dumps(kw) + "\n").encode()


# ---------------------------------------------------------------------------
# MANDATE 4 — EOF mid-payload is caught, TUI survives, backoff task queued
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eof_midpayload_caught_tui_survives_backoff_queued():
    collected = []
    reconnecting = asyncio.Event()
    slept = []

    async def connector():
        # Deliver one good frame, THEN raise EOFError mid-stream.
        return _FakeReader([_frame(type="telemetry", system_state="ready")],
                           then="exc"), _FakeWriter()

    async def sleeper(d):
        slept.append(d)
        reconnecting.set()
        await asyncio.Event().wait()          # hold in RECONNECTING for assertion

    mgr = sp.TelemetryConnectionManager(
        connector=connector, on_frame=collected.append,
        sleeper=sleeper, base_backoff_s=0.01)
    task = asyncio.get_event_loop().create_task(mgr.run())
    await asyncio.wait_for(reconnecting.wait(), timeout=2.0)

    # 1. The frame that arrived BEFORE the fault was delivered to the TUI.
    assert any(f.get("system_state") == "ready" for f in collected)
    # 2. The EOFError was caught — the run loop SURVIVED (no teardown, no raise).
    assert not task.done()
    # 3. A reconnection backoff task was QUEUED, and we're backing off.
    assert mgr.state is sp.ConnState.RECONNECTING
    assert isinstance(mgr.reconnect_task, asyncio.Task)
    assert not mgr.reconnect_task.done()
    assert slept and slept[0] == pytest.approx(0.01)

    mgr.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_connection_reset_is_also_a_graceful_detach():
    reconnecting = asyncio.Event()

    async def connector():
        return _FakeReader([], then="reset"), _FakeWriter()

    async def sleeper(d):
        reconnecting.set()
        await asyncio.Event().wait()

    mgr = sp.TelemetryConnectionManager(
        connector=connector, sleeper=sleeper, base_backoff_s=0.01)
    task = asyncio.get_event_loop().create_task(mgr.run())
    await asyncio.wait_for(reconnecting.wait(), timeout=2.0)
    assert not task.done()                    # ConnectionResetError did NOT crash it
    assert "reset" in mgr.last_error.lower()

    mgr.stop(); task.cancel()
    try: await task
    except (asyncio.CancelledError, Exception): pass


@pytest.mark.asyncio
async def test_connect_refused_backs_off_without_crashing():
    reconnecting = asyncio.Event()

    async def connector():
        raise ConnectionRefusedError("no socket — daemon down")

    async def sleeper(d):
        reconnecting.set()
        await asyncio.Event().wait()

    mgr = sp.TelemetryConnectionManager(
        connector=connector, sleeper=sleeper, base_backoff_s=0.01)
    task = asyncio.get_event_loop().create_task(mgr.run())
    await asyncio.wait_for(reconnecting.wait(), timeout=2.0)
    assert not task.done()
    assert mgr.state is sp.ConnState.RECONNECTING
    assert mgr.attempt >= 1

    mgr.stop(); task.cancel()
    try: await task
    except (asyncio.CancelledError, Exception): pass


@pytest.mark.asyncio
async def test_reattaches_after_transient_detach():
    attaches = {"n": 0}
    attached_twice = asyncio.Event()

    async def connector():
        attaches["n"] += 1
        if attaches["n"] == 1:
            return _FakeReader([], then="exc"), _FakeWriter()   # immediate fault
        # Second attach: deliver a frame, then stay attached.
        return _FakeReader([_frame(type="telemetry", system_state="ready")],
                           then="block"), _FakeWriter()

    async def sleeper(d):
        return None                            # zero-wait backoff

    got = []
    def on_frame(f):
        got.append(f)
        if len(got) >= 1:
            attached_twice.set()

    mgr = sp.TelemetryConnectionManager(
        connector=connector, on_frame=on_frame, sleeper=sleeper, base_backoff_s=0.01)
    task = asyncio.get_event_loop().create_task(mgr.run())
    await asyncio.wait_for(attached_twice.wait(), timeout=2.0)
    await asyncio.sleep(0.02)

    assert mgr.state is sp.ConnState.ATTACHED   # recovered after the detach
    assert mgr.reconnects >= 2                   # first attach + reattach
    assert any(f.get("system_state") == "ready" for f in got)

    mgr.stop(); task.cancel()
    try: await task
    except (asyncio.CancelledError, Exception): pass


@pytest.mark.asyncio
async def test_malformed_frame_is_skipped_not_fatal():
    delivered = []
    reconnecting = asyncio.Event()

    async def connector():
        return _FakeReader([b"{not json\n",
                            _frame(type="telemetry", system_state="degraded")],
                           then="exc"), _FakeWriter()

    async def sleeper(d):
        reconnecting.set(); await asyncio.Event().wait()

    mgr = sp.TelemetryConnectionManager(
        connector=connector, on_frame=delivered.append, sleeper=sleeper,
        base_backoff_s=0.01)
    task = asyncio.get_event_loop().create_task(mgr.run())
    await asyncio.wait_for(reconnecting.wait(), timeout=2.0)
    # The malformed line was skipped; the valid frame still arrived.
    assert delivered == [{"type": "telemetry", "system_state": "degraded"}]

    mgr.stop(); task.cancel()
    try: await task
    except (asyncio.CancelledError, Exception): pass


# ---------------------------------------------------------------------------
# model folding
# ---------------------------------------------------------------------------

def test_model_folds_telemetry_into_control_plane_state():
    m = sp.SystemPanelModel()
    # The Phase-0 handshake nests the whole snapshot under ``status`` (this is
    # exactly what converged_headless._snapshot() emits).
    m.ingest({"type": "hydration",
              "status": {"phase": "READY", "system_state": "ready",
                         "hydration": {"state": "ready",
                                       "subsystems": {"governance_bridge": "ok"}},
                         "selftest": "failover_proven"}})
    assert m.system_state == "ready"
    assert m.hydration_state == "ready"
    assert m.subsystems["governance_bridge"] == "ok"
    assert m.selftest == "failover_proven"
    assert m.phase == "READY"

    m.ingest({"type": "telemetry", "lifecycle": "OUROBOROS_FAULT",
              "narration_text": "O+V faulted — auto-restarting",
              "actor": {"state": "restarting", "restarts": 2}})
    assert m.actor_state == "restarting"
    assert m.actor_restarts == 2
    assert any("OUROBOROS_FAULT" in e for e in m.events)


def test_model_ingest_tolerates_partial_frames():
    m = sp.SystemPanelModel()
    m.ingest({})                       # no type
    m.ingest({"type": "telemetry"})    # no fields
    m.ingest({"type": "line", "text": "hello"})
    assert m.events[-1] == "hello"
    assert m.system_state == "unknown"  # unchanged, no crash


def test_render_panel_shows_offline_overlay_when_detached():
    from rich.console import Console
    m = sp.SystemPanelModel(system_state="ready")
    con = Console(width=80, record=True)
    con.print(sp.render_panel(m, sp.ConnState.OFFLINE))
    out = con.export_text()
    assert "DAEMON OFFLINE" in out and "RECONNECT" in out


def test_render_panel_online_has_no_offline_overlay():
    from rich.console import Console
    m = sp.SystemPanelModel(system_state="ready", hydration_state="ready",
                            selftest="failover_proven")
    con = Console(width=80, record=True)
    con.print(sp.render_panel(m, sp.ConnState.ATTACHED))
    out = con.export_text()
    assert "DAEMON OFFLINE" not in out
    assert "failover_proven" in out

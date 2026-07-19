"""MANDATE 1 — end-to-end SSE telemetry flow.

Inject a mock ``autonomy.op_completed`` into the REAL ``TrinityEventBus``
and assert the EXACT Swift ``DaemonEvent`` Codable JSON is yielded by the
REAL ``EventStream.sse_stream`` async generator (after the device-stream
serialization adapter). No prints, no static string checks — the real
async pipeline, end to end.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.api import governance_sse_bridge as gsb
from backend.api.sse_contract import eventstream_frame_to_jarviskit


class _FakeWS:
    async def send_text(self, text): return None
    async def send_json(self, obj): return None


@pytest.mark.asyncio
async def test_autonomy_event_yields_daemon_contract_through_real_generator(
    monkeypatch,
):
    monkeypatch.setenv("JARVIS_GOVERNANCE_SSE_DRAIN_WINDOW_S", "0.02")

    # ---- REAL EventStream + a registered governance session (so a
    # broadcast COMMITS to the replay buffer) ----
    from backend.core.event_stream import EventStreamProtocol, ClientSession
    es = EventStreamProtocol()
    es._sessions["hud"] = ClientSession(client_id="hud", websocket=_FakeWS())

    # ---- REAL TrinityEventBus ----
    from backend.core.trinity_event_bus import TrinityEventBus
    bus = await TrinityEventBus.create()
    monkeypatch.setattr(
        "backend.core.trinity_event_bus.get_event_bus_if_exists",
        lambda: bus, raising=False)
    monkeypatch.setattr(
        "backend.core.event_stream.get_event_stream_if_initialized",
        lambda: es, raising=False)

    bridge = await gsb.install_governance_sse_bridge()
    assert bridge is not None and bridge.installed

    try:
        # ---- MANDATE 1: inject into the REAL bus ----
        await bus.publish_raw(
            topic="autonomy.op_completed",
            data={"op_id": "op-77", "success": True,
                  "affected_files": ["a.py"]},
            persist=False)

        # Wait until the bus delivered → bridge enqueued → pump broadcast
        # committed the frame to the replay buffer (deterministic).
        for _ in range(60):
            await asyncio.sleep(0.05)
            if bridge.stats["forwarded"] >= 1:
                break
        assert bridge.stats["forwarded"] >= 1, "bridge never broadcast"

        # ---- read the committed frame from the REAL async generator ----
        # Opening with last_ack=0 replays the buffered governance frame.
        gen = es.sse_stream(last_ack=0, channels={"governance"}).__aiter__()
        adapted = None
        for _ in range(10):
            try:
                frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if frame.startswith(":"):
                continue                          # keepalive
            out = eventstream_frame_to_jarviskit(frame)   # device-stream adapter
            if out and "event:daemon" in out:
                adapted = out
                break
        await gen.aclose()

        # ---- assert the EXACT Swift DaemonEvent Codable contract ----
        assert adapted is not None, "no daemon frame yielded by the generator"
        assert "event:daemon\n" in adapted        # byte-exact: NO space
        assert adapted.endswith("\n\n")
        data_line = [l for l in adapted.split("\n") if l.startswith("data:")][0]
        payload = json.loads(data_line[len("data:"):])
        for k in ("command_id", "narration_text", "narration_priority",
                  "source_brain"):
            assert k in payload and isinstance(payload[k], str), f"missing {k}"
        assert payload["command_id"] == "op-77"     # from the injected op_id
        assert payload["source_brain"] == "ouroboros"
    finally:
        await gsb.reset_governance_sse_bridge()
        try:
            await bus.stop()
        except Exception:
            pass

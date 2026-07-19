"""Stateful IPC Audio Pub/Sub Bridge — reconciliation + degradation spine.

Pins the operator-signed audio architecture (2026-07-18): the
unified_supervisor owns the hardware plane; the ov cockpit subscribes to
STATE over a UDS. Four mandates covered:

  1. Root-cause: no audio binding here — pure state pub/sub over the
     socket (the substrate imports no audio/hardware modules).
  2. **State-reconciliation handshake** — a client that connects while
     Karen is MID-SENTENCE receives the accumulated ``text_so_far`` in
     the FIRST frame and renders it immediately (no ghosting, no fresh
     prompt). THE headline telemetry of this suite.
  3. DRY — event kinds are the closed module vocabulary; state machine
     asserted through the same publish surface the supervisor hooks use.
  4. Bulletproof degradation — missing socket / refused path / dead
     server all return ``False`` from a BOUNDED connect (measured), and
     a malformed frame never kills the feed.
"""
from __future__ import annotations

import asyncio
import os
import stat
import time

import pytest

from backend.core.ouroboros.governance.comms.duplex import audio_state_ipc as ipc


@pytest.fixture()
def sock():
    """A SHORT socket path — AF_UNIX caps sun_path at ~104 chars on
    macOS and pytest's tmp_path breaches it. Production is unaffected
    (the default is the repo-relative ``.jarvis/audio_state.sock``)."""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="aipc")
    try:
        yield __import__("pathlib").Path(d) / "a.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def _mk_server(sock):
    b = ipc.AudioStateBroadcaster(path=sock)
    assert await b.start() is True
    return b


class _Sink:
    def __init__(self) -> None:
        self.handshakes = []
        self.messages = []
        self.rendered = []

    def on_handshake(self, msg):
        self.handshakes.append(msg)
        utt = msg.get("active_utterance")
        if utt and utt.get("text_so_far"):
            self.rendered.append(
                f"{utt['role']} (mid-sentence) > {utt['text_so_far']}"
            )

    def on_message(self, msg):
        self.messages.append(msg)


async def _connect(sock, sink):
    c = ipc.AudioStateClient(
        on_handshake=sink.on_handshake, on_message=sink.on_message, path=sock,
    )
    ok = await c.connect()
    return c, ok


# ---------------------------------------------------------------------------
# (1) THE reconciliation handshake — mid-sentence join renders immediately
# ---------------------------------------------------------------------------


async def test_mid_sentence_join_renders_ongoing_transcript(sock):
    b = await _mk_server(sock)
    try:
        # Karen is mid-sentence: three chunks published, none final.
        uid = b.publish_transcript("karen", "The validation gate ")
        b.publish_transcript("karen", "is holding because ", utterance_id=uid)
        b.publish_transcript("karen", "the sandbox drifted", utterance_id=uid)

        # A NEW terminal opens now.
        sink = _Sink()
        c, ok = await _connect(sock, sink)
        assert ok is True
        # The FIRST frame carried the full accumulated text — the UI
        # rendered it with zero further round-trips.
        assert len(sink.handshakes) == 1
        utt = sink.handshakes[0]["active_utterance"]
        assert utt["utterance_id"] == uid
        assert utt["role"] == "karen"
        assert utt["text_so_far"] == (
            "The validation gate is holding because the sandbox drifted"
        )
        assert sink.rendered == [
            "karen (mid-sentence) > The validation gate is holding "
            "because the sandbox drifted"
        ]
        await c.close()
    finally:
        await b.stop()


async def test_handshake_state_snapshot_reflects_live_flags(sock):
    b = await _mk_server(sock)
    try:
        b.publish_event(ipc.EVENT_TTS_GENERATING)
        b.publish_event(ipc.EVENT_AUDIO_PLAYING)
        sink = _Sink()
        c, ok = await _connect(sock, sink)
        assert ok
        state = sink.handshakes[0]["state"]
        assert state["audio_playing"] is True
        assert state["tts_generating"] is False   # PLAYING supersedes
        assert sink.handshakes[0]["schema_version"] == (
            ipc.AUDIO_IPC_SCHEMA_VERSION
        )
        await c.close()
    finally:
        await b.stop()


async def test_final_utterance_clears_active_slot(sock):
    b = await _mk_server(sock)
    try:
        uid = b.publish_transcript("karen", "done now", final=True)
        assert uid
        sink = _Sink()
        c, ok = await _connect(sock, sink)
        assert ok
        assert sink.handshakes[0]["active_utterance"] is None
        # ...but the sealed line is replayable from the recent ring.
        recent = sink.handshakes[0]["recent"]
        assert any(
            m.get("type") == "transcript" and m.get("final") for m in recent
        )
        await c.close()
    finally:
        await b.stop()


# ---------------------------------------------------------------------------
# (2) Live pub/sub after connect
# ---------------------------------------------------------------------------


async def test_events_stream_to_connected_clients(sock):
    b = await _mk_server(sock)
    try:
        sink = _Sink()
        c, ok = await _connect(sock, sink)
        assert ok
        b.publish_event(ipc.EVENT_VAD_ACTIVE)
        b.publish_transcript("user", "run the tests", final=True)
        for _ in range(50):
            if len(sink.messages) >= 2:
                break
            await asyncio.sleep(0.02)
        kinds = [m.get("kind") or m.get("type") for m in sink.messages]
        assert "VAD_ACTIVE" in kinds
        assert "transcript" in kinds
        await c.close()
    finally:
        await b.stop()


async def test_vad_publisher_is_edge_coalesced(sock):
    b = await _mk_server(sock)
    try:
        sink = _Sink()
        c, ok = await _connect(sock, sink)
        assert ok
        for _ in range(25):
            b.publish_vad(True)      # 50Hz frame spam — ONE edge
        b.publish_vad(False)
        for _ in range(50):
            if len(sink.messages) >= 2:
                break
            await asyncio.sleep(0.02)
        kinds = [m.get("kind") for m in sink.messages]
        assert kinds.count("VAD_ACTIVE") == 1
        assert kinds.count("VAD_INACTIVE") == 1
        await c.close()
    finally:
        await b.stop()


async def test_two_clients_both_receive(sock):
    b = await _mk_server(sock)
    try:
        s1, s2 = _Sink(), _Sink()
        c1, ok1 = await _connect(sock, s1)
        c2, ok2 = await _connect(sock, s2)
        assert ok1 and ok2 and b.client_count == 2
        b.publish_event(ipc.EVENT_TTS_GENERATING)
        for _ in range(50):
            if s1.messages and s2.messages:
                break
            await asyncio.sleep(0.02)
        assert s1.messages and s2.messages
        await c1.close()
        await c2.close()
    finally:
        await b.stop()


# ---------------------------------------------------------------------------
# (3) Bulletproof degradation — the dead-daemon contract
# ---------------------------------------------------------------------------


async def test_missing_socket_degrades_fast_and_false(sock):
    c = ipc.AudioStateClient(path=sock.parent / "nope.sock")
    t0 = time.monotonic()
    ok = await c.connect()
    elapsed = time.monotonic() - t0
    assert ok is False
    assert c.connected is False
    assert elapsed < 1.5          # bounded — never hangs the UI loop


async def test_refused_non_socket_path_degrades(sock):
    bogus = sock.parent / "plainfile.sock"
    bogus.write_text("plain file")
    c = ipc.AudioStateClient(path=bogus)
    assert await c.connect() is False


async def test_server_gone_midstream_marks_disconnected(sock):
    b = await _mk_server(sock)
    sink = _Sink()
    c, ok = await _connect(sock, sink)
    assert ok and c.connected
    await b.stop()
    for _ in range(50):
        if not c.connected:
            break
        await asyncio.sleep(0.02)
    assert c.connected is False
    await c.close()


async def test_malformed_frame_never_kills_feed(sock):
    b = await _mk_server(sock)
    try:
        sink = _Sink()
        c, ok = await _connect(sock, sink)
        assert ok
        # Inject garbage directly to every connected writer.
        for w in list(b._clients):
            w.write(b"{not json}\n")
        b.publish_event(ipc.EVENT_AUDIO_IDLE)
        for _ in range(50):
            if sink.messages:
                break
            await asyncio.sleep(0.02)
        assert any(m.get("kind") == "AUDIO_IDLE" for m in sink.messages)
        await c.close()
    finally:
        await b.stop()


# ---------------------------------------------------------------------------
# (4) Hygiene — perms, ring bounds, stale-socket rebind, teardown
# ---------------------------------------------------------------------------


async def test_socket_perms_owner_only(sock):
    b = await _mk_server(sock)
    try:
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        assert mode == 0o600
    finally:
        await b.stop()


async def test_recent_ring_is_bounded(sock, monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIO_IPC_RECENT", "10")
    b = ipc.AudioStateBroadcaster(path=sock)
    assert await b.start()
    try:
        for i in range(50):
            b.publish_transcript("karen", f"line {i}", final=True)
        sink = _Sink()
        c, ok = await _connect(sock, sink)
        assert ok
        assert len(sink.handshakes[0]["recent"]) <= 10
        await c.close()
    finally:
        await b.stop()


async def test_stale_socket_file_is_rebindable(sock):
    b1 = await _mk_server(sock)
    # Simulate crash: no stop(), just drop the server object's bind by
    # closing without unlink — then a fresh boot must still bind.
    b1._server.close()
    b2 = ipc.AudioStateBroadcaster(path=sock)
    assert await b2.start() is True
    await b2.stop()


async def test_stop_unlinks_socket(sock):
    b = await _mk_server(sock)
    assert sock.exists()
    await b.stop()
    assert not sock.exists()


# ---------------------------------------------------------------------------
# (5) Wiring pins — supervisor + cockpit mounts (source-anchored)
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / rel).read_text()


def test_supervisor_bootstrap_mounts_broadcaster_and_hooks():
    src = _read("backend/audio/audio_pipeline_bootstrap.py")
    assert "AudioStateBroadcaster" in src
    assert "publish_vad" in src                  # shared VAD decision feeds IPC
    assert "_ipc_turn_fork" in src               # user transcript fork
    assert "_prev_fork" in src                   # ...CHAINS the voice->build fork
    assert "_ipc_submit_speech" in src           # Karen line wrapper
    assert "handle.audio_ipc.stop()" in src      # shutdown unlinks socket


def test_cockpit_harness_mounts_bounded_client():
    src = _read("backend/core/ouroboros/battle_test/harness.py")
    assert "_start_audio_ipc_client" in src
    assert "is_cockpit()" in src
    body = src[src.index("async def _start_audio_ipc_client"):][:4000]
    assert "mid-sentence" in body                # reconciliation render
    assert "text-only" in body                   # degradation contract

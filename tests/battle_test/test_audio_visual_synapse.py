"""Audio-Visual Synapse + Rigid TUI Cockpit spine (operator mandate 2026-07-18).

Covers the four authorized roots:

  1. attach-protocol v2 audio lane — upstream ``{"type":"audio"}``
     command frames, downstream ``{"type":"audio_state"}`` FSM frames,
     hydration retention for late joiners;
  2. ``AudioVisualSynapse`` — the DRY remote-control adapter over the
     EXISTING karen_duplex handle (no audio logic of its own);
  3. Dynamic UI Morphing — a mocked incoming ``AUDIO_STATE: LISTENING``
     frame repaints the Footer prompt asynchronously WITHOUT touching
     the active keystroke buffer (mandate 4, verbatim);
  4. leak class kills — ``emit_handshake`` raw-stdout silenced in
     cockpit; crest off-by-one bounding margin + single-frame blit.

UDS tests use short ``tempfile.mkdtemp`` roots (macOS ~104-char
``sun_path``) and need the sandbox disabled (UDS bind).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.audio_synapse import AudioVisualSynapse
from backend.core.ouroboros.battle_test.cockpit_attach import (
    AUDIO_STATES,
    CockpitAttachBridge,
    CockpitAttachClient,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def sock_dir():
    d = Path(tempfile.mkdtemp(prefix="avs-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def _pair(sock_dir, **bridge_kw):
    path = sock_dir / "a.sock"
    bridge = CockpitAttachBridge(path=path, **bridge_kw)
    assert await bridge.start()
    return bridge, path


# ---------------------------------------------------------------------------
# (1) Protocol v2 — the audio lane
# ---------------------------------------------------------------------------


class TestAttachProtocolV2:
    async def test_upstream_audio_frame_reaches_on_audio(self, sock_dir):
        got: list = []
        bridge, path = await _pair(sock_dir, on_audio=got.append)
        client = CockpitAttachClient(path=path)
        try:
            assert await client.connect()
            assert client.send_audio("wake")
            for _ in range(40):
                if got:
                    break
                await asyncio.sleep(0.05)
            assert got == ["wake"]
        finally:
            await client.close()
            await bridge.stop()

    async def test_bogus_audio_cmd_refused_client_side(self, sock_dir):
        bridge, path = await _pair(sock_dir)
        client = CockpitAttachClient(path=path)
        try:
            assert await client.connect()
            assert client.send_audio("rm -rf /") is False
            assert client.send_audio("") is False
        finally:
            await client.close()
            await bridge.stop()

    async def test_downstream_audio_state_reaches_client(self, sock_dir):
        states: list = []
        bridge, path = await _pair(sock_dir)
        client = CockpitAttachClient(
            path=path, on_audio_state=states.append,
        )
        try:
            assert await client.connect()
            bridge.publish_audio_state("LISTENING")
            for _ in range(40):
                if states:
                    break
                await asyncio.sleep(0.05)
            assert states == ["LISTENING"]
        finally:
            await client.close()
            await bridge.stop()

    async def test_publish_is_edge_coalesced(self, sock_dir):
        bridge, path = await _pair(sock_dir)
        try:
            bridge.publish_audio_state("LISTENING")
            bridge.publish_audio_state("LISTENING")
            bridge.publish_audio_state("SPEAKING")
            bridge.publish_audio_state("not-a-state")
            assert bridge.stats["audio_states_published"] == 2
        finally:
            await bridge.stop()

    async def test_hydration_carries_current_audio_state(self, sock_dir):
        bridge, path = await _pair(sock_dir)
        bridge.publish_audio_state("THINKING")
        payloads: list = []
        client = CockpitAttachClient(
            path=path, on_hydration=payloads.append,
        )
        try:
            assert await client.connect()
            assert payloads and payloads[0]["audio"]["state"] == "THINKING"
            assert payloads[0]["schema_version"] == "cockpit_attach.v2"
        finally:
            await client.close()
            await bridge.stop()


# ---------------------------------------------------------------------------
# (2) The synapse adapter — DRY remote control of the existing duplex
# ---------------------------------------------------------------------------


class _FakeState:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeArbiter:
    def __init__(self) -> None:
        self.state = _FakeState("listening")
        self.barges = 0

    async def on_user_speech_start(self) -> None:
        self.barges += 1

    async def on_user_speech_end(self) -> None:
        pass


class _FakeHandle:
    def __init__(self) -> None:
        self.arbiter = _FakeArbiter()
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class TestAudioVisualSynapse:
    async def test_wake_without_duplex_answers_unavailable(
        self, sock_dir, monkeypatch,
    ):
        # Isolate from any LIVE supervisor socket on this machine — the
        # broker path (v2) would otherwise genuinely acquire a lease.
        monkeypatch.setenv(
            "JARVIS_AUDIO_IPC_SOCKET", str(sock_dir / "absent.sock"),
        )
        out: list = []
        syn = AudioVisualSynapse(out.append, handle_resolver=lambda: None)
        await syn.handle_cmd("wake")
        assert out == ["UNAVAILABLE"]
        assert syn.armed is False

    async def test_wake_starts_existing_handle_and_publishes_listening(self):
        out: list = []
        handle = _FakeHandle()
        syn = AudioVisualSynapse(out.append, handle_resolver=lambda: handle)
        await syn.handle_cmd("wake")
        try:
            assert handle.started == 1          # DRY: ITS start, not ours
            assert out[0] == "LISTENING"
            assert syn.armed is True
        finally:
            await syn.stop()

    async def test_fsm_edges_stream_through_watch(self):
        out: list = []
        handle = _FakeHandle()
        syn = AudioVisualSynapse(out.append, handle_resolver=lambda: handle)
        await syn.handle_cmd("wake")
        try:
            handle.arbiter.state = _FakeState("thinking")
            await asyncio.sleep(0.4)
            handle.arbiter.state = _FakeState("karen_speaking")
            await asyncio.sleep(0.4)
            assert "THINKING" in out and "SPEAKING" in out
            # Edge-coalesced: steady states don't repeat.
            assert len(out) == len(set(out))
        finally:
            await syn.stop()

    async def test_sleep_stops_handle_and_goes_offline(self):
        out: list = []
        handle = _FakeHandle()
        syn = AudioVisualSynapse(out.append, handle_resolver=lambda: handle)
        await syn.handle_cmd("wake")
        await syn.handle_cmd("sleep")
        assert handle.stopped == 1
        assert out[-1] == "OFFLINE"
        assert syn.armed is False

    async def test_barge_routes_to_arbiter_interrupt_seam(self):
        out: list = []
        handle = _FakeHandle()
        syn = AudioVisualSynapse(out.append, handle_resolver=lambda: handle)
        await syn.handle_cmd("barge")
        assert handle.arbiter.barges == 1

    async def test_publish_fault_never_raises(self):
        def _boom(_s: str) -> None:
            raise RuntimeError("sink died")
        syn = AudioVisualSynapse(_boom, handle_resolver=lambda: None)
        await syn.handle_cmd("wake")           # must not raise

    def test_all_published_states_are_in_the_closed_taxonomy(self):
        from backend.core.ouroboros.battle_test import audio_synapse
        for mapped in audio_synapse._FSM_MAP.values():
            assert mapped in AUDIO_STATES


# ---------------------------------------------------------------------------
# (3) Dynamic UI Morphing — mandate 4 verbatim
# ---------------------------------------------------------------------------


class TestFooterMorphing:
    def _session(self):
        from prompt_toolkit import PromptSession
        from backend.core.ouroboros.cli.ov import AttachUI
        ui = AttachUI()
        session = PromptSession(
            message=lambda: ui.prompt(),
            bottom_toolbar=lambda: ui.toolbar(),
        )
        ui.bind_app(session.app)
        return ui, session

    async def test_listening_frame_morphs_prompt_without_touching_buffer(self):
        """Mock an incoming ``AUDIO_STATE: LISTENING`` IPC frame; the
        Footer prompt repaints while the active keystroke buffer is
        preserved byte-for-byte."""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                ui, session = self._session()
                # Operator mid-keystroke:
                session.default_buffer.insert_text("deploy the fix")
                assert ui.prompt() == "ov › "
                # The mocked IPC frame lands (exactly what
                # CockpitAttachClient's read loop dispatches):
                ui.on_audio_state("LISTENING")
                assert ui.prompt() == "🎙 Karen › "
                assert "listening" in ui.toolbar()
                # Keystroke integrity: the buffer was never interrupted.
                assert session.default_buffer.text == "deploy the fix"

    def test_full_fsm_cycle_prompts(self):
        from backend.core.ouroboros.cli.ov import AttachUI
        ui = AttachUI()
        seen = []
        for state in ("LISTENING", "HEARING", "THINKING", "SPEAKING"):
            ui.on_audio_state(state)
            seen.append(ui.prompt())
        assert len(set(seen)) == 4              # every state is distinct
        ui.on_audio_state("OFFLINE")
        assert ui.prompt() == "ov › "

    def test_unknown_state_is_inert(self):
        from backend.core.ouroboros.cli.ov import AttachUI
        ui = AttachUI()
        ui.on_audio_state("QUANTUM")
        assert ui.prompt() == "ov › "           # graceful: default prompt

    def test_invalidate_called_on_morph(self):
        from backend.core.ouroboros.cli.ov import AttachUI

        class _App:
            def __init__(self) -> None:
                self.invalidations = 0

            def invalidate(self) -> None:
                self.invalidations += 1

        ui = AttachUI()
        app = _App()
        ui.bind_app(app)
        ui.on_audio_state("LISTENING")
        ui.on_audio_state("LISTENING")          # same state = no repaint
        assert app.invalidations == 1


# ---------------------------------------------------------------------------
# (4) Leak-class kills — handshake silencing + crest bounding/blit
# ---------------------------------------------------------------------------


class TestHandshakeSilencing:
    def test_cockpit_suppresses_raw_stdout(self, monkeypatch, capsys):
        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
        from backend.core.ouroboros.governance import fsm_checkpoint
        fsm_checkpoint.emit_handshake("[HYDRATION-HANDSHAKE] test-line")
        assert "[HYDRATION-HANDSHAKE]" not in capsys.readouterr().out

    def test_soak_keeps_raw_stdout_forensics(self, monkeypatch, capsys):
        monkeypatch.setenv("JARVIS_OV_PRESENTATION", "soak")
        from backend.core.ouroboros.governance import fsm_checkpoint
        fsm_checkpoint.emit_handshake("[HYDRATION-HANDSHAKE] test-line")
        assert "[HYDRATION-HANDSHAKE] test-line" in capsys.readouterr().out


class TestCrestBoundingAndBlit:
    def test_clamp_reserves_one_column_margin(self, monkeypatch):
        monkeypatch.delenv("JARVIS_OV_CREST_MIN_COLS", raising=False)
        monkeypatch.delenv("JARVIS_OV_CREST_MAX_COLS", raising=False)
        from backend.core.ouroboros.ui import crest
        # A crest exactly as wide as the terminal wraps its last cell —
        # the off-by-one detached-artifact class. One column of margin.
        _lo, _hi, clamped = crest._clamp_cols(80)
        assert clamped == 79
        _lo, _hi, clamped = crest._clamp_cols(200)
        assert clamped == 88                    # cap still wins when roomy

    def test_blit_writes_one_atomic_frame(self):
        from backend.core.ouroboros.ui import crest
        from rich.text import Text

        class _CountingFile:
            def __init__(self) -> None:
                self.writes: list = []

            def write(self, s: str) -> None:
                self.writes.append(s)

            def flush(self) -> None:
                pass

        class _Size:
            width, height = 100, 50

        class _Console:
            size = _Size()
            file = _CountingFile()

        console = _Console()
        assert crest.blit_text(console, Text("▀▀▀\n▀▀▀"))
        # Double-buffered: the whole frame arrived in a SINGLE write.
        assert len(console.file.writes) == 1
        assert "▀▀▀" in console.file.writes[0]

    def test_print_static_crest_routes_through_blit_pin(self):
        src = (_REPO / "backend/core/ouroboros/ui/crest.py").read_text()
        body = src[src.index("def print_static_crest"):]
        assert "blit_text(console, emblem)" in body

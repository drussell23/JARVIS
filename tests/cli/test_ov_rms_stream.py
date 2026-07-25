"""The header wave is driven by the SUPERVISOR's microphone, not the cockpit's.

The gap these tests close. `ov` and `unified_supervisor` are separate
processes, and CoreAudio hands the microphone to exactly one of them — the
supervisor. The scope was wired only to ``audio_broadcast_tap``, an in-process
broadcast, so inside the cockpit process nothing ever captured audio, the tap
never fired, and the wave sat at its flat baseline however loudly anyone spoke.

The supervisor had been publishing ``rms_level`` frames on the audio-state
socket the entire time. There was simply no reader — and a producer with no
reader looks exactly like a silent room, which is why it went unnoticed.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from backend.core.ouroboros.cli.ov import _attach_rms_stream
from backend.core.ouroboros.ui.audio_scope import AudioPlane, BrailleScope


@pytest.fixture
def scope():
    return BrailleScope(width=8)


# ---------------------------------------------------------------------------
# End-to-end over a REAL unix socket — the only proof that matters
# ---------------------------------------------------------------------------


async def _live_session(tmp_path, monkeypatch, scope):
    # NOT tmp_path: macOS caps sun_path at ~104 bytes and pytest's per-test
    # directory names blow straight past it, so binding fails for a reason
    # that has nothing to do with the code under test.
    import tempfile
    sock_dir = tempfile.mkdtemp(prefix="ovrms")
    monkeypatch.setenv(
        "JARVIS_AUDIO_IPC_SOCKET", str(Path(sock_dir) / "a.sock"),
    )
    from backend.core.ouroboros.governance.comms.duplex import (
        audio_state_ipc as ipc,
    )

    server = ipc.AudioStateBroadcaster()
    if not await server.start():
        pytest.skip("cannot bind a unix socket in this environment")
    client = await _attach_rms_stream(scope)
    if client is None:
        await server.stop()
        pytest.skip("subscriber could not connect in this environment")
    await asyncio.sleep(0.1)
    return server, client


async def test_the_wave_moves_when_the_operator_speaks(tmp_path, monkeypatch, scope):
    """THE REGRESSION. Amplitude published by the supervisor must reach the
    cockpit's scope — this is the whole feature."""
    server, client = await _live_session(tmp_path, monkeypatch, scope)
    try:
        assert scope.is_silent(), "scope started with phantom signal"

        for i in range(16):
            server.publish_rms(abs(math.sin(i / 2.2)) * 0.95, plane="user")
        await asyncio.sleep(0.35)

        assert not scope.is_silent(), "the wave never moved"
        assert scope.plane is AudioPlane.USER
        assert scope.render() != "⣀" * scope.width
    finally:
        await client.close()
        await server.stop()


async def test_karen_speaking_switches_the_plane(tmp_path, monkeypatch, scope):
    """Colour answers 'is that me or her?' at a glance — cyan for the operator,
    venom green for Karen."""
    server, client = await _live_session(tmp_path, monkeypatch, scope)
    try:
        server.publish_rms(0.8, plane="user")
        await asyncio.sleep(0.2)
        assert scope.plane is AudioPlane.USER

        server.publish_rms(0.8, plane="karen")
        await asyncio.sleep(0.2)
        assert scope.plane is AudioPlane.SYSTEM
        assert scope.accent != "", "Karen's plane has no colour"
    finally:
        await client.close()
        await server.stop()


async def test_a_dead_supervisor_leaves_a_working_cockpit(tmp_path, monkeypatch, scope):
    """No socket must cost the wave, never the terminal."""
    monkeypatch.setenv(
        "JARVIS_AUDIO_IPC_SOCKET", str(tmp_path / "absent.sock"),
    )
    assert await _attach_rms_stream(scope) is None
    assert scope.render()          # still renders its baseline


# ---------------------------------------------------------------------------
# Frame handling
# ---------------------------------------------------------------------------


async def test_non_amplitude_frames_are_ignored(tmp_path, monkeypatch, scope):
    """The socket carries state changes, warnings and handshakes on the same
    lane. Only rms_level drives the wave."""
    server, client = await _live_session(tmp_path, monkeypatch, scope)
    try:
        server.publish_vad(True)
        await asyncio.sleep(0.2)
        assert scope.is_silent(), "a state frame moved the wave"
    finally:
        await client.close()
        await server.stop()


async def test_levels_are_taken_as_already_normalized(tmp_path, monkeypatch, scope):
    """RMS and adaptive scaling ran on the PRODUCER side, next to the frames.
    Re-normalizing here would square the curve and make quiet speech vanish."""
    server, client = await _live_session(tmp_path, monkeypatch, scope)
    try:
        server.publish_rms(0.5, plane="user")
        await asyncio.sleep(0.25)
        assert scope.samples(), "no sample landed"
        assert scope.samples()[-1] == pytest.approx(0.5, abs=0.01)
    finally:
        await client.close()
        await server.stop()


async def test_a_stalled_stream_decays_to_the_baseline(tmp_path, monkeypatch, scope):
    """Composes with kinetic decay: when the supervisor stops publishing, the
    wave falls rather than freezing on its last amplitude."""
    server, client = await _live_session(tmp_path, monkeypatch, scope)
    try:
        for _ in range(16):
            server.publish_rms(0.9, plane="user")
        await asyncio.sleep(0.3)
        assert not scope.is_silent()

        await asyncio.sleep(0.5)
        scope.tick()
        assert scope.render() == "⣀" * scope.width, "the wave froze"
    finally:
        await client.close()
        await server.stop()


# ---------------------------------------------------------------------------
# Wiring pin
# ---------------------------------------------------------------------------


def test_the_cockpit_actually_subscribes():
    """Structural pin against the exact trap this closes: a publisher with no
    subscriber is indistinguishable from silence, so no runtime check catches
    it. Only the wiring can be asserted."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/cli/ov.py",
    ).read_text(encoding="utf-8")
    assert "_attach_rms_stream(_scope)" in src, (
        "the cockpit no longer subscribes to the supervisor's amplitude stream"
    )
    assert 'rms_client' in src


def test_the_subscription_is_released_on_exit():
    """A live socket plus its read task must not outlive the cockpit."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/cli/ov.py",
    ).read_text(encoding="utf-8")
    tail = src[src.index("await run_bipartite_repl("):][:2000]
    assert "finally:" in tail and "rms_client" in tail

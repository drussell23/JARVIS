"""There is one speaker: lines may be synthesized in parallel, never played so."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time

import pytest

from backend.core import native_audio_player as nap


@pytest.fixture
def recorded(monkeypatch):
    spans = []
    lock = threading.Lock()

    def fake_player(argv, timeout):
        start = time.monotonic()
        time.sleep(0.2)
        with lock:
            spans.append((start, time.monotonic(), argv[-1]))

    monkeypatch.setattr(nap, "_run_player", fake_player)
    return spans


def test_concurrent_lines_play_one_after_another(recorded):
    async def main():
        await asyncio.gather(*(nap.play_audio_file(f"line{i}") for i in range(3)))

    asyncio.run(main())
    spans = sorted(recorded)
    assert len(spans) == 3
    for (_, end, _), (start, _, _) in zip(spans, spans[1:]):
        assert start >= end, "two lines played at once"


def test_a_line_that_would_start_stale_is_dropped(recorded):
    async def main():
        first = asyncio.create_task(nap.play_audio_file("long"))
        await asyncio.sleep(0.05)
        with pytest.raises(nap.SpeakerBusy):
            await nap.play_audio_file("late", wait_s=0.05)
        await first

    asyncio.run(main())
    assert [p for _, _, p in recorded] == ["long"]


def test_the_speaker_is_released_after_a_failed_line(monkeypatch):
    def broken(argv, timeout):
        raise OSError("device gone")

    monkeypatch.setattr(nap, "_run_player", broken)
    with pytest.raises(OSError):
        asyncio.run(nap.play_audio_file("x"))
    assert nap._speaker.acquire(blocking=False)
    nap._speaker.release()


def test_every_platform_plays_in_a_player_process(monkeypatch):
    """A line is played by a fresh process that meets the audio endpoint as
    it is now -- an in-process MCI kept a Remote Desktop endpoint that a
    reconnect had replaced, and went silent."""
    for platform in ("win32", "darwin", "linux"):
        monkeypatch.setattr(nap.sys, "platform", platform)
        argv = nap.player_command("line.mp3")
        assert argv and argv[-1] == "line.mp3"
    monkeypatch.setattr(nap.sys, "platform", "win32")
    argv = nap.player_command("line.mp3")
    assert argv[1:3] == ["-I", "-S"]
    assert argv[3].endswith("native_audio_player.py")


def _completed(code, stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=None, stderr=stderr)


def test_a_player_that_fails_is_a_failed_line(monkeypatch):
    """The exit status is the verdict and the player's words are the reason;
    afplay's status used to be ignored, so a silent line counted as spoken."""
    monkeypatch.setattr(nap.subprocess, "run",
                        lambda *a, **k: _completed(1, b"open x.mp3: MCI error 277: internal error\n"))
    with pytest.raises(OSError, match="MCI error 277"):
        nap._run_player(["player", "x.mp3"], timeout=5)
    monkeypatch.setattr(nap.subprocess, "run", lambda *a, **k: _completed(0))
    nap._run_player(["player", "x.mp3"], timeout=5)


def test_a_line_past_its_timeout_is_stopped(monkeypatch):
    def overrun(argv, timeout=None, **kwargs):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(nap.subprocess, "run", overrun)
    with pytest.raises(TimeoutError):
        nap._run_player(["player", "x.mp3"], timeout=0.5)


@pytest.mark.skipif(sys.platform != "win32", reason="the MCI player exists on Windows")
def test_the_windows_player_reports_mci_in_its_own_words(tmp_path):
    """Run the real player process on a file MCI cannot open: it exits non-zero
    and says why, without touching the speaker."""
    bad = tmp_path / "empty.mp3"
    bad.write_bytes(b"")
    with pytest.raises(OSError, match="MCI error"):
        nap._run_player(nap.player_command(str(bad)), timeout=30)

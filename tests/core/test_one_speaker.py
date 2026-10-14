"""There is one speaker: lines may be synthesized in parallel, never played so."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from backend.core import native_audio_player as nap


@pytest.fixture
def recorded(monkeypatch):
    spans = []
    lock = threading.Lock()

    def fake_play(path):
        start = time.monotonic()
        time.sleep(0.2)
        with lock:
            spans.append((start, time.monotonic(), path))

    monkeypatch.setattr(nap, "player_command", lambda path: None)
    monkeypatch.setattr(nap, "_mci_play_blocking", fake_play)
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
    monkeypatch.setattr(nap, "player_command", lambda path: None)

    def broken(path):
        raise OSError("device gone")

    monkeypatch.setattr(nap, "_mci_play_blocking", broken)
    with pytest.raises(OSError):
        asyncio.run(nap.play_audio_file("x"))
    assert nap._speaker.acquire(blocking=False)
    nap._speaker.release()

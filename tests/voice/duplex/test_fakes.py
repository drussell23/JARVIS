# tests/voice/duplex/test_fakes.py
from __future__ import annotations

import asyncio
import pytest

from tests.voice.duplex.fakes import FakePlayback


@pytest.mark.asyncio
async def test_fake_play_completes_on_release():
    fp = FakePlayback()
    task = asyncio.create_task(fp.play("hello"))
    await asyncio.sleep(0)          # let play() start
    assert fp.is_active is True
    assert fp.played == ["hello"]
    fp.release()                    # simulate playback finishing
    await task
    assert fp.is_active is False


@pytest.mark.asyncio
async def test_fake_preempt_cancels_active_play():
    fp = FakePlayback()
    task = asyncio.create_task(fp.play("hello"))
    await asyncio.sleep(0)
    fp.preempt()                    # barge-in
    await task
    assert fp.is_active is False
    assert fp.preempt_count == 1

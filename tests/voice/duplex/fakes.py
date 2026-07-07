# tests/voice/duplex/fakes.py
from __future__ import annotations

import asyncio
from typing import List


class FakePlayback:
    """Controllable PlaybackHandle for arbiter tests. play() awaits an internal
    Event so tests deterministically control when 'audio' finishes (release())
    or is cut off (preempt())."""

    def __init__(self) -> None:
        self.played: List[str] = []
        self.preempt_count = 0
        self._active = False
        self._gate: "asyncio.Event | None" = None

    @property
    def is_active(self) -> bool:
        return self._active

    async def play(self, text: str) -> None:
        self.played.append(text)
        self._active = True
        self._gate = asyncio.Event()
        try:
            await self._gate.wait()
        finally:
            self._active = False

    def preempt(self) -> None:
        self.preempt_count += 1
        self._active = False
        if self._gate is not None:
            self._gate.set()   # unblock play() early

    def release(self) -> None:
        """Test helper: simulate playback finishing naturally."""
        if self._gate is not None:
            self._gate.set()

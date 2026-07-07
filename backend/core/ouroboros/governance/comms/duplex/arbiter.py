from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque, Dict, Optional

from .protocols import (
    ArbiterConfig, PlaybackHandle, Priority, SpeechRequest, VoiceState,
)

logger = logging.getLogger("Ouroboros.Karen.Arbiter")


class VoiceDuplexArbiter:
    """Single async owner of the audio floor (Sprint 1: engine-free)."""

    def __init__(
        self, playback: PlaybackHandle, *, config: Optional[ArbiterConfig] = None,
    ) -> None:
        self._playback = playback
        self._config = config or ArbiterConfig.from_env()
        self._state = VoiceState.LISTENING
        # Per-priority FIFO queues (drop-oldest bounded in a later task).
        self._queues: Dict[Priority, Deque[SpeechRequest]] = {
            p: deque() for p in Priority
        }
        self._wake = asyncio.Event()          # signalled when work is enqueued
        self._play_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def state(self) -> VoiceState:
        return self._state

    def submit(self, request: SpeechRequest) -> None:
        """Non-blocking enqueue. Never raises."""
        if not self._config.enabled:
            return
        try:
            self._queues[request.priority].append(request)
            self._wake.set()
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] submit failed", exc_info=True)

    def _pop_highest(self) -> Optional[SpeechRequest]:
        for p in sorted(Priority, reverse=True):
            q = self._queues[p]
            if q:
                return q.popleft()
        return None

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self._wake.wait()
            self._wake.clear()
            while self._state == VoiceState.LISTENING:
                req = self._pop_highest()
                if req is None:
                    break
                await self._speak(req)

    async def _speak(self, req: SpeechRequest) -> None:
        self._state = VoiceState.KAREN_SPEAKING
        self._play_task = asyncio.create_task(self._playback.play(req.text))
        try:
            await self._play_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] playback failed", exc_info=True)
        finally:
            self._play_task = None
            if self._state == VoiceState.KAREN_SPEAKING:
                self._state = VoiceState.LISTENING

    async def stop(self) -> None:
        # Clean shutdown: cancel any active playback so no blocked play task
        # leaks past teardown (bulletproof mandate #4). Idempotent + never raises.
        self._running = False
        if self._play_task is not None:
            try:
                self._playback.preempt()
                self._play_task.cancel()
            except Exception:  # noqa: BLE001
                logger.debug("[Arbiter] stop cleanup failed", exc_info=True)
        self._wake.set()

    def snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "queued": {p.name: len(q) for p, q in self._queues.items()},
            "enabled": self._config.enabled,
        }

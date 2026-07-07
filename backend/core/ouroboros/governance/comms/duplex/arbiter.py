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
        self._stopped = False
        self._active_priority: Optional[Priority] = None
        self.shed_count = 0
        self.coalesced_count = 0

    @property
    def state(self) -> VoiceState:
        return self._state

    def submit(self, request: SpeechRequest) -> None:
        """Non-blocking enqueue. A strictly-higher-priority request preempts an
        active lower-priority playback (but never interrupts the user). Never
        raises."""
        if not self._config.enabled:
            return
        if (
            request.priority in (Priority.PROACTIVE_INFO, Priority.PROACTIVE_CRITICAL)
            and not self._config.proactive_enabled
        ):
            return
        try:
            q = self._queues[request.priority]
            if request.coalesce_key:
                before = len(q)
                q = deque(
                    r for r in q if r.coalesce_key != request.coalesce_key
                )
                self._queues[request.priority] = q
                self.coalesced_count += before - len(q)
            q.append(request)
            while len(q) > max(1, self._config.queue_max_per_priority):
                q.popleft()
                self.shed_count += 1
            if (
                self._state == VoiceState.KAREN_SPEAKING
                and self._active_priority is not None
                and request.priority > self._active_priority
            ):
                self._playback.preempt()
                if self._play_task is not None:
                    self._play_task.cancel()
                self._state = VoiceState.LISTENING     # let run() pick the winner
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
        if self._stopped:
            return
        self._running = True
        while self._running:
            await self._wake.wait()
            self._wake.clear()
            # Inner drain MUST also honor _running: after stop() sets it false,
            # _speak's finally flips state back to LISTENING, and without this
            # guard the loop would pop queued work into a new, never-cancelled
            # play task that blocks teardown forever (shutdown race).
            while self._running and self._state == VoiceState.LISTENING:
                req = self._pop_highest()
                if req is None:
                    break
                await self._speak(req)

    async def _speak(self, req: SpeechRequest) -> None:
        self._state = VoiceState.KAREN_SPEAKING
        self._active_priority = req.priority
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
            self._active_priority = None

    async def on_user_speech_start(self) -> None:
        """Barge-in trigger. Preempts Karen and holds the floor. Never raises."""
        if not self._config.barge_in_enabled:
            return
        try:
            if self._state == VoiceState.KAREN_SPEAKING:
                self._playback.preempt()          # kill playback (idempotent)
                if self._play_task is not None:
                    self._play_task.cancel()
            self._state = VoiceState.USER_SPEAKING
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] on_user_speech_start failed", exc_info=True)

    async def on_user_speech_end(self) -> None:
        try:
            if self._state == VoiceState.USER_SPEAKING:
                self._state = VoiceState.LISTENING
                self._wake.set()                  # resume draining the queue
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] on_user_speech_end failed", exc_info=True)

    async def stop(self) -> None:
        # Clean shutdown: cancel any active playback so no blocked play task
        # leaks past teardown (bulletproof mandate #4). Idempotent + never raises.
        self._stopped = True
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
            "shed_count": self.shed_count,
            "coalesced_count": self.coalesced_count,
        }

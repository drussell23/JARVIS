"""backend/core/ouroboros/governance/comms/duplex/vad_bridge.py

`VADArbiterBridge` — bridges the existing VAD speech-active signal (from
`StreamingSTTEngine` / the AudioBus barge-in VAD consumer) to the Sprint-1
`VoiceDuplexArbiter`'s barge-in lifecycle.

Design (Sprint 3 Step 2):

* **Edge-coalescing, no timers (mandates #1 + #4).** Only a genuine
  ``False -> True`` VAD edge fires ``on_user_speech_start`` and ``True ->
  False`` fires ``on_user_speech_end``; redundant same-state signals are
  dropped. There is NO sleep / debounce window — the millisecond-flicker
  hysteresis already lives in ``webrtcvad`` + ``StreamingSTTEngine``'s
  speech-active hangover (mandate #3: leverage it, don't re-add it).

* **Audio-thread safe, non-blocking (mandate #2).** ``feed(is_speech)`` is
  synchronous and safe to call from the PortAudio callback thread — it only
  marshals the boolean onto the event loop via ``call_soon_threadsafe`` and
  returns instantly. An async drain task applies the edge logic on the loop
  and awaits the arbiter, never obstructing the audio callback or the FSM.

* **DRY (mandate #3).** No copy of any AudioBus/FullDuplexDevice queue or
  config — the bridge holds only its own tiny edge-latch + a marshal queue.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("Ouroboros.Karen.VADBridge")


class VADArbiterBridge:
    """Maps a VAD speech-active boolean stream onto the arbiter's barge-in
    entry points, coalescing redundant edges. NEVER raises out of ``feed`` /
    ``dispatch``."""

    def __init__(self, arbiter: object) -> None:
        self._arbiter = arbiter
        self._speaking = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional["asyncio.Queue[bool]"] = None
        self._task: Optional["asyncio.Task[None]"] = None

    # ---- edge logic (pure, testable) ---------------------------------

    def _edge(self, is_speech: bool) -> Optional[str]:
        """Return ``"start"`` on a genuine False->True edge, ``"end"`` on
        True->False, ``None`` for a redundant same-state signal. Pure state
        transition — no timing."""
        is_speech = bool(is_speech)
        if is_speech and not self._speaking:
            self._speaking = True
            return "start"
        if not is_speech and self._speaking:
            self._speaking = False
            return "end"
        return None

    async def dispatch(self, is_speech: bool) -> None:
        """Apply the edge and drive the arbiter. Fault-isolated — a raising
        arbiter never propagates (no deadlock in the audio loop)."""
        try:
            edge = self._edge(is_speech)
            if edge == "start":
                await self._arbiter.on_user_speech_start()
            elif edge == "end":
                await self._arbiter.on_user_speech_end()
        except Exception:  # noqa: BLE001
            logger.debug("[VADBridge] dispatch failed", exc_info=True)

    # ---- audio-thread ingress (mandate #2) ---------------------------

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Bind the event loop and start the async drain task. Call from an
        async context (or pass the loop explicitly)."""
        self._loop = loop or asyncio.get_event_loop()
        self._queue = asyncio.Queue()
        self._task = self._loop.create_task(self._drain())

    def feed(self, is_speech: bool) -> None:
        """SYNC, audio-thread-safe, non-blocking. Marshals the VAD signal onto
        the loop; never blocks the PortAudio callback, never raises."""
        try:
            loop, queue = self._loop, self._queue
            if loop is not None and queue is not None:
                loop.call_soon_threadsafe(queue.put_nowait, bool(is_speech))
        except Exception:  # noqa: BLE001
            logger.debug("[VADBridge] feed failed", exc_info=True)

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            try:
                is_speech = await self._queue.get()
            except asyncio.CancelledError:
                break
            await self.dispatch(is_speech)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None


__all__ = ["VADArbiterBridge"]

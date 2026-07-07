"""backend/core/ouroboros/governance/comms/duplex/karen_duplex_factory.py

`build_karen_duplex` — assembles the full-duplex Karen control layer
(playback + arbiter + VAD bridge) into one lifecycle-managed
:class:`KarenDuplexHandle`, so wiring it into the live audio bootstrap
(`backend/audio/audio_pipeline_bootstrap.wire_conversation_pipeline`) is a
single call rather than a scatter of construction across the boot path.

Sprint 3 Step 3. The handle exposes exactly the three seams the audio pipeline
needs:
  * ``on_vad(is_speech)`` — a SYNC, audio-thread-safe VAD consumer to register
    with ``AudioBus.register_mic_consumer`` (or the existing barge-in VAD
    consumer). Marshals through :class:`VADArbiterBridge`.
  * ``submit_speech(text, priority)`` — enqueue synthesized Karen speech (the
    ``VoiceNarrator``/``KarenSpeechSynthesizer`` path feeds this).
  * ``start()`` / ``stop()`` — schedule/tear down the arbiter run loop + bridge
    drain, cleanly (mandate #4: no leaked tasks, no frozen FSM).

No audio device / AEC / queue is constructed here — those live in
``backend/audio`` and are passed in via the TTS engine (mandate #3).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from .arbiter import VoiceDuplexArbiter
from .protocols import ArbiterConfig, Priority, SpeechRequest
from .tts_playback import ArbiterTTSPlayback
from .vad_bridge import VADArbiterBridge

logger = logging.getLogger("Ouroboros.Karen.Duplex")


@dataclass
class KarenDuplexHandle:
    arbiter: VoiceDuplexArbiter
    playback: ArbiterTTSPlayback
    bridge: VADArbiterBridge
    _run_task: Optional["asyncio.Task[None]"] = field(default=None, repr=False)

    async def start(self) -> None:
        """Schedule the arbiter run loop + start the VAD bridge drain. Idempotent."""
        if self._run_task is not None and not self._run_task.done():
            return
        loop = asyncio.get_event_loop()
        self._run_task = loop.create_task(self.arbiter.run())
        self.bridge.start(loop)

    async def stop(self) -> None:
        """Tear down cleanly — bridge, arbiter, run task. Never raises."""
        try:
            await self.bridge.stop()
        except Exception:  # noqa: BLE001
            logger.debug("[Duplex] bridge stop failed", exc_info=True)
        try:
            await self.arbiter.stop()
        except Exception:  # noqa: BLE001
            logger.debug("[Duplex] arbiter stop failed", exc_info=True)
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._run_task = None

    def on_vad(self, is_speech: bool) -> None:
        """SYNC VAD consumer hook (audio-thread safe). Register with the audio
        pipeline's mic/barge-in VAD signal."""
        self.bridge.feed(is_speech)

    def submit_speech(self, text: str, priority: Priority = Priority.PROACTIVE_INFO) -> None:
        """Enqueue a synthesized Karen line. Non-blocking; gated + coalesced by
        the arbiter."""
        try:
            self.arbiter.submit(SpeechRequest(text, priority))
        except Exception:  # noqa: BLE001
            logger.debug("[Duplex] submit_speech failed", exc_info=True)


def build_karen_duplex(
    tts_engine: object,
    *,
    config: Optional[ArbiterConfig] = None,
    source: str = "karen",
) -> KarenDuplexHandle:
    """Assemble the Karen full-duplex control layer over an existing
    ``UnifiedTTSEngine`` (or ``None`` for a silent handle). Pure construction —
    no side effects until :meth:`KarenDuplexHandle.start`."""
    cfg = config or ArbiterConfig.from_env()
    playback = ArbiterTTSPlayback(tts_engine, source=source)
    arbiter = VoiceDuplexArbiter(playback, config=cfg)
    bridge = VADArbiterBridge(arbiter)
    return KarenDuplexHandle(arbiter=arbiter, playback=playback, bridge=bridge)


__all__ = ["KarenDuplexHandle", "build_karen_duplex"]

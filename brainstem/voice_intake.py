"""Voice Intake — bridges STT output to CommandSender."""
import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger("jarvis.brainstem.voice")


class VoiceIntake:
    def __init__(self, on_transcript: Callable[[str], Coroutine[Any, Any, Any]]) -> None:
        self._on_transcript = on_transcript
        self._stt_engine: Any = None
        self._listening = False

    def set_stt_engine(self, engine: Any) -> None:
        self._stt_engine = engine

    async def run(self, shutdown: asyncio.Event) -> None:
        if self._stt_engine is None:
            logger.warning("[Voice] No STT engine — voice intake disabled")
            await shutdown.wait()
            return
        logger.info("[Voice] Voice intake started")
        self._listening = True
        try:
            while not shutdown.is_set():
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            self._listening = False
            logger.info("[Voice] Voice intake stopped")

    def on_transcription(self, text: str, is_final: bool = True) -> None:
        if not is_final or not text.strip():
            return
        logger.info("[Voice] Transcript: %s", text[:50])
        asyncio.create_task(self._on_transcript(text))

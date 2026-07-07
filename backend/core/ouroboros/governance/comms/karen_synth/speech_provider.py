from __future__ import annotations

import logging
from typing import AsyncIterator, Protocol, runtime_checkable

logger = logging.getLogger("Ouroboros.Karen.Synth")


@runtime_checkable
class SpeechProvider(Protocol):
    def source(self, *, system_prompt: str, user_prompt: str) -> AsyncIterator[str]: ...


class DWSpeechProvider:
    """DW-primary (Option A). Wraps DoublewordProvider.complete_sync (full
    payload) and exposes it as a one-chunk async source — the sentence chunker
    then splits it. Stream-ready by contract: a future StreamingSpeechProvider
    yields many chunks from the same `source()` shape. Never raises."""

    def __init__(self, dw_provider: object, *, max_tokens: int = 80) -> None:
        self._dw = dw_provider
        self._max_tokens = max_tokens

    async def source(self, *, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        try:
            res = await self._dw.complete_sync(
                user_prompt,
                system_prompt=system_prompt,
                caller_id="karen_synth",
                max_tokens=self._max_tokens,
            )
            content = getattr(res, "content", "") or ""
            if content.strip():
                yield content
        except Exception:  # noqa: BLE001
            logger.debug("[KarenSynth] DW completion failed", exc_info=True)
            return

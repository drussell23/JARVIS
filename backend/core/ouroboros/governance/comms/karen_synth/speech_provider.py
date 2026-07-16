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
        # Phase 2 RT repositioning (2026-07-16): a HUMAN is waiting on speech —
        # the purest buy-time call in the codebase. Routes through the unified
        # Claude-RT-first gate router (this provider's DW handle stays as the
        # opportunistic fallback). Contract unchanged: one chunk, never raises.
        try:
            from backend.core.ouroboros.governance.rt_gate import gate_completion

            content = await gate_completion(
                user_prompt,
                caller_id="karen_synth",
                system_prompt=system_prompt,
                max_tokens=self._max_tokens,
                dw_provider=self._dw,
            )
            if content.strip():
                yield content
        except Exception:  # noqa: BLE001 — incl. GateProviderExhaustedError
            logger.debug("[KarenSynth] RT completion failed", exc_info=True)
            return

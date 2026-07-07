# backend/core/ouroboros/governance/comms/karen_synth/synthesizer.py
from __future__ import annotations

import logging
from typing import AsyncIterator, Callable, Mapping, Optional

from .ledger_view import LedgerView
from .persona import build_prompt
from .sentence_chunker import stream_sentences
from .speech_provider import SpeechProvider

logger = logging.getLogger("Ouroboros.Karen.Synth")


class KarenSpeechSynthesizer:
    """OperationContext ledger → persona prompt → provider → sentences.
    Provider-agnostic (DW payload today, token stream later). Never raises —
    on any failure it yields nothing and the arbiter stays silent."""

    def __init__(
        self,
        provider: SpeechProvider,
        *,
        persona_ctx_fn: Optional[Callable[[], Mapping]] = None,
    ) -> None:
        self._provider = provider
        self._persona_ctx_fn = persona_ctx_fn

    async def synthesize(self, view: LedgerView) -> AsyncIterator[str]:
        try:
            ctx = self._persona_ctx_fn() if self._persona_ctx_fn else None
        except Exception:  # noqa: BLE001
            ctx = None
        try:
            system, user = build_prompt(view, ctx)
            source = self._provider.source(system_prompt=system, user_prompt=user)
            async for sentence in stream_sentences(source):
                if sentence.strip():
                    yield sentence
        except Exception:  # noqa: BLE001
            logger.debug("[KarenSynth] synthesize failed", exc_info=True)
            return

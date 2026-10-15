"""
speech_segments - JARVIS speaks a reply while it is still being written.

A reply was spoken only once it was whole: the model wrote all of it, then
all of it was rendered to audio, then it played -- about six seconds of
silence after "do I have any meetings tomorrow?" (2026-10-14: 1.0 s to
write, 2.1 s to render, 1.1 s of that fixed per render). Spoken a sentence
at a time, the first sentence plays while the second is written and
rendered.

* ``SentenceSplitter`` turns a stream of model tokens into sentences (it
  came from the audio pipeline, ``backend/audio/conversation_pipeline``,
  which re-exports it); ``sentences`` splits finished text by the same rule.
* ``voice_while_writing`` consumes a token stream: when the command's voice
  is a client (backend.core.command_identity), each finished sentence is
  said as soon as it is written, and the text voiced is recorded on the
  command, so its end does not say the reply again. The host's own speakers
  take the reply whole, as before -- their announcement queue has several
  workers and would not keep a reply's sentences in order.
"""
from __future__ import annotations

import logging
import os
import re
from typing import AsyncIterator, List

logger = logging.getLogger(__name__)

# A sentence ends at ., ! or ? followed by whitespace.
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')


def _min_sentence_chars() -> int:
    """Shortest piece said on its own ("8 a.m." must not be one); a typo is the default."""
    try:
        return max(1, int(os.getenv("JARVIS_SPEECH_MIN_SENTENCE_CHARS", "") or 10))
    except ValueError:
        return 10


class SentenceSplitter:
    """
    Accumulates LLM tokens until a sentence boundary, then yields complete
    sentences for TTS. This lets the user hear the first word at ~300-500ms
    instead of waiting for the full LLM response.
    """

    def __init__(self, min_sentence_len: int = 0):
        self._min_len = min_sentence_len or _min_sentence_chars()

    async def split(
        self, token_stream: AsyncIterator[str]
    ) -> AsyncIterator[str]:
        """
        Consume an async stream of tokens and yield complete sentences.
        Handles cases where sentence-ending punctuation arrives without
        trailing whitespace (common with token-by-token LLM streaming).
        """
        buffer = ""

        async for token in token_stream:
            buffer += token

            # Check for sentence boundaries
            while True:
                match = _SENTENCE_END.search(buffer)
                if match and match.start() >= self._min_len:
                    sentence = buffer[:match.end()].strip()
                    buffer = buffer[match.end():]
                    if sentence:
                        yield sentence
                else:
                    # Check for punctuation at end of buffer followed by
                    # trailing whitespace (the regex doesn't match because
                    # there's no subsequent text yet).
                    stripped = buffer.rstrip()
                    if (
                        len(stripped) >= self._min_len
                        and stripped
                        and stripped[-1] in '.!?'
                        and len(buffer) != len(stripped)
                    ):
                        buffer = ""
                        yield stripped
                    break

        # Flush remaining buffer
        buffer = buffer.strip()
        if buffer:
            yield buffer


def sentences(text: str, min_sentence_len: int = 0) -> List[str]:
    """*text* as the sentences SentenceSplitter would yield, in order."""
    limit = min_sentence_len or _min_sentence_chars()
    out: List[str] = []
    rest = (text or "").strip()
    while rest:
        cut = next((m for m in _SENTENCE_END.finditer(rest) if m.start() >= limit), None)
        if cut is None:
            out.append(rest)
            break
        out.append(rest[:cut.start()].strip())
        rest = rest[cut.end():].strip()
    return [s for s in out if s]


async def voice_while_writing(chunks: AsyncIterator[str], *, source: str) -> str:
    """The whole text of *chunks*; on the way, each finished sentence is said
    to the command's client voice and recorded as voiced. Without a client
    voice the text is only collected (the end of the command says it)."""
    from backend.core.command_identity import command_voice, note_voiced

    written: List[str] = []

    async def tee() -> AsyncIterator[str]:
        async for chunk in chunks:
            if chunk:
                written.append(chunk)
                yield chunk

    if command_voice.get() is None:
        async for _ in tee():
            pass
        return "".join(written)

    from backend.core.supervisor.unified_voice_orchestrator import safe_say, spoken_form

    async for sentence in SentenceSplitter().split(tee()):
        # An answer the user asked for is never a repeat to suppress.
        if await safe_say(spoken_form(sentence), source=source, skip_dedup=True):
            note_voiced(sentence)
        else:
            logger.info("[Speech] a written sentence was not voiced (%s): %s", source, sentence[:60])
    return "".join(written)


__all__ = ["SentenceSplitter", "sentences", "voice_while_writing"]

# backend/core/ouroboros/governance/comms/karen_synth/sentence_chunker.py
from __future__ import annotations

import re
from typing import AsyncIterable, AsyncIterator

_BOUNDARY = re.compile(r"(.+?[.!?])(\s+|$)", re.DOTALL)


async def single_payload(text: str) -> AsyncIterator[str]:
    """Adapt a full-payload string (DW) into a one-shot async source."""
    yield text


async def stream_sentences(source: AsyncIterable[str]) -> AsyncIterator[str]:
    """Accumulate incoming text chunks and yield complete sentences as their
    boundaries arrive. Works for a token stream (Claude) or a single payload
    (DW). Flushes any trailing non-terminated remainder at the end."""
    buf = ""
    async for chunk in source:
        buf += chunk or ""
        while True:
            m = _BOUNDARY.match(buf)
            if not m:
                break
            sentence = m.group(1).strip()
            buf = buf[m.end():]
            if sentence:
                yield sentence
    tail = buf.strip()
    if tail:
        yield tail

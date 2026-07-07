# tests/governance/comms/karen_synth/test_sentence_chunker.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.karen_synth.sentence_chunker import (
    stream_sentences, single_payload,
)

async def _tokens(chunks):
    for c in chunks:
        yield c

@pytest.mark.asyncio
async def test_token_stream_emits_complete_sentences():
    src = _tokens(["Fix ", "applied", ". Tests ", "green", "!"])
    got = [s async for s in stream_sentences(src)]
    assert got == ["Fix applied.", "Tests green!"]

@pytest.mark.asyncio
async def test_single_payload_splits_into_sentences():
    got = [s async for s in stream_sentences(single_payload("One thing. Two things?"))]
    assert got == ["One thing.", "Two things?"]

@pytest.mark.asyncio
async def test_trailing_text_without_terminator_is_flushed():
    got = [s async for s in stream_sentences(_tokens(["no period here"]))]
    assert got == ["no period here"]

from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.karen_synth.ledger_view import LedgerView
from backend.core.ouroboros.governance.comms.karen_synth.synthesizer import KarenSpeechSynthesizer

class _FakeProvider:
    def __init__(self, text): self.text, self.seen = text, {}
    async def source(self, *, system_prompt, user_prompt):
        self.seen = {"system": system_prompt, "user": user_prompt}
        yield self.text

@pytest.mark.asyncio
async def test_synthesize_yields_sentences_from_ledger():
    prov = _FakeProvider("Fix applied. Tests green.")
    synth = KarenSpeechSynthesizer(prov, persona_ctx_fn=lambda: {"user_name": "Derek"})
    view = LedgerView.from_payload("decision", {"outcome": "applied", "target_files": ["a/x.py"]})
    out = [s async for s in synth.synthesize(view)]
    assert out == ["Fix applied.", "Tests green."]
    assert "Derek" in prov.seen["system"]        # persona injected
    assert "x.py" in prov.seen["user"]           # ledger injected

@pytest.mark.asyncio
async def test_synthesize_never_speaks_code():
    prov = _FakeProvider("It broke. Rolled back.")
    synth = KarenSpeechSynthesizer(prov)
    view = LedgerView.from_payload("postmortem", {"root_cause": "x ```raw code``` y"})
    out = [s async for s in synth.synthesize(view)]
    assert all("```" not in s for s in out)

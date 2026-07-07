from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.karen_synth.speech_provider import DWSpeechProvider

class _Res:
    def __init__(self, content): self.content = content

class _FakeDW:
    def __init__(self, content="Fix applied. Tests green.", boom=False):
        self.content, self.boom, self.calls = content, boom, []
    async def complete_sync(self, prompt, *, system_prompt, caller_id, max_tokens=512, **kw):
        self.calls.append((prompt, system_prompt, max_tokens))
        if self.boom: raise RuntimeError("dw down")
        return _Res(self.content)

@pytest.mark.asyncio
async def test_dw_provider_yields_completion_text():
    dw = _FakeDW()
    sp = DWSpeechProvider(dw, max_tokens=80)
    out = [c async for c in sp.source(system_prompt="sys", user_prompt="usr")]
    assert out == ["Fix applied. Tests green."]
    assert dw.calls[0][1] == "sys" and dw.calls[0][2] == 80   # system prompt + cap forwarded

@pytest.mark.asyncio
async def test_dw_provider_failure_yields_nothing():
    sp = DWSpeechProvider(_FakeDW(boom=True))
    assert [c async for c in sp.source(system_prompt="s", user_prompt="u")] == []

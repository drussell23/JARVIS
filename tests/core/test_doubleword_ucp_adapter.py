"""DoubleWord UCP Adapter + Adaptive Context Transformer (Phase 12, Slice A)."""
from __future__ import annotations

import pytest

from backend.core import doubleword_ucp_adapter as dwa
from backend.core import active_failover as af


class _AnthropicError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message); self.status_code = status_code


class _CompleteSyncResult:
    def __init__(self, content): self.content = content


class _FakeDW:
    """Mirrors DoublewordProvider.complete_sync — captures what it got."""
    def __init__(self, answer="Good to see you, Sir."):
        self.answer = answer; self.received_prompt = None; self.received_system = None
        self.received_caller = None
    async def complete_sync(self, prompt, *, system_prompt, caller_id,
                            max_tokens=512, timeout_s=20.0, **kw):
        self.received_prompt = prompt
        self.received_system = system_prompt
        self.received_caller = caller_id
        return _CompleteSyncResult(self.answer)


# ---------------------------------------------------------------------------
# Adaptive Context Transformer
# ---------------------------------------------------------------------------

def test_transformer_strips_claude_reasoning_blocks():
    t = dwa.AdaptiveContextTransformer()
    out = t.transform("<thinking>I should reason about this</thinking>\n"
                      "hello JARVIS")
    assert "<thinking>" not in out.prompt
    assert "reason about" not in out.prompt        # reasoning block DROPPED
    assert "hello JARVIS" in out.prompt            # semantic core KEPT


def test_transformer_keeps_inner_text_of_instruction_tags():
    t = dwa.AdaptiveContextTransformer()
    out = t.transform("<instruction>open Safari</instruction>")
    assert "<instruction>" not in out.prompt       # tag stripped
    assert "open Safari" in out.prompt             # inner text kept


def test_transformer_strips_markdown_fences_keeps_code():
    t = dwa.AdaptiveContextTransformer()
    out = t.transform("```python\nprint(1)\n```")
    assert "```" not in out.prompt
    assert "print(1)" in out.prompt


def test_transformer_noop_on_plain_text():
    t = dwa.AdaptiveContextTransformer()
    assert t.transform("hello JARVIS").prompt == "hello JARVIS"


# ---------------------------------------------------------------------------
# Adapter reuses complete_sync (DRY) with the right caller_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adapter_calls_complete_sync_and_returns_content():
    dw = _FakeDW()
    adapter = dwa.DoubleWordUCPAdapter(provider=dw)
    text = await adapter.complete({"text": "hello JARVIS", "system_prompt": "You are JARVIS."})
    assert text == "Good to see you, Sir."
    assert dw.received_caller == "ucp_command_failover"   # leases scoped, not raw
    assert dw.received_system == "You are JARVIS."


# ---------------------------------------------------------------------------
# MANDATE 4 — live-shape integration: Anthropic credit-400 → transform →
# pivot to DoubleWordUCPAdapter → valid CommandResponse
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_credit400_failover_transforms_and_pivots_to_doubleword():
    dw = _FakeDW(answer="Good to see you, Sir.")
    adapter = dwa.DoubleWordUCPAdapter(provider=dw)

    async def claude(ctx):
        raise _AnthropicError(400, "Error code: 400 - Your credit balance is "
                                   "too low to access the Anthropic API.")

    # A Claude-dialect context (reasoning scaffolding + the real request).
    context = {
        "text": "<thinking>The user greeted me.</thinking>\nhello JARVIS",
        "system_prompt": "<role>You are JARVIS.</role>",
    }
    providers = dwa.build_ucp_failover_providers(claude, dw_adapter=adapter)
    result = await af.generate_with_failover(context, providers)

    # Router pivoted cleanly to DoubleWord.
    assert result.ok is True
    assert result.provider == "doubleword"
    assert result.failed_over is True
    assert result.text == "Good to see you, Sir."       # valid response
    # Anti-Drift Guard: DoubleWord received the SANITIZED payload.
    assert "<thinking>" not in dw.received_prompt
    assert "The user greeted me" not in dw.received_prompt   # reasoning dropped
    assert "hello JARVIS" in dw.received_prompt              # core preserved
    assert "<role>" not in dw.received_system
    assert "You are JARVIS." in dw.received_system


@pytest.mark.asyncio
async def test_build_chain_order_claude_then_doubleword():
    async def claude(ctx): return "primary"
    providers = dwa.build_ucp_failover_providers(claude, dw_adapter=dwa.DoubleWordUCPAdapter(provider=_FakeDW()))
    assert [p.name for p in providers] == ["claude", "doubleword"]

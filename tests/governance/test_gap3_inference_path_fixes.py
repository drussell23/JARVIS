"""Gap 3 inference-path API-contract fixes (re-soak bt-2026-07-16-095428).

The organic DreamEngine conception loop reached inference but failed on every
provider tier:
  A. DW hard-rejected the deprecated top-level ``chat_template_kwargs.enable_thinking``
     with a 400 ("use 'reasoning_effort'").
  B. the DreamEngine's Claude fallback called ``ClaudeProvider.prompt_only`` — a
     method that did not exist (ClaudeProvider's real API is context-based
     ``generate``, which produces code-candidates, not free-form text).

These pin both API contracts so they cannot silently drift again.
"""
from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# Bug A — DW reasoning params never send the deprecated enable_thinking flag
# ---------------------------------------------------------------------------

import backend.core.ouroboros.governance.doubleword_provider as dwp


def test_reasoning_params_never_send_top_level_enable_thinking(monkeypatch):
    """The deprecated top-level chat_template_kwargs (DW 400) is gone; the
    compliant reasoning_effort is always present."""
    # Force the eff=="none" branch with no catalog-confirmed extra_body.
    monkeypatch.setattr(dwp, "_dw_model_min_effort", lambda model: "none")
    monkeypatch.setattr(dwp, "_dw_thinking_extra_body", lambda model="": {})
    p = dwp._reasoning_request_params(effort="none", model="some/model")
    assert p.get("reasoning_effort") == "none"
    assert "chat_template_kwargs" not in p          # the deprecated 400-trigger
    assert "extra_body" not in p                    # unconfirmed → effort alone


def test_reasoning_params_use_extra_body_when_catalog_confirms(monkeypatch):
    """When the catalog confirms reasoning control, the COMPLIANT nested
    extra_body form is used (never the top-level flag)."""
    monkeypatch.setattr(dwp, "_dw_model_min_effort", lambda model: "none")
    monkeypatch.setattr(
        dwp, "_dw_thinking_extra_body",
        lambda model="": {"chat_template_kwargs": {"enable_thinking": False}},
    )
    p = dwp._reasoning_request_params(effort="none", model="reasoning/model")
    assert p.get("reasoning_effort") == "none"
    assert "extra_body" in p                         # nested, compliant
    assert "chat_template_kwargs" not in p           # never top-level


def test_reasoning_params_nonzero_effort_has_no_thinking_keys(monkeypatch):
    monkeypatch.setattr(dwp, "_dw_model_min_effort", lambda model: "none")
    p = dwp._reasoning_request_params(effort="low", model="m")
    assert p["reasoning_effort"] == "low"
    assert "chat_template_kwargs" not in p and "extra_body" not in p


# ---------------------------------------------------------------------------
# Bug B — ClaudeProvider.prompt_only (raw text, symmetric with DW)
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.providers import ClaudeProvider


class _Blk:
    def __init__(self, type_, text):
        self.type = type_
        self.text = text


class _Msg:
    def __init__(self, blocks):
        self.content = blocks


def _bare_claude():
    inst = ClaudeProvider.__new__(ClaudeProvider)   # skip live-client __init__
    inst._model = "claude-sonnet-4-6"
    inst._max_tokens = 2000
    return inst


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_prompt_only_exists_and_is_async():
    assert hasattr(ClaudeProvider, "prompt_only")
    assert asyncio.iscoroutinefunction(ClaudeProvider.prompt_only)


def test_prompt_only_composes_clean_create_kwargs():
    inst = _bare_claude()
    seen = {}

    async def _fake(**kw):
        seen.update(kw)
        return _Msg([_Blk("text", "hello")])

    inst._claude_create_with_resilience = _fake
    out = _run(inst.prompt_only(
        "dream prompt", model="claude-x", max_tokens=1500,
        response_format={"type": "json_object"},
    ))
    ck = seen["create_kwargs"]
    assert out == "hello"
    assert ck["model"] == "claude-x" and ck["max_tokens"] == 1500
    assert ck["messages"][0] == {"role": "user", "content": "dream prompt"}
    assert "system" in ck
    # response_format is inapplicable to Claude and must NOT leak into the body,
    # nor may any deprecated DW-style key.
    assert "response_format" not in ck and "chat_template_kwargs" not in ck


def test_prompt_only_skips_thinking_blocks():
    inst = _bare_claude()

    async def _fake(**kw):
        return _Msg([_Blk("thinking", "internal reasoning"), _Blk("text", "ANSWER")])

    inst._claude_create_with_resilience = _fake
    assert _run(inst.prompt_only("p")) == "ANSWER"


def test_prompt_only_falls_back_to_first_block_text():
    inst = _bare_claude()

    async def _fake(**kw):
        return _Msg([_Blk("other", "only block")])   # no 'text'-typed block

    inst._claude_create_with_resilience = _fake
    assert _run(inst.prompt_only("p")) == "only block"


def test_prompt_only_empty_content_returns_empty_string():
    inst = _bare_claude()

    async def _fake(**kw):
        return _Msg([])

    inst._claude_create_with_resilience = _fake
    assert _run(inst.prompt_only("p")) == ""


def test_prompt_only_propagates_errors_no_swallow():
    """Mandate 1: no catch-all swallow — a create error propagates to the
    caller's tier-fallback logic."""
    inst = _bare_claude()

    async def _boom(**kw):
        raise RuntimeError("anthropic 529 overloaded")

    inst._claude_create_with_resilience = _boom
    with pytest.raises(RuntimeError, match="overloaded"):
        _run(inst.prompt_only("p"))

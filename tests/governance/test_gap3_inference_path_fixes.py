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


# ---------------------------------------------------------------------------
# complete_sync Aegis credential gate (Slice-27 parity, bt-2026-07-16-185947)
#
# Under Aegis env_scrub, DOUBLEWORD_API_KEY is absent from os.environ and
# self._api_key is empty — but Aegis injects the real key server-side at the
# daemon's forwarding handler (complete_sync's transport is already
# Aegis-bridged). The gate must accept EITHER credential source, exactly like
# prompt_only post-Slice-27.
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock

import backend.core.ouroboros.aegis.client as aegis_client
from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider


class _GatePassed(Exception):
    """Sentinel raised by the mocked budget check — proves the credential
    gate ADMITTED the call (budget is the very next statement)."""


def _scrubbed_provider(monkeypatch):
    """A provider skeleton in an Aegis-scrubbed world: no env key, empty
    _api_key. No live HTTP is possible — _check_budget raises the sentinel."""
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    p = DoublewordProvider.__new__(DoublewordProvider)
    p._api_key = ""                       # post-scrub state
    p._model = "some/model"
    p._check_budget = MagicMock(side_effect=_GatePassed())
    return p


def test_complete_sync_admits_when_aegis_active(monkeypatch):
    """Scrubbed env + Aegis broker active → the gate passes and execution
    reaches payload construction (sentinel fires)."""
    monkeypatch.setattr(aegis_client, "is_enabled", lambda: True)
    p = _scrubbed_provider(monkeypatch)
    with pytest.raises(_GatePassed):
        asyncio.new_event_loop().run_until_complete(
            p.complete_sync("hi", system_prompt="s", caller_id="test")
        )


def test_complete_sync_rejects_when_no_credential_source(monkeypatch):
    """Scrubbed env + Aegis OFF → fail-closed ValueError (no silent no-auth
    call, no hardcoded fallback)."""
    monkeypatch.setattr(aegis_client, "is_enabled", lambda: False)
    p = _scrubbed_provider(monkeypatch)
    with pytest.raises(ValueError, match="Aegis is not"):
        asyncio.new_event_loop().run_until_complete(
            p.complete_sync("hi", system_prompt="s", caller_id="test")
        )


def test_complete_sync_admits_with_direct_key_aegis_off(monkeypatch):
    """Legacy path unchanged: a directly-configured key admits without Aegis."""
    monkeypatch.setattr(aegis_client, "is_enabled", lambda: False)
    p = _scrubbed_provider(monkeypatch)
    p._api_key = "dw-legacy-key"
    with pytest.raises(_GatePassed):
        asyncio.new_event_loop().run_until_complete(
            p.complete_sync("hi", system_prompt="s", caller_id="test")
        )


def test_complete_sync_gate_defensive_on_aegis_probe_error(monkeypatch):
    """An erroring Aegis probe degrades to 'not active' (defensive), so
    scrubbed + broken-probe still fail-closes rather than crashing oddly."""
    def _boom():
        raise RuntimeError("aegis probe failed")
    monkeypatch.setattr(aegis_client, "is_enabled", _boom)
    p = _scrubbed_provider(monkeypatch)
    with pytest.raises(ValueError, match="cannot call complete_sync"):
        asyncio.new_event_loop().run_until_complete(
            p.complete_sync("hi", system_prompt="s", caller_id="test")
        )


# ---------------------------------------------------------------------------
# complete_sync payload schema — DRY vs the deprecated twin
#
# bt-2026-07-16-200920: the DreamEngine's DW-RT tier 400'd on EVERY call —
# "Unsupported parameter 'chat_template_kwargs.enable_thinking'; use
# 'reasoning_effort'" — because complete_sync hand-rolled its own payload and
# never composed _reasoning_request_params (the helper the earlier contract fix
# corrected for prompt_only). The duplication WAS the defect. These pin the
# wire schema so the twin cannot regrow.
# ---------------------------------------------------------------------------


class _CapturedBody(BaseException):
    """Carries the composed request body out of complete_sync before any HTTP.

    Subclasses BaseException deliberately: the egress block inside
    complete_sync wraps sanitation in ``except Exception: pass`` (fail-soft by
    design), which would swallow a normal Exception and let the call proceed to
    transport."""
    def __init__(self, body):
        self.body = body


def _payload_provider(monkeypatch, *, model="some/model"):
    """A REAL DoublewordProvider (not a skeleton — the fake must mirror the
    real contract) whose composed body is captured at the egress boundary
    before any transport. Legacy direct-key credential path (Aegis off) —
    the gate is orthogonal to payload composition."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(aegis_client, "is_enabled", lambda: False)
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "dw-test-key")
    p = DoublewordProvider()                     # real __init__, real defaults
    p._api_key = "dw-test-key"                   # satisfies the legacy gate
    p._model = model
    p._check_budget = MagicMock()
    p._get_session = AsyncMock(return_value=MagicMock())   # no real transport

    # Capture exactly where the body is finished: egress sanitation runs on the
    # composed dict, so intercept there and abort before the request leaves.
    import backend.core.ouroboros.governance.doubleword_provider as dwp_mod
    monkeypatch.setattr(dwp_mod, "egress_interceptor_enabled", lambda: True)

    def _capture(body, model_id):
        raise _CapturedBody(body)

    monkeypatch.setattr(dwp_mod, "sanitize_egress_body", _capture)
    return p


def _compose(monkeypatch, **kw):
    p = _payload_provider(monkeypatch, model=kw.pop("model", "some/model"))
    try:
        asyncio.new_event_loop().run_until_complete(
            p.complete_sync("hi", system_prompt="s", caller_id="test", **kw)
        )
    except _CapturedBody as c:
        return c.body
    raise AssertionError("body was never composed/captured")


def test_complete_sync_payload_has_reasoning_effort_never_deprecated_key(monkeypatch):
    """THE pin: compliant reasoning_effort present, deprecated top-level key
    absolutely absent (the 400 trigger)."""
    body = _compose(monkeypatch)
    assert "reasoning_effort" in body
    assert "chat_template_kwargs" not in body     # the DW 400 trigger
    assert "extra_body" not in body               # SDK-only kwarg, never raw


def test_complete_sync_legacy_none_suppresses_reasoning(monkeypatch):
    """enable_thinking=None (legacy Functions callers) → effort 'none'."""
    monkeypatch.setattr(dwp, "_dw_model_min_effort", lambda model: "none")
    body = _compose(monkeypatch, enable_thinking=None)
    assert body["reasoning_effort"] == "none"
    assert "chat_template_kwargs" not in body


def test_complete_sync_explicit_false_suppresses_reasoning(monkeypatch):
    monkeypatch.setattr(dwp, "_dw_model_min_effort", lambda model: "none")
    body = _compose(monkeypatch, enable_thinking=False)
    assert body["reasoning_effort"] == "none"


def test_complete_sync_true_unlocks_reasoning_not_pinned_none(monkeypatch):
    """enable_thinking=True (heavy lane) → effort DERIVES, never forced 'none'."""
    monkeypatch.setattr(dwp, "_dw_model_min_effort", lambda model: "none")
    monkeypatch.setattr(dwp, "_reasoning_effort_for", lambda c, model="": "medium")
    body = _compose(monkeypatch, enable_thinking=True)
    assert body["reasoning_effort"] == "medium"
    assert "chat_template_kwargs" not in body


def test_complete_sync_honors_per_model_effort_floor(monkeypatch):
    """DRY payoff: the Slice-168 per-model floor now applies to complete_sync
    for free, because it composes the shared helper."""
    monkeypatch.setattr(dwp, "_dw_model_min_effort", lambda model: "low")
    body = _compose(monkeypatch, enable_thinking=False)   # asks for "none"
    assert body["reasoning_effort"] == "low"              # clamped up by the floor


def test_complete_sync_composes_the_single_source_of_truth(monkeypatch):
    """Structural: complete_sync must call _reasoning_request_params, not
    hand-roll a payload (the duplication that caused the outage)."""
    import inspect
    src = inspect.getsource(DoublewordProvider.complete_sync)
    assert "_reasoning_request_params(" in src
    # no hand-rolled deprecated construction anywhere in the method body
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert '"chat_template_kwargs"' not in code

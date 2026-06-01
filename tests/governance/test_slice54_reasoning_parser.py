"""Slice 54 — native Chain-of-Thought handling for DoubleWord Qwen3.5.

Verified (2026-06-01, out-of-band probes): Qwen3.5 are reasoning models that
emit chain-of-thought into `message.reasoning` (+ `reasoning_details`) and the
answer into `message.content` only after reasoning. Probe matrix:

    enable_thinking=False  -> IGNORED by DW (still 62 reasoning tokens, empty content)
    reasoning_effort=none  -> WORKS (finish=stop, content='OK', 0 reasoning tokens)
    max_tokens=2000        -> content appears once reasoning fits

So the unlock is the OpenAI-standard `reasoning_effort` param (not the
DW-ignored `chat_template_kwargs.enable_thinking`), plus reasoning-aware
parsing (the old batch fallback used the WRONG field name `reasoning_content`).
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.doubleword_provider import (
    _reasoning_request_params,
    _extract_completion_text,
)


def test_default_effort_is_none_and_carries_working_param(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_REASONING_EFFORT", raising=False)
    p = _reasoning_request_params()
    assert p["reasoning_effort"] == "none", "default must be the proven-working suppression"
    # belt-and-braces template flag retained (harmless; DW ignores it)
    assert p.get("chat_template_kwargs") == {"enable_thinking": False}


def test_effort_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_REASONING_EFFORT", "medium")
    p = _reasoning_request_params()
    assert p["reasoning_effort"] == "medium"
    # reasoning intentionally enabled -> do NOT also suppress via the template flag
    assert "chat_template_kwargs" not in p


def test_explicit_effort_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_REASONING_EFFORT", "high")
    assert _reasoning_request_params(effort="none")["reasoning_effort"] == "none"


def test_extract_prefers_content_when_present():
    msg = {"role": "assistant", "content": "the answer", "reasoning": "i thought hard"}
    assert _extract_completion_text(msg) == "the answer"


def test_extract_falls_back_to_reasoning_field_not_reasoning_content():
    # The actual DW field is `reasoning` — the OLD code wrongly used
    # `reasoning_content` and so always read empty.
    msg = {"role": "assistant", "content": None, "reasoning": "chain of thought"}
    assert _extract_completion_text(msg) == "chain of thought"


def test_extract_falls_back_to_reasoning_details_text():
    msg = {
        "role": "assistant",
        "content": "",
        "reasoning_details": [{"format": "unknown", "index": 0, "text": "detail A"},
                              {"text": "detail B"}],
    }
    out = _extract_completion_text(msg)
    assert "detail A" in out and "detail B" in out


def test_extract_empty_message_is_empty_string():
    assert _extract_completion_text({}) == ""
    assert _extract_completion_text({"role": "assistant", "content": None}) == ""
    assert _extract_completion_text(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Wiring pins — the reasoning_effort param must reach the real request bodies,
# and the streaming loop must treat reasoning deltas as liveness (per Slice 45).
# ---------------------------------------------------------------------------


def test_reasoning_param_wired_into_request_bodies():
    import inspect

    import backend.core.ouroboros.governance.doubleword_provider as dw

    src = inspect.getsource(dw)
    # The working param helper must be spread into the request bodies, and the
    # ineffective bare enable_thinking literal must no longer be the sole knob.
    assert "_reasoning_request_params(" in src
    assert src.count("**_reasoning_request_params()") >= 3, (
        "reasoning params must be wired into the batch + streaming + sync bodies"
    )


def test_streaming_treats_reasoning_delta_as_liveness():
    import inspect

    import backend.core.ouroboros.governance.doubleword_provider as dw

    src = inspect.getsource(dw)
    # The streaming loop must read delta.reasoning (liveness), not only content.
    assert 'delta.get("reasoning"' in src or "delta.get('reasoning'" in src, (
        "streaming loop must observe reasoning deltas as liveness"
    )

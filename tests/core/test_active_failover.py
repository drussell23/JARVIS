"""Provider-Agnostic Active Failover spine (Phase 12).

Mandate 4: mock an Anthropic 402/insufficient-quota during command
execution; assert the failover router catches the specific fault, pivots
to the DoubleWord mock, and returns a valid response to the caller.
"""
from __future__ import annotations

import pytest

from backend.core import active_failover as af


# --- realistic SDK error fakes (mirror the real shapes) ---

class _AnthropicError(Exception):
    """Mirrors anthropic.BadRequestError: a status_code + a body message.
    Anthropic's insufficient-credit ships as HTTP 400 with 'credit balance
    is too low' — the exact shape from the live log."""
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_credit_balance_400_is_retriable():
    exc = _AnthropicError(400, "Error code: 400 - Your credit balance is too "
                               "low to access the Anthropic API.")
    assert af.is_retriable_provider_error(exc) is True     # the live bug


def test_402_429_5xx_are_retriable():
    for code in (402, 429, 500, 502, 503, 504, 529):
        assert af.is_retriable_provider_error(_AnthropicError(code, "x")) is True


def test_plain_400_is_not_retriable():
    exc = _AnthropicError(400, "invalid request: bad field 'foo'")
    assert af.is_retriable_provider_error(exc) is False    # real client bug — don't mask


def test_rate_limit_message_without_code_is_retriable():
    assert af.is_retriable_provider_error(Exception("429 rate limit exceeded")) is True


def test_extract_status_from_response_attr():
    class _R: status_code = 503
    class _E(Exception): response = _R()
    assert af.extract_status_code(_E("down")) == 503


# ---------------------------------------------------------------------------
# MANDATE 4 — the active failover: Anthropic 402/credit → pivot to DoubleWord
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anthropic_credit_error_pivots_to_doubleword():
    calls = []

    async def anthropic(ctx):
        calls.append("claude")
        raise _AnthropicError(400, "Your credit balance is too low to access "
                                   "the Anthropic API.")

    async def doubleword(ctx):
        calls.append("doubleword")
        # DoubleWord gets the SAME context payload.
        assert ctx["text"] == "hello JARVIS"
        return "Good to see you, Sir."

    result = await af.generate_with_failover(
        {"text": "hello JARVIS"},
        [af.Provider("claude", anthropic), af.Provider("doubleword", doubleword)],
    )

    assert result.ok is True
    assert result.provider == "doubleword"                 # seamless pivot
    assert result.text == "Good to see you, Sir."          # valid response
    assert result.failed_over is True
    assert calls == ["claude", "doubleword"]               # tried claude first, then DW


@pytest.mark.asyncio
async def test_primary_success_no_failover():
    async def claude(ctx): return "primary answer"
    async def dw(ctx): raise AssertionError("must not be called")
    result = await af.generate_with_failover(
        {}, [af.Provider("claude", claude), af.Provider("doubleword", dw)])
    assert result.ok and result.provider == "claude"
    assert result.failed_over is False


@pytest.mark.asyncio
async def test_all_providers_fail_returns_failed_result_not_raise():
    async def a(ctx): raise _AnthropicError(402, "payment required")
    async def b(ctx): raise _AnthropicError(429, "rate limit")
    result = await af.generate_with_failover(
        {}, [af.Provider("a", a), af.Provider("b", b)])
    assert result.ok is False                              # never raises
    assert result.attempts == ["a", "b"]
    assert result.error


@pytest.mark.asyncio
async def test_on_failover_callback_fires_for_retriable():
    events = []
    async def a(ctx): raise _AnthropicError(429, "rate limit")
    async def b(ctx): return "ok"
    await af.generate_with_failover(
        {}, [af.Provider("a", a), af.Provider("b", b)],
        on_failover=lambda name, exc: events.append(name))
    assert events == ["a"]


@pytest.mark.asyncio
async def test_empty_response_advances_to_next_provider():
    async def a(ctx): return ""            # empty → not a valid answer
    async def b(ctx): return "real"
    result = await af.generate_with_failover(
        {}, [af.Provider("a", a), af.Provider("b", b)])
    assert result.ok and result.provider == "b"

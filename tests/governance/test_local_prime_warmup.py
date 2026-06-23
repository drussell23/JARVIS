# tests/governance/test_local_prime_warmup.py
"""TDD tests for LocalPrimeClient.warmup() (VRAM pre-warm -- Phase 3b+).

These tests exercise the warmup() method in isolation using a fake session --
zero real Ollama. All assertions must fail before warmup() is implemented.

Covered:
  * warmup fires a minimal 1-token POST to /v1/chat/completions
  * warmup returns True on a successful (200) response
  * warmup returns False (fail-soft) on a simulated timeout
  * warmup returns False (fail-soft) on any exception from the session
  * warmup is bounded by asyncio.wait_for(timeout_s) -- does not hang
  * num_predict/max_tokens == 1 in the request body (minimal generation)
  * temperature is the lowest value (0.0)
  * fail-soft: never raises (catches all exceptions)
"""
from __future__ import annotations

import asyncio
import pytest

from backend.core.ouroboros.governance.local_inference_director import (
    LocalPrimeClient,
    LocalConfig,
)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

def _cfg() -> LocalConfig:
    return LocalConfig.from_env()


class _OkSession:
    """Session that returns a valid 1-token completion immediately."""

    def __init__(self) -> None:
        self.posts: list = []

    def post(self, url: str, **kw):
        self.posts.append({"url": url, **kw})

        class _R:
            status = 200

            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False

            async def json(self_):
                return {
                    "choices": [{"message": {"content": "w"}}],
                    "usage": {"completion_tokens": 1},
                }

        return _R()

    async def close(self) -> None:
        pass


class _HangSession:
    """Session whose POST never completes (simulates a wedged Ollama)."""

    def post(self, url: str, **kw):
        class _R:
            async def __aenter__(self_):
                await asyncio.sleep(9999)  # hang forever
                return self_

            async def __aexit__(self_, *a):
                return False

            async def json(self_):
                return {}

        return _R()

    async def close(self) -> None:
        pass


class _ErrorSession:
    """Session that raises on POST."""

    def post(self, url: str, **kw):
        raise ConnectionRefusedError("ollama not running")

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_warmup_returns_true_on_success():
    """warmup() returns True when the fake Ollama responds immediately."""
    client = LocalPrimeClient(_cfg(), session=_OkSession())
    result = await client.warmup(timeout_s=5.0)
    assert result is True


async def test_warmup_fires_post_to_chat_completions():
    """warmup() sends a POST to the /v1/chat/completions endpoint."""
    sess = _OkSession()
    client = LocalPrimeClient(_cfg(), session=sess)
    await client.warmup(timeout_s=5.0)
    assert len(sess.posts) == 1
    assert sess.posts[0]["url"].endswith("/v1/chat/completions")


async def test_warmup_sends_minimal_token_request():
    """warmup() requests exactly 1 output token (num_predict / max_tokens == 1)."""
    sess = _OkSession()
    client = LocalPrimeClient(_cfg(), session=sess)
    await client.warmup(timeout_s=5.0)
    body = sess.posts[0]["json"]
    # Ollama uses num_predict; the OpenAI-compat layer also accepts max_tokens.
    token_cap = body.get("num_predict") or body.get("max_tokens")
    assert token_cap == 1, f"expected 1-token cap, got body={body!r}"


async def test_warmup_sends_lowest_temperature():
    """warmup() uses temperature 0.0 (deterministic, minimal compute)."""
    sess = _OkSession()
    client = LocalPrimeClient(_cfg(), session=sess)
    await client.warmup(timeout_s=5.0)
    body = sess.posts[0]["json"]
    assert body.get("temperature") == 0.0


async def test_warmup_returns_false_on_timeout():
    """warmup() returns False (fail-soft) when the session hangs past timeout_s."""
    client = LocalPrimeClient(_cfg(), session=_HangSession())
    # Tiny budget so the test completes quickly.
    result = await client.warmup(timeout_s=0.05)
    assert result is False


async def test_warmup_returns_false_on_exception():
    """warmup() returns False (fail-soft) when the session raises."""
    client = LocalPrimeClient(_cfg(), session=_ErrorSession())
    result = await client.warmup(timeout_s=5.0)
    assert result is False


async def test_warmup_never_raises():
    """warmup() swallows all exceptions and always returns bool."""
    client = LocalPrimeClient(_cfg(), session=_ErrorSession())
    # Must complete without raising anything.
    result = await client.warmup(timeout_s=5.0)
    assert isinstance(result, bool)


async def test_warmup_uses_configured_model_name():
    """warmup() targets the model configured in LocalConfig."""
    sess = _OkSession()
    client = LocalPrimeClient(_cfg(), session=sess)
    await client.warmup(timeout_s=5.0)
    body = sess.posts[0]["json"]
    assert body.get("model") == _cfg().model_name


async def test_warmup_timeout_is_respected():
    """warmup() finishes within timeout_s+small-epsilon even on a hanging session."""
    client = LocalPrimeClient(_cfg(), session=_HangSession())
    start = asyncio.get_event_loop().time()
    await client.warmup(timeout_s=0.1)
    elapsed = asyncio.get_event_loop().time() - start
    # Should complete well within 1 second (not hang for 9999s).
    assert elapsed < 1.0, f"warmup took {elapsed:.2f}s, expected <1.0s"

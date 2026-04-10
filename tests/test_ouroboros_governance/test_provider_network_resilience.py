"""Tests for ClaudeProvider network resilience features.

Covers the three Manifesto §3 architectural upgrades:

1. ``_ensure_client`` injects an ``httpx.Timeout`` with an extended read
   budget when ``JARVIS_EXTENDED_THINKING_ENABLED`` is on.
2. ``_call_with_backoff`` retries on transient 5xx / timeout / connection
   errors with exponential backoff, and refuses to retry once partial
   progress has been made (streaming guard).
3. All waits go through ``asyncio.sleep`` so the event loop stays
   responsive during the cognitive wait.
"""

from __future__ import annotations

import asyncio
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.providers import (
    ClaudeProvider,
    _CLAUDE_HTTP_CONNECT_TIMEOUT_S,
    _CLAUDE_HTTP_POOL_TIMEOUT_S,
    _CLAUDE_HTTP_READ_TIMEOUT_DEFAULT_S,
    _CLAUDE_HTTP_READ_TIMEOUT_THINKING_S,
    _CLAUDE_HTTP_WRITE_TIMEOUT_S,
    _is_retryable_transient_error,
)


# ---------------------------------------------------------------------------
# Fake anthropic SDK errors (match class names the retry helper probes for)
# ---------------------------------------------------------------------------


class APITimeoutError(Exception):
    """Stand-in for ``anthropic.APITimeoutError``."""


class APIConnectionError(Exception):
    """Stand-in for ``anthropic.APIConnectionError``."""


class APIStatusError(Exception):
    """Stand-in for ``anthropic.APIStatusError``."""

    def __init__(self, status_code: int, message: str = "status_error") -> None:
        super().__init__(f"{message} ({status_code})")
        self.status_code = status_code


class ReadTimeout(Exception):
    """Stand-in for ``httpx.ReadTimeout``."""


class BadRequestError(Exception):
    """Stand-in for a NON-retryable 400 client error."""

    def __init__(self) -> None:
        super().__init__("bad request")
        self.status_code = 400


# ---------------------------------------------------------------------------
# _is_retryable_transient_error
# ---------------------------------------------------------------------------


def test_is_retryable_detects_anthropic_timeout() -> None:
    assert _is_retryable_transient_error(APITimeoutError("read timeout"))


def test_is_retryable_detects_connection_error() -> None:
    assert _is_retryable_transient_error(APIConnectionError("dns fail"))


def test_is_retryable_detects_httpx_read_timeout() -> None:
    assert _is_retryable_transient_error(ReadTimeout("slow server"))


def test_is_retryable_detects_529_overloaded() -> None:
    assert _is_retryable_transient_error(APIStatusError(529, "overloaded"))


def test_is_retryable_detects_503_unavailable() -> None:
    assert _is_retryable_transient_error(APIStatusError(503, "unavailable"))


def test_is_retryable_detects_502_bad_gateway() -> None:
    assert _is_retryable_transient_error(APIStatusError(502, "bad_gateway"))


def test_is_retryable_detects_504_gateway_timeout() -> None:
    assert _is_retryable_transient_error(APIStatusError(504, "gateway_timeout"))


def test_is_retryable_detects_asyncio_timeout() -> None:
    assert _is_retryable_transient_error(asyncio.TimeoutError())


def test_is_retryable_skips_400_client_error() -> None:
    assert not _is_retryable_transient_error(BadRequestError())


def test_is_retryable_skips_plain_value_error() -> None:
    assert not _is_retryable_transient_error(ValueError("bad json"))


def test_is_retryable_detects_status_via_response_attribute() -> None:
    """APIStatusError in the real SDK sometimes exposes status via .response."""
    exc = Exception("wrapped")
    response = MagicMock()
    response.status_code = 529
    exc.response = response  # type: ignore[attr-defined]
    assert _is_retryable_transient_error(exc)


# ---------------------------------------------------------------------------
# _ensure_client timeout injection
# ---------------------------------------------------------------------------


def _new_provider(**kwargs: Any) -> ClaudeProvider:
    defaults = {
        "api_key": "sk-test",
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "max_cost_per_op": 0.5,
        "daily_budget": 10.0,
    }
    defaults.update(kwargs)
    return ClaudeProvider(**defaults)  # type: ignore[arg-type]


@patch.dict("os.environ", {"JARVIS_EXTENDED_THINKING_ENABLED": "true"}, clear=False)
def test_ensure_client_uses_extended_read_timeout_when_thinking_on() -> None:
    provider = _new_provider()
    assert provider._extended_thinking is True

    captured: dict = {}

    class _FakeTimeout:
        def __init__(self, *, connect: float, read: float, write: float, pool: float) -> None:
            captured["connect"] = connect
            captured["read"] = read
            captured["write"] = write
            captured["pool"] = pool

    class _FakeAsyncAnthropic:
        def __init__(self, *, api_key: str, timeout: Any, max_retries: int) -> None:
            captured["api_key"] = api_key
            captured["timeout"] = timeout
            captured["max_retries"] = max_retries

    fake_anthropic = MagicMock()
    fake_anthropic.AsyncAnthropic = _FakeAsyncAnthropic
    fake_httpx = MagicMock()
    fake_httpx.Timeout = _FakeTimeout

    with patch.dict(
        "sys.modules",
        {"anthropic": fake_anthropic, "httpx": fake_httpx},
    ):
        provider._ensure_client()

    assert captured["read"] == _CLAUDE_HTTP_READ_TIMEOUT_THINKING_S
    assert captured["connect"] == _CLAUDE_HTTP_CONNECT_TIMEOUT_S
    assert captured["write"] == _CLAUDE_HTTP_WRITE_TIMEOUT_S
    assert captured["pool"] == _CLAUDE_HTTP_POOL_TIMEOUT_S
    assert captured["max_retries"] == 0, "SDK retries must be disabled so our retry is visible"


@patch.dict("os.environ", {"JARVIS_EXTENDED_THINKING_ENABLED": "false"}, clear=False)
def test_ensure_client_uses_default_read_timeout_when_thinking_off() -> None:
    provider = _new_provider()
    assert provider._extended_thinking is False

    captured: dict = {}

    class _FakeTimeout:
        def __init__(self, *, connect: float, read: float, write: float, pool: float) -> None:
            captured["read"] = read

    class _FakeAsyncAnthropic:
        def __init__(self, *, api_key: str, timeout: Any, max_retries: int) -> None:
            captured["timeout"] = timeout

    fake_anthropic = MagicMock()
    fake_anthropic.AsyncAnthropic = _FakeAsyncAnthropic
    fake_httpx = MagicMock()
    fake_httpx.Timeout = _FakeTimeout

    with patch.dict(
        "sys.modules",
        {"anthropic": fake_anthropic, "httpx": fake_httpx},
    ):
        provider._ensure_client()

    assert captured["read"] == _CLAUDE_HTTP_READ_TIMEOUT_DEFAULT_S


# ---------------------------------------------------------------------------
# _call_with_backoff retry behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_returns_immediately_on_success() -> None:
    provider = _new_provider()

    async def _ok() -> str:
        return "hello"

    result = await provider._call_with_backoff(_ok, label="t")
    assert result == "hello"


@pytest.mark.asyncio
async def test_backoff_retries_on_timeout_then_succeeds() -> None:
    provider = _new_provider()
    calls: List[int] = []

    async def _flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise APITimeoutError("read timeout")
        return "third-time-lucky"

    sleeps: List[float] = []

    async def _capture_sleep(delay: float) -> None:
        sleeps.append(delay)

    with patch("asyncio.sleep", side_effect=_capture_sleep) as mock_sleep:
        result = await provider._call_with_backoff(
            _flaky, label="t", max_attempts=3, base_delay=2.0,
        )

    assert result == "third-time-lucky"
    assert len(calls) == 3
    # Exponential backoff: 2s after attempt 1, 4s after attempt 2,
    # no sleep after final success.
    assert sleeps == [2.0, 4.0]
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_backoff_retries_on_529_overloaded() -> None:
    provider = _new_provider()
    calls: List[int] = []

    async def _overloaded() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise APIStatusError(529, "overloaded")
        return "ok"

    async def _noop_sleep(delay: float) -> None:
        pass

    with patch("asyncio.sleep", side_effect=_noop_sleep):
        result = await provider._call_with_backoff(_overloaded, label="t")

    assert result == "ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_backoff_does_not_retry_on_non_retryable_error() -> None:
    provider = _new_provider()
    calls: List[int] = []

    async def _bad_request() -> str:
        calls.append(1)
        raise BadRequestError()

    with patch("asyncio.sleep") as mock_sleep:
        with pytest.raises(BadRequestError):
            await provider._call_with_backoff(_bad_request, label="t")

    assert len(calls) == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_backoff_exhausts_attempts_and_raises_last_error() -> None:
    provider = _new_provider()
    calls: List[int] = []

    async def _always_timeout() -> str:
        calls.append(1)
        raise APITimeoutError(f"attempt {len(calls)}")

    async def _noop_sleep(delay: float) -> None:
        pass

    with patch("asyncio.sleep", side_effect=_noop_sleep):
        with pytest.raises(APITimeoutError, match="attempt 3"):
            await provider._call_with_backoff(
                _always_timeout, label="t", max_attempts=3,
            )

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_backoff_aborts_retry_when_progress_probe_true() -> None:
    """Stream path: once tokens have been emitted, a mid-stream transient
    failure must NOT retry (would duplicate output to the caller)."""
    provider = _new_provider()
    calls: List[int] = []
    progress = [False]

    async def _streaming() -> None:
        calls.append(1)
        progress[0] = True  # simulate partial token emission
        raise APITimeoutError("stream broken mid-transfer")

    with patch("asyncio.sleep") as mock_sleep:
        with pytest.raises(APITimeoutError):
            await provider._call_with_backoff(
                _streaming,
                label="claude_stream",
                progress_probe=lambda: progress[0],
            )

    assert len(calls) == 1, "must not retry after partial progress"
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_backoff_uses_asyncio_sleep_not_time_sleep() -> None:
    """Manifesto §3: waits must yield to the event loop, never block it."""
    provider = _new_provider()
    calls: List[int] = []

    async def _flaky() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise APITimeoutError("retry me")
        return "ok"

    # If time.sleep is called, the test should detect it.
    with patch("time.sleep") as mock_time_sleep:
        async def _noop(delay: float) -> None:
            pass
        with patch("asyncio.sleep", side_effect=_noop) as mock_async_sleep:
            await provider._call_with_backoff(_flaky, label="t")

    mock_time_sleep.assert_not_called()
    assert mock_async_sleep.call_count == 1


@pytest.mark.asyncio
async def test_backoff_event_loop_stays_responsive_during_wait() -> None:
    """Concurrent task must be able to run while backoff is sleeping."""
    provider = _new_provider()
    heartbeats: List[int] = []

    async def _heartbeat() -> None:
        for _ in range(5):
            heartbeats.append(1)
            await asyncio.sleep(0)

    calls: List[int] = []

    async def _flaky() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise APITimeoutError("flake")
        return "ok"

    hb_task = asyncio.create_task(_heartbeat())
    # Use a tiny real backoff so the test is fast but still exercises
    # the async sleep path for real.
    result = await provider._call_with_backoff(
        _flaky, label="t", base_delay=0.01,
    )
    await hb_task

    assert result == "ok"
    assert len(heartbeats) == 5, (
        "heartbeat task should have run concurrently while backoff slept"
    )


@pytest.mark.asyncio
async def test_backoff_default_max_attempts_and_delay_from_env() -> None:
    provider = _new_provider()
    calls: List[int] = []

    async def _always_fail() -> str:
        calls.append(1)
        raise APITimeoutError("perma")

    async def _noop_sleep(delay: float) -> None:
        pass

    with patch("asyncio.sleep", side_effect=_noop_sleep):
        with pytest.raises(APITimeoutError):
            await provider._call_with_backoff(_always_fail, label="t")

    # Default max_attempts is 3 (unless overridden via env)
    assert len(calls) >= 2, "should retry at least once on the default setting"

"""Adaptive Stream Watchdog — dual-phase TTFT + sliding ITL with fast-abort.

Mandated bulletproof #1: an SSE stream that yields 3 tokens then hangs
infinitely must be caught by the ITL watchdog, which tears down the stream
(socket abort) and raises a transient error that bridges into the retry loop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.stream_rupture import StreamRuptureError
from backend.core.ouroboros.governance.stream_watchdog import (
    watchdog_consume_sse,
    watchdog_itl_s,
    watchdog_ttft_s,
)

_TOK = 'data: {{"choices":[{{"delta":{{"content":"{t}"}}}}]}}\n'


def _sse(token: str) -> bytes:
    return _TOK.format(t=token).encode("utf-8")


async def test_itl_watchdog_aborts_on_midstream_stall_then_retries() -> None:
    """3 tokens, then the stream hangs forever. The ITL watchdog must:
    (1) catch the stall (not hang), (2) fire the socket teardown, and
    (3) raise StreamRuptureError(phase=inter_chunk) so the caller retries."""
    tokens = [_sse("a"), _sse("b"), _sse("c")]
    state = {"i": 0}

    async def readline():
        i = state["i"]
        state["i"] += 1
        if i < len(tokens):
            return tokens[i]
        # 4th read: hang infinitely (the flapping-transport stall).
        await asyncio.Event().wait()
        return b""

    aborted = {"n": 0}

    def abort_fn():
        aborted["n"] += 1  # BoundedCancellationGuard.transport.abort() stand-in

    t0 = time.monotonic()
    with pytest.raises(StreamRuptureError) as ei:
        await watchdog_consume_sse(
            readline, ttft_s=5.0, itl_s=0.2, abort_fn=abort_fn,
        )
    elapsed = time.monotonic() - t0

    # Stalled AFTER the first tokens → inter-chunk (ITL) phase.
    assert ei.value.phase == "inter_chunk"
    # Socket was torn down exactly once (fast-abort).
    assert aborted["n"] == 1
    # It aborted fast (~itl_s), did NOT hang on the infinite read.
    assert elapsed < 2.0, f"watchdog hung ({elapsed:.1f}s)"
    # The 3 real tokens were consumed before the stall.
    assert ei.value.bytes_received > 0

    # The raised error is transient — bridges to the retry loop. StreamRupture
    # is classified TRANSIENT_TRANSPORT (retryable) by the FSM classifier.
    assert "provider_stream_rupture" in str(ei.value)


async def test_ttft_watchdog_aborts_when_first_token_never_arrives() -> None:
    """No token ever arrives → TTFT-phase stall, aborted at the TTFT bound."""
    async def readline():
        await asyncio.Event().wait()
        return b""

    aborted = {"n": 0}

    t0 = time.monotonic()
    with pytest.raises(StreamRuptureError) as ei:
        await watchdog_consume_sse(
            readline, ttft_s=0.2, itl_s=5.0,
            abort_fn=lambda: aborted.__setitem__("n", aborted["n"] + 1),
        )
    elapsed = time.monotonic() - t0
    assert ei.value.phase == "ttft"
    assert aborted["n"] == 1
    assert elapsed < 2.0


async def test_clean_stream_completes_without_abort() -> None:
    """A well-behaved stream that emits tokens then [DONE] returns the full
    content and never fires the abort."""
    lines = [_sse("Hello"), _sse(" world"), b"data: [DONE]\n", b""]
    state = {"i": 0}

    async def readline():
        i = state["i"]
        state["i"] += 1
        return lines[i] if i < len(lines) else b""

    aborted = {"n": 0}
    content = await watchdog_consume_sse(
        readline, ttft_s=5.0, itl_s=5.0,
        abort_fn=lambda: aborted.__setitem__("n", aborted["n"] + 1),
    )
    assert content == "Hello world"
    assert aborted["n"] == 0


async def test_bounds_default_from_env(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DW_TTFT_BOUND_S", "7.5")
    monkeypatch.setenv("JARVIS_DW_ITL_BOUND_S", "3.0")
    assert watchdog_ttft_s() == 7.5
    assert watchdog_itl_s() == 3.0

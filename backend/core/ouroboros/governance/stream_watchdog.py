"""Adaptive Stream Watchdog — dual-phase TTFT + sliding ITL with fast-abort.

The DW streaming consumer's existing two-phase rupture breaker
(``stream_rupture``: a generous TTFT phase → a tighter inter-chunk phase)
catches a fully-silent stream, but two gaps remain for HEAVY, multi-round ops
on a flapping transport (soak bt-2026-07-22-163424, the wedged Saga repair):

  1. On a stall it does NOT tear down the TCP socket — it waits for the
     aiohttp ``async with`` to unwind at request-total timeout, leaking a dead
     FD and delaying the retry.
  2. The bound is a fixed per-chunk timeout, not a phase-aware
     time-to-first-token (TTFT) vs. inter-token-latency (ITL) split with an
     instant fast-abort.

This module makes the watchdog SURGICAL:

  * **Dual-phase:** a generous TTFT bound until the first content token, then a
    tight sliding ITL bound for every subsequent token.
  * **Fast-abort:** on a breach it invokes an injected abort callback (wired to
    ``BoundedCancellationGuard`` → ``transport.abort()``, a surgical single-FD
    TCP teardown) BEFORE raising — the wedged socket is severed instantly.
  * **Bridges to retry:** raises ``StreamRuptureError`` (phase=ttft|inter_chunk),
    which the classifier routes to a transient decision → the caller's
    exponential-backoff/transient-retry loop.

Pure async ``wait_for`` — never blocks the event loop while waiting. Bounds are
env-driven (composes the existing ``stream_rupture`` timeout family — DRY, no
hardcoding).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional, Union

from backend.core.ouroboros.governance.stream_rupture import (
    StreamRuptureError,
    stream_inter_chunk_timeout_s,
    stream_rupture_timeout_s,
)

logger = logging.getLogger("Ouroboros.StreamWatchdog")

_ENABLED_ENV = "JARVIS_DW_STREAM_WATCHDOG_ENABLED"
_TTFT_ENV = "JARVIS_DW_TTFT_BOUND_S"
_ITL_ENV = "JARVIS_DW_ITL_BOUND_S"


def watchdog_enabled() -> bool:
    """Master gate (default TRUE). OFF → callers keep the legacy per-chunk
    rupture behavior byte-identically."""
    return os.environ.get(_ENABLED_ENV, "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def watchdog_ttft_s() -> float:
    """Time-To-First-Token bound. Defaults to the existing rupture TTFT budget
    (``JARVIS_STREAM_RUPTURE_TIMEOUT_S``); overridable via
    ``JARVIS_DW_TTFT_BOUND_S``. Never raises."""
    raw = os.environ.get(_TTFT_ENV, "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
    try:
        return stream_rupture_timeout_s()
    except Exception:  # noqa: BLE001
        return 120.0


def watchdog_itl_s() -> float:
    """Inter-Token-Latency bound (sliding gap allowed between content tokens).
    Defaults to the existing inter-chunk budget
    (``JARVIS_STREAM_INTER_CHUNK_TIMEOUT_S``); overridable via
    ``JARVIS_DW_ITL_BOUND_S``. Never raises."""
    raw = os.environ.get(_ITL_ENV, "").strip()
    if raw:
        try:
            return max(0.05, float(raw))
        except (TypeError, ValueError):
            pass
    try:
        return stream_inter_chunk_timeout_s()
    except Exception:  # noqa: BLE001
        return 30.0


def fast_abort_response(response: Any) -> bool:
    """Surgically tear down the single TCP socket behind an aiohttp response —
    the watchdog fast-abort. Mirrors ``BoundedCancellationGuard._fire_abort``
    (Slice 7b): ``transport.abort()`` severs ONE file descriptor, never a
    pool-wide close. Wire this into a provider's rupture path so a stalled
    socket is cut instantly instead of leaking until request-total timeout.
    Gated by ``watchdog_enabled``; returns True if aborted; NEVER raises."""
    if not watchdog_enabled() or response is None:
        return False
    try:
        conn = getattr(response, "connection", None)
        transport = getattr(conn, "transport", None) if conn is not None else None
        if transport is not None and hasattr(transport, "abort"):
            transport.abort()
            logger.debug("[StreamWatchdog] fast-abort: severed stalled DW socket")
            return True
    except Exception:  # noqa: BLE001 — a failed abort must not mask the stall
        logger.debug("[StreamWatchdog] fast_abort_response failed", exc_info=True)
    return False


def _fire_abort(abort_fn: Optional[Callable[[], None]]) -> None:
    """Invoke the socket-teardown callback, swallowing any error — a failed
    abort must never mask the underlying stall."""
    if abort_fn is None:
        return
    try:
        abort_fn()
    except Exception:  # noqa: BLE001
        logger.debug("[StreamWatchdog] abort_fn raised (ignored)", exc_info=True)


def _extract_token(data: str) -> str:
    """Parse an OpenAI-compatible SSE ``data:`` payload → the content delta
    (``""`` for role/reasoning-only deltas). Never raises."""
    try:
        chunk = json.loads(data)
        choices = chunk.get("choices") or [{}]
        delta = choices[0].get("delta", {}) if choices else {}
        return delta.get("content", "") or ""
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""


ReadLine = Callable[[], Awaitable[Union[bytes, str]]]


async def watchdog_consume_sse(
    readline: ReadLine,
    *,
    ttft_s: float,
    itl_s: float,
    abort_fn: Optional[Callable[[], None]] = None,
    provider: str = "doubleword",
    parse_token: Optional[Callable[[str], str]] = None,
    on_token: Optional[Callable[[str], None]] = None,
) -> str:
    """Consume an SSE line-reader under dual-phase TTFT + sliding-ITL bounds.

    ``readline`` is an async callable returning the next raw SSE line (bytes or
    str); an empty return signals the stream closed. Before the first content
    token the TTFT bound applies; after it, the ITL bound applies to the gap
    before each subsequent read. On a bound breach the watchdog invokes
    ``abort_fn`` (surgical socket teardown) and raises ``StreamRuptureError``
    with ``phase="ttft"`` or ``"inter_chunk"``. Returns the accumulated content.

    Never blocks the loop between reads (pure ``asyncio.wait_for``)."""
    content = ""
    bytes_received = 0
    seen_first = False
    start = time.monotonic()
    last_token_at = start
    while True:
        bound = itl_s if seen_first else ttft_s
        try:
            line = await asyncio.wait_for(readline(), timeout=bound)
        except asyncio.TimeoutError:
            phase = "inter_chunk" if seen_first else "ttft"
            _fire_abort(abort_fn)
            elapsed = time.monotonic() - (last_token_at if seen_first else start)
            logger.warning(
                "[StreamWatchdog] %s STALL phase=%s elapsed=%.1fs bound=%.1fs "
                "bytes=%d — socket torn down, bridging to transient retry",
                provider, phase, elapsed, bound, bytes_received,
            )
            raise StreamRuptureError(
                provider=provider,
                elapsed_s=elapsed,
                bytes_received=bytes_received,
                rupture_timeout_s=bound,
                phase=phase,
            )
        if not line:
            break  # stream closed cleanly
        s = (
            line.decode("utf-8", "replace")
            if isinstance(line, (bytes, bytearray))
            else str(line)
        ).strip()
        bytes_received += len(s)
        if not s.startswith("data:"):
            continue
        data = s[5:].strip() if s[5:6] != " " else s[6:].strip()
        if data == "[DONE]":
            break
        token = (parse_token or _extract_token)(data)
        if token:
            content += token
            seen_first = True
            last_token_at = time.monotonic()
            if on_token is not None:
                try:
                    on_token(token)
                except Exception:  # noqa: BLE001
                    pass
    return content


__all__ = [
    "ReadLine",
    "watchdog_consume_sse",
    "watchdog_enabled",
    "watchdog_itl_s",
    "watchdog_ttft_s",
]

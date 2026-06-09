"""Slice 180 — the Immortal Execution Layer (total vendor resilience).

The DW-only soak proved the organism BLEEDS tasks: when all DW surfaces degrade and Claude is
disabled, an op hits ``all_providers_exhausted:fallback_skipped:no_fallback_configured`` and is
deleted. A Sovereign Organism must be mathematically immortal — an op either succeeds, hedges,
retries, or QUEUES and waits, but is NEVER lost. This module is the resilience substrate:

  1. ``immortal_should_retry`` / ``immortal_retry`` — when there's NO fallback, backoff-retry
     DW until the vendor recovers (or the op's own deadline / a bounded attempt cap).
  2. ``hedge_to_batch_on_rupture`` — a transport rupture should retry the SAME request over the
     stream-free batch lane before the model is declared failed.
  3. ``batch_should_retry`` — the batch lane is NOT bulletproof; re-submit on transient 5xx.

Pure + injectable (clock/sleep) so the recovery path is unit-testable without real time. Gated
default-TRUE (failure-path-only: only acts on the exhaustion path, so it cannot affect a healthy
run). NEVER raises out of the predicates.
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Optional

_ENV_ENABLED = "JARVIS_DW_IMMORTAL_QUEUE_ENABLED"
_ENV_BASE_BACKOFF = "JARVIS_DW_IMMORTAL_BACKOFF_BASE_S"
_ENV_CAP_BACKOFF = "JARVIS_DW_IMMORTAL_BACKOFF_CAP_S"
_ENV_MAX_ATTEMPTS = "JARVIS_DW_IMMORTAL_MAX_ATTEMPTS"

_DEFAULT_BASE_BACKOFF = 2.0
_DEFAULT_CAP_BACKOFF = 60.0
_DEFAULT_MAX_ATTEMPTS = 30

# Transport-rupture markers that warrant an intra-request batch hedge (stream-specific).
_RUPTURE_MARKERS = ("live_transport", "transferencoding", "clientpayload", "stream_stall")


def immortal_queue_enabled() -> bool:
    """Master — default **TRUE** (failure-path-only: only the exhaustion path consults it).
    NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in ("0", "false", "no", "off")


def dw_hedge_enabled() -> bool:
    """Slice 181 — master for the intra-request RT→batch hedge. Default **TRUE**
    (failure-path-only: only fires on a stream rupture). NEVER raises."""
    return os.environ.get("JARVIS_DW_HEDGE_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def dw_batch_retry_enabled() -> bool:
    """Slice 181 — master for the Kevlar batch-creation retry. Default **TRUE**
    (failure-path-only: only fires on a transient 5xx). NEVER raises."""
    return os.environ.get("JARVIS_DW_BATCH_RETRY_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def dw_batch_max_retries() -> int:
    """Slice 181 — bounded batch-creation re-submits. NEVER raises."""
    try:
        v = int(_envf("JARVIS_DW_BATCH_MAX_RETRIES", 3.0))
        return v if v > 0 else 3
    except Exception:  # noqa: BLE001
        return 3


def _envf(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v > 0 else default
    except Exception:  # noqa: BLE001
        return default


def immortal_max_attempts() -> int:
    """Bounded backstop on retries (the immortal budget is the primary bound). NEVER raises."""
    try:
        v = int(_envf(_ENV_MAX_ATTEMPTS, _DEFAULT_MAX_ATTEMPTS))
        return v if v > 0 else _DEFAULT_MAX_ATTEMPTS
    except Exception:  # noqa: BLE001
        return _DEFAULT_MAX_ATTEMPTS


def immortal_max_wait_s() -> float:
    """Slice 182 Gap 3 — the immortal queue's budget, DETACHED from the op's 120s generation
    deadline. A sustained DW outage must not expire the op; this is a separate, much longer
    wall (default 1h) over which the queue keeps backing off and retrying. NEVER raises."""
    return _envf("JARVIS_DW_IMMORTAL_MAX_WAIT_S", 3600.0)


def immortal_per_attempt_window_s() -> float:
    """Slice 182 Gap 3 — each immortal re-attempt gets a FRESH generation window (default 180s)
    rather than inheriting the original op's already-elapsed deadline, so a retry actually has
    time to complete once DW recovers. NEVER raises."""
    return _envf("JARVIS_DW_IMMORTAL_ATTEMPT_WINDOW_S", 180.0)


def immortal_backoff_s(attempt: int, *, base: Optional[float] = None, cap: Optional[float] = None) -> float:
    """Exponential backoff ``base * 2**attempt``, capped. NEVER raises."""
    try:
        b = base if base is not None else _envf(_ENV_BASE_BACKOFF, _DEFAULT_BASE_BACKOFF)
        c = cap if cap is not None else _envf(_ENV_CAP_BACKOFF, _DEFAULT_CAP_BACKOFF)
        return min(c, b * (2.0 ** max(0, int(attempt))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_BASE_BACKOFF


def immortal_should_retry(
    *, deadline: float, now: float, claude_available: bool, attempt: int, max_attempts: int,
) -> bool:
    """QUEUE-vs-exhaust decision: retry DW iff immortal-queue is on, there is NO fallback
    (Claude unavailable — so exhausting would DELETE the op), the op's deadline is still ahead,
    and the bounded attempt cap isn't reached. NEVER raises."""
    try:
        if not immortal_queue_enabled():
            return False
        if claude_available:
            return False  # a real fallback exists → the normal cascade owns this, not the queue
        if now >= deadline:
            return False  # the op's own deadline — bounded, not infinite
        if attempt >= max_attempts:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def hedge_to_batch_on_rupture(failure_class: Any) -> bool:
    """True iff a failure is a TRANSPORT rupture (stream-specific) — the same request should be
    retried over the stream-free batch lane before declaring the model failed. NEVER raises."""
    try:
        fc = str(failure_class or "").strip().lower()
        return any(m in fc for m in _RUPTURE_MARKERS)
    except Exception:  # noqa: BLE001
        return False


def batch_should_retry(status_code: Any, attempt: int, *, max_retries: int) -> bool:
    """Kevlar batch net: re-submit on a TRANSIENT 5xx batch-creation error, up to max_retries.
    4xx (client/param error — the 168 class) is NOT retried (re-submitting won't help). NEVER
    raises."""
    try:
        code = int(status_code)
        if attempt >= max_retries:
            return False
        return 500 <= code < 600
    except Exception:  # noqa: BLE001
        return False


async def immortal_retry(
    attempt_fn: Callable[[], Awaitable[Any]],
    *,
    deadline_fn: Callable[[], float],
    now_fn: Callable[[], float],
    sleep_fn: Callable[[float], Awaitable[None]],
    claude_available: bool,
    max_attempts: Optional[int] = None,
    base_backoff: Optional[float] = None,
    cap_backoff: Optional[float] = None,
) -> Any:
    """Run ``attempt_fn`` with exponential-backoff retry while QUEUE-vs-exhaust says to keep
    trying (no fallback, deadline ahead, attempts left). Returns the first success. Re-raises the
    LAST exception only when the retry budget is genuinely exhausted (deadline / attempt cap) —
    so a transient total DW outage is survived, while a permanent one still fails (bounded).
    Clock + sleep are injected for deterministic tests."""
    _max = max_attempts if max_attempts is not None else int(_envf(_ENV_MAX_ATTEMPTS, _DEFAULT_MAX_ATTEMPTS))
    attempt = 0
    last_exc: Optional[BaseException] = None
    while True:
        try:
            return await attempt_fn()
        except BaseException as exc:  # noqa: BLE001 — the retry loop's whole purpose
            last_exc = exc
            if not immortal_should_retry(
                deadline=deadline_fn(), now=now_fn(),
                claude_available=claude_available, attempt=attempt, max_attempts=_max,
            ):
                raise
            await sleep_fn(immortal_backoff_s(attempt, base=base_backoff, cap=cap_backoff))
            attempt += 1

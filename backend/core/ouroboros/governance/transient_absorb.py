"""Idempotent Transient-Absorb Decorator — per-round self-healing for DW.

Big-file ReAct repairs (the 932-line Saga fix) drive MANY sequential DoubleWord
streaming rounds. Any one round can stall on a flapping transport; the Adaptive
Stream Watchdog fast-aborts it — but a fast-abort tears down the socket
MID-STREAM, leaving a partial, corrupted artifact (a broken JSON tool-call).
If that partial is ingested, the whole multi-round loop is poisoned.

This decorator injects resilience into a DW round with ZERO invasive
indentation — no 95-line re-indent of the legacy provider hot paths (mandate 1):

  * **Transient-absorb backoff** — a transient round failure (StreamRuptureError
    fast-abort, ``upstream_error``, 5xx, 429-with-Retry-After) is classified via
    the SAME 5xx Resiliency Matrix (``provider_retry_classifier``) and retried
    with the SAME exponential-backoff-with-jitter primitive
    (``_dw_transient_backoff_s``) proven in PR #70016/#70017 (DRY, mandate 3).
  * **State-Safe Resumption (mandate 2)** — before the round the decorator
    snapshots the mutable ReAct state (the message transcript); on a watchdog
    abort it PURGES the partial artifact and RESTORES the pre-round checkpoint
    before the retry, so the loop never ingests corrupted mid-stream state.

Applied as ``@with_transient_absorb(...)`` directly above ``_call_primary`` /
``_generate_realtime``. Master-gated, never blocks the loop (async sleep only),
and — when disabled or on a non-transient error — byte-identical to the
undecorated method.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import logging
import os
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("Ouroboros.TransientAbsorb")

_ENABLED_ENV = "JARVIS_TRANSIENT_ABSORB_DECORATOR_ENABLED"


def decorator_enabled() -> bool:
    """Master gate (default TRUE). OFF → the wrapped method runs exactly once,
    byte-identical to the undecorated legacy behavior."""
    return os.environ.get(_ENABLED_ENV, "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _is_transient(exc: BaseException) -> bool:
    """True when *exc* is a transient DW round failure that a retry can absorb.

    Requires a POSITIVE transient signal — a watchdog fast-abort / stream
    rupture, a bare timeout, a 5xx/408/429 HTTP status, or an explicit
    ``TRANSIENT_NETWORK`` classification (upstream_error / gateway timeout /
    429-with-Retry-After) via the 5xx Resiliency Matrix. It deliberately does
    NOT trust the classifier's permissive "unknown → RETRY_TRANSIENT" fallback,
    so a genuine code bug (a bare ``ValueError``/``KeyError`` with no transport
    signal) is NEVER retried and never masked as retryable. Never raises."""
    try:
        from backend.core.ouroboros.governance.stream_rupture import (
            StreamRuptureError,
        )
        if isinstance(exc, StreamRuptureError):
            return True
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, asyncio.TimeoutError):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (408, 429, 500, 502, 503, 504):
        return True
    try:
        from backend.core.ouroboros.governance.provider_retry_classifier import (
            classify, RetryDecision,
        )
        retry_after = getattr(exc, "ratelimit_reset_ts", None)
        decision = classify(
            failure_class=type(exc).__name__,
            http_status=status,
            failure_message=str(exc),
            retry_after_present=retry_after is not None,
        )
        # Only the EXPLICIT transient-network class counts (not the bare
        # unknown→RETRY_TRANSIENT fallback).
        return decision == RetryDecision.TRANSIENT_NETWORK
    except Exception:  # noqa: BLE001
        return False


def _backoff_s(attempt: int, exc: BaseException, remaining_s: float) -> float:
    """Delay before the next round attempt — reuses the DW transient backoff
    (Retry-After-aware full-jitter) from candidate_generator (DRY). Fail-soft."""
    retry_after = getattr(exc, "ratelimit_reset_ts", None)
    try:
        from backend.core.ouroboros.governance.candidate_generator import (
            _dw_transient_backoff_s,
        )
        return _dw_transient_backoff_s(
            attempt, retry_after, remaining_s=remaining_s,
        )
    except Exception:  # noqa: BLE001 — degrade to a bounded fixed backoff
        return min(8.0, 1.5 * (2 ** max(0, attempt)))


def _max_retries() -> int:
    try:
        from backend.core.ouroboros.governance.candidate_generator import (
            _dw_transient_max_retries,
        )
        return _dw_transient_max_retries()
    except Exception:  # noqa: BLE001
        try:
            return max(0, int(os.environ.get("JARVIS_DW_TRANSIENT_MAX_RETRIES", "2")))
        except (TypeError, ValueError):
            return 2


def with_transient_absorb(
    *,
    transcript: Optional[Callable[..., Any]] = None,
    remaining_s: Optional[Callable[..., float]] = None,
    max_retries: Optional[Callable[[], int]] = None,
    label: Optional[str] = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator factory for per-round transient-absorb + state-safe resumption.

    Parameters
    ----------
    transcript:
        Optional ``(*args, **kwargs) -> mutable list/dict`` accessor for the
        ReAct message transcript (the state that a mid-stream abort would
        corrupt). Snapshotted before the round; DEEP-restored in place on a
        transient abort so any partial artifact is purged. ``None`` → no state
        to protect (the provider-call level, where the round is naturally
        atomic: it returns a full result or raises, never a partial).
    remaining_s:
        Optional ``(*args, **kwargs) -> float`` remaining-budget accessor;
        retries stop when the budget is spent. Default: unbounded-by-budget.
    max_retries / label:
        Override the retry ceiling / log label.
    """
    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        _name = label or getattr(fn, "__name__", "dw_round")

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not decorator_enabled():
                return await fn(*args, **kwargs)
            budget = (max_retries or _max_retries)()
            # State-Safe Resumption: snapshot the pre-round transcript once.
            live_state = None
            snapshot = None
            if transcript is not None:
                try:
                    live_state = transcript(*args, **kwargs)
                    snapshot = copy.deepcopy(live_state)
                except Exception:  # noqa: BLE001 — never block on snapshot
                    live_state = None
                    snapshot = None
            attempt = 0
            while True:
                try:
                    return await fn(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001
                    _rem = 1e9
                    if remaining_s is not None:
                        try:
                            _rem = float(remaining_s(*args, **kwargs))
                        except Exception:  # noqa: BLE001
                            _rem = 1e9
                    if (
                        not _is_transient(exc)
                        or attempt >= budget
                        or _rem <= 0.0
                    ):
                        raise
                    # PURGE the partial artifact + RESTORE the pre-round state,
                    # in place, so the caller's transcript reference is intact.
                    if live_state is not None and snapshot is not None:
                        try:
                            _restore_in_place(live_state, snapshot)
                        except Exception:  # noqa: BLE001
                            pass
                    delay = _backoff_s(attempt, exc, _rem)
                    logger.warning(
                        "[TransientAbsorb] %s round abort (%s) — purged partial, "
                        "restored pre-round state, backoff %.1fs, retry %d/%d",
                        _name, type(exc).__name__, delay, attempt + 1, budget,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
        return wrapper
    return deco


def _restore_in_place(live: Any, snapshot: Any) -> None:
    """Restore a mutable container's contents to *snapshot* WITHOUT rebinding
    the caller's reference (so the ReAct loop keeps working against the same
    object). Supports list and dict transcripts."""
    if isinstance(live, list):
        live[:] = copy.deepcopy(snapshot)
    elif isinstance(live, dict):
        live.clear()
        live.update(copy.deepcopy(snapshot))
    # Any other type: nothing safe to restore in place; leave as-is.


__all__ = [
    "decorator_enabled",
    "with_transient_absorb",
]

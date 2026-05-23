"""Foreground cooldown — Slice 12O adaptive macro-retry.

Empirical context: bt-2026-05-23-022809 (Path A post-Slice-12N
soak). The SWE-Bench-Pro fixture op reached GENERATE end-to-end
through the Slice 12N substrate but terminated with::

    [CircuitBreaker] op=op-019e52ab tripped → OPEN_TERMINAL
      reason=circuit_breaker_tripped:terminal_structural
      origin=foreground
    [CandidateGenerator] EXHAUSTION
      cause=circuit_breaker_tripped:terminal_structural
    Generation attempt 2/2 failed: all_providers_exhausted...

Both DW and Claude were refusing requests in the same window
(provider-side flakiness, NOT a JARVIS-side bug). The op had
healthy budget + wall remaining but the retry-table inside
``candidate_generator`` exhausted its in-window attempts and
the orchestrator terminated the op.

Slice 12O Phase 1 adds a MACRO-retry layer above the in-window
retries: when a FOREGROUND op hits provider-exhaustion-class
``terminal_structural`` AND budget/wall budget remains, park the
op for an exponential-backoff cooldown (60s → 120s → 240s,
capped) and re-attempt GENERATE without burning a retry slot.

This sits BETWEEN the orchestrator's retry-counter decrement and
the op's terminal transition — composes the existing retry loop
rather than introducing a parallel one.

Slice 12O Phase 3 cancellation discipline: ``sleep_cooldown`` is
a thin wrapper over ``asyncio.sleep`` which is natively
cancellation-aware. When Layer-2 graceful shutdown fires, every
in-flight cooldown sleep wakes immediately, the
``CancelledError`` propagates up, and the orchestrator records a
distinct ``cooldown_cancelled_shutdown`` terminal reason before
the asyncio cleanup cascade runs. NO blocking ``time.sleep`` is
ever used — the wall-clock hard-kill that ended
bt-2026-05-23-022809 should not need to fire again.

## API surface

  * ``CooldownReason`` — closed 2-value enum (PROVIDER_EXHAUSTION /
    STREAM_RUPTURE)
  * ``CooldownDecision`` — frozen dataclass returned by
    ``ForegroundCooldownPolicy.decide``
  * ``ForegroundCooldownPolicy`` — process singleton via
    ``get_default_policy()``
  * ``sleep_cooldown`` — cancellation-aware async helper
  * ``is_provider_exhaustion_cause`` — pure classifier on a
    ``terminal_reason_code`` string

## Env knobs

  * ``JARVIS_FOREGROUND_COOLDOWN_ENABLED``         default true
  * ``JARVIS_FOREGROUND_COOLDOWN_MAX_ATTEMPTS``    default 3
  * ``JARVIS_FOREGROUND_COOLDOWN_BASE_S``          default 60.0
  * ``JARVIS_FOREGROUND_COOLDOWN_CAP_S``           default 300.0
  * ``JARVIS_FOREGROUND_COOLDOWN_MIN_BUDGET_USD``  default 0.05
  * ``JARVIS_FOREGROUND_COOLDOWN_MIN_WALL_S``      default 30.0
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("Ouroboros.ForegroundCooldown")


# ============================================================================
# Env knobs
# ============================================================================


_MASTER_FLAG_ENV: str = "JARVIS_FOREGROUND_COOLDOWN_ENABLED"
_MAX_ATTEMPTS_ENV: str = "JARVIS_FOREGROUND_COOLDOWN_MAX_ATTEMPTS"
_BASE_S_ENV: str = "JARVIS_FOREGROUND_COOLDOWN_BASE_S"
_CAP_S_ENV: str = "JARVIS_FOREGROUND_COOLDOWN_CAP_S"
_MIN_BUDGET_USD_ENV: str = "JARVIS_FOREGROUND_COOLDOWN_MIN_BUDGET_USD"
_MIN_WALL_S_ENV: str = "JARVIS_FOREGROUND_COOLDOWN_MIN_WALL_S"


_DEFAULT_MAX_ATTEMPTS: int = 3
_DEFAULT_BASE_S: float = 60.0
_DEFAULT_CAP_S: float = 300.0
_DEFAULT_MIN_BUDGET_USD: float = 0.05
_DEFAULT_MIN_WALL_S: float = 30.0

# Floors so a misconfigured env can't bypass the safety contract.
_FLOOR_MAX_ATTEMPTS: int = 0   # 0 = disabled (legal escape hatch)
_CEIL_MAX_ATTEMPTS: int = 20
_FLOOR_BASE_S: float = 1.0
_CEIL_BASE_S: float = 3600.0
_FLOOR_CAP_S: float = 1.0
_CEIL_CAP_S: float = 3600.0


def cooldown_enabled() -> bool:
    """Master gate. Default TRUE. Explicit ``"false"`` opts out
    (byte-identical pre-Slice-12O behavior). NEVER raises."""
    try:
        raw = os.environ.get(_MASTER_FLAG_ENV, "").strip().lower()
        if raw == "":
            return True
        return raw not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        return True


def _read_int(env: str, default: int, *, floor: int, ceil: int) -> int:
    try:
        raw = os.environ.get(env, "").strip()
        if not raw:
            return default
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(floor, min(ceil, v))


def _read_float(env: str, default: float, *, floor: float, ceil: float) -> float:
    try:
        raw = os.environ.get(env, "").strip()
        if not raw:
            return default
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(floor, min(ceil, v))


# ============================================================================
# Closed taxonomies
# ============================================================================


class CooldownReason(str, enum.Enum):
    """Closed 2-value taxonomy of WHY a foreground op is being
    parked for cooldown. Both shapes share the same backoff
    policy — the taxonomy exists for telemetry / observability,
    not for behavioral branching."""

    PROVIDER_EXHAUSTION = "provider_exhaustion"
    STREAM_RUPTURE = "stream_rupture"


@dataclass(frozen=True)
class CooldownDecision:
    """Frozen decision returned by ``ForegroundCooldownPolicy.decide``.

    Caller contract:

      * ``should_cooldown=True``: caller MUST ``await
        sleep_cooldown(decision.backoff_s, ...)`` before re-attempting
        GENERATE. After sleep, retry attempt should NOT decrement the
        op's retry counter — this is a macro-retry layer ABOVE the
        in-window retries.

      * ``should_cooldown=False``: caller MUST proceed with the
        existing terminal-transition path. ``refuse_reason`` is a
        canonical short string for telemetry attribution.
    """

    should_cooldown: bool
    backoff_s: float = 0.0
    reason: Optional[CooldownReason] = None
    attempt: int = 0  # 1-indexed; which cooldown attempt this would be
    max_attempts: int = 0
    refuse_reason: Optional[str] = None


# ============================================================================
# Pure classifiers
# ============================================================================


# Substrings that indicate the terminal reason was caused by
# upstream provider unavailability (the kind worth a macro-retry)
# vs. a structural-bug failure (the kind that should NOT retry).
_PROVIDER_EXHAUSTION_SUBSTRINGS = (
    "all_providers_exhausted",
    "circuit_breaker_tripped:terminal_structural",
    "circuit_breaker_tripped:terminal_quota",
    "provider_exhausted",
)

_STREAM_RUPTURE_SUBSTRINGS = (
    "stream_rupture",
    "stream_disconnected",
    "stream_eof",
    "stream_timeout",
)


def is_provider_exhaustion_cause(
    terminal_reason_code: str,
) -> Optional[CooldownReason]:
    """Pure classifier. Returns the matching ``CooldownReason`` if
    the terminal reason is upstream-provider-class, else None.
    NEVER raises.

    Order matters: stream rupture is checked first because some
    rupture reasons may contain "provider" tokens as context."""
    if not isinstance(terminal_reason_code, str):
        return None
    code = terminal_reason_code.lower()
    for needle in _STREAM_RUPTURE_SUBSTRINGS:
        if needle in code:
            return CooldownReason.STREAM_RUPTURE
    for needle in _PROVIDER_EXHAUSTION_SUBSTRINGS:
        if needle in code:
            return CooldownReason.PROVIDER_EXHAUSTION
    return None


# ============================================================================
# Policy
# ============================================================================


class ForegroundCooldownPolicy:
    """Process singleton governing per-op cooldown attempt counts
    and policy gates.

    State: a dict mapping ``op_id`` → cooldown_attempt_count.
    Cleared per-op when the op is terminated (success OR final
    failure) via ``record_recovery`` / ``forget``.

    Decision is PURE — ``decide()`` reads env knobs + the current
    attempt counter + caller-provided budget/wall snapshots; never
    side-effects until ``record_attempt`` is called after a
    cooldown actually begins.
    """

    def __init__(self) -> None:
        self._attempts_by_op: Dict[str, int] = {}
        self._lock = threading.Lock()

    # ---- introspection ----

    def attempt_count(self, op_id: str) -> int:
        """Current cooldown attempt count for ``op_id`` (0 if
        never parked)."""
        with self._lock:
            return self._attempts_by_op.get(op_id, 0)

    def total_parked(self) -> int:
        """Total number of ops currently tracked. For telemetry."""
        with self._lock:
            return len(self._attempts_by_op)

    # ---- decision ----

    def decide(
        self,
        *,
        op_id: str,
        origin_is_foreground: bool,
        terminal_reason_code: str,
        remaining_budget_usd: Optional[float],
        remaining_wall_s: Optional[float],
    ) -> CooldownDecision:
        """Pure decision. NEVER raises.

        Caller passes the CURRENT remaining-budget + remaining-wall
        snapshots from the CostGovernor + WallClockWatchdog (the
        single sources of truth for those quantities). Policy
        consults env knobs + per-op attempt counter to decide
        whether a cooldown is safe + worth attempting.

        Gates (refuse in this order; first-fail-wins reason):

          1. master flag — if FALSE, refuse with ``"disabled"``
          2. origin — only FOREGROUND origins are eligible
          3. terminal_reason classify — must be a provider-class
             cause (not a structural bug)
          4. attempt_count < max_attempts
          5. remaining_budget_usd >= min_budget_usd
          6. remaining_wall_s >= (backoff_s + min_wall_s)
        """
        if not cooldown_enabled():
            return CooldownDecision(
                should_cooldown=False, refuse_reason="disabled",
            )
        if not origin_is_foreground:
            return CooldownDecision(
                should_cooldown=False,
                refuse_reason="not_foreground_origin",
            )
        cooldown_reason = is_provider_exhaustion_cause(terminal_reason_code)
        if cooldown_reason is None:
            return CooldownDecision(
                should_cooldown=False,
                refuse_reason="not_provider_exhaustion",
            )

        max_attempts = _read_int(
            _MAX_ATTEMPTS_ENV, _DEFAULT_MAX_ATTEMPTS,
            floor=_FLOOR_MAX_ATTEMPTS, ceil=_CEIL_MAX_ATTEMPTS,
        )
        if max_attempts <= 0:
            return CooldownDecision(
                should_cooldown=False,
                refuse_reason="max_attempts_zero",
                max_attempts=max_attempts,
            )

        current_attempt = self.attempt_count(op_id)
        if current_attempt >= max_attempts:
            return CooldownDecision(
                should_cooldown=False,
                refuse_reason=f"max_attempts_exhausted_{current_attempt}_of_{max_attempts}",
                attempt=current_attempt,
                max_attempts=max_attempts,
                reason=cooldown_reason,
            )

        backoff_s = self._compute_backoff(current_attempt)
        min_budget = _read_float(
            _MIN_BUDGET_USD_ENV, _DEFAULT_MIN_BUDGET_USD,
            floor=0.0, ceil=1000.0,
        )
        min_wall = _read_float(
            _MIN_WALL_S_ENV, _DEFAULT_MIN_WALL_S,
            floor=0.0, ceil=3600.0,
        )

        # Budget gate — only check if caller provided a snapshot.
        # None means "caller doesn't know" → skip the gate (safer
        # to attempt than to refuse on missing data).
        if remaining_budget_usd is not None and \
                remaining_budget_usd < min_budget:
            return CooldownDecision(
                should_cooldown=False,
                refuse_reason=f"insufficient_budget_remaining_{remaining_budget_usd:.3f}_below_floor_{min_budget:.3f}",
                attempt=current_attempt,
                max_attempts=max_attempts,
                reason=cooldown_reason,
                backoff_s=backoff_s,
            )

        # Wall gate — cooldown + post-cooldown retry both need
        # wall headroom. None means "caller doesn't know" → skip.
        if remaining_wall_s is not None and \
                remaining_wall_s < (backoff_s + min_wall):
            return CooldownDecision(
                should_cooldown=False,
                refuse_reason=f"insufficient_wall_remaining_{remaining_wall_s:.0f}s_below_floor_{backoff_s + min_wall:.0f}s",
                attempt=current_attempt,
                max_attempts=max_attempts,
                reason=cooldown_reason,
                backoff_s=backoff_s,
            )

        # All gates passed.
        return CooldownDecision(
            should_cooldown=True,
            backoff_s=backoff_s,
            reason=cooldown_reason,
            attempt=current_attempt + 1,
            max_attempts=max_attempts,
        )

    # ---- side-effecting state transitions ----

    def record_attempt(self, op_id: str) -> int:
        """Increment the attempt counter for ``op_id``. Returns
        the new count. Called AFTER the policy decision returns
        should_cooldown=True AND the orchestrator has committed
        to actually sleeping."""
        with self._lock:
            new_count = self._attempts_by_op.get(op_id, 0) + 1
            self._attempts_by_op[op_id] = new_count
        return new_count

    def record_recovery(self, op_id: str) -> None:
        """Clear the attempt counter after a successful retry —
        the op recovered, so subsequent failures get fresh
        attempts. NEVER raises."""
        with self._lock:
            self._attempts_by_op.pop(op_id, None)

    def forget(self, op_id: str) -> None:
        """Drop the attempt counter at op terminal transition
        (whether success or failure). Alias of record_recovery;
        named for the terminal-cleanup site clarity."""
        self.record_recovery(op_id)

    def reset(self) -> None:
        """For tests. Clears all per-op state."""
        with self._lock:
            self._attempts_by_op.clear()

    # ---- helpers ----

    def _compute_backoff(self, attempt: int) -> float:
        """Exponential backoff: base * 2^attempt, capped. ``attempt``
        is 0-indexed (the first cooldown is attempt=0, backoff=base).
        """
        base = _read_float(
            _BASE_S_ENV, _DEFAULT_BASE_S,
            floor=_FLOOR_BASE_S, ceil=_CEIL_BASE_S,
        )
        cap = _read_float(
            _CAP_S_ENV, _DEFAULT_CAP_S,
            floor=_FLOOR_CAP_S, ceil=_CEIL_CAP_S,
        )
        # 2^attempt overflow protection — cap exponent at 10 so
        # an env-cranked max_attempts can't blow the math.
        exp = min(int(attempt), 10)
        return min(cap, base * (2 ** exp))


# ============================================================================
# Process singleton
# ============================================================================


_default_policy: Optional[ForegroundCooldownPolicy] = None
_default_lock = threading.Lock()


def get_default_policy() -> ForegroundCooldownPolicy:
    """Process singleton accessor. NEVER raises."""
    global _default_policy
    with _default_lock:
        if _default_policy is None:
            _default_policy = ForegroundCooldownPolicy()
        return _default_policy


def reset_default_policy() -> None:
    """For tests."""
    global _default_policy
    with _default_lock:
        _default_policy = None


# ============================================================================
# Cancellation-aware sleep
# ============================================================================


async def sleep_cooldown(
    backoff_s: float,
    *,
    op_id: str = "",
    label: str = "",
) -> bool:
    """Cancellation-aware async sleep for the cooldown window.

    Slice 12O Phase 3 cancellation discipline. This is a thin
    wrapper over ``asyncio.sleep`` which is natively
    cancellation-aware: when the parent task is cancelled (e.g.
    Layer-2 graceful shutdown nudge), this sleep wakes
    immediately and the ``CancelledError`` propagates so the
    asyncio cancel cascade can complete cleanly. The orchestrator
    is expected to catch ``CancelledError`` AT THE CALLER and
    record a terminal reason like
    ``cooldown_cancelled_shutdown`` before re-raising.

    Returns ``True`` on normal completion. Raises
    ``CancelledError`` on cancellation (propagated, not silently
    eaten — silent eating would break asyncio's cancel cascade
    contract and prevent clean WAL drain).

    ``op_id`` + ``label`` are logged for operator attribution —
    "is anything parked in cooldown right now?" is a critical
    question during a busy shutdown drain.
    """
    if backoff_s <= 0:
        return True
    started_at = time.monotonic()
    logger.info(
        "[ForegroundCooldown] sleeping op=%s label=%s backoff_s=%.1f",
        op_id or "?", label or "?", backoff_s,
    )
    try:
        await asyncio.sleep(backoff_s)
    except asyncio.CancelledError:
        elapsed = time.monotonic() - started_at
        logger.info(
            "[ForegroundCooldown] CANCELLED op=%s label=%s "
            "elapsed_s=%.1f remaining_s=%.1f — yielding to "
            "shutdown drain",
            op_id or "?", label or "?",
            elapsed, max(0.0, backoff_s - elapsed),
        )
        # Re-raise — caller (orchestrator) catches and transitions
        # the op to terminal with cooldown_cancelled_shutdown so
        # the asyncio cancel cascade can drain WAL + exit before
        # the WallClockWatchdog Layer-3 hard-kill fires.
        raise
    elapsed = time.monotonic() - started_at
    logger.info(
        "[ForegroundCooldown] resumed op=%s label=%s elapsed_s=%.1f",
        op_id or "?", label or "?", elapsed,
    )
    return True


__all__ = [
    "CooldownDecision",
    "CooldownReason",
    "ForegroundCooldownPolicy",
    "cooldown_enabled",
    "get_default_policy",
    "is_provider_exhaustion_cause",
    "reset_default_policy",
    "sleep_cooldown",
]

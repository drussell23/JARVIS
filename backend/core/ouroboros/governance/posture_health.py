"""Tier 1 #2 — PostureObserver task-death detection + safe-read wrapper.

Closes §28.5.1 v9 brutal review's worst silent-degradation cascade:

  ``posture_observer.py:558-572`` `_run_forever`: exception in
  ``run_one_cycle()`` is caught at line 565, increments
  ``_cycles_failed`` counter, logs once, **continues silent retry
  every 300s indefinitely**. No alarm callback, no fail-loud signal
  to orchestrator. Downstream consumers (sensor_governor,
  invariant_drift_observer's posture_reader, ide_observability_stream)
  call ``get_default_observer()`` and read **stale ``_store`` state**
  — they have no way to detect the task is dead-but-still-listed.

  Compound risk: combined with disk-full (which makes
  ``write_current()`` swallow at ``posture_store.py:326-328``),
  posture freezes at last-good reading; sensor_governor applies its
  weight against frozen posture; routing decisions made on stale
  state for hours/days.

This module is the missing detection + safe-read surface.

Design pillars:

  * **Asynchronous** — pure-data classifier runs synchronously inside
    consumer code paths. No new tasks, no new threads. Observer's
    own loop owns the heartbeat update; classifier just reads
    timestamps.

  * **Dynamic** — degraded threshold env-tunable as a multiplier of
    the observer's own ``observer_interval_s()`` so the threshold
    auto-scales when operators tune the cadence.

  * **Adaptive** — degraded state propagates through
    ``safe_load_posture()`` returning None; consumers fall back to
    safe defaults (1.0× cadence multiplier in
    ``invariant_drift_observer``, equivalent to MAINTAIN posture).

  * **Intelligent** — distinguishes 4 health states explicitly:
    HEALTHY / DEGRADED_HUNG / DEGRADED_FAILING / TASK_DEAD. No
    implicit "unknown" / no None — every input maps to exactly
    one outcome (J.A.R.M.A.T.R.I.X. discipline).

  * **Robust** — never raises; never blocks. Defensive everywhere.
    Best-effort SSE publish.

  * **No hardcoding** — every threshold env-tunable.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + ``posture_observer`` (read-only API surface)
    + ``posture_store`` (read-only API surface) + ``posture``
    (frozen dataclass) ONLY.
  * NEVER imports orchestrator / phase_runners / candidate_generator
    / iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / auto_action_router / subagent_scheduler /
    invariant_drift_*.
  * Never raises out of any public method.

Master flag default-false until graduation cadence:
``JARVIS_POSTURE_HEALTH_DETECTION_ENABLED``. When off:

  * ``evaluate_observer_health`` always returns HEALTHY (no-op).
  * ``safe_load_posture`` is byte-equivalent to
    ``store.load_current()`` — no degraded short-circuit.
  * SSE event never publishes.

  This is the safe revert path: turning detection off restores
  pre-Tier-1 behavior exactly.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


POSTURE_HEALTH_SCHEMA_VERSION: str = "posture_health.1"


# ---------------------------------------------------------------------------
# Env knobs — defaults overridable; never hardcoded behavior constants
# ---------------------------------------------------------------------------


_DEFAULT_DEGRADED_THRESHOLD_MULTIPLIER: float = 3.0
_DEGRADED_THRESHOLD_FLOOR: float = 1.5
_DEFAULT_FAILURE_STREAK_THRESHOLD: int = 3
_FAILURE_STREAK_FLOOR: int = 1
_DEFAULT_SSE_DEBOUNCE_S: float = 60.0
_SSE_DEBOUNCE_FLOOR_S: float = 5.0


def detection_enabled() -> bool:
    """``JARVIS_POSTURE_HEALTH_DETECTION_ENABLED`` (default ``false``
    until graduation cadence). Asymmetric env semantics: empty/
    whitespace = unset = current default; explicit truthy/falsy
    overrides at call time. Re-read on every public-API entry so
    flips hot-revert."""
    raw = os.environ.get(
        "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until graduation
    return raw in ("1", "true", "yes", "on")


def degraded_threshold_multiplier() -> float:
    """``JARVIS_POSTURE_HEALTH_DEGRADED_MULTIPLIER`` (default 3.0,
    floor 1.5). Multiplier applied to the observer's
    ``observer_interval_s()`` to derive the DEGRADED threshold —
    if the observer hasn't completed a successful cycle in this
    many intervals, classify DEGRADED. Auto-scales with operator
    cadence tuning (no hardcoded seconds)."""
    raw = os.environ.get(
        "JARVIS_POSTURE_HEALTH_DEGRADED_MULTIPLIER", "",
    ).strip()
    if not raw:
        return _DEFAULT_DEGRADED_THRESHOLD_MULTIPLIER
    try:
        return max(_DEGRADED_THRESHOLD_FLOOR, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_DEGRADED_THRESHOLD_MULTIPLIER


def failure_streak_threshold() -> int:
    """``JARVIS_POSTURE_HEALTH_FAILURE_STREAK_THRESHOLD`` (default 3,
    floor 1). N consecutive cycle failures classifies DEGRADED_FAILING
    independently of the time-since-last-OK threshold. Catches
    "task is running and not hung but every cycle is throwing"
    failure mode."""
    raw = os.environ.get(
        "JARVIS_POSTURE_HEALTH_FAILURE_STREAK_THRESHOLD", "",
    ).strip()
    if not raw:
        return _DEFAULT_FAILURE_STREAK_THRESHOLD
    try:
        return max(_FAILURE_STREAK_FLOOR, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_FAILURE_STREAK_THRESHOLD


def sse_debounce_s() -> float:
    """``JARVIS_POSTURE_HEALTH_SSE_DEBOUNCE_S`` (default 60s, floor
    5s). Minimum interval between repeated DEGRADED SSE fires.
    Prevents storm if a degraded observer is consulted from many
    consumers per second."""
    raw = os.environ.get(
        "JARVIS_POSTURE_HEALTH_SSE_DEBOUNCE_S", "",
    ).strip()
    if not raw:
        return _DEFAULT_SSE_DEBOUNCE_S
    try:
        return max(_SSE_DEBOUNCE_FLOOR_S, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SSE_DEBOUNCE_S


# ---------------------------------------------------------------------------
# Closed taxonomy of health states (J.A.R.M.A.T.R.I.X. discipline)
# ---------------------------------------------------------------------------


class PostureHealthStatus(str, enum.Enum):
    """Closed 4-value taxonomy. Every input maps to exactly one
    outcome — never None, never implicit fall-through. Mirrors
    Move 3 / Move 4 explicit-state discipline.

    HEALTHY            — observer running, recent OK cycle, no
                         consecutive failure streak. Posture state
                         can be trusted.
    DEGRADED_HUNG      — observer task is running but no OK cycle
                         in N × interval. Likely stuck in an await.
                         Posture state is stale; consumers should
                         fall back to safe defaults.
    DEGRADED_FAILING   — observer task is running and ticking BUT
                         consecutive cycle failures exceed threshold.
                         Cycles are completing-with-error; posture
                         state may be stale.
    TASK_DEAD          — observer task is None / done / not started.
                         No state is being updated. Posture state
                         is at-rest (could be from prior session)."""

    HEALTHY = "healthy"
    DEGRADED_HUNG = "degraded_hung"
    DEGRADED_FAILING = "degraded_failing"
    TASK_DEAD = "task_dead"


@dataclass(frozen=True)
class PostureHealthVerdict:
    """Result of one ``evaluate_observer_health`` call. Frozen for
    safe propagation."""

    status: PostureHealthStatus
    detail: str
    seconds_since_last_ok: Optional[float]
    consecutive_failures: int
    interval_s: float
    threshold_multiplier: float
    schema_version: str = POSTURE_HEALTH_SCHEMA_VERSION

    def is_degraded(self) -> bool:
        """True iff status is anything other than HEALTHY. Convenience
        for consumer fallback decisions."""
        return self.status is not PostureHealthStatus.HEALTHY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "detail": self.detail,
            "seconds_since_last_ok": self.seconds_since_last_ok,
            "consecutive_failures": self.consecutive_failures,
            "interval_s": self.interval_s,
            "threshold_multiplier": self.threshold_multiplier,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Pure classifier — operates on raw heartbeat snapshot
# ---------------------------------------------------------------------------


def evaluate_observer_health(
    snapshot: Dict[str, Any],
    *,
    interval_s: Optional[float] = None,
    now: Optional[float] = None,
) -> PostureHealthVerdict:
    """Classify observer health from a raw snapshot dict (as returned
    by ``PostureObserver.task_health_snapshot()``). Pure function;
    NEVER raises.

    Decision sequence (every input maps to exactly one outcome):

      1. Master flag off → HEALTHY (no-op revert path).
      2. Snapshot malformed (missing keys / wrong types) → HEALTHY
         (defensive — don't false-positive on bad input).
      3. ``not is_running and task_started and task_done`` → TASK_DEAD
         (task crashed entirely — exception escaped _run_forever's
         catch, which shouldn't happen but is defended against).
      4. ``not task_started`` → TASK_DEAD (start() never called).
      5. ``consecutive_cycle_failures >= failure_streak_threshold()``
         → DEGRADED_FAILING.
      6. ``last_cycle_ok_at_unix is None`` AND ``last_cycle_attempt_at
         _unix is not None`` AND elapsed > N × interval →
         DEGRADED_HUNG (cold-start: tried but never completed).
      7. ``last_cycle_ok_at_unix`` exists AND ``now - it > N ×
         interval`` → DEGRADED_HUNG.
      8. Otherwise → HEALTHY."""
    if not detection_enabled():
        return PostureHealthVerdict(
            status=PostureHealthStatus.HEALTHY,
            detail="detection master flag off",
            seconds_since_last_ok=None,
            consecutive_failures=0,
            interval_s=float(interval_s) if interval_s else 0.0,
            threshold_multiplier=degraded_threshold_multiplier(),
        )

    if not isinstance(snapshot, dict):
        return PostureHealthVerdict(
            status=PostureHealthStatus.HEALTHY,
            detail="malformed snapshot (not a dict)",
            seconds_since_last_ok=None,
            consecutive_failures=0,
            interval_s=float(interval_s) if interval_s else 0.0,
            threshold_multiplier=degraded_threshold_multiplier(),
        )

    try:
        is_running = bool(snapshot.get("is_running", False))
        task_started = bool(snapshot.get("task_started", False))
        task_done = bool(snapshot.get("task_done", False))
        last_ok = snapshot.get("last_cycle_ok_at_unix")
        last_attempt = snapshot.get("last_cycle_attempt_at_unix")
        consecutive_failures = int(
            snapshot.get("consecutive_cycle_failures", 0)
        )
    except (TypeError, ValueError):
        return PostureHealthVerdict(
            status=PostureHealthStatus.HEALTHY,
            detail="snapshot field type coercion failed",
            seconds_since_last_ok=None,
            consecutive_failures=0,
            interval_s=float(interval_s) if interval_s else 0.0,
            threshold_multiplier=degraded_threshold_multiplier(),
        )

    wall_now = float(now) if now is not None else time.time()
    threshold_mult = degraded_threshold_multiplier()
    streak_threshold = failure_streak_threshold()

    # Interval default — caller passes from observer_interval_s();
    # if missing, fall back to a defensive value that won't
    # false-positive (very large threshold).
    safe_interval = (
        float(interval_s) if interval_s and interval_s > 0
        else 60.0
    )
    seconds_since_last_ok: Optional[float] = None
    if isinstance(last_ok, (int, float)):
        seconds_since_last_ok = max(0.0, wall_now - float(last_ok))

    # Step 3-4: TASK_DEAD diagnostics
    if not task_started:
        return PostureHealthVerdict(
            status=PostureHealthStatus.TASK_DEAD,
            detail="task never started",
            seconds_since_last_ok=seconds_since_last_ok,
            consecutive_failures=consecutive_failures,
            interval_s=safe_interval,
            threshold_multiplier=threshold_mult,
        )
    if task_started and task_done and not is_running:
        return PostureHealthVerdict(
            status=PostureHealthStatus.TASK_DEAD,
            detail="task done (crashed or stopped)",
            seconds_since_last_ok=seconds_since_last_ok,
            consecutive_failures=consecutive_failures,
            interval_s=safe_interval,
            threshold_multiplier=threshold_mult,
        )

    # Step 5: failure streak
    if consecutive_failures >= streak_threshold:
        return PostureHealthVerdict(
            status=PostureHealthStatus.DEGRADED_FAILING,
            detail=(
                f"{consecutive_failures} consecutive cycle "
                f"failures (threshold {streak_threshold})"
            ),
            seconds_since_last_ok=seconds_since_last_ok,
            consecutive_failures=consecutive_failures,
            interval_s=safe_interval,
            threshold_multiplier=threshold_mult,
        )

    threshold_s = safe_interval * threshold_mult

    # Step 6: cold-start hang (attempted but never completed OK)
    if last_ok is None and isinstance(last_attempt, (int, float)):
        elapsed = max(0.0, wall_now - float(last_attempt))
        if elapsed >= threshold_s:
            return PostureHealthVerdict(
                status=PostureHealthStatus.DEGRADED_HUNG,
                detail=(
                    f"first cycle attempted {elapsed:.1f}s ago "
                    f"but never completed (threshold "
                    f"{threshold_s:.1f}s)"
                ),
                seconds_since_last_ok=None,
                consecutive_failures=consecutive_failures,
                interval_s=safe_interval,
                threshold_multiplier=threshold_mult,
            )

    # Step 7: stale-OK hang
    if (
        seconds_since_last_ok is not None
        and seconds_since_last_ok >= threshold_s
    ):
        return PostureHealthVerdict(
            status=PostureHealthStatus.DEGRADED_HUNG,
            detail=(
                f"last OK cycle {seconds_since_last_ok:.1f}s ago "
                f"(threshold {threshold_s:.1f}s)"
            ),
            seconds_since_last_ok=seconds_since_last_ok,
            consecutive_failures=consecutive_failures,
            interval_s=safe_interval,
            threshold_multiplier=threshold_mult,
        )

    # Step 8: HEALTHY
    return PostureHealthVerdict(
        status=PostureHealthStatus.HEALTHY,
        detail="observer running, recent OK cycle",
        seconds_since_last_ok=seconds_since_last_ok,
        consecutive_failures=consecutive_failures,
        interval_s=safe_interval,
        threshold_multiplier=threshold_mult,
    )


# ---------------------------------------------------------------------------
# Safe-read wrappers — what consumers should call instead of direct
# store.load_current().
# ---------------------------------------------------------------------------


def safe_load_posture(
    *,
    observer: Any = None,
    store: Any = None,
    interval_s: Optional[float] = None,
    now: Optional[float] = None,
) -> Optional[Any]:
    """Safe wrapper around ``PostureStore.load_current()`` that
    returns ``None`` when the observer is degraded — consumers fall
    back to safe defaults rather than reading frozen stale state.

    When detection master flag is off, this is byte-equivalent to
    ``store.load_current()`` (no degraded short-circuit). NEVER
    raises.

    Both ``observer`` and ``store`` accept ``None`` — a missing
    observer is treated as TASK_DEAD; a missing store returns None.
    Production callers pass both; tests inject."""
    if store is None:
        return None
    if not detection_enabled():
        # Master-off revert: pass-through to store.
        try:
            return store.load_current()
        except Exception:  # noqa: BLE001 — defensive
            return None
    if observer is None:
        # Detection on but no observer wired → assume degraded
        # (safe default).
        try:
            _maybe_publish_degraded_event(
                PostureHealthVerdict(
                    status=PostureHealthStatus.TASK_DEAD,
                    detail="no observer instance available",
                    seconds_since_last_ok=None,
                    consecutive_failures=0,
                    interval_s=float(interval_s) if interval_s
                    else 0.0,
                    threshold_multiplier=(
                        degraded_threshold_multiplier()
                    ),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            pass
        return None
    try:
        snapshot = observer.task_health_snapshot()
        verdict = evaluate_observer_health(
            snapshot, interval_s=interval_s, now=now,
        )
    except Exception:  # noqa: BLE001 — defensive
        # On any error in the health check itself, fall through to
        # store.load_current() — better to surface stale state than
        # hide the posture entirely on a transient classifier bug.
        try:
            return store.load_current()
        except Exception:  # noqa: BLE001 — defensive
            return None

    if verdict.is_degraded():
        try:
            _maybe_publish_degraded_event(verdict)
        except Exception:  # noqa: BLE001 — defensive
            pass
        return None

    try:
        return store.load_current()
    except Exception:  # noqa: BLE001 — defensive
        return None


def safe_load_posture_value(
    *,
    observer: Any = None,
    store: Any = None,
    interval_s: Optional[float] = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """Convenience wrapper returning posture string (e.g.,
    ``"EXPLORE"``) or ``None``. NEVER raises."""
    reading = safe_load_posture(
        observer=observer, store=store,
        interval_s=interval_s, now=now,
    )
    if reading is None:
        return None
    try:
        posture_attr = getattr(reading, "posture", None)
        if posture_attr is None:
            return None
        value = getattr(posture_attr, "value", None)
        return str(value) if value is not None else None
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# SSE event surface — best-effort, debounced
# ---------------------------------------------------------------------------


EVENT_TYPE_POSTURE_OBSERVER_DEGRADED: str = (
    "posture_observer_degraded"
)


_last_publish_at_unix_lock = threading.Lock()
_last_publish_at_unix: float = 0.0


def _maybe_publish_degraded_event(
    verdict: PostureHealthVerdict,
) -> Optional[str]:
    """Fire the degraded SSE event subject to debounce. Best-effort;
    NEVER raises. Returns broker frame_id on publish, None on
    suppression / failure."""
    global _last_publish_at_unix
    if not detection_enabled():
        return None
    if verdict.status is PostureHealthStatus.HEALTHY:
        return None  # never fire HEALTHY
    now = time.time()
    debounce = sse_debounce_s()
    with _last_publish_at_unix_lock:
        if (
            _last_publish_at_unix > 0
            and (now - _last_publish_at_unix) < debounce
        ):
            return None
        _last_publish_at_unix = now
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            get_default_broker,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_POSTURE_OBSERVER_DEGRADED,
            op_id="posture_observer",
            payload=verdict.to_dict(),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[PostureHealth] degraded publish swallowed",
            exc_info=True,
        )
        return None


def reset_publish_debounce_for_tests() -> None:
    """Test isolation — reset the debounce timer."""
    global _last_publish_at_unix
    with _last_publish_at_unix_lock:
        _last_publish_at_unix = 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EVENT_TYPE_POSTURE_OBSERVER_DEGRADED",
    "POSTURE_HEALTH_SCHEMA_VERSION",
    "PostureHealthStatus",
    "PostureHealthVerdict",
    "degraded_threshold_multiplier",
    "detection_enabled",
    "evaluate_observer_health",
    "failure_streak_threshold",
    "reset_publish_debounce_for_tests",
    "safe_load_posture",
    "safe_load_posture_value",
    "sse_debounce_s",
]

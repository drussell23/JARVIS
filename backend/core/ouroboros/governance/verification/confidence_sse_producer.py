"""Tier 1 #1 — Confidence Drop SSE Producer wiring.

The publish helpers exist in ``verification/confidence_observability.py``:

  * ``publish_confidence_drop_event``        (P1 — BELOW_FLOOR)
  * ``publish_confidence_approaching_event`` (P2 — APPROACHING_FLOOR)
  * ``publish_sustained_low_confidence_event`` (P3 — sustained trend)

The SSE event types exist in ``ide_observability_stream.py:142-144``:

  * ``EVENT_TYPE_MODEL_CONFIDENCE_DROP``
  * ``EVENT_TYPE_MODEL_CONFIDENCE_APPROACHING``
  * ``EVENT_TYPE_MODEL_SUSTAINED_LOW_CONFIDENCE``

What was missing: **the caller that fires these helpers on verdict
state transitions**. v9 brutal review §28.4 found the dictionary
defined and zero producers wired. This module is that producer.

Design pillars (per the operator directive):

  * **Asynchronous** — best-effort publish in the streaming hot path;
    lazy broker import via the existing publishers; never blocks
    token generation.

  * **Dynamic** — state-transition-fire (only on rising edges
    OK→APPROACHING / OK→BELOW / APPROACHING→BELOW); per-token
    duplicate verdicts emit nothing. Otherwise every BELOW_FLOOR
    token would storm the SSE broker.

  * **Adaptive** — per-op rate-limit (env-tunable
    ``JARVIS_CONFIDENCE_SSE_MIN_INTERVAL_S``, default 1.0s, floor
    0.05s). Sustained-low milestone fires every ``threshold``
    consecutive BELOW_FLOOR ticks (env-tunable, default 5, floor 2).
    Repeated sustained episodes get repeated milestone fires
    (5, 10, 15, ...) so escalating concern is visible to operators.

  * **Intelligent** — state-transition signature dedup mirrors
    Move 4's drift signature ring. Same verdict twice in a row at
    the same op_id never re-fires. Recovery (BELOW→OK, APPROACHING→OK)
    resets the consecutive counter and the sustained-fired marker.

  * **Robust** — never raises out of any public method. Broker
    missing / publish error / observability disabled / malformed
    verdict / ring-full eviction all produce defined outcomes.
    Bounded op-ring (env-tunable
    ``JARVIS_CONFIDENCE_SSE_OP_RING_SIZE``, default 256, floor 16);
    oldest op evicted when full.

  * **No hardcoding** — every threshold env-tunable; defaults are
    operator-overridable, not magic constants.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + ``verification.confidence_monitor`` (Verdict
    enum) + ``verification.confidence_observability`` (publishers)
    ONLY.
  * NEVER imports orchestrator / phase_runners / candidate_generator
    / iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / auto_action_router / subagent_scheduler.
  * Never raises. Never blocks. Never persists state across
    processes.

Master flag default-false until graduation cadence:
``JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED``. Asymmetric env
semantics: empty/whitespace = unset = current default; explicit
truthy/falsy overrides at call time.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Optional,
)

from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
    ConfidenceVerdict,
)
from backend.core.ouroboros.governance.verification.confidence_observability import (  # noqa: E501
    publish_confidence_approaching_event,
    publish_confidence_drop_event,
    publish_sustained_low_confidence_event,
)

logger = logging.getLogger(__name__)


CONFIDENCE_SSE_PRODUCER_SCHEMA_VERSION: str = "confidence_sse_producer.1"


# ---------------------------------------------------------------------------
# Env knobs — defaults overridable, never hardcoded behavior constants
# ---------------------------------------------------------------------------


_DEFAULT_MIN_INTERVAL_S: float = 1.0
_INTERVAL_FLOOR_S: float = 0.05
_DEFAULT_SUSTAINED_LOW_THRESHOLD: int = 5
_SUSTAINED_LOW_FLOOR: int = 2
_DEFAULT_OP_RING_SIZE: int = 256
_OP_RING_FLOOR: int = 16


def producer_enabled() -> bool:
    """``JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED`` (default ``false``
    until graduation).

    Asymmetric semantics: empty/whitespace = unset = current default;
    explicit ``0`` / ``false`` / ``no`` / ``off`` evaluates false;
    explicit truthy values evaluate true. Re-read on every call so
    flips hot-revert."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until graduation
    return raw in ("1", "true", "yes", "on")


def min_interval_s() -> float:
    """``JARVIS_CONFIDENCE_SSE_MIN_INTERVAL_S`` (default 1.0s, floor
    0.05s). Per-op minimum interval between SSE fires; protects the
    broker from storm during wobbly streaming generations."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_SSE_MIN_INTERVAL_S", "",
    ).strip()
    if not raw:
        return _DEFAULT_MIN_INTERVAL_S
    try:
        return max(_INTERVAL_FLOOR_S, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_INTERVAL_S


def sustained_low_threshold() -> int:
    """``JARVIS_CONFIDENCE_SSE_SUSTAINED_LOW_THRESHOLD`` (default 5,
    floor 2). Number of consecutive BELOW_FLOOR ticks required to
    fire one P3 sustained-low milestone. Subsequent milestones fire
    at multiples (10, 15, 20, ...) so escalating concern is visible."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_SSE_SUSTAINED_LOW_THRESHOLD", "",
    ).strip()
    if not raw:
        return _DEFAULT_SUSTAINED_LOW_THRESHOLD
    try:
        return max(_SUSTAINED_LOW_FLOOR, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SUSTAINED_LOW_THRESHOLD


def op_ring_size() -> int:
    """``JARVIS_CONFIDENCE_SSE_OP_RING_SIZE`` (default 256, floor 16).
    Maximum number of ops tracked simultaneously; oldest evicted when
    ring fills. Bounds memory."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_SSE_OP_RING_SIZE", "",
    ).strip()
    if not raw:
        return _DEFAULT_OP_RING_SIZE
    try:
        return max(_OP_RING_FLOOR, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_OP_RING_SIZE


# ---------------------------------------------------------------------------
# Closed taxonomy of fire decisions (J.A.R.M.A.T.R.I.X. discipline)
# ---------------------------------------------------------------------------


class FireDecision(str, enum.Enum):
    """Closed 6-value taxonomy of why an observe_verdict call did or
    did not fire an SSE event. Mirrors Move 3's AdvisoryActionType /
    Move 4's BootSnapshotOutcome explicit-state discipline."""

    FIRED_DROP = "fired_drop"             # P1 — fresh BELOW_FLOOR transition
    FIRED_APPROACHING = "fired_approaching"  # P2 — fresh APPROACHING transition
    FIRED_SUSTAINED = "fired_sustained"   # P3 — sustained-low milestone
    SUPPRESSED_DISABLED = "suppressed_disabled"     # master flag off
    SUPPRESSED_RATE_LIMITED = "suppressed_rate_limited"
    SUPPRESSED_NO_TRANSITION = "suppressed_no_transition"  # same verdict, no rising edge


# ---------------------------------------------------------------------------
# Per-op tracking state (mutable — single-process, lock-protected)
# ---------------------------------------------------------------------------


@dataclass
class _OpState:
    """Per-op tracker state. NOT frozen — mutated on each observation.
    Carried in a lock-protected dict on the singleton tracker."""

    last_verdict: ConfidenceVerdict = ConfidenceVerdict.OK
    consecutive_below: int = 0
    last_emit_at_unix: float = 0.0
    last_sustained_milestone: int = 0  # last consecutive_below at which
                                       # sustained event was fired


# ---------------------------------------------------------------------------
# Frozen result (caller-friendly, propagatable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of one ``observe_verdict`` call. Frozen so callers
    can propagate it through async signal bridges without aliasing
    concerns. Mirrors Move 4's ``ObserverTickResult`` shape."""

    op_id: str
    prior_verdict: str
    current_verdict: str
    decision: FireDecision
    consecutive_below: int
    fired_event_type: Optional[str] = None  # SSE event_type when fired
    schema_version: str = CONFIDENCE_SSE_PRODUCER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "prior_verdict": self.prior_verdict,
            "current_verdict": self.current_verdict,
            "decision": self.decision.value,
            "consecutive_below": self.consecutive_below,
            "fired_event_type": self.fired_event_type,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Pluggable publisher set — for tests + future extension
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PublisherSet:
    """Three publishers bundled for injection. Production wires the
    real publishers from confidence_observability; tests inject
    capturing fakes."""

    publish_drop: Callable[..., Optional[str]]
    publish_approaching: Callable[..., Optional[str]]
    publish_sustained: Callable[..., Optional[str]]


_DEFAULT_PUBLISHERS: _PublisherSet = _PublisherSet(
    publish_drop=publish_confidence_drop_event,
    publish_approaching=publish_confidence_approaching_event,
    publish_sustained=publish_sustained_low_confidence_event,
)


# ---------------------------------------------------------------------------
# The tracker — per-op verdict transition machine
# ---------------------------------------------------------------------------


class ConfidenceTransitionTracker:
    """Tracks per-op confidence verdict state; fires SSE events on
    rising-edge transitions and sustained-low milestones.

    Lifecycle: pure in-process state, lock-protected. Never persists.
    Bounded by ``op_ring_size()`` — oldest op evicted when ring fills.

    Public API:

      * ``observe_verdict(op_id, verdict, ...)`` — main entry
      * ``stats()`` — observability snapshot
      * ``reset_op(op_id)`` — clear state for one op (e.g., on op end)
      * ``clear_all_for_tests()`` — full reset

    Injectable publishers via ``__init__`` for tests; production
    callers pass none and get the real ``confidence_observability``
    publishers. NEVER raises."""

    def __init__(
        self,
        *,
        publishers: Optional[_PublisherSet] = None,
    ) -> None:
        self._publishers = (
            publishers if publishers is not None else _DEFAULT_PUBLISHERS
        )
        self._states: Dict[str, _OpState] = {}
        self._op_order: Deque[str] = deque()
        self._lock = threading.Lock()
        # Stats counters
        self._total_observations = 0
        self._fired_drop = 0
        self._fired_approaching = 0
        self._fired_sustained = 0
        self._suppressed_rate_limited = 0
        self._suppressed_no_transition = 0
        self._suppressed_disabled = 0
        self._evicted_ops = 0

    # ---- core entry point ------------------------------------------------

    def observe_verdict(
        self,
        *,
        op_id: str,
        verdict: ConfidenceVerdict,
        rolling_margin: Optional[float] = None,
        floor: Optional[float] = None,
        effective_floor: Optional[float] = None,
        posture: Optional[str] = None,
        window_size: Optional[int] = None,
        observations_count: Optional[int] = None,
        provider: Optional[str] = None,
        model_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> TransitionResult:
        """Observe one verdict for ``op_id``; fire SSE if the
        transition warrants. NEVER raises.

        Decision sequence:
          1. Master flag off → ``SUPPRESSED_DISABLED``.
          2. Malformed verdict (not a ``ConfidenceVerdict`` enum) →
             treated as OK (defensive — no-op transition).
          3. Same verdict as prior + not at sustained-low milestone →
             ``SUPPRESSED_NO_TRANSITION``.
          4. Rate-limit gate (last_emit < min_interval) → unless this
             is a sustained-low milestone (operators must see
             escalation), suppress with ``SUPPRESSED_RATE_LIMITED``.
          5. Verdict == BELOW_FLOOR + consecutive_below at multiple of
             sustained_threshold → ``FIRED_SUSTAINED``.
          6. Verdict == BELOW_FLOOR + prior != BELOW_FLOOR →
             ``FIRED_DROP``.
          7. Verdict == APPROACHING_FLOOR + prior != APPROACHING and
             not BELOW → ``FIRED_APPROACHING``.
          8. Otherwise (recovery / dampened transition) →
             ``SUPPRESSED_NO_TRANSITION``."""
        # 1. Master flag
        if not producer_enabled():
            with self._lock:
                self._total_observations += 1
                self._suppressed_disabled += 1
            return TransitionResult(
                op_id=str(op_id),
                prior_verdict=ConfidenceVerdict.OK.value,
                current_verdict=(
                    verdict.value
                    if isinstance(verdict, ConfidenceVerdict)
                    else ConfidenceVerdict.OK.value
                ),
                decision=FireDecision.SUPPRESSED_DISABLED,
                consecutive_below=0,
            )

        # 2. Defensive verdict normalization
        if not isinstance(verdict, ConfidenceVerdict):
            with self._lock:
                self._total_observations += 1
                self._suppressed_no_transition += 1
            return TransitionResult(
                op_id=str(op_id),
                prior_verdict=ConfidenceVerdict.OK.value,
                current_verdict=ConfidenceVerdict.OK.value,
                decision=FireDecision.SUPPRESSED_NO_TRANSITION,
                consecutive_below=0,
            )

        safe_op_id = str(op_id) if op_id else "unknown"
        wall_now = float(now) if now is not None else time.time()

        # State transition compute (lock-held)
        with self._lock:
            self._total_observations += 1
            state = self._get_or_create_state(safe_op_id)
            prior = state.last_verdict

            # Update consecutive_below counter
            if verdict is ConfidenceVerdict.BELOW_FLOOR:
                state.consecutive_below += 1
            else:
                state.consecutive_below = 0
                state.last_sustained_milestone = 0

            # Decision tree
            decision = self._decide_fire(state, prior, verdict, wall_now)

            # Update state for next observation
            state.last_verdict = verdict

            # Track stats + capture state at decision time so we can
            # release lock before publishing
            consecutive_below_snapshot = state.consecutive_below
            if decision is FireDecision.SUPPRESSED_NO_TRANSITION:
                self._suppressed_no_transition += 1
            elif decision is FireDecision.SUPPRESSED_RATE_LIMITED:
                self._suppressed_rate_limited += 1
            elif decision is FireDecision.FIRED_DROP:
                self._fired_drop += 1
                state.last_emit_at_unix = wall_now
            elif decision is FireDecision.FIRED_APPROACHING:
                self._fired_approaching += 1
                state.last_emit_at_unix = wall_now
            elif decision is FireDecision.FIRED_SUSTAINED:
                self._fired_sustained += 1
                state.last_emit_at_unix = wall_now
                state.last_sustained_milestone = (
                    state.consecutive_below
                )

        # Publish OUTSIDE the lock so a slow broker doesn't block
        # other observers. Defensive — publisher exceptions swallowed.
        fired_event_type: Optional[str] = None
        if decision is FireDecision.FIRED_DROP:
            fired_event_type = "model_confidence_drop"
            self._safe_publish_drop(
                verdict=verdict,
                rolling_margin=rolling_margin,
                floor=floor,
                effective_floor=effective_floor,
                posture=posture,
                window_size=window_size,
                observations_count=observations_count,
                op_id=safe_op_id,
                provider=provider,
                model_id=model_id,
            )
        elif decision is FireDecision.FIRED_APPROACHING:
            fired_event_type = "model_confidence_approaching"
            self._safe_publish_approaching(
                verdict=verdict,
                rolling_margin=rolling_margin,
                floor=floor,
                effective_floor=effective_floor,
                posture=posture,
                window_size=window_size,
                observations_count=observations_count,
                op_id=safe_op_id,
                provider=provider,
                model_id=model_id,
            )
        elif decision is FireDecision.FIRED_SUSTAINED:
            fired_event_type = "model_sustained_low_confidence"
            # P3 publisher has different signature — translate
            self._safe_publish_sustained(
                op_count_in_window=consecutive_below_snapshot,
                low_confidence_count=consecutive_below_snapshot,
                rate=1.0,  # entire window is BELOW_FLOOR by definition
                posture=posture,
                provider=provider,
                model_id=model_id,
            )

        return TransitionResult(
            op_id=safe_op_id,
            prior_verdict=prior.value,
            current_verdict=verdict.value,
            decision=decision,
            consecutive_below=consecutive_below_snapshot,
            fired_event_type=fired_event_type,
        )

    # ---- decision logic --------------------------------------------------

    def _decide_fire(
        self,
        state: _OpState,
        prior: ConfidenceVerdict,
        verdict: ConfidenceVerdict,
        wall_now: float,
    ) -> FireDecision:
        """Pure decision function — assumes lock is held."""
        # Sustained-low milestone — bypasses rate-limit
        if (
            verdict is ConfidenceVerdict.BELOW_FLOOR
            and state.consecutive_below > 0
            and state.consecutive_below >= sustained_low_threshold()
            and state.consecutive_below
            != state.last_sustained_milestone
            and (
                state.consecutive_below
                % sustained_low_threshold()
                == 0
            )
        ):
            return FireDecision.FIRED_SUSTAINED

        # Rate-limit gate for non-milestone fires
        if state.last_emit_at_unix > 0:
            interval = wall_now - state.last_emit_at_unix
            if interval < min_interval_s():
                # If this would otherwise be a fire, suppress it
                if self._would_fire(prior, verdict):
                    return FireDecision.SUPPRESSED_RATE_LIMITED

        # Rising-edge transitions
        if (
            verdict is ConfidenceVerdict.BELOW_FLOOR
            and prior is not ConfidenceVerdict.BELOW_FLOOR
        ):
            return FireDecision.FIRED_DROP

        if (
            verdict is ConfidenceVerdict.APPROACHING_FLOOR
            and prior is not ConfidenceVerdict.APPROACHING_FLOOR
            and prior is not ConfidenceVerdict.BELOW_FLOOR
        ):
            return FireDecision.FIRED_APPROACHING

        return FireDecision.SUPPRESSED_NO_TRANSITION

    @staticmethod
    def _would_fire(
        prior: ConfidenceVerdict,
        verdict: ConfidenceVerdict,
    ) -> bool:
        """True iff the transition would fire if rate-limit didn't
        suppress. Used for stats accuracy."""
        if (
            verdict is ConfidenceVerdict.BELOW_FLOOR
            and prior is not ConfidenceVerdict.BELOW_FLOOR
        ):
            return True
        if (
            verdict is ConfidenceVerdict.APPROACHING_FLOOR
            and prior is ConfidenceVerdict.OK
        ):
            return True
        return False

    # ---- defensive publish wrappers --------------------------------------

    def _safe_publish_drop(self, **kwargs: Any) -> None:
        try:
            self._publishers.publish_drop(**kwargs)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ConfidenceSSEProducer] publish_drop swallowed",
                exc_info=True,
            )

    def _safe_publish_approaching(self, **kwargs: Any) -> None:
        try:
            self._publishers.publish_approaching(**kwargs)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ConfidenceSSEProducer] publish_approaching "
                "swallowed", exc_info=True,
            )

    def _safe_publish_sustained(self, **kwargs: Any) -> None:
        try:
            self._publishers.publish_sustained(**kwargs)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[ConfidenceSSEProducer] publish_sustained swallowed",
                exc_info=True,
            )

    # ---- op state management --------------------------------------------

    def _get_or_create_state(self, op_id: str) -> _OpState:
        """Return state for ``op_id``, creating + tracking + ring-
        evicting as needed. Lock must be held."""
        state = self._states.get(op_id)
        if state is not None:
            return state
        # Ring eviction — oldest first
        ring_max = op_ring_size()
        while len(self._states) >= ring_max and self._op_order:
            evict = self._op_order.popleft()
            if evict in self._states:
                del self._states[evict]
                self._evicted_ops += 1
        new_state = _OpState()
        self._states[op_id] = new_state
        self._op_order.append(op_id)
        return new_state

    def reset_op(self, op_id: str) -> bool:
        """Drop state for one op (typically called at op-end). Returns
        True if state existed. NEVER raises."""
        try:
            with self._lock:
                if op_id in self._states:
                    del self._states[op_id]
                    try:
                        self._op_order.remove(op_id)
                    except ValueError:
                        pass
                    return True
                return False
        except Exception:  # noqa: BLE001 — defensive
            return False

    def clear_all_for_tests(self) -> None:
        """Test-only — full reset of internal state."""
        with self._lock:
            self._states.clear()
            self._op_order.clear()
            self._total_observations = 0
            self._fired_drop = 0
            self._fired_approaching = 0
            self._fired_sustained = 0
            self._suppressed_rate_limited = 0
            self._suppressed_no_transition = 0
            self._suppressed_disabled = 0
            self._evicted_ops = 0

    # ---- diagnostics ----------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Snapshot of internal counters. NEVER raises."""
        with self._lock:
            return {
                "schema_version": (
                    CONFIDENCE_SSE_PRODUCER_SCHEMA_VERSION
                ),
                "total_observations": self._total_observations,
                "fired_drop": self._fired_drop,
                "fired_approaching": self._fired_approaching,
                "fired_sustained": self._fired_sustained,
                "suppressed_rate_limited": (
                    self._suppressed_rate_limited
                ),
                "suppressed_no_transition": (
                    self._suppressed_no_transition
                ),
                "suppressed_disabled": self._suppressed_disabled,
                "evicted_ops": self._evicted_ops,
                "tracked_ops": len(self._states),
                "ring_capacity": op_ring_size(),
                "min_interval_s": min_interval_s(),
                "sustained_low_threshold": (
                    sustained_low_threshold()
                ),
            }


# ---------------------------------------------------------------------------
# Default singleton (mirrors Move 4 store / observer pattern)
# ---------------------------------------------------------------------------


_default_tracker: Optional[ConfidenceTransitionTracker] = None
_default_tracker_lock = threading.Lock()


def get_default_tracker() -> ConfidenceTransitionTracker:
    """Singleton default tracker. NEVER raises."""
    global _default_tracker
    with _default_tracker_lock:
        if _default_tracker is None:
            _default_tracker = ConfidenceTransitionTracker()
        return _default_tracker


def reset_default_tracker_for_tests() -> None:
    """Test isolation — drop the singleton."""
    global _default_tracker
    with _default_tracker_lock:
        _default_tracker = None


# ---------------------------------------------------------------------------
# Convenience entry point — for the doubleword_provider streaming hot
# path. One-line caller surface that handles all defensive concerns.
# ---------------------------------------------------------------------------


def observe_streaming_verdict(
    *,
    op_id: Any,
    verdict: Any,
    rolling_margin: Any = None,
    floor: Any = None,
    effective_floor: Any = None,
    posture: Any = None,
    window_size: Any = None,
    observations_count: Any = None,
    provider: Any = None,
    model_id: Any = None,
) -> Optional[TransitionResult]:
    """One-line caller surface for streaming providers. Handles all
    type coercion + master-flag check + ring management + publishes
    via the singleton tracker. NEVER raises.

    Returns the TransitionResult on success; None on any defensive
    short-circuit (e.g., invalid op_id type, exception in the
    tracker)."""
    try:
        if not isinstance(verdict, ConfidenceVerdict):
            return None
        op_id_str = str(op_id) if op_id is not None else ""
        if not op_id_str:
            return None
        return get_default_tracker().observe_verdict(
            op_id=op_id_str,
            verdict=verdict,
            rolling_margin=rolling_margin,
            floor=floor,
            effective_floor=effective_floor,
            posture=posture,
            window_size=window_size,
            observations_count=observations_count,
            provider=provider,
            model_id=model_id,
        )
    except Exception:  # noqa: BLE001 — defensive last-resort
        logger.debug(
            "[ConfidenceSSEProducer] observe_streaming_verdict "
            "swallowed", exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_SSE_PRODUCER_SCHEMA_VERSION",
    "ConfidenceTransitionTracker",
    "FireDecision",
    "TransitionResult",
    "get_default_tracker",
    "min_interval_s",
    "observe_streaming_verdict",
    "op_ring_size",
    "producer_enabled",
    "reset_default_tracker_for_tests",
    "sustained_low_threshold",
]

"""SensorGovernor — posture-weighted op-emission cap across the 16 sensors.

Closes the "truly unattended" gap. Before any sensor emits an op, it
calls ``governor.request_budget(sensor_name, urgency)`` and receives
a :class:`BudgetDecision`. The governor tracks a rolling-window of
emissions per sensor + globally, and returns ``allowed=False`` when
the posture-weighted cap is exhausted.

Authority posture
-----------------

* §1 Boundary Principle — **advisory only, zero execution authority**.
  The governor returns a decision; the sensor CHOOSES to honor it.
  Enforcement wiring (intake router consulting the governor before
  routing) is Slice 5 deferred work.
* §5 Tier 0 — pure dict + deque + threading.Lock; no LLM, no network,
  no disk I/O.
* §8 Observability — every denial is loggable + SSE-publishable (via
  Slice 3 bridge); ``snapshot()`` exposes the full state machine for
  ``/governor status``.

Authority invariant (grep-pinned Slice 4): zero imports from
``orchestrator``, ``policy``, ``iron_gate``, ``risk_tier``,
``change_engine``, ``candidate_generator``, ``gate``.

Kill switch
-----------

``JARVIS_SENSOR_GOVERNOR_ENABLED`` (default ``false`` Slice 1, graduates
Slice 4). When off, ``request_budget()`` always returns
``allowed=True`` with ``reason_code="governor.disabled"`` so sensors
fall through to unconstrained emission (the pre-governor status quo).

Per-posture weighting
---------------------

The current StrategicPosture (from Wave 1 #1 DirectionInferrer) is read
via an injectable callable. For each sensor:

  weighted_cap = base_cap_per_hour * posture_weight(posture, sensor)
                                   * urgency_multiplier(urgency)

Emergency brake: when the DirectionInferrer's current signal bundle
reports ``cost_burn_normalized > 0.9`` OR ``postmortem_failure_rate >
0.6``, all weighted caps are multiplied by ``_EMERGENCY_REDUCTION_PCT``
(default 0.2). The brake is a *soft* cut — it squeezes budgets but
doesn't hard-zero them.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


SENSOR_GOVERNOR_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Master switch.

    Default: **``true``** (graduated 2026-04-21 via Slice 4 after
    Slices 1-3 shipped primitive + 16-sensor seed + /governor REPL +
    GET /observability/governor + SSE throttle/brake/memory_pressure
    events with 130 governance tests + 3 live-fire proofs). Explicit
    ``"false"`` reverts to the Slice 1 deny-by-default posture — every
    surface disables in lockstep:

      * request_budget() returns ``allowed=True`` with reason
        ``"governor.disabled"`` so sensors fall through to the
        pre-governor path (no throttling)
      * record_emission() is a no-op
      * /governor REPL rejects operational verbs (help still works)
      * GET /observability/governor{,/history} return 403
      * SSE publish_governor_* helpers return None

    The rolling-window counters, posture-weight math, emergency-brake
    thresholds, authority invariants (grep-pinned), and §5 Tier 0
    discipline remain in force regardless of this flag — graduation
    flips opt-in friction, NOT authority surface.
    """
    return _env_bool("JARVIS_SENSOR_GOVERNOR_ENABLED", True)


def global_cap_per_hour() -> int:
    """Ceiling across all sensors combined. Runs under even an otherwise
    permissive per-sensor posture weight."""
    return _env_int("JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR", 200, minimum=1)


def window_seconds() -> int:
    """Sliding-window duration for rate calculation. Default 3600s (1h).
    Lower values (e.g. 900s / 15min) produce quicker reactivity at the
    cost of noisier per-sensor caps."""
    return _env_int("JARVIS_SENSOR_GOVERNOR_WINDOW_S", 3600, minimum=60)


def emergency_reduction_pct() -> float:
    """Multiplier applied to all weighted caps when the emergency brake
    fires. Default 0.2 = 20% of normal. Lower → harsher brake; higher
    → softer brake."""
    raw = _env_float(
        "JARVIS_SENSOR_GOVERNOR_EMERGENCY_REDUCTION_PCT", 0.2, minimum=0.01,
    )
    return min(1.0, raw)


def emergency_cost_threshold() -> float:
    """cost_burn_normalized signal above which the emergency brake fires."""
    raw = _env_float(
        "JARVIS_SENSOR_GOVERNOR_EMERGENCY_COST_THRESHOLD", 0.9, minimum=0.0,
    )
    return min(1.0, raw)


def emergency_postmortem_threshold() -> float:
    """postmortem_failure_rate signal above which the emergency brake fires."""
    raw = _env_float(
        "JARVIS_SENSOR_GOVERNOR_EMERGENCY_POSTMORTEM_THRESHOLD", 0.6,
        minimum=0.0,
    )
    return min(1.0, raw)


def topology_backpressure_enabled() -> bool:
    """Slice 3c — TopologySentinel-aware throttle for low-urgency
    sensors. ``JARVIS_TOPOLOGY_BACKPRESSURE_ENABLED`` (default
    ``true``). When on, a SensorGovernor whose injected
    ``topology_state_fn`` reports any DW endpoint blocked applies an
    additional multiplier to the weighted cap of BACKGROUND and
    SPECULATIVE urgency requests. IMMEDIATE/STANDARD/COMPLEX caps
    are untouched (those routes can fall back to Claude). Hot-revert:
    ``export JARVIS_TOPOLOGY_BACKPRESSURE_ENABLED=false`` returns the
    cap math to posture × urgency × brake (the pre-3c formula)."""
    return _env_bool("JARVIS_TOPOLOGY_BACKPRESSURE_ENABLED", True)


def topology_backpressure_mult() -> float:
    """Multiplier applied to BG/SPEC weighted caps when topology is
    blocked. Default 0.2 = throttle to 20% of the unblocked cap.
    ``JARVIS_TOPOLOGY_BACKPRESSURE_MULT``. Floor at 0.0 (full halt
    is allowed); ceiling at 1.0 (no-op upper bound)."""
    raw = _env_float(
        "JARVIS_TOPOLOGY_BACKPRESSURE_MULT", 0.2, minimum=0.0,
    )
    return min(1.0, raw)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Urgency(str, enum.Enum):
    """Mirrors the UrgencyRouter vocabulary (§5 Tier routing) so sensors
    can pass their own urgency classification in without translating."""
    IMMEDIATE = "immediate"
    STANDARD = "standard"
    COMPLEX = "complex"
    BACKGROUND = "background"
    SPECULATIVE = "speculative"


_DEFAULT_URGENCY_MULTIPLIERS: Mapping[Urgency, float] = {
    Urgency.IMMEDIATE: 2.0,
    Urgency.STANDARD: 1.0,
    Urgency.COMPLEX: 0.8,
    Urgency.BACKGROUND: 0.5,
    Urgency.SPECULATIVE: 0.3,
}


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensorBudgetSpec:
    """Frozen budget descriptor for one sensor.

    ``posture_weights`` maps posture-value strings (e.g. ``"HARDEN"``)
    to float multipliers applied on top of ``base_cap_per_hour``.
    Missing posture → default multiplier 1.0. Same string-key pattern
    as FlagRegistry posture_relevance — we don't import the Posture
    enum to stay decoupled from Wave 1 #1's runtime surface.

    M9 Slice 3 (PRD §30.5.1): ``curiosity_aware`` is the per-sensor
    opt-in for CuriosityGradient bias. When True (and the M9 master
    flag is on, and the caller supplies a ``cluster_id`` to
    :meth:`SensorGovernor.request_budget`), the weighted-cap formula
    composes a ``curiosity_multiplier`` from the
    :mod:`curiosity_collector` snapshot. Default ``False`` keeps
    every existing sensor byte-identical (zero behavior change for
    Slice 5 graduation). Bias is opt-in, not blanket.
    """

    sensor_name: str
    base_cap_per_hour: int
    posture_weights: Mapping[str, float] = field(default_factory=dict)
    urgency_multipliers: Mapping[str, float] = field(default_factory=dict)
    description: str = ""
    curiosity_aware: bool = False

    def weight_for_posture(self, posture: Optional[str]) -> float:
        if not posture:
            return 1.0
        return float(self.posture_weights.get(posture.upper(), 1.0))

    def urgency_mult(self, urgency: Urgency) -> float:
        # Instance-level override, else default table, else 1.0
        override = self.urgency_multipliers.get(urgency.value)
        if override is not None:
            return float(override)
        return float(_DEFAULT_URGENCY_MULTIPLIERS.get(urgency, 1.0))


@dataclass(frozen=True)
class BudgetDecision:
    """Result of ``request_budget()``."""

    allowed: bool
    sensor_name: str
    urgency: Urgency
    posture: Optional[str]
    weighted_cap: int
    current_count: int
    remaining: int
    reason_code: str
    emergency_brake: bool = False
    global_cap: int = 0
    global_count: int = 0
    # Slice 3c — set when topology-backpressure factor was applied to
    # this sensor's weighted_cap (DW blocked + urgency in BG/SPEC).
    # Independent of `allowed` — the cap may still leave headroom even
    # under backpressure; the field is purely observability so SSE
    # consumers can distinguish "throttled by topology" from "throttled
    # by capacity".
    topology_blocked: bool = False
    # M9 Slice 3 — curiosity multiplier applied to the weighted_cap
    # via the CuriosityGradient consumer. ``1.0`` means no bias
    # (sensor not curiosity-aware OR M9 master off OR cold-start
    # OR collector unavailable). > 1.0 amplifies high-curiosity
    # clusters; < 1.0 throttles low-curiosity ones. Bounded by
    # construction at the M9 primitive layer
    # (curiosity_multiplier_floor / ceiling clamps).
    curiosity_multiplier: float = 1.0
    # M9 Slice 3 — cluster_id consulted (when caller supplied one
    # AND the sensor was curiosity-aware). ``None`` means no
    # cluster context provided / no bias applied. Operator-
    # explainability: SSE consumers can render
    # "throttled toward cluster X via curiosity 0.7×".
    curiosity_cluster_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "sensor_name": self.sensor_name,
            "urgency": self.urgency.value,
            "posture": self.posture,
            "weighted_cap": self.weighted_cap,
            "current_count": self.current_count,
            "remaining": self.remaining,
            "reason_code": self.reason_code,
            "emergency_brake": self.emergency_brake,
            "global_cap": self.global_cap,
            "global_count": self.global_count,
            "topology_blocked": self.topology_blocked,
            "curiosity_multiplier": self.curiosity_multiplier,
            "curiosity_cluster_id": self.curiosity_cluster_id,
        }


# ---------------------------------------------------------------------------
# Signal-bundle reader — Wave 1 #1 integration
# ---------------------------------------------------------------------------


def _default_posture_fn() -> Optional[str]:
    """Default posture reader — pulls current posture via the
    canonical ``posture_health.safe_load_posture_value`` wrapper
    so a dead PostureObserver task degrades the governor to
    unweighted (1.0×) caps — equivalent to MAINTAIN safe-default —
    instead of silently applying weights against frozen state.

    §37 Tier 1 #2 (v2.84): closes the silent-degradation cascade
    documented at ``posture_observer.py:558-572``. When detection
    master flag is off, the wrapper passes through to
    ``store.load_current()`` byte-equivalent to legacy behavior.

    Returns None on any error so sensors get unweighted caps
    without crashing."""
    try:
        from backend.core.ouroboros.governance.posture_health import (
            safe_load_posture_value,
        )
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_observer,
            get_default_store,
        )
        return safe_load_posture_value(
            observer=get_default_observer(),
            store=get_default_store(),
        )
    except Exception:  # noqa: BLE001
        # Substrate-unavailable rollback — preserve legacy direct
        # store read so a missing posture_health module never
        # breaks the governor.
        try:
            from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
                get_default_store as _get_store_fallback,
            )
            reading = _get_store_fallback().load_current()
            if reading is not None:
                return reading.posture.value
        except Exception:  # noqa: BLE001
            pass
        return None


def _default_signal_bundle_fn() -> Optional[Any]:
    """Default signal-bundle reader for emergency brake thresholds.

    Returns the most recent PostureReading's underlying signals if
    accessible AND the observer is healthy, else None (brake
    disabled). We read through the reading's evidence list which
    carries raw_value per signal.

    §37 Tier 1 #2 (v2.84): composes ``safe_load_posture`` so a
    dead observer disables the brake (safer than triggering
    emergency brake on stale signals — operators see unweighted
    caps rather than frozen-state panic responses). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.posture_health import (
            safe_load_posture,
        )
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_observer,
            get_default_store,
        )
        reading = safe_load_posture(
            observer=get_default_observer(),
            store=get_default_store(),
        )
        if reading is None:
            return None
        # Rebuild a lookup from evidence for cost_burn + postmortem
        signals = {c.signal_name: c.raw_value for c in reading.evidence}
        return signals
    except Exception:  # noqa: BLE001
        return None


def _default_topology_state_fn() -> Tuple[str, ...]:
    """Slice 3c — default reader for the TopologySentinel singleton.

    Returns the tuple of model_ids currently OPEN/TERMINAL_OPEN. When
    the sentinel module isn't importable (Slice 1 isolation) OR the
    sentinel master flag is off, returns ``()`` so backpressure is a
    no-op and the cap math collapses to the pre-3c formula. NEVER
    raises — backpressure is advisory; sentinel outage must not break
    the governor."""
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (
            get_default_sentinel,
        )
        return get_default_sentinel().list_blocked_endpoints()
    except Exception:  # noqa: BLE001
        return ()


def _curiosity_multiplier_for(
    cluster_id: Optional[str],
) -> Tuple[float, Optional[str]]:
    """M9 Slice 3 — lazy-import + query the CuriosityGradient
    consumer (Decision X pattern from Upgrade 1's
    epistemic_budget_provider_bridge).

    Returns ``(multiplier, normalized_cluster_id)``. Defaults
    cleanly to ``(1.0, None)`` when:

      * ``cluster_id`` is None / empty
      * The M9 module is not importable (Slice 1 isolation
        before ``curiosity_gradient.py`` lands — should never
        happen post-Slice 1 but defensive)
      * The M9 master flag is off
      * The collector is empty or returns INSUFFICIENT_DATA
        (cold-start)
      * The collector has decayed the cluster
        (STALE_FOCUS / RECURRENCE_LOOP / OPERATOR_RESET)
      * Score confidence is below the consumer threshold
      * Any exception (broken collector, broken primitive, etc.)

    Bounded by construction at the M9 primitive layer
    (curiosity_multiplier_from_score clamps to [floor, ceiling]).
    NEVER raises."""
    if not cluster_id:
        return (1.0, None)
    try:
        from backend.core.ouroboros.governance.curiosity_collector import (
            get_default_collector,
        )
        from backend.core.ouroboros.governance.curiosity_gradient import (
            curiosity_multiplier_from_score,
        )
    except Exception:  # noqa: BLE001 — defensive (M9 module absent)
        return (1.0, None)
    try:
        collector = get_default_collector()
        score = collector.score_for_cluster(cluster_id)
        mult = curiosity_multiplier_from_score(score)
        return (float(mult), score.cluster_id)
    except Exception:  # noqa: BLE001 — defensive
        return (1.0, None)


# ---------------------------------------------------------------------------
# SensorGovernor
# ---------------------------------------------------------------------------


class SensorGovernor:
    """Rolling-window emission counter + posture-weighted caps.

    Public API:
      * ``register(spec)`` — install or override a SensorBudgetSpec
      * ``request_budget(sensor, urgency)`` → :class:`BudgetDecision`
      * ``record_emission(sensor, urgency)`` — tick the counter
      * ``snapshot()`` — full state for REPL/GET/SSE
      * ``reset()`` — clear all counters (operator override)

    Thread-safe. ``request_budget()`` does NOT auto-record the emission
    — callers must explicitly ``record_emission()`` if they proceed
    with the op. This split lets callers ask "am I allowed?" without
    committing (useful for dry-run / simulation paths).
    """

    def __init__(
        self,
        *,
        posture_fn: Optional[Callable[[], Optional[str]]] = None,
        signal_bundle_fn: Optional[Callable[[], Optional[Any]]] = None,
        topology_state_fn: Optional[
            Callable[[], Tuple[str, ...]]
        ] = None,
    ) -> None:
        self._specs: Dict[str, SensorBudgetSpec] = {}
        # Per-sensor deques of emission timestamps (monotonic seconds)
        self._per_sensor: Dict[str, Deque[float]] = {}
        # Global emission deque (all sensors)
        self._global: Deque[float] = deque()
        # Decision history for /governor history
        self._decisions: Deque[BudgetDecision] = deque(maxlen=512)
        self._posture_fn = posture_fn or _default_posture_fn
        self._signal_bundle_fn = signal_bundle_fn or _default_signal_bundle_fn
        # Slice 3c — TopologySentinel-aware backpressure. Returns the
        # tuple of blocked model_ids; truthy → throttle BG/SPEC caps.
        self._topology_state_fn = (
            topology_state_fn or _default_topology_state_fn
        )
        self._lock = threading.Lock()

    # -- registration -------------------------------------------------------

    def register(self, spec: SensorBudgetSpec, *, override: bool = True) -> None:
        if not isinstance(spec, SensorBudgetSpec):
            raise TypeError(f"expected SensorBudgetSpec, got {type(spec).__name__}")
        with self._lock:
            if spec.sensor_name in self._specs and not override:
                raise ValueError(
                    f"sensor {spec.sensor_name!r} already registered"
                )
            self._specs[spec.sensor_name] = spec
            self._per_sensor.setdefault(spec.sensor_name, deque())

    def bulk_register(self, specs: List[SensorBudgetSpec]) -> None:
        for s in specs:
            self.register(s)

    def get_spec(self, sensor_name: str) -> Optional[SensorBudgetSpec]:
        with self._lock:
            return self._specs.get(sensor_name)

    def list_specs(self) -> List[SensorBudgetSpec]:
        with self._lock:
            return sorted(self._specs.values(), key=lambda s: s.sensor_name)

    # -- window maintenance -------------------------------------------------

    def _evict_expired(self, now: float) -> None:
        """Drop timestamps older than the rolling window. Must hold self._lock."""
        cutoff = now - window_seconds()
        for name, dq in self._per_sensor.items():
            while dq and dq[0] < cutoff:
                dq.popleft()
        while self._global and self._global[0] < cutoff:
            self._global.popleft()

    # -- cap math -----------------------------------------------------------

    def _emergency_brake_active(self) -> bool:
        """Reads the current signal bundle via the injected callable.
        Returns True if cost_burn > threshold or postmortem_rate > threshold,
        OR if the CD-2 control-plane load-shed latch is active (stream is
        consuming the event loop under critical lag). CD-2 is gated by
        JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED (default OFF) so this OR is
        a no-op when the feature is disabled.
        Missing signals → False (brake disabled)."""
        # CD-2 — load-shed latch: if a DW SSE stream is active AND event-loop
        # lag exceeds JARVIS_LOAD_SHED_LAG_THRESHOLD_MS, shed low-priority
        # background sensors to free the loop. Wrapped defensively so a
        # missing/broken module never silences the existing brake logic.
        try:
            from backend.core.ouroboros.governance.control_plane_load_shed import (
                is_shedding as _cd2_is_shedding,
            )
            if _cd2_is_shedding():
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            bundle = self._signal_bundle_fn()
        except Exception:  # noqa: BLE001
            return False
        if bundle is None:
            return False
        try:
            cost_burn = float(bundle.get("cost_burn_normalized", 0.0))
            pm_rate = float(bundle.get("postmortem_failure_rate", 0.0))
        except (AttributeError, TypeError, ValueError):
            return False
        return (cost_burn >= emergency_cost_threshold()
                or pm_rate >= emergency_postmortem_threshold())

    def _topology_blocking(self, urgency: Urgency) -> bool:
        """Slice 3c — predicate for "should the topology factor apply
        to this request?". Returns True iff:
          * the master flag is on, AND
          * urgency is BACKGROUND or SPECULATIVE (high-urgency routes
            cascade to Claude and must not throttle on DW health), AND
          * the injected ``topology_state_fn`` reports any blocked
            endpoint.

        The state-fn callable is permitted to raise — we swallow and
        return False so a sentinel outage never breaks the governor.
        """
        if not topology_backpressure_enabled():
            return False
        if urgency not in (Urgency.BACKGROUND, Urgency.SPECULATIVE):
            return False
        try:
            blocked = self._topology_state_fn()
        except Exception:  # noqa: BLE001
            return False
        return bool(blocked)

    def _weighted_cap(
        self,
        spec: SensorBudgetSpec,
        urgency: Urgency,
        posture: Optional[str],
        brake: bool,
        topology_blocked: bool = False,
        curiosity_multiplier: float = 1.0,
    ) -> int:
        # Slice 33 Arc 0 — diagnostic only.
        from backend.core.ouroboros.telemetry.loop_sink import (
            sink_sync as _ls_sink_sync,
        )
        with _ls_sink_sync("sensor_governor.SensorGovernor._weighted_cap"):
            return self._weighted_cap_impl(
                spec, urgency, posture, brake, topology_blocked,
                curiosity_multiplier,
            )

    def _weighted_cap_impl(
        self,
        spec: SensorBudgetSpec,
        urgency: Urgency,
        posture: Optional[str],
        brake: bool,
        topology_blocked: bool = False,
        curiosity_multiplier: float = 1.0,
    ) -> int:
        base = spec.base_cap_per_hour
        posture_mult = spec.weight_for_posture(posture)
        urgency_mult = spec.urgency_mult(urgency)
        cap = base * posture_mult * urgency_mult
        # M9 Slice 3 — curiosity multiplier composes BEFORE topology
        # + brake so high-curiosity regions can be amplified within
        # the same envelope that topology + brake would throttle.
        # Bounded at the M9 primitive layer to [floor, ceiling]
        # (defaults [0.5, 2.0]) — global cap structurally cannot be
        # bypassed since the global cap is enforced separately in
        # request_budget against gcap. ``1.0`` (default) is a no-op
        # so existing behavior is byte-identical when no sensor
        # opts in or M9 master flag is off.
        if curiosity_multiplier != 1.0:
            cap *= curiosity_multiplier
        # Slice 3c — topology backpressure factor applied BEFORE the
        # emergency brake so the two compose (DW blocked + cost-burn
        # high → 0.2 × 0.2 = 0.04× throttle on BG/SPEC).
        if topology_blocked:
            cap *= topology_backpressure_mult()
        if brake:
            cap *= emergency_reduction_pct()
        # Floor at 1 so brake + low-weight sensor isn't fully zeroed
        return max(1, int(cap))

    # -- public API ---------------------------------------------------------

    def request_budget(
        self,
        sensor_name: str,
        urgency: Urgency = Urgency.STANDARD,
        *,
        cluster_id: Optional[str] = None,
    ) -> BudgetDecision:
        """Query whether ``sensor_name`` may emit an op at this urgency.

        When the master flag is off, always returns allowed=True with
        reason_code='governor.disabled' so sensors fall through to the
        pre-governor path.

        M9 Slice 3 (PRD §30.5.1): when ``cluster_id`` is supplied AND
        the registered :class:`SensorBudgetSpec` has
        ``curiosity_aware=True`` AND the M9 master flag is on, the
        cap formula composes a ``curiosity_multiplier`` from the
        :mod:`curiosity_collector` snapshot (lazy-imported via
        :func:`_curiosity_multiplier_for`). All other cases collapse
        to the pre-Slice-3 formula (multiplier=1.0). Decoupled by
        design — governor never imports M9 at module load; the
        lazy import keeps M9 dormant when not graduated.
        """
        if not is_enabled():
            return BudgetDecision(
                allowed=True, sensor_name=sensor_name, urgency=urgency,
                posture=None, weighted_cap=0, current_count=0, remaining=0,
                reason_code="governor.disabled",
            )
        try:
            posture = self._posture_fn()
        except Exception:  # noqa: BLE001
            posture = None

        with self._lock:
            spec = self._specs.get(sensor_name)
            if spec is None:
                # Unregistered sensor → allow but report
                decision = BudgetDecision(
                    allowed=True, sensor_name=sensor_name, urgency=urgency,
                    posture=posture, weighted_cap=0, current_count=0,
                    remaining=0, reason_code="governor.unregistered_sensor",
                )
                self._decisions.append(decision)
                return decision

            now = time.monotonic()
            self._evict_expired(now)

            brake = self._emergency_brake_active()
            topology_blocked = self._topology_blocking(urgency)
            # M9 Slice 3 — query CuriosityGradient consumer iff this
            # sensor opted in. Decoupled by lazy-import; defaults to
            # (1.0, None) on M9-off / cold-start / decay / error.
            cur_mult: float = 1.0
            cur_cid: Optional[str] = None
            if spec.curiosity_aware and cluster_id is not None:
                cur_mult, cur_cid = _curiosity_multiplier_for(
                    cluster_id,
                )
            weighted_cap = self._weighted_cap(
                spec, urgency, posture, brake,
                topology_blocked=topology_blocked,
                curiosity_multiplier=cur_mult,
            )
            current = len(self._per_sensor.get(sensor_name, ()))
            remaining = max(0, weighted_cap - current)

            gcap = global_cap_per_hour()
            gcount = len(self._global)
            if brake:
                gcap = max(1, int(gcap * emergency_reduction_pct()))
            gremaining = max(0, gcap - gcount)

            # Slice 3c — when the cap was reduced by topology AND the
            # reduction is what exhausted the per-sensor budget, stamp
            # a more specific reason_code so SSE/REPL consumers can
            # distinguish "throttled by DW health" from "throttled by
            # capacity". reason_code is otherwise unchanged when the
            # topology factor wasn't load-bearing.
            if gremaining <= 0:
                decision = BudgetDecision(
                    allowed=False, sensor_name=sensor_name, urgency=urgency,
                    posture=posture, weighted_cap=weighted_cap,
                    current_count=current, remaining=remaining,
                    reason_code="governor.global_cap_exhausted",
                    emergency_brake=brake, global_cap=gcap, global_count=gcount,
                    topology_blocked=topology_blocked,
                    curiosity_multiplier=cur_mult,
                    curiosity_cluster_id=cur_cid,
                )
            elif remaining <= 0:
                _reason = (
                    "governor.topology_backpressure"
                    if topology_blocked
                    else "governor.sensor_cap_exhausted"
                )
                decision = BudgetDecision(
                    allowed=False, sensor_name=sensor_name, urgency=urgency,
                    posture=posture, weighted_cap=weighted_cap,
                    current_count=current, remaining=remaining,
                    reason_code=_reason,
                    emergency_brake=brake, global_cap=gcap, global_count=gcount,
                    topology_blocked=topology_blocked,
                    curiosity_multiplier=cur_mult,
                    curiosity_cluster_id=cur_cid,
                )
            else:
                decision = BudgetDecision(
                    allowed=True, sensor_name=sensor_name, urgency=urgency,
                    posture=posture, weighted_cap=weighted_cap,
                    current_count=current, remaining=remaining,
                    reason_code="governor.ok",
                    emergency_brake=brake, global_cap=gcap, global_count=gcount,
                    topology_blocked=topology_blocked,
                    curiosity_multiplier=cur_mult,
                    curiosity_cluster_id=cur_cid,
                )

            self._decisions.append(decision)
            return decision

    def record_emission(
        self,
        sensor_name: str,
        urgency: Urgency = Urgency.STANDARD,
    ) -> None:
        """Register that ``sensor_name`` emitted one op. Should be called
        only after a positive ``request_budget()`` decision — but the
        governor is advisory, so callers who ignore the decision can
        still record their emission and have it counted."""
        if not is_enabled():
            return
        now = time.monotonic()
        with self._lock:
            if sensor_name not in self._specs:
                # Still count unregistered sensors globally for observability
                self._global.append(now)
                return
            self._per_sensor.setdefault(sensor_name, deque()).append(now)
            self._global.append(now)

    # -- diagnostics --------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        if not is_enabled():
            return {
                "schema_version": SENSOR_GOVERNOR_SCHEMA_VERSION,
                "enabled": False,
                "sensors": [],
                "global": {"cap": 0, "count": 0},
            }
        try:
            posture = self._posture_fn()
        except Exception:  # noqa: BLE001
            posture = None
        with self._lock:
            now = time.monotonic()
            self._evict_expired(now)
            brake = self._emergency_brake_active()
            sensors = []
            for name in sorted(self._specs.keys()):
                spec = self._specs[name]
                # Synthesize per-urgency caps snapshot (STANDARD default view)
                cap_standard = self._weighted_cap(
                    spec, Urgency.STANDARD, posture, brake,
                )
                count = len(self._per_sensor.get(name, ()))
                sensors.append({
                    "sensor_name": name,
                    "base_cap_per_hour": spec.base_cap_per_hour,
                    "posture_weight": spec.weight_for_posture(posture),
                    "weighted_cap_standard": cap_standard,
                    "current_count": count,
                    "remaining_standard": max(0, cap_standard - count),
                })
            gcap = global_cap_per_hour()
            if brake:
                gcap = max(1, int(gcap * emergency_reduction_pct()))
            return {
                "schema_version": SENSOR_GOVERNOR_SCHEMA_VERSION,
                "enabled": True,
                "posture": posture,
                "emergency_brake": brake,
                "sensors": sensors,
                "global": {
                    "cap": gcap, "count": len(self._global),
                    "remaining": max(0, gcap - len(self._global)),
                },
                "window_s": window_seconds(),
                "emergency_thresholds": {
                    "cost_burn": emergency_cost_threshold(),
                    "postmortem_rate": emergency_postmortem_threshold(),
                    "reduction_pct": emergency_reduction_pct(),
                },
                "decisions_count": len(self._decisions),
            }

    def recent_decisions(self, limit: int = 20) -> List[BudgetDecision]:
        with self._lock:
            n = max(1, min(len(self._decisions), int(limit)))
            return list(self._decisions)[-n:]

    def reset(self) -> None:
        """Operator override — clear all counters. Preserves specs."""
        with self._lock:
            self._per_sensor.clear()
            for name in self._specs:
                self._per_sensor[name] = deque()
            self._global.clear()
            self._decisions.clear()

    # ----------------------------------------------------------------------
    # PRD §11 (S2) additive composition surface — preemption signal
    # ----------------------------------------------------------------------
    # S2's predictive admission gate emits a structured advisory when
    # forecasted spend approaches the budget AND a high-urgency op is
    # queued. The governor RECORDS the signal (no new quarantine
    # machinery: existing posture-weighted caps + emergency brakes are
    # the authoritative quarantine path; this method only adds a
    # structured record so the existing decision/snapshot surfaces can
    # report it).
    #
    # Load-bearing invariant (PRD §11.4): the signal's advice path is
    # restricted to ``BACKGROUND`` / ``SPECULATIVE`` — high-urgency
    # routes (IMMEDIATE / STANDARD / COMPLEX) are IMMUNE. Enforced
    # both here (input validation) and at the consumer site.
    #
    # NEVER raises. Garbage input silently rejected.

    # Single-source-of-truth for the immune/quarantinable partition.
    # Authoritative for S2 §11.4 invariant; AST-pinned downstream.
    _S2_HIGH_URGENCY_IMMUNE: frozenset = frozenset({
        Urgency.IMMEDIATE.value,
        Urgency.STANDARD.value,
        Urgency.COMPLEX.value,
    })
    _S2_QUARANTINABLE: frozenset = frozenset({
        Urgency.BACKGROUND.value,
        Urgency.SPECULATIVE.value,
    })

    def apply_preemption_signal(
        self,
        *,
        kind: str,
        severity: float,
        high_prio_queued: bool,
        advice: str,
    ) -> bool:
        """Record an S2 preemption-advisory signal (PRD §11). Returns
        True iff the signal was accepted + recorded.

        Authoritative behavior contract:
          * High-urgency routes are NEVER quarantined by this signal.
          * Severity is clipped to [0.0, 1.0]; NaN → rejected.
          * ``advice`` MUST be ``'quarantine_low_prio_sensors'`` (the
            only advice S2 is allowed to emit). Other strings are
            silently rejected — closed surface.
          * ``kind`` MUST be a short identifier; truncated to 64 chars.
          * The signal is appended to the decision ring so existing
            ``recent_decisions()`` + ``snapshot()`` observability
            paths report it without changing their shape.

        NEVER raises."""
        try:
            # Validate advice (closed surface)
            if advice != "quarantine_low_prio_sensors":
                return False
            # Validate kind
            try:
                k = str(kind or "").strip()
            except Exception:  # noqa: BLE001
                return False
            if not k:
                return False
            k = k[:64]
            # Validate severity (clip + NaN-reject)
            try:
                sev = float(severity)
            except (TypeError, ValueError):
                return False
            if sev != sev:                   # NaN
                return False
            sev = max(0.0, min(1.0, sev))
            hpq = bool(high_prio_queued)
            with self._lock:
                # Record into existing decision ring — composes the
                # existing snapshot/recent_decisions surfaces. The
                # signal is a distinct record kind, distinguishable
                # by ``sensor_name`` starting with ``_s2_preempt:``.
                self._decisions.append(BudgetDecision(
                    allowed=False,
                    sensor_name=f"_s2_preempt:{k}",
                    urgency=Urgency.BACKGROUND,
                    posture=None,
                    weighted_cap=0,
                    current_count=0,
                    remaining=0,
                    reason_code=(
                        f"s2_preemption_signal severity={sev:.3f} "
                        f"high_prio_queued={hpq} advice={advice}"
                    ),
                ))
            return True
        except Exception as exc:  # noqa: BLE001 — defensive
            try:
                _logger = __import__("logging").getLogger(
                    "Ouroboros.SensorGovernor",
                )
                _logger.debug(
                    "[SensorGovernor] apply_preemption_signal "
                    "degraded: %s", exc,
                )
            except Exception:  # noqa: BLE001
                pass
            return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_default_governor: Optional[SensorGovernor] = None
_singleton_lock = threading.Lock()
_seed_applied = False


def get_default_governor() -> SensorGovernor:
    global _default_governor
    with _singleton_lock:
        if _default_governor is None:
            _default_governor = SensorGovernor()
        return _default_governor


def reset_default_governor() -> None:
    global _default_governor, _seed_applied
    with _singleton_lock:
        _default_governor = None
        _seed_applied = False


def ensure_seeded() -> SensorGovernor:
    """Install seed registrations + register flags in Wave 1 #2's
    FlagRegistry if available. Idempotent."""
    global _seed_applied
    governor = get_default_governor()
    with _singleton_lock:
        if _seed_applied:
            return governor
        _seed_applied = True
    try:
        from backend.core.ouroboros.governance.sensor_governor_seed import (
            seed_default_governor,
        )
        seed_default_governor(governor)
    except ImportError:
        logger.debug(
            "[SensorGovernor] seed module unavailable; starts empty",
        )
    # Register own flags in Wave 1 #2 FlagRegistry if available
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType, Relevance, ensure_seeded as _fr_seed,
        )
        fr = _fr_seed()
        for spec in _own_flag_specs():
            fr.register(spec, override=True)
    except ImportError:
        pass  # FlagRegistry not loaded — registry stays empty of our flags
    return governor


def _own_flag_specs() -> List[Any]:
    """Our own env flags, registered into Wave 1 #2's FlagRegistry at
    ensure_seeded() time so `/help flags --search governor` works."""
    from backend.core.ouroboros.governance.flag_registry import (
        Category, FlagSpec, FlagType, Relevance,
    )
    return [
        FlagSpec(
            name="JARVIS_SENSOR_GOVERNOR_ENABLED",
            type=FlagType.BOOL, default=True,
            description=(
                "Master kill switch for the SensorGovernor — posture-"
                "weighted op-emission cap across the 16 sensors."
            ),
            category=Category.SAFETY,
            source_file="backend/core/ouroboros/governance/sensor_governor.py",
            example="true", since="v1.0",
            posture_relevance={
                "EXPLORE": Relevance.CRITICAL, "CONSOLIDATE": Relevance.CRITICAL,
                "HARDEN": Relevance.CRITICAL, "MAINTAIN": Relevance.CRITICAL,
            },
        ),
        FlagSpec(
            name="JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR",
            type=FlagType.INT, default=200,
            description=(
                "Total op emissions across all 16 sensors per rolling window."
            ),
            category=Category.CAPACITY,
            source_file="backend/core/ouroboros/governance/sensor_governor.py",
            example="200", since="v1.0",
        ),
        FlagSpec(
            name="JARVIS_SENSOR_GOVERNOR_WINDOW_S",
            type=FlagType.INT, default=3600,
            description=(
                "Rolling window duration for op-emission counting. Default "
                "1h — lower values are more reactive, higher values "
                "smoother."
            ),
            category=Category.TIMING,
            source_file="backend/core/ouroboros/governance/sensor_governor.py",
            example="3600", since="v1.0",
        ),
        FlagSpec(
            name="JARVIS_SENSOR_GOVERNOR_EMERGENCY_REDUCTION_PCT",
            type=FlagType.FLOAT, default=0.2,
            description=(
                "Multiplier applied to all weighted caps when the emergency "
                "brake fires (cost_burn>0.9 OR postmortem>0.6). Default 0.2."
            ),
            category=Category.TUNING,
            source_file="backend/core/ouroboros/governance/sensor_governor.py",
            example="0.2", since="v1.0",
            posture_relevance={"HARDEN": Relevance.CRITICAL},
        ),
        FlagSpec(
            name="JARVIS_SENSOR_GOVERNOR_EMERGENCY_COST_THRESHOLD",
            type=FlagType.FLOAT, default=0.9,
            description=(
                "cost_burn_normalized signal above which the emergency "
                "brake fires."
            ),
            category=Category.TUNING,
            source_file="backend/core/ouroboros/governance/sensor_governor.py",
            example="0.9", since="v1.0",
        ),
        FlagSpec(
            name="JARVIS_SENSOR_GOVERNOR_EMERGENCY_POSTMORTEM_THRESHOLD",
            type=FlagType.FLOAT, default=0.6,
            description=(
                "postmortem_failure_rate signal above which the emergency "
                "brake fires."
            ),
            category=Category.TUNING,
            source_file="backend/core/ouroboros/governance/sensor_governor.py",
            example="0.6", since="v1.0",
            posture_relevance={"HARDEN": Relevance.CRITICAL},
        ),
    ]


__all__ = [
    "BudgetDecision",
    "SENSOR_GOVERNOR_SCHEMA_VERSION",
    "SensorBudgetSpec",
    "SensorGovernor",
    "Urgency",
    "emergency_cost_threshold",
    "emergency_postmortem_threshold",
    "emergency_reduction_pct",
    "ensure_seeded",
    "get_default_governor",
    "global_cap_per_hour",
    "is_enabled",
    "reset_default_governor",
    "window_seconds",
]

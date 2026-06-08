"""Risk-tier floor: paranoia-mode knobs that FORBID SAFE_AUTO.

The existing ``JARVIS_RISK_CEILING`` env knob sets an upward escalation
floor — it can push SAFE_AUTO to NOTIFY_APPLY, etc. This module ADDS
two further paranoia controls the operator can flip before going to
sleep:

* ``JARVIS_MIN_RISK_TIER={safe_auto|notify_apply|approval_required}``
    Absolute floor. If the risk engine classifies SAFE_AUTO but the
    floor is ``notify_apply``, the classification is upgraded. Same
    for ``approval_required`` forcing every op to pause for the human.

* ``JARVIS_PARANOIA_MODE=1``
    Convenience shortcut equivalent to ``MIN_RISK_TIER=notify_apply``.
    When set, the floor is active even if ``JARVIS_MIN_RISK_TIER`` is
    unset.

* ``JARVIS_AUTO_APPLY_QUIET_HOURS=<start>-<end>``
    Time-of-day window during which ``MIN_RISK_TIER=notify_apply`` is
    implicitly active. Supports wrap-around (``22-7`` → 10 PM to 7 AM).
    **Defaults to UTC** when ``JARVIS_AUTO_APPLY_QUIET_HOURS_TZ`` is
    unset — implicit local-wall-clock semantics are ambiguous across
    multi-operator deployments (Manifesto §4: clarity over convenience).

* ``JARVIS_AUTO_APPLY_QUIET_HOURS_TZ=UTC|America/Los_Angeles|...``
    IANA timezone name used to interpret ``QUIET_HOURS``. Absent /
    malformed → falls back to UTC with a DEBUG log. An explicit
    ``UTC`` pass-through keeps the default documented.

* ``JARVIS_VISION_SENSOR_RISK_FLOOR={notify_apply|approval_required|blocked}``
    VisionSensor-specific floor (Task 6 of the VisionSensor + Visual
    VERIFY arc). When an op's ``signal_source == "vision_sensor"``,
    this env value is merged into the normal floor composition. The
    *hard-coded* default is ``notify_apply`` — vision-originated ops
    can never reach ``safe_auto`` (Invariant I2 in the design spec).
    The env is tunable *upward only*: ``approval_required`` / ``blocked``
    strengthen the floor. An explicit ``safe_auto`` raises ``ValueError``
    because it would break I2; unknown tier names are ignored (DEBUG
    log) and the default is used.

Authority invariant: this module is pure-read. It consumes env vars +
the current time + the op's signal source and returns a recommended
floor. The caller (the risk-engine wrapper) is responsible for
applying the floor to the classification.

Orderings are *safer-is-higher*:

    SAFE_AUTO < NOTIFY_APPLY < APPROVAL_REQUIRED < BLOCKED

So a floor of ``NOTIFY_APPLY`` means "never go below NOTIFY_APPLY" —
i.e. no SAFE_AUTO auto-applies.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.RiskFloor")

_ENV_MIN_TIER = "JARVIS_MIN_RISK_TIER"
_ENV_PARANOIA = "JARVIS_PARANOIA_MODE"
_ENV_QUIET_HOURS = "JARVIS_AUTO_APPLY_QUIET_HOURS"
_ENV_QUIET_HOURS_TZ = "JARVIS_AUTO_APPLY_QUIET_HOURS_TZ"
_ENV_VISION_FLOOR = "JARVIS_VISION_SENSOR_RISK_FLOOR"

# Canonical signal-source name for the VisionSensor. Mirrors
# ``SignalSource.VISION_SENSOR.value`` in ``intent/signals.py`` — we
# stringly-match to avoid an import-cycle (risk_tier_floor is a leaf
# module consumed by orchestrator / gate layers that also touch
# intent). The string form is the contract.
_VISION_SENSOR_SOURCE = "vision_sensor"

# Hard-coded floor for vision-originated ops. Cannot be weakened by env
# (attempting to set a weaker value raises ValueError). Upward moves
# (approval_required / blocked) are allowed.
_VISION_SENSOR_HARD_FLOOR = "notify_apply"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Canonical ordering — safer-is-higher. Used only for comparison; the
# orchestrator continues to import RiskTier from risk_engine for its
# own handling so this module stays dependency-free.
_ORDER = {
    "safe_auto": 0,
    "notify_apply": 1,
    "approval_required": 2,
    "blocked": 3,
}


def get_active_tier_order() -> Dict[str, int]:
    """Return the active risk-tier order dict.

    Phase 7.4 caller wiring (Caller Wiring PR #3 — 2026-04-26):
    composes the canonical ``_ORDER`` baseline with operator-approved
    adapted tiers loaded via ``compute_extended_ladder()``.

    Master-off byte-identical: when
    ``JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS=false`` (default),
    no adapted YAML is loaded → returns ``dict(_ORDER)`` unchanged.

    Master-on: each operator-approved adapted tier (per Pass C
    Slice 4b miner — see ``adaptation/risk_tier_extender.py``) is
    inserted into the ladder at its ``insert_after`` slot. Adapted
    tier names from the YAML are normalized to lowercase to match
    the canonical ``_ORDER`` convention. Cage rule (load-bearing
    per Pass C §8.3): the ladder ONLY GROWS — no canonical tier
    is removed or reordered.

    Defense-in-depth: if the loader raises for any reason, falls
    back to the canonical ``_ORDER`` baseline. NEVER raises into
    the caller.

    Returns a NEW dict on every call so callers may mutate it
    without affecting future callers.
    """
    base_lower = sorted(_ORDER.keys(), key=lambda k: _ORDER[k])
    # Phase 7.4 helper expects uppercase per Slice 4b miner's
    # `_synthesize_tier_name` output charset `[A-Z0-9_]+`. Lift the
    # base ladder to uppercase for the helper, then lowercase the
    # extended result to match _ORDER's canonical case.
    base_upper = tuple(n.upper() for n in base_lower)
    try:
        from backend.core.ouroboros.governance.adaptation.adapted_risk_tier_loader import (  # noqa: E501
            compute_extended_ladder,
        )
        extended_upper = compute_extended_ladder(base_upper)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[RiskFloor] compute_extended_ladder raised %s — "
            "falling back to canonical _ORDER", exc,
        )
        return dict(_ORDER)
    # Build new ordered dict: lowercase, rank by position.
    return {name.lower(): rank for rank, name in enumerate(extended_upper)}


def _norm_tier(s: str) -> str:
    return (s or "").strip().lower()


def paranoia_mode_enabled() -> bool:
    return os.environ.get(_ENV_PARANOIA, "0").strip().lower() in _TRUTHY


def _env_floor() -> Optional[str]:
    """Read the explicit ``JARVIS_MIN_RISK_TIER`` env, normalized.

    Returns None when unset or malformed. Recognised values are
    ``"safe_auto"``, ``"notify_apply"``, ``"approval_required"``,
    ``"blocked"`` (case-insensitive).
    """
    raw = _norm_tier(os.environ.get(_ENV_MIN_TIER, ""))
    if not raw:
        return None
    if raw not in get_active_tier_order():
        logger.debug(
            "[RiskFloor] unrecognised %s=%r — ignoring",
            _ENV_MIN_TIER, raw,
        )
        return None
    return raw


def _parse_quiet_hours(raw: str) -> Optional[Tuple[int, int]]:
    """Parse ``<start>-<end>`` where each side is 0-23. Returns
    ``(start, end)`` or ``None`` when malformed.
    """
    if not raw:
        return None
    parts = raw.strip().split("-", 1)
    if len(parts) != 2:
        return None
    try:
        start = int(parts[0].strip())
        end = int(parts[1].strip())
    except ValueError:
        return None
    if not (0 <= start < 24 and 0 <= end < 24):
        return None
    return (start, end)


def _resolve_tz():
    """Resolve JARVIS_AUTO_APPLY_QUIET_HOURS_TZ to a tzinfo. Falls back
    to UTC when unset / malformed. Emits one DEBUG line on fallback so
    operators can spot a typo without spam.

    Uses stdlib ``zoneinfo`` (Python 3.9+); if ZoneInfoNotFoundError or
    import failure occurs, returns UTC. Returning a concrete tzinfo
    keeps ``datetime.now(tz=...)`` call sites simple.
    """
    raw = os.environ.get(_ENV_QUIET_HOURS_TZ, "").strip()
    if not raw or raw.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except Exception:  # noqa: BLE001
        logger.debug(
            "[RiskFloor] zoneinfo unavailable — falling back to UTC",
        )
        return timezone.utc
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        logger.debug(
            "[RiskFloor] unknown IANA zone %r — falling back to UTC",
            raw,
        )
        return timezone.utc
    except Exception:  # noqa: BLE001
        logger.debug(
            "[RiskFloor] zone resolution raised — falling back to UTC",
            exc_info=True,
        )
        return timezone.utc


def quiet_hours_active(now: Optional[datetime] = None) -> bool:
    """True when ``JARVIS_AUTO_APPLY_QUIET_HOURS`` is set and the current
    wall-clock hour (in ``QUIET_HOURS_TZ``, defaulting to UTC) falls
    inside the configured window.

    Supports wrap-around: ``22-7`` means 22:00-06:59 in the resolved
    zone. A window of ``9-17`` means 09:00-16:59. Equal start/end is
    treated as the whole day (rare — operator wanted 24h paranoia,
    should use ``MIN_RISK_TIER`` instead, but we honor it).

    Parameters
    ----------
    now:
        Optional explicit datetime for testing. May be naive (assumed
        UTC) or aware. When aware, it's converted to the resolved zone
        before extracting the hour.
    """
    raw = os.environ.get(_ENV_QUIET_HOURS, "").strip()
    if not raw:
        return False
    parsed = _parse_quiet_hours(raw)
    if parsed is None:
        return False
    start, end = parsed

    tz = _resolve_tz()
    if now is None:
        # Default: current time in the resolved zone (UTC by default).
        hour = datetime.now(tz=tz).hour
    elif now.tzinfo is None:
        # Naive datetime — historically this was "local time". The new
        # contract: naive is treated as UTC, then converted if TZ set.
        aware = now.replace(tzinfo=timezone.utc)
        hour = aware.astimezone(tz).hour
    else:
        hour = now.astimezone(tz).hour

    if start == end:
        return True
    if start < end:
        return start <= hour < end
    # Wrap-around — e.g. 22-7 means 22,23,0,1,2,3,4,5,6.
    return hour >= start or hour < end


def _is_vision_source(signal_source: str) -> bool:
    """Return ``True`` when *signal_source* names the VisionSensor.

    Case-insensitive + whitespace-tolerant. The enum member
    ``SignalSource.VISION_SENSOR`` (``intent/signals.py``) compares
    equal to ``"vision_sensor"`` via its StrEnum semantics, so passing
    either the enum or the raw string both work.
    """
    return _norm_tier(signal_source) == _VISION_SENSOR_SOURCE


def _vision_floor_from_env() -> str:
    """Resolve ``JARVIS_VISION_SENSOR_RISK_FLOOR`` honouring Invariant I2.

    Returns the normalised tier name. Default is ``notify_apply``
    (the hard floor). Raises ``ValueError`` if the env asks for a tier
    weaker than ``notify_apply`` — I2 forbids it, and silently coercing
    upward would mask an operator typo that *thought* it was weakening
    the floor. Unknown tier values fall back to the default with a
    DEBUG log (same policy as ``_env_floor``).
    """
    raw = _norm_tier(os.environ.get(_ENV_VISION_FLOOR, ""))
    if not raw:
        return _VISION_SENSOR_HARD_FLOOR
    _order = get_active_tier_order()
    if raw not in _order:
        logger.debug(
            "[RiskFloor] unrecognised %s=%r — using default %s",
            _ENV_VISION_FLOOR, raw, _VISION_SENSOR_HARD_FLOOR,
        )
        return _VISION_SENSOR_HARD_FLOOR
    if _order[raw] < _order[_VISION_SENSOR_HARD_FLOOR]:
        raise ValueError(
            f"{_ENV_VISION_FLOOR}={raw!r} cannot be lower than "
            f"{_VISION_SENSOR_HARD_FLOOR!r}. Vision-originated ops are "
            "forbidden from reaching safe_auto (Invariant I2 in "
            "docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md)."
        )
    return raw


def _confidence_floor_for_op(
    op_id: Optional[str],
) -> Optional[str]:
    """§37 Tier 2 #13 Slice 3 — confidence-derived floor.

    Returns the tier-name floor implied by the worst confidence
    band observed for ``op_id``, or ``None`` when:

      * ``op_id`` is empty / None (no per-op signal).
      * The Slice 1 master flag is off
        (``JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED=false``).
      * No tool-call has been observed for this op yet (band
        absent — caller treats absence as "no signal" rather
        than UNKNOWN, mirroring Slice 2's "no capturer → no
        observation" discipline).
      * The worst band is HIGH or CERTAIN (no clamp needed).

    Mapping (load-bearing Antivenom semantic):

      * UNKNOWN / LOW / MEDIUM → ``"notify_apply"``
        (low-confidence single-roll Quine-class hallucinations
        SHOULD NOT auto-apply — see §35 Move 9 and §3.6.2
        vector #7).
      * HIGH / CERTAIN → ``None`` (no clamp).

    Composition discipline: lazy-imports Slice 1 to avoid a
    hard module-load cycle (risk_tier_floor is a leaf substrate;
    Slice 1 transitively touches the SSE broker). NEVER raises.
    """
    if not op_id:
        return None
    try:
        from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
            ToolConfidenceBand,
            master_enabled,
            worst_band_for_op,
        )
    except ImportError:
        return None
    try:
        if not master_enabled():
            return None
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        band = worst_band_for_op(op_id)
    except Exception:  # noqa: BLE001 — defensive
        return None
    if band is None:
        return None
    if band in (
        ToolConfidenceBand.UNKNOWN,
        ToolConfidenceBand.LOW,
        ToolConfidenceBand.MEDIUM,
    ):
        return "notify_apply"
    return None


def _governance_boundary_floor(
    target_files: Optional[Sequence[Any]],
) -> Optional[str]:
    """§40 Wave 2 #5 — compose the RRD §1 Boundary
    recursion-depth gate. Lazy-imports the gate substrate;
    returns ``"approval_required"`` when any target path crosses
    into the canonical governance directory; ``None`` otherwise.
    NEVER raises.

    Composes the gate **at the consumer side** — the gate
    substrate stays pure (no risk_tier_floor import); this
    helper does the composition.
    """
    if not target_files:
        return None
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            BoundaryVerdict,
            evaluate_target_files,
        )
        report = evaluate_target_files(target_files)
        if report.verdict is BoundaryVerdict.BOUNDARY_CROSSED:
            return "approval_required"
    except Exception:  # noqa: BLE001 — defensive
        return None
    return None


def _convergence_floor(
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Slice 98 Phase 2 — compose the Dynamic Risk-State Convergence
    Engine's recommended floor.

    The convergence engine raises the floor toward observation-only /
    paranoia under measurable cross-repo / contradictory-output /
    malformed-intent ambiguity, and **relaxes automatically** (its
    floor is a pure function of the rolling window — no latch).

    Master-OFF (default) → the engine returns ``None`` → this helper
    returns ``None`` → ``recommended_floor`` is byte-identical to the
    engine not existing.

    Lazy-imports the engine to avoid a hard dependency cycle, and wraps
    the call in try/except so a convergence-engine failure can NEVER
    break the floor computation (the floor must stay robust). NEVER
    raises.
    """
    try:
        from backend.core.ouroboros.governance import (  # noqa: E501
            dynamic_risk_convergence,
        )
        # recommended_convergence_floor uses time.time() when now_unix
        # is None; pass the explicit `now` (converted to unix) so a
        # caller-supplied clock stays coherent.
        now_unix: Optional[float] = None
        if now is not None:
            try:
                if now.tzinfo is None:
                    now_unix = now.replace(tzinfo=timezone.utc).timestamp()
                else:
                    now_unix = now.timestamp()
            except Exception:  # noqa: BLE001 — defensive
                now_unix = None
        return dynamic_risk_convergence.recommended_convergence_floor(
            now_unix=now_unix,
        )
    except Exception:  # noqa: BLE001 — floor must stay robust
        logger.debug(
            "[RiskFloor] convergence floor lookup failed", exc_info=True,
        )
        return None


def _rehearsal_floor(
    target_files: Optional[Sequence[Any]],
) -> Optional[str]:
    """Slice 101 Phase 5 — compose the Counterfactual Rehearsal pre-APPLY
    verdict into the strictest-wins floor.

    A candidate whose target files overlap a recent postmortem failure zone
    raises friction (``notify_apply``); a cage-boundary crossing escalates
    (``approval_required``). This is the "structural failure-memory checked
    before a patch lands" gate — it composes EXACTLY like the convergence and
    boundary floors above, so the orchestrator GATE seam needs no change (it
    already passes ``target_files``). Master-gated inside the substrate
    (``JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED``, §33.1 default-FALSE → DISABLED
    verdict → None → byte-identical legacy). NEVER raises — the floor must stay
    robust regardless of the rehearsal substrate's health.
    """
    if not target_files:
        return None
    try:
        from backend.core.ouroboros.governance.counterfactual_rehearsal_mode import (  # noqa: E501
            RehearsalVerdict,
            evaluate_rehearsal,
        )
        report = evaluate_rehearsal(target_files)
        if report.verdict is RehearsalVerdict.ESCALATE:
            return "approval_required"
        if report.verdict is RehearsalVerdict.CONCERN_RAISED:
            return "notify_apply"
    except Exception:  # noqa: BLE001 — floor must stay robust
        logger.debug(
            "[RiskFloor] rehearsal floor lookup failed", exc_info=True,
        )
        return None
    return None


def _recursion_depth_floor(
    target_files: Optional[Sequence[Any]],
) -> Optional[str]:
    """Slice 104 — compose the Operator-Independent Recursion-Depth Gate. Returns
    ``"blocked"`` when a self-modification chain would exceed MAX_RECURSION_DEPTH
    (the un-bypassable HALT), else None. Reads the live process depth counter.
    Master-gated inside the substrate (default-TRUE). NEVER raises — the floor
    must stay robust regardless of the gate's health.
    """
    try:
        from backend.core.ouroboros.governance.recursion_depth_gate import (
            recursion_depth_floor,
        )
        return recursion_depth_floor(target_files)
    except Exception:  # noqa: BLE001 — floor must stay robust
        logger.debug(
            "[RiskFloor] recursion-depth floor lookup failed", exc_info=True,
        )
        return None


def recommended_floor(
    now: Optional[datetime] = None,
    *,
    signal_source: str = "",
    op_id: Optional[str] = None,
    target_files: Optional[Sequence[Any]] = None,
) -> Optional[str]:
    """Compose the floor signals into a single recommendation.

    Ordering (strictest wins):
        1. ``JARVIS_MIN_RISK_TIER`` explicit value
        2. ``JARVIS_PARANOIA_MODE=1`` implies ``notify_apply``
        3. Active quiet-hours window implies ``notify_apply``
        4. Vision-originated op (``signal_source == "vision_sensor"``)
           implies at least ``notify_apply``, or the stronger value
           in ``JARVIS_VISION_SENSOR_RISK_FLOOR`` (Invariant I2).
        5. **§37 Tier 2 #13 Slice 3** — when ``op_id`` is provided
           and the Slice 1 master flag is on, the worst observed
           confidence band for the op implies a floor (LOW /
           UNKNOWN / MEDIUM → ``notify_apply``).
        6. **§40 Wave 2 #5 RRD §1 Boundary** — when ``target_files``
           is provided and any path lies inside the canonical
           governance directory (``backend/core/ouroboros/
           governance/``), forces ``approval_required``. Closes
           infinite-regress risk: M10 ArchitectureProposer (or
           any future autonomous proposer) cannot autonomously
           modify the cage layer.

    Returns the normalised tier name or ``None`` when nothing applies.

    Raises
    ------
    ValueError
        If ``signal_source`` names the VisionSensor and
        ``JARVIS_VISION_SENSOR_RISK_FLOOR`` is set to a tier weaker
        than ``notify_apply``. Weakening the vision floor is a
        configuration error, not a silent clamp.
    """
    explicit = _env_floor()
    candidates: list = []
    if explicit is not None:
        candidates.append(explicit)
    if paranoia_mode_enabled():
        candidates.append("notify_apply")
    if quiet_hours_active(now):
        candidates.append("notify_apply")
    if _is_vision_source(signal_source):
        # Raises ValueError if env tries to weaken below notify_apply.
        candidates.append(_vision_floor_from_env())
    confidence = _confidence_floor_for_op(op_id)
    if confidence is not None:
        candidates.append(confidence)
    boundary = _governance_boundary_floor(target_files)
    if boundary is not None:
        candidates.append(boundary)
    convergence = _convergence_floor(now)
    if convergence is not None:
        candidates.append(convergence)
    # Slice 101 Phase 5 — Counterfactual Rehearsal pre-APPLY gate. Overlap with
    # a recent postmortem failure zone → notify_apply; cage-boundary cross →
    # approval_required. Master-gated (default-FALSE) → None when off.
    rehearsal = _rehearsal_floor(target_files)
    if rehearsal is not None:
        candidates.append(rehearsal)
    # Slice 104 — Operator-Independent Recursion-Depth Gate (RRD §23.5). When a
    # self-modification chain (consecutive governance-touching APPLYs) would
    # exceed MAX_RECURSION_DEPTH, this returns "blocked" — the strictest, un-
    # bypassable tier — severing the runaway loop autonomously. Master-gated
    # (default-TRUE safety gate); None for non-governance ops / chains in bound.
    recursion = _recursion_depth_floor(target_files)
    if recursion is not None:
        candidates.append(recursion)
    if not candidates:
        return None
    # Pick the strictest — highest ordinal wins.
    _order = get_active_tier_order()
    return max(candidates, key=lambda t: _order.get(t, 0))


def apply_floor_to_name(
    tier_name: str,
    *,
    now: Optional[datetime] = None,
    signal_source: str = "",
    op_id: Optional[str] = None,
    target_files: Optional[Sequence[Any]] = None,
) -> Tuple[str, Optional[str]]:
    """Apply the recommended floor to a tier *name*.

    Returns ``(effective_tier_name, applied_floor_or_None)``. When the
    floor is stricter than the input tier, ``effective_tier_name`` is
    the floor; otherwise the input passes through untouched.
    ``applied_floor_or_None`` is non-None only when the floor actually
    upgraded the tier — useful for observability logging.

    Unknown input tier names pass through unchanged.

    Passing ``signal_source="vision_sensor"`` engages the VisionSensor
    floor (Invariant I2). Passing ``op_id`` engages the §37 Tier 2 #13
    Slice 3 confidence-derived floor (master-flag-gated). Passing
    ``target_files`` engages the §40 Wave 2 #5 RRD §1 Boundary gate
    (forces ``approval_required`` for ops touching the canonical
    governance directory). See :func:`recommended_floor`.
    """
    raw_in = _norm_tier(tier_name)
    _order = get_active_tier_order()
    if raw_in not in _order:
        return (tier_name, None)
    try:
        floor = recommended_floor(
            now,
            signal_source=signal_source,
            op_id=op_id,
            target_files=target_files,
        )
    except Exception:  # noqa: BLE001
        # Slice 163 — FAIL-CLOSED. A failure computing the recommendation must NOT
        # silently bypass the operator's deliberate governance posture. Fall back to
        # the explicit MIN_RISK_TIER floor so an erroring subsystem can never let an
        # op auto-apply below the configured floor.
        floor = _env_floor()
    if floor is None:
        return (tier_name, None)
    if _order[floor] <= _order[raw_in]:
        return (tier_name, None)
    return (floor, floor)


def apply_floor_to_risk_tier(
    risk_tier: Any, *,
    signal_source: str = "",
    op_id: Optional[str] = None,
    target_files: Optional[Sequence[Any]] = None,
) -> Any:
    """Slice 165 — apply the governance floor to a ``RiskTier`` ENUM (composing
    :func:`apply_floor_to_name` + the name<->RiskTier mapping that was previously
    duplicated across gate sites). Returns the floored tier, or the input unchanged
    when no floor applies / the floor is not stricter.

    This is the single, authoritative tier-level floor: call it at ANY decision
    boundary (e.g. the APPROVE auto-apply-vs-approve gate) so no classification path
    can route around the operator's MIN_RISK_TIER posture. Fail-closed via
    apply_floor_to_name (Slice 163). NEVER raises — returns the input on any error."""
    try:
        from backend.core.ouroboros.governance.risk_engine import RiskTier
        effective, applied = apply_floor_to_name(
            risk_tier.name.lower(),
            signal_source=signal_source, op_id=op_id, target_files=target_files,
        )
        if applied is None:
            return risk_tier
        tgt = {
            "safe_auto": RiskTier.SAFE_AUTO,
            "notify_apply": RiskTier.NOTIFY_APPLY,
            "approval_required": RiskTier.APPROVAL_REQUIRED,
            "blocked": RiskTier.BLOCKED,
        }.get(effective)
        if tgt is not None and risk_tier.value < tgt.value:
            return tgt
        return risk_tier
    except Exception:  # noqa: BLE001 — fail-soft; never break the gate
        return risk_tier


def floor_reason(
    now: Optional[datetime] = None,
    *,
    signal_source: str = "",
    op_id: Optional[str] = None,
    target_files: Optional[Sequence[Any]] = None,
) -> str:
    """Human-readable explanation of why the floor fires.

    Used by the orchestrator when logging a tier upgrade so the operator
    can tell *which* knob triggered the upgrade. When a VisionSensor
    source is passed, the vision-specific floor rationale is included.
    When ``op_id`` is supplied + the §37 Tier 2 #13 Slice 3 master flag
    is on, the worst observed confidence band for the op is included.
    When ``target_files`` is supplied + the §40 Wave 2 #5 RRD §1
    Boundary gate fires, the crossing-paths rationale is included.
    """
    bits: list = []
    explicit = _env_floor()
    if explicit is not None:
        bits.append(f"{_ENV_MIN_TIER}={explicit}")
    if paranoia_mode_enabled():
        bits.append(f"{_ENV_PARANOIA}=1")
    if quiet_hours_active(now):
        raw = os.environ.get(_ENV_QUIET_HOURS, "").strip()
        tz_env = os.environ.get(_ENV_QUIET_HOURS_TZ, "").strip() or "UTC"
        bits.append(f"{_ENV_QUIET_HOURS}={raw} (tz={tz_env}) active")
    if _is_vision_source(signal_source):
        try:
            tier = _vision_floor_from_env()
        except ValueError:
            # Don't fail observability formatting — name the bad env value.
            raw_env = os.environ.get(_ENV_VISION_FLOOR, "").strip() or "(unset)"
            bits.append(
                f"signal_source=vision_sensor "
                f"{_ENV_VISION_FLOOR}={raw_env} (INVALID — rejects safe_auto)"
            )
        else:
            bits.append(
                f"signal_source=vision_sensor floor={tier} (I2)"
            )
    # §37 Tier 2 #13 Slice 3 — confidence floor reason. Only
    # included when op_id supplied + the floor actually applies.
    if op_id:
        try:
            from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
                master_enabled as _conf_master,
                worst_band_for_op as _conf_worst,
            )
            if _conf_master():
                _band = _conf_worst(op_id)
                if _band is not None and _confidence_floor_for_op(
                    op_id,
                ) is not None:
                    bits.append(
                        f"tool_confidence_band={_band.value} "
                        f"floor=notify_apply (Slice 3)"
                    )
        except Exception:  # noqa: BLE001 — defensive
            pass
    # §40 Wave 2 #5 — RRD §1 Boundary recursion-depth gate
    # rationale. Only included when target_files supplied + the
    # boundary gate actually fires (returns BOUNDARY_CROSSED).
    if target_files:
        try:
            from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
                BoundaryVerdict,
                evaluate_target_files,
            )
            report = evaluate_target_files(target_files)
            if report.verdict is BoundaryVerdict.BOUNDARY_CROSSED:
                # Bound the rendered path list at 3 entries +
                # canonical ellipsis for the rest.
                paths = list(report.crossing_paths)[:3]
                more = (
                    f" (+{len(report.crossing_paths) - 3} more)"
                    if len(report.crossing_paths) > 3 else ""
                )
                bits.append(
                    f"governance_boundary_crossed={','.join(paths)}"
                    f"{more} floor=approval_required (RRD §1)"
                )
        except Exception:  # noqa: BLE001 — defensive
            pass
    if not bits:
        return "(no floor active)"
    return ", ".join(bits)

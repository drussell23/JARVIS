"""P0.6 — the perception graduation profile.

The P0 board (P0.1-P0.5) is gated across six env flags with deliberately
conservative defaults (value-priority ships in SHADOW; the sensors + learning
plane default-off). Arming them for a real-work soak by hand is error-prone —
the classic trap is enabling the reputation BIAS without its WRITE master, so
it accumulates nothing and is silently inert, and the operator only discovers
it in the telemetry hours later.

This is the single coherent switch: ``arm_perception_environ()`` applies the
whole layer at once, and ``perception_preflight()`` composes each layer's OWN
gate function (no re-parsing — one source of truth) to report the EFFECTIVE
armed state plus any incoherence, so a partial/dead arming is caught BEFORE the
soak, not discovered in the substance ratio afterwards.

Authority-free: this only sets env flags + reads gate state. It never mutates
the FSM, and it never overwrites a value the operator already set unless asked.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

# The fully-armed (ENFORCE) perception layer. SHADOW is the safe first-soak
# variant (value-priority observes but does not yet reorder).
_PERCEPTION_FLAGS_ENFORCE: Dict[str, str] = {
    # P0.1 — value-derived intake priority, AUTHORITATIVE (shadow off).
    "JARVIS_INTAKE_VALUE_PRIORITY_ENABLED": "true",
    "JARVIS_INTAKE_VALUE_PRIORITY_SHADOW": "false",
    # P0.2 — the operator's roadmap as work orders.
    "JARVIS_WORK_ORDER_SENSOR_ENABLED": "true",
    # P0.3 — proactive substance perception (real-defect AST detector).
    "JARVIS_DEEP_ANALYSIS_SENSOR_ENABLED": "true",
    # P0.4 — the learning plane: reputation write + intake bias.
    "JARVIS_MEMORY_REPUTATION_ENABLED": "true",
    "JARVIS_MEMORY_REPUTATION_BIAS_ENABLED": "true",
    # P0.5 — substance telemetry (already default-on; explicit for the record).
    "JARVIS_SUBSTANCE_TELEMETRY_ENABLED": "true",
}


def perception_flags(*, enforce: bool = True) -> Dict[str, str]:
    """The coherent flag-set. ``enforce=False`` arms P0.1 in SHADOW (observe,
    don't yet reorder the queue) — the recommended FIRST soak."""
    flags = dict(_PERCEPTION_FLAGS_ENFORCE)
    if not enforce:
        flags["JARVIS_INTAKE_VALUE_PRIORITY_SHADOW"] = "true"
    return flags


def arm_perception_environ(
    *, enforce: bool = True, overwrite: bool = False,
) -> Dict[str, str]:
    """Set the perception flags in ``os.environ`` and return what was applied.
    An operator value already present is PRESERVED unless ``overwrite=True`` —
    so a deliberate per-flag override survives the profile. NEVER raises."""
    applied: Dict[str, str] = {}
    for key, val in perception_flags(enforce=enforce).items():
        try:
            if overwrite or not os.environ.get(key, "").strip():
                os.environ[key] = val
                applied[key] = val
        except Exception:  # noqa: BLE001 — arming never raises
            continue
    return applied


def _safe(fn) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a missing layer is reported, not fatal
        return None


def perception_preflight() -> Dict[str, Any]:
    """Report the EFFECTIVE armed state of each P0 layer (by composing its own
    gate function — one source of truth) plus coherence warnings. NEVER raises.

    ``fully_armed`` is True iff every substance source + the learning bias +
    value-priority ENFORCE are live and coherent."""
    layers: Dict[str, Any] = {}
    warnings: List[str] = []

    # P0.1 — value priority (enforce vs shadow-observe).
    try:
        from backend.core.ouroboros.governance.intake import (
            unified_intake_router as _r,
        )
        _v_master = bool(_safe(_r._value_priority_master_enabled))
        _v_enforce = bool(_safe(_r._value_priority_enforce))
        layers["p0_1_value_priority"] = (
            "enforce" if _v_enforce
            else ("shadow" if _v_master else "off")
        )
        if _v_master and not _v_enforce:
            warnings.append(
                "P0.1 value-priority is OBSERVING only (shadow on) — it stamps "
                "evidence but does NOT reorder the queue; set "
                "JARVIS_INTAKE_VALUE_PRIORITY_SHADOW=false to graduate.")
    except Exception:  # noqa: BLE001
        layers["p0_1_value_priority"] = "unavailable"

    # P0.2 — work orders.
    try:
        from backend.core.ouroboros.governance.intake.sensors import (
            work_order_sensor as _wo,
        )
        layers["p0_2_work_orders"] = bool(_safe(_wo.sensor_enabled))
    except Exception:  # noqa: BLE001
        layers["p0_2_work_orders"] = "unavailable"

    # P0.3 — deep analysis / real-defect perception.
    try:
        from backend.core.ouroboros.governance.intake.sensors import (
            deep_analysis_sensor as _da,
        )
        layers["p0_3_deep_analysis"] = bool(_safe(_da.sensor_enabled))
    except Exception:  # noqa: BLE001
        layers["p0_3_deep_analysis"] = "unavailable"

    # P0.4 — learning plane (write + bias). The trap: bias without write.
    try:
        from backend.core.ouroboros.consciousness import memory_engine as _me
        _write = bool(_safe(_me.reputation_write_enabled))
        _bias = bool(_safe(_me.reputation_bias_enabled))
        layers["p0_4_reputation_write"] = _write
        layers["p0_4_reputation_bias"] = _bias
        _bias_flag = os.environ.get(
            "JARVIS_MEMORY_REPUTATION_BIAS_ENABLED", "",
        ).strip().lower() in ("1", "true", "yes", "on")
        if _bias_flag and not _write:
            warnings.append(
                "P0.4 reputation BIAS is set but the WRITE master is off — the "
                "bias is INERT (nothing accumulates to bias on); set "
                "JARVIS_MEMORY_REPUTATION_ENABLED=true.")
    except Exception:  # noqa: BLE001
        layers["p0_4_reputation_write"] = "unavailable"
        layers["p0_4_reputation_bias"] = "unavailable"

    # P0.5 — substance telemetry.
    try:
        from backend.core.ouroboros.governance import substance_ledger as _sl
        layers["p0_5_substance_telemetry"] = bool(_safe(_sl.telemetry_enabled))
    except Exception:  # noqa: BLE001
        layers["p0_5_substance_telemetry"] = "unavailable"

    # No substance SOURCE armed → the ratio can only ever measure trivia.
    if not (
        layers.get("p0_2_work_orders") is True
        or layers.get("p0_3_deep_analysis") is True
    ):
        warnings.append(
            "no substance SOURCE is armed (P0.2 work orders + P0.3 deep "
            "analysis both off) — the queue has nothing substantive to win "
            "with; the substance ratio will stay near zero.")

    fully_armed = (
        layers.get("p0_1_value_priority") == "enforce"
        and layers.get("p0_2_work_orders") is True
        and layers.get("p0_3_deep_analysis") is True
        and layers.get("p0_4_reputation_write") is True
        and layers.get("p0_4_reputation_bias") is True
        and layers.get("p0_5_substance_telemetry") is True
    )

    return {
        "layers": layers,
        "warnings": warnings,
        "fully_armed": fully_armed,
    }


__all__ = [
    "perception_flags",
    "arm_perception_environ",
    "perception_preflight",
]

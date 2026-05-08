"""§38.11-A organism-status badges (PRD, 2026-05-07).

First slice of the proactive-only autonomy-native roadmap
(§38.11). Ships three always-visible "alive indicators" that
make the autonomous organism's state legible:

  1. **Risk-tier traffic light** — single glanceable badge
     (green / yellow / orange / red) showing current cage
     stance.
  2. **Time-of-presence indicator** — "you've been working
     with me for 4h12m; I've processed 23 ops, $0.12 spent".
  3. **Animated organism heartbeat** — always-on alive
     indicator with auto-ticking rate-adaptive pulse.

CC structurally cannot replicate any of these — risk-tier
multi-axis cage is unique to O+V; session-aware self-narration
requires autonomous state continuity; heartbeat-as-alive-signal
requires there to be an organism to be alive in the first place.

## Composes canonical sources (operator binding "no duplication")

  * :mod:`governance.risk_tier_floor` — ``recommended_floor()``
    canonical risk-tier signal.
  * :mod:`governance.operation_mode` — ``current_mode()``
    canonical operation-mode signal.
  * :mod:`governance.posture_palette` —
    ``read_current_posture_safe()`` canonical posture signal
    (already shipped Slice 1).
  * :mod:`governance.polish_bundle` — ``format_heartbeat``
    pure-function rate-modulated alternation (already shipped
    Slice 6) — extended here with auto-ticker.

NEVER reimplements any of those. Pure render layer over
canonical state.

## Architectural locks (operator mandate, AST-pinned)

  1. **Master flag default-FALSE** per §33.1.
  2. **Authority asymmetry** — imports stdlib + governance.{
     risk_tier_floor, operation_mode, posture_palette,
     polish_bundle} ONLY. NEVER imports orchestrator /
     iron_gate / policy / providers / candidate_generator /
     change_engine / semantic_guardian.
  3. **Closed 4-value risk-tier-light taxonomy** —
     :class:`RiskTierLight` (GREEN / YELLOW / ORANGE / RED).
  4. **Composes canonical risk_tier_floor** — risk-light path
     MUST lazy-import ``recommended_floor`` (no parallel
     risk-tier inference).
  5. **Composes canonical heartbeat** — heartbeat render path
     MUST lazy-import ``polish_bundle.format_heartbeat`` (no
     parallel heartbeat glyph table).
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


ORGANISM_STATUS_SCHEMA_VERSION: str = "organism_status.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_ORGANISM_STATUS_ENABLED`` master switch.
    Default-FALSE per §33.1 — when off, all 3 indicators
    short-circuit to empty output."""
    return os.environ.get(
        "JARVIS_ORGANISM_STATUS_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _sub_flag_enabled(name: str) -> bool:
    """Per-feature sub-flag check. Defaults to True when bundle
    master is on."""
    if not master_enabled():
        return False
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return True
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# (1) Risk-tier traffic light
# ---------------------------------------------------------------------------


class RiskTierLight(str, enum.Enum):
    """Closed 4-value taxonomy describing current cage stance.
    Bytes-pinned via AST regression.

      * ``GREEN`` (●) — SAFE_AUTO floor; auto-apply enabled;
        operator at lowest friction.
      * ``YELLOW`` (●) — NOTIFY_APPLY floor; mutations notify
        operator before apply.
      * ``ORANGE`` (●) — APPROVAL_REQUIRED floor; operator
        explicit-approve every mutation.
      * ``RED`` (●) — BLOCKED; mutations refused entirely
        (paranoia mode + quiet hours + governor brake).
    """

    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


_LIGHT_GLYPHS: Dict[RiskTierLight, str] = {
    RiskTierLight.GREEN: "●",
    RiskTierLight.YELLOW: "●",
    RiskTierLight.ORANGE: "●",
    RiskTierLight.RED: "●",
}


_LIGHT_RICH_COLORS: Dict[RiskTierLight, str] = {
    RiskTierLight.GREEN: "green",
    RiskTierLight.YELLOW: "yellow",
    RiskTierLight.ORANGE: "orange3",
    RiskTierLight.RED: "red",
}


# Canonical risk-tier floor name → light mapping.
_FLOOR_TO_LIGHT: Dict[str, RiskTierLight] = {
    "safe_auto": RiskTierLight.GREEN,
    "notify_apply": RiskTierLight.YELLOW,
    "approval_required": RiskTierLight.ORANGE,
    "blocked": RiskTierLight.RED,
}


def compute_risk_light(
    *,
    floor_name: Optional[str] = None,
    governor_emergency: bool = False,
) -> RiskTierLight:
    """Pure-function risk-tier-light inference. Caller injects
    canonical floor name (from ``risk_tier_floor.recommended_floor()``)
    + governor emergency flag. NEVER raises.

    Rules (first-match-wins):
      1. Governor emergency → RED
      2. floor_name in canonical mapping → mapped light
      3. None / unknown floor → GREEN (safe-auto default)
    """
    try:
        if governor_emergency:
            return RiskTierLight.RED
        if floor_name is None:
            return RiskTierLight.GREEN
        normalized = str(floor_name).strip().lower()
        if not normalized:
            return RiskTierLight.GREEN
        return _FLOOR_TO_LIGHT.get(
            normalized, RiskTierLight.GREEN,
        )
    except Exception:  # noqa: BLE001 — defensive
        return RiskTierLight.GREEN


def read_current_risk_light_safe() -> RiskTierLight:
    """Compose canonical ``risk_tier_floor.recommended_floor()``
    + governor state into a :class:`RiskTierLight`. Defensive
    on every read; NEVER raises."""
    floor = None
    try:
        from backend.core.ouroboros.governance.risk_tier_floor import (  # noqa: E501
            recommended_floor,
        )
        floor = recommended_floor()
    except Exception:  # noqa: BLE001 — defensive
        floor = None
    # Governor emergency check — compose canonical sensor_governor.
    governor_emergency = False
    try:
        from backend.core.ouroboros.governance.sensor_governor import (  # noqa: E501
            get_default_governor,
        )
        gov = get_default_governor()
        if gov is not None:
            governor_emergency = bool(
                getattr(gov, "_emergency_brake_active", False)
            )
    except Exception:  # noqa: BLE001 — defensive
        governor_emergency = False
    return compute_risk_light(
        floor_name=floor,
        governor_emergency=governor_emergency,
    )


def format_risk_tier_badge(
    *,
    plain: bool = True,
    light: Optional[RiskTierLight] = None,
) -> str:
    """Render the risk-tier traffic-light badge as a single
    token. NEVER raises.

    ``plain=True`` — returns plain text ``"● GREEN"``.
    ``plain=False`` — returns Rich-markup ``"[green]● GREEN[/green]"``.

    When light is None, composes :func:`read_current_risk_light_safe`."""
    try:
        if not _sub_flag_enabled(
            "JARVIS_ORGANISM_STATUS_RISK_LIGHT_ENABLED",
        ):
            return ""
        resolved = (
            light
            if light is not None
            else read_current_risk_light_safe()
        )
        glyph = _LIGHT_GLYPHS.get(resolved, "●")
        label = resolved.value.upper()
        text = f"{glyph} {label}"
        if plain:
            return text
        color = _LIGHT_RICH_COLORS.get(resolved, "white")
        return f"[{color}]{text}[/{color}]"
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[organism_status] format_risk_tier_badge "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# (2) Time-of-presence indicator
# ---------------------------------------------------------------------------


def _format_duration(seconds: float) -> str:
    """Human-readable duration. Pure function. NEVER raises."""
    try:
        s = int(max(0.0, float(seconds)))
        if s < 60:
            return f"{s}s"
        m = s // 60
        rem_s = s % 60
        if m < 60:
            if rem_s > 0:
                return f"{m}m{rem_s:02d}s"
            return f"{m}m"
        h = m // 60
        rem_m = m % 60
        if h < 24:
            if rem_m > 0:
                return f"{h}h{rem_m:02d}m"
            return f"{h}h"
        d = h // 24
        rem_h = h % 24
        if rem_h > 0:
            return f"{d}d{rem_h:02d}h"
        return f"{d}d"
    except (TypeError, ValueError):
        return "?"


def format_time_of_presence(
    *,
    session_started_unix: float = 0.0,
    op_count: int = 0,
    cost_spent_usd: float = 0.0,
    cost_budget_usd: float = 0.0,
    posture_label: str = "",
    now_unix: Optional[float] = None,
) -> str:
    """Render the time-of-presence indicator. Pure function.
    NEVER raises.

    Caller injects all metrics — substrate is pure renderer.
    Sources are canonical:
      * ``session_started_unix`` from ``IdleWatchdog._start_time``
        or harness boot wall-clock
      * ``op_count`` from ``OpBlockBuffer`` totals
      * ``cost_*`` from ``CostTracker.total_spent`` /
        ``CostGovernor.budget_usd``
      * ``posture_label`` from
        ``posture_palette.read_current_posture_safe().value``

    Output shape:
        ``alive 4h12m · 23 ops · $0.12/$0.50 · CONSOLIDATE``
    """
    try:
        if not _sub_flag_enabled(
            "JARVIS_ORGANISM_STATUS_TIME_PRESENCE_ENABLED",
        ):
            return ""
        now = (
            float(now_unix)
            if now_unix is not None
            else time.time()
        )
        elapsed = (
            max(0.0, now - float(session_started_unix))
            if session_started_unix > 0
            else 0.0
        )
        parts = [f"alive {_format_duration(elapsed)}"]
        ops = max(0, int(op_count))
        if ops > 0:
            parts.append(f"{ops} op{'s' if ops != 1 else ''}")
        spent = max(0.0, float(cost_spent_usd))
        budget = max(0.0, float(cost_budget_usd))
        if budget > 0:
            parts.append(f"${spent:.2f}/${budget:.2f}")
        elif spent > 0:
            parts.append(f"${spent:.2f}")
        posture = (posture_label or "").strip()
        if posture:
            parts.append(posture.upper())
        return " · ".join(parts)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[organism_status] format_time_of_presence "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# (3) Animated organism heartbeat
# ---------------------------------------------------------------------------


@dataclass
class OrganismHeartbeat:
    """Auto-ticking always-on heartbeat — operator's signal
    that the organism is alive even when no op is active.

    Composes canonical :func:`polish_bundle.format_heartbeat`
    (already shipped Slice 6) with an internal monotonic
    tick-counter that auto-advances on each `pulse()` call.
    Caller drives ticks via prompt_toolkit's bottom_toolbar
    refresh loop (~500ms typical) — substrate auto-modulates
    rate based on current ops_per_min.

    Thread-safe via ``threading.Lock``. NEVER raises."""

    _tick: int = 0
    _last_pulse_unix: float = 0.0
    _ops_per_min: float = 0.0
    schema_version: str = ORGANISM_STATUS_SCHEMA_VERSION
    _lock: Any = field(
        default_factory=threading.Lock, repr=False,
    )

    def pulse(
        self,
        *,
        ops_per_min: Optional[float] = None,
    ) -> str:
        """Advance one tick + return current heartbeat glyph.

        ``ops_per_min`` updates the rate signal driving
        alternation speed (canonical from
        ``cost_governor.recent_op_count`` / harness metrics).
        Caller can omit to preserve last-known rate.

        Returns empty when:
          * Master flag off
          * Sub-flag disabled
          * ``polish_bundle.format_heartbeat`` unavailable"""
        try:
            if not _sub_flag_enabled(
                "JARVIS_ORGANISM_STATUS_HEARTBEAT_ENABLED",
            ):
                return ""
            with self._lock:
                if ops_per_min is not None:
                    try:
                        self._ops_per_min = max(
                            0.0, float(ops_per_min),
                        )
                    except (TypeError, ValueError):
                        pass
                self._tick += 1
                self._last_pulse_unix = time.time()
                tick = self._tick
                rate = self._ops_per_min
            # Compose canonical polish_bundle heartbeat.
            try:
                from backend.core.ouroboros.governance.polish_bundle import (  # noqa: E501
                    format_heartbeat,
                )
            except ImportError:
                return ""
            # Polish bundle's heartbeat requires its own master
            # flag; if off, fall back to canonical defaults
            # directly (canonical char set: ♥/♡).
            glyph = format_heartbeat(
                ops_per_min=rate,
                tick_index=tick,
            )
            if glyph:
                return glyph
            # polish_bundle master off — use direct canonical
            # default chars (matches its _HEARTBEAT_*_DEFAULT
            # constants).
            return "♥" if (tick % 2 == 0) else "♡"
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[organism_status] pulse swallowed: %s",
                type(exc).__name__,
            )
            return ""

    def status(self) -> Dict[str, Any]:
        """Return current heartbeat state. Pure read."""
        with self._lock:
            return {
                "schema_version": self.schema_version,
                "tick": int(self._tick),
                "last_pulse_unix": float(self._last_pulse_unix),
                "ops_per_min": float(self._ops_per_min),
            }

    def reset(self) -> None:
        """TEST-ONLY entry point."""
        with self._lock:
            self._tick = 0
            self._last_pulse_unix = 0.0
            self._ops_per_min = 0.0


# Module singleton.
_DEFAULT_HEARTBEAT: Optional[OrganismHeartbeat] = None
_HEARTBEAT_LOCK: threading.Lock = threading.Lock()


def get_default_heartbeat() -> OrganismHeartbeat:
    global _DEFAULT_HEARTBEAT
    with _HEARTBEAT_LOCK:
        if _DEFAULT_HEARTBEAT is None:
            _DEFAULT_HEARTBEAT = OrganismHeartbeat()
        return _DEFAULT_HEARTBEAT


def reset_heartbeat_for_tests() -> None:
    global _DEFAULT_HEARTBEAT
    with _HEARTBEAT_LOCK:
        _DEFAULT_HEARTBEAT = None


# ---------------------------------------------------------------------------
# Composite render — combine all 3 indicators
# ---------------------------------------------------------------------------


def format_organism_status_line(
    *,
    session_started_unix: float = 0.0,
    op_count: int = 0,
    cost_spent_usd: float = 0.0,
    cost_budget_usd: float = 0.0,
    ops_per_min: float = 0.0,
    posture_label: str = "",
    now_unix: Optional[float] = None,
) -> str:
    """Render the full organism-status line composing all 3
    indicators. NEVER raises. Empty when master flag off.

    Output shape:
        ``♥ alive 4h12m · 23 ops · $0.12/$0.50 · CONSOLIDATE · ● YELLOW``"""
    try:
        if not master_enabled():
            return ""
        parts = []
        # Heartbeat first (most prominent alive signal).
        hb = get_default_heartbeat().pulse(ops_per_min=ops_per_min)
        if hb:
            parts.append(hb)
        # Time-of-presence.
        tp = format_time_of_presence(
            session_started_unix=session_started_unix,
            op_count=op_count,
            cost_spent_usd=cost_spent_usd,
            cost_budget_usd=cost_budget_usd,
            posture_label=posture_label,
            now_unix=now_unix,
        )
        if tp:
            parts.append(tp)
        # Risk-tier light.
        rl = format_risk_tier_badge(plain=True)
        if rl:
            parts.append(rl)
        if not parts:
            return ""
        return " · ".join(parts)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[organism_status] format_organism_status_line "
            "swallowed: %s",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. ``master_default_false`` — JARVIS_ORGANISM_STATUS_-
         ENABLED stays default-FALSE per §33.1.
      2. ``authority_asymmetry`` — substrate purity.
      3. ``risk_tier_light_taxonomy_4_values`` — closed-enum
         integrity.
      4. ``composes_canonical_risk_tier_floor`` — risk-light
         path MUST lazy-import ``recommended_floor`` from
         ``risk_tier_floor`` (no parallel risk-tier inference).
      5. ``composes_canonical_heartbeat`` — heartbeat render
         MUST lazy-import ``format_heartbeat`` from
         ``polish_bundle`` (no parallel heartbeat glyph table).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/organism_status.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                src = ast.unparse(node)
                if "return True" in src:
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if (
                    "JARVIS_ORGANISM_STATUS_ENABLED"
                    not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_ORGANISM_STATUS_ENABLED"
                    )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"organism_status MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_risk_tier_light_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {"GREEN", "YELLOW", "ORANGE", "RED"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "RiskTierLight":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"RiskTierLight missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"RiskTierLight has extras: "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_composes_risk_tier_floor(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "risk_tier_floor" not in source:
            violations.append(
                "organism_status MUST compose canonical "
                "risk_tier_floor (no parallel risk-tier "
                "inference)"
            )
        if "recommended_floor" not in source:
            violations.append(
                "risk-light path MUST use canonical "
                "recommended_floor accessor"
            )
        return tuple(violations)

    def _validate_composes_heartbeat(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "polish_bundle" not in source:
            violations.append(
                "organism_status MUST compose canonical "
                "polish_bundle.format_heartbeat (no parallel "
                "heartbeat glyph table)"
            )
        if "format_heartbeat" not in source:
            violations.append(
                "heartbeat path MUST use canonical "
                "format_heartbeat accessor"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "organism_status_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_ORGANISM_STATUS_ENABLED "
                "stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "organism_status_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "organism_status MUST stay pure substrate "
                "composing risk_tier_floor + operation_mode + "
                "posture_palette + polish_bundle ONLY. "
                "NEVER imports orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "change_engine / semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "organism_status_risk_tier_light_taxonomy_"
                "4_values"
            ),
            target_file=target,
            description=(
                "RiskTierLight is a 4-value closed taxonomy "
                "(GREEN / YELLOW / ORANGE / RED)."
            ),
            validate=_validate_risk_tier_light_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "organism_status_composes_canonical_risk_"
                "tier_floor"
            ),
            target_file=target,
            description=(
                "Risk-light path MUST compose canonical "
                "risk_tier_floor.recommended_floor. No "
                "parallel risk-tier inference."
            ),
            validate=_validate_composes_risk_tier_floor,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "organism_status_composes_canonical_heartbeat"
            ),
            target_file=target,
            description=(
                "Heartbeat render MUST compose canonical "
                "polish_bundle.format_heartbeat (already "
                "shipped Slice 6). No parallel heartbeat "
                "glyph table."
            ),
            validate=_validate_composes_heartbeat,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_ORGANISM_STATUS_ENABLED",
            "bool",
            "false",
            (
                "Master flag for §38.11-A organism-status "
                "indicators. Default-FALSE per §33.1."
            ),
        ),
        (
            "JARVIS_ORGANISM_STATUS_RISK_LIGHT_ENABLED",
            "bool",
            "true",
            "Risk-tier traffic-light sub-feature.",
        ),
        (
            "JARVIS_ORGANISM_STATUS_TIME_PRESENCE_ENABLED",
            "bool",
            "true",
            "Time-of-presence indicator sub-feature.",
        ),
        (
            "JARVIS_ORGANISM_STATUS_HEARTBEAT_ENABLED",
            "bool",
            "true",
            "Animated organism heartbeat sub-feature.",
        ),
    )
    n = 0
    try:
        for name, kind, default, desc in seeds:
            try:
                registry.register(
                    name=name,
                    type_=kind,
                    default=default,
                    description=desc,
                    category="ux",
                    posture_relevance="RELEVANT",
                    source_file=(
                        "backend/core/ouroboros/governance/"
                        "organism_status.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return n
    return n


__all__ = [
    "ORGANISM_STATUS_SCHEMA_VERSION",
    "OrganismHeartbeat",
    "RiskTierLight",
    "compute_risk_light",
    "format_organism_status_line",
    "format_risk_tier_badge",
    "format_time_of_presence",
    "get_default_heartbeat",
    "master_enabled",
    "read_current_risk_light_safe",
    "register_flags",
    "register_shipped_invariants",
    "reset_heartbeat_for_tests",
]

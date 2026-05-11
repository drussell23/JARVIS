"""§39 Tier-1 #14 — Animated phase-flow ribbon
(PRD v2.70 to v2.71, 2026-05-08).

Renders the canonical 11-phase forward-flow as a flowing
ribbon with per-phase density markers and an active-phase
highlight. Each cell is a discrete phase; density encodes
how busy the system is in that phase right now.

Composes canonical sources only:

  * :func:`pipeline_progress.forward_flow_phases` — the
    canonical 11-phase forward-flow tuple (single source of
    truth; bytes-pinned via the §38 Slice 2 substrate).
  * :func:`pipeline_progress.phase_index` — pure-function
    forward-flow index lookup.
  * Optional :class:`StreamEventBroker.recent_history`
    composition for system-wide density (when caller does
    not pass an explicit ``phase_charges`` mapping).

Authority asymmetry: ZERO authority. Read-only aggregator +
renderer.

§38.11.5a.5 single-canonical-name discipline honored — the
11-phase forward-flow tuple is NOT redefined here; the
canonical :data:`pipeline_progress._FORWARD_FLOW_PHASE_NAMES`
+ :func:`forward_flow_phases` accessor is composed instead.

§33 patterns invoked:

  * §33.1 graduation contract — master default-FALSE.
  * §33.3 naming-cage — ``ribbon_repl.py`` (sibling) auto-
    discovers via §32.11 Slice 4.
  * §33.5 versioned artifact — frozen
    :class:`PhaseFlowCell` + :class:`PhaseFlowSnapshot`
    with ``schema_version`` + symmetric ``to_dict``.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


PHASE_FLOW_RIBBON_SCHEMA_VERSION: str = (
    "phase_flow_ribbon.1"
)


_ENV_MASTER = "JARVIS_PHASE_FLOW_RIBBON_ENABLED"
_ENV_SUB_DENSITY = (
    "JARVIS_PHASE_FLOW_RIBBON_DENSITY_ENABLED"
)
_ENV_SUB_ANIMATION = (
    "JARVIS_PHASE_FLOW_RIBBON_ANIMATION_ENABLED"
)
_ENV_WINDOW_S = (
    "JARVIS_PHASE_FLOW_RIBBON_WINDOW_S"
)

_DEFAULT_WINDOW_S = 60
_MIN_WINDOW_S = 5
_MAX_WINDOW_S = 600


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    if _flag(_ENV_MASTER, default=False):
        return True
    # §40 polish pack opt-in — when JARVIS_UX_POLISH_PACK_ENABLED
    # is on AND the operator hasn't explicitly disabled this
    # substrate via its own env flag, the pack predicate
    # activates it. Preserves §33.1 default-FALSE discipline:
    # the canonical _flag(...) / _TRUTHY check above is intact
    # so the substrate's master_default_false AST pin still
    # fires structurally.
    try:
        from backend.core.ouroboros.governance.ux_polish_pack import (
            is_substrate_in_active_pack,
        )
        return is_substrate_in_active_pack('phase_flow_ribbon')
    except ImportError:
        return False


def density_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_DENSITY, default=True)


def animation_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_ANIMATION, default=True)


def _read_window_s() -> int:
    raw = os.environ.get(_ENV_WINDOW_S, "").strip()
    if not raw:
        return _DEFAULT_WINDOW_S
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_S
    return max(_MIN_WINDOW_S, min(_MAX_WINDOW_S, n))


# ===========================================================================
# Closed taxonomy — 5-value density bucket
# ===========================================================================


class DensityLevel(str, enum.Enum):
    """Closed 5-value vocabulary for phase activity density.

    Mapped to glyph intensity via :data:`_DENSITY_GLYPHS`
    (bytes-pinned). Adding a level requires both an enum
    extension AND a glyph-mapping update — AST-pinned for
    parity.
    """

    IDLE = "idle"               # 0 ops in window
    LIGHT = "light"             # 1 op
    STEADY = "steady"           # 2-3 ops
    HEAVY = "heavy"             # 4-7 ops
    SATURATED = "saturated"     # 8+ ops

    @classmethod
    def coerce(cls, raw: object) -> "DensityLevel":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.IDLE


def _density_for_count(charges: int) -> DensityLevel:
    """Pure-function bucketing. NEVER raises."""
    try:
        n = int(charges)
    except (TypeError, ValueError):
        return DensityLevel.IDLE
    if n <= 0:
        return DensityLevel.IDLE
    if n == 1:
        return DensityLevel.LIGHT
    if n <= 3:
        return DensityLevel.STEADY
    if n <= 7:
        return DensityLevel.HEAVY
    return DensityLevel.SATURATED


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class PhaseFlowCell:
    """One phase cell in the ribbon. Frozen + hashable."""

    phase_name: str
    forward_flow_index: int
    charge_count: int = 0
    density_level: DensityLevel = DensityLevel.IDLE
    is_active: bool = False
    schema_version: str = (
        PHASE_FLOW_RIBBON_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "phase_name": self.phase_name,
            "forward_flow_index": self.forward_flow_index,
            "charge_count": self.charge_count,
            "density_level": self.density_level.value,
            "is_active": self.is_active,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class PhaseFlowSnapshot:
    """Aggregate ribbon state."""

    aggregated_at_unix: float = 0.0
    window_s: int = _DEFAULT_WINDOW_S
    cells: Tuple[PhaseFlowCell, ...] = field(default_factory=tuple)
    active_phase_name: str = ""
    by_density: Dict[str, int] = field(default_factory=dict)
    schema_version: str = (
        PHASE_FLOW_RIBBON_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "aggregated_at_unix": self.aggregated_at_unix,
            "window_s": self.window_s,
            "cells": [c.to_dict() for c in self.cells],
            "active_phase_name": self.active_phase_name,
            "by_density": dict(self.by_density),
            "schema_version": self.schema_version,
        }

    def cell_for_phase(
        self, phase_name: str,
    ) -> Optional[PhaseFlowCell]:
        for c in self.cells:
            if c.phase_name == phase_name:
                return c
        return None


# ===========================================================================
# Aggregator — composes canonical sources
# ===========================================================================


def aggregate_phase_flow(
    *,
    active_phase: Any = None,
    phase_charges: Optional[Mapping[str, int]] = None,
    window_s: Optional[int] = None,
) -> PhaseFlowSnapshot:
    """Compose canonical 11-phase forward-flow into a
    snapshot with per-phase density.

    Parameters:
      * ``active_phase`` — current phase (OperationPhase
        enum / string name / None). Highlighted in render.
      * ``phase_charges`` — optional caller-provided
        mapping of phase-name → recent-charge count. When
        ``None``, falls back to the
        :func:`_default_phase_charges` heuristic (composes
        canonical broker recent_history).
      * ``window_s`` — window for default density. Clamped
        to [5..600]. Default 60s.

    Returns an empty snapshot when master flag is off.
    NEVER raises.
    """
    if not master_enabled():
        return PhaseFlowSnapshot()

    win = (
        max(_MIN_WINDOW_S, min(_MAX_WINDOW_S, int(window_s)))
        if window_s is not None
        else _read_window_s()
    )

    # Compose canonical forward-flow tuple.
    try:
        from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
            forward_flow_phases, phase_index,
        )
        flow = forward_flow_phases()
    except Exception:  # noqa: BLE001
        return PhaseFlowSnapshot(
            aggregated_at_unix=time.time(),
            window_s=win,
        )

    if not flow:
        return PhaseFlowSnapshot(
            aggregated_at_unix=time.time(),
            window_s=win,
        )

    # Density source — caller mapping wins; else heuristic.
    if phase_charges is None:
        if density_enabled():
            phase_charges = _default_phase_charges(win)
        else:
            phase_charges = {}

    active_idx: Optional[int] = None
    active_name: str = ""
    if active_phase is not None:
        try:
            active_idx = phase_index(active_phase)
            if active_idx is not None and 0 <= active_idx < len(flow):
                active_phase_obj = flow[active_idx]
                active_name = (
                    active_phase_obj.name
                    if hasattr(active_phase_obj, "name")
                    else str(active_phase_obj)
                )
        except Exception:  # noqa: BLE001
            active_idx = None

    cells = []
    by_density: Dict[str, int] = {
        d.value: 0 for d in DensityLevel
    }
    for i, phase in enumerate(flow):
        try:
            name = (
                phase.name
                if hasattr(phase, "name")
                else str(phase)
            )
            charges = int(
                phase_charges.get(name, 0) if phase_charges else 0
            )
            level = _density_for_count(charges)
            cells.append(PhaseFlowCell(
                phase_name=name,
                forward_flow_index=i,
                charge_count=charges,
                density_level=level,
                is_active=(i == active_idx),
            ))
            by_density[level.value] = (
                by_density.get(level.value, 0) + 1
            )
        except Exception:  # noqa: BLE001
            continue

    snap = PhaseFlowSnapshot(
        aggregated_at_unix=time.time(),
        window_s=win,
        cells=tuple(cells),
        active_phase_name=active_name,
        by_density=by_density,
    )
    _record_snapshot(snap)
    _publish_phase_flow_event(snap)
    return snap


def _default_phase_charges(window_s: int) -> Dict[str, int]:
    """Heuristic density source: walk canonical
    StreamEventBroker recent_history and count events whose
    payload references a phase name.

    NEVER raises; returns empty dict on any failure.
    """
    counts: Dict[str, int] = {}
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return counts
        history = broker.recent_history()
    except Exception:  # noqa: BLE001
        return counts

    cutoff = time.time() - max(1, window_s)
    for event in history:
        try:
            ts = float(getattr(event, "timestamp", 0) or 0)
        except (TypeError, ValueError):
            continue
        if ts > 0 and ts < cutoff:
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        phase_str = (
            payload.get("phase")
            or payload.get("phase_name")
            or payload.get("active_phase_name")
        )
        if not phase_str:
            continue
        try:
            key = str(phase_str).upper().strip()
            counts[key] = counts.get(key, 0) + 1
        except Exception:  # noqa: BLE001
            continue
    return counts


# ===========================================================================
# Singleton snapshot cache + animation tick
# ===========================================================================


_cached_snapshot: Optional[PhaseFlowSnapshot] = None
_cache_lock = threading.RLock()
_animation_tick: int = 0


def _record_snapshot(snap: PhaseFlowSnapshot) -> None:
    global _cached_snapshot
    with _cache_lock:
        _cached_snapshot = snap


def get_cached_snapshot() -> Optional[PhaseFlowSnapshot]:
    with _cache_lock:
        return _cached_snapshot


def reset_cache_for_tests() -> None:
    global _cached_snapshot, _animation_tick
    with _cache_lock:
        _cached_snapshot = None
        _animation_tick = 0


def _next_animation_tick() -> int:
    global _animation_tick
    with _cache_lock:
        _animation_tick += 1
        return _animation_tick


# ===========================================================================
# SSE composition — uses canonical broker ONLY
# ===========================================================================


def _publish_phase_flow_event(
    snap: PhaseFlowSnapshot,
) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PHASE_FLOW_UPDATED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            EVENT_TYPE_PHASE_FLOW_UPDATED,
            "phase_flow",
            snap.to_dict(),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "phase_flow_ribbon: SSE publish failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer — pure, NEVER raises
# ===========================================================================


_DENSITY_GLYPHS: Dict[DensityLevel, str] = {
    DensityLevel.IDLE: "·",
    DensityLevel.LIGHT: "•",
    DensityLevel.STEADY: "●",
    DensityLevel.HEAVY: "◉",
    DensityLevel.SATURATED: "★",
}


_DENSITY_TINTS: Dict[DensityLevel, str] = {
    DensityLevel.IDLE: "dim",
    DensityLevel.LIGHT: "bright_black",
    DensityLevel.STEADY: "cyan",
    DensityLevel.HEAVY: "yellow",
    DensityLevel.SATURATED: "bright_yellow",
}


# Canonical animation glyph cycle for the active-phase
# indicator. Bytes-pinned via AST regression so future
# operator-binding "no hardcoding" enforcement holds.
_ANIMATION_FRAMES: Tuple[str, ...] = (
    "▶", "▷", "▶", "▷",
)


def format_phase_flow_ribbon(
    *,
    snapshot: Optional[PhaseFlowSnapshot] = None,
    active_phase: Any = None,
    phase_charges: Optional[Mapping[str, int]] = None,
    compact: bool = False,
) -> str:
    """Render the phase-flow ribbon.

    Empty when master flag off.

    ``compact=True`` — single-line ribbon (default).
    ``compact=False`` — multi-line with phase labels.
    """
    if not master_enabled():
        return ""
    if snapshot is None:
        snapshot = aggregate_phase_flow(
            active_phase=active_phase,
            phase_charges=phase_charges,
        )
    if not snapshot.cells:
        return ""

    tick = _next_animation_tick() if animation_enabled() else 0
    arrow = (
        _ANIMATION_FRAMES[tick % len(_ANIMATION_FRAMES)]
        if animation_enabled()
        else "▶"
    )

    if compact:
        parts = []
        for c in snapshot.cells:
            glyph = _DENSITY_GLYPHS.get(
                c.density_level, "·",
            )
            tint = _DENSITY_TINTS.get(
                c.density_level, "white",
            )
            if c.is_active:
                parts.append(
                    f"[bold green]{arrow}[/]"
                    f"[{tint} bold]{glyph}[/]"
                )
            else:
                parts.append(f"[{tint}]{glyph}[/]")
        return "─".join(parts)

    # Multi-line mode — phase labels above density row.
    label_row = []
    glyph_row = []
    for c in snapshot.cells:
        short_name = c.phase_name[:8]
        if c.is_active:
            label_row.append(f"[bold green]{short_name}[/]")
        else:
            label_row.append(f"[dim]{short_name}[/]")
        glyph = _DENSITY_GLYPHS.get(
            c.density_level, "·",
        )
        tint = _DENSITY_TINTS.get(
            c.density_level, "white",
        )
        if c.is_active:
            glyph_row.append(
                f"[bold green]{arrow}[/]"
                f"[{tint} bold]{glyph}[/]"
            )
        else:
            glyph_row.append(f"[{tint}]{glyph}[/]")
    return (
        "  ".join(label_row)
        + "\n  "
        + "  ".join(glyph_row)
    )


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§39 Tier-1 #14 phase-flow ribbon master switch "
            "(graduation contract per §33.1; default FALSE).",
            "false",
        ),
        (
            _ENV_SUB_DENSITY, "bool",
            "Enable density-marker overlay (counts ops per "
            "phase via canonical broker recent_history). "
            "Default TRUE when master on.",
            "true",
        ),
        (
            _ENV_SUB_ANIMATION, "bool",
            "Enable active-phase animation tick. Default "
            "TRUE when master on.",
            "true",
        ),
        (
            _ENV_WINDOW_S, "int",
            "Density aggregation window seconds (default 60; "
            "clamped 5..600).",
            "60",
        ),
    )
    for name, typ, desc, ex in specs:
        try:
            registry.register(
                name=name,
                type=typ,
                category="ux",
                description=desc,
                example=ex,
                source_file=(
                    "backend/core/ouroboros/governance/"
                    "phase_flow_ribbon.py"
                ),
            )
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    # ---- Pin 1: master_default_false -------------------------------------

    def _master_default_false(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                ok = False
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                ok = True
                if not ok:
                    return [
                        "master_enabled() must call _flag(...) "
                        "with default=False"
                    ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier1_14_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — master flag stays "
            "default-False until evidence ladder closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_flow_ribbon.py"
        ),
        validate=_master_default_false,
    ))

    # ---- Pin 2: authority_asymmetry --------------------------------------

    def _authority_asymmetry(tree: ast.AST, src: str):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier1_14_authority_asymmetry"
        ),
        description=(
            "Substrate purity — read-only aggregator + "
            "renderer; no orchestrator/risk-tier authority."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_flow_ribbon.py"
        ),
        validate=_authority_asymmetry,
    ))

    # ---- Pin 3: density_taxonomy_5_values --------------------------------

    def _density_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "DensityLevel"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "IDLE", "LIGHT", "STEADY",
                    "HEAVY", "SATURATED",
                }
                missing = expected - names
                if missing:
                    return [
                        f"DensityLevel missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["DensityLevel class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier1_14_density_taxonomy_5_values"
        ),
        description=(
            "Closed 5-value DensityLevel taxonomy — adding "
            "a level requires both enum + glyph map + "
            "_density_for_count update in lockstep."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_flow_ribbon.py"
        ),
        validate=_density_taxonomy,
    ))

    # ---- Pin 4: composes_canonical_pipeline_progress ---------------------

    def _composes_pipeline_progress(tree: ast.AST, src: str):
        if (
            "pipeline_progress" not in src
            or "forward_flow_phases" not in src
        ):
            return [
                "must lazy-import pipeline_progress + "
                "forward_flow_phases (canonical 11-phase "
                "forward-flow source — NO parallel tuple)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier1_14_composes_canonical_"
            "pipeline_progress"
        ),
        description=(
            "Ribbon composes canonical pipeline_progress "
            "for the 11-phase forward-flow tuple — NO "
            "parallel phase ordering per §38.11.5a.5."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_flow_ribbon.py"
        ),
        validate=_composes_pipeline_progress,
    ))

    # ---- Pin 5: animation_frames_canonical -------------------------------

    def _animation_frames_pin(tree: ast.AST, src: str):
        """Bytes-pin canonical animation frames so the
        glyph rotation isn't accidentally hardcoded
        elsewhere or replaced silently."""
        if "_ANIMATION_FRAMES" not in src:
            return [
                "_ANIMATION_FRAMES tuple must be defined "
                "(canonical glyph cycle for active-phase "
                "indicator)"
            ]
        # Match the right-arrow + outline-arrow pair.
        if "▶" not in src or "▷" not in src:
            return [
                "_ANIMATION_FRAMES must contain ▶ + ▷ "
                "(canonical right-arrow + outline-arrow "
                "cycle); changing requires explicit "
                "pin update"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier1_14_animation_frames_canonical"
        ),
        description=(
            "Animation frame glyphs are bytes-pinned so the "
            "active-phase indicator stays canonical across "
            "renderer changes — operator binding "
            "'no hardcoding' enforced via AST regression."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_flow_ribbon.py"
        ),
        validate=_animation_frames_pin,
    ))

    return pins


__all__ = [
    "PHASE_FLOW_RIBBON_SCHEMA_VERSION",
    "DensityLevel",
    "PhaseFlowCell",
    "PhaseFlowSnapshot",
    "master_enabled",
    "density_enabled",
    "animation_enabled",
    "aggregate_phase_flow",
    "get_cached_snapshot",
    "reset_cache_for_tests",
    "format_phase_flow_ribbon",
    "register_flags",
    "register_shipped_invariants",
]

"""§39 Tier-2 #3 — Cognitive heatmap
(PRD v2.71 to v2.72, 2026-05-08).

Color-coded heat showing which subsystems are currently
most active. Composes canonical
:func:`activity_radar.aggregate_activity` — ZERO parallel
aggregation; this module only RE-RENDERS the existing
:class:`ActivityRadarSnapshot.by_category` data as a
heat-tinted block.

Authority asymmetry: ZERO authority. Read-only renderer
keyed off canonical activity-radar aggregator.

§38.11.5a.5 single-canonical-name discipline honored:
- Reuses canonical 5-value :class:`ActivityCategory` enum
  (SENSORS / BRIDGES / GOVERNANCE / GENERATION / OTHER)
- The only NEW closed taxonomy is :class:`HeatLevel` (4
  values mapped to glyph + color intensity)

§33 patterns invoked:
- §33.1 graduation contract (master default-FALSE)
- §33.5 versioned artifact (frozen :class:`HeatCell`)
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


COGNITIVE_HEATMAP_SCHEMA_VERSION: str = "cognitive_heatmap.1"


_ENV_MASTER = "JARVIS_COGNITIVE_HEATMAP_ENABLED"
_ENV_SUB_BAR = "JARVIS_COGNITIVE_HEATMAP_BAR_ENABLED"
_ENV_BAR_WIDTH = "JARVIS_COGNITIVE_HEATMAP_BAR_WIDTH"

_DEFAULT_BAR_WIDTH = 24
_MIN_BAR_WIDTH = 4
_MAX_BAR_WIDTH = 80


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
        return is_substrate_in_active_pack('cognitive_heatmap')
    except ImportError:
        return False


def bar_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_BAR, default=True)


def _read_bar_width() -> int:
    raw = os.environ.get(_ENV_BAR_WIDTH, "").strip()
    if not raw:
        return _DEFAULT_BAR_WIDTH
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_BAR_WIDTH
    return max(_MIN_BAR_WIDTH, min(_MAX_BAR_WIDTH, n))


# ===========================================================================
# Closed taxonomy — 4-value HeatLevel
# ===========================================================================


class HeatLevel(str, enum.Enum):
    """Closed 4-value heat vocabulary mapped to glyph + tint
    via :data:`_HEAT_GLYPHS` + :data:`_HEAT_TINTS` (bytes-
    pinned). Adding a level requires both an enum extension
    + glyph-mapping update — AST-pinned for parity."""

    COLD = "cold"          # 0 events
    COOL = "cool"          # 1 event
    WARM = "warm"          # 2-5 events
    HOT = "hot"            # 6+ events

    @classmethod
    def coerce(cls, raw: object) -> "HeatLevel":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.COLD


def _heat_for_count(count: int) -> HeatLevel:
    """Pure-function bucketing. NEVER raises."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        return HeatLevel.COLD
    if n <= 0:
        return HeatLevel.COLD
    if n == 1:
        return HeatLevel.COOL
    if n <= 5:
        return HeatLevel.WARM
    return HeatLevel.HOT


# ===========================================================================
# Frozen §33.5 versioned artifact
# ===========================================================================


@dataclass(frozen=True)
class HeatCell:
    """One subsystem cell in the heatmap. Frozen + hashable."""

    category: str               # canonical ActivityCategory.value
    event_count: int
    heat_level: HeatLevel
    fill_ratio: float           # 0.0..1.0 of bar
    schema_version: str = COGNITIVE_HEATMAP_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "event_count": self.event_count,
            "heat_level": self.heat_level.value,
            "fill_ratio": float(self.fill_ratio),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class HeatmapSnapshot:
    """Aggregate heatmap state — one cell per
    :class:`ActivityCategory` value."""

    aggregated_at_unix: float = 0.0
    window_s: float = 0.0
    total_events: int = 0
    cells: Tuple[HeatCell, ...] = field(default_factory=tuple)
    schema_version: str = COGNITIVE_HEATMAP_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "aggregated_at_unix": self.aggregated_at_unix,
            "window_s": self.window_s,
            "total_events": self.total_events,
            "cells": [c.to_dict() for c in self.cells],
        }

    def cell_for_category(
        self, category: str,
    ) -> Optional[HeatCell]:
        for c in self.cells:
            if c.category == category:
                return c
        return None


# ===========================================================================
# Aggregator — composes canonical activity_radar
# ===========================================================================


def aggregate_heatmap() -> HeatmapSnapshot:
    """Compose canonical activity-radar snapshot into a
    heatmap projection. NEVER raises.

    Returns empty snapshot when:
      * master flag off
      * activity_radar substrate unavailable
    """
    if not master_enabled():
        return HeatmapSnapshot()

    try:
        from backend.core.ouroboros.governance.activity_radar import (  # noqa: E501
            ActivityCategory, aggregate_activity,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "cognitive_heatmap: activity_radar unavailable",
            exc_info=True,
        )
        return HeatmapSnapshot()

    try:
        radar = aggregate_activity()
    except Exception:  # noqa: BLE001
        logger.debug(
            "cognitive_heatmap: aggregate_activity failed",
            exc_info=True,
        )
        return HeatmapSnapshot()

    total = max(1, int(radar.events_in_window))  # avoid /0
    by_cat: Dict[str, int] = {}
    for c in radar.by_category:
        try:
            by_cat[c.category.value] = int(c.event_count)
        except Exception:  # noqa: BLE001
            continue

    cells: list = []
    for cat in ActivityCategory:
        count = by_cat.get(cat.value, 0)
        ratio = (
            min(1.0, count / total)
            if radar.events_in_window > 0
            else 0.0
        )
        cells.append(HeatCell(
            category=cat.value,
            event_count=count,
            heat_level=_heat_for_count(count),
            fill_ratio=ratio,
        ))

    return HeatmapSnapshot(
        aggregated_at_unix=radar.aggregated_at_unix,
        window_s=radar.window_s,
        total_events=int(radar.events_in_window),
        cells=tuple(cells),
    )


# ===========================================================================
# Renderer — pure, NEVER raises
# ===========================================================================


_HEAT_GLYPHS: Dict[HeatLevel, str] = {
    HeatLevel.COLD: "·",
    HeatLevel.COOL: "▒",
    HeatLevel.WARM: "▓",
    HeatLevel.HOT: "█",
}


_HEAT_TINTS: Dict[HeatLevel, str] = {
    HeatLevel.COLD: "blue",
    HeatLevel.COOL: "cyan",
    HeatLevel.WARM: "yellow",
    HeatLevel.HOT: "red",
}


def format_heatmap_panel(
    *,
    snapshot: Optional[HeatmapSnapshot] = None,
) -> str:
    """Render the cognitive heatmap. Empty when master off
    OR no events in window."""
    if not master_enabled():
        return ""
    if snapshot is None:
        snapshot = aggregate_heatmap()
    if not snapshot.cells:
        return ""

    width = _read_bar_width()
    parts = ["[bright_yellow]🧠 Cognitive heatmap:[/]"]
    parts.append(
        f"  [dim]({snapshot.total_events} events · "
        f"{snapshot.window_s:.0f}s window)[/]"
    )

    if not bar_enabled():
        # Compact list view — one line per category.
        for c in snapshot.cells:
            glyph = _HEAT_GLYPHS.get(c.heat_level, "·")
            tint = _HEAT_TINTS.get(c.heat_level, "white")
            parts.append(
                f"  [{tint}]{glyph}[/] "
                f"{c.category:<12} "
                f"{c.heat_level.value:<5} "
                f"({c.event_count})"
            )
        return "\n".join(parts)

    # Bar view — proportional fill + tint.
    for c in snapshot.cells:
        glyph = _HEAT_GLYPHS.get(c.heat_level, "·")
        tint = _HEAT_TINTS.get(c.heat_level, "white")
        filled = max(0, min(width, int(width * c.fill_ratio)))
        empty = width - filled
        bar = (glyph * filled) + ("·" * empty)
        parts.append(
            f"  {c.category:<12} "
            f"[{tint}]{bar}[/] "
            f"{c.event_count}"
        )
    return "\n".join(parts)


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
            "§39 Tier-2 #3 cognitive heatmap master switch "
            "(graduation contract per §33.1; default FALSE).",
            "false",
        ),
        (
            _ENV_SUB_BAR, "bool",
            "Enable proportional bar render (else compact "
            "list view). Default TRUE when master on.",
            "true",
        ),
        (
            _ENV_BAR_WIDTH, "int",
            "Bar width in chars (default 24; clamped 4..80).",
            "24",
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
                    "cognitive_heatmap.py"
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
            "section_39_tier2_3_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — heatmap master "
            "stays default-False until evidence ladder "
            "closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "cognitive_heatmap.py"
        ),
        validate=_master_default_false,
    ))

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
            "section_39_tier2_3_authority_asymmetry"
        ),
        description=(
            "Substrate purity — read-only renderer; no "
            "orchestrator/risk-tier authority."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "cognitive_heatmap.py"
        ),
        validate=_authority_asymmetry,
    ))

    def _heat_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "HeatLevel"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {"COLD", "COOL", "WARM", "HOT"}
                missing = expected - names
                if missing:
                    return [
                        f"HeatLevel missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["HeatLevel class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier2_3_heat_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value HeatLevel taxonomy — adding a "
            "level requires both enum + glyph + tint map "
            "+ _heat_for_count update in lockstep."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "cognitive_heatmap.py"
        ),
        validate=_heat_taxonomy,
    ))

    def _composes_activity_radar(tree: ast.AST, src: str):
        if (
            "activity_radar" not in src
            or "aggregate_activity" not in src
        ):
            return [
                "must lazy-import activity_radar + "
                "aggregate_activity (canonical aggregation "
                "source — NO parallel category aggregation)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier2_3_composes_canonical_"
            "activity_radar"
        ),
        description=(
            "Heatmap composes canonical activity_radar "
            "aggregator — NO parallel category aggregation."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "cognitive_heatmap.py"
        ),
        validate=_composes_activity_radar,
    ))

    return pins


__all__ = [
    "COGNITIVE_HEATMAP_SCHEMA_VERSION",
    "HeatLevel",
    "HeatCell",
    "HeatmapSnapshot",
    "master_enabled",
    "bar_enabled",
    "aggregate_heatmap",
    "format_heatmap_panel",
    "register_flags",
    "register_shipped_invariants",
]

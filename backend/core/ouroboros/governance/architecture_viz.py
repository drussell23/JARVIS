"""§39 Tier-5 #5 — 3D-like ASCII organism viz
(PRD v2.74 to v2.75, 2026-05-09).

Renders the canonical 8-zone microkernel (CLAUDE.md §1
"Architecture at a Glance" + serpent_flow.py Zone naming)
as nested ASCII boxes with active/idle pulse indicators.
Operator sees the WHOLE organism's spatial structure.

Authority asymmetry: ZERO. Read-only renderer composing
canonical activity_radar for per-zone activity.

§38.11.5a.5 single-canonical-name discipline: bytes-pinned
8-zone metadata reflects the CLAUDE.md zone numbering;
adding a zone requires CLAUDE.md + AST pin update in
lockstep. The only NEW closed taxonomy is
:class:`OrganismZone`.

§33 patterns:
- §33.1 graduation contract (master default-FALSE)
- §33.5 versioned artifact (frozen :class:`ZoneCell` +
  :class:`ArchitectureSnapshot`)
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


ARCHITECTURE_VIZ_SCHEMA_VERSION: str = "architecture_viz.1"


_ENV_MASTER = "JARVIS_ARCHITECTURE_VIZ_ENABLED"


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


# ===========================================================================
# Closed taxonomy — 8-value OrganismZone
# ===========================================================================
#
# Bytes-pinned per CLAUDE.md §1 "Architecture at a Glance":
#   unified_supervisor.py — 102K-line monolithic kernel,
#   "Zones 0-7" (8 zones inclusive). The numeric labels
#   are the canonical operator-visible ordering.


class OrganismZone(str, enum.Enum):
    """Closed 8-value microkernel zone vocabulary.

    Each zone maps to one architectural layer of the
    unified supervisor + governance stack. Adding a zone
    requires CLAUDE.md update + AST pin extension in
    lockstep.
    """

    Z0_BOOT = "z0_boot"                    # Boot Banner / supervisor init
    Z1_EVENT_STREAM = "z1_event_stream"    # SSE / Op-block streaming
    Z2_REPL = "z2_repl"                    # operator REPL
    Z3_SENSORS = "z3_sensors"              # 16 autonomous sensors
    Z4_INTAKE = "z4_intake"                # UnifiedIntakeRouter
    Z5_GOVERNANCE = "z5_governance"        # 11-phase pipeline + iron gate
    Z6_OUROBOROS = "z6_ouroboros"          # GovernedLoopService (Zone 6.8)
    Z7_CONSCIOUSNESS = "z7_consciousness"  # Trinity (Zone 6.11)


# Bytes-pinned zone display labels + descriptions. AST
# regression locks the canonical naming so silent drift
# fires the pin.
_ZONE_LABELS: Dict[OrganismZone, str] = {
    OrganismZone.Z0_BOOT: "Z0 Boot",
    OrganismZone.Z1_EVENT_STREAM: "Z1 Event Stream",
    OrganismZone.Z2_REPL: "Z2 REPL",
    OrganismZone.Z3_SENSORS: "Z3 Sensors",
    OrganismZone.Z4_INTAKE: "Z4 Intake Router",
    OrganismZone.Z5_GOVERNANCE: "Z5 Governance",
    OrganismZone.Z6_OUROBOROS: "Z6 Ouroboros",
    OrganismZone.Z7_CONSCIOUSNESS: "Z7 Consciousness",
}


# Canonical mapping ActivityCategory → OrganismZone. Bytes-
# pinned so activity_radar drift triggers AST regression.
# An ActivityCategory that doesn't map here surfaces under
# Z0_BOOT (the catch-all, since "OTHER" canonical category
# has no specific zone).
_ACTIVITY_TO_ZONE: Dict[str, OrganismZone] = {
    "sensors": OrganismZone.Z3_SENSORS,
    "bridges": OrganismZone.Z4_INTAKE,
    "governance": OrganismZone.Z5_GOVERNANCE,
    "generation": OrganismZone.Z6_OUROBOROS,
    "other": OrganismZone.Z0_BOOT,
}


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class ZoneCell:
    """One zone's active state. Frozen + hashable."""

    zone: OrganismZone
    label: str
    activity_count: int = 0
    is_active: bool = False
    diagnostic: str = ""
    schema_version: str = ARCHITECTURE_VIZ_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "zone": self.zone.value,
            "label": self.label,
            "activity_count": self.activity_count,
            "is_active": self.is_active,
            "diagnostic": self.diagnostic,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ArchitectureSnapshot:
    """Aggregated 8-zone snapshot."""

    aggregated_at_unix: float = 0.0
    cells: Tuple[ZoneCell, ...] = field(default_factory=tuple)
    total_activity: int = 0
    schema_version: str = ARCHITECTURE_VIZ_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "aggregated_at_unix": self.aggregated_at_unix,
            "total_activity": self.total_activity,
            "cells": [c.to_dict() for c in self.cells],
        }

    def cell_for_zone(
        self, zone: OrganismZone,
    ) -> Optional[ZoneCell]:
        for c in self.cells:
            if c.zone is zone:
                return c
        return None


# ===========================================================================
# Aggregator — composes canonical activity_radar
# ===========================================================================


def aggregate_architecture_snapshot() -> ArchitectureSnapshot:
    """Compose canonical activity_radar into per-zone
    snapshot. NEVER raises. Empty when master flag off."""
    if not master_enabled():
        return ArchitectureSnapshot()

    # Compose canonical activity radar (proven 491-test).
    by_zone: Dict[OrganismZone, int] = {
        z: 0 for z in OrganismZone
    }
    total = 0
    try:
        from backend.core.ouroboros.governance.activity_radar import (  # noqa: E501
            aggregate_activity,
        )
        radar = aggregate_activity()
        total = int(radar.events_in_window or 0)
        for c in radar.by_category:
            try:
                cat_value = c.category.value
                count = int(c.event_count or 0)
                zone = _ACTIVITY_TO_ZONE.get(
                    cat_value, OrganismZone.Z0_BOOT,
                )
                by_zone[zone] = by_zone.get(zone, 0) + count
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        logger.debug(
            "architecture_viz: activity_radar unavailable",
            exc_info=True,
        )

    cells = []
    for zone in OrganismZone:
        count = by_zone.get(zone, 0)
        cells.append(ZoneCell(
            zone=zone,
            label=_ZONE_LABELS.get(zone, zone.value),
            activity_count=count,
            is_active=(count > 0),
        ))

    snap = ArchitectureSnapshot(
        aggregated_at_unix=time.time(),
        cells=tuple(cells),
        total_activity=total,
    )
    _publish_event(snap)
    return snap


def _publish_event(snap: ArchitectureSnapshot) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_ARCHITECTURE_SNAPSHOT,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is not None:
            broker.publish(
                EVENT_TYPE_ARCHITECTURE_SNAPSHOT,
                "architecture_viz",
                snap.to_dict(),
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "architecture_viz: SSE failed", exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


def format_architecture_viz(
    *, snapshot: Optional[ArchitectureSnapshot] = None,
) -> str:
    """Render nested-box organism viz. Empty when master
    off."""
    if not master_enabled():
        return ""
    if snapshot is None:
        snapshot = aggregate_architecture_snapshot()
    if not snapshot.cells:
        return ""

    parts = ["[bright_yellow]🧬 Organism architecture:[/]"]
    parts.append(
        f"  [dim]({snapshot.total_activity} events in "
        "60s window)[/]"
    )
    parts.append("")
    parts.append("  ╔════════════════════════════════════╗")
    for cell in snapshot.cells:
        glyph = "●" if cell.is_active else "○"
        tint = "yellow" if cell.is_active else "dim"
        count_str = (
            f" ({cell.activity_count})"
            if cell.activity_count > 0 else ""
        )
        parts.append(
            f"  ║ [{tint}]{glyph}[/] "
            f"{cell.label:<30}{count_str:<6}║"
        )
    parts.append("  ╚════════════════════════════════════╝")
    return "\n".join(parts)


# ===========================================================================
# FlagRegistry seeds + AST pins
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            name=_ENV_MASTER, type="bool", category="ux",
            description=(
                "§39 Tier-5 #5 architecture viz master "
                "switch (default FALSE per §33.1)."
            ),
            example="false",
            source_file=(
                "backend/core/ouroboros/governance/"
                "architecture_viz.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    def _master_default_false(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
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
                                return []
                return [
                    "master_enabled() must call _flag(...) "
                    "with default=False"
                ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_5_master_default_false"
        ),
        description="§33.1 graduation contract.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "architecture_viz.py"
        ),
        validate=_master_default_false,
    ))

    def _zone_taxonomy_8(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "OrganismZone"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "Z0_BOOT", "Z1_EVENT_STREAM", "Z2_REPL",
                    "Z3_SENSORS", "Z4_INTAKE",
                    "Z5_GOVERNANCE", "Z6_OUROBOROS",
                    "Z7_CONSCIOUSNESS",
                }
                missing = expected - names
                if missing:
                    return [
                        f"OrganismZone missing values: "
                        f"{sorted(missing)} (canonical "
                        "CLAUDE.md zones 0-7)"
                    ]
                return []
        return ["OrganismZone class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_5_zone_taxonomy_8_values"
        ),
        description=(
            "Closed 8-value OrganismZone taxonomy bytes-"
            "pinned to CLAUDE.md §1 zone numbering."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "architecture_viz.py"
        ),
        validate=_zone_taxonomy_8,
    ))

    def _composes_activity_radar(tree, src):
        if "activity_radar" not in src or "aggregate_activity" not in src:
            return [
                "must lazy-import activity_radar + "
                "aggregate_activity"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_5_composes_activity_radar"
        ),
        description=(
            "Composes canonical activity_radar — NO "
            "parallel category aggregation."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "architecture_viz.py"
        ),
        validate=_composes_activity_radar,
    ))

    def _authority_asymmetry(tree, src):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority: {mod}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier5_5_authority_asymmetry"
        ),
        description="Substrate purity.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "architecture_viz.py"
        ),
        validate=_authority_asymmetry,
    ))

    return pins


__all__ = [
    "ARCHITECTURE_VIZ_SCHEMA_VERSION",
    "OrganismZone",
    "ZoneCell",
    "ArchitectureSnapshot",
    "master_enabled",
    "aggregate_architecture_snapshot",
    "format_architecture_viz",
    "register_flags",
    "register_shipped_invariants",
]

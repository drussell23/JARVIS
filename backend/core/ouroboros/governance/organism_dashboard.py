"""§39 Tier-2 #1 — Living organism dashboard
(PRD v2.71 to v2.72, 2026-05-08).

Mission Control multi-pane view of the WHOLE organism
state. Composes EVERY canonical render surface across
§38 Slices + §38.11 (A-F) + §39 Tier-1 into one operator-
summon-able dashboard.

This module is a PURE COMPOSER. It performs ZERO
aggregation of its own — every pane is rendered by its
canonical owner. Authority asymmetry: ZERO authority;
read-only assembly.

§38.11.5a.5 single-canonical-name discipline honored:
- Every pane composes one canonical render-surface
  (organism_status / activity_radar / op_fanout_tree /
  unified_graduation_dashboard / posture_palette /
  phase_flow_ribbon / cognitive_heatmap /
  capability_constellation).
- The only NEW closed taxonomy is :class:`DashboardPane`
  (8 values — one slot per canonical-pane render surface).
- AST-pinned: must lazy-import ALL 8 canonical render
  modules; missing any compose triggers regression.

§33 patterns invoked:
- §33.1 graduation contract (master default-FALSE)
- §33.3 naming-cage (``dashboard_repl.py`` sibling auto-
  discovers via §32.11 Slice 4)
- §33.5 versioned artifact (frozen
  :class:`DashboardSnapshot`)
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)


ORGANISM_DASHBOARD_SCHEMA_VERSION: str = (
    "organism_dashboard.1"
)


_ENV_MASTER = "JARVIS_ORGANISM_DASHBOARD_ENABLED"
_ENV_LAYOUT = "JARVIS_ORGANISM_DASHBOARD_LAYOUT"

_DEFAULT_LAYOUT = "stacked"
_VALID_LAYOUTS: FrozenSet[str] = frozenset(
    {"stacked", "compact"}
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def _read_layout() -> str:
    raw = (
        os.environ.get(_ENV_LAYOUT, "")
        .strip()
        .lower()
    )
    if not raw or raw not in _VALID_LAYOUTS:
        return _DEFAULT_LAYOUT
    return raw


# ===========================================================================
# Closed taxonomy — 8-value DashboardPane
# ===========================================================================


class DashboardPane(str, enum.Enum):
    """Closed 8-value vocabulary — one slot per canonical
    render-surface. Adding a pane requires both enum
    extension + composer registration + AST pin update.
    """

    # §38.11-A composite: heartbeat + risk-light + time-of-
    # presence in a single line.
    ALIVE = "alive"
    # §38 Slice 4 — full activity radar (5 categories).
    ACTIVITY_RADAR = "activity_radar"
    # §38 Slice 5 — Move 6.5 K-way + L3 subagent fan-out.
    FANOUT = "fanout"
    # §38.11-B — graduation ticker (RADIANT/GLOWING flags).
    GRADUATION = "graduation"
    # canonical posture palette badge.
    POSTURE = "posture"
    # §39 Tier-1 #14 — animated phase-flow ribbon.
    PHASE_RIBBON = "phase_ribbon"
    # §39 Tier-2 #3 — cognitive heatmap.
    HEATMAP = "heatmap"
    # §38.11-F — capability constellation (top RADIANT
    # entries summary).
    CONSTELLATION = "constellation"

    @classmethod
    def coerce(cls, raw: object) -> Optional["DashboardPane"]:
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return None


# Default ordering for stacked render — driven entirely by
# the enum declaration order. NO parallel ordering tuple.
def _default_order() -> Tuple[DashboardPane, ...]:
    return tuple(p for p in DashboardPane)


# ===========================================================================
# Frozen §33.5 versioned artifact
# ===========================================================================


@dataclass(frozen=True)
class DashboardSnapshot:
    """Aggregated multi-pane render. Frozen + serializable."""

    aggregated_at_unix: float = 0.0
    layout: str = _DEFAULT_LAYOUT
    panes: Tuple[DashboardPane, ...] = field(
        default_factory=tuple,
    )
    rendered_panes: Dict[str, str] = field(
        default_factory=dict,
    )
    elapsed_s: float = 0.0
    schema_version: str = (
        ORGANISM_DASHBOARD_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "aggregated_at_unix": self.aggregated_at_unix,
            "layout": self.layout,
            "panes": [p.value for p in self.panes],
            "rendered_panes": dict(self.rendered_panes),
            "elapsed_s": self.elapsed_s,
        }

    def has_pane(self, pane: DashboardPane) -> bool:
        return pane.value in self.rendered_panes


# ===========================================================================
# Pane composers — each MUST compose one canonical surface
# ===========================================================================


def _compose_alive_pane() -> str:
    """§38.11-A organism_status composite."""
    try:
        from backend.core.ouroboros.governance.organism_status import (  # noqa: E501
            format_organism_status_line,
        )
        return format_organism_status_line() or ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: alive pane failed", exc_info=True,
        )
        return ""


def _compose_activity_radar_pane() -> str:
    """§38 Slice 4 activity_radar."""
    try:
        from backend.core.ouroboros.governance.activity_radar import (  # noqa: E501
            format_activity_radar,
        )
        return format_activity_radar() or ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: radar pane failed", exc_info=True,
        )
        return ""


def _compose_fanout_pane() -> str:
    """§38 Slice 5 op_fanout_tree."""
    try:
        from backend.core.ouroboros.governance.op_fanout_tree import (  # noqa: E501
            format_fanout_tree,
        )
        return format_fanout_tree() or ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: fanout pane failed", exc_info=True,
        )
        return ""


def _compose_graduation_pane() -> str:
    """§38.11-B session_continuity graduation ticker."""
    try:
        from backend.core.ouroboros.governance.session_continuity import (  # noqa: E501
            format_graduation_ticker,
        )
        return format_graduation_ticker() or ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: graduation pane failed",
            exc_info=True,
        )
        return ""


def _compose_posture_pane() -> str:
    """canonical posture_palette badge."""
    try:
        from backend.core.ouroboros.governance.posture_palette import (  # noqa: E501
            format_posture_badge,
        )
        return format_posture_badge() or ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: posture pane failed", exc_info=True,
        )
        return ""


def _compose_phase_ribbon_pane() -> str:
    """§39 Tier-1 #14 phase_flow_ribbon."""
    try:
        from backend.core.ouroboros.governance.phase_flow_ribbon import (  # noqa: E501
            format_phase_flow_ribbon,
        )
        return format_phase_flow_ribbon(compact=True) or ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: phase ribbon pane failed",
            exc_info=True,
        )
        return ""


def _compose_heatmap_pane() -> str:
    """§39 Tier-2 #3 cognitive_heatmap (sister surface)."""
    try:
        from backend.core.ouroboros.governance.cognitive_heatmap import (  # noqa: E501
            format_heatmap_panel,
        )
        return format_heatmap_panel() or ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: heatmap pane failed", exc_info=True,
        )
        return ""


def _compose_constellation_pane() -> str:
    """§38.11-F capability_constellation (RADIANT-only filter
    to keep the dashboard pane bounded)."""
    try:
        from backend.core.ouroboros.governance.capability_constellation import (  # noqa: E501
            ConstellationBrightness,
            format_constellation_panel,
        )
        return format_constellation_panel(
            only_brightness=ConstellationBrightness.RADIANT,
            limit_per_axis=3,
        ) or ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: constellation pane failed",
            exc_info=True,
        )
        return ""


# Pane → composer dispatch. Bytes-pinned closed map; the
# AST pin `composes_all_canonical_panes` enforces every
# DashboardPane value has a registered composer.
_PANE_COMPOSERS: Dict[DashboardPane, callable] = {  # type: ignore[type-arg]
    DashboardPane.ALIVE: _compose_alive_pane,
    DashboardPane.ACTIVITY_RADAR: _compose_activity_radar_pane,
    DashboardPane.FANOUT: _compose_fanout_pane,
    DashboardPane.GRADUATION: _compose_graduation_pane,
    DashboardPane.POSTURE: _compose_posture_pane,
    DashboardPane.PHASE_RIBBON: _compose_phase_ribbon_pane,
    DashboardPane.HEATMAP: _compose_heatmap_pane,
    DashboardPane.CONSTELLATION: _compose_constellation_pane,
}


# ===========================================================================
# Aggregator + Renderer
# ===========================================================================


def aggregate_dashboard(
    *,
    panes: Optional[Tuple[DashboardPane, ...]] = None,
) -> DashboardSnapshot:
    """Compose multi-pane snapshot. NEVER raises.

    ``panes`` defaults to ALL 8 panes in canonical order.
    """
    if not master_enabled():
        return DashboardSnapshot()

    started = time.monotonic()
    layout = _read_layout()
    pane_order = (
        tuple(panes) if panes else _default_order()
    )

    rendered: Dict[str, str] = {}
    for p in pane_order:
        composer = _PANE_COMPOSERS.get(p)
        if composer is None:
            continue
        try:
            text = composer()
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            rendered[p.value] = text

    snap = DashboardSnapshot(
        aggregated_at_unix=time.time(),
        layout=layout,
        panes=pane_order,
        rendered_panes=rendered,
        elapsed_s=time.monotonic() - started,
    )
    _publish_dashboard_event(snap)
    return snap


_PANE_TITLES: Dict[DashboardPane, str] = {
    DashboardPane.ALIVE: "♡ Alive",
    DashboardPane.ACTIVITY_RADAR: "📡 Activity Radar",
    DashboardPane.FANOUT: "🌳 Op Fan-out",
    DashboardPane.GRADUATION: "✨ Graduation",
    DashboardPane.POSTURE: "🧭 Posture",
    DashboardPane.PHASE_RIBBON: "▶ Phase Flow",
    DashboardPane.HEATMAP: "🧠 Heatmap",
    DashboardPane.CONSTELLATION: "🌌 Constellation",
}


def format_organism_dashboard(
    *,
    snapshot: Optional[DashboardSnapshot] = None,
    panes: Optional[Tuple[DashboardPane, ...]] = None,
) -> str:
    """Render the multi-pane dashboard. Empty when master
    off OR no panes have content."""
    if not master_enabled():
        return ""
    if snapshot is None:
        snapshot = aggregate_dashboard(panes=panes)
    if not snapshot.rendered_panes:
        return ""

    sections = ["[bright_yellow]🪐 ORGANISM DASHBOARD[/]"]
    sections.append(
        f"[dim]aggregated {snapshot.aggregated_at_unix:.0f} · "
        f"{len(snapshot.rendered_panes)} panes · "
        f"{snapshot.elapsed_s * 1000:.1f}ms[/]"
    )
    sections.append("")  # spacer

    for p in snapshot.panes:
        text = snapshot.rendered_panes.get(p.value, "")
        if not text:
            continue
        title = _PANE_TITLES.get(p, p.value)
        sections.append(f"[bold]{title}[/]")
        if snapshot.layout == "compact":
            # Compact: trim multi-line panes to first line.
            first_line = text.split("\n", 1)[0]
            sections.append(f"  {first_line}")
        else:
            # Stacked: indent each line under the title.
            for line in text.split("\n"):
                sections.append(f"  {line}")
        sections.append("")  # spacer between panes

    return "\n".join(sections).rstrip()


# ===========================================================================
# SSE composition
# ===========================================================================


def _publish_dashboard_event(
    snap: DashboardSnapshot,
) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_DASHBOARD_RENDERED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        # Bounded payload — only pane names + sizes (not
        # the full rendered text, which can be large).
        broker.publish(
            EVENT_TYPE_DASHBOARD_RENDERED,
            "dashboard",
            {
                "schema_version": (
                    ORGANISM_DASHBOARD_SCHEMA_VERSION
                ),
                "aggregated_at_unix": (
                    snap.aggregated_at_unix
                ),
                "layout": snap.layout,
                "panes": [p.value for p in snap.panes],
                "pane_sizes": {
                    name: len(text)
                    for name, text in (
                        snap.rendered_panes.items()
                    )
                },
                "elapsed_s": snap.elapsed_s,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "dashboard: SSE publish failed", exc_info=True,
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
            "§39 Tier-2 #1 organism dashboard master switch "
            "(graduation contract per §33.1; default FALSE).",
            "false",
        ),
        (
            _ENV_LAYOUT, "string",
            "Dashboard layout mode: 'stacked' (default; "
            "full panes) or 'compact' (one-line summaries).",
            "stacked",
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
                    "organism_dashboard.py"
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
            "section_39_tier2_1_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — dashboard master "
            "stays default-False until evidence ladder "
            "closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "organism_dashboard.py"
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
            "section_39_tier2_1_authority_asymmetry"
        ),
        description=(
            "Substrate purity — pure composer; no "
            "orchestrator/risk-tier authority."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "organism_dashboard.py"
        ),
        validate=_authority_asymmetry,
    ))

    def _pane_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "DashboardPane"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "ALIVE", "ACTIVITY_RADAR", "FANOUT",
                    "GRADUATION", "POSTURE", "PHASE_RIBBON",
                    "HEATMAP", "CONSTELLATION",
                }
                missing = expected - names
                if missing:
                    return [
                        f"DashboardPane missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["DashboardPane class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier2_1_pane_taxonomy_8_values"
        ),
        description=(
            "Closed 8-value DashboardPane taxonomy — one "
            "slot per canonical pane render-surface; "
            "adding a pane requires registration in "
            "_PANE_COMPOSERS too."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "organism_dashboard.py"
        ),
        validate=_pane_taxonomy,
    ))

    def _composes_all_canonical_panes(
        tree: ast.AST, src: str,
    ):
        """Bytes-pin: every canonical pane render-surface
        MUST be lazy-imported in the composer dispatch
        functions. This is the load-bearing pin —
        accidentally dropping a compose silently breaks
        a dashboard pane."""
        required = (
            "organism_status",
            "activity_radar",
            "op_fanout_tree",
            "session_continuity",
            "posture_palette",
            "phase_flow_ribbon",
            "cognitive_heatmap",
            "capability_constellation",
        )
        missing = [m for m in required if m not in src]
        if missing:
            return [
                f"missing canonical pane composes: "
                f"{missing}"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier2_1_composes_all_canonical_panes"
        ),
        description=(
            "Dashboard composes ALL 8 canonical pane "
            "render-surfaces (organism_status / "
            "activity_radar / op_fanout_tree / "
            "session_continuity / posture_palette / "
            "phase_flow_ribbon / cognitive_heatmap / "
            "capability_constellation) — accidentally "
            "dropping any breaks a pane silently."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "organism_dashboard.py"
        ),
        validate=_composes_all_canonical_panes,
    ))

    def _pane_composer_completeness(
        tree: ast.AST, src: str,
    ):
        """The ``_PANE_COMPOSERS`` dispatch dict MUST
        cover every DashboardPane enum value. Bytes-pin
        via substring search for each pane's enum name."""
        required = (
            "DashboardPane.ALIVE",
            "DashboardPane.ACTIVITY_RADAR",
            "DashboardPane.FANOUT",
            "DashboardPane.GRADUATION",
            "DashboardPane.POSTURE",
            "DashboardPane.PHASE_RIBBON",
            "DashboardPane.HEATMAP",
            "DashboardPane.CONSTELLATION",
        )
        missing = [k for k in required if k not in src]
        if missing:
            return [
                f"_PANE_COMPOSERS missing keys: {missing}"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier2_1_pane_composer_completeness"
        ),
        description=(
            "_PANE_COMPOSERS dispatch dict must cover every "
            "DashboardPane value — adding a pane to the "
            "enum without registering its composer breaks "
            "rendering for that slot."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "organism_dashboard.py"
        ),
        validate=_pane_composer_completeness,
    ))

    return pins


__all__ = [
    "ORGANISM_DASHBOARD_SCHEMA_VERSION",
    "DashboardPane",
    "DashboardSnapshot",
    "master_enabled",
    "aggregate_dashboard",
    "format_organism_dashboard",
    "register_flags",
    "register_shipped_invariants",
]

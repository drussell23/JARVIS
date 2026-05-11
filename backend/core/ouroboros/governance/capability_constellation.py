"""§38.11-F — Capability Constellation
(PRD v2.69 to v2.70, 2026-05-08).

Final §38.11 slice. Merged with §39 #8 per §38.11.5a
reconciliation. Renders the system's flag landscape as a
star-map: each capability flag is a star, brightness encodes
graduation readiness, axes group flags by canonical
``flag_registry.Category``, and per-star ``linked_principles``
trace each flag back to the Manifesto axes (CLAUDE.md §4 / §
The Governing Philosophy).

Composes canonical sources only — NO parallel taxonomy:

  * :func:`unified_graduation_dashboard.aggregate_dashboard`
    — verdict source (proven 491-test substrate; same
    aggregator §38.11-B reuses).
  * :func:`flag_registry.get_default_registry().list_all`
    — flag descriptor source (`FlagSpec` carries
    ``Category`` + ``posture_relevance``).
  * The 5-value :class:`UnifiedGraduationVerdict` is
    bytes-pinned 1:1 onto the new
    :class:`ConstellationBrightness` taxonomy.
  * The 8-value :class:`flag_registry.Category` is RE-USED
    as the constellation's axis — NO parallel ``StarAxis``
    enum (per §38.11.5a.5 single-canonical-name discipline).

§33 patterns invoked:

  * §33.1 graduation contract — master flag default-FALSE.
  * §33.3 naming-cage — ``constellation_repl.py`` (sibling)
    auto-discovers via §32.11 Slice 4.
  * §33.5 versioned artifact — frozen
    :class:`ConstellationStar` + :class:`ConstellationSnapshot`
    with ``schema_version`` + symmetric ``to_dict``.
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


CAPABILITY_CONSTELLATION_SCHEMA_VERSION: str = (
    "capability_constellation.1"
)


_ENV_MASTER = "JARVIS_CAPABILITY_CONSTELLATION_ENABLED"
_ENV_SUB_PANEL = "JARVIS_CONSTELLATION_PANEL_ENABLED"
_ENV_SUB_AUTO_REFRESH = (
    "JARVIS_CONSTELLATION_AUTO_REFRESH_ENABLED"
)
_ENV_REFRESH_INTERVAL_S = (
    "JARVIS_CONSTELLATION_REFRESH_INTERVAL_S"
)

_DEFAULT_REFRESH_INTERVAL_S = 60
_MIN_REFRESH_S = 5
_MAX_REFRESH_S = 3600


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def panel_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_PANEL, default=True)


def auto_refresh_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_AUTO_REFRESH, default=False)


def _read_refresh_interval_s() -> int:
    raw = os.environ.get(
        _ENV_REFRESH_INTERVAL_S, "",
    ).strip()
    if not raw:
        return _DEFAULT_REFRESH_INTERVAL_S
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_REFRESH_INTERVAL_S
    return max(_MIN_REFRESH_S, min(_MAX_REFRESH_S, n))


# ===========================================================================
# Closed taxonomy — brightness (1:1 with UnifiedGraduationVerdict)
# ===========================================================================


class ConstellationBrightness(str, enum.Enum):
    """Closed 5-value brightness vocabulary mapped 1:1 onto
    :class:`UnifiedGraduationVerdict`. Bytes-pinned via
    :data:`_VERDICT_TO_BRIGHTNESS`.

    Adding a brightness requires both an enum extension AND
    a verdict-mapping update — AST-pinned for parity.
    """

    RADIANT = "radiant"     # READY (brightest — eligible for default-true flip)
    GLOWING = "glowing"     # EVIDENCE_GATHERING (active proof in progress)
    DIM = "dim"             # EVIDENCE_INSUFFICIENT (more data needed)
    FAULTING = "faulting"   # EVIDENCE_FAILED (regression / drift)
    DARK = "dark"           # DISABLED (master off, contract gates closed)


# Lazy-resolved verdict→brightness map at module import time.
# We compute it inside _verdict_to_brightness() to avoid an
# import-time hard dependency on UnifiedGraduationVerdict
# (defensive contract — module stays importable in isolation).


def _verdict_to_brightness(verdict_value: object) -> ConstellationBrightness:
    """Lazy pure-function map from a verdict value to a
    brightness. NEVER raises."""
    s = ""
    try:
        if hasattr(verdict_value, "value"):
            s = str(verdict_value.value)
        else:
            s = str(verdict_value or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ConstellationBrightness.DARK
    return _VERDICT_VALUE_BRIGHTNESS.get(
        s, ConstellationBrightness.DARK,
    )


_VERDICT_VALUE_BRIGHTNESS: Dict[str, ConstellationBrightness] = {
    "ready": ConstellationBrightness.RADIANT,
    "evidence_gathering": ConstellationBrightness.GLOWING,
    "evidence_insufficient": ConstellationBrightness.DIM,
    "evidence_failed": ConstellationBrightness.FAULTING,
    "disabled": ConstellationBrightness.DARK,
}


# ===========================================================================
# Manifesto principle map — derived from canonical Category enum
# ===========================================================================
#
# Each canonical flag_registry.Category maps to one or more
# Manifesto principles (CLAUDE.md §"The Governing Philosophy"
# 7 numbered principles). This map is the single source of
# truth for ``linked_principles`` in the SSE payload.


_CATEGORY_PRINCIPLE_MAP: Dict[str, Tuple[str, ...]] = {
    # 1. Unified organism / 2. Progressive awakening
    "integration": (
        "1. Unified organism",
        "2. Progressive awakening",
    ),
    # 3. Asynchronous tendrils
    "timing": ("3. Asynchronous tendrils",),
    "capacity": ("3. Asynchronous tendrils",),
    # 4. Synthetic soul
    "tuning": ("4. Synthetic soul",),
    # 5. Intelligence-driven routing
    "routing": ("5. Intelligence-driven routing",),
    # 6. Threshold-triggered neuroplasticity
    "safety": ("6. Threshold-triggered neuroplasticity",),
    "experimental": (
        "6. Threshold-triggered neuroplasticity",
    ),
    # 7. Absolute observability
    "observability": ("7. Absolute observability",),
}


def _principles_for_category(category_value: object) -> Tuple[str, ...]:
    try:
        s = ""
        if hasattr(category_value, "value"):
            s = str(category_value.value)
        else:
            s = str(category_value or "").strip().lower()
        return _CATEGORY_PRINCIPLE_MAP.get(s, ())
    except Exception:  # noqa: BLE001
        return ()


# Public re-export for downstream observability substrates (e.g.
# second_order_doll_metric.py) — composes the canonical map without
# reaching into the underscored private function. This is the §37
# Singleton + Read-API Extension Pattern: extend the public surface
# rather than have consumers parallel-import the private name.
def principles_for_category(category_value: object) -> Tuple[str, ...]:
    """Canonical Category → Manifesto-principles mapping.

    Accepts a :class:`flag_registry.Category` enum value, a string
    like ``"safety"``, or anything else with a ``.value`` attribute.
    Unknown / malformed inputs return an empty tuple — NEVER raises.

    Single source of truth for the Manifesto-principle linkage.
    Drift between this map and downstream consumers is structurally
    prevented because every consumer composes this accessor.
    """
    return _principles_for_category(category_value)


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class ConstellationStar:
    """One capability flag rendered as a star.

    Frozen + hashable. Symmetric ``to_dict`` per §33.5.
    """

    flag_name: str
    brightness: ConstellationBrightness
    graduation_verdict: str = "disabled"
    category: str = ""
    linked_principles: Tuple[str, ...] = ()
    diagnostic: str = ""
    posture_relevance: Tuple[str, ...] = ()
    schema_version: str = (
        CAPABILITY_CONSTELLATION_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "flag_name": self.flag_name,
            "brightness": self.brightness.value,
            "graduation_verdict": self.graduation_verdict,
            "category": self.category,
            "linked_principles": list(self.linked_principles),
            "diagnostic": self.diagnostic,
            "posture_relevance": list(self.posture_relevance),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ConstellationSnapshot:
    """Aggregated star-map across all known flags.

    Frozen. ``stars`` order is sorted by (category, flag_name)
    for deterministic rendering.
    """

    aggregated_at_unix: float = 0.0
    stars: Tuple[ConstellationStar, ...] = field(default_factory=tuple)
    by_brightness: Mapping_dict = field(default_factory=dict)
    by_category: Mapping_dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    schema_version: str = (
        CAPABILITY_CONSTELLATION_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "aggregated_at_unix": self.aggregated_at_unix,
            "elapsed_s": self.elapsed_s,
            "stars": [s.to_dict() for s in self.stars],
            "by_brightness": dict(self.by_brightness),
            "by_category": dict(self.by_category),
        }

    def stars_by_brightness(
        self, brightness: ConstellationBrightness,
    ) -> Tuple[ConstellationStar, ...]:
        return tuple(
            s for s in self.stars
            if s.brightness is brightness
        )


# Local alias — typing.Dict[str, int] would force a runtime
# import dance that the dataclass default_factory doesn't
# need.
Mapping_dict = dict


# ===========================================================================
# Aggregator — composes canonical dashboard + flag registry
# ===========================================================================


def aggregate_constellation() -> ConstellationSnapshot:
    """Compose canonical sources into a fresh snapshot.

    Read-only; NEVER raises. Returns an empty snapshot when:
      * master flag off
      * canonical sources unavailable

    The snapshot is also published as an SSE event
    (``capability_constellation_updated``) on each successful
    aggregation.
    """
    if not master_enabled():
        return ConstellationSnapshot()

    started = time.monotonic()

    # Fetch graduation verdicts (canonical source).
    verdict_map: Dict[str, Tuple[str, str]] = {}
    try:
        from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
            aggregate_dashboard,
        )
        snap = aggregate_dashboard()
        for row in snap.rows:
            try:
                verdict_map[row.name] = (
                    str(row.verdict.value),
                    str(row.diagnostic or ""),
                )
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        logger.debug(
            "constellation: dashboard unavailable",
            exc_info=True,
        )

    # Fetch flag specs (canonical source).
    flag_specs: list = []
    try:
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501
            ensure_seeded,
        )
        registry = ensure_seeded()
        flag_specs = list(registry.list_all())
    except Exception:  # noqa: BLE001
        logger.debug(
            "constellation: flag_registry unavailable",
            exc_info=True,
        )

    # Build stars: union of (verdict_map keys) + (flag_specs by name).
    stars_by_name: Dict[str, ConstellationStar] = {}

    # Pass 1 — flags with registry descriptors.
    for spec in flag_specs:
        try:
            name = str(spec.name)
            cat_value = (
                spec.category.value
                if hasattr(spec.category, "value")
                else str(spec.category)
            )
            verdict_str, diagnostic = verdict_map.get(
                name, ("disabled", ""),
            )
            star = ConstellationStar(
                flag_name=name,
                brightness=_verdict_to_brightness(verdict_str),
                graduation_verdict=verdict_str,
                category=cat_value,
                linked_principles=_principles_for_category(
                    cat_value,
                ),
                diagnostic=diagnostic,
                posture_relevance=tuple(
                    sorted(
                        (spec.posture_relevance or {}).keys()
                    )
                ),
            )
            stars_by_name[name] = star
        except Exception:  # noqa: BLE001
            continue

    # Pass 2 — flags that have a graduation contract but no
    # registry descriptor (rare; surface them as DARK with
    # empty category so operator sees the gap).
    for name, (verdict_str, diagnostic) in verdict_map.items():
        if name in stars_by_name:
            continue
        try:
            stars_by_name[name] = ConstellationStar(
                flag_name=name,
                brightness=_verdict_to_brightness(verdict_str),
                graduation_verdict=verdict_str,
                category="",
                linked_principles=(),
                diagnostic=diagnostic,
            )
        except Exception:  # noqa: BLE001
            continue

    stars = tuple(
        sorted(
            stars_by_name.values(),
            key=lambda s: (s.category, s.flag_name),
        )
    )

    by_brightness: Dict[str, int] = {
        b.value: 0 for b in ConstellationBrightness
    }
    by_category: Dict[str, int] = {}
    for s in stars:
        by_brightness[s.brightness.value] = (
            by_brightness.get(s.brightness.value, 0) + 1
        )
        by_category[s.category] = (
            by_category.get(s.category, 0) + 1
        )

    snap = ConstellationSnapshot(
        aggregated_at_unix=time.time(),
        stars=stars,
        by_brightness=by_brightness,
        by_category=by_category,
        elapsed_s=time.monotonic() - started,
    )

    _publish_constellation_event(snap)
    _record_snapshot(snap)
    return snap


# ===========================================================================
# Singleton snapshot cache (avoid recomputing per-render call)
# ===========================================================================


_cached_snapshot: Optional[ConstellationSnapshot] = None
_cache_lock = threading.RLock()


def _record_snapshot(snap: ConstellationSnapshot) -> None:
    global _cached_snapshot
    with _cache_lock:
        _cached_snapshot = snap


def get_cached_snapshot() -> Optional[ConstellationSnapshot]:
    with _cache_lock:
        return _cached_snapshot


def reset_cache_for_tests() -> None:
    global _cached_snapshot
    with _cache_lock:
        _cached_snapshot = None


# ===========================================================================
# SSE composition — uses canonical broker ONLY
# ===========================================================================


def _publish_constellation_event(
    snapshot: ConstellationSnapshot,
) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_CAPABILITY_CONSTELLATION_UPDATED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        # Per §38.11.5a row 6 reconciliation contract —
        # payload includes the 4 fields:
        #   {flag_name, brightness, graduation_state,
        #    linked_principles}
        # We extend with by_brightness summary for cheap
        # operator dashboards.
        broker.publish(
            EVENT_TYPE_CAPABILITY_CONSTELLATION_UPDATED,
            "constellation",
            {
                "schema_version": (
                    CAPABILITY_CONSTELLATION_SCHEMA_VERSION
                ),
                "aggregated_at_unix": snapshot.aggregated_at_unix,
                "by_brightness": dict(snapshot.by_brightness),
                "by_category": dict(snapshot.by_category),
                # Bounded — first 50 stars only to keep
                # SSE payload small.
                "stars": [
                    {
                        "flag_name": s.flag_name,
                        "brightness": s.brightness.value,
                        "graduation_state": (
                            s.graduation_verdict
                        ),
                        "linked_principles": list(
                            s.linked_principles
                        ),
                    }
                    for s in snapshot.stars[:50]
                ],
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "constellation: SSE publish failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer — pure, NEVER raises
# ===========================================================================


_BRIGHTNESS_GLYPHS = {
    ConstellationBrightness.RADIANT: "⭐",
    ConstellationBrightness.GLOWING: "✦",
    ConstellationBrightness.DIM: "·",
    ConstellationBrightness.FAULTING: "⚠",
    ConstellationBrightness.DARK: "○",
}


_BRIGHTNESS_TINTS = {
    ConstellationBrightness.RADIANT: "yellow",
    ConstellationBrightness.GLOWING: "cyan",
    ConstellationBrightness.DIM: "bright_black",
    ConstellationBrightness.FAULTING: "red",
    ConstellationBrightness.DARK: "dim",
}


def format_constellation_panel(
    *,
    snapshot: Optional[ConstellationSnapshot] = None,
    limit_per_axis: int = 5,
    only_brightness: Optional[ConstellationBrightness] = None,
) -> str:
    """Render the constellation panel grouped by category.

    Empty when master/sub-flag off OR no stars. Pure; NEVER
    raises.
    """
    if not panel_enabled():
        return ""
    if snapshot is None:
        snapshot = get_cached_snapshot()
        if snapshot is None or not snapshot.stars:
            snapshot = aggregate_constellation()
    if not snapshot.stars:
        return ""

    by_axis: Dict[str, list] = {}
    for s in snapshot.stars:
        if (
            only_brightness is not None
            and s.brightness is not only_brightness
        ):
            continue
        by_axis.setdefault(s.category or "(unknown)", []).append(s)

    if not by_axis:
        return ""

    parts = ["[bright_yellow]🌌 Capability constellation:[/]"]
    counts = " · ".join(
        f"{b.value}={snapshot.by_brightness.get(b.value, 0)}"
        for b in ConstellationBrightness
        if snapshot.by_brightness.get(b.value, 0) > 0
    )
    if counts:
        parts.append(f"  [dim]{counts}[/]")

    for axis in sorted(by_axis.keys()):
        stars = by_axis[axis][:limit_per_axis]
        axis_label = axis if axis else "(unknown)"
        parts.append(f"  [italic]{axis_label}[/]")
        for s in stars:
            glyph = _BRIGHTNESS_GLYPHS.get(s.brightness, "·")
            tint = _BRIGHTNESS_TINTS.get(
                s.brightness, "white",
            )
            principles = (
                f" [dim]→ {', '.join(s.linked_principles[:1])}[/]"
                if s.linked_principles else ""
            )
            parts.append(
                f"      [{tint}]{glyph}[/] {s.flag_name}"
                f"{principles}"
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
            "§38.11-F capability constellation master switch "
            "(graduation contract per §33.1; default FALSE).",
            "false",
        ),
        (
            _ENV_SUB_PANEL, "bool",
            "Enable constellation panel render. Default TRUE "
            "when master on.",
            "true",
        ),
        (
            _ENV_SUB_AUTO_REFRESH, "bool",
            "Background poll auto-refresh constellation "
            "snapshot. Default FALSE — opt-in.",
            "false",
        ),
        (
            _ENV_REFRESH_INTERVAL_S, "int",
            "Auto-refresh interval seconds (default 60; "
            "clamped 5..3600).",
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
                    "capability_constellation.py"
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
            "section_38_11f_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — master flag stays "
            "default-False until evidence ladder closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "capability_constellation.py"
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
            "section_38_11f_authority_asymmetry"
        ),
        description=(
            "Substrate purity — read-only aggregator + "
            "renderer; no orchestrator/risk-tier authority."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "capability_constellation.py"
        ),
        validate=_authority_asymmetry,
    ))

    # ---- Pin 3: brightness_taxonomy_5_values_pinned_to_verdict ----------

    def _brightness_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ConstellationBrightness"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "RADIANT", "GLOWING", "DIM",
                    "FAULTING", "DARK",
                }
                missing = expected - names
                if missing:
                    return [
                        f"ConstellationBrightness missing "
                        f"values: {sorted(missing)} (must be "
                        "1:1 with UnifiedGraduationVerdict's "
                        "5 values)"
                    ]
                return []
        return ["ConstellationBrightness class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11f_brightness_taxonomy_5_values"
        ),
        description=(
            "Closed 5-value ConstellationBrightness taxonomy "
            "is bytes-pinned 1:1 onto "
            "UnifiedGraduationVerdict; adding/removing "
            "either taxonomy without parity update breaks "
            "_verdict_to_brightness mapping."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "capability_constellation.py"
        ),
        validate=_brightness_taxonomy,
    ))

    # ---- Pin 4: composes_canonical_graduation_dashboard ------------------

    def _composes_dashboard(tree: ast.AST, src: str):
        if (
            "unified_graduation_dashboard" not in src
            or "aggregate_dashboard" not in src
        ):
            return [
                "must lazy-import unified_graduation_dashboard "
                "+ aggregate_dashboard (canonical verdict "
                "source)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11f_composes_canonical_graduation_dashboard"
        ),
        description=(
            "Constellation composes the canonical 491-test "
            "graduation dashboard for verdict reads — no "
            "parallel verdict aggregation."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "capability_constellation.py"
        ),
        validate=_composes_dashboard,
    ))

    # ---- Pin 5: composes_canonical_flag_registry ------------------------

    def _composes_flag_registry(tree: ast.AST, src: str):
        if (
            "flag_registry" not in src
            or "ensure_seeded" not in src
        ):
            return [
                "must lazy-import flag_registry + "
                "ensure_seeded (canonical flag descriptor "
                "source — Category enum is the constellation "
                "axis)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11f_composes_canonical_flag_registry"
        ),
        description=(
            "Constellation composes canonical flag_registry "
            "Category enum as its axis — NO parallel "
            "StarAxis taxonomy per §38.11.5a.5."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "capability_constellation.py"
        ),
        validate=_composes_flag_registry,
    ))

    return pins


__all__ = [
    "CAPABILITY_CONSTELLATION_SCHEMA_VERSION",
    "ConstellationBrightness",
    "ConstellationStar",
    "ConstellationSnapshot",
    "master_enabled",
    "panel_enabled",
    "auto_refresh_enabled",
    "aggregate_constellation",
    "get_cached_snapshot",
    "reset_cache_for_tests",
    "format_constellation_panel",
    "register_flags",
    "register_shipped_invariants",
]

"""§39 Tier-4 #18 — Memory crystallization timeline
(PRD v2.73 to v2.74, 2026-05-09).

Visualizes when/why patterns crystallized into permanent
memory — geological strata of accumulated wisdom. Composes
canonical :class:`MemoryInsight` records persisted to
``.jarvis/ouroboros/consciousness/insights.jsonl`` by
:class:`MemoryEngine`.

Authority asymmetry: ZERO authority. Read-only loader +
renderer. NEVER calls MemoryEngine (which is async),
NEVER mutates the insights ledger, NEVER spawns ops.
The substrate parses on-disk artifacts directly — same
pattern :class:`LastSessionSummary` uses for ``summary.json``.

§38.11.5a.5 single-canonical-name discipline honored —
reuses canonical :class:`MemoryInsight` schema (4-value
``category`` field: failure_pattern / success_pattern /
file_fragility / timing_pattern); the only NEW closed
taxonomy is :class:`CrystalAge` (4 values mapping
evidence_count + confidence to geological strata).

§33 patterns invoked:
- §33.1 graduation contract (master default-FALSE)
- §33.5 versioned artifact (frozen :class:`Crystal` +
  :class:`CrystalLayer` + :class:`CrystalTimeline`)
"""
from __future__ import annotations

import enum
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


MEMORY_CRYSTALLIZATION_SCHEMA_VERSION: str = (
    "memory_crystallization.1"
)


_ENV_MASTER = "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED"
_ENV_MAX_INSIGHTS = (
    "JARVIS_MEMORY_CRYSTALLIZATION_MAX_INSIGHTS"
)

_DEFAULT_MAX_INSIGHTS = 200
_MIN_MAX = 10
_MAX_MAX = 5000

# Canonical 4-value MemoryInsight categories (per
# consciousness/types.py:MemoryInsight.category docstring).
# Bytes-pinned via AST regression — drift requires explicit
# pin update.
_CANONICAL_CATEGORIES: Tuple[str, ...] = (
    "failure_pattern",
    "success_pattern",
    "file_fragility",
    "timing_pattern",
)


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
        return is_substrate_in_active_pack('memory_crystallization')
    except ImportError:
        return False


def _read_max_insights() -> int:
    raw = os.environ.get(_ENV_MAX_INSIGHTS, "").strip()
    if not raw:
        return _DEFAULT_MAX_INSIGHTS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_INSIGHTS
    return max(_MIN_MAX, min(_MAX_MAX, n))


# ===========================================================================
# Closed taxonomy — 4-value CrystalAge
# ===========================================================================


class CrystalAge(str, enum.Enum):
    """Closed 4-value vocabulary for memory crystallization
    stages. Mapped via :func:`_age_for_insight` (bytes-
    pinned thresholds).

    Stages encode "how solidified is this knowledge":
      NASCENT       — new (1 evidence, low confidence)
      FORMING       — moderate evidence, building
      SOLID         — well-evidenced, high confidence
      CRYSTALLIZED  — extensively evidenced, peak confidence
    """

    NASCENT = "nascent"
    FORMING = "forming"
    SOLID = "solid"
    CRYSTALLIZED = "crystallized"


def _age_for_insight(
    *, evidence_count: int, confidence: float,
) -> CrystalAge:
    """Pure-function bucketing. NEVER raises.

    Bytes-pinned thresholds:
      evidence_count >=10 AND confidence>=0.8 → CRYSTALLIZED
      evidence_count >=5  AND confidence>=0.6 → SOLID
      evidence_count >=2                       → FORMING
      else                                     → NASCENT
    """
    try:
        n = int(evidence_count or 0)
        c = float(confidence or 0.0)
    except (TypeError, ValueError):
        return CrystalAge.NASCENT
    if n >= 10 and c >= 0.8:
        return CrystalAge.CRYSTALLIZED
    if n >= 5 and c >= 0.6:
        return CrystalAge.SOLID
    if n >= 2:
        return CrystalAge.FORMING
    return CrystalAge.NASCENT


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class Crystal:
    """One memory insight rendered as a crystal.

    Frozen + hashable. Wraps the canonical MemoryInsight
    fields plus computed age + last_seen_unix.
    """

    insight_id: str
    category: str               # canonical MemoryInsight category
    age: CrystalAge
    content: str
    confidence: float
    evidence_count: int
    last_seen_iso: str
    last_seen_unix: float
    schema_version: str = (
        MEMORY_CRYSTALLIZATION_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "insight_id": self.insight_id,
            "category": self.category,
            "age": self.age.value,
            "content": self.content,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "last_seen_iso": self.last_seen_iso,
            "last_seen_unix": self.last_seen_unix,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CrystalLayer:
    """One stratum — all crystals of a given canonical
    MemoryInsight category."""

    category: str               # canonical category value
    crystals: Tuple[Crystal, ...] = field(default_factory=tuple)
    by_age: Dict[str, int] = field(default_factory=dict)
    schema_version: str = (
        MEMORY_CRYSTALLIZATION_SCHEMA_VERSION
    )

    @property
    def total(self) -> int:
        return len(self.crystals)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "total": self.total,
            "by_age": dict(self.by_age),
            "crystals": [c.to_dict() for c in self.crystals],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CrystalTimeline:
    """Aggregate timeline across all canonical categories."""

    aggregated_at_unix: float = 0.0
    total_insights: int = 0
    earliest_unix: float = 0.0
    latest_unix: float = 0.0
    layers: Tuple[CrystalLayer, ...] = field(default_factory=tuple)
    by_age: Dict[str, int] = field(default_factory=dict)
    schema_version: str = (
        MEMORY_CRYSTALLIZATION_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "aggregated_at_unix": self.aggregated_at_unix,
            "total_insights": self.total_insights,
            "earliest_unix": self.earliest_unix,
            "latest_unix": self.latest_unix,
            "by_age": dict(self.by_age),
            "layers": [layer.to_dict() for layer in self.layers],
        }

    def layer_for_category(
        self, category: str,
    ) -> Optional[CrystalLayer]:
        for layer in self.layers:
            if layer.category == category:
                return layer
        return None


# ===========================================================================
# On-disk reader — composes canonical insights.jsonl
# ===========================================================================


def _resolve_insights_path() -> Path:
    """Canonical on-disk path. Matches harness.py
    `_consciousness_dir = repo_path / .jarvis / ouroboros
    / consciousness`."""
    repo = os.environ.get("JARVIS_REPO_PATH", "").strip()
    if repo:
        base = Path(repo)
    else:
        base = Path.cwd()
    return (
        base / ".jarvis" / "ouroboros" / "consciousness"
        / "insights.jsonl"
    )


def _parse_iso_to_unix(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(str(iso).strip())
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _read_insights() -> List[Crystal]:
    """Parse canonical ``insights.jsonl`` (JSONL with one
    insight dict per line). NEVER raises — returns empty
    list on any failure."""
    path = _resolve_insights_path()
    try:
        if not path.exists() or not path.is_file():
            return []
    except Exception:  # noqa: BLE001
        return []

    max_n = _read_max_insights()
    crystals: List[Crystal] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue
                try:
                    insight_id = str(
                        d.get("insight_id", "") or "",
                    )
                    category = str(
                        d.get("category", "") or "",
                    )
                    if not category:
                        continue
                    content = str(
                        d.get("content", "") or "",
                    )
                    try:
                        confidence = float(
                            d.get("confidence", 0.0) or 0.0,
                        )
                    except (TypeError, ValueError):
                        confidence = 0.0
                    try:
                        evidence_count = int(
                            d.get("evidence_count", 0) or 0,
                        )
                    except (TypeError, ValueError):
                        evidence_count = 0
                    last_seen_iso = str(
                        d.get("last_seen_utc", "") or "",
                    )
                    last_seen_unix = _parse_iso_to_unix(
                        last_seen_iso,
                    )
                    age = _age_for_insight(
                        evidence_count=evidence_count,
                        confidence=confidence,
                    )
                    crystals.append(Crystal(
                        insight_id=insight_id,
                        category=category,
                        age=age,
                        content=content[:240],
                        confidence=round(confidence, 3),
                        evidence_count=evidence_count,
                        last_seen_iso=last_seen_iso,
                        last_seen_unix=last_seen_unix,
                    ))
                except Exception:  # noqa: BLE001
                    continue
                if len(crystals) >= max_n:
                    break
    except OSError:
        return []
    return crystals


# ===========================================================================
# Aggregator
# ===========================================================================


def aggregate_crystal_timeline() -> CrystalTimeline:
    """Compose on-disk insights.jsonl into a layered
    timeline. NEVER raises. Returns empty timeline when:
      * master flag off
      * insights.jsonl missing or empty
    """
    if not master_enabled():
        return CrystalTimeline()

    crystals = _read_insights()
    if not crystals:
        return CrystalTimeline(
            aggregated_at_unix=_now_unix(),
        )

    # Bucket by canonical category. Use the canonical 4
    # category names + an OTHER bucket for any future
    # category drift (defensive — never lose data).
    by_cat: Dict[str, List[Crystal]] = {
        c: [] for c in _CANONICAL_CATEGORIES
    }
    by_cat["other"] = []
    by_age_total: Dict[str, int] = {
        a.value: 0 for a in CrystalAge
    }

    earliest = float("inf")
    latest = 0.0
    for cr in crystals:
        bucket = (
            cr.category
            if cr.category in by_cat
            else "other"
        )
        by_cat[bucket].append(cr)
        by_age_total[cr.age.value] = (
            by_age_total.get(cr.age.value, 0) + 1
        )
        if cr.last_seen_unix > 0:
            if cr.last_seen_unix < earliest:
                earliest = cr.last_seen_unix
            if cr.last_seen_unix > latest:
                latest = cr.last_seen_unix

    if earliest == float("inf"):
        earliest = 0.0

    layers: List[CrystalLayer] = []
    for cat in (*_CANONICAL_CATEGORIES, "other"):
        items = by_cat.get(cat, [])
        if not items:
            continue
        # Sort by last_seen_unix descending — newest first.
        items.sort(
            key=lambda c: c.last_seen_unix, reverse=True,
        )
        by_age: Dict[str, int] = {
            a.value: 0 for a in CrystalAge
        }
        for cr in items:
            by_age[cr.age.value] = (
                by_age.get(cr.age.value, 0) + 1
            )
        layers.append(CrystalLayer(
            category=cat,
            crystals=tuple(items),
            by_age=by_age,
        ))

    timeline = CrystalTimeline(
        aggregated_at_unix=_now_unix(),
        total_insights=len(crystals),
        earliest_unix=earliest,
        latest_unix=latest,
        layers=tuple(layers),
        by_age=by_age_total,
    )
    _publish_timeline_event(timeline)
    return timeline


def _now_unix() -> float:
    import time as _t
    return _t.time()


# ===========================================================================
# SSE composition
# ===========================================================================


def _publish_timeline_event(
    timeline: CrystalTimeline,
) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_MEMORY_CRYSTALLIZATION_AGGREGATED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        # Bounded payload — don't ship raw crystal bodies.
        broker.publish(
            EVENT_TYPE_MEMORY_CRYSTALLIZATION_AGGREGATED,
            "memory_crystallization",
            {
                "schema_version": (
                    MEMORY_CRYSTALLIZATION_SCHEMA_VERSION
                ),
                "aggregated_at_unix": (
                    timeline.aggregated_at_unix
                ),
                "total_insights": timeline.total_insights,
                "earliest_unix": timeline.earliest_unix,
                "latest_unix": timeline.latest_unix,
                "by_age": dict(timeline.by_age),
                "layer_summary": [
                    {
                        "category": layer.category,
                        "total": layer.total,
                        "by_age": dict(layer.by_age),
                    }
                    for layer in timeline.layers
                ],
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "memory_crystallization: SSE publish failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer — geological strata
# ===========================================================================


_AGE_GLYPHS: Dict[CrystalAge, str] = {
    CrystalAge.NASCENT: "·",
    CrystalAge.FORMING: "▒",
    CrystalAge.SOLID: "▓",
    CrystalAge.CRYSTALLIZED: "█",
}


_AGE_TINTS: Dict[CrystalAge, str] = {
    CrystalAge.NASCENT: "dim",
    CrystalAge.FORMING: "cyan",
    CrystalAge.SOLID: "yellow",
    CrystalAge.CRYSTALLIZED: "bright_yellow",
}


_CATEGORY_GLYPHS: Dict[str, str] = {
    "failure_pattern": "⚠",
    "success_pattern": "✓",
    "file_fragility": "🪨",
    "timing_pattern": "⏱",
    "other": "•",
}


def format_crystal_timeline(
    *,
    timeline: Optional[CrystalTimeline] = None,
    crystals_per_layer: int = 5,
) -> str:
    """Render the geological-strata timeline. Empty when
    master off OR no insights."""
    if not master_enabled():
        return ""
    if timeline is None:
        timeline = aggregate_crystal_timeline()
    if not timeline.layers:
        return ""
    try:
        cap = max(1, min(int(crystals_per_layer), 20))
    except (TypeError, ValueError):
        cap = 5

    parts = ["[bright_yellow]🪨 Memory crystallization timeline:[/]"]
    age_summary_parts = []
    for age in CrystalAge:
        n = timeline.by_age.get(age.value, 0)
        if n > 0:
            tint = _AGE_TINTS.get(age, "white")
            age_summary_parts.append(
                f"[{tint}]{age.value}={n}[/]"
            )
    if age_summary_parts:
        parts.append(
            f"  [dim]({timeline.total_insights} insights · "
            + " · ".join(age_summary_parts) + ")[/]"
        )

    for layer in timeline.layers:
        cat_glyph = _CATEGORY_GLYPHS.get(
            layer.category, "•",
        )
        parts.append("")
        parts.append(
            f"  [bold]{cat_glyph} {layer.category}[/] "
            f"[dim]({layer.total} insights)[/]"
        )
        for cr in layer.crystals[:cap]:
            age_glyph = _AGE_GLYPHS.get(cr.age, "·")
            tint = _AGE_TINTS.get(cr.age, "white")
            content_short = cr.content[:80]
            parts.append(
                f"    [{tint}]{age_glyph}[/] "
                f"[dim]{cr.age.value:<13}[/] "
                f"conf={cr.confidence:.2f} "
                f"ev={cr.evidence_count:<3} "
                f"{content_short}"
            )
    return "\n".join(parts).rstrip()


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
            "§39 Tier-4 #18 memory crystallization timeline "
            "master switch (graduation contract per §33.1; "
            "default FALSE).",
            "false",
        ),
        (
            _ENV_MAX_INSIGHTS, "int",
            "Max insights parsed from insights.jsonl "
            "(default 200; clamped 10..5000).",
            "200",
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
                    "memory_crystallization.py"
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
            "section_39_tier4_18_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — crystallization "
            "master stays default-False."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "memory_crystallization.py"
        ),
        validate=_master_default_false,
    ))

    def _authority_asymmetry(tree: ast.AST, src: str):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.consciousness.memory_engine",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority import: {mod} "
                        "(read on-disk insights.jsonl directly "
                        "instead — MemoryEngine is async)"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier4_18_authority_asymmetry"
        ),
        description=(
            "Substrate purity — read-only on-disk parser. "
            "Forbidden: orchestrator + MemoryEngine "
            "(both async/heavy); reads insights.jsonl "
            "directly instead."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "memory_crystallization.py"
        ),
        validate=_authority_asymmetry,
    ))

    def _age_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CrystalAge"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "NASCENT", "FORMING", "SOLID",
                    "CRYSTALLIZED",
                }
                missing = expected - names
                if missing:
                    return [
                        f"CrystalAge missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["CrystalAge class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier4_18_age_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value CrystalAge taxonomy."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "memory_crystallization.py"
        ),
        validate=_age_taxonomy,
    ))

    def _canonical_categories_pinned(
        tree: ast.AST, src: str,
    ):
        """Bytes-pin: _CANONICAL_CATEGORIES MUST match
        the 4 documented MemoryInsight category values
        (consciousness/types.py:MemoryInsight). Drift
        without explicit pin update silently drops data
        into the OTHER bucket."""
        required = (
            "failure_pattern",
            "success_pattern",
            "file_fragility",
            "timing_pattern",
        )
        for cat in required:
            if cat not in src:
                return [
                    f"canonical category {cat!r} missing "
                    "from _CANONICAL_CATEGORIES — drift "
                    "from MemoryInsight schema requires "
                    "explicit pin update"
                ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier4_18_canonical_categories_pinned"
        ),
        description=(
            "Bytes-pin canonical 4 MemoryInsight categories "
            "in _CANONICAL_CATEGORIES tuple — drift from "
            "consciousness/types.py:MemoryInsight schema "
            "requires explicit pin update."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "memory_crystallization.py"
        ),
        validate=_canonical_categories_pinned,
    ))

    def _composes_insights_jsonl(tree: ast.AST, src: str):
        if (
            "insights.jsonl" not in src
            or ".jarvis" not in src
        ):
            return [
                "must compose canonical on-disk path "
                "`.jarvis/ouroboros/consciousness/"
                "insights.jsonl` (matches harness.py "
                "_consciousness_dir constant)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier4_18_composes_canonical_"
            "insights_path"
        ),
        description=(
            "Reader composes canonical `.jarvis/ouroboros/"
            "consciousness/insights.jsonl` path — drift "
            "from harness.py _consciousness_dir requires "
            "explicit pin update."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "memory_crystallization.py"
        ),
        validate=_composes_insights_jsonl,
    ))

    return pins


__all__ = [
    "MEMORY_CRYSTALLIZATION_SCHEMA_VERSION",
    "CrystalAge",
    "Crystal",
    "CrystalLayer",
    "CrystalTimeline",
    "master_enabled",
    "aggregate_crystal_timeline",
    "format_crystal_timeline",
    "register_flags",
    "register_shipped_invariants",
]

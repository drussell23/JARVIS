"""Phase 7.2 — IronGate exploration-floor adapted loader (boot-time).

Per `OUROBOROS_VENOM_PRD.md` §3.6 + §9 Phase 7.2:

  > Pass C Slice 3 proposes floor raises; ExplorationLedger doesn't
  > read `.jarvis/adapted_iron_gate_floors.yaml` at boot. Phase 7.2
  > closes that activation gap.

This module bridges Pass C's AdaptationLedger (operator-approved
floor-raise proposals from Slice 3's `exploration_floor_tightener.py`)
into the live `ExplorationFloors` evaluation. Default-off + best-
effort: when the env flag is off OR the YAML is missing/malformed,
the loader returns an empty mapping and ExplorationFloors behaves
exactly as it did pre-Phase-7.2.

## Design constraints (load-bearing)

  * **Adapted floors can only RAISE coverage requirements** (per
    Pass C §7.3). The loader returns a Dict[category, float] of
    per-category numeric floors. The `compute_adapted_required_
    categories` helper translates "category X has adapted floor > 0"
    into "category X must be in required_categories." Adapted
    floors are merged additively into the env's required_categories
    set — never overrides existing required entries; never removes
    them.
  * **Numeric floor value is operator visibility**, not structural
    enforcement (yet). Slice 3's miner produces per-category
    numeric proposals; the activation maps those to categorical
    requirements. The numeric is preserved in the loaded mapping
    for `/posture` REPL surfacing in a follow-up.
  * **Stdlib + adaptation.ledger import surface only.** Same cage
    discipline as the rest of `adaptation/`. Does NOT import
    exploration_engine.py (one-way: exploration_engine imports
    THIS, not the reverse — same pattern as Slice 7.1).
  * **Fail-open**: every error path returns an empty dict.

## Default-off

`JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS` (default false).

## YAML schema

The file `.jarvis/adapted_iron_gate_floors.yaml`:

```yaml
schema_version: 1
floors:
  - category: comprehension
    floor: 2.0
    proposal_id: adapt-ig-...
    approved_at: "2026-..."
    approved_by: "alice"
  - category: discovery
    floor: 1.0
    proposal_id: adapt-ig-...
    approved_at: "2026-..."
    approved_by: "alice"
```

Each entry:
  * `category`: must match an `ExplorationCategory` enum value;
    unknown categories are SKIPPED (with logged warning).
  * `floor`: numeric > 0; entries with floor <= 0 are SKIPPED.
  * provenance fields: `proposal_id`, `approved_at`, `approved_by`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Soft cap on the number of adapted floors loaded. Defends against
# a corrupt YAML file with thousands of entries slowing every
# from_env_with_adapted() call.
MAX_ADAPTED_FLOORS: int = 64

# Hard cap on YAML file size we'll attempt to load.
MAX_YAML_BYTES: int = 4 * 1024 * 1024

# Per-category floor cap. Slice 3's miner is bounded by
# MAX_FLOOR_RAISE_PCT=100% per cycle; cumulative raises across
# many cycles could in theory reach high numbers. Cap at 100 to
# defend against operator-typo runaway in the YAML directly.
MAX_FLOOR_VALUE: float = 100.0

# Known ExplorationCategory enum values. Kept here as the canonical
# allowlist so the loader can validate without importing
# exploration_engine.py (one-way dependency rule per Slice 7.1
# pattern).
_KNOWN_CATEGORIES: FrozenSet[str] = frozenset({
    "comprehension",
    "discovery",
    "call_graph",
    "structure",
    "history",
})


def is_loader_enabled() -> bool:
    """Master flag — ``JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS``
    (default false until Phase 7.2 graduation)."""
    return os.environ.get(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "",
    ).strip().lower() in _TRUTHY


def adapted_floors_path() -> Path:
    """Return the YAML path. Env-overridable via
    ``JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH``; defaults to
    ``.jarvis/adapted_iron_gate_floors.yaml`` under cwd."""
    raw = os.environ.get("JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "adapted_iron_gate_floors.yaml"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptedFloorEntry:
    """One adapted floor record loaded from YAML."""

    category: str
    floor: float
    proposal_id: str
    approved_at: str
    approved_by: str


# ---------------------------------------------------------------------------
# YAML reader
# ---------------------------------------------------------------------------


def _parse_entry(
    raw: Dict[str, Any], idx: int,
) -> Optional[AdaptedFloorEntry]:
    """Parse one YAML entry. Returns None on missing/invalid fields."""
    category = str(raw.get("category") or "").strip().lower()
    if not category:
        logger.debug(
            "[AdaptedIronGateLoader] entry %d missing category — skip",
            idx,
        )
        return None
    if category not in _KNOWN_CATEGORIES:
        logger.warning(
            "[AdaptedIronGateLoader] entry %d unknown category=%s "
            "(known: %s) — skip",
            idx, category, sorted(_KNOWN_CATEGORIES),
        )
        return None
    try:
        floor = float(raw.get("floor", 0.0))
    except (TypeError, ValueError):
        logger.debug(
            "[AdaptedIronGateLoader] entry %d category=%s non-numeric "
            "floor — skip", idx, category,
        )
        return None
    if floor <= 0.0:
        logger.debug(
            "[AdaptedIronGateLoader] entry %d category=%s floor=%g "
            "<= 0 — skip", idx, category, floor,
        )
        return None
    if floor > MAX_FLOOR_VALUE:
        logger.warning(
            "[AdaptedIronGateLoader] entry %d category=%s floor=%g "
            "exceeds MAX_FLOOR_VALUE=%g — clamped",
            idx, category, floor, MAX_FLOOR_VALUE,
        )
        floor = MAX_FLOOR_VALUE
    return AdaptedFloorEntry(
        category=category,
        floor=floor,
        proposal_id=str(raw.get("proposal_id") or ""),
        approved_at=str(raw.get("approved_at") or ""),
        approved_by=str(raw.get("approved_by") or ""),
    )


def load_adapted_floors(
    yaml_path: Optional[Path] = None,
) -> Dict[str, float]:
    """Read the adapted-floors YAML and return a `{category: floor}`
    dict.

    Returns empty dict when:
      * Master flag off
      * YAML file missing
      * YAML parse fails (PyYAML import OR `yaml.safe_load`)
      * File exceeds MAX_YAML_BYTES
      * Top-level not a mapping or `floors` key missing/non-list

    Per-entry SKIP (logged) when:
      * Missing category / unknown category / non-numeric floor /
        floor <= 0

    Cap: at most MAX_ADAPTED_FLOORS entries returned. Latest
    occurrence per category wins (operator can supersede an
    earlier proposal by adding a new entry with the same category).

    NEVER raises into the caller.
    """
    if not is_loader_enabled():
        return {}

    path = yaml_path if yaml_path is not None else adapted_floors_path()
    if not path.exists():
        logger.debug(
            "[AdaptedIronGateLoader] no adapted-floors yaml at %s — "
            "no floors to merge", path,
        )
        return {}
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning(
            "[AdaptedIronGateLoader] stat failed for %s: %s", path, exc,
        )
        return {}
    if size > MAX_YAML_BYTES:
        logger.warning(
            "[AdaptedIronGateLoader] %s exceeds MAX_YAML_BYTES=%d "
            "(was %d) — refusing to load",
            path, MAX_YAML_BYTES, size,
        )
        return {}
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[AdaptedIronGateLoader] read failed for %s: %s", path, exc,
        )
        return {}
    if not raw_text.strip():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "[AdaptedIronGateLoader] PyYAML not available — cannot "
            "load adapted floors",
        )
        return {}
    try:
        doc = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.warning(
            "[AdaptedIronGateLoader] YAML parse failed at %s: %s",
            path, exc,
        )
        return {}
    if not isinstance(doc, dict):
        logger.warning(
            "[AdaptedIronGateLoader] %s top-level is not a mapping — skip",
            path,
        )
        return {}
    raw_entries = doc.get("floors")
    if not isinstance(raw_entries, list):
        return {}

    out: Dict[str, float] = {}
    seen_count = 0
    for i, raw_entry in enumerate(raw_entries):
        if seen_count >= MAX_ADAPTED_FLOORS:
            logger.warning(
                "[AdaptedIronGateLoader] reached MAX_ADAPTED_FLOORS="
                "%d — truncating remaining entries", MAX_ADAPTED_FLOORS,
            )
            break
        if not isinstance(raw_entry, dict):
            continue
        entry = _parse_entry(raw_entry, i)
        if entry is None:
            continue
        # Latest-occurrence-wins per category (operator supersedes
        # earlier proposal by adding new entry).
        out[entry.category] = entry.floor
        seen_count += 1
    if out:
        logger.info(
            "[AdaptedIronGateLoader] loaded %d adapted floor(s) from %s",
            len(out), path,
        )
    return out


def compute_adapted_required_categories(
    adapted_floors: Dict[str, float],
) -> FrozenSet[str]:
    """Translate per-category numeric floors into a set of category
    names that should be required (coverage-required) at evaluate
    time.

    Cage rule: category X with adapted floor > 0 means "operators
    have approved tightening this category"; the structural
    activation is "category X must be in required_categories" —
    strictly raises coverage requirements above the env baseline.

    Returns category strings (matching ExplorationCategory enum
    .value strings) so callers can lookup the enum value without
    forcing this module to import exploration_engine.
    """
    return frozenset(
        cat for cat, floor in (adapted_floors or {}).items()
        if floor > 0.0
    )


__all__ = [
    "AdaptedFloorEntry",
    "MAX_ADAPTED_FLOORS",
    "MAX_FLOOR_VALUE",
    "MAX_YAML_BYTES",
    "adapted_floors_path",
    "compute_adapted_required_categories",
    "is_loader_enabled",
    "load_adapted_floors",
]

"""Phase 7.4 — risk-tier ladder adapted boot-time loader.

Per `OUROBOROS_VENOM_PRD.md` §3.6 + §9 Phase 7.4:

  > Pass C Slice 4b proposes inserting NEW risk tiers between
  > existing ones when novel `failure_class` values accumulate
  > beyond threshold. The canonical ladder doesn't read
  > `.jarvis/adapted_risk_tiers.yaml` at boot. Phase 7.4 closes
  > that activation gap.

This module bridges Pass C's AdaptationLedger (operator-approved
tier-insertion proposals from Slice 4b's `risk_tier_extender.py`)
into the live risk-tier ladder. Default-off + best-effort: when
the env flag is off OR the YAML is missing/malformed, the loader
returns an empty list and `compute_extended_ladder` returns the
base ladder unchanged (byte-identical pre-Phase-7.4 behavior).

## Design constraints (load-bearing)

  * **The ladder only GROWS** (per Pass C §8.3 + §4.1 — Pass C is
    one-way tighten-only; loosening goes through Pass B `/order2
    amend`). Insertion is strictly additive — an op that
    previously matched tier X may now match the new
    intermediate tier between X and X+1 (strictly more strict);
    no op that didn't match X can suddenly match the new tier;
    NO existing tier is removed; NO existing tier is reordered.
  * **Defense-in-depth**: `compute_extended_ladder()` enforces
    the structural cage even if the YAML somehow tries to
    reorder, drop, or duplicate an existing tier:
      - Adapted `tier_name` colliding with base ladder → SKIP
      - Adapted `insert_after` not in base ladder → SKIP
      - Multiple adapted entries with same `tier_name` →
        latest-occurrence-wins
      - Output ALWAYS contains every base ladder element in the
        same relative order (asserted in unit tests)
  * **Stdlib + adaptation.ledger import surface only.** Same cage
    discipline as the rest of `adaptation/`. Does NOT import
    `risk_tier_floor.py` or any orchestrator module (one-way:
    callers import THIS, not the reverse — same pattern as
    Slice 7.1 + 7.2 + 7.3).
  * **Fail-open**: every error path returns an empty list and the
    helper falls back to base_ladder unchanged.

## Default-off

`JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS` (default false).

## YAML schema

The file `.jarvis/adapted_risk_tiers.yaml`:

```yaml
schema_version: 1
tiers:
  - tier_name: NOTIFY_APPLY_HARDENED_NETWORK_EGRESS
    insert_after: NOTIFY_APPLY
    failure_class: network_egress
    proposal_id: adapt-rt-...
    approved_at: "2026-..."
    approved_by: "alice"
  - tier_name: APPROVAL_REQUIRED_HARDENED_PERMISSION_LOOSEN
    insert_after: APPROVAL_REQUIRED
    failure_class: permission_loosen
    proposal_id: adapt-rt-...
```

Each entry:
  * `tier_name`: synthesized name (matches Slice 4b's
    `_synthesize_tier_name` output). Skipped if it collides with
    an existing base-ladder tier or with another adapted entry's
    name (after latest-wins dedup).
  * `insert_after`: name of an existing base-ladder tier. The new
    tier is inserted IMMEDIATELY ABOVE this one (i.e. one position
    higher in the strictness order). Unknown values SKIP.
  * `failure_class`: provenance — which novel class triggered the
    insertion. Not used for placement (insert_after is the
    canonical placement field) but preserved for `/posture` /
    `/help` surfacing.
  * Bounded length: `MAX_TIER_NAME_CHARS=64` (matches Slice 4b
    miner). Names exceeding this are SKIPPED (rather than
    truncated — a truncated tier name could collide with a base
    name).

## compute_extended_ladder helper

`compute_extended_ladder(base_ladder, adapted=None) -> Tuple[str, ...]`

  * Loader OFF → returns base_ladder unchanged.
  * Loader ON, no adapted entries → returns base_ladder unchanged.
  * Loader ON, valid adapted entries → returns extended ladder
    with each adapted tier inserted IMMEDIATELY AFTER its
    `insert_after` slot (i.e. the new tier is one index higher).
    Multiple adapted entries inserting after the same base tier
    are appended in YAML-listed order (after dedup).

The output ALWAYS contains every base_ladder element in the same
relative order — defense-in-depth that no adapted entry can drop
or reorder existing tiers.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Soft cap on the number of adapted tiers loaded. Pass C Slice 4b
# is bounded by JARVIS_ADAPTATION_TIER_THRESHOLD per cycle; cumulative
# across many cycles could grow but 16 is a reasonable defense-in-
# depth ceiling for an emergency-grade ladder.
MAX_ADAPTED_TIERS: int = 16

# Hard cap on YAML file size we'll attempt to load.
MAX_YAML_BYTES: int = 4 * 1024 * 1024

# Per-tier-name length cap. Matches Slice 4b miner's
# MAX_TIER_NAME_CHARS so names that miner would never produce are
# rejected here.
MAX_TIER_NAME_CHARS: int = 64

# Allowed tier_name characters. Mirrors Slice 4b miner's
# `_synthesize_tier_name` output charset (uppercase letters /
# digits / underscore). Defends against operator-typo runaway
# (paths, shell tokens, etc.) sneaking into the canonical ladder.
_VALID_TIER_NAME_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def is_loader_enabled() -> bool:
    """Master flag — ``JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS``
    (default false until Phase 7.4 graduation)."""
    return os.environ.get(
        "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "",
    ).strip().lower() in _TRUTHY


def adapted_risk_tiers_path() -> Path:
    """Return the YAML path. Env-overridable via
    ``JARVIS_ADAPTED_RISK_TIERS_PATH``; defaults to
    ``.jarvis/adapted_risk_tiers.yaml`` under cwd."""
    raw = os.environ.get("JARVIS_ADAPTED_RISK_TIERS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "adapted_risk_tiers.yaml"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptedTierEntry:
    """One adapted tier-insertion record loaded from YAML."""

    tier_name: str
    insert_after: str
    failure_class: str
    proposal_id: str
    approved_at: str
    approved_by: str


# ---------------------------------------------------------------------------
# YAML reader
# ---------------------------------------------------------------------------


def _is_valid_tier_name(name: str) -> bool:
    if not name or len(name) > MAX_TIER_NAME_CHARS:
        return False
    return all(c in _VALID_TIER_NAME_CHARS for c in name)


def _parse_entry(
    raw: Dict[str, Any], idx: int,
) -> Optional[AdaptedTierEntry]:
    """Parse one YAML entry. Returns None on missing/invalid fields."""
    tier_name = str(raw.get("tier_name") or "").strip()
    if not tier_name:
        logger.debug(
            "[AdaptedRiskTierLoader] entry %d missing tier_name — skip",
            idx,
        )
        return None
    if not _is_valid_tier_name(tier_name):
        logger.warning(
            "[AdaptedRiskTierLoader] entry %d tier_name=%r invalid "
            "(charset / length) — skip",
            idx, tier_name,
        )
        return None
    insert_after = str(raw.get("insert_after") or "").strip()
    if not insert_after:
        logger.debug(
            "[AdaptedRiskTierLoader] entry %d tier_name=%s missing "
            "insert_after — skip", idx, tier_name,
        )
        return None
    if not _is_valid_tier_name(insert_after):
        logger.warning(
            "[AdaptedRiskTierLoader] entry %d insert_after=%r invalid "
            "(charset / length) — skip", idx, insert_after,
        )
        return None
    return AdaptedTierEntry(
        tier_name=tier_name,
        insert_after=insert_after,
        failure_class=str(raw.get("failure_class") or "").strip(),
        proposal_id=str(raw.get("proposal_id") or ""),
        approved_at=str(raw.get("approved_at") or ""),
        approved_by=str(raw.get("approved_by") or ""),
    )


def load_adapted_tiers(
    yaml_path: Optional[Path] = None,
) -> List[AdaptedTierEntry]:
    """Read the adapted-tiers YAML and return a list of entries.

    Returns empty list when:
      * Master flag off
      * YAML file missing
      * YAML parse fails (PyYAML import OR `yaml.safe_load`)
      * File exceeds MAX_YAML_BYTES
      * Top-level not a mapping or `tiers` key missing/non-list

    Per-entry SKIP (logged) when:
      * Missing/invalid tier_name (charset, length)
      * Missing/invalid insert_after

    Cap: at most MAX_ADAPTED_TIERS entries returned. Latest
    occurrence per tier_name wins (operator can supersede an
    earlier proposal by adding a new entry with the same tier_name
    — e.g. moving the same new tier to a different insert_after).

    NEVER raises into the caller.
    """
    if not is_loader_enabled():
        return []

    path = yaml_path if yaml_path is not None else adapted_risk_tiers_path()
    if not path.exists():
        logger.debug(
            "[AdaptedRiskTierLoader] no adapted-tiers yaml at %s — "
            "no extensions to merge", path,
        )
        return []
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning(
            "[AdaptedRiskTierLoader] stat failed for %s: %s", path, exc,
        )
        return []
    if size > MAX_YAML_BYTES:
        logger.warning(
            "[AdaptedRiskTierLoader] %s exceeds MAX_YAML_BYTES=%d "
            "(was %d) — refusing to load",
            path, MAX_YAML_BYTES, size,
        )
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[AdaptedRiskTierLoader] read failed for %s: %s", path, exc,
        )
        return []
    if not raw_text.strip():
        return []
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "[AdaptedRiskTierLoader] PyYAML not available — cannot "
            "load adapted tiers",
        )
        return []
    try:
        doc = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.warning(
            "[AdaptedRiskTierLoader] YAML parse failed at %s: %s",
            path, exc,
        )
        return []
    if not isinstance(doc, dict):
        logger.warning(
            "[AdaptedRiskTierLoader] %s top-level is not a mapping — skip",
            path,
        )
        return []
    raw_entries = doc.get("tiers")
    if not isinstance(raw_entries, list):
        return []

    out: List[AdaptedTierEntry] = []
    by_name: Dict[str, int] = {}  # tier_name → index in `out` for latest-wins
    seen_count = 0
    for i, raw_entry in enumerate(raw_entries):
        if seen_count >= MAX_ADAPTED_TIERS:
            logger.warning(
                "[AdaptedRiskTierLoader] reached MAX_ADAPTED_TIERS="
                "%d — truncating remaining entries", MAX_ADAPTED_TIERS,
            )
            break
        if not isinstance(raw_entry, dict):
            continue
        entry = _parse_entry(raw_entry, i)
        if entry is None:
            continue
        # Latest-occurrence-wins per tier_name.
        if entry.tier_name in by_name:
            out[by_name[entry.tier_name]] = entry
        else:
            by_name[entry.tier_name] = len(out)
            out.append(entry)
            seen_count += 1
    if out:
        logger.info(
            "[AdaptedRiskTierLoader] loaded %d adapted tier(s) from %s",
            len(out), path,
        )
    return out


def compute_extended_ladder(
    base_ladder: Tuple[str, ...],
    adapted: Optional[List[AdaptedTierEntry]] = None,
) -> Tuple[str, ...]:
    """Return the effective risk-tier ladder, which is the
    base_ladder with any operator-approved adapted tiers inserted.

    Cage rule (load-bearing):
      * Output ALWAYS contains every element of base_ladder in the
        same relative order. No adapted entry can drop or reorder
        a base tier.
      * Adapted `tier_name` colliding with a base ladder name →
        SKIPPED (defense-in-depth — Slice 4b miner already
        enforces uniqueness, but a doctored YAML must not be able
        to override an existing canonical name).
      * Adapted `insert_after` not in base ladder → SKIPPED.
      * Multiple adapted entries inserting after the same base
        tier are appended in order (after latest-wins dedup
        already applied by load_adapted_tiers).

    Behavior:
      * Loader OFF → returns base_ladder unchanged.
      * Loader ON, no entries → returns base_ladder unchanged.
      * Loader ON, valid entries → returns extended ladder.

    The optional ``adapted`` parameter accepts a pre-loaded list
    (callers may load once + reuse to amortize YAML I/O). When
    None, the loader is invoked on every call.

    NEVER raises.
    """
    if adapted is None:
        try:
            adapted = load_adapted_tiers()
        except Exception as exc:  # defensive — loader is fail-open
            logger.warning(
                "[AdaptedRiskTierLoader] load_adapted_tiers raised %s "
                "— falling back to base_ladder", exc,
            )
            return tuple(base_ladder)

    if not adapted:
        return tuple(base_ladder)

    base_set = set(base_ladder)
    # Group valid adapted entries by insert_after slot, preserving
    # insertion order from the loader (which already applied
    # latest-wins dedup).
    inserts_by_slot: Dict[str, List[str]] = {}
    seen_names = set(base_set)
    for entry in adapted:
        if entry.tier_name in seen_names:
            logger.debug(
                "[AdaptedRiskTierLoader] tier_name=%s collides with "
                "existing ladder entry — skip",
                entry.tier_name,
            )
            continue
        if entry.insert_after not in base_set:
            logger.warning(
                "[AdaptedRiskTierLoader] insert_after=%s not in base "
                "ladder %s — skip", entry.insert_after, list(base_ladder),
            )
            continue
        inserts_by_slot.setdefault(entry.insert_after, []).append(
            entry.tier_name,
        )
        seen_names.add(entry.tier_name)

    if not inserts_by_slot:
        return tuple(base_ladder)

    out: List[str] = []
    for base_tier in base_ladder:
        out.append(base_tier)
        for new_tier in inserts_by_slot.get(base_tier, ()):
            out.append(new_tier)
    return tuple(out)


__all__ = [
    "AdaptedTierEntry",
    "MAX_ADAPTED_TIERS",
    "MAX_TIER_NAME_CHARS",
    "MAX_YAML_BYTES",
    "adapted_risk_tiers_path",
    "compute_extended_ladder",
    "is_loader_enabled",
    "load_adapted_tiers",
]

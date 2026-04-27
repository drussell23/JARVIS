"""Phase 7.5 — ExplorationLedger category-weight adapted boot-time loader.

Per `OUROBOROS_VENOM_PRD.md` §3.6 + §9 Phase 7.5:

  > Pass C Slice 5 proposes per-category weight rebalances
  > (raise high-value categories, lower low-value categories —
  > but with mass-conservation enforcing **net tightening**).
  > ExplorationLedger doesn't read
  > `.jarvis/adapted_category_weights.yaml` at boot. Phase 7.5
  > closes that activation gap.

This module bridges Pass C's AdaptationLedger (operator-approved
weight-rebalance proposals from Slice 5's
`category_weight_rebalancer.py`) into the live category-weight
computation. Default-off + best-effort: when the env flag is off
OR the YAML is missing/malformed, the loader returns an empty
list and `compute_effective_category_weights` returns
``base_weights`` unchanged (byte-identical pre-Phase-7.5
behavior).

## Design constraints (load-bearing)

  * **Net cage strictness only RISES** (per Pass C §4.1 — Pass C
    is one-way tighten-only; loosening goes through Pass B
    `/order2 amend`). Slice 5 is the only Pass C surface where
    individual values *appear* to fall — net mass-conservation
    keeps the whole vector tightening. Defense-in-depth at three
    independent layers:
      - **Sum invariant**: `Σ(new) ≥ Σ(base)` enforced by
        `compute_effective_category_weights()`. A doctored YAML
        with a lower sum is REJECTED and the helper returns
        ``base_weights`` unchanged.
      - **Per-category floor**: each new weight must be
        `≥ HALF_OF_BASE × base[k]` (matches Slice 5 miner's
        `JARVIS_ADAPTATION_WEIGHT_FLOOR_PCT=50%` rule). A
        doctored YAML driving any category below half of its
        base value is REJECTED.
      - **Absolute floor**: each new weight must be
        `≥ MIN_WEIGHT_VALUE=0.01`. Matches Slice 5 miner —
        defends against operator-typo collapsing a category
        to effectively zero.
  * **Schema invariant**: the output ALWAYS contains every
    base_weights key (preserving structure). Unknown categories
    in adapted YAML are dropped from output — Pass C cannot
    add new categories via this surface (that's a Pass B
    Order-2 amendment).
  * **Stdlib + adaptation.ledger import surface only.** Same cage
    discipline as the rest of `adaptation/`. Does NOT import
    `exploration_engine.py` (one-way: callers import THIS, not
    the reverse — same pattern as Slice 7.1+7.2+7.3+7.4).
  * **Fail-open**: every error path (master flag off, YAML
    missing/malformed, REJECTED candidate) returns base_weights
    unchanged.

## Default-off

`JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS`
(default false).

## YAML schema

The file `.jarvis/adapted_category_weights.yaml`:

```yaml
schema_version: 1
rebalances:
  - high_value_category: comprehension
    low_value_category: discovery
    new_weights:
      comprehension: 1.20
      discovery: 0.90
      call_graph: 1.00
      structure: 1.00
      history: 1.00
    proposal_id: adapt-cw-...
    approved_at: "2026-..."
    approved_by: "alice"
```

Each entry:
  * `new_weights`: full vector mapping category name → numeric
    weight. Slice 5 produces one such vector per cycle (the
    strongest correlation gap — see `category_weight_rebalancer
    .py` `MinedWeightRebalance.new_weights`).
  * `high_value_category` / `low_value_category`: provenance
    pointers (which categories drove the proposal). Not used by
    the helper for placement; preserved for `/posture` /
    `/help` surfacing.
  * Latest-occurrence-wins (only the **last** valid entry is
    used — operator can supersede an earlier rebalance by adding
    a new entry).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Soft cap on the number of adapted rebalances loaded. Slice 5
# produces ONE per cycle; cumulative across many cycles could
# grow but only the latest is active. 8 is a defense-in-depth
# ceiling for a corrupt YAML file.
MAX_ADAPTED_REBALANCES: int = 8

# Hard cap on YAML file size we'll attempt to load.
MAX_YAML_BYTES: int = 4 * 1024 * 1024

# Per-category weight cap. Slice 5 miner is bounded by
# DEFAULT_RAISE_PCT=20% per cycle; cumulative raises across many
# cycles could in theory reach high numbers. Cap at 100 to defend
# against operator-typo runaway in the YAML directly.
MAX_WEIGHT_VALUE: float = 100.0

# Per-category absolute floor — matches Slice 5 miner's
# MIN_WEIGHT_VALUE constant. Pass C never proposes a weight that
# would effectively zero out a category (would let it drop out of
# the cage entirely).
MIN_WEIGHT_VALUE: float = 0.01

# Half-of-base floor — matches Slice 5 miner's
# JARVIS_ADAPTATION_WEIGHT_FLOOR_PCT=50% default. Per-category new
# weight must be at least half of its base value. Defense-in-
# depth: Slice 5 miner already enforces this; the loader re-checks
# in case of a doctored YAML.
HALF_OF_BASE: float = 0.5


def is_loader_enabled() -> bool:
    """Master flag —
    ``JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS``
    (default false until Phase 7.5 graduation)."""
    return os.environ.get(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS", "",
    ).strip().lower() in _TRUTHY


def adapted_category_weights_path() -> Path:
    """Return the YAML path. Env-overridable via
    ``JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH``; defaults to
    ``.jarvis/adapted_category_weights.yaml`` under cwd."""
    raw = os.environ.get("JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "adapted_category_weights.yaml"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptedRebalanceEntry:
    """One adapted category-weight rebalance record loaded from YAML."""

    new_weights: Dict[str, float]
    high_value_category: str
    low_value_category: str
    proposal_id: str
    approved_at: str
    approved_by: str

    def __post_init__(self):
        # Defensive: freeze the dict shape by replacing with a
        # sorted-key copy so equality / hash / ordering are
        # deterministic. (Frozen dataclass forbids self.attr = ...,
        # so we use object.__setattr__ in __post_init__.)
        object.__setattr__(
            self, "new_weights",
            {k: float(self.new_weights[k]) for k in sorted(self.new_weights)},
        )


# ---------------------------------------------------------------------------
# YAML reader
# ---------------------------------------------------------------------------


def _parse_entry(
    raw: Dict[str, Any], idx: int,
) -> Optional[AdaptedRebalanceEntry]:
    """Parse one YAML entry. Returns None on missing/invalid fields."""
    raw_weights = raw.get("new_weights")
    if not isinstance(raw_weights, dict):
        logger.debug(
            "[AdaptedCategoryWeightLoader] entry %d missing or non-mapping "
            "new_weights — skip", idx,
        )
        return None
    parsed: Dict[str, float] = {}
    for k, v in raw_weights.items():
        if not isinstance(k, str) or not k.strip():
            logger.debug(
                "[AdaptedCategoryWeightLoader] entry %d non-string-key "
                "in new_weights — skip", idx,
            )
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            logger.debug(
                "[AdaptedCategoryWeightLoader] entry %d category=%s "
                "non-numeric weight — skip", idx, k,
            )
            return None
        if f <= 0.0:
            logger.debug(
                "[AdaptedCategoryWeightLoader] entry %d category=%s "
                "weight=%g <= 0 — skip", idx, k, f,
            )
            return None
        if f > MAX_WEIGHT_VALUE:
            logger.warning(
                "[AdaptedCategoryWeightLoader] entry %d category=%s "
                "weight=%g exceeds MAX_WEIGHT_VALUE=%g — clamped",
                idx, k, f, MAX_WEIGHT_VALUE,
            )
            f = MAX_WEIGHT_VALUE
        parsed[k.strip().lower()] = f
    if not parsed:
        logger.debug(
            "[AdaptedCategoryWeightLoader] entry %d empty new_weights "
            "— skip", idx,
        )
        return None
    return AdaptedRebalanceEntry(
        new_weights=parsed,
        high_value_category=str(raw.get("high_value_category") or "").strip().lower(),
        low_value_category=str(raw.get("low_value_category") or "").strip().lower(),
        proposal_id=str(raw.get("proposal_id") or ""),
        approved_at=str(raw.get("approved_at") or ""),
        approved_by=str(raw.get("approved_by") or ""),
    )


def load_adapted_rebalances(
    yaml_path: Optional[Path] = None,
) -> List[AdaptedRebalanceEntry]:
    """Read the adapted-category-weights YAML and return a list of
    rebalance entries.

    Returns empty list when:
      * Master flag off
      * YAML file missing
      * YAML parse fails (PyYAML import OR `yaml.safe_load`)
      * File exceeds MAX_YAML_BYTES
      * Top-level not a mapping or `rebalances` key missing/non-list

    Per-entry SKIP (logged) when:
      * Missing or non-mapping `new_weights` / non-string keys /
        non-numeric / weight <= 0 / empty new_weights

    Cap: at most MAX_ADAPTED_REBALANCES entries returned.

    NEVER raises into the caller.
    """
    if not is_loader_enabled():
        return []

    path = (
        yaml_path if yaml_path is not None
        else adapted_category_weights_path()
    )
    if not path.exists():
        logger.debug(
            "[AdaptedCategoryWeightLoader] no adapted-weights yaml at %s "
            "— no rebalances to merge", path,
        )
        return []
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning(
            "[AdaptedCategoryWeightLoader] stat failed for %s: %s",
            path, exc,
        )
        return []
    if size > MAX_YAML_BYTES:
        logger.warning(
            "[AdaptedCategoryWeightLoader] %s exceeds MAX_YAML_BYTES=%d "
            "(was %d) — refusing to load",
            path, MAX_YAML_BYTES, size,
        )
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[AdaptedCategoryWeightLoader] read failed for %s: %s",
            path, exc,
        )
        return []
    if not raw_text.strip():
        return []
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "[AdaptedCategoryWeightLoader] PyYAML not available — cannot "
            "load adapted weights",
        )
        return []
    try:
        doc = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.warning(
            "[AdaptedCategoryWeightLoader] YAML parse failed at %s: %s",
            path, exc,
        )
        return []
    if not isinstance(doc, dict):
        logger.warning(
            "[AdaptedCategoryWeightLoader] %s top-level is not a mapping "
            "— skip", path,
        )
        return []
    raw_entries = doc.get("rebalances")
    if not isinstance(raw_entries, list):
        return []

    out: List[AdaptedRebalanceEntry] = []
    seen_count = 0
    for i, raw_entry in enumerate(raw_entries):
        if seen_count >= MAX_ADAPTED_REBALANCES:
            logger.warning(
                "[AdaptedCategoryWeightLoader] reached "
                "MAX_ADAPTED_REBALANCES=%d — truncating remaining entries",
                MAX_ADAPTED_REBALANCES,
            )
            break
        if not isinstance(raw_entry, dict):
            continue
        entry = _parse_entry(raw_entry, i)
        if entry is None:
            continue
        out.append(entry)
        seen_count += 1
    if out:
        logger.info(
            "[AdaptedCategoryWeightLoader] loaded %d adapted rebalance(s) "
            "from %s", len(out), path,
        )
    return out


def _net_tighten_check(
    base_weights: Dict[str, float],
    candidate: Dict[str, float],
) -> Tuple[bool, str]:
    """Apply the three defense-in-depth net-tighten checks.

    Returns (passed, reason). On `passed=False`, `reason` is the
    structured reject token suitable for logging.
    """
    base_sum = sum(base_weights.values())
    cand_sum = sum(candidate.values())
    # Allow a tiny FP epsilon to avoid false-rejecting a true-equal sum.
    if cand_sum < base_sum - 1e-9:
        return False, (
            f"sum_invariant:base={base_sum:.6f}<cand={cand_sum:.6f}"
        )
    for cat, base_w in base_weights.items():
        new_w = candidate.get(cat, base_w)
        if new_w < HALF_OF_BASE * base_w - 1e-9:
            return False, (
                f"per_category_floor:cat={cat}:base={base_w:.6f}:"
                f"new={new_w:.6f}"
            )
        if new_w < MIN_WEIGHT_VALUE - 1e-9:
            return False, (
                f"absolute_floor:cat={cat}:new={new_w:.6f}<"
                f"MIN={MIN_WEIGHT_VALUE}"
            )
    return True, ""


def compute_effective_category_weights(
    base_weights: Dict[str, float],
    adapted: Optional[List[AdaptedRebalanceEntry]] = None,
) -> Dict[str, float]:
    """Return the effective per-category weight dict, which is
    ``base_weights`` with the latest valid operator-approved
    rebalance applied — IF it passes all three defense-in-depth
    net-tighten checks.

    Cage rule (load-bearing):
      * Output ALWAYS contains every key from base_weights
        (preserving schema). Unknown adapted keys are silently
        dropped (Pass C cannot add new categories via this
        surface).
      * Sum invariant: `Σ(new) ≥ Σ(base)` — REJECT and return
        base_weights if violated.
      * Per-category floor: each new weight ≥ 0.5 × base[k] —
        REJECT if violated.
      * Absolute floor: each new weight ≥ MIN_WEIGHT_VALUE —
        REJECT if violated.

    Behavior:
      * Loader OFF → returns ``dict(base_weights)`` unchanged.
      * Loader ON, no entries → returns ``dict(base_weights)``.
      * Loader ON, latest valid entry passes all checks →
        returns merged weights.
      * Loader ON, latest valid entry FAILS any check → REJECTED;
        returns ``dict(base_weights)`` unchanged. Earlier entries
        are NOT consulted (latest-wins; operator's most recent
        proposal is the active one).

    The optional ``adapted`` parameter accepts a pre-loaded list
    (callers may load once + reuse to amortize YAML I/O). When
    None, the loader is invoked on every call.

    NEVER raises.
    """
    if adapted is None:
        try:
            adapted = load_adapted_rebalances()
        except Exception as exc:  # defensive — loader is fail-open
            logger.warning(
                "[AdaptedCategoryWeightLoader] load_adapted_rebalances "
                "raised %s — falling back to base_weights", exc,
            )
            return dict(base_weights)

    if not adapted:
        return dict(base_weights)

    # Latest-occurrence-wins per Slice 5's "ONE per cycle" design.
    latest = adapted[-1]
    candidate = dict(base_weights)
    for cat, w in latest.new_weights.items():
        if cat in base_weights:
            candidate[cat] = float(w)
        # else: unknown category — silently dropped.
    passed, reason = _net_tighten_check(base_weights, candidate)
    if not passed:
        logger.warning(
            "[AdaptedCategoryWeightLoader] adapted rebalance proposal_id=%s "
            "REJECTED at boot: %s — falling back to base_weights",
            latest.proposal_id, reason,
        )
        return dict(base_weights)
    return candidate


__all__ = [
    "AdaptedRebalanceEntry",
    "HALF_OF_BASE",
    "MAX_ADAPTED_REBALANCES",
    "MAX_WEIGHT_VALUE",
    "MAX_YAML_BYTES",
    "MIN_WEIGHT_VALUE",
    "adapted_category_weights_path",
    "compute_effective_category_weights",
    "is_loader_enabled",
    "load_adapted_rebalances",
]

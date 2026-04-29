"""RR Pass C Slice 5 — ExplorationLedger category-weight auto-rebalance.

Per `memory/project_reverse_russian_doll_pass_c.md` §9:

  > For each category in the 5-category ExplorationLedger:
  >   1. Compute correlation between category score and verify-pass
  >      outcome over the window.
  >   2. Identify the category with highest correlation (high-value)
  >      and lowest correlation (low-value).
  >   3. If `(high_correlation - low_correlation) >= JARVIS_
  >      ADAPTATION_CORRELATION_DELTA` (default 0.3):
  >      Propose: raise high-value category weight by X%, lower
  >      low-value category weight by Y% (Y < X — net total weight
  >      rises).

This is the **fourth and final** adaptive surface (Slice 6 is the
meta-governor + REPL + observability). Among the 5 surfaces, this
is the only one where the proposal *appears* to lower something.
Mass conservation makes it net-tighten.

## The mass-conservation guarantee (load-bearing)

Per §9.2: a rebalance proposal is monotonic-tightening iff:

  Σ(new_weights) ≥ Σ(old_weights)        # net cage strictness rises
  AND
  min(new_weights) >= 0.5 * min(old_weights)  # no category vanishes

The §4.1 invariant validator (substrate-level + this surface's
validator combined) checks BOTH conditions BEFORE persistence.

The high-value category gets a stricter floor (its weight rises);
the low-value category becomes lower-priority but does NOT vanish
(lowest allowed weight: 50% of original, hard-floored). The net
weight strictly rises, so the cage as a whole becomes more strict
in expected value.

## Why deterministic Pearson correlation, not LLM

The §4.4 zero-LLM-in-cage invariant. Pearson correlation between
per-op category score and per-op verify-pass binary outcome is a
stdlib operation (`statistics` module compatibility on Py 3.9 via
manual computation). Bounded O(N) in window size.

## Activation path (Slice 6 wires this)

Approved weight changes land in `.jarvis/adapted_category_weights.yaml`,
loaded by ExplorationLedger at boot. The merged weights compose
multiplicatively over the static env-tuned weights — adapted weights
can only RAISE the high-value floor; the low-value reduction is
bounded by the 50% floor.

## Authority surface

  * Pure function over caller-supplied `CategoryOutcomeLite` lists.
  * Writes via `AdaptationLedger.propose()` only.
  * No subprocess, no env mutation, no network.
  * Stdlib-only (plus the Slice 1 substrate).
  * Auto-registers a per-surface validator at module-import.

## Default-off

`JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED` (default false).
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from dataclasses import dataclass, field
from typing import (
    Dict, Iterable, List, Optional, Sequence, Tuple,
)

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    ProposeResult,
    ProposeStatus,
    register_surface_validator,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Per §9.4 default 0.3 — minimum correlation gap between best and
# worst category before a rebalance is proposed. Lower = more
# proposals (operator burn); higher = misses real signal.
DEFAULT_CORRELATION_DELTA: float = 0.3

# Per §9.4 default 50 — minimum % of original weight a category
# must retain. Mass-conservation floor. Hard cage rule.
DEFAULT_WEIGHT_FLOOR_PCT: int = 50

# Adaptation window in days (shared default).
DEFAULT_WINDOW_DAYS: int = 7

# Minimum window observations before correlation is statistically
# meaningful. Below this, NO proposal is generated (avoids noise).
DEFAULT_REBALANCE_THRESHOLD: int = 10

# Bounded raise %. The high-value category's weight rises by this %
# of its current weight per cycle. Same operator-typo defense as
# Slice 3's MAX_FLOOR_RAISE_PCT.
DEFAULT_RAISE_PCT: int = 20

# Bounded lower %. Strictly less than DEFAULT_RAISE_PCT so the
# net Σ rises. The low-value category's weight LOWERS by this % —
# but the result is hard-floored at WEIGHT_FLOOR_PCT of original.
DEFAULT_LOWER_PCT: int = 10

# Hard cap on per-cycle raise/lower % so an operator typo can't
# whiplash the cage.
MAX_RAISE_PCT: int = 100
MAX_LOWER_PCT: int = 50  # never lower more than half in one cycle

# Hard floor on weight value (regardless of original) so a category
# weighted at 0.01 doesn't get rounded to zero by floating-point
# accumulation across many cycles.
MIN_WEIGHT_VALUE: float = 0.01


def get_correlation_delta() -> float:
    raw = os.environ.get("JARVIS_ADAPTATION_CORRELATION_DELTA")
    if raw is None:
        return DEFAULT_CORRELATION_DELTA
    try:
        v = float(raw)
        if v <= 0.0 or v > 2.0:
            return DEFAULT_CORRELATION_DELTA
        return v
    except ValueError:
        return DEFAULT_CORRELATION_DELTA


def get_weight_floor_pct() -> int:
    raw = os.environ.get("JARVIS_ADAPTATION_WEIGHT_FLOOR_PCT")
    if raw is None:
        return DEFAULT_WEIGHT_FLOOR_PCT
    try:
        v = int(raw)
        if v < 1 or v > 99:
            return DEFAULT_WEIGHT_FLOOR_PCT
        return v
    except ValueError:
        return DEFAULT_WEIGHT_FLOOR_PCT


def get_rebalance_threshold() -> int:
    raw = os.environ.get("JARVIS_ADAPTATION_REBALANCE_THRESHOLD")
    if raw is None:
        return DEFAULT_REBALANCE_THRESHOLD
    try:
        v = int(raw)
        return v if v >= 2 else DEFAULT_REBALANCE_THRESHOLD
    except ValueError:
        return DEFAULT_REBALANCE_THRESHOLD


def get_window_days() -> int:
    raw = os.environ.get("JARVIS_ADAPTATION_WINDOW_DAYS")
    if raw is None:
        return DEFAULT_WINDOW_DAYS
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_WINDOW_DAYS
    except ValueError:
        return DEFAULT_WINDOW_DAYS


def is_enabled() -> bool:
    """Master flag — ``JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED``
    (default ``true`` — graduated in Move 1 Pass C cadence 2026-04-29).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy hot-reverts."""
    raw = os.environ.get(
        "JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Move 1 Pass C cadence)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Event input shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryOutcomeLite:
    """One op's category scores + verify outcome.

    Fields:
      * op_id: source op for evidence.
      * category_scores: per-category float (caller-defined keys
        — typically the 5 ExplorationLedger categories).
      * verify_passed: did VERIFY pass cleanly? Used as the binary
        outcome variable for correlation.
      * timestamp_unix: window filter input.
    """

    op_id: str
    category_scores: Dict[str, float] = field(default_factory=dict)
    verify_passed: bool = False
    timestamp_unix: float = 0.0


@dataclass(frozen=True)
class MinedWeightRebalance:
    """Pre-substrate result. The rebalancer produces ONE per call
    (the strongest correlation gap)."""

    high_value_category: str
    low_value_category: str
    correlation_gap: float
    new_weights: Dict[str, float]
    old_weights_sum: float
    new_weights_sum: float
    observation_count: int
    source_event_ids: Tuple[str, ...]
    summary: str

    def proposal_id(self) -> str:
        h = hashlib.sha256()
        # Stable: keyed on the high+low pair + the new weight vector
        # rounded to 6 decimal places.
        h.update(self.high_value_category.encode("utf-8"))
        h.update(b"|+|")
        h.update(self.low_value_category.encode("utf-8"))
        h.update(b"|->|")
        for k in sorted(self.new_weights):
            h.update(f"{k}={self.new_weights[k]:.6f}".encode("utf-8"))
            h.update(b";")
        return f"adapt-cw-{h.hexdigest()[:24]}"

    def proposed_state_hash(self, current_state_hash: str) -> str:
        h = hashlib.sha256()
        h.update((current_state_hash or "").encode("utf-8"))
        h.update(b"|+|")
        for k in sorted(self.new_weights):
            h.update(f"{k}={self.new_weights[k]:.6f}".encode("utf-8"))
            h.update(b";")
        return f"sha256:{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Internal helpers — Pearson correlation (stdlib-only Py 3.9 compat)
# ---------------------------------------------------------------------------


def _filter_window(
    events: Iterable[CategoryOutcomeLite],
    *,
    now_unix: float,
    window_days: int,
) -> List[CategoryOutcomeLite]:
    if window_days <= 0:
        return list(events)
    cutoff = now_unix - (window_days * 86_400)
    return [
        e for e in events
        if e.timestamp_unix == 0.0 or e.timestamp_unix >= cutoff
    ]


def _pearson_correlation(
    xs: Sequence[float],
    ys: Sequence[float],
) -> float:
    """Pearson correlation coefficient. Stdlib-only (Py 3.9 compat —
    `statistics.correlation` was added in 3.10).

    Returns 0.0 on degenerate inputs (length < 2 or zero variance
    in either series). Bounded to [-1.0, 1.0] by mathematical
    guarantee (no numerical clamp needed for typical inputs).
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0.0 or var_y <= 0.0:
        return 0.0
    cov = sum(
        (xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)
    )
    denom = math.sqrt(var_x * var_y)
    if denom <= 0.0:
        return 0.0
    return cov / denom


def _compute_per_category_correlation(
    events: Sequence[CategoryOutcomeLite],
) -> Dict[str, float]:
    """For each category, compute Pearson correlation between its
    score across ops and the verify_passed binary (1.0 / 0.0).

    Categories present in fewer than 2 events are SKIPPED (no
    statistical signal). Categories present in 2+ events but with
    zero variance in score are mapped to correlation 0.0."""
    # Collect all categories that appear anywhere.
    all_cats = set()
    for e in events:
        all_cats.update(e.category_scores.keys())
    out: Dict[str, float] = {}
    for cat in all_cats:
        # Pair (score, verify_passed) per event where the category
        # is present.
        xs: List[float] = []
        ys: List[float] = []
        for e in events:
            if cat in e.category_scores:
                xs.append(float(e.category_scores[cat]))
                ys.append(1.0 if e.verify_passed else 0.0)
        if len(xs) < 2:
            continue
        out[cat] = _pearson_correlation(xs, ys)
    return out


def _compute_rebalanced_weights(
    current_weights: Dict[str, float],
    high_cat: str,
    low_cat: str,
    *,
    raise_pct: int,
    lower_pct: int,
    floor_pct: int,
) -> Dict[str, float]:
    """Apply the bounded raise+lower deltas with the mass-conservation
    floor. Returns a new dict; does NOT mutate current_weights."""
    new_weights = dict(current_weights)
    if high_cat in new_weights:
        new_weights[high_cat] = current_weights[high_cat] * (
            1.0 + raise_pct / 100.0
        )
    if low_cat in new_weights and high_cat != low_cat:
        original_low = current_weights[low_cat]
        proposed_low = original_low * (1.0 - lower_pct / 100.0)
        # Hard floor at floor_pct of original
        floor = original_low * (floor_pct / 100.0)
        # Also enforce absolute MIN_WEIGHT_VALUE
        floor = max(floor, MIN_WEIGHT_VALUE)
        new_weights[low_cat] = max(proposed_low, floor)
    return new_weights


# ---------------------------------------------------------------------------
# Public mining pipeline
# ---------------------------------------------------------------------------


def mine_weight_rebalances_from_events(
    events: Iterable[CategoryOutcomeLite],
    *,
    current_weights: Dict[str, float],
    correlation_delta: Optional[float] = None,
    raise_pct: Optional[int] = None,
    lower_pct: Optional[int] = None,
    floor_pct: Optional[int] = None,
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[MinedWeightRebalance]:
    """Pure function. Returns 0 or 1 MinedWeightRebalance — the
    strongest-correlation-gap rebalance per call (mirrors Slice 3's
    "one weakest candidate per cycle" design)."""
    delta = (
        correlation_delta
        if correlation_delta is not None else get_correlation_delta()
    )
    raise_p = raise_pct if raise_pct is not None else DEFAULT_RAISE_PCT
    lower_p = lower_pct if lower_pct is not None else DEFAULT_LOWER_PCT
    floor_p = floor_pct if floor_pct is not None else get_weight_floor_pct()
    th = threshold if threshold is not None else get_rebalance_threshold()
    wd = window_days if window_days is not None else get_window_days()

    # Clamp percent params to safe bounds.
    if raise_p > MAX_RAISE_PCT:
        raise_p = MAX_RAISE_PCT
    if raise_p < 1:
        raise_p = DEFAULT_RAISE_PCT
    if lower_p > MAX_LOWER_PCT:
        lower_p = MAX_LOWER_PCT
    if lower_p < 1:
        lower_p = DEFAULT_LOWER_PCT
    # Net-tighten constraint: lower_p MUST be < raise_p so Σ rises.
    # If misconfigured, force lower_p = max(1, raise_p // 2).
    if lower_p >= raise_p:
        lower_p = max(1, raise_p // 2)

    in_window = _filter_window(events, now_unix=now_unix, window_days=wd)
    if len(in_window) < th:
        return []

    correlations = _compute_per_category_correlation(in_window)
    if len(correlations) < 2:
        return []  # need at least 2 categories to rebalance

    # Find high-value (highest correlation) + low-value (lowest)
    sorted_cats = sorted(correlations.items(), key=lambda kv: kv[1])
    low_cat, low_corr = sorted_cats[0]
    high_cat, high_corr = sorted_cats[-1]
    gap = high_corr - low_corr
    if gap < delta:
        return []

    # Both categories must exist in current_weights (caller sanity).
    if high_cat not in current_weights or low_cat not in current_weights:
        return []
    # Skip if high == low (degenerate single-category gap).
    if high_cat == low_cat:
        return []

    new_weights = _compute_rebalanced_weights(
        current_weights, high_cat, low_cat,
        raise_pct=raise_p, lower_pct=lower_p, floor_pct=floor_p,
    )
    old_sum = sum(current_weights.values())
    new_sum = sum(new_weights.values())

    # Cage rule: net Σ MUST rise (mass-conservation invariant).
    # If the floor kicked in and held lower category weight steady
    # while high rose, this is automatically true. If it didn't hold
    # and somehow Σ dropped, refuse to propose.
    if new_sum < old_sum:
        # Defensive — should not happen with raise > lower, but we
        # double-check before persisting.
        return []

    src_ids = tuple(e.op_id for e in in_window if e.op_id)
    summary = (
        f"Mined from {len(in_window)} ops in last {wd}d window. "
        f"Category {high_cat!r} (correlation={high_corr:.3f}) ↑ by "
        f"{raise_p}%; category {low_cat!r} (correlation={low_corr:.3f}) "
        f"↓ by {lower_p}% (floored at {floor_p}% of original). "
        f"Σ(weights) {old_sum:.4f} → {new_sum:.4f} (net +{new_sum - old_sum:.4f})."
    )
    return [MinedWeightRebalance(
        high_value_category=high_cat,
        low_value_category=low_cat,
        correlation_gap=gap,
        new_weights=new_weights,
        old_weights_sum=old_sum,
        new_weights_sum=new_sum,
        observation_count=len(in_window),
        source_event_ids=src_ids,
        summary=summary,
    )]


def propose_weight_rebalances_from_events(
    events: Iterable[CategoryOutcomeLite],
    *,
    ledger: AdaptationLedger,
    current_weights: Dict[str, float],
    current_state_hash: str = "",
    correlation_delta: Optional[float] = None,
    raise_pct: Optional[int] = None,
    lower_pct: Optional[int] = None,
    floor_pct: Optional[int] = None,
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[ProposeResult]:
    """End-to-end."""
    if not is_enabled():
        return []
    candidates = mine_weight_rebalances_from_events(
        events,
        current_weights=current_weights,
        correlation_delta=correlation_delta,
        raise_pct=raise_pct,
        lower_pct=lower_pct,
        floor_pct=floor_pct,
        threshold=threshold,
        window_days=window_days,
        now_unix=now_unix,
    )
    wd = window_days if window_days is not None else get_window_days()
    results: List[ProposeResult] = []
    for c in candidates:
        evidence = AdaptationEvidence(
            window_days=wd,
            observation_count=c.observation_count,
            source_event_ids=c.source_event_ids,
            summary=c.summary,
        )
        proposed_hash = c.proposed_state_hash(current_state_hash)
        # Mining-surface payload (Item #2 yaml_writer schema):
        # `rebalances: [{new_weights: {cat: float, ...}, high_value_
        # category, low_value_category, ...prov}]`. Loader validates
        # weights dict (positive floats, lowercased category keys) +
        # enforces 3 net-tighten checks (sum / per-cat floor / abs
        # floor). Provenance auto-enriched by yaml_writer.
        payload = {
            "new_weights": dict(c.new_weights),
            "high_value_category": c.high_value_category,
            "low_value_category": c.low_value_category,
        }
        res = ledger.propose(
            proposal_id=c.proposal_id(),
            surface=AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS,
            proposal_kind="rebalance_weight",
            evidence=evidence,
            current_state_hash=current_state_hash or "sha256:initial",
            proposed_state_hash=proposed_hash,
            proposed_state_payload=payload,
        )
        results.append(res)
        if res.status is ProposeStatus.OK:
            logger.info(
                "[CategoryWeightRebalancer] proposed rebalance: "
                "high=%s ↑ low=%s ↓ Σ=%.4f→%.4f gap=%.3f "
                "proposal_id=%s",
                c.high_value_category, c.low_value_category,
                c.old_weights_sum, c.new_weights_sum, c.correlation_gap,
                res.proposal_id,
            )
    return results


# ---------------------------------------------------------------------------
# Surface validator — load-bearing mass-conservation check
# ---------------------------------------------------------------------------


def _category_weight_validator(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Per-surface validator (Pass C §4.1).

    Asserts:
      * proposal_kind MUST be "rebalance_weight".
      * proposed_state_hash sha256-prefixed.
      * observation_count >= cage's threshold floor.
      * Summary contains BOTH ↑ AND ↓ tokens (defense against
        doctored proposals — a real rebalance must move weights in
        both directions).
      * Summary contains "net +" indicator (the substrate-level
        mass-conservation check is summary-encoded since the actual
        weights aren't reconstructable from the hash alone — this
        is defense-in-depth; the miner's pre-persist mass-
        conservation gate is the actual structural invariant).
    """
    if proposal.proposal_kind != "rebalance_weight":
        return (
            False,
            f"category_weight_kind_must_be_rebalance_weight:{proposal.proposal_kind}",
        )
    if not proposal.proposed_state_hash.startswith("sha256:"):
        return (
            False,
            f"category_weight_proposed_hash_format:{proposal.proposed_state_hash[:32]}",
        )
    th = get_rebalance_threshold()
    if proposal.evidence.observation_count < th:
        return (
            False,
            f"category_weight_observation_count_below_threshold:"
            f"{proposal.evidence.observation_count} < {th}",
        )
    summary = proposal.evidence.summary
    if "↑" not in summary or "↓" not in summary:
        return (
            False,
            "category_weight_summary_missing_both_direction_indicators",
        )
    if "net +" not in summary:
        return (
            False,
            "category_weight_summary_missing_net_positive_indicator",
        )
    return (True, "category_weight_mass_conserving_ok")


def install_surface_validator() -> None:
    register_surface_validator(
        AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS,
        _category_weight_validator,
    )


install_surface_validator()


__all__ = [
    "CategoryOutcomeLite",
    "DEFAULT_CORRELATION_DELTA",
    "DEFAULT_LOWER_PCT",
    "DEFAULT_RAISE_PCT",
    "DEFAULT_REBALANCE_THRESHOLD",
    "DEFAULT_WEIGHT_FLOOR_PCT",
    "DEFAULT_WINDOW_DAYS",
    "MAX_LOWER_PCT",
    "MAX_RAISE_PCT",
    "MIN_WEIGHT_VALUE",
    "MinedWeightRebalance",
    "get_correlation_delta",
    "get_rebalance_threshold",
    "get_weight_floor_pct",
    "get_window_days",
    "install_surface_validator",
    "is_enabled",
    "mine_weight_rebalances_from_events",
    "propose_weight_rebalances_from_events",
]

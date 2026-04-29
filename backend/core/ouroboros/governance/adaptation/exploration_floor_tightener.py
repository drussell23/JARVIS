"""RR Pass C Slice 3 — IronGate exploration-floor auto-tightener.

Per `memory/project_reverse_russian_doll_pass_c.md` §7:

  > Analyzes ExplorationLedger entries vs. VERIFY outcomes. For each
  > op in the adaptation window:
  >   1. Read per-category exploration scores at GENERATE time.
  >   2. Read VERIFY outcome (pass / regression / L2-recovered / failed).
  >   3. Group ops where `floor_satisfied=True AND verify_outcome IN
  >      {regression, failed}` — the op passed the exploration gate
  >      but the patch was wrong.
  >   4. Identify the *weakest* category (lowest score among the 5)
  >      — the candidate for floor tightening.
  >   5. If `weakest_category_count >= JARVIS_ADAPTATION_FLOOR_
  >      THRESHOLD` (default 5) within the window, compute a proposed
  >      new floor: `current_floor + ceil(current_floor * 0.10)` and
  >      emit AdaptationProposal.

This module is the second adaptive surface (Slice 2 was SemanticGuardian
patterns). Same composition pattern: pure stdlib analyzer over caller-
supplied event lists + auto-registered surface validator that enforces
the cage rule (per Pass C §4.1: floors only RAISE, never lower).

## Why bounded 10% raise per cycle

Per §7.2: adaptation must not whiplash. A 10% per-cycle raise gives
the operator visibility-and-veto for each tightening step rather
than a single large jump. Across 7-day windows the cage tightens at
most ~10% per category per week — enough to track shell expansion,
not enough to catastrophically over-correct.

## Activation path (Slice 6 wires this)

Approved floor changes land in `.jarvis/adapted_iron_gate_floors.yaml`.
At GENERATE time, the ExplorationLedger reads `max(env_floor,
adapted_floor)` — the static env-tuned floor stays as a hard lower
bound; adapted floors can only RAISE above it. (This module ships
the proposer; ExplorationLedger wiring is a follow-up under
operator-approval, mirroring Slice 2's SemanticGuardian-loader split.)

## Authority surface

  * Pure function over caller-supplied `ExplorationOutcomeLite` lists.
  * Writes via `AdaptationLedger.propose()` only.
  * No subprocess, no env mutation, no network.
  * Stdlib-only (plus the Slice 1 substrate).
  * Auto-registers a per-surface validator at module-import that
    enforces:
      - kind == "raise_floor"
      - proposed_state_hash sha256-prefixed
      - observation_count >= JARVIS_ADAPTATION_FLOOR_THRESHOLD
      - proposed floor in the evidence summary > current floor
        (parsed from summary; substrate-level cage check)

## Default-off

`JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED` (default false).
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from dataclasses import dataclass, field
from typing import (
    Dict, Iterable, List, Optional, Sequence, Set, Tuple,
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
# Constants (env-overridable)
# ---------------------------------------------------------------------------


# Per §7.4 default 5 — slightly higher bar than Slice 2's pattern
# threshold (3) because a floor-raise has broader impact (it gates
# every future op on that category, not just ones matching one
# detector pattern).
DEFAULT_FLOOR_THRESHOLD: int = 5

# Per §7.2 default 10% — the bounded per-cycle raise.
DEFAULT_FLOOR_RAISE_PCT: int = 10

# Adaptation window in days (shared default with Slice 2).
DEFAULT_WINDOW_DAYS: int = 7

# Minimum nominal raise. If current_floor=1 and pct=10, ceil gives
# 1; we still bump by at least this so the math doesn't stall on
# small floors.
MIN_NOMINAL_RAISE: int = 1

# Hard cap on per-cycle raise to prevent operator-typo runaway
# (e.g., env override to 500%).
MAX_FLOOR_RAISE_PCT: int = 100

# Verify outcomes that count as "exploration gate bypass" — the op
# satisfied the floor but the patch turned out wrong.
BYPASS_FAILURE_OUTCOMES: Set[str] = {"regression", "failed"}


def get_floor_threshold() -> int:
    raw = os.environ.get("JARVIS_ADAPTATION_FLOOR_THRESHOLD")
    if raw is None:
        return DEFAULT_FLOOR_THRESHOLD
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_FLOOR_THRESHOLD
    except ValueError:
        return DEFAULT_FLOOR_THRESHOLD


def get_floor_raise_pct() -> int:
    raw = os.environ.get("JARVIS_ADAPTATION_FLOOR_RAISE_PCT")
    if raw is None:
        return DEFAULT_FLOOR_RAISE_PCT
    try:
        v = int(raw)
        if v < 1:
            return DEFAULT_FLOOR_RAISE_PCT
        if v > MAX_FLOOR_RAISE_PCT:
            return MAX_FLOOR_RAISE_PCT
        return v
    except ValueError:
        return DEFAULT_FLOOR_RAISE_PCT


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
    """Master flag — ``JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED``
    (default ``true`` — graduated in Move 1 Pass C cadence 2026-04-29).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy hot-reverts."""
    raw = os.environ.get(
        "JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Move 1 Pass C cadence)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Event input shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplorationOutcomeLite:
    """Minimal shape the tightener consumes — one op's exploration-
    vs-verify outcome.

    Fields:
      * op_id: source op for evidence.source_event_ids.
      * category_scores: per-category float scores observed at
        GENERATE time. Categories are caller-defined strings — the
        tightener does not hardcode the 5 ExplorationLedger categories
        (they are an Order-1 detail and could change without breaking
        this module).
      * floor_satisfied: did the op pass the exploration gate?
      * verify_outcome: terminal outcome — "pass" / "regression" /
        "failed" / "l2_recovered" etc. Only ops with
        ``floor_satisfied=True AND verify_outcome IN BYPASS_FAILURE_
        OUTCOMES`` count toward the tightening signal.
      * timestamp_unix: window filter input.
    """

    op_id: str
    category_scores: Dict[str, float] = field(default_factory=dict)
    floor_satisfied: bool = False
    verify_outcome: str = ""
    timestamp_unix: float = 0.0


@dataclass(frozen=True)
class MinedFloorRaise:
    """Pre-substrate result. The tightener produces these; then
    `propose_floor_raises_from_events()` lifts each into an
    `AdaptationProposal`."""

    category: str
    current_floor: float
    proposed_floor: float
    bypass_count: int
    source_event_ids: Tuple[str, ...]
    summary: str

    def proposal_id(self) -> str:
        """Stable id keyed on category + current floor + proposed
        floor. Re-mining the same window with the same evidence
        yields the same id → DUPLICATE_PROPOSAL_ID at substrate
        layer (idempotency)."""
        h = hashlib.sha256()
        h.update(self.category.encode("utf-8"))
        h.update(b"|")
        h.update(f"{self.current_floor:.6f}".encode("utf-8"))
        h.update(b"->")
        h.update(f"{self.proposed_floor:.6f}".encode("utf-8"))
        return f"adapt-ig-{h.hexdigest()[:24]}"

    def proposed_state_hash(self, current_state_hash: str) -> str:
        """sha256 over (current_state_hash | category | proposed_floor)
        — keeps the proposed hash deterministic AND structurally
        distinct from current_state_hash (substrate compares for
        distinctness)."""
        h = hashlib.sha256()
        h.update((current_state_hash or "").encode("utf-8"))
        h.update(b"|+|")
        h.update(self.category.encode("utf-8"))
        h.update(b":")
        h.update(f"{self.proposed_floor:.6f}".encode("utf-8"))
        return f"sha256:{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _filter_window(
    events: Iterable[ExplorationOutcomeLite],
    *,
    now_unix: float,
    window_days: int,
) -> List[ExplorationOutcomeLite]:
    if window_days <= 0:
        return list(events)
    cutoff = now_unix - (window_days * 86_400)
    out: List[ExplorationOutcomeLite] = []
    for e in events:
        if e.timestamp_unix == 0.0 or e.timestamp_unix >= cutoff:
            out.append(e)
    return out


def _filter_bypass_failures(
    events: Sequence[ExplorationOutcomeLite],
) -> List[ExplorationOutcomeLite]:
    """Keep ONLY ops where the exploration gate was satisfied but
    the patch failed VERIFY anyway — the structural signal that the
    floor was not strict enough for the pattern this op exhibited.

    Per §7.1 step 3 of the spec."""
    return [
        e for e in events
        if e.floor_satisfied and e.verify_outcome in BYPASS_FAILURE_OUTCOMES
    ]


def _identify_weakest_category(
    events: Sequence[ExplorationOutcomeLite],
) -> Optional[Tuple[str, int, Tuple[str, ...]]]:
    """Find the category that scored lowest (averaged) across the
    bypass-failure ops. That category's floor is the candidate for
    tightening.

    Returns (weakest_category, count_of_ops_where_it_was_weakest,
    source_op_ids). Returns None if no clear winner (e.g., empty
    input or no scored categories).
    """
    if not events:
        return None
    # Per §7.1 step 4: identify the weakest category PER OP, then
    # group by which-category-was-weakest. The category cited most
    # often wins; ties break by alphabetical order (deterministic).
    weakest_per_op: Dict[str, List[str]] = {}  # category -> [op_ids]
    for e in events:
        if not e.category_scores:
            continue
        # Find the lowest-scoring category in THIS op.
        items = sorted(e.category_scores.items())  # alpha tie-break
        items.sort(key=lambda kv: kv[1])  # then by score asc
        weakest_cat = items[0][0]
        weakest_per_op.setdefault(weakest_cat, []).append(e.op_id)
    if not weakest_per_op:
        return None
    # Pick the category that was weakest most often.
    sorted_cats = sorted(
        weakest_per_op.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),  # by count desc, then alpha
    )
    cat, op_ids = sorted_cats[0]
    return (cat, len(op_ids), tuple(op_ids))


def compute_proposed_floor(
    current_floor: float,
    *,
    raise_pct: Optional[int] = None,
    min_nominal_raise: int = MIN_NOMINAL_RAISE,
) -> float:
    """Per §7.1 step 5: `current_floor + ceil(current_floor * pct/100)`,
    floor-shaped to at least `min_nominal_raise`.

    Pure function (testable independently). The cap on pct happens
    inside `get_floor_raise_pct()` — by the time pct reaches here,
    it's already in [1, MAX_FLOOR_RAISE_PCT]."""
    pct = raise_pct if raise_pct is not None else get_floor_raise_pct()
    if current_floor <= 0:
        # Defensive: a zero/negative current floor should still bump
        # by min_nominal_raise so the math progresses.
        return float(min_nominal_raise)
    nominal_raise = math.ceil(current_floor * (pct / 100.0))
    if nominal_raise < min_nominal_raise:
        nominal_raise = min_nominal_raise
    return float(current_floor + nominal_raise)


# ---------------------------------------------------------------------------
# Public mining pipeline
# ---------------------------------------------------------------------------


def mine_floor_raises_from_events(
    events: Iterable[ExplorationOutcomeLite],
    *,
    current_floors: Dict[str, float],
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    raise_pct: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[MinedFloorRaise]:
    """Pure function: identifies the single weakest-category floor
    raise per call (one MinedFloorRaise OR empty). Per §7.1 the
    spec is "the candidate for floor tightening" (singular per
    cycle) — keeps the operator-review surface trim.

    Caller supplies `current_floors` (category -> current floor).
    The tightener proposes a raise ONLY for the most-weakest
    category; ties are alpha-sorted (deterministic).
    """
    th = threshold if threshold is not None else get_floor_threshold()
    wd = window_days if window_days is not None else get_window_days()

    in_window = _filter_window(
        events, now_unix=now_unix, window_days=wd,
    )
    bypass_failures = _filter_bypass_failures(in_window)
    if len(bypass_failures) < th:
        return []
    weakest = _identify_weakest_category(bypass_failures)
    if weakest is None:
        return []
    category, count, source_ids = weakest
    if count < th:
        # Even though there are enough total bypass-failures, the
        # *weakest* category specifically didn't show up enough
        # times to justify tightening it.
        return []
    current_floor = float(current_floors.get(category, 0.0))
    proposed_floor = compute_proposed_floor(
        current_floor, raise_pct=raise_pct,
    )
    if proposed_floor <= current_floor:
        # Defensive: shouldn't happen with min_nominal_raise=1, but
        # the cage check at the surface validator will catch any
        # edge case where a buggy compute slipped through.
        return []
    summary = (
        f"Mined from {count} of {len(bypass_failures)} bypass-failure "
        f"ops in the last {wd}-day window. Weakest exploration category "
        f"is {category!r} (current floor {current_floor:g} → proposed "
        f"floor {proposed_floor:g}, {(get_floor_raise_pct() if raise_pct is None else raise_pct)}% per cycle)."
    )
    return [MinedFloorRaise(
        category=category,
        current_floor=current_floor,
        proposed_floor=proposed_floor,
        bypass_count=count,
        source_event_ids=source_ids,
        summary=summary,
    )]


def propose_floor_raises_from_events(
    events: Iterable[ExplorationOutcomeLite],
    *,
    ledger: AdaptationLedger,
    current_floors: Dict[str, float],
    current_state_hash: str = "",
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    raise_pct: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[ProposeResult]:
    """End-to-end: mine candidates → submit through the Slice 1
    AdaptationLedger. Returns one ProposeResult per attempted
    submission (typically 0 or 1 per call per §7.1's "one weakest
    candidate" design).

    Master flag check happens here BEFORE any work.
    """
    if not is_enabled():
        return []
    candidates = mine_floor_raises_from_events(
        events,
        current_floors=current_floors,
        threshold=threshold,
        window_days=window_days,
        raise_pct=raise_pct,
        now_unix=now_unix,
    )
    wd = window_days if window_days is not None else get_window_days()
    results: List[ProposeResult] = []
    for c in candidates:
        evidence = AdaptationEvidence(
            window_days=wd,
            observation_count=c.bypass_count,
            source_event_ids=c.source_event_ids,
            summary=c.summary,
        )
        proposed_hash = c.proposed_state_hash(current_state_hash)
        # Mining-surface payload (Item #2 yaml_writer schema):
        # `floors: [{category, floor, ...prov}]`. Loader reads
        # `category` (validated against _KNOWN_CATEGORIES) + `floor`
        # (numeric > 0). Provenance auto-enriched by yaml_writer.
        payload = {
            "category": c.category,
            "floor": c.proposed_floor,
        }
        res = ledger.propose(
            proposal_id=c.proposal_id(),
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            proposal_kind="raise_floor",
            evidence=evidence,
            current_state_hash=current_state_hash or "sha256:initial",
            proposed_state_hash=proposed_hash,
            proposed_state_payload=payload,
        )
        results.append(res)
        if res.status is ProposeStatus.OK:
            logger.info(
                "[ExplorationFloorTightener] proposed floor raise for "
                "category=%s %g → %g (bypass_count=%d) proposal_id=%s",
                c.category, c.current_floor, c.proposed_floor,
                c.bypass_count, res.proposal_id,
            )
        elif res.status is ProposeStatus.DUPLICATE_PROPOSAL_ID:
            logger.debug(
                "[ExplorationFloorTightener] duplicate proposal_id "
                "(idempotent re-mine) category=%s",
                c.category,
            )
        else:
            logger.info(
                "[ExplorationFloorTightener] propose returned %s "
                "category=%s detail=%s",
                res.status.value, c.category, res.detail,
            )
    return results


# ---------------------------------------------------------------------------
# Surface validator — enforces "raise only" semantic
# ---------------------------------------------------------------------------


def _iron_gate_floor_validator(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Per-surface validator (Pass C §4.1 surface-specific layer).

    Asserts:
      * proposal_kind MUST be "raise_floor".
      * proposed_state_hash sha256-prefixed.
      * observation_count >= cage's threshold floor.
      * Evidence summary contains a `→` token (sanity check that
        the summary describes the proposed direction; defends
        against an attacker constructing a proposal with a doctored
        summary that omits the raise indicator).
    """
    if proposal.proposal_kind != "raise_floor":
        return (False, f"iron_gate_kind_must_be_raise_floor:{proposal.proposal_kind}")
    if not proposal.proposed_state_hash.startswith("sha256:"):
        return (False, f"iron_gate_proposed_hash_format:{proposal.proposed_state_hash[:32]}")
    th = get_floor_threshold()
    if proposal.evidence.observation_count < th:
        return (
            False,
            f"iron_gate_observation_count_below_threshold:"
            f"{proposal.evidence.observation_count} < {th}",
        )
    # Sanity: the summary should mention the raise direction. Pure
    # string check, deterministic, defense-in-depth against doctored
    # proposals.
    if "→" not in proposal.evidence.summary:
        return (
            False,
            "iron_gate_summary_missing_raise_indicator",
        )
    return (True, "iron_gate_raise_only_ok")


def install_surface_validator() -> None:
    register_surface_validator(
        AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
        _iron_gate_floor_validator,
    )


install_surface_validator()


__all__ = [
    "BYPASS_FAILURE_OUTCOMES",
    "DEFAULT_FLOOR_RAISE_PCT",
    "DEFAULT_FLOOR_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "ExplorationOutcomeLite",
    "MAX_FLOOR_RAISE_PCT",
    "MIN_NOMINAL_RAISE",
    "MinedFloorRaise",
    "compute_proposed_floor",
    "get_floor_raise_pct",
    "get_floor_threshold",
    "get_window_days",
    "install_surface_validator",
    "is_enabled",
    "mine_floor_raises_from_events",
    "propose_floor_raises_from_events",
]

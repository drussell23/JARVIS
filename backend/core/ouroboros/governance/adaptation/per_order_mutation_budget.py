"""RR Pass C Slice 4a — Per-Order mutation budget calibration.

Per `memory/project_reverse_russian_doll_pass_c.md` §8.1:

  > The current `ScopedToolBackend._mutations_count` budget is a
  > single value. Pass C splits this into JARVIS_ORDER1_MAX_MUTATIONS
  > and JARVIS_ORDER2_MAX_MUTATIONS (default 1 — far stricter for
  > Order-2 ops). The adaptation surface here proposes **lowering**
  > JARVIS_ORDER2_MAX_MUTATIONS if Order-2 ops are observed using
  > fewer mutations than budgeted (over-budget waste = wasted
  > attack surface).

This is the third adaptive surface (Slice 2 mined patterns; Slice 3
tightened exploration floors). Same composition pattern: pure
stdlib analyzer over caller-supplied event lists + auto-registered
surface validator that enforces the cage rule (per Pass C §4.1:
mutation budgets only LOWER, never raise).

## Why "lower budget" tightens

Mutation budget = max number of mutating tool calls per op. A
LOWER budget = STRICTER cage (op is forced to make fewer changes
per dispatch). Per the universal allowlist `lower_budget` is one
of the 5 strict-direction kinds. A `raise_budget` proposal would
be loosening — Pass C cannot loosen via any path.

## Activation path (Slice 6 wires this)

Approved budget changes land in `.jarvis/adapted_mutation_budgets.yaml`
keyed by Order (1 or 2). At GENERATE/APPLY time, the orchestrator
reads `min(env_budget, adapted_budget)` — adapted budgets can only
LOWER below the env-tuned static value. (This module ships the
proposer; orchestrator wiring is a follow-up.)

## Authority surface

  * Pure function over caller-supplied `MutationUsageLite` lists.
  * Writes via `AdaptationLedger.propose()` only.
  * No subprocess, no env mutation, no network.
  * Stdlib-only (plus the Slice 1 substrate).
  * Auto-registers a per-surface validator at module-import.

## Default-off

`JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED` (default false).
"""
from __future__ import annotations

import hashlib
import logging
import os
import statistics
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

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


# Per §8.5 default 5 — same threshold floor as Slice 3 (cross-cutting
# observation count for proposing a per-Order tightening).
DEFAULT_BUDGET_THRESHOLD: int = 5

# Adaptation window in days (shared default).
DEFAULT_WINDOW_DAYS: int = 7

# Hard floor on Order-2 budget. Cage rule: even if observed usage
# is 0, we never propose a budget BELOW this — an Order-2 op needs
# at least one mutation to be functional (otherwise the FSM should
# have rejected it earlier as a no-op).
MIN_ORDER2_BUDGET: int = 1

# Order tag values. Caller assigns one per op based on whether the
# op's target_files matched Pass B's Order-2 manifest.
ORDER_1: int = 1
ORDER_2: int = 2


def get_budget_threshold() -> int:
    raw = os.environ.get("JARVIS_ADAPTATION_BUDGET_THRESHOLD")
    if raw is None:
        return DEFAULT_BUDGET_THRESHOLD
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_BUDGET_THRESHOLD
    except ValueError:
        return DEFAULT_BUDGET_THRESHOLD


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
    """Master flag — ``JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED``
    (default ``true`` — graduated in Move 1 Pass C cadence 2026-04-29).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy hot-reverts."""
    raw = os.environ.get(
        "JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Move 1 Pass C cadence)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Event input shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MutationUsageLite:
    """Minimal shape: one op's observed mutation usage.

    Fields:
      * op_id: source op id for evidence.
      * order: 1 or 2 — which budget this op was governed by.
      * observed_mutations: actual number of mutating tool calls
        the op consumed.
      * budget_at_time: the budget the op was granted at GATE time.
        The "underutilization gap" = budget_at_time - observed.
      * timestamp_unix: window filter input.
    """

    op_id: str
    order: int = ORDER_2
    observed_mutations: int = 0
    budget_at_time: int = 0
    timestamp_unix: float = 0.0


@dataclass(frozen=True)
class MinedBudgetLowering:
    """Pre-substrate result. The adapter produces these; then
    `propose_budget_lowering_from_events()` lifts each into an
    `AdaptationProposal`."""

    order: int
    current_budget: int
    proposed_budget: int
    underutilized_count: int
    source_event_ids: Tuple[str, ...]
    summary: str

    def proposal_id(self) -> str:
        h = hashlib.sha256()
        h.update(f"order-{self.order}".encode("utf-8"))
        h.update(b"|")
        h.update(f"{self.current_budget}".encode("utf-8"))
        h.update(b"->")
        h.update(f"{self.proposed_budget}".encode("utf-8"))
        return f"adapt-mb-{h.hexdigest()[:24]}"

    def proposed_state_hash(self, current_state_hash: str) -> str:
        h = hashlib.sha256()
        h.update((current_state_hash or "").encode("utf-8"))
        h.update(b"|+|")
        h.update(f"order-{self.order}:{self.proposed_budget}".encode("utf-8"))
        return f"sha256:{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _filter_window(
    events: Iterable[MutationUsageLite],
    *,
    now_unix: float,
    window_days: int,
) -> List[MutationUsageLite]:
    if window_days <= 0:
        return list(events)
    cutoff = now_unix - (window_days * 86_400)
    out: List[MutationUsageLite] = []
    for e in events:
        if e.timestamp_unix == 0.0 or e.timestamp_unix >= cutoff:
            out.append(e)
    return out


def _compute_proposed_budget_for_order(
    events: Sequence[MutationUsageLite],
    order: int,
    current_budget: int,
) -> Optional[Tuple[int, int, Tuple[str, ...]]]:
    """For ops of `order`, compute the proposed lower budget based
    on observed usage.

    Heuristic per §8.1: if Order-X ops consistently use FEWER
    mutations than budgeted, lower the budget toward the observed
    p95 (the max observed in the window — single-pass and bounded).

    Returns (proposed_budget, underutilized_count, source_op_ids)
    or None if no lowering is justified.
    """
    matching = [e for e in events if e.order == order]
    if not matching:
        return None
    underutilized = [
        e for e in matching
        if e.observed_mutations < e.budget_at_time
    ]
    if not underutilized:
        return None
    # Use the MAX observed usage as the proposed new budget. This
    # is the safe choice: any op that needed N mutations in the
    # window will still get N under the new budget. (Future Slice
    # could refine to p95 once we have enough data; for v1 max-
    # observed is the conservative choice.)
    max_observed = max(e.observed_mutations for e in matching)
    # Cage floor: even with zero observed, never go below
    # MIN_ORDER2_BUDGET for Order 2 (Order 1 has no hard floor —
    # the env-tuned static value remains the lower bound at apply
    # time per §8.4 min-rule).
    if order == ORDER_2:
        proposed = max(max_observed, MIN_ORDER2_BUDGET)
    else:
        proposed = max(max_observed, 1)  # at least 1 mutation possible
    if proposed >= current_budget:
        # No tightening to propose.
        return None
    return (
        proposed,
        len(underutilized),
        tuple(e.op_id for e in underutilized if e.op_id),
    )


# ---------------------------------------------------------------------------
# Public mining pipeline
# ---------------------------------------------------------------------------


def mine_budget_lowerings_from_events(
    events: Iterable[MutationUsageLite],
    *,
    current_budgets: dict,  # {1: int, 2: int}
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[MinedBudgetLowering]:
    """Pure function. Returns up to 2 MinedBudgetLowerings (one per
    Order). Each represents a proposed lower budget for that Order
    based on observed underutilization in the window.

    Skips an Order if:
      * threshold underutilized count not met, OR
      * proposed budget would equal/exceed current (no tightening).
    """
    th = threshold if threshold is not None else get_budget_threshold()
    wd = window_days if window_days is not None else get_window_days()
    in_window = _filter_window(events, now_unix=now_unix, window_days=wd)

    out: List[MinedBudgetLowering] = []
    for order in (ORDER_1, ORDER_2):
        current_budget = int(current_budgets.get(order, 0))
        if current_budget <= 0:
            continue
        result = _compute_proposed_budget_for_order(
            in_window, order, current_budget,
        )
        if result is None:
            continue
        proposed, underutil_count, src_ids = result
        if underutil_count < th:
            continue
        # Also compute average observed for the summary (operator
        # context — they want to see how slack the budget was).
        order_ops = [e for e in in_window if e.order == order]
        avg_observed = (
            statistics.fmean(e.observed_mutations for e in order_ops)
            if order_ops else 0.0
        )
        summary = (
            f"Order-{order} mutation budget underutilization in last {wd}d "
            f"window: {underutil_count} of {len(order_ops)} ops used fewer "
            f"mutations than budgeted (avg={avg_observed:.2f}, "
            f"max={proposed}). Proposed lower budget: "
            f"{current_budget} → {proposed}."
        )
        out.append(MinedBudgetLowering(
            order=order, current_budget=current_budget,
            proposed_budget=proposed,
            underutilized_count=underutil_count,
            source_event_ids=src_ids, summary=summary,
        ))
    return out


def propose_budget_lowerings_from_events(
    events: Iterable[MutationUsageLite],
    *,
    ledger: AdaptationLedger,
    current_budgets: dict,
    current_state_hash: str = "",
    threshold: Optional[int] = None,
    window_days: Optional[int] = None,
    now_unix: float = 0.0,
) -> List[ProposeResult]:
    """End-to-end: mine candidates → submit through the substrate."""
    if not is_enabled():
        return []
    candidates = mine_budget_lowerings_from_events(
        events,
        current_budgets=current_budgets,
        threshold=threshold,
        window_days=window_days,
        now_unix=now_unix,
    )
    wd = window_days if window_days is not None else get_window_days()
    results: List[ProposeResult] = []
    for c in candidates:
        evidence = AdaptationEvidence(
            window_days=wd,
            observation_count=c.underutilized_count,
            source_event_ids=c.source_event_ids,
            summary=c.summary,
        )
        proposed_hash = c.proposed_state_hash(current_state_hash)
        # Mining-surface payload (Item #2 yaml_writer schema):
        # `budgets: [{order, budget, ...prov}]`. Loader validates
        # `order` ∈ {1, 2} + `budget` non-negative int + Order-2
        # floor MIN_ORDER2_BUDGET. Provenance auto-enriched.
        payload = {
            "order": c.order,
            "budget": c.proposed_budget,
        }
        res = ledger.propose(
            proposal_id=c.proposal_id(),
            surface=AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET,
            proposal_kind="lower_budget",
            evidence=evidence,
            current_state_hash=current_state_hash or "sha256:initial",
            proposed_state_hash=proposed_hash,
            proposed_state_payload=payload,
        )
        results.append(res)
        if res.status is ProposeStatus.OK:
            logger.info(
                "[PerOrderMutationBudget] proposed lower budget for "
                "order=%d %d → %d (underutilized=%d) proposal_id=%s",
                c.order, c.current_budget, c.proposed_budget,
                c.underutilized_count, res.proposal_id,
            )
    return results


# ---------------------------------------------------------------------------
# Surface validator
# ---------------------------------------------------------------------------


def _per_order_budget_validator(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Per-surface validator (Pass C §4.1).

    Asserts:
      * proposal_kind MUST be "lower_budget".
      * proposed_state_hash sha256-prefixed.
      * observation_count >= cage's threshold floor.
      * Summary contains '→' (sanity check on direction indicator).
    """
    if proposal.proposal_kind != "lower_budget":
        return (
            False,
            f"per_order_budget_kind_must_be_lower_budget:{proposal.proposal_kind}",
        )
    if not proposal.proposed_state_hash.startswith("sha256:"):
        return (
            False,
            f"per_order_budget_proposed_hash_format:{proposal.proposed_state_hash[:32]}",
        )
    th = get_budget_threshold()
    if proposal.evidence.observation_count < th:
        return (
            False,
            f"per_order_budget_observation_count_below_threshold:"
            f"{proposal.evidence.observation_count} < {th}",
        )
    if "→" not in proposal.evidence.summary:
        return (False, "per_order_budget_summary_missing_direction_indicator")
    return (True, "per_order_budget_lower_only_ok")


def install_surface_validator() -> None:
    register_surface_validator(
        AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET,
        _per_order_budget_validator,
    )


install_surface_validator()


__all__ = [
    "DEFAULT_BUDGET_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "MIN_ORDER2_BUDGET",
    "MinedBudgetLowering",
    "MutationUsageLite",
    "ORDER_1",
    "ORDER_2",
    "get_budget_threshold",
    "get_window_days",
    "install_surface_validator",
    "is_enabled",
    "mine_budget_lowerings_from_events",
    "propose_budget_lowerings_from_events",
]

"""Slice 119 — Bounded M10 Synapse (Order-2 RSI, human-gated).

M10 (the ArchitectureProposer) is the "Neurosurgeon" — Second-Order RSI, the
system proposing upgrades to its OWN cognition. The PRD holds that this must
NEVER be a self-authorizing autonomous driver: the operator is the recursion
bound (§1, the Zero-Order Doll). This synapse is the bounded wiring that lets
the live orchestrator *trigger a proposal* while keeping that invariant absolute:

  TRIGGER (high shannon-entropy OR repeated algorithmic failures)
     │
     ▼  evaluate_m10_routing()  → route + FORCED tier = APPROVAL_REQUIRED
  propose_structural_upgrade()  → M10 synthesizes a proposal
     │
     ▼  the proposal is PERSISTED to the M10 pending store (awaiting the
        operator's signature) and the triggering op is routed to
        APPROVAL_REQUIRED — the FSM pauses for the human.
     ✗  NO PATH TO APPLY. This module imports nothing from change_engine /
        the apply path; ``propose_structural_upgrade`` only synthesizes + stores.
        A structural cognitive upgrade reaches the codebase ONLY after the
        operator explicitly signs it (the existing M10 graduation surface).

Defense-in-depth: an M10 proposal is also a governance self-mod chain step, so
the Slice-104 recursion-depth gate independently bounds it. Master
``JARVIS_M10_SYNAPSE_ENABLED`` — §33.1 default-FALSE: M10 stays dormant unless
the operator deliberately ignites it. NEVER raises into the FSM.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ouroboros.m10_synapse")

_TRUTHY = ("1", "true", "yes", "on")

# The strict invariant: every M10 proposal routes here, full stop.
M10_FORCED_TIER = "APPROVAL_REQUIRED"

_ENV_MASTER = "JARVIS_M10_SYNAPSE_ENABLED"
_ENV_ENTROPY_THRESHOLD = "JARVIS_M10_ENTROPY_THRESHOLD"
_ENV_FAILURE_THRESHOLD = "JARVIS_M10_FAILURE_THRESHOLD"


def m10_synapse_enabled() -> bool:
    """§33.1 master — default FALSE. M10 is dormant unless explicitly ignited."""
    try:
        return (os.environ.get(_ENV_MASTER, "") or "").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def _entropy_threshold() -> float:
    try:
        return float(os.environ.get(_ENV_ENTROPY_THRESHOLD, "0.85"))
    except Exception:  # noqa: BLE001
        return 0.85


def _failure_threshold() -> int:
    try:
        return max(1, int(os.environ.get(_ENV_FAILURE_THRESHOLD, "3")))
    except Exception:  # noqa: BLE001
        return 3


def should_route_to_m10(
    *,
    shannon_entropy: Optional[float],
    recent_algorithmic_failures: int,
) -> bool:
    """The trigger predicate: the system is in a state where a *structural*
    cognitive upgrade is warranted — high domain entropy (scattered, no clear
    approach) OR repeated algorithmic failures (the current cognition keeps
    failing the same way). PURE; NEVER raises."""
    try:
        if shannon_entropy is not None and float(shannon_entropy) >= _entropy_threshold():
            return True
        if int(recent_algorithmic_failures) >= _failure_threshold():
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


@dataclass(frozen=True)
class M10RoutingDecision:
    route_to_m10: bool
    reason: str
    forced_tier: str  # M10_FORCED_TIER when routing, "" otherwise


def evaluate_m10_routing(
    *,
    shannon_entropy: Optional[float],
    recent_algorithmic_failures: int,
) -> M10RoutingDecision:
    """Decide whether the orchestrator should route this op to M10. When it
    does, the forced tier is ALWAYS APPROVAL_REQUIRED — the synapse can never
    return a self-authorizing route. Inert (no route) when the master is off."""
    if not m10_synapse_enabled():
        return M10RoutingDecision(False, "m10 synapse disabled (default)", "")
    if should_route_to_m10(shannon_entropy=shannon_entropy,
                           recent_algorithmic_failures=recent_algorithmic_failures):
        reason = (f"high entropy ({shannon_entropy}) / failures "
                  f"({recent_algorithmic_failures}) → structural upgrade warranted")
        return M10RoutingDecision(True, reason, M10_FORCED_TIER)
    return M10RoutingDecision(False, "no structural-upgrade trigger", "")


def propose_structural_upgrade(
    *,
    context: Any,
    proposer: Callable[[Any], Any],
    store_fn: Optional[Callable[[Any], bool]] = None,
) -> Optional[Any]:
    """Route to M10 to SYNTHESIZE a structural-upgrade proposal, then PERSIST it
    as pending (awaiting the operator's signature). **The proposal is NEVER
    executed/applied here** — this function has no path to change_engine/APPLY.
    ``proposer`` is injectable (production = the M10 proposal_synthesizer; tests
    = a fake). Returns the proposal, or None. NEVER raises into the FSM."""
    if not m10_synapse_enabled():
        return None
    try:
        proposal = proposer(context)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[M10Synapse] synthesis swallowed: %s", exc)
        return None
    if proposal is None:
        return None
    try:
        (store_fn or _default_store)(proposal)
    except Exception:  # noqa: BLE001 — persistence best-effort; never fatal
        logger.debug("[M10Synapse] proposal persist swallowed", exc_info=True)
    logger.warning(
        "[STRUCTURAL UPGRADE PROPOSAL] M10 proposed a cognitive upgrade — "
        "routed to APPROVAL_REQUIRED, PENDING operator signature. The FSM does "
        "NOT self-apply; the codebase is unchanged until the human signs.",
    )
    return proposal


def _default_store(proposal: Any) -> bool:
    """Persist via the existing M10 proposal store (pending lifecycle). NEVER
    raises."""
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import append_proposal
        return bool(append_proposal(proposal))
    except Exception:  # noqa: BLE001
        return False


def pending_m10_proposals(limit: int = 25) -> List[Dict[str, Any]]:
    """Read pending M10 proposals (for the operator Approval-Matrix UI). The
    AST-diff fields (proposed_class_name / proposed_ast_pin_name) surface here.
    Read-only; NEVER raises."""
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import list_pending_proposals
        out: List[Dict[str, Any]] = []
        for p in list_pending_proposals(limit):
            d = p.to_dict() if hasattr(p, "to_dict") else dict(getattr(p, "__dict__", {}))
            out.append(d)
        return out
    except Exception:  # noqa: BLE001
        return []

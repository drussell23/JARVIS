"""Q4 Priority #2 Slice 3 — COHERENCE_AUDITOR_BUDGETS surface validator.

Cage validator for the new ``AdaptationSurface.COHERENCE_AUDITOR_BUDGETS``
surface. Mirrors the discipline of the four sibling tighteners:

  * ``confidence_threshold_tightener._confidence_threshold_validator``
  * ``exploration_floor_tightener._iron_gate_floor_validator``
  * ``per_order_mutation_budget._per_order_mutation_budget_validator``
  * ``category_weight_rebalancer._category_weight_validator``

Each per-surface validator runs INSIDE ``AdaptationLedger.propose``
BEFORE the universal monotonic-tightening cage check. Together, they
form Pass C §4.1's two-layer defense:

  1. **Per-surface structural validator** (this module) — checks
     proposal shape + payload kinds + monotonic-tightening direction
     for the specific parameter set.
  2. **Universal cage** (``ledger.validate_monotonic_tightening``) —
     checks current_state_hash != proposed_state_hash and the
     monotonic-tightening rule across all surfaces uniformly.

Authority invariant:
  This module IMPORTS only ``adaptation.ledger`` substrate (validator
  registration + frozen dataclasses). It does NOT import authority
  modules. The validator is **read-only** — it inspects a proposal
  and returns ``(ok, detail)``. Mutation happens only through
  ``AdaptationLedger.propose`` (cage-gated) and the operator-approval
  path (``yaml_writer`` after ``/adapt approve``).

Auto-registration:
  ``install_surface_validator()`` is called unconditionally at module
  import (mirrors all four sibling tighteners). The closure-loop
  bridge imports this module to trigger registration.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Tuple

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationProposal,
    AdaptationSurface,
    register_surface_validator,
)

logger = logging.getLogger(__name__)


COHERENCE_BUDGET_TIGHTENER_SCHEMA_VERSION = (
    "coherence_budget_tightener.v1"
)


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------


# The set of parameter names ``coherence_action_bridge
# ._DefaultTighteningProposer`` produces — pinned here so a silent
# expansion in the proposer breaks this validator's tests (Slice 5
# graduation will AST-pin the literal set across both modules).
_VALID_PARAMETER_NAMES: frozenset = frozenset({
    "route_drift_pct",
    "recurrence_count",
    "confidence_rise_pct",
})


# ``proposal_kind`` for this surface mirrors the parameter name —
# 1:1 mapping. We accept the same vocabulary the Tightening intent
# produces.
_VALID_PROPOSAL_KINDS: frozenset = _VALID_PARAMETER_NAMES


# Direction tokens the bridge stamps when building the payload.
_DIRECTIONS_VALID: frozenset = frozenset({
    "smaller_is_tighter",
    "larger_is_tighter",
})


# Indicator the validator looks for in the evidence summary so a
# half-formed evidence string can't slip through.
_TIGHTEN_INDICATOR: str = "→"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def _coherence_budget_validator(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Per-surface structural validator for
    ``AdaptationSurface.COHERENCE_AUDITOR_BUDGETS``.

    Returns ``(ok, detail)``. NEVER raises — internal failures
    collapse to ``(False, "validator_internal_error:...")`` so the
    proposal is cleanly rejected with structured reason.

    Decision tree (top-down, first failure short-circuits):

      1. ``proposal.surface`` is ``COHERENCE_AUDITOR_BUDGETS``
         (defense-in-depth — the dispatcher already keys by surface).
      2. ``proposal.proposal_kind`` ∈ :data:`_VALID_PROPOSAL_KINDS`.
      3. ``proposal.evidence.observation_count`` ≥ 1
         (the bridge always stamps 1+ for a non-empty advisory).
      4. :data:`_TIGHTEN_INDICATOR` ∈ ``proposal.evidence.summary``.
      5. ``proposal.proposed_state_payload`` shape:

         ``{
              "current": {"parameter_name": str,
                          "value": float, "direction": str},
              "proposed": {"parameter_name": str,
                          "value": float, "direction": str}
         }``

         — both branches present, both have valid direction tokens,
         both reference the same ``parameter_name``.
      6. Direction token is in :data:`_DIRECTIONS_VALID`.
      7. Monotonic-tightening: per ``direction``, the proposed value
         must be strictly tighter than current. ``smaller_is_tighter``
         requires ``proposed < current``; ``larger_is_tighter``
         requires ``proposed > current``. Equal values reject as
         no-op (the universal cage would reject them too — fail
         fast here for a clean per-surface reason)."""
    try:
        # 1. Surface match
        if proposal.surface is not (
            AdaptationSurface.COHERENCE_AUDITOR_BUDGETS
        ):
            return (
                False,
                f"coherence_budget_validator_wrong_surface:"
                f"{proposal.surface.value}",
            )

        # 2. Kind in vocabulary
        if proposal.proposal_kind not in _VALID_PROPOSAL_KINDS:
            return (
                False,
                f"coherence_budget_kind_unknown:"
                f"{proposal.proposal_kind}",
            )

        # 3. Observation-count floor
        try:
            obs_count = int(proposal.evidence.observation_count)
        except (TypeError, ValueError):
            obs_count = 0
        if obs_count < 1:
            return (
                False,
                f"coherence_budget_obs_count_below_floor:"
                f"{obs_count}",
            )

        # 4. Tighten indicator in evidence summary
        if _TIGHTEN_INDICATOR not in (
            proposal.evidence.summary or ""
        ):
            return (
                False,
                "coherence_budget_summary_missing_tighten_indicator",
            )

        # 5. Payload shape
        payload = proposal.proposed_state_payload
        if not isinstance(payload, Mapping):
            return (
                False,
                "coherence_budget_payload_not_mapping",
            )
        current = payload.get("current")
        proposed = payload.get("proposed")
        if not isinstance(current, Mapping) or not isinstance(
            proposed, Mapping,
        ):
            return (
                False,
                "coherence_budget_payload_branches_not_mappings",
            )

        # 5a. Per-branch field presence + types
        for branch_name, branch in (
            ("current", current), ("proposed", proposed),
        ):
            if "parameter_name" not in branch:
                return (
                    False,
                    f"coherence_budget_{branch_name}_missing_parameter_name",
                )
            if "value" not in branch:
                return (
                    False,
                    f"coherence_budget_{branch_name}_missing_value",
                )
            if "direction" not in branch:
                return (
                    False,
                    f"coherence_budget_{branch_name}_missing_direction",
                )

        # 5b. parameter_name agreement across branches + matches
        # proposal_kind
        cur_param = str(current["parameter_name"])
        prop_param = str(proposed["parameter_name"])
        if cur_param != prop_param:
            return (
                False,
                f"coherence_budget_parameter_name_mismatch:"
                f"{cur_param}!={prop_param}",
            )
        if cur_param != proposal.proposal_kind:
            return (
                False,
                f"coherence_budget_parameter_proposal_kind_mismatch:"
                f"{cur_param}!={proposal.proposal_kind}",
            )

        # 6. Direction token
        cur_dir = str(current["direction"])
        prop_dir = str(proposed["direction"])
        if cur_dir != prop_dir:
            return (
                False,
                f"coherence_budget_direction_mismatch:"
                f"{cur_dir}!={prop_dir}",
            )
        if cur_dir not in _DIRECTIONS_VALID:
            return (
                False,
                f"coherence_budget_direction_unknown:{cur_dir}",
            )

        # 7. Monotonic-tightening per direction
        try:
            cur_value = float(current["value"])
            prop_value = float(proposed["value"])
        except (TypeError, ValueError):
            return (
                False,
                "coherence_budget_value_not_numeric",
            )
        if cur_dir == "smaller_is_tighter":
            if not (prop_value < cur_value):
                return (
                    False,
                    f"coherence_budget_not_strictly_smaller:"
                    f"current={cur_value} proposed={prop_value}",
                )
        else:  # larger_is_tighter (only other allowed value)
            if not (prop_value > cur_value):
                return (
                    False,
                    f"coherence_budget_not_strictly_larger:"
                    f"current={cur_value} proposed={prop_value}",
                )

        return (True, "coherence_budget_payload_ok")

    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CoherenceBudgetTightener] validator internal error: %s",
            exc,
        )
        return (
            False,
            f"validator_internal_error:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Helper for the closure-loop bridge: build the payload shape from
# a TighteningIntent
# ---------------------------------------------------------------------------


def build_proposed_state_payload_for_intent(
    intent: Any,
) -> dict:
    """Compose the validator-expected payload from a
    :class:`TighteningIntent`. Pure function; NEVER raises.
    Returns a structurally-valid empty shape on bad input so the
    validator's payload-presence check fires cleanly."""
    try:
        return {
            "current": {
                "parameter_name": str(
                    getattr(intent, "parameter_name", ""),
                ),
                "value": float(
                    getattr(intent, "current_value", 0.0),
                ),
                "direction": str(
                    getattr(intent, "direction", ""),
                ),
            },
            "proposed": {
                "parameter_name": str(
                    getattr(intent, "parameter_name", ""),
                ),
                "value": float(
                    getattr(intent, "proposed_value", 0.0),
                ),
                "direction": str(
                    getattr(intent, "direction", ""),
                ),
            },
        }
    except Exception:  # noqa: BLE001 — last-resort defensive
        return {"current": {}, "proposed": {}}


# ---------------------------------------------------------------------------
# Auto-registration at module import (mirror of all four sibling
# tighteners)
# ---------------------------------------------------------------------------


def install_surface_validator() -> None:
    """Idempotent: registers ``_coherence_budget_validator`` against
    ``AdaptationSurface.COHERENCE_AUDITOR_BUDGETS``. Called
    unconditionally at module import — same convention as the four
    sibling tighteners."""
    register_surface_validator(
        AdaptationSurface.COHERENCE_AUDITOR_BUDGETS,
        _coherence_budget_validator,
    )


install_surface_validator()


__all__ = [
    "COHERENCE_BUDGET_TIGHTENER_SCHEMA_VERSION",
    "build_proposed_state_payload_for_intent",
    "install_surface_validator",
]

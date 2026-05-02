"""Gap #2 Slice 2 — Confidence-monitor threshold surface validator.

Bridges the Slice 1 substrate (``verification.confidence_policy``)
into the existing Pass C cage (``adaptation.ledger``). Adds a 6th
adaptive surface that reuses the propose / approve / reject
lifecycle without weakening the universal cage rule.

This module is the SECOND-GATE in the propose path:

  ``AdaptationLedger.propose(proposal)``
        │
        ├── 1. propose-master gate (env)
        ├── 2. surface validator  ← THIS MODULE for confidence
        ├── 3. universal cage rule (compute_monotonic_tightening_verdict)
        └── 4. capacity / dedup / persist

The surface validator runs FIRST inside ``propose`` so structural
defects (wrong kind, missing payload, malformed hash) are caught
before the universal cage check even sees the proposal. By the time
the universal cage runs, the surface has confirmed the proposal is
*shaped right*. The universal cage then enforces the conjunctive
tightening rule via Slice 1's ``compute_policy_diff`` — which is
the SAME predicate the validator uses, so divergence is impossible
by construction.

## Why no auto-miner here

Mirror of ``exploration_floor_tightener`` was tempting, but
confidence thresholds are operator policy, not auto-mined evidence:

  * Iron-gate floors map directly to *observed VERIFY outcomes*
    (regression rate per category) — auto-mining is sound.
  * Confidence thresholds map to a *cost / quality tradeoff* that
    only the operator can value. The model can SURFACE evidence
    (sustained_low_confidence pattern, fallback rate climbing) via
    the existing SSE event stream, but the *floor* / *window* /
    *factor* / *enforce* deltas need an operator decision.

Slice 4 ships the operator surface (HTTP POST + IDE panel). This
slice ships the validator. An auto-miner could be added later as
a follow-up — the validator does not care WHO produced the
proposal; that's the right separation.

## Authority surface

  * Imports stdlib + ``adaptation.ledger`` (substrate) +
    ``verification.confidence_policy`` (decision rule) ONLY.
  * No subprocess, no env mutation, no network, no filesystem I/O.
  * Auto-registers a per-surface validator at module-import via
    ``register_surface_validator(SURFACE, _validator)``. Mirrors
    Slices 2-5 of Pass C.
  * MUST NOT import: orchestrator / iron_gate / policy / risk_engine /
    change_engine / tool_executor / providers / candidate_generator /
    semantic_guardian / semantic_firewall / scoped_tool_backend /
    subagent_scheduler / confidence_monitor (env accessors are
    consumed via ``confidence_policy.ConfidencePolicy.from_dict``,
    NOT by direct env reads here — keeps the validator stateless).

## Default-on at module level

Surface validator registration is unconditional at module import:
operators NEVER want a write surface that silently degrades to
"no validation" mode. The kill switch lives on Slice 4's HTTP
router (the POST surface refuses requests when the master is off);
once a proposal reaches ``AdaptationLedger.propose``, the
validator MUST run.
"""
from __future__ import annotations

import logging
import os
from typing import FrozenSet, Optional, Tuple

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationProposal,
    AdaptationSurface,
    register_surface_validator,
)
from backend.core.ouroboros.governance.verification.confidence_policy import (
    CONFIDENCE_POLICY_SCHEMA_VERSION,
    ConfidencePolicy,
    ConfidencePolicyKind,
    ConfidencePolicyOutcome,
    PolicyDiff,
    compute_policy_diff,
)

logger = logging.getLogger(__name__)


CONFIDENCE_THRESHOLD_TIGHTENER_SCHEMA_VERSION: str = (
    "confidence_threshold_tightener.1"
)


# ---------------------------------------------------------------------------
# Closed proposal-kind vocabulary (sourced from Slice 1's enum)
# ---------------------------------------------------------------------------
#
# A proposal MAY target one specific dimension (raise_floor,
# shrink_window, widen_approaching, enable_enforce) or carry the
# multi-dim sentinel "multi_dim_tighten" when it moves several knobs
# atomically. The validator accepts any kind in this closed set; the
# universal cage's compute_policy_diff is the structural arbiter of
# whether the *resulting* state-diff actually tightens.
#
# DISABLED is excluded because it's the master-off sentinel; a
# proposal stamped with DISABLED would have produced PolicyDiff
# (outcome=DISABLED) which carries an empty kinds tuple and
# wouldn't have a payload to persist.

_PROPOSAL_KIND_MULTI_DIM: str = "multi_dim_tighten"

_VALID_PROPOSAL_KINDS: FrozenSet[str] = frozenset(
    [k.value for k in ConfidencePolicyKind if k is not ConfidencePolicyKind.DISABLED]
    + [_PROPOSAL_KIND_MULTI_DIM]
)


# ---------------------------------------------------------------------------
# Env knobs (operator-tunable bounds; never a code change)
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    """Read an int env knob clamped to [floor, ceiling]. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def confidence_threshold_observation_count_floor() -> int:
    """``JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR`` (default
    ``3``, floor ``1``, ceiling ``1000``).

    Minimum number of supporting observations a proposal MUST cite
    in ``evidence.observation_count`` to clear the surface
    validator. Operator-tunable; stricter (higher) requires more
    evidence per proposal, looser (lower) accepts smaller-sample
    deltas.

    Default of 3 reflects the operator-driven nature of this
    surface — a single sustained_low_confidence event is enough
    pattern to consider tightening, but two corroborating events
    raise confidence (per the operator preference for "trust but
    verify"). NEVER raises."""
    return _env_int_clamped(
        "JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR",
        3, floor=1, ceiling=1000,
    )


# ---------------------------------------------------------------------------
# Tightening-direction indicator (defense-in-depth on summary text)
# ---------------------------------------------------------------------------
#
# Mirror of `exploration_floor_tightener`'s `→` requirement. The
# indicator must appear in `evidence.summary` to defend against an
# attacker constructing a proposal with a doctored summary that
# omits the direction. The indicator is a Unicode arrow that humans
# render as a clear left-to-right transition.

_TIGHTEN_INDICATOR: str = "→"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def _confidence_threshold_validator(
    proposal: AdaptationProposal,
) -> Tuple[bool, str]:
    """Per-Pass-C surface validator for the
    ``CONFIDENCE_MONITOR_THRESHOLDS`` surface.

    Returns ``(ok, detail)``. NEVER raises — the ledger never sees
    an exception; on internal failure we return
    ``(False, "validator_internal_error:...")`` so the proposal is
    cleanly rejected with a structured reason.

    Decision tree (top-down, first failure short-circuits):

      1. ``proposal.surface`` matches this validator's surface
         (defense-in-depth — the dispatcher already keys by surface).
      2. ``proposal.proposal_kind`` ∈ ``_VALID_PROPOSAL_KINDS``.
      3. ``proposal.proposed_state_hash`` starts with ``"sha256:"``
         (provenance: hash MUST be computed by Slice 1's
         ``ConfidencePolicy.state_hash`` which sha256-prefixes).
      4. ``proposal.evidence.observation_count`` ≥ env floor.
      5. ``_TIGHTEN_INDICATOR`` ∈ ``proposal.evidence.summary``.
      6. ``proposal.proposed_state_payload`` is a non-empty dict.
      7. Payload deserializes to a ``ConfidencePolicy`` AND the
         payload schema_version matches Slice 1's current version
         (so Slice-N+1 schema upgrades don't silently accept
         stale-shape payloads).
      8. Substrate decision: ``compute_policy_diff(current_payload,
         proposed_payload)`` returns ``APPLIED`` with at least one
         moved kind. The diff predicate is the SAME one the
         universal cage will run; checking it here surfaces a
         clean per-surface reason instead of a generic cage
         rejection.

    Note: step 8 requires a ``current_state_payload`` companion in
    the proposal payload (under key ``"current"``) so the validator
    can reconstruct both sides. Slice 4's HTTP submission path
    populates this; tests construct it explicitly.
    """
    try:
        # 1. Surface match
        if proposal.surface is not (
            AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS
        ):
            return (
                False,
                f"confidence_validator_wrong_surface:"
                f"{proposal.surface.value}",
            )

        # 2. Kind in vocabulary
        if proposal.proposal_kind not in _VALID_PROPOSAL_KINDS:
            return (
                False,
                f"confidence_kind_not_in_vocabulary:"
                f"{proposal.proposal_kind} (allowed: "
                f"{sorted(_VALID_PROPOSAL_KINDS)})",
            )

        # 3. sha256 hash prefix
        if not proposal.proposed_state_hash.startswith("sha256:"):
            return (
                False,
                f"confidence_proposed_hash_format:"
                f"{proposal.proposed_state_hash[:32]}",
            )
        if not proposal.current_state_hash.startswith("sha256:"):
            return (
                False,
                f"confidence_current_hash_format:"
                f"{proposal.current_state_hash[:32]}",
            )

        # 4. Observation count floor
        threshold = confidence_threshold_observation_count_floor()
        if proposal.evidence.observation_count < threshold:
            return (
                False,
                f"confidence_observation_count_below_threshold:"
                f"{proposal.evidence.observation_count} < {threshold}",
            )

        # 5. Tightening indicator in summary
        if _TIGHTEN_INDICATOR not in proposal.evidence.summary:
            return (
                False,
                "confidence_summary_missing_tighten_indicator",
            )

        # 6. Payload presence
        payload = proposal.proposed_state_payload
        if not isinstance(payload, dict) or not payload:
            return (
                False,
                "confidence_payload_missing_or_empty",
            )

        # 7. Payload deserialization + schema parity
        proposed_raw = payload.get("proposed")
        current_raw = payload.get("current")
        if not isinstance(proposed_raw, dict):
            return (
                False,
                "confidence_payload_proposed_not_dict",
            )
        if not isinstance(current_raw, dict):
            return (
                False,
                "confidence_payload_current_not_dict",
            )

        proposed_schema = str(
            proposed_raw.get(
                "schema_version", CONFIDENCE_POLICY_SCHEMA_VERSION,
            )
        )
        if proposed_schema != CONFIDENCE_POLICY_SCHEMA_VERSION:
            return (
                False,
                f"confidence_payload_schema_mismatch:"
                f"{proposed_schema} != "
                f"{CONFIDENCE_POLICY_SCHEMA_VERSION}",
            )

        try:
            proposed_policy = ConfidencePolicy.from_dict(proposed_raw)
            current_policy = ConfidencePolicy.from_dict(current_raw)
        except Exception as exc:  # noqa: BLE001 — defensive
            return (
                False,
                f"confidence_payload_deserialize_failed:"
                f"{type(exc).__name__}",
            )

        # Hash-match check (defense in depth: the proposer's hash
        # must equal what we recompute on the round-tripped payload).
        recomputed_proposed = proposed_policy.state_hash()
        if recomputed_proposed != proposal.proposed_state_hash:
            return (
                False,
                f"confidence_payload_hash_mismatch:proposed "
                f"{recomputed_proposed[:24]} != "
                f"{proposal.proposed_state_hash[:24]}",
            )
        recomputed_current = current_policy.state_hash()
        if recomputed_current != proposal.current_state_hash:
            return (
                False,
                f"confidence_payload_hash_mismatch:current "
                f"{recomputed_current[:24]} != "
                f"{proposal.current_state_hash[:24]}",
            )

        # 8. Substrate decision (APPLIED + non-empty kinds)
        # Force-enable the master flag for this evaluation: the
        # validator is the cage entry point — gating it on the
        # operator-facing master would create a path where the
        # master being off lets proposals through unvalidated.
        # The Slice 4 router is the master-flag gate for the
        # write surface; here we run the predicate unconditionally.
        diff: PolicyDiff = compute_policy_diff(
            current=current_policy,
            proposed=proposed_policy,
            enabled_override=True,
        )
        if diff.outcome is not ConfidencePolicyOutcome.APPLIED:
            return (
                False,
                f"confidence_policy_diff_not_applied:"
                f"{diff.outcome.value} ({diff.detail[:120]})",
            )
        if not diff.kinds:
            return (
                False,
                "confidence_policy_diff_no_op_proposal_rejected",
            )

        # Cross-check: claimed kind in the proposal MUST match the
        # diff outcome. Single-kind proposals must equal exactly;
        # multi-dim proposals must move >1 dimension.
        if proposal.proposal_kind == _PROPOSAL_KIND_MULTI_DIM:
            if len(diff.kinds) < 2:
                return (
                    False,
                    f"confidence_multi_dim_only_one_kind_moved:"
                    f"{[k.value for k in diff.kinds]}",
                )
        else:
            if len(diff.kinds) != 1:
                return (
                    False,
                    f"confidence_single_kind_proposal_moved_many:"
                    f"{[k.value for k in diff.kinds]}",
                )
            if diff.kinds[0].value != proposal.proposal_kind:
                return (
                    False,
                    f"confidence_kind_mismatch:claimed="
                    f"{proposal.proposal_kind} "
                    f"actual={diff.kinds[0].value}",
                )

        return (True, "confidence_threshold_validator_ok")
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[ConfidenceThresholdTightener] validator raised: %s",
            exc,
        )
        return (
            False,
            f"validator_internal_error:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Helper for Slice 4's HTTP submission path (and for tests)
# ---------------------------------------------------------------------------


def build_proposed_state_payload(
    *,
    current: ConfidencePolicy,
    proposed: ConfidencePolicy,
) -> dict:
    """Compose the ``proposed_state_payload`` shape that the
    validator expects: ``{"current": <policy_dict>, "proposed":
    <policy_dict>}``. Pure function; NEVER raises."""
    try:
        return {
            "current": current.to_dict(),
            "proposed": proposed.to_dict(),
        }
    except Exception:  # noqa: BLE001 — defensive
        # Last-resort: return a structurally valid empty shape so
        # the validator's payload-presence check fires cleanly.
        return {"current": {}, "proposed": {}}


def classify_proposal_kind(diff: PolicyDiff) -> Optional[str]:
    """Map a ``PolicyDiff`` to the ``proposal_kind`` string the
    validator expects:

      * Empty kinds → ``None`` (no-op proposals are rejected at
        validator step 8; caller should not submit).
      * Single moved kind → that ``ConfidencePolicyKind.value``.
      * Multiple moved kinds → ``_PROPOSAL_KIND_MULTI_DIM``.

    Pure function; NEVER raises."""
    try:
        if not diff.kinds:
            return None
        if len(diff.kinds) == 1:
            return diff.kinds[0].value
        return _PROPOSAL_KIND_MULTI_DIM
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# Auto-registration at module import (mirror of Pass C Slices 2-5)
# ---------------------------------------------------------------------------


def install_surface_validator() -> None:
    """Idempotent: registers ``_confidence_threshold_validator``
    against ``AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS``.
    Called unconditionally at module import — see module docstring
    for why default-on is the correct discipline for this surface."""
    register_surface_validator(
        AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
        _confidence_threshold_validator,
    )


install_surface_validator()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_THRESHOLD_TIGHTENER_SCHEMA_VERSION",
    "build_proposed_state_payload",
    "classify_proposal_kind",
    "confidence_threshold_observation_count_floor",
    "install_surface_validator",
]

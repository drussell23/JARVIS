"""Move 6 Slice 1 — Generative Quorum primitive.

Solves §28.5.2 v9 brutal review's two undefended Antivenom bypass
vectors in ONE architectural move via independent-roll consensus:

  * **Test-shape gaming** (vector #4) — model writes tests that
    pass without exercising the property. Three independent
    rolls all generating the same vacuous test (asserts on mock,
    no SUT exercise) is exponentially unlikely.
  * **Quine-class hallucination** (vector #5) — generated code
    that includes its own validators. Three independent rolls
    converging on the *same* hallucinated structure is
    vanishingly unlikely.

The mechanism: K=3 candidate generations from same prompt with
seed variation for true independence. AST-normalized signature
(Slice 2) for each candidate's diff. Accept only candidates with
semantic agreement.

Slice 1 ships the **primitive layer only** — pure data + pure
compute. No async, no I/O, no LLM calls. The runner (Slice 3),
AST signature canonicalizer (Slice 2), risk-tier gate (Slice 4),
and graduation (Slice 5) build on top.

Direct-solve principles (per the operator directive):

  * **Asynchronous-ready** — pure-data primitives serialize
    cleanly across async boundaries. Frozen dataclasses safe to
    propagate through Slice 3's parallel runner.

  * **Dynamic** — K + agreement threshold both env-tunable
    (with floor + ceiling). Tier-threshold knob env-driven.

  * **Adaptive** — consensus math degrades gracefully as more
    rolls arrive. Single roll → FAILED (insufficient input).
    Two rolls agreeing → CONSENSUS. Mixed → MAJORITY or
    DISAGREEMENT based on quorum.

  * **Intelligent** — group rolls by ast_signature; largest
    cluster wins; canonical answer = first roll in cluster
    (deterministic via input order). Mirrors Move 5's drift-
    signature ring discipline.

  * **Robust** — ``compute_consensus`` is total: every input
    maps to exactly one ``ConsensusVerdict``. Empty rolls,
    malformed rolls, single roll, K-1 agreement at every K —
    all defined outcomes. Never raises.

  * **No hardcoding** — ``ConsensusOutcome`` is a closed 5-value
    taxonomy. Quorum + K are caller-supplied with sensible
    defaults. No magic constants in behavior logic.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib ONLY — no governance imports yet (Slices
    2-5 add ast_canonical + candidate_generator + cost_contract_
    assertion as needed; Slice 1 is pure-data).
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor.
  * Never raises out of any public function.
  * No mutation tools imported anywhere — read-only contract
    (enforced by Slice 5 graduation pin).

Master flag default-TRUE post Q4 Priority #1 graduation
(2026-05-02): ``JARVIS_GENERATIVE_QUORUM_ENABLED``. Asymmetric env
semantics: empty/whitespace = unset = current default; explicit
truthy/falsy overrides at call time.

Cost contract preserved by construction — see
:func:`quorum_enabled` for the three downstream gates that bound
the K× amplification (sub-gate / risk-tier filter / route filter).
"""
from __future__ import annotations

import enum
import logging
import os
from collections import Counter
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
)

logger = logging.getLogger(__name__)


GENERATIVE_QUORUM_SCHEMA_VERSION: str = "generative_quorum.1"


# ---------------------------------------------------------------------------
# Env knobs — defaults overridable, never hardcoded behavior constants
# ---------------------------------------------------------------------------


_DEFAULT_K: int = 3
_K_FLOOR: int = 2
_K_CEILING: int = 5

_DEFAULT_AGREEMENT_THRESHOLD: int = 2
_AGREEMENT_THRESHOLD_FLOOR: int = 2


def quorum_enabled() -> bool:
    """``JARVIS_GENERATIVE_QUORUM_ENABLED`` (default ``true`` —
    operator-authorized graduation per Q4 Priority #1, 2026-05-02).

    Asymmetric env semantics: empty/whitespace = unset = current
    default; explicit ``0`` / ``false`` / ``no`` / ``off``
    evaluates false; explicit truthy values evaluate true.
    Re-read on every call so flips hot-revert without restart.

    Cost contract preserved by construction: master-on does NOT
    mean every op runs Quorum. The K× generation cost is gated by
    three downstream checks in :func:`should_invoke_quorum`:

      1. ``JARVIS_QUORUM_GATE_ENABLED`` sub-gate (operator's
         emergency kill switch — flips off without disabling the
         whole subsystem)
      2. Risk-tier filter — Quorum invokes only on
         ``APPROVAL_REQUIRED+`` ops (operator-review-required tier)
      3. Route filter — ``COST_GATED_ROUTES`` excludes
         ``BACKGROUND`` / ``SPECULATIVE`` routes structurally

    K=3 default (clamped [2, 5]) bounds amplification. Operators
    can flip the master back to false (or set the sub-gate false)
    for instant rollback — both are re-read on every call."""
    raw = os.environ.get(
        "JARVIS_GENERATIVE_QUORUM_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default 2026-05-02
    return raw in ("1", "true", "yes", "on")


def quorum_k() -> int:
    """``JARVIS_QUORUM_K`` (default 3, floor 2, ceiling 5).

    Number of candidate rolls per quorum. Cap structure:
    ``min(ceiling, max(floor, value))`` so operators cannot
    loosen below structural floor (single-candidate is not a
    quorum — defeats the purpose) or exceed ceiling (cost
    amplification cap)."""
    raw = os.environ.get("JARVIS_QUORUM_K", "").strip()
    if not raw:
        return _DEFAULT_K
    try:
        v = int(raw)
        return min(_K_CEILING, max(_K_FLOOR, v))
    except (TypeError, ValueError):
        return _DEFAULT_K


def agreement_threshold() -> int:
    """``JARVIS_QUORUM_AGREEMENT_THRESHOLD`` (default 2, floor 2).

    Minimum cluster size required to declare MAJORITY_CONSENSUS.
    Floor 2 because single-roll agreement is meaningless —
    consensus requires at least two rolls to align."""
    raw = os.environ.get(
        "JARVIS_QUORUM_AGREEMENT_THRESHOLD", "",
    ).strip()
    if not raw:
        return _DEFAULT_AGREEMENT_THRESHOLD
    try:
        return max(_AGREEMENT_THRESHOLD_FLOOR, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_AGREEMENT_THRESHOLD


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of consensus outcomes (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class ConsensusOutcome(str, enum.Enum):
    """Closed 5-value taxonomy of generative-quorum outcomes.
    Every input maps to exactly one — never None, never implicit
    fall-through. Mirrors Move 3 ``AdvisoryActionType`` / Move 4
    ``BootSnapshotOutcome`` / Move 5 ``ProbeOutcome`` discipline.

    ``CONSENSUS``           — All K rolls agree on canonical
                              signature. Accept any roll from the
                              cluster; downstream APPLY proceeds.
    ``MAJORITY_CONSENSUS``  — ``agreement_count`` >=
                              ``agreement_threshold`` but NOT
                              unanimous (one or more outliers).
                              Caller (Slice 4) routes to
                              operator review (raise risk_tier
                              to NOTIFY_APPLY).
    ``DISAGREEMENT``        — No cluster meets the threshold.
                              Caller routes to BLOCKED tier
                              (escalate via existing path).
    ``DISABLED``            — Master flag off OR quorum
                              short-circuited at gate. No rolls
                              were executed.
    ``FAILED``              — Defensive sentinel. Insufficient
                              input (< 2 rolls), all-empty
                              signatures, or runner exception.
                              Caller falls through to single-
                              candidate path (no behavior change
                              from pre-Quorum baseline)."""

    CONSENSUS = "consensus"
    MAJORITY_CONSENSUS = "majority_consensus"
    DISAGREEMENT = "disagreement"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Frozen dataclasses — propagation-safe across async + lock boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRoll:
    """One candidate generation roll's output. Frozen so
    propagation through Slice 3's parallel runner is safe.

    ``candidate_diff`` is the actual source/diff produced by the
    roll. ``ast_signature`` is the canonical sha256 hash (Slice 2
    computes; Slice 1 stores). ``seed`` is the provider seed used
    for this roll (production callers pass distinct seeds for true
    independence; tests pass deterministic seeds for reproducible
    fixtures).

    ``cost_estimate_usd`` is the cost incurred by this roll —
    runner aggregates across K rolls for cost-budget tracking."""

    roll_id: str
    candidate_diff: str
    ast_signature: str
    cost_estimate_usd: float = 0.0
    seed: Optional[int] = None
    schema_version: str = GENERATIVE_QUORUM_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "roll_id": self.roll_id,
            "candidate_diff": self.candidate_diff,
            "ast_signature": self.ast_signature,
            "cost_estimate_usd": self.cost_estimate_usd,
            "seed": self.seed,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["CandidateRoll"]:
        """Reconstruct from a ``to_dict`` payload. Returns ``None``
        on schema mismatch OR malformed shape. NEVER raises."""
        try:
            schema = payload.get("schema_version")
            if schema != GENERATIVE_QUORUM_SCHEMA_VERSION:
                return None
            seed_raw = payload.get("seed")
            seed = (
                int(seed_raw) if seed_raw is not None else None
            )
            return cls(
                roll_id=str(payload["roll_id"]),
                candidate_diff=str(payload["candidate_diff"]),
                ast_signature=str(payload["ast_signature"]),
                cost_estimate_usd=float(
                    payload.get("cost_estimate_usd", 0.0),
                ),
                seed=seed,
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class ConsensusVerdict:
    """Outcome of one ``compute_consensus`` call. Frozen for safe
    propagation across async + lock boundaries."""

    outcome: ConsensusOutcome
    agreement_count: int
    distinct_count: int
    total_rolls: int
    canonical_signature: Optional[str]
    accepted_roll_id: Optional[str]
    detail: str
    schema_version: str = GENERATIVE_QUORUM_SCHEMA_VERSION

    def is_actionable(self) -> bool:
        """True iff outcome is CONSENSUS or MAJORITY_CONSENSUS —
        the two outcomes that map to definitive action in Slice 4
        (accept the canonical roll). DISAGREEMENT / DISABLED /
        FAILED are non-definitive."""
        return self.outcome in (
            ConsensusOutcome.CONSENSUS,
            ConsensusOutcome.MAJORITY_CONSENSUS,
        )

    def is_unanimous(self) -> bool:
        """True iff outcome is CONSENSUS (all K rolls agreed).
        Distinct from ``is_actionable``: MAJORITY_CONSENSUS is
        actionable but not unanimous."""
        return self.outcome is ConsensusOutcome.CONSENSUS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "agreement_count": self.agreement_count,
            "distinct_count": self.distinct_count,
            "total_rolls": self.total_rolls,
            "canonical_signature": self.canonical_signature,
            "accepted_roll_id": self.accepted_roll_id,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Consensus math — pure, total, never raises
# ---------------------------------------------------------------------------


def compute_consensus(
    rolls: Iterable[CandidateRoll],
    *,
    threshold: Optional[int] = None,
) -> ConsensusVerdict:
    """Pure decision function over a sequence of CandidateRoll.
    Returns the ConsensusVerdict. NEVER raises.

    Decision tree (every input maps to exactly one outcome):

      1. ``rolls`` is None or non-iterable → ``FAILED``.
      2. After filtering to valid CandidateRoll instances, fewer
         than 2 remain → ``FAILED`` (insufficient input — single
         roll cannot establish consensus).
      3. After filtering to non-empty signatures, all are empty
         → ``DISAGREEMENT`` (no usable signal — Slice 2 returns
         empty fingerprint on syntax errors; convergence detector
         treats as no-signal).
      4. Group by ``ast_signature``; compute ``largest_cluster``
         (size of largest agreeing cluster) and ``distinct_count``
         (number of unique signatures).
      5. ``largest_cluster == total_rolls`` → ``CONSENSUS``
         (unanimous agreement) with cluster's signature as
         canonical.
      6. ``largest_cluster >= effective_threshold`` →
         ``MAJORITY_CONSENSUS`` (quorum hit but not unanimous).
      7. Otherwise → ``DISAGREEMENT`` (no quorum reached).

    ``threshold`` defaults to ``agreement_threshold()`` env knob.
    Tests pass explicit values; production callers usually pass
    None."""
    try:
        # Step 1: defensive iterable handling
        if rolls is None:
            return ConsensusVerdict(
                outcome=ConsensusOutcome.FAILED,
                agreement_count=0,
                distinct_count=0,
                total_rolls=0,
                canonical_signature=None,
                accepted_roll_id=None,
                detail="rolls argument was None",
            )

        roll_list: List[CandidateRoll] = []
        try:
            for r in rolls:
                if isinstance(r, CandidateRoll):
                    roll_list.append(r)
        except TypeError:
            # Not iterable
            return ConsensusVerdict(
                outcome=ConsensusOutcome.FAILED,
                agreement_count=0,
                distinct_count=0,
                total_rolls=0,
                canonical_signature=None,
                accepted_roll_id=None,
                detail="rolls argument was not iterable",
            )

        total = len(roll_list)

        effective_threshold = (
            int(threshold) if threshold is not None and threshold >= 1
            else agreement_threshold()
        )
        # Defensive floor — even if caller passes 0 or 1, force >= 2
        effective_threshold = max(
            _AGREEMENT_THRESHOLD_FLOOR, effective_threshold,
        )

        # Step 2: insufficient input
        if total < 2:
            return ConsensusVerdict(
                outcome=ConsensusOutcome.FAILED,
                agreement_count=0,
                distinct_count=0,
                total_rolls=total,
                canonical_signature=None,
                accepted_roll_id=None,
                detail=(
                    f"insufficient rolls: need >= 2 for consensus, "
                    f"got {total}"
                ),
            )

        # Step 3: filter empty signatures (Slice 2 returns empty
        # on syntax error — treat as no-signal)
        non_empty = [r for r in roll_list if r.ast_signature]

        if not non_empty:
            return ConsensusVerdict(
                outcome=ConsensusOutcome.DISAGREEMENT,
                agreement_count=0,
                distinct_count=0,
                total_rolls=total,
                canonical_signature=None,
                accepted_roll_id=None,
                detail=(
                    f"all {total} rolls had empty signatures "
                    "(syntax errors / unparseable)"
                ),
            )

        # Step 4: group by signature, find largest cluster
        sig_counter: Counter = Counter(
            r.ast_signature for r in non_empty
        )
        largest_signature, largest_cluster = (
            sig_counter.most_common(1)[0]
        )
        distinct_count = len(sig_counter)

        # Find first roll in largest cluster (deterministic via
        # input order)
        accepted_roll: Optional[CandidateRoll] = None
        for r in non_empty:
            if r.ast_signature == largest_signature:
                accepted_roll = r
                break

        accepted_roll_id = (
            accepted_roll.roll_id if accepted_roll is not None
            else None
        )

        # Step 5: unanimous (all rolls — including empty-signature
        # ones — agree, OR all non-empty rolls agree AND no
        # empty-signature rolls exist)
        if largest_cluster == total:
            return ConsensusVerdict(
                outcome=ConsensusOutcome.CONSENSUS,
                agreement_count=largest_cluster,
                distinct_count=distinct_count,
                total_rolls=total,
                canonical_signature=largest_signature,
                accepted_roll_id=accepted_roll_id,
                detail=(
                    f"unanimous: all {total} rolls produced "
                    f"identical AST signature"
                ),
            )

        # Step 6: majority quorum
        if largest_cluster >= effective_threshold:
            return ConsensusVerdict(
                outcome=ConsensusOutcome.MAJORITY_CONSENSUS,
                agreement_count=largest_cluster,
                distinct_count=distinct_count,
                total_rolls=total,
                canonical_signature=largest_signature,
                accepted_roll_id=accepted_roll_id,
                detail=(
                    f"majority: {largest_cluster} of {total} "
                    f"rolls agree (threshold "
                    f"{effective_threshold}); "
                    f"{distinct_count} distinct signatures"
                ),
            )

        # Step 7: disagreement
        return ConsensusVerdict(
            outcome=ConsensusOutcome.DISAGREEMENT,
            agreement_count=largest_cluster,
            distinct_count=distinct_count,
            total_rolls=total,
            canonical_signature=None,
            accepted_roll_id=None,
            detail=(
                f"no quorum: largest cluster {largest_cluster} < "
                f"threshold {effective_threshold}; "
                f"{distinct_count} distinct signatures across "
                f"{total} rolls"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[GenerativeQuorum] compute_consensus raised: %s", exc,
        )
        return ConsensusVerdict(
            outcome=ConsensusOutcome.FAILED,
            agreement_count=0,
            distinct_count=0,
            total_rolls=0,
            canonical_signature=None,
            accepted_roll_id=None,
            detail=f"compute_consensus raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CandidateRoll",
    "ConsensusOutcome",
    "ConsensusVerdict",
    "GENERATIVE_QUORUM_SCHEMA_VERSION",
    "agreement_threshold",
    "compute_consensus",
    "quorum_enabled",
    "quorum_k",
]

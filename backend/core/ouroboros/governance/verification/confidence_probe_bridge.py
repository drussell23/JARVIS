"""Move 5 Slice 1 — Confidence-Aware Probe Loop bridge primitive.

Bridges Phase 7.6's bounded ``hypothesis_probe`` primitive (which
already enforces three independent termination guarantees:
``MAX_CALLS_PER_PROBE`` + monotonic-clock wall-clock cap + sha256
diminishing-returns) into the confidence-collapse pipeline as a
**4th outcome** (``ConfidenceCollapseAction.PROBE_ENVIRONMENT``)
shipped in Slice 4.

§28.3 v9 brutal review found the cognitive gap explicitly:

  * O+V's Venom does multi-turn tools but has **no inner reasoning
    between tool rounds**. CC's Extended Thinking has no analog.
  * 3-action ``ConfidenceCollapseAction`` enum offers only
    RETRY_WITH_FEEDBACK / ESCALATE_TO_OPERATOR / INCONCLUSIVE.
    No autonomous "I'm uncertain, let me probe the codebase to
    disambiguate" outcome.
  * ``ask_human`` is the only escape valve when ambiguity bites,
    which violates the "proactive autonomous opposite of CC"
    operator-binding mandate.

Slice 1 ships the **primitive layer only** — pure data + pure
compare functions. No async, no I/O, no LLM calls. The probe
runner (Slice 3), question generator (Slice 2), and
ConfidenceCollapseAction wire-up (Slice 4) build on top.

Design pillars (per the operator directive):

  * **Asynchronous-ready** — pure-data primitives serialize
    cleanly across async boundaries. Frozen dataclasses are
    safe to propagate through Slice 3's async runner.

  * **Dynamic** — convergence quorum + max-probes both env-
    tunable (with floors + ceilings). The compute_convergence
    function takes parameters; Slice 3's runner reads env knobs.

  * **Adaptive** — the convergence math degrades gracefully as
    more answers arrive. Single answer → EXHAUSTED (need more
    data). Quorum hit → CONVERGED (early-stop). All distinct +
    budget hit → DIVERGED (escalate).

  * **Intelligent** — sha256 canonical-fingerprint dedup mirrors
    Move 4 InvariantDriftObserver's drift-signature ring exactly.
    Two answers with semantically equivalent text (whitespace /
    case differences) get the same fingerprint.

  * **Robust** — ``compute_convergence`` is total: every input
    maps to exactly one ``ConvergenceVerdict``. Empty answers,
    malformed answers, single answer, K-1 agreement at every K
    — all defined outcomes. Never raises.

  * **No hardcoding** — ``ProbeOutcome`` is a closed 5-value
    taxonomy enum. Quorum + max-probes are caller-supplied
    parameters with sensible defaults. No magic constants in
    behavior logic.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + ``adaptation.hypothesis_probe`` (Phase 7.6
    primitive — for ``ProbeVerdict`` import only) +
    ``verification.confidence_monitor`` (``ConfidenceVerdict``
    enum) ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor.
  * Never raises out of any public function.
  * No mutation tools imported anywhere — read-only contract
    (enforced by Slice 5 graduation pin).

Master flag default-false until Slice 5 graduation:
``JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED``. Asymmetric env
semantics: empty/whitespace = unset = current default; explicit
truthy/falsy overrides at call time.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION: str = (
    "confidence_probe_bridge.1"
)


# ---------------------------------------------------------------------------
# Env knobs — defaults overridable, never hardcoded behavior constants
# ---------------------------------------------------------------------------


_DEFAULT_MAX_QUESTIONS: int = 3
_MAX_QUESTIONS_FLOOR: int = 2
_MAX_QUESTIONS_CEILING: int = 5

_DEFAULT_CONVERGENCE_QUORUM: int = 2
_CONVERGENCE_QUORUM_FLOOR: int = 2

_DEFAULT_MAX_TOOL_ROUNDS: int = 5
_MAX_TOOL_ROUNDS_FLOOR: int = 1
_MAX_TOOL_ROUNDS_CEILING: int = 10


def bridge_enabled() -> bool:
    """``JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED`` (**graduated
    2026-05-01 Slice 5 — default ``true``**).

    Asymmetric env semantics: empty/whitespace = unset = current
    default (post-graduation = ``true``); explicit ``0`` /
    ``false`` / ``no`` / ``off`` hot-reverts. Re-read on every
    call so flips take effect without restart."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default — Slice 5
    return raw in ("1", "true", "yes", "on")


def max_questions() -> int:
    """``JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS`` (default 3, floor 2,
    ceiling 5). Number of probe questions to generate per
    ambiguity. Cap structure: ``min(ceiling, max(floor, value))``
    so operators cannot loosen below structural floor."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS", "",
    ).strip()
    if not raw:
        return _DEFAULT_MAX_QUESTIONS
    try:
        v = int(raw)
        return min(_MAX_QUESTIONS_CEILING, max(_MAX_QUESTIONS_FLOOR, v))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_QUESTIONS


def convergence_quorum() -> int:
    """``JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM`` (default 2,
    floor 2). Number of agreeing answers required to declare
    CONVERGED. Floor 2 because a single agreement is meaningless —
    convergence requires at least two probes to align."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM", "",
    ).strip()
    if not raw:
        return _DEFAULT_CONVERGENCE_QUORUM
    try:
        return max(_CONVERGENCE_QUORUM_FLOOR, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_CONVERGENCE_QUORUM


def max_tool_rounds_per_question() -> int:
    """``JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS`` (default 5,
    floor 1, ceiling 10). Maximum tool calls per single probe
    question. Composes with Phase 7.6's MAX_CALLS_PER_PROBE — the
    runner uses min(this, Phase 7.6 cap) as the effective limit."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS", "",
    ).strip()
    if not raw:
        return _DEFAULT_MAX_TOOL_ROUNDS
    try:
        v = int(raw)
        return min(
            _MAX_TOOL_ROUNDS_CEILING,
            max(_MAX_TOOL_ROUNDS_FLOOR, v),
        )
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOOL_ROUNDS


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of probe outcomes (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class ProbeOutcome(str, enum.Enum):
    """Closed 5-value taxonomy of confidence-probe-loop outcomes.
    Every input maps to exactly one — never None, never implicit
    fall-through. Mirrors Move 3 ``AdvisoryActionType`` / Move 4
    ``BootSnapshotOutcome`` / Tier 1 #1 ``FireDecision`` /
    Tier 1 #2 ``PostureHealthStatus`` discipline.

    ``CONVERGED``  — K-1+ probes agree on canonical answer.
                     Confidence elevated; original op proceeds.
    ``DIVERGED``   — All K probes return distinct answers within
                     budget. Caller routes to ESCALATE_TO_OPERATOR.
    ``EXHAUSTED``  — Probes consumed budget without hitting quorum
                     (partial agreement only). Caller decides:
                     Slice 4's wire-up routes EXHAUSTED to
                     RETRY_WITH_FEEDBACK (one retry then escalate).
    ``DISABLED``   — Master flag off. No probe loop ran.
    ``FAILED``     — Defensive sentinel. Probe runner raised an
                     unhandled exception. Caller logs + falls
                     through to existing safe default."""

    CONVERGED = "converged"
    DIVERGED = "diverged"
    EXHAUSTED = "exhausted"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Frozen dataclasses — propagation-safe across async + lock boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeQuestion:
    """One disambiguation question. Frozen so propagation through
    Slice 3's async runner is safe.

    ``resolution_method`` is a hint to the prober (Slice 2)
    indicating which read-only tool best resolves the question.
    Examples: ``"read_file"`` / ``"search_code"`` /
    ``"get_callers"`` / ``"list_symbols"``. The prober is free
    to use any read-only tool from the allowlist; this field is
    advisory only."""

    question: str
    resolution_method: str = ""
    max_tool_rounds: int = 0  # 0 → use env default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "resolution_method": self.resolution_method,
            "max_tool_rounds": self.max_tool_rounds,
        }


@dataclass(frozen=True)
class ProbeAnswer:
    """One resolved answer. Frozen for safe propagation.

    ``evidence_fingerprint`` is the canonical sha256 of the
    answer text (after whitespace + case normalization), used
    by ``compute_convergence`` to detect agreement across probes.
    Two answers with semantically equivalent text get the same
    fingerprint."""

    question: str
    answer_text: str
    evidence_fingerprint: str
    tool_rounds_used: int = 0
    schema_version: str = CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "answer_text": self.answer_text,
            "evidence_fingerprint": self.evidence_fingerprint,
            "tool_rounds_used": self.tool_rounds_used,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ConvergenceVerdict:
    """Outcome of one ``compute_convergence`` call. Frozen for
    safe propagation."""

    outcome: ProbeOutcome
    agreement_count: int
    distinct_count: int
    total_answers: int
    canonical_answer: Optional[str]
    canonical_fingerprint: Optional[str]
    detail: str
    schema_version: str = CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION

    def is_actionable(self) -> bool:
        """True iff outcome is CONVERGED or DIVERGED — the two
        outcomes that map to definitive action in Slice 4. EXHAUSTED
        / DISABLED / FAILED are non-definitive."""
        return self.outcome in (
            ProbeOutcome.CONVERGED,
            ProbeOutcome.DIVERGED,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "agreement_count": self.agreement_count,
            "distinct_count": self.distinct_count,
            "total_answers": self.total_answers,
            "canonical_answer": self.canonical_answer,
            "canonical_fingerprint": self.canonical_fingerprint,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Canonical-fingerprint computation — shared between probe runners
# ---------------------------------------------------------------------------


_WHITESPACE_RE = re.compile(r"\s+")


def canonical_fingerprint(answer_text: str) -> str:
    """Compute the canonical sha256 fingerprint of an answer. Two
    answers with semantically equivalent text (whitespace + case
    differences only) get the same fingerprint. NEVER raises —
    on any input failure returns the empty string (which behaves
    as "unique" against other fingerprints, defensively)."""
    try:
        if not isinstance(answer_text, str):
            answer_text = str(answer_text)
        # Normalize: lowercase + collapse whitespace + strip
        normalized = _WHITESPACE_RE.sub(" ", answer_text.strip().lower())
        if not normalized:
            return ""  # empty answer — defensive
        return hashlib.sha256(
            normalized.encode("utf-8"),
        ).hexdigest()
    except Exception:  # noqa: BLE001 — defensive
        return ""


def make_probe_answer(
    question: str,
    answer_text: str,
    *,
    tool_rounds_used: int = 0,
) -> ProbeAnswer:
    """Convenience constructor that auto-computes the canonical
    fingerprint. Slice 2's prober uses this; Slice 3's runner uses
    this. NEVER raises."""
    try:
        return ProbeAnswer(
            question=str(question) if question else "",
            answer_text=str(answer_text) if answer_text else "",
            evidence_fingerprint=canonical_fingerprint(
                answer_text or "",
            ),
            tool_rounds_used=int(tool_rounds_used),
        )
    except Exception:  # noqa: BLE001 — defensive
        return ProbeAnswer(
            question="",
            answer_text="",
            evidence_fingerprint="",
            tool_rounds_used=0,
        )


# ---------------------------------------------------------------------------
# Convergence math — pure, total, never raises
# ---------------------------------------------------------------------------


def compute_convergence(
    answers: Iterable[ProbeAnswer],
    *,
    quorum: Optional[int] = None,
    max_probes: Optional[int] = None,
) -> ConvergenceVerdict:
    """Pure decision function over a sequence of ProbeAnswer.
    Returns the ConvergenceVerdict. NEVER raises.

    Decision tree (every input maps to exactly one outcome):

      1. ``answers`` is empty → ``EXHAUSTED`` ("no answers
         gathered").
      2. Group answers by ``evidence_fingerprint``. Compute
         ``largest_cluster`` (size of largest agreeing cluster)
         and ``distinct_count`` (number of unique fingerprints).
      3. If ``largest_cluster >= effective_quorum`` →
         ``CONVERGED`` with the cluster's answer text as canonical.
      4. If ``len(answers) >= effective_max_probes`` AND
         ``distinct_count == len(answers)`` (all distinct) →
         ``DIVERGED``.
      5. If ``len(answers) >= effective_max_probes`` AND
         ``largest_cluster < effective_quorum`` →
         ``EXHAUSTED`` (partial agreement, budget consumed).
      6. Else → ``EXHAUSTED`` (caller should keep probing).

    ``quorum`` and ``max_probes`` default to the env-tunable values
    (``convergence_quorum()`` and ``max_questions()``). Tests pass
    explicit values; production callers usually pass None."""
    try:
        # Materialize iterable so we can take len()
        answer_list: List[ProbeAnswer] = []
        for a in answers:
            if isinstance(a, ProbeAnswer):
                answer_list.append(a)
        total = len(answer_list)

        effective_quorum = (
            int(quorum) if quorum is not None and quorum >= 1
            else convergence_quorum()
        )
        effective_quorum = max(1, effective_quorum)
        effective_max = (
            int(max_probes) if max_probes is not None and max_probes >= 1
            else max_questions()
        )
        effective_max = max(1, effective_max)

        if total == 0:
            return ConvergenceVerdict(
                outcome=ProbeOutcome.EXHAUSTED,
                agreement_count=0,
                distinct_count=0,
                total_answers=0,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail="no answers gathered",
            )

        # Group by fingerprint, ignoring empty-fingerprint answers
        # (treat them as singleton "unknowns" that never converge).
        non_empty = [
            a for a in answer_list
            if a.evidence_fingerprint
        ]
        fingerprint_counter: Counter = Counter(
            a.evidence_fingerprint for a in non_empty
        )

        if not fingerprint_counter:
            # All answers had empty fingerprints — no usable signal
            return ConvergenceVerdict(
                outcome=(
                    ProbeOutcome.EXHAUSTED
                    if total < effective_max
                    else ProbeOutcome.DIVERGED
                ),
                agreement_count=0,
                distinct_count=0,
                total_answers=total,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail=(
                    "all answers had empty fingerprints "
                    "(unparseable / blank)"
                ),
            )

        most_common_fp, largest_cluster = (
            fingerprint_counter.most_common(1)[0]
        )
        distinct_count = len(fingerprint_counter)

        # Find canonical answer text from largest cluster
        canonical_answer: Optional[str] = None
        for a in non_empty:
            if a.evidence_fingerprint == most_common_fp:
                canonical_answer = a.answer_text
                break

        # Step 3: CONVERGED
        if largest_cluster >= effective_quorum:
            return ConvergenceVerdict(
                outcome=ProbeOutcome.CONVERGED,
                agreement_count=largest_cluster,
                distinct_count=distinct_count,
                total_answers=total,
                canonical_answer=canonical_answer,
                canonical_fingerprint=most_common_fp,
                detail=(
                    f"{largest_cluster} of {total} probes agree "
                    f"(quorum {effective_quorum})"
                ),
            )

        # Steps 4-5: budget exhausted paths
        if total >= effective_max:
            if distinct_count == total:
                return ConvergenceVerdict(
                    outcome=ProbeOutcome.DIVERGED,
                    agreement_count=largest_cluster,
                    distinct_count=distinct_count,
                    total_answers=total,
                    canonical_answer=None,
                    canonical_fingerprint=None,
                    detail=(
                        f"all {total} probes returned distinct "
                        f"answers (budget {effective_max} consumed)"
                    ),
                )
            return ConvergenceVerdict(
                outcome=ProbeOutcome.EXHAUSTED,
                agreement_count=largest_cluster,
                distinct_count=distinct_count,
                total_answers=total,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail=(
                    f"budget {effective_max} consumed; largest "
                    f"cluster {largest_cluster} < quorum "
                    f"{effective_quorum} (partial agreement only)"
                ),
            )

        # Step 6: caller should keep probing
        return ConvergenceVerdict(
            outcome=ProbeOutcome.EXHAUSTED,
            agreement_count=largest_cluster,
            distinct_count=distinct_count,
            total_answers=total,
            canonical_answer=None,
            canonical_fingerprint=None,
            detail=(
                f"{total} of {effective_max} probes consumed; "
                f"largest cluster {largest_cluster} < quorum "
                f"{effective_quorum} — keep probing"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[ConfidenceProbeBridge] compute_convergence raised: "
            "%s", exc,
        )
        return ConvergenceVerdict(
            outcome=ProbeOutcome.FAILED,
            agreement_count=0,
            distinct_count=0,
            total_answers=0,
            canonical_answer=None,
            canonical_fingerprint=None,
            detail=f"compute_convergence raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Reconstruction helpers — for cross-process / cross-session replay
# ---------------------------------------------------------------------------


def probe_answer_from_dict(
    payload: Mapping[str, Any],
) -> Optional[ProbeAnswer]:
    """Inverse of ``ProbeAnswer.to_dict``. Returns None on any
    malformed shape. NEVER raises."""
    try:
        if payload.get("schema_version") != \
                CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION:
            return None
        return ProbeAnswer(
            question=str(payload["question"]),
            answer_text=str(payload["answer_text"]),
            evidence_fingerprint=str(
                payload["evidence_fingerprint"],
            ),
            tool_rounds_used=int(
                payload.get("tool_rounds_used", 0),
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION",
    "ConvergenceVerdict",
    "ProbeAnswer",
    "ProbeOutcome",
    "ProbeQuestion",
    "bridge_enabled",
    "canonical_fingerprint",
    "compute_convergence",
    "convergence_quorum",
    "make_probe_answer",
    "max_questions",
    "max_tool_rounds_per_question",
    "probe_answer_from_dict",
]

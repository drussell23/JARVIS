"""Priority #3 Slice 1 — Counterfactual Replay primitive.

The policy-evaluation primitive: replay every recorded session
WITH and WITHOUT a chosen policy override (PostmortemRecall /
Recurrence Boost / Move 6 Quorum / Coherence Observer / Gate
decision), measure the prevention delta empirically, produce
the recurrence-reduction baseline that Move 6 master flag
graduation requires.

Slice 1 ships the **primitive layer only** — pure data + pure
compute. No I/O, no async, no governance imports. Slice 2 adds
the phase_capture extension with policy-override injection;
Slice 3 the branch comparator; Slice 4 the history store + SSE
event publisher; Slice 5 graduation.

Closes the policy-evaluation gap identified in §29 (post-
Priority-#2 review):

  * Priority #1 detects behavioral drift.
  * Priority #2 prevents recurrence cross-session.
  * **Both are operational; both produce signals.**
  * **What's missing**: the ability to *measure* whether
    prevention actually reduces recurrence empirically.
    Today operators can only observe correlation: "after
    PostmortemRecall shipped, recurrences went down." That's
    not proof.

Priority #3 closes this with **counterfactual A/B replay**:
re-run every recorded session WITH and WITHOUT a chosen
policy override. Cached generation hashes from Phase 1's
``phase_capture`` mean ZERO LLM cost — the experiment is
purely deterministic.

Direct-solve principles (per the operator directive):

  * **Asynchronous-ready** — frozen dataclasses propagate
    cleanly across async boundaries (Slice 5's orchestrator
    will round-trip ``ReplayVerdict`` through ``asyncio.to_
    thread`` and SSE serialization).

  * **Dynamic** — every numeric threshold (verdict-tolerance
    tie-breaker / max replay seconds / max phases per branch)
    is env-tunable with floor + ceiling clamps. NO hardcoded
    magic constants in verdict logic.

  * **Adaptive** — degraded inputs (single-branch replay,
    missing phase_capture, mismatched hashes) all map to
    explicit ``ReplayOutcome`` values rather than raises.
    PARTIAL (one branch only) and DIVERGED (cached hash
    mismatch) are first-class outcomes — Slice 4 records them
    distinct from FAILED.

  * **Intelligent** — verdict comparison is multi-criteria
    with closed-taxonomy resolution: terminal_success →
    postmortem_count → verify_pass_rate → apply_outcome,
    each tier with explicit tolerance. Contradicting
    criteria → ``DIVERGED_NEUTRAL`` (neither unambiguously
    better) rather than arbitrary tiebreak.

  * **Robust** — every public function NEVER raises out.
    Garbage input → ``ReplayOutcome.FAILED`` /
    ``BranchVerdict.FAILED`` rather than exception. Pure-data
    primitive can be called from any context, sync or async.

  * **No hardcoding** — 5-value closed taxonomy enums
    (J.A.R.M.A.T.R.I.X. — every input maps to exactly one).
    Per-knob env helpers with floor + ceiling clamps mirror
    Move 5/6/Priority #1/#2 patterns.

  * **Replay is observational, never prescriptive** — Slice 1
    primitives produce verdicts but NEVER propose policy
    changes. Operators interpret the empirical reality and
    drive Phase C ``MetaAdaptationGovernor`` proposals.
    Replay's `MonotonicTighteningVerdict.PASSED` stamping
    happens in Slice 3 (the comparator) — Slice 1 stays pure
    data.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib ONLY. NEVER imports any governance module
    — strongest authority invariant. Slice 3+ may import
    ``adaptation.ledger.MonotonicTighteningVerdict``; Slice 1
    stays pure.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers
    / doubleword_provider / urgency_router / auto_action_
    router / subagent_scheduler / tool_executor /
    semantic_guardian / semantic_firewall / risk_engine.
  * No async (Slice 5 wraps via to_thread at orchestrator).
  * Read-only — never writes a file, never executes code.
  * No mutation tools.
  * **No exec/eval/compile** (mirrors Move 6 Slice 2 +
    Priority #1/#2 Slice 1 critical safety pin).

Master flag default-false until Slice 5 graduation:
``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED``. Asymmetric env
semantics — empty/whitespace = unset = current default;
explicit truthy/falsy overrides at call time.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


COUNTERFACTUAL_REPLAY_SCHEMA_VERSION: str = (
    "counterfactual_replay.1"
)


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


def counterfactual_replay_enabled() -> bool:
    """``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED`` (default ``true`` —
    graduated 2026-05-02 in Priority #3 Slice 5).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default; explicit ``0``/``false``/``no``/``off`` evaluates false;
    explicit truthy values evaluate true.
    Re-read on every call so flips hot-revert without restart.

    Graduated default-true matches Priority #1 + #2 discipline:
    replay is read-only over cached artifacts (zero LLM cost by
    AST-pinned construction; every verdict stamps
    MonotonicTighteningVerdict.PASSED — observational not
    prescriptive). Operator approval still required for any
    downstream flag-flip proposal via MetaAdaptationGovernor."""
    raw = os.environ.get(
        "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5, 2026-05-02)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric clamped (floor + ceiling)
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def replay_max_duration_seconds() -> float:
    """``JARVIS_REPLAY_MAX_DURATION_SECONDS`` (default 300.0,
    floor 30.0, ceiling 1800.0).

    Wall-clock cap on a single replay. Slice 2's engine
    enforces; Slice 1 returns the configured value for caller
    composition."""
    return _env_float_clamped(
        "JARVIS_REPLAY_MAX_DURATION_SECONDS",
        300.0, floor=30.0, ceiling=1800.0,
    )


def replay_max_phases_per_branch() -> int:
    """``JARVIS_REPLAY_MAX_PHASES_PER_BRANCH`` (default 50,
    floor 5, ceiling 500).

    Phase count cap per branch. Pathologically-long sessions
    are bounded by this — replay never iterates beyond."""
    return _env_int_clamped(
        "JARVIS_REPLAY_MAX_PHASES_PER_BRANCH",
        50, floor=5, ceiling=500,
    )


def replay_min_replays_for_baseline() -> int:
    """``JARVIS_REPLAY_MIN_REPLAYS_FOR_BASELINE`` (default 5,
    floor 1, ceiling 100).

    Minimum replay count before the recurrence-reduction
    aggregator (Slice 4) returns a meaningful percentage.
    Below this → INSUFFICIENT_DATA outcome on aggregation."""
    return _env_int_clamped(
        "JARVIS_REPLAY_MIN_REPLAYS_FOR_BASELINE",
        5, floor=1, ceiling=100,
    )


def verdict_tolerance_postmortem_count() -> int:
    """``JARVIS_REPLAY_VERDICT_TOLERANCE_POSTMORTEM`` (default 0,
    floor 0, ceiling 10).

    Tolerance for postmortem-count tiebreaking: branches
    differing by ≤tolerance on postmortem count are treated as
    equivalent on this dimension. Default 0 = strict equality."""
    return _env_int_clamped(
        "JARVIS_REPLAY_VERDICT_TOLERANCE_POSTMORTEM",
        0, floor=0, ceiling=10,
    )


def verdict_tolerance_verify_pass_pct() -> float:
    """``JARVIS_REPLAY_VERDICT_TOLERANCE_VERIFY_PCT`` (default
    1.0, floor 0.0, ceiling 50.0).

    Tolerance percentage for verify-pass-rate tiebreaking:
    branches whose verify pass-rate differ by ≤tolerance% are
    treated as equivalent on this dimension."""
    return _env_float_clamped(
        "JARVIS_REPLAY_VERDICT_TOLERANCE_VERIFY_PCT",
        1.0, floor=0.0, ceiling=50.0,
    )


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of replay outcomes (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class ReplayOutcome(str, enum.Enum):
    """5-value closed taxonomy of replay-call outcomes. Every
    ``compute_replay_outcome`` invocation returns exactly one —
    never None, never implicit fall-through.

    ``SUCCESS``  — both branches replayed cleanly; verdict
                   computed.
    ``PARTIAL``  — one branch replayed; the other failed at a
                   recoverable phase (cache miss / transient
                   read failure). Caller can still compare
                   the available branch but verdict is
                   asymmetric.
    ``DIVERGED`` — branches diverged structurally before
                   ``swap_at_phase`` (cached hash mismatch).
                   Replay non-deterministic for this session;
                   caller may investigate via Slice 5b GET
                   routes.
    ``DISABLED`` — master flag off; no replay performed.
    ``FAILED``   — defensive sentinel: corrupt phase_capture,
                   unknown swap target, garbage input."""

    SUCCESS = "success"
    PARTIAL = "partial"
    DIVERGED = "diverged"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of branch verdicts
# ---------------------------------------------------------------------------


class BranchVerdict(str, enum.Enum):
    """5-value closed taxonomy of branch comparison outcomes.

    ``EQUIVALENT``        — no measurable delta in terminal
                            outcome. Both branches succeeded
                            (or both failed) within tolerance
                            on every secondary criterion.
    ``DIVERGED_BETTER``   — original branch had better terminal
                            outcome. **Counterfactual would
                            have been worse** — original
                            policy is justified empirically.
    ``DIVERGED_WORSE``    — counterfactual branch had better
                            terminal outcome. **Original policy
                            was actively harmful** — flag
                            candidate for Phase C tightening
                            review (operator-driven, NOT
                            auto-flipped).
    ``DIVERGED_NEUTRAL``  — branches differ but neither is
                            unambiguously better. Multi-
                            criteria contradict (e.g., one
                            better on postmortem count, the
                            other better on verify rate).
    ``FAILED``            — defensive sentinel: garbage
                            input, corrupt branch snapshot."""

    EQUIVALENT = "equivalent"
    DIVERGED_BETTER = "diverged_better"
    DIVERGED_WORSE = "diverged_worse"
    DIVERGED_NEUTRAL = "diverged_neutral"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of decision-override kinds
# ---------------------------------------------------------------------------


class DecisionOverrideKind(str, enum.Enum):
    """5-value closed taxonomy of policy overrides supported
    by Priority #3. Slice 2's engine maps each kind to the
    corresponding phase swap target.

    ``GATE_DECISION``         — risk-tier gate verdict swap
                                (e.g., notify_apply →
                                approval_required)
    ``POSTMORTEM_INJECTION``  — Priority #2 Slice 3 enable/
                                disable
    ``RECURRENCE_BOOST``      — Priority #2 Slice 4 enable/
                                disable
    ``QUORUM_INVOCATION``     — Move 6 enable/disable
    ``COHERENCE_OBSERVER``    — Priority #1 enable/disable"""

    GATE_DECISION = "gate_decision"
    POSTMORTEM_INJECTION = "postmortem_injection"
    RECURRENCE_BOOST = "recurrence_boost"
    QUORUM_INVOCATION = "quorum_invocation"
    COHERENCE_OBSERVER = "coherence_observer"


# ---------------------------------------------------------------------------
# Frozen dataclasses — propagation-safe across async + lock boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayTarget:
    """Bounded query: which session to replay + what to swap at
    which phase. Frozen for safe propagation across orchestrator
    hooks.

    ``swap_at_phase`` is a free-form string (e.g., ``"GATE"``,
    ``"VALIDATE"``, ``"CONTEXT_EXPANSION"``) — Slice 2's engine
    validates against the recorded session's actual phases.
    Slice 1 is opaque to phase taxonomy.

    ``swap_decision_payload`` is a kind-specific structured
    payload (e.g., ``{"enabled": False}`` for boost on/off, or
    ``{"verdict": "approval_required"}`` for gate swap)."""

    session_id: str
    swap_at_phase: str
    swap_decision_kind: DecisionOverrideKind
    swap_decision_payload: Mapping[str, Any] = field(
        default_factory=dict,
    )
    max_replay_seconds: float = 300.0
    schema_version: str = COUNTERFACTUAL_REPLAY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "swap_at_phase": self.swap_at_phase,
            "swap_decision_kind": self.swap_decision_kind.value,
            "swap_decision_payload": dict(
                self.swap_decision_payload,
            ),
            "max_replay_seconds": self.max_replay_seconds,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["ReplayTarget"]:
        """Schema-tolerant reconstruction. Returns ``None`` on
        schema mismatch / malformed shape. NEVER raises."""
        try:
            if (
                payload.get("schema_version")
                != COUNTERFACTUAL_REPLAY_SCHEMA_VERSION
            ):
                return None
            kind = DecisionOverrideKind(
                payload["swap_decision_kind"],
            )
            return cls(
                session_id=str(payload["session_id"]),
                swap_at_phase=str(payload["swap_at_phase"]),
                swap_decision_kind=kind,
                swap_decision_payload=dict(
                    payload.get("swap_decision_payload", {}),
                ),
                max_replay_seconds=float(
                    payload.get("max_replay_seconds", 300.0),
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class BranchSnapshot:
    """Frozen result of replaying one branch (original or
    counterfactual). Slice 2's engine constructs these from
    real recorded session data; Slice 1 receives them as input
    for verdict comparison.

    Quality criteria for branch comparison (used by
    ``compute_branch_verdict``):

      1. ``terminal_success`` — primary axis (success vs failed)
      2. ``postmortem_records`` count — secondary, lower is
         better (recurrence indicator)
      3. ``verify_passed`` / ``verify_total`` — tertiary, higher
         pass rate is better
      4. ``apply_outcome`` — quaternary (``"single"`` or
         ``"multi"`` are both fine; ``"none"`` worse than
         success-with-apply)"""

    branch_id: str
    terminal_phase: str
    terminal_success: bool
    apply_outcome: str = ""  # "none" | "single" | "multi"
    verify_passed: int = 0
    verify_total: int = 0
    postmortem_records: Tuple[str, ...] = field(
        default_factory=tuple,
    )
    phase_results: Mapping[str, str] = field(
        default_factory=dict,
    )
    ops_summary: Mapping[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    schema_version: str = COUNTERFACTUAL_REPLAY_SCHEMA_VERSION

    def verify_pass_rate(self) -> float:
        """Fraction of verify tests passed in [0.0, 1.0].
        Returns 1.0 when ``verify_total == 0`` (no tests = no
        failures = full credit). NEVER raises."""
        try:
            if self.verify_total <= 0:
                return 1.0
            return min(
                1.0,
                max(0.0, self.verify_passed / self.verify_total),
            )
        except Exception:  # noqa: BLE001 — defensive
            return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "terminal_phase": self.terminal_phase,
            "terminal_success": self.terminal_success,
            "apply_outcome": self.apply_outcome,
            "verify_passed": self.verify_passed,
            "verify_total": self.verify_total,
            "postmortem_records": list(self.postmortem_records),
            "phase_results": dict(self.phase_results),
            "ops_summary": dict(self.ops_summary),
            "cost_usd": self.cost_usd,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["BranchSnapshot"]:
        """Schema-tolerant reconstruction. NEVER raises."""
        try:
            if (
                payload.get("schema_version")
                != COUNTERFACTUAL_REPLAY_SCHEMA_VERSION
            ):
                return None
            return cls(
                branch_id=str(payload["branch_id"]),
                terminal_phase=str(payload["terminal_phase"]),
                terminal_success=bool(
                    payload["terminal_success"],
                ),
                apply_outcome=str(
                    payload.get("apply_outcome", ""),
                ),
                verify_passed=int(
                    payload.get("verify_passed", 0),
                ),
                verify_total=int(
                    payload.get("verify_total", 0),
                ),
                postmortem_records=tuple(
                    str(r) for r in (
                        payload.get("postmortem_records") or []
                    )
                ),
                phase_results=dict(
                    payload.get("phase_results", {}),
                ),
                ops_summary=dict(payload.get("ops_summary", {})),
                cost_usd=float(payload.get("cost_usd", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class ReplayVerdict:
    """Aggregate result of one ``compute_replay_outcome``
    invocation. Frozen for safe propagation across Slice 4's
    history store + Slice 5's SSE serialization."""

    outcome: ReplayOutcome
    target: Optional[ReplayTarget] = None
    original_branch: Optional[BranchSnapshot] = None
    counterfactual_branch: Optional[BranchSnapshot] = None
    verdict: BranchVerdict = BranchVerdict.FAILED
    divergence_phase: str = ""
    divergence_reason: str = ""
    detail: str = ""
    schema_version: str = COUNTERFACTUAL_REPLAY_SCHEMA_VERSION

    def has_actionable_verdict(self) -> bool:
        """True iff outcome is SUCCESS AND verdict is not
        EQUIVALENT/FAILED. Used by Slice 4's recurrence-
        reduction aggregator to filter records that contribute
        to the empirical baseline."""
        return (
            self.outcome is ReplayOutcome.SUCCESS
            and self.verdict in (
                BranchVerdict.DIVERGED_BETTER,
                BranchVerdict.DIVERGED_WORSE,
                BranchVerdict.DIVERGED_NEUTRAL,
            )
        )

    def is_prevention_evidence(self) -> bool:
        """True iff the verdict is empirical evidence that the
        original policy prevented a worse outcome. Used by
        Slice 4's recurrence-reduction aggregator: numerator
        for the prevention-rate calculation.

        Specifically: ``DIVERGED_BETTER`` means original was
        better, i.e. *counterfactual would have been worse*,
        i.e. *original policy successfully prevented the worse
        case*."""
        return (
            self.outcome is ReplayOutcome.SUCCESS
            and self.verdict is BranchVerdict.DIVERGED_BETTER
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "target": (
                self.target.to_dict()
                if self.target is not None else None
            ),
            "original_branch": (
                self.original_branch.to_dict()
                if self.original_branch is not None else None
            ),
            "counterfactual_branch": (
                self.counterfactual_branch.to_dict()
                if self.counterfactual_branch is not None
                else None
            ),
            "verdict": self.verdict.value,
            "divergence_phase": self.divergence_phase,
            "divergence_reason": self.divergence_reason,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["ReplayVerdict"]:
        """Reconstruct from a JSON-shaped mapping. NEVER raises —
        returns None on schema-mismatch / missing required fields.

        Pairs with ``to_dict`` for round-trip persistence (Slice 4's
        history store + IDE GET endpoints). Tolerates schema drift:
        missing optional fields default; ReplayOutcome /
        BranchVerdict are looked up case-insensitively against
        their value vocabularies."""
        try:
            if not isinstance(raw, Mapping):
                return None
            if (
                raw.get("schema_version")
                != COUNTERFACTUAL_REPLAY_SCHEMA_VERSION
            ):
                return None
            outcome_raw = raw.get("outcome")
            verdict_raw = raw.get("verdict")
            if not isinstance(outcome_raw, str):
                return None
            if not isinstance(verdict_raw, str):
                return None
            try:
                outcome = ReplayOutcome(outcome_raw)
            except ValueError:
                return None
            try:
                verdict = BranchVerdict(verdict_raw)
            except ValueError:
                return None

            target = None
            target_raw = raw.get("target")
            if isinstance(target_raw, Mapping):
                target = ReplayTarget.from_dict(target_raw)

            original = None
            original_raw = raw.get("original_branch")
            if isinstance(original_raw, Mapping):
                original = BranchSnapshot.from_dict(original_raw)

            counterfactual = None
            cf_raw = raw.get("counterfactual_branch")
            if isinstance(cf_raw, Mapping):
                counterfactual = BranchSnapshot.from_dict(cf_raw)

            return cls(
                outcome=outcome,
                target=target,
                original_branch=original,
                counterfactual_branch=counterfactual,
                verdict=verdict,
                divergence_phase=str(raw.get("divergence_phase", "")),
                divergence_reason=str(raw.get("divergence_reason", "")),
                detail=str(raw.get("detail", "")),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# Internal: stable verdict-fingerprint hash (for Slice 4 dedup)
# ---------------------------------------------------------------------------


def _verdict_fingerprint(
    target: Optional[ReplayTarget],
    verdict: BranchVerdict,
) -> str:
    """Stable sha256[:16] over (session_id, swap_at_phase,
    swap_decision_kind, verdict). Same target + same verdict
    produce same fingerprint → enables idempotent dedup at
    Slice 4. NEVER raises."""
    try:
        if target is None:
            return ""
        payload = (
            f"{target.session_id}|{target.swap_at_phase}|"
            f"{target.swap_decision_kind.value}|{verdict.value}"
        )
        return hashlib.sha256(
            payload.encode("utf-8"),
        ).hexdigest()[:16]
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public: compute_branch_verdict — pure decision over branch pair
# ---------------------------------------------------------------------------


def compute_branch_verdict(
    original: Optional[BranchSnapshot],
    counterfactual: Optional[BranchSnapshot],
    *,
    postmortem_tolerance: Optional[int] = None,
    verify_pct_tolerance: Optional[float] = None,
) -> BranchVerdict:
    """Pure multi-criteria comparison. Closed 5-value taxonomy.
    NEVER raises.

    Decision tree (every input maps to exactly one verdict):

      1. Either branch is None / not BranchSnapshot → FAILED.
      2. Primary axis — ``terminal_success``:
         * orig success, counter failed → DIVERGED_BETTER
         * counter success, orig failed → DIVERGED_WORSE
      3. Both same success-status — secondary axes:
         * postmortem_count delta > tolerance → resolve
         * verify_pass_rate delta > tolerance → resolve
         * apply_outcome qualitative → resolve
      4. Multi-criteria contradict (e.g., orig better on
         postmortem, counter better on verify) → DIVERGED_NEUTRAL
      5. All criteria within tolerance → EQUIVALENT"""
    try:
        if not isinstance(original, BranchSnapshot):
            return BranchVerdict.FAILED
        if not isinstance(counterfactual, BranchSnapshot):
            return BranchVerdict.FAILED

        eff_pm_tolerance = (
            int(postmortem_tolerance)
            if postmortem_tolerance is not None
            else verdict_tolerance_postmortem_count()
        )
        eff_pm_tolerance = max(0, eff_pm_tolerance)

        eff_vp_tolerance = (
            float(verify_pct_tolerance)
            if verify_pct_tolerance is not None
            else verdict_tolerance_verify_pass_pct()
        )
        eff_vp_tolerance = max(0.0, eff_vp_tolerance)

        # Step 2: primary axis — terminal_success
        orig_success = bool(original.terminal_success)
        counter_success = bool(counterfactual.terminal_success)

        if orig_success and not counter_success:
            return BranchVerdict.DIVERGED_BETTER
        if counter_success and not orig_success:
            return BranchVerdict.DIVERGED_WORSE

        # Both same primary outcome — accumulate secondary
        # signals. We track which side "wins" on each axis;
        # if orig wins on some + counter wins on others →
        # DIVERGED_NEUTRAL. If only one side wins (and it's a
        # measurable delta) → DIVERGED_BETTER/WORSE.

        orig_wins = 0
        counter_wins = 0

        # Secondary axis: postmortem count (lower is better)
        orig_pm = len(original.postmortem_records)
        counter_pm = len(counterfactual.postmortem_records)
        pm_delta = orig_pm - counter_pm
        if pm_delta > eff_pm_tolerance:
            # orig has MORE postmortems → counter wins
            counter_wins += 1
        elif -pm_delta > eff_pm_tolerance:
            # counter has MORE postmortems → orig wins
            orig_wins += 1

        # Tertiary axis: verify pass rate (higher is better),
        # expressed as percentage
        orig_vp = original.verify_pass_rate() * 100.0
        counter_vp = counterfactual.verify_pass_rate() * 100.0
        vp_delta = orig_vp - counter_vp
        if vp_delta > eff_vp_tolerance:
            orig_wins += 1
        elif -vp_delta > eff_vp_tolerance:
            counter_wins += 1

        # Quaternary axis: apply_outcome qualitative
        # ("single"/"multi" are both fine; "none" is worse
        # than success-with-apply).
        orig_apply_quality = _apply_quality(
            original.apply_outcome, orig_success,
        )
        counter_apply_quality = _apply_quality(
            counterfactual.apply_outcome, counter_success,
        )
        if orig_apply_quality > counter_apply_quality:
            orig_wins += 1
        elif counter_apply_quality > orig_apply_quality:
            counter_wins += 1

        # Resolve
        if orig_wins > 0 and counter_wins > 0:
            return BranchVerdict.DIVERGED_NEUTRAL
        if orig_wins > 0 and counter_wins == 0:
            return BranchVerdict.DIVERGED_BETTER
        if counter_wins > 0 and orig_wins == 0:
            return BranchVerdict.DIVERGED_WORSE
        # Both zero — branches equivalent within tolerance
        return BranchVerdict.EQUIVALENT
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CounterfactualReplay] compute_branch_verdict "
            "raised: %s", exc,
        )
        return BranchVerdict.FAILED


def _apply_quality(apply_outcome: str, success: bool) -> int:
    """Map apply_outcome to a comparable integer.
    success-with-apply > success-without > failed-with-apply >
    failed-without. NEVER raises."""
    try:
        outcome = (apply_outcome or "").strip().lower()
        applied = outcome in ("single", "multi")
        if success and applied:
            return 3
        if success:
            return 2
        if applied:
            return 1
        return 0
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# Public: compute_replay_outcome — aggregate verdict producer
# ---------------------------------------------------------------------------


def compute_replay_outcome(
    target: Optional[ReplayTarget],
    original: Optional[BranchSnapshot],
    counterfactual: Optional[BranchSnapshot],
    *,
    divergence_phase: str = "",
    divergence_reason: str = "",
    enabled_override: Optional[bool] = None,
    postmortem_tolerance: Optional[int] = None,
    verify_pct_tolerance: Optional[float] = None,
) -> ReplayVerdict:
    """Aggregate replay result. Composes branch verdict over
    (original, counterfactual) and stamps the closed-taxonomy
    outcome. NEVER raises.

    Decision tree:

      1. Master flag off → ``DISABLED``.
      2. Target missing / not ReplayTarget → ``FAILED``.
      3. ``divergence_phase`` non-empty AND
         ``divergence_reason`` non-empty → ``DIVERGED``
         (cached hash mismatch detected upstream).
      4. Both branches missing → ``FAILED``.
      5. One branch missing → ``PARTIAL`` (caller can still
         observe the available branch).
      6. Both branches present → ``compute_branch_verdict``
         then wrap in ``SUCCESS``."""
    try:
        is_enabled = (
            enabled_override if enabled_override is not None
            else counterfactual_replay_enabled()
        )
        if not is_enabled:
            return ReplayVerdict(
                outcome=ReplayOutcome.DISABLED,
                target=(
                    target if isinstance(target, ReplayTarget)
                    else None
                ),
                detail=(
                    "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED is "
                    "false (or override) — no replay performed"
                ),
            )

        if not isinstance(target, ReplayTarget):
            return ReplayVerdict(
                outcome=ReplayOutcome.FAILED,
                detail="target is not a ReplayTarget",
            )

        # Cached-hash divergence takes priority — if the engine
        # detected structural divergence pre-swap, the verdict
        # is meaningless (replay non-deterministic for this
        # session).
        if divergence_phase or divergence_reason:
            return ReplayVerdict(
                outcome=ReplayOutcome.DIVERGED,
                target=target,
                original_branch=(
                    original
                    if isinstance(original, BranchSnapshot)
                    else None
                ),
                counterfactual_branch=(
                    counterfactual
                    if isinstance(
                        counterfactual, BranchSnapshot,
                    ) else None
                ),
                divergence_phase=str(divergence_phase or ""),
                divergence_reason=str(
                    divergence_reason or "",
                )[:500],
                detail=(
                    "branches diverged structurally at "
                    f"{divergence_phase!r} — replay non-"
                    "deterministic for this session"
                ),
            )

        orig_valid = isinstance(original, BranchSnapshot)
        counter_valid = isinstance(counterfactual, BranchSnapshot)

        if not orig_valid and not counter_valid:
            return ReplayVerdict(
                outcome=ReplayOutcome.FAILED,
                target=target,
                detail="both branches missing or invalid",
            )

        if not orig_valid or not counter_valid:
            return ReplayVerdict(
                outcome=ReplayOutcome.PARTIAL,
                target=target,
                original_branch=(
                    original if orig_valid else None
                ),
                counterfactual_branch=(
                    counterfactual if counter_valid else None
                ),
                detail=(
                    "one branch only: " + (
                        "original-only"
                        if orig_valid else "counterfactual-only"
                    )
                ),
            )

        # Both valid — compute verdict
        verdict = compute_branch_verdict(
            original, counterfactual,
            postmortem_tolerance=postmortem_tolerance,
            verify_pct_tolerance=verify_pct_tolerance,
        )
        return ReplayVerdict(
            outcome=ReplayOutcome.SUCCESS,
            target=target,
            original_branch=original,
            counterfactual_branch=counterfactual,
            verdict=verdict,
            detail=(
                f"verdict={verdict.value} "
                f"(orig succ={original.terminal_success} "
                f"counter succ={counterfactual.terminal_success})"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CounterfactualReplay] compute_replay_outcome "
            "raised: %s", exc,
        )
        return ReplayVerdict(
            outcome=ReplayOutcome.FAILED,
            target=(
                target if isinstance(target, ReplayTarget)
                else None
            ),
            detail=f"compute_replay_outcome raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Antivenom Vector 4: per-kind payload schema validation
# ---------------------------------------------------------------------------


# Closed-vocabulary gate verdicts — the only values a GATE_DECISION
# payload's "verdict" key may carry. Sourced from the canonical
# risk-tier vocabulary in counterfactual_replay_engine.py.
_GATE_VERDICT_VOCABULARY: frozenset = frozenset({
    "auto_apply",
    "safe_auto",
    "notify_apply",
    "approval_required",
    "blocked",
})


# Per-kind valid key sets. Each DecisionOverrideKind has a closed set
# of valid payload keys. Unknown keys → validation failure.
_VALID_PAYLOAD_KEYS: Dict[DecisionOverrideKind, frozenset] = {
    DecisionOverrideKind.GATE_DECISION: frozenset({"verdict"}),
    DecisionOverrideKind.POSTMORTEM_INJECTION: frozenset({"enabled"}),
    DecisionOverrideKind.RECURRENCE_BOOST: frozenset({"enabled"}),
    DecisionOverrideKind.QUORUM_INVOCATION: frozenset({"enabled"}),
    DecisionOverrideKind.COHERENCE_OBSERVER: frozenset({"enabled"}),
}


def _validate_swap_payload_enabled() -> bool:
    """``JARVIS_REPLAY_PAYLOAD_VALIDATION_ENABLED`` (default
    ``true``). Kill switch for payload schema validation.
    Explicit ``false`` disables; empty/unset = default ``true``."""
    raw = os.environ.get(
        "JARVIS_REPLAY_PAYLOAD_VALIDATION_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def validate_swap_payload(
    kind: DecisionOverrideKind,
    payload: Mapping[str, Any],
) -> Tuple[bool, str]:
    """Structurally validate ``swap_decision_payload`` against the
    per-kind schema whitelist. Returns ``(valid, reason)``.
    NEVER raises.

    Per-kind schemas (closed vocabulary):

      * ``GATE_DECISION``: ``{"verdict": str}`` where verdict ∈
        ``{auto_apply, safe_auto, notify_apply,
        approval_required, blocked}``. Empty ``{}`` is also valid
        (engine treats missing verdict as "halt at swap").

      * ``POSTMORTEM_INJECTION``, ``RECURRENCE_BOOST``,
        ``QUORUM_INVOCATION``, ``COHERENCE_OBSERVER``: ``{}`` or
        ``{"enabled": bool}``. No other keys.

    Validation is env-gated via
    ``JARVIS_REPLAY_PAYLOAD_VALIDATION_ENABLED`` (default ``true``).
    When disabled, returns ``(True, "")`` unconditionally."""
    try:
        if not _validate_swap_payload_enabled():
            return (True, "")

        if not isinstance(kind, DecisionOverrideKind):
            return (False, f"invalid kind type: {type(kind).__name__}")

        if not isinstance(payload, Mapping):
            return (
                False,
                f"payload must be a Mapping, got {type(payload).__name__}",
            )

        # Empty payload is always valid — engine treats it as
        # "use kind-specific defaults".
        if not payload:
            return (True, "")

        valid_keys = _VALID_PAYLOAD_KEYS.get(kind)
        if valid_keys is None:
            # Unknown kind — cannot validate; reject defensively.
            return (
                False,
                f"no payload schema for kind {kind.value!r}",
            )

        # Check for unknown keys.
        payload_keys = frozenset(str(k) for k in payload.keys())
        unknown_keys = payload_keys - valid_keys
        if unknown_keys:
            return (
                False,
                f"unknown key(s) {sorted(unknown_keys)!r} in "
                f"payload for {kind.value}",
            )

        # Kind-specific value validation.
        if kind is DecisionOverrideKind.GATE_DECISION:
            verdict_raw = payload.get("verdict")
            if verdict_raw is not None:
                try:
                    verdict_str = str(verdict_raw).strip().lower()
                except Exception:  # noqa: BLE001
                    return (
                        False,
                        f"verdict value not coercible to string",
                    )
                if verdict_str not in _GATE_VERDICT_VOCABULARY:
                    return (
                        False,
                        f"invalid verdict {verdict_str!r} for "
                        f"GATE_DECISION — valid: "
                        f"{sorted(_GATE_VERDICT_VOCABULARY)}",
                    )
        else:
            # enabled/disabled kinds — validate "enabled" is bool.
            enabled_raw = payload.get("enabled")
            if enabled_raw is not None:
                if not isinstance(enabled_raw, bool):
                    return (
                        False,
                        f"'enabled' must be bool, got "
                        f"{type(enabled_raw).__name__}",
                    )

        return (True, "")
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CounterfactualReplay] validate_swap_payload "
            "raised: %s", exc,
        )
        return (False, f"validation error: {exc!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "BranchSnapshot",
    "BranchVerdict",
    "COUNTERFACTUAL_REPLAY_SCHEMA_VERSION",
    "DecisionOverrideKind",
    "ReplayOutcome",
    "ReplayTarget",
    "ReplayVerdict",
    "compute_branch_verdict",
    "compute_replay_outcome",
    "counterfactual_replay_enabled",
    "replay_max_duration_seconds",
    "replay_max_phases_per_branch",
    "replay_min_replays_for_baseline",
    "validate_swap_payload",
    "verdict_tolerance_postmortem_count",
    "verdict_tolerance_verify_pass_pct",
]

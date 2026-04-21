"""
RecoveryAdvisor — rule-based "3 things to try next" after an op fails.
========================================================================

Slice 1 of the Recovery Guidance + Voice Loop Closure arc. Closes the
CC-parity gap *"no error-recovery guidance — when an op fails, the
operator sees a stack trace, not '3 things to try next'."*

Scope
-----

* **Rule-based, deterministic.** Every suggestion comes from a
  hand-written rule table keyed on ``failure_class`` / ``stop_reason``
  / terminal phase. No LLM call, no guessed commands — grep-pinned at
  graduation so the advisor stays a Tier 0 reflex (§5 Manifesto).
* **Safe by construction.** Suggestions describe *commands* / env
  *toggles* / *diagnostic next steps* — never state-mutating actions
  the advisor itself performs. The operator decides what to run.
* **Bounded output.** A :class:`RecoveryPlan` always carries ≤5
  suggestions (3 is the default cap) — respects the original gap
  framing and keeps TTS renderings short.

Authority boundary
------------------

* §1 read-only — the advisor observes failure state; it never
  invokes orchestrator, policy engines, or tool executors.
* §5 Tier 0 reflex — pure pattern matching, microseconds.
* §7 fail-closed — unknown failure signatures yield a generic
  "check debug.log + re-run in trace mode" plan rather than raising.
* §8 observable — every plan is JSON-safe via ``project()``.
* No imports from orchestrator / policy_engine / iron_gate /
  risk_tier_floor / semantic_guardian / tool_executor /
  candidate_generator / change_engine. Grep-pinned at graduation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.RecoveryAdvisor")


RECOVERY_PLAN_SCHEMA_VERSION: str = "recovery_plan.v1"


# ---------------------------------------------------------------------------
# Priority vocabulary
# ---------------------------------------------------------------------------


PRIORITY_CRITICAL: str = "critical"
PRIORITY_HIGH: str = "high"
PRIORITY_MEDIUM: str = "medium"
PRIORITY_LOW: str = "low"

_PRIORITY_ORDER: Dict[str, int] = {
    PRIORITY_CRITICAL: 0,
    PRIORITY_HIGH: 1,
    PRIORITY_MEDIUM: 2,
    PRIORITY_LOW: 3,
}


# ---------------------------------------------------------------------------
# Known failure classes — matched by the rule table
# ---------------------------------------------------------------------------


# Stop-reason constants — match the strings orchestrator uses. These
# are kept as module-level constants so callers can hard-code them
# without importing the orchestrator FSM.
STOP_COST_CAP: str = "cost_cap_exceeded"
STOP_VALIDATION_EXHAUSTED: str = "validation_retries_exhausted"
STOP_L2_EXHAUSTED: str = "l2_repair_exhausted"
STOP_APPROVAL_REQUIRED: str = "approval_required"
STOP_APPROVAL_TIMEOUT: str = "approval_timeout"
STOP_IRON_GATE_REJECT: str = "iron_gate_rejected"
STOP_EXPLORATION_INSUFFICIENT: str = "exploration_insufficient"
STOP_ASCII_GATE: str = "ascii_gate_rejected"
STOP_MULTI_FILE_COVERAGE: str = "multi_file_coverage_insufficient"
STOP_PROVIDER_EXHAUSTED: str = "provider_exhaustion"
STOP_POLICY_DENIED: str = "policy_denied"
STOP_CANCELLED_BY_OPERATOR: str = "cancelled_by_operator"
STOP_IDLE_TIMEOUT: str = "idle_timeout"
STOP_UNHANDLED_EXCEPTION: str = "unhandled_exception"

_KNOWN_STOP_REASONS: Tuple[str, ...] = (
    STOP_COST_CAP,
    STOP_VALIDATION_EXHAUSTED,
    STOP_L2_EXHAUSTED,
    STOP_APPROVAL_REQUIRED,
    STOP_APPROVAL_TIMEOUT,
    STOP_IRON_GATE_REJECT,
    STOP_EXPLORATION_INSUFFICIENT,
    STOP_ASCII_GATE,
    STOP_MULTI_FILE_COVERAGE,
    STOP_PROVIDER_EXHAUSTED,
    STOP_POLICY_DENIED,
    STOP_CANCELLED_BY_OPERATOR,
    STOP_IDLE_TIMEOUT,
    STOP_UNHANDLED_EXCEPTION,
)


def known_stop_reasons() -> Tuple[str, ...]:
    """Return the set the rule table is guaranteed to cover."""
    return _KNOWN_STOP_REASONS


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureContext:
    """Bundle of signals the advisor uses to pattern-match.

    All fields are optional — unknown context yields a generic plan.
    The advisor never raises on missing data.
    """
    op_id: str = ""
    final_phase: str = ""
    stop_reason: str = ""
    failure_class: str = ""  # 'test' / 'infra' / 'content' / 'exploration' / 'ascii' / etc.
    exception_type: str = ""
    exception_message: str = ""
    cost_spent_usd: float = 0.0
    cost_cap_usd: float = 0.0
    validation_retries: int = 0
    l2_iterations: int = 0
    route: str = ""
    complexity: str = ""
    session_id: str = ""
    # Path to debug.log / session dir — advisor embeds this in the
    # generic "check debug.log" suggestion.
    debug_log_path: str = ""

    def project(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "final_phase": self.final_phase,
            "stop_reason": self.stop_reason,
            "failure_class": self.failure_class,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message[:500],
            "cost_spent_usd": self.cost_spent_usd,
            "cost_cap_usd": self.cost_cap_usd,
            "validation_retries": self.validation_retries,
            "l2_iterations": self.l2_iterations,
            "route": self.route,
            "complexity": self.complexity,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class RecoverySuggestion:
    """One actionable suggestion — title + command/instruction + why."""
    title: str
    command: str = ""  # REPL verb / shell command / env-var instruction
    rationale: str = ""
    priority: str = PRIORITY_MEDIUM

    def project(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "command": self.command,
            "rationale": self.rationale,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class RecoveryPlan:
    """Ordered list of suggestions for one failed op."""
    op_id: str
    failure_summary: str  # human-readable one-liner
    suggestions: Tuple[RecoverySuggestion, ...] = field(default_factory=tuple)
    matched_rule: str = ""  # "cost_cap" / "validation_exhausted" / "generic"
    context: Optional[FailureContext] = None
    schema_version: str = RECOVERY_PLAN_SCHEMA_VERSION

    def project(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "failure_summary": self.failure_summary,
            "matched_rule": self.matched_rule,
            "suggestions": [s.project() for s in self.suggestions],
            "context": (
                self.context.project() if self.context is not None else None
            ),
        }

    @property
    def has_suggestions(self) -> bool:
        return len(self.suggestions) > 0

    def top_suggestion(self) -> Optional[RecoverySuggestion]:
        """Highest-priority suggestion, or ``None`` when empty."""
        if not self.suggestions:
            return None
        return min(
            self.suggestions,
            key=lambda s: _PRIORITY_ORDER.get(s.priority, 99),
        )


# ---------------------------------------------------------------------------
# Rule table — the heart of the advisor
# ---------------------------------------------------------------------------


RuleMatcher = Callable[[FailureContext], bool]
RuleBuilder = Callable[[FailureContext], Tuple[str, str, List[RecoverySuggestion]]]


def _cost_cap_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    spent = ctx.cost_spent_usd
    cap = ctx.cost_cap_usd
    summary = (
        f"Op hit cost cap at ${spent:.4f} / ${cap:.4f}"
        if cap > 0
        else f"Op hit cost cap at ${spent:.4f}"
    )
    return ("cost_cap", summary, [
        RecoverySuggestion(
            title="Inspect which phase ate the budget",
            command=f"/cost {ctx.op_id}" if ctx.op_id else "/cost",
            rationale=(
                "Find the hot phase before spending more — the cost may "
                "be concentrated in GENERATE or VERIFY and the fix may "
                "be prompt-side, not budget-side."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Raise the per-op cost cap",
            command=(
                "JARVIS_OP_COST_BASELINE_USD=1.0 "
                "(or set complexity/route factors)"
            ),
            rationale=(
                "If the hot phase is unavoidable and the work is genuinely "
                "complex, widen the cap rather than narrow the op."
            ),
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Simplify the op or split it",
            command="Edit intent / resubmit as smaller ops",
            rationale=(
                "The cheapest recovery is usually a smaller scope — "
                "Tier 0 routing + BackgroundAgentPool will re-price."
            ),
            priority=PRIORITY_MEDIUM,
        ),
    ])


def _validation_exhausted_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    summary = (
        f"Validation exhausted retries "
        f"(failure_class={ctx.failure_class or 'unknown'})"
    )
    return ("validation_exhausted", summary, [
        RecoverySuggestion(
            title="Inspect the failing test(s)",
            command=(
                f"grep -n FAIL .ouroboros/sessions/{ctx.session_id}/debug.log"
                if ctx.session_id else "grep -n FAIL on the latest debug.log"
            ),
            rationale=(
                "Distinguish a real content regression from an infra flake "
                "— the failure_class tag is heuristic and can mismatch."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Bypass validation retries once to reach POSTMORTEM",
            command="JARVIS_MAX_VALIDATE_RETRIES=0 /resume <op-id>",
            rationale=(
                "When validation has a known flake, zero retries collapses "
                "it to a single attempt so the pipeline reaches L2 faster."
            ),
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Widen the L2 repair time budget",
            command="JARVIS_L2_TIMEBOX_S=180 /resume <op-id>",
            rationale=(
                "L2 may have been choked by a 120s default on a legitimately "
                "larger repair."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _l2_exhausted_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("l2_exhausted", "L2 self-repair exhausted its iteration budget", [
        RecoverySuggestion(
            title="Check exploration depth before re-running",
            command="/recover exploration <op-id>",
            rationale=(
                "L2 stalls when the generation prompt lacks context — "
                "widen the exploration floor and re-run."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Raise L2 iteration limit for this re-run",
            command="JARVIS_L2_MAX_ITERATIONS=8 /resume <op-id>",
            rationale=(
                "Default is 5 iterations — complex multi-file repairs "
                "sometimes need more headroom."
            ),
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Disable L2 and hand-fix from the last candidate",
            command="JARVIS_L2_ENABLED=false + review .ouroboros/candidates/",
            rationale=(
                "Escape hatch when L2 is fighting itself — grab the last "
                "generation, apply by hand, verify with /resume."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _approval_required_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("approval_required", "Op paused awaiting human approval", [
        RecoverySuggestion(
            title="Approve the plan",
            command=f"/plan approve {ctx.op_id}" if ctx.op_id else "/plan approve <op-id>",
            rationale="Resumes the pipeline through APPLY + VERIFY.",
            priority=PRIORITY_CRITICAL,
        ),
        RecoverySuggestion(
            title="Reject with reasoning",
            command=(
                f"/plan reject {ctx.op_id} <reason>"
                if ctx.op_id else "/plan reject <op-id> <reason>"
            ),
            rationale="POSTMORTEM captures the reason for learning.",
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Inspect the candidate + diff before deciding",
            command=(
                f"/plan show {ctx.op_id}" if ctx.op_id else "/plan show <op-id>"
            ),
            rationale=(
                "Approval gates exist precisely because pure-code gates "
                "can't judge intent — read the plan."
            ),
            priority=PRIORITY_HIGH,
        ),
    ])


def _approval_timeout_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("approval_timeout", "Approval request timed out without a decision", [
        RecoverySuggestion(
            title="Re-submit via /resume with extended timeout",
            command="JARVIS_PLAN_APPROVAL_TIMEOUT_S=600 /resume <op-id>",
            rationale=(
                "Default timeout is short — legitimate review can need "
                "more time."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Approve retroactively and re-resume",
            command="/plan approve <op-id> && /resume <op-id>",
            rationale="The plan object is still in the controller's history.",
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Disable plan-approval mode for trusted flows",
            command="JARVIS_PLAN_APPROVAL_MODE=false",
            rationale=(
                "Only for flows where async review is infeasible — loses "
                "the operator-review safety net."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _iron_gate_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("iron_gate", "Generation rejected by Iron Gate", [
        RecoverySuggestion(
            title="Read the gate's retry feedback in debug.log",
            command=(
                f"grep -n IronGate .ouroboros/sessions/{ctx.session_id}/debug.log"
                if ctx.session_id else "grep -n IronGate debug.log"
            ),
            rationale=(
                "The gate tells the model what's missing (category/ascii/"
                "multi-file) — the feedback is in the retry prompt."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Widen the exploration floor for complex ops",
            command=(
                "JARVIS_EXPLORATION_MIN_SCORE_COMPLEX=5 "
                "JARVIS_EXPLORATION_MIN_CATEGORIES_COMPLEX=2"
            ),
            rationale=(
                "Complex ops sometimes need more diversity of read/search "
                "before the gate admits the patch."
            ),
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Resubmit with richer intent (pointers to callers)",
            command="Edit intent + /resume",
            rationale=(
                "Pointing at call sites is free context for the model and "
                "often flips the gate's verdict."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _exploration_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("exploration", "Exploration ledger verdict: insufficient", [
        RecoverySuggestion(
            title="Ask model to diversify tool calls",
            command="Edit generation prompt via JARVIS_EXPLORATION_HINT",
            rationale=(
                "Low score across categories usually means too many "
                "read_file calls — add get_callers or git_log."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Lower the floor for experimental ops",
            command="JARVIS_EXPLORATION_MIN_SCORE_LIGHT=2",
            rationale=(
                "Light-complexity experiments sometimes don't need the "
                "full diversity table; the default is tuned for COMPLEX."
            ),
            priority=PRIORITY_LOW,
        ),
        RecoverySuggestion(
            title="Switch to shadow mode while tuning",
            command="JARVIS_EXPLORATION_LEDGER_ENABLED=false",
            rationale=(
                "Falls back to the legacy int counter so you can unblock "
                "today and tune ledger thresholds at leisure."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _ascii_gate_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("ascii_gate", "Candidate rejected by ASCII-strictness gate", [
        RecoverySuggestion(
            title="Inspect the offending codepoints",
            command="grep -P '[^\\x00-\\x7F]' <candidate-file>",
            rationale=(
                "ASCII gate catches hidden unicode (e.g. `rapidفuzz`) — "
                "view the exact chars before blaming the model."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Fix the prompt to demand ASCII-only identifiers",
            command="Add 'ASCII only, no unicode' to intent or template",
            rationale=(
                "The model sometimes echoes unicode seen in source; a "
                "prompt-level pin usually prevents the loop."
            ),
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Temporarily disable the gate (development only)",
            command="JARVIS_ASCII_GATE=false",
            rationale=(
                "Only safe in dev — removes the Unicode-corruption guard."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _multi_file_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("multi_file_coverage", "Multi-file candidate missed required files", [
        RecoverySuggestion(
            title="Review the plan's target_files vs. candidate files",
            command="/plan show <op-id>",
            rationale=(
                "The coverage gate fires when the candidate's files list "
                "doesn't match the plan's scope — fix the plan or the patch."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Resubmit with explicit file list in intent",
            command="Add 'MUST edit: <fileA>, <fileB>' to intent",
            rationale=(
                "Explicit target files short-circuit the inference path "
                "and keep the coverage check happy."
            ),
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Disable the gate (escape hatch)",
            command="JARVIS_MULTI_FILE_GEN_ENABLED=false",
            rationale=(
                "Falls back to single-file generation — loses the atomic "
                "multi-file guarantee; only for emergencies."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _provider_exhausted_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("provider_exhausted", "All provider tiers exhausted", [
        RecoverySuggestion(
            title="Check provider health dashboards",
            command="curl https://status.anthropic.com + dw.health",
            rationale=(
                "Upstream outages mask as exhaustion; if a tier is down, "
                "no local tuning will unblock."
            ),
            priority=PRIORITY_CRITICAL,
        ),
        RecoverySuggestion(
            title="Force Tier 1 (Claude) via route override",
            command="JARVIS_FORCE_ROUTE=immediate /resume <op-id>",
            rationale=(
                "Skips DW exhaustion by going straight to Claude — higher "
                "cost but unblocks when Tier 0 is flaky."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Extend the fallback timeout window",
            command="JARVIS_FALLBACK_MAX_TIMEOUT_S=120",
            rationale=(
                "Default 60s cap is aggressive — a longer window lets "
                "slow Tier 1 complete instead of aborting."
            ),
            priority=PRIORITY_MEDIUM,
        ),
    ])


def _policy_denied_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("policy_denied", "Operation blocked by policy engine", [
        RecoverySuggestion(
            title="Read the policy decision reason in debug.log",
            command="grep -n POLICY_DENIED debug.log",
            rationale=(
                "Policy denial is deterministic — the reason_code tells "
                "you exactly which rule triggered."
            ),
            priority=PRIORITY_CRITICAL,
        ),
        RecoverySuggestion(
            title="Check FORBIDDEN_PATH in user_preference_memory",
            command="cat .jarvis/user_preferences/*forbidden*",
            rationale=(
                "If the path is in a persistent forbidden list, that's "
                "operator intent and the op should be rescoped."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Elevate the risk tier for review",
            command="JARVIS_MIN_RISK_TIER=approval_required",
            rationale=(
                "If you believe the policy is over-restrictive, route the "
                "op through human approval instead of auto-denying."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _cancelled_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("cancelled", "Operation cancelled by operator", [
        RecoverySuggestion(
            title="Resume from the last checkpoint",
            command=(
                f"/resume {ctx.op_id}" if ctx.op_id else "/resume <op-id>"
            ),
            rationale=(
                "Cancellation preserves state — /resume picks up at the "
                "phase where /cancel fired."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Inspect why you cancelled",
            command=(
                f"less .ouroboros/sessions/{ctx.session_id}/debug.log"
                if ctx.session_id else "less on latest debug.log"
            ),
            rationale=(
                "Often the cancel reason is recoverable with a config "
                "tweak — worth checking before resubmit."
            ),
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Submit a new, smaller op instead",
            command="Edit intent + submit",
            rationale=(
                "If the original scope was the reason for cancel, a "
                "smaller replacement is cleaner than resume."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _idle_timeout_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    return ("idle_timeout", "Session ended on idle timeout", [
        RecoverySuggestion(
            title="Increase idle timeout for long-running sessions",
            command="--idle-timeout 1800 (or JARVIS_BATTLE_IDLE_TIMEOUT_S)",
            rationale=(
                "Default idle is 600s — background exploration + intent "
                "mining can legitimately idle longer."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Submit a warm-up op to keep the loop active",
            command="/intent <any active work item>",
            rationale=(
                "Manually priming the intake router prevents the idle "
                "timer from firing."
            ),
            priority=PRIORITY_LOW,
        ),
        RecoverySuggestion(
            title="Disable idle timeout (only for curated runs)",
            command="JARVIS_BATTLE_IDLE_TIMEOUT_S=0",
            rationale=(
                "No auto-shutdown — safe only when you're going to kill "
                "the process manually."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


def _unhandled_exception_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    exc_line = (
        f" ({ctx.exception_type}: {ctx.exception_message[:60]})"
        if ctx.exception_type else ""
    )
    return ("unhandled_exception", f"Unhandled exception{exc_line}", [
        RecoverySuggestion(
            title="Read the full traceback in debug.log",
            command=(
                f"less .ouroboros/sessions/{ctx.session_id}/debug.log"
                if ctx.session_id else "less on latest debug.log"
            ),
            rationale=(
                "Unhandled exceptions carry the real diagnostic — the "
                "POSTMORTEM summary is always a lossy projection."
            ),
            priority=PRIORITY_CRITICAL,
        ),
        RecoverySuggestion(
            title="Re-run with verbose tracing",
            command="JARVIS_LOG_LEVEL=DEBUG /resume <op-id>",
            rationale=(
                "Many exceptions emerge from async event-loop races that "
                "only surface at DEBUG level."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Capture + file an incident if the exception is new",
            command="gh issue create --title '...' --body <traceback>",
            rationale=(
                "Exception stability is a correctness property — if this "
                "isn't a known flake, track it."
            ),
            priority=PRIORITY_MEDIUM,
        ),
    ])


def _generic_rule(ctx: FailureContext) -> Tuple[str, str, List[RecoverySuggestion]]:
    reason = ctx.stop_reason or ctx.final_phase or "unknown"
    return ("generic", f"Op ended in '{reason}'; no dedicated rule matched", [
        RecoverySuggestion(
            title="Read debug.log",
            command=(
                f"less .ouroboros/sessions/{ctx.session_id}/debug.log"
                if ctx.session_id else "less on latest session's debug.log"
            ),
            rationale=(
                "The authoritative answer to 'what happened' is always "
                "the session's debug.log, not the summary."
            ),
            priority=PRIORITY_HIGH,
        ),
        RecoverySuggestion(
            title="Inspect the session browser",
            command=(
                f"/session show {ctx.session_id}"
                if ctx.session_id else "/session recent"
            ),
            rationale=(
                "Session browser surfaces ops_digest, verify counts, "
                "and stop_reason in a glanceable form."
            ),
            priority=PRIORITY_MEDIUM,
        ),
        RecoverySuggestion(
            title="Re-submit the op; transient failures often clear",
            command="/resume <op-id> or re-submit via intent",
            rationale=(
                "Many unclassified failures are one-shot flakes "
                "(network, disk, upstream hiccup)."
            ),
            priority=PRIORITY_LOW,
        ),
    ])


# Ordered dispatch table — first match wins. Matchers are simple so
# callers can read the rule file top-to-bottom and predict behavior.
_RULES: List[Tuple[RuleMatcher, RuleBuilder]] = [
    (lambda c: c.stop_reason == STOP_COST_CAP, _cost_cap_rule),
    (lambda c: c.stop_reason == STOP_VALIDATION_EXHAUSTED, _validation_exhausted_rule),
    (lambda c: c.stop_reason == STOP_L2_EXHAUSTED, _l2_exhausted_rule),
    (lambda c: c.stop_reason == STOP_APPROVAL_REQUIRED, _approval_required_rule),
    (lambda c: c.stop_reason == STOP_APPROVAL_TIMEOUT, _approval_timeout_rule),
    (lambda c: c.stop_reason == STOP_IRON_GATE_REJECT, _iron_gate_rule),
    (lambda c: c.stop_reason == STOP_EXPLORATION_INSUFFICIENT, _exploration_rule),
    (lambda c: c.stop_reason == STOP_ASCII_GATE, _ascii_gate_rule),
    (lambda c: c.stop_reason == STOP_MULTI_FILE_COVERAGE, _multi_file_rule),
    (lambda c: c.stop_reason == STOP_PROVIDER_EXHAUSTED, _provider_exhausted_rule),
    (lambda c: c.stop_reason == STOP_POLICY_DENIED, _policy_denied_rule),
    (lambda c: c.stop_reason == STOP_CANCELLED_BY_OPERATOR, _cancelled_rule),
    (lambda c: c.stop_reason == STOP_IDLE_TIMEOUT, _idle_timeout_rule),
    (lambda c: c.stop_reason == STOP_UNHANDLED_EXCEPTION, _unhandled_exception_rule),
    (lambda c: bool(c.exception_type), _unhandled_exception_rule),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def advise(
    ctx: FailureContext,
    *,
    max_suggestions: int = 3,
) -> RecoveryPlan:
    """Project a failure state into a :class:`RecoveryPlan`.

    The first matching rule wins; unmatched contexts get a generic plan
    pointing at debug.log + session browser + resubmit.

    ``max_suggestions`` clamps the plan size (default 3, min 1, max 5).
    """
    if not isinstance(ctx, FailureContext):
        raise TypeError(
            f"advise expected FailureContext, got {type(ctx).__name__}"
        )
    n = max(1, min(5, int(max_suggestions)))
    matched_rule = "generic"
    summary = ""
    suggestions: List[RecoverySuggestion] = []
    for matcher, builder in _RULES:
        try:
            if matcher(ctx):
                matched_rule, summary, suggestions = builder(ctx)
                break
        except Exception as exc:  # noqa: BLE001 — rule failures must not kill advise
            logger.debug(
                "[RecoveryAdvisor] rule %r raised: %s",
                getattr(builder, "__name__", "?"), exc,
            )
    else:
        matched_rule, summary, suggestions = _generic_rule(ctx)
    # Clip to the cap, keeping priority order stable
    suggestions = suggestions[:n]
    return RecoveryPlan(
        op_id=ctx.op_id,
        failure_summary=summary,
        suggestions=tuple(suggestions),
        matched_rule=matched_rule,
        context=ctx,
    )


def rule_count() -> int:
    """Number of registered rules (excluding the generic fallback)."""
    return len(_RULES)


__all__ = [
    "PRIORITY_CRITICAL",
    "PRIORITY_HIGH",
    "PRIORITY_LOW",
    "PRIORITY_MEDIUM",
    "RECOVERY_PLAN_SCHEMA_VERSION",
    "FailureContext",
    "RecoveryPlan",
    "RecoverySuggestion",
    "STOP_APPROVAL_REQUIRED",
    "STOP_APPROVAL_TIMEOUT",
    "STOP_ASCII_GATE",
    "STOP_CANCELLED_BY_OPERATOR",
    "STOP_COST_CAP",
    "STOP_EXPLORATION_INSUFFICIENT",
    "STOP_IDLE_TIMEOUT",
    "STOP_IRON_GATE_REJECT",
    "STOP_L2_EXHAUSTED",
    "STOP_MULTI_FILE_COVERAGE",
    "STOP_POLICY_DENIED",
    "STOP_PROVIDER_EXHAUSTED",
    "STOP_UNHANDLED_EXCEPTION",
    "STOP_VALIDATION_EXHAUSTED",
    "advise",
    "known_stop_reasons",
    "rule_count",
]

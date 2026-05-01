"""Priority #3 Slice 5b — Counterfactual Replay orchestrator hook.

The production wire-up surface that connects the 4-slice replay
pipeline to autonomous orchestration.

Slices 1-4 ship pure pipeline pieces (primitive → engine → comparator
→ observer). Slice 5 graduated their flags. This module ships the
hook that orchestrator code (or scheduled background tasks) calls
after a session completes:

    await record_session_replay(
        session_id="bt-2026-05-02-...",
        policy_overrides=DEFAULT_REPLAY_POLICIES,
    )

For every ``ReplayTarget`` in ``policy_overrides``, the hook:

  1. Calls Slice 2's ``run_counterfactual_replay`` (zero LLM cost
     by AST-pinned construction — reads cached ledger + summary).
  2. Calls Slice 4's ``record_replay_verdict`` to persist + emit
     the per-verdict SSE event.
  3. Triggers Slice 4's ``compare_recent_history`` once at the end
     (best-effort) to update the empirical baseline. The
     ReplayObserver fires the BASELINE_UPDATED SSE event on its
     own cadence; this hook just refreshes the in-memory aggregate.

ZERO LLM cost — every call path through this hook reads cached
artifacts. The cost-contract is preserved by AST-pinned construction
(the hook imports ONLY Slice 1-4 modules + Phase 1 last_session_summary
for path resolution; never reaches into orchestrator-tier providers).

Direct-solve principles:

  * **Asynchronous** — public surface is ``async def
    record_session_replay(...)``. Slice 2's engine wraps disk I/O
    in ``asyncio.to_thread`` so the harness event loop is never
    blocked. Per-target replays run sequentially under a configurable
    concurrency cap.

  * **Dynamic** — the policy list is OPERATOR-SUPPLIED, not
    hardcoded. Default policies are constructed on-demand at call
    time from the 5 closed-taxonomy DecisionOverrideKind values
    plus a minimal payload. Operators inject custom targets via the
    ``policy_overrides`` parameter.

  * **Adaptive** — every per-target failure is captured as a
    ``HookOutcome.FAILED`` row in the bundle result; the hook
    does NOT abort the bundle on individual failures. Disk faults
    on persistence map to RECORDED_PERSIST_ERROR. Master-flag-off
    short-circuits to DISABLED with zero I/O.

  * **Intelligent** — the hook never forces a replay when the
    session lacks the swap point (Slice 2 returns PARTIAL); the
    bundle result distinguishes PARTIAL from FAILED so operators
    see the difference between "couldn't replay" and "ran but bad".

  * **Robust** — every public function NEVER raises. Garbage
    inputs map to FAILED. The hook is idempotent: calling it
    twice on the same session simply records two replays; the
    Slice 4 ring buffer rotates as needed.

  * **No hardcoding** — concurrency cap, default-policy
    construction, and master flag all env-driven.

Authority invariants (AST-pinned by Slice 5b regression suite):

  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * No exec / eval / compile.
  * Reuses Slice 1-4 primitives — no re-implementation.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence, Tuple

# Slice 1 schema reuse.
from backend.core.ouroboros.governance.verification.counterfactual_replay import (
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
    counterfactual_replay_enabled,
)

# Slice 2 engine reuse.
from backend.core.ouroboros.governance.verification.counterfactual_replay_engine import (
    run_counterfactual_replay,
)

# Slice 3 comparator reuse (used to refresh in-memory baseline at the
# tail of a bundle).
from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
    ComparisonOutcome,
    ComparisonReport,
)

# Slice 4 observer reuse.
from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
    RecordOutcome,
    compare_recent_history,
    record_replay_verdict,
)

logger = logging.getLogger(__name__)


REPLAY_HOOK_SCHEMA_VERSION: str = "replay_orchestrator_hook.1"


# ---------------------------------------------------------------------------
# Sub-flag — independent rollback knob from Slice 1's master
# ---------------------------------------------------------------------------


def replay_hook_enabled() -> bool:
    """``JARVIS_REPLAY_HOOK_ENABLED`` — orchestrator-hook gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``true`` post Slice 5b — operators rely on the hook
    being available to wire production callers. Hot-revert via
    explicit ``false`` short-circuits to DISABLED with zero I/O.
    """
    raw = os.environ.get(
        "JARVIS_REPLAY_HOOK_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def replay_hook_concurrency() -> int:
    """``JARVIS_REPLAY_HOOK_CONCURRENCY`` — max parallel
    ``run_counterfactual_replay`` calls per bundle. Default 1
    (sequential); clamped [1, 8]. Operators raising this should
    keep in mind that all replays read the same JSONL ring buffer
    + JSONL is flock'd, so concurrent writes serialize anyway —
    the speedup comes from disk-read parallelism on the engine
    side, not the persistence side."""
    try:
        raw = os.environ.get("JARVIS_REPLAY_HOOK_CONCURRENCY", "").strip()
        if not raw:
            return 1
        return max(1, min(8, int(raw)))
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# HookOutcome — closed taxonomy for per-bundle results
# ---------------------------------------------------------------------------


class HookOutcome(str, enum.Enum):
    """5-value closed taxonomy for ``record_session_replay``.

    Caller branches on the enum, never on free-form fields."""

    OK = "ok"
    """Bundle succeeded; every per-target replay produced a verdict
    that landed in the JSONL store. Baseline aggregate refreshed."""

    PARTIAL = "partial"
    """Some per-target replays succeeded, others returned
    ``ReplayOutcome.PARTIAL`` (typically because the swap point
    isn't in the session's recorded ledger). Bundle is still
    actionable — operators see the per-target detail."""

    DISABLED = "disabled"
    """Master flag or hook sub-flag is off. No replays run, no
    records written, no SSE events."""

    REJECTED = "rejected"
    """Garbage input (non-iterable policy_overrides, empty
    session_id, etc.). No work performed."""

    FAILED = "failed"
    """Bundle hit a failure path (e.g., every target raised, every
    record_replay_verdict returned PERSIST_ERROR). Distinct from
    PARTIAL — caller knows nothing useful was produced."""


# ---------------------------------------------------------------------------
# Per-target result + bundle result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookTargetResult:
    """One per-target replay invocation — frozen value object.

    Every ``ReplayTarget`` in the bundle produces exactly one
    HookTargetResult, regardless of outcome (engine failure vs
    persist failure vs success). Operators iterate the bundle's
    ``target_results`` to see the full picture."""
    target: ReplayTarget
    verdict: Optional[ReplayVerdict]
    record_outcome: Optional[RecordOutcome]
    error_detail: str = ""

    def is_actionable(self) -> bool:
        """True iff this target produced a SUCCESS-outcome verdict
        AND it landed in the persistence store. Caller uses this
        to short-circuit prevention-evidence reporting."""
        if self.verdict is None:
            return False
        if self.verdict.outcome is not ReplayOutcome.SUCCESS:
            return False
        if self.record_outcome not in (
            RecordOutcome.OK, RecordOutcome.OK_NO_STREAM,
        ):
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "target": (
                self.target.to_dict() if self.target is not None
                else None
            ),
            "verdict": (
                self.verdict.to_dict() if self.verdict is not None
                else None
            ),
            "record_outcome": (
                self.record_outcome.value
                if self.record_outcome is not None else None
            ),
            "error_detail": str(self.error_detail or ""),
        }


@dataclass(frozen=True)
class HookBundleResult:
    """Aggregate result for one ``record_session_replay`` call.

    Carries the full per-target detail + an aggregate ComparisonReport
    refreshed at the tail of the bundle. Frozen for clean snapshot
    semantics."""
    outcome: HookOutcome
    session_id: str
    target_results: Tuple[HookTargetResult, ...] = ()
    baseline_report: Optional[ComparisonReport] = None
    detail: str = ""
    schema_version: str = REPLAY_HOOK_SCHEMA_VERSION

    @property
    def actionable_count(self) -> int:
        return sum(1 for tr in self.target_results if tr.is_actionable())

    @property
    def prevention_evidence_count(self) -> int:
        return sum(
            1 for tr in self.target_results
            if tr.is_actionable() and tr.verdict is not None
            and tr.verdict.is_prevention_evidence()
        )

    def to_dict(self) -> dict:
        return {
            "outcome": str(self.outcome.value),
            "session_id": str(self.session_id),
            "target_results": [
                tr.to_dict() for tr in self.target_results
            ],
            "baseline_report": (
                self.baseline_report.to_dict()
                if self.baseline_report is not None else None
            ),
            "detail": str(self.detail or ""),
            "actionable_count": int(self.actionable_count),
            "prevention_evidence_count": int(
                self.prevention_evidence_count,
            ),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Default policy bundle — constructed on demand from closed taxonomy
# ---------------------------------------------------------------------------


def default_replay_policies(
    session_id: str,
) -> Tuple[ReplayTarget, ...]:
    """Construct the default per-session replay bundle.

    Returns one ReplayTarget per ``DecisionOverrideKind`` value —
    operators get a full sweep over the closed taxonomy without
    hardcoding a fixed list. Each target uses a sensible default
    swap_at_phase + payload that the engine's inference registry
    already knows how to handle.

    NEVER raises — empty session_id → empty bundle (caller
    interprets as a no-op)."""
    sid = str(session_id or "").strip()
    if not sid:
        return ()
    return (
        ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "approval_required"},
        ),
        ReplayTarget(
            session_id=sid, swap_at_phase="CONTEXT_EXPANSION",
            swap_decision_kind=DecisionOverrideKind.POSTMORTEM_INJECTION,
            swap_decision_payload={},
        ),
        ReplayTarget(
            session_id=sid, swap_at_phase="CONTEXT_EXPANSION",
            swap_decision_kind=DecisionOverrideKind.RECURRENCE_BOOST,
            swap_decision_payload={},
        ),
        ReplayTarget(
            session_id=sid, swap_at_phase="GENERATE",
            swap_decision_kind=DecisionOverrideKind.QUORUM_INVOCATION,
            swap_decision_payload={},
        ),
        ReplayTarget(
            session_id=sid, swap_at_phase="VALIDATE",
            swap_decision_kind=DecisionOverrideKind.COHERENCE_OBSERVER,
            swap_decision_payload={},
        ),
    )


# ---------------------------------------------------------------------------
# Public surface — record_session_replay
# ---------------------------------------------------------------------------


async def record_session_replay(
    session_id: str,
    *,
    policy_overrides: Optional[Sequence[ReplayTarget]] = None,
    enabled_override: Optional[bool] = None,
) -> HookBundleResult:
    """Run + record N counterfactual replays for one completed
    session.

    Resolution order:
      1. ``enabled_override`` (test/REPL escape hatch).
      2. Slice 1 master flag.
      3. Slice 5b hook sub-flag ``JARVIS_REPLAY_HOOK_ENABLED``.

    For every target in ``policy_overrides`` (or the default sweep
    when None), runs Slice 2's engine + records via Slice 4. Bundle
    summary distinguishes OK / PARTIAL / DISABLED / REJECTED /
    FAILED via the closed-taxonomy enum.

    NEVER raises out — disk faults, engine raises, persistence
    errors all map to per-target ``error_detail`` + bundle outcome.

    Parameters
    ----------
    session_id : str
        Session whose recorded ledger + summary will drive the
        replays. Empty string → REJECTED.
    policy_overrides : Sequence[ReplayTarget], optional
        Caller-supplied replay targets. None → use
        ``default_replay_policies(session_id)`` (one per
        DecisionOverrideKind value).
    enabled_override : bool, optional
        Force the hook on/off regardless of env (test/REPL hook).

    Returns
    -------
    HookBundleResult
        Frozen aggregate. ``target_results`` is one row per target.
        ``baseline_report`` is a fresh ComparisonReport over the
        recent history (None if computing it failed)."""
    sid = str(session_id or "").strip()
    if not sid:
        return HookBundleResult(
            outcome=HookOutcome.REJECTED,
            session_id="",
            detail="empty_session_id",
        )

    # 1. Flag resolution.
    if enabled_override is False:
        return HookBundleResult(
            outcome=HookOutcome.DISABLED,
            session_id=sid,
            detail="enabled_override=false",
        )
    if enabled_override is None:
        if not counterfactual_replay_enabled():
            return HookBundleResult(
                outcome=HookOutcome.DISABLED,
                session_id=sid,
                detail="counterfactual_replay_master_flag_off",
            )
        if not replay_hook_enabled():
            return HookBundleResult(
                outcome=HookOutcome.DISABLED,
                session_id=sid,
                detail="replay_hook_sub_flag_off",
            )

    # 2. Resolve targets.
    if policy_overrides is None:
        targets: Tuple[ReplayTarget, ...] = default_replay_policies(sid)
    else:
        try:
            targets = tuple(
                t for t in policy_overrides
                if isinstance(t, ReplayTarget)
            )
        except TypeError:
            return HookBundleResult(
                outcome=HookOutcome.REJECTED,
                session_id=sid,
                detail="non_iterable_policy_overrides",
            )

    if not targets:
        return HookBundleResult(
            outcome=HookOutcome.REJECTED,
            session_id=sid,
            detail="empty_targets_bundle",
        )

    # 3. Run each target through engine + observer.
    concurrency = replay_hook_concurrency()
    semaphore = asyncio.Semaphore(concurrency)

    async def _process_one(target: ReplayTarget) -> HookTargetResult:
        async with semaphore:
            return await _run_and_record(target)

    target_results = await asyncio.gather(
        *(_process_one(t) for t in targets),
        return_exceptions=False,
    )

    # 4. Refresh in-memory baseline. Best-effort — failure here
    # doesn't change the bundle outcome.
    baseline = None
    try:
        baseline = await asyncio.to_thread(compare_recent_history)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_hook] baseline refresh failed: %s", exc,
        )

    # 5. Compose bundle outcome from per-target results.
    return _compose_bundle(
        sid, tuple(target_results), baseline,
    )


async def _run_and_record(target: ReplayTarget) -> HookTargetResult:
    """One target's engine call + persistence step. Defensive
    contract: returns a HookTargetResult on every path, never
    raises."""
    verdict: Optional[ReplayVerdict] = None
    record_outcome: Optional[RecordOutcome] = None
    error_detail = ""
    try:
        verdict = await run_counterfactual_replay(target)
    except Exception as exc:  # noqa: BLE001 — defensive
        error_detail = f"engine_raise:{type(exc).__name__}:{exc}"
        logger.debug(
            "[replay_hook] engine raised for target %s: %s",
            target, exc,
        )

    if verdict is not None:
        try:
            record_outcome = record_replay_verdict(verdict)
        except Exception as exc:  # noqa: BLE001 — defensive
            error_detail = (
                f"{error_detail} | record_raise:"
                f"{type(exc).__name__}:{exc}"
            ).strip(" |")
            logger.debug(
                "[replay_hook] record raised for target %s: %s",
                target, exc,
            )

    return HookTargetResult(
        target=target,
        verdict=verdict,
        record_outcome=record_outcome,
        error_detail=error_detail,
    )


def _compose_bundle(
    session_id: str,
    target_results: Tuple[HookTargetResult, ...],
    baseline: Optional[ComparisonReport],
) -> HookBundleResult:
    """Map per-target results to the closed-taxonomy bundle outcome.

    Decision tree:
      * 0 results → FAILED
      * Every result has verdict=None or PERSIST_ERROR → FAILED
      * At least one actionable + at least one PARTIAL → PARTIAL
      * Every result is actionable → OK
      * Mixed actionable + non-actionable but no PARTIAL → PARTIAL
        (operators see the per-target detail)
    """
    if not target_results:
        return HookBundleResult(
            outcome=HookOutcome.FAILED,
            session_id=session_id,
            target_results=(),
            baseline_report=baseline,
            detail="no_target_results",
        )

    actionable_count = sum(1 for tr in target_results if tr.is_actionable())
    total = len(target_results)

    detail_tokens = [
        f"total_targets={total}",
        f"actionable={actionable_count}",
    ]
    if baseline is not None:
        detail_tokens.append(f"baseline={baseline.outcome.value}")

    if actionable_count == 0:
        return HookBundleResult(
            outcome=HookOutcome.FAILED,
            session_id=session_id,
            target_results=target_results,
            baseline_report=baseline,
            detail=" ".join(detail_tokens) + " no_actionable_replays",
        )

    if actionable_count == total:
        return HookBundleResult(
            outcome=HookOutcome.OK,
            session_id=session_id,
            target_results=target_results,
            baseline_report=baseline,
            detail=" ".join(detail_tokens),
        )

    return HookBundleResult(
        outcome=HookOutcome.PARTIAL,
        session_id=session_id,
        target_results=target_results,
        baseline_report=baseline,
        detail=" ".join(detail_tokens) + " partial_bundle",
    )


# ---------------------------------------------------------------------------
# Cost-contract authority constant (AST-pin target for Slice 5b)
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "HookBundleResult",
    "HookOutcome",
    "HookTargetResult",
    "REPLAY_HOOK_SCHEMA_VERSION",
    "default_replay_policies",
    "record_session_replay",
    "replay_hook_concurrency",
    "replay_hook_enabled",
]

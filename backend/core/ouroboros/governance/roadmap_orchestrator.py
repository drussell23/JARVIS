"""§41.4 Phase 2 — Roadmap-to-Execution Composer
==================================================

**The production composer that chains roadmap_reader →
goal_decomposition_planner → multi_step_orchestrator into one
operator-callable async surface.**

Honest classification of what this closes: PRD §41.4 Phase 1
shipped 9 substrates (2026-05-11). The Phase 2 audit revealed
the **production composition path was unwired** — the only
chained-end-to-end proof was `test_phase2_roadmap_to_goals_
integration.py`'s `CapturingRouter` test harness. No file in
the production tree imported more than one of the three
substrates in sequence. This module IS the missing chain.

Composition contract (operator-binding 2026-05-16):

  * **NO parallel state** — composes the canonical
    :func:`roadmap_reader.process_roadmap` +
    :func:`goal_decomposition_planner.decompose_goal` +
    :func:`multi_step_orchestrator.advance_orchestration`
    surfaces exclusively. NO duplicate decomposer, NO parallel
    completion tracker, NO invented intermediate state.
  * **NO hardcoded triggers** — every caller-tunable knob is an
    env var (timeout, poll interval, max iterations, master
    flag). Production callers pass a real
    :class:`UnifiedIntakeRouter`; tests inject a duck-typed
    capturing router.
  * **NO trust bypass** — the composer drives ``router.ingest()``
    exclusively (the canonical intake surface that gates +
    routes + governs every envelope). Envelopes from
    roadmap_reader / decomposer / orchestrator all flow
    through the same router. No back-door dispatch.
  * **NEVER raises** — every entry point yields a frozen
    :class:`RoadmapExecutionReport` even on parse errors,
    decomposition failures, polling timeouts, or canceled
    coroutines.

The composer is OPERATOR-INITIATED via this module's
:func:`execute_roadmap` function. Autonomous cadence-driven
execution (e.g., orchestration loop polls for new roadmaps
every N minutes) is intentionally deferred per the same
discipline applied to M10 Slice 3 — that's a tier-changing
event that requires explicit operator authorization.

Authority asymmetry (AST-pinned): stdlib + the three §41.4
substrates ONLY. NEVER imports orchestrator / iron_gate /
policy / candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
tool_executor / plan_generator / providers. The composer is a
pure substrate-chainer, not a decision authority.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


ROADMAP_ORCHESTRATOR_SCHEMA_VERSION: str = "roadmap_orchestrator.1"


# ---------------------------------------------------------------------------
# Env knobs — operator-tunable, no hardcoding
# ---------------------------------------------------------------------------


_ENV_MASTER = "JARVIS_ROADMAP_ORCHESTRATOR_ENABLED"
_ENV_POLL_INTERVAL_S = "JARVIS_ROADMAP_ORCHESTRATOR_POLL_INTERVAL_S"
_ENV_MAX_ITERATIONS = "JARVIS_ROADMAP_ORCHESTRATOR_MAX_ITERATIONS"
_ENV_WALL_CLOCK_CAP_S = "JARVIS_ROADMAP_ORCHESTRATOR_WALL_CLOCK_CAP_S"
_ENV_PER_GOAL_TIMEOUT_S = (
    "JARVIS_ROADMAP_ORCHESTRATOR_PER_GOAL_TIMEOUT_S"
)


# §33.1 cognitive substrate — default-FALSE; operator must
# explicitly flip the master flag for production use. The
# composer is autonomy-flavored (drives the intake router with
# emitted envelopes), so the same discipline applied to M10 /
# fast_path_qa applies here.
_DEFAULT_POLL_INTERVAL_S: float = 5.0
_DEFAULT_MAX_ITERATIONS: int = 100
_DEFAULT_WALL_CLOCK_CAP_S: float = 600.0
_DEFAULT_PER_GOAL_TIMEOUT_S: float = 1800.0


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(
    name: str, *, default: float, lo: float, hi: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _env_int(
    name: str, *, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def master_enabled() -> bool:
    """§33.1 master gate. Default-FALSE. NEVER raises."""
    return _env_truthy(_ENV_MASTER, default=False)


def poll_interval_s() -> float:
    """Polling cadence between ``advance_orchestration`` ticks.
    Clamped [0.1, 120.0]. Default 5.0s."""
    return _env_float(
        _ENV_POLL_INTERVAL_S,
        default=_DEFAULT_POLL_INTERVAL_S,
        lo=0.1, hi=120.0,
    )


def max_iterations() -> int:
    """Safety guard on the orchestration polling loop. Clamped
    [1, 100000]. Default 100."""
    return _env_int(
        _ENV_MAX_ITERATIONS,
        default=_DEFAULT_MAX_ITERATIONS,
        lo=1, hi=100_000,
    )


def wall_clock_cap_s() -> float:
    """Per-roadmap wall-clock ceiling. Clamped [10, 86400].
    Default 600s (10 min)."""
    return _env_float(
        _ENV_WALL_CLOCK_CAP_S,
        default=_DEFAULT_WALL_CLOCK_CAP_S,
        lo=10.0, hi=86400.0,
    )


def per_goal_timeout_s() -> float:
    """Per-goal orchestration timeout. Clamped [10, 86400].
    Default 1800s (30 min)."""
    return _env_float(
        _ENV_PER_GOAL_TIMEOUT_S,
        default=_DEFAULT_PER_GOAL_TIMEOUT_S,
        lo=10.0, hi=86400.0,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy verdict
# ---------------------------------------------------------------------------


class RoadmapExecutionVerdict(str, enum.Enum):
    """Closed 7-value taxonomy — bytes-pinned via AST.

    The execution-level verdict the operator sees; aggregates
    across all goal-level outcomes."""

    DISABLED = "disabled"
    """Master flag off — composer returned without firing."""

    NO_ROADMAP = "no_roadmap"
    """roadmap_reader returned NO_ROADMAP (file missing / empty
    / no goals after parse). Pass-through verdict."""

    INVALID_ROADMAP = "invalid_roadmap"
    """roadmap_reader returned INVALID_SIGNATURE or MALFORMED.
    Caller sees the original reader verdict in
    ``roadmap_report.verdict``."""

    DECOMPOSITION_FAILED = "decomposition_failed"
    """At least one goal failed decomposition (cycle / invalid
    DAG / decomposer error). Per-goal status visible in
    ``goal_executions``."""

    ORCHESTRATION_STALLED = "orchestration_stalled"
    """At least one decomposed plan reached STALLED verdict
    (failed sub-goal blocks downstream)."""

    POLLING_EXHAUSTED = "polling_exhausted"
    """max_iterations / wall_clock_cap_s reached before the
    plan completed. Not a failure — partial progress may have
    occurred."""

    COMPLETED = "completed"
    """All goals decomposed AND all plans reached COMPLETED
    verdict. The happy path."""


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalExecutionRecord:
    """Per-goal outcome — decomposition + final orchestration
    verdict + emitted-envelope counts. Frozen for safe
    propagation across substrates."""

    goal_id: str
    title: str = ""
    decomposition_verdict: str = ""
    """``DecompositionVerdict.value`` from goal_decomposition_
    planner. Empty when goal extraction failed before
    decomposition."""

    orchestration_verdict: str = ""
    """``OrchestrationVerdict.value`` from
    multi_step_orchestrator's terminal report. Empty when
    decomposition failed (no plan to orchestrate)."""

    sub_goals_emitted: int = 0
    sub_goals_completed: int = 0
    sub_goals_total: int = 0
    iterations_used: int = 0
    elapsed_s: float = 0.0
    failure_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id[:128],
            "title": self.title[:256],
            "decomposition_verdict": self.decomposition_verdict[:64],
            "orchestration_verdict": self.orchestration_verdict[:64],
            "sub_goals_emitted": int(self.sub_goals_emitted),
            "sub_goals_completed": int(self.sub_goals_completed),
            "sub_goals_total": int(self.sub_goals_total),
            "iterations_used": int(self.iterations_used),
            "elapsed_s": float(self.elapsed_s),
            "failure_reason": str(self.failure_reason)[:512],
        }


@dataclass(frozen=True)
class RoadmapExecutionReport:
    """Aggregate result of one ``execute_roadmap`` invocation.

    Frozen + JSON-projectable so observability surfaces +
    REPL renderers + test assertions consume one canonical
    shape."""

    verdict: RoadmapExecutionVerdict
    roadmap_verdict: str = ""
    """``RoadmapVerdict.value`` from roadmap_reader's report.
    Surfaces the upstream pass-through verdict even when the
    composer's aggregate verdict is INVALID_ROADMAP /
    NO_ROADMAP."""

    goals_processed: int = 0
    """Count of goals roadmap_reader successfully emitted."""

    goal_executions: Tuple[GoalExecutionRecord, ...] = field(
        default_factory=tuple,
    )

    total_iterations: int = 0
    elapsed_s: float = 0.0
    diagnostic: str = ""
    schema_version: str = field(
        default=ROADMAP_ORCHESTRATOR_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "roadmap_verdict": self.roadmap_verdict[:64],
            "goals_processed": int(self.goals_processed),
            "total_iterations": int(self.total_iterations),
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": self.diagnostic[:512],
            "goal_executions": [
                g.to_dict() for g in self.goal_executions
            ],
        }


# ---------------------------------------------------------------------------
# Internal helpers — substrate composition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GoalShim:
    """Duck-typed goal object the canonical decomposer expects.

    The decomposer reads goal_id / title / description /
    target_files / depends_on via getattr — any object with
    those attributes works. We use a frozen dataclass for
    immutability + zero-coupling to roadmap_reader's internal
    Goal type (which is also frozen but private).
    """

    goal_id: str
    title: str = ""
    description: str = ""
    target_files: Tuple[str, ...] = ()
    depends_on: Tuple[str, ...] = ()


def _envelope_to_goal_shim(envelope: Any) -> Optional[_GoalShim]:
    """Project a roadmap-emitted IntentEnvelope into a
    decomposer-compatible goal shim. NEVER raises."""
    try:
        evidence = getattr(envelope, "evidence", {}) or {}
        goal_id = str(evidence.get("goal_id", "")).strip()
        if not goal_id:
            return None
        description = str(
            getattr(envelope, "description", "") or "",
        )
        # roadmap_reader sets description to the goal's title or
        # description; we don't have a clean split. Use the same
        # text for both since the decomposer treats them as one
        # free-text context blob.
        target_files = tuple(
            getattr(envelope, "target_files", ()) or (),
        )
        depends_on = tuple(
            evidence.get("depends_on", ()) or (),
        )
        return _GoalShim(
            goal_id=goal_id,
            title=description[:256],
            description=description,
            target_files=target_files,
            depends_on=depends_on,
        )
    except Exception as err:  # noqa: BLE001 — defensive
        logger.debug(
            "[roadmap_orchestrator] envelope shim failed: %r",
            err,
        )
        return None


def _is_goal_envelope(envelope: Any) -> bool:
    """True iff this is a goal-level envelope (from roadmap_
    reader) and NOT a sub-goal envelope (from decomposer or
    orchestrator). Both carry ``source="roadmap"``; the
    distinguisher is the ``"multi_step_orchestrated"`` /
    ``"sub_goal_id"`` evidence key absence."""
    try:
        if getattr(envelope, "source", "") != "roadmap":
            return False
        evidence = getattr(envelope, "evidence", {}) or {}
        # Sub-goal envelopes (from decomposer or orchestrator)
        # carry sub_goal_id. Goal envelopes carry only goal_id.
        if "sub_goal_id" in evidence:
            return False
        if "multi_step_orchestrated" in evidence:
            return False
        return "goal_id" in evidence
    except Exception:  # noqa: BLE001
        return False


def _classify_aggregate_verdict(
    *,
    roadmap_verdict_value: str,
    goal_records: Tuple[GoalExecutionRecord, ...],
) -> Tuple[RoadmapExecutionVerdict, str]:
    """Compute the aggregate verdict + a diagnostic string.
    NEVER raises."""
    # Upstream pass-throughs.
    if roadmap_verdict_value == "no_roadmap":
        return (
            RoadmapExecutionVerdict.NO_ROADMAP,
            "roadmap_reader returned NO_ROADMAP",
        )
    if roadmap_verdict_value in (
        "invalid_signature", "malformed",
    ):
        return (
            RoadmapExecutionVerdict.INVALID_ROADMAP,
            f"roadmap_reader returned {roadmap_verdict_value!r}",
        )
    if not goal_records:
        return (
            RoadmapExecutionVerdict.NO_ROADMAP,
            "no goals processed",
        )
    # Aggregate across goal_records.
    any_decomp_failed = any(
        r.decomposition_verdict not in (
            "", "decomposed",
        ) and r.orchestration_verdict == ""
        for r in goal_records
    )
    if any_decomp_failed:
        first = next(
            r for r in goal_records
            if r.decomposition_verdict not in (
                "", "decomposed",
            ) and r.orchestration_verdict == ""
        )
        return (
            RoadmapExecutionVerdict.DECOMPOSITION_FAILED,
            (
                f"{first.goal_id!r}: decomposition verdict="
                f"{first.decomposition_verdict!r}"
            ),
        )
    any_stalled = any(
        r.orchestration_verdict == "stalled"
        for r in goal_records
    )
    if any_stalled:
        first = next(
            r for r in goal_records
            if r.orchestration_verdict == "stalled"
        )
        return (
            RoadmapExecutionVerdict.ORCHESTRATION_STALLED,
            (
                f"{first.goal_id!r}: orchestration stalled — "
                f"failed sub-goal blocks downstream"
            ),
        )
    any_exhausted = any(
        r.orchestration_verdict == "progressing"
        for r in goal_records
    )
    if any_exhausted:
        first = next(
            r for r in goal_records
            if r.orchestration_verdict == "progressing"
        )
        return (
            RoadmapExecutionVerdict.POLLING_EXHAUSTED,
            (
                f"{first.goal_id!r}: max_iterations / wall-clock "
                f"reached while plan still PROGRESSING"
            ),
        )
    all_completed = all(
        r.orchestration_verdict == "completed"
        for r in goal_records
    )
    if all_completed:
        return (
            RoadmapExecutionVerdict.COMPLETED,
            f"all {len(goal_records)} goal(s) completed cleanly",
        )
    # Mixed state — surface the dominant non-completion.
    return (
        RoadmapExecutionVerdict.POLLING_EXHAUSTED,
        "mixed orchestration verdicts across goals",
    )


# ---------------------------------------------------------------------------
# Per-goal execution loop
# ---------------------------------------------------------------------------


async def _execute_one_goal(
    goal_shim: _GoalShim,
    *,
    router: Any,
    poll_interval: float,
    max_iters: int,
    per_goal_cap_s: float,
    wall_clock_deadline: Optional[float] = None,
    completion_status_override: Optional[
        Dict[str, str]
    ] = None,
) -> GoalExecutionRecord:
    """Decompose one goal + drive orchestration polling until
    terminal verdict. NEVER raises.

    Wall-clock discipline: both the per-goal cap AND the global
    wall-clock deadline (passed from the composer) bound the
    polling loop. Whichever fires first short-circuits."""
    started = time.monotonic()
    try:
        from backend.core.ouroboros.governance.goal_decomposition_planner import (  # noqa: E501
            decompose_goal,
        )
        from backend.core.ouroboros.governance.multi_step_orchestrator import (  # noqa: E501
            advance_orchestration,
        )
    except Exception as err:  # noqa: BLE001
        return GoalExecutionRecord(
            goal_id=goal_shim.goal_id,
            title=goal_shim.title,
            failure_reason=(
                f"substrate import failed: "
                f"{type(err).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    try:
        verdict, plan, diagnostic = decompose_goal(goal_shim)
    except Exception as err:  # noqa: BLE001
        return GoalExecutionRecord(
            goal_id=goal_shim.goal_id,
            title=goal_shim.title,
            decomposition_verdict="error",
            failure_reason=(
                f"decompose_goal raised: {type(err).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    decomp_verdict_str = (
        verdict.value if hasattr(verdict, "value")
        else str(verdict or "")
    )

    if plan is None:
        return GoalExecutionRecord(
            goal_id=goal_shim.goal_id,
            title=goal_shim.title,
            decomposition_verdict=decomp_verdict_str,
            failure_reason=diagnostic or "no plan",
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    sub_goals_total = len(getattr(plan, "sub_goals", ()) or ())

    # Polling loop — keep calling advance_orchestration until
    # the verdict is terminal (COMPLETED / STALLED) or we hit
    # a guard (max_iters / wall-clock).
    iterations = 0
    orch_verdict_str = ""
    final_report: Any = None
    while iterations < max_iters:
        now_mono = time.monotonic()
        if (now_mono - started) > per_goal_cap_s:
            break
        # Global wall-clock cap also bounds this loop — whichever
        # fires first wins.
        if (
            wall_clock_deadline is not None
            and now_mono > wall_clock_deadline
        ):
            break
        iterations += 1
        try:
            final_report = await advance_orchestration(
                plan,
                router=router,
                completion_status_override=(
                    completion_status_override
                ),
            )
        except Exception as err:  # noqa: BLE001
            return GoalExecutionRecord(
                goal_id=goal_shim.goal_id,
                title=goal_shim.title,
                decomposition_verdict=decomp_verdict_str,
                orchestration_verdict="error",
                iterations_used=iterations,
                sub_goals_total=sub_goals_total,
                failure_reason=(
                    f"advance_orchestration raised: "
                    f"{type(err).__name__}"
                ),
                elapsed_s=max(0.0, time.monotonic() - started),
            )
        orch_verdict = getattr(final_report, "verdict", None)
        orch_verdict_str = (
            orch_verdict.value
            if hasattr(orch_verdict, "value")
            else str(orch_verdict or "")
        )
        if orch_verdict_str in ("completed", "stalled", "no_plan"):
            break
        # PROGRESSING — sleep and re-poll. Skip sleep on the
        # last iteration so the timing budget isn't wasted.
        if iterations < max_iters and poll_interval > 0:
            try:
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                # NEVER raises: surface as a structured outcome
                # so the aggregate verdict reflects cancellation.
                return GoalExecutionRecord(
                    goal_id=goal_shim.goal_id,
                    title=goal_shim.title,
                    decomposition_verdict=decomp_verdict_str,
                    orchestration_verdict=orch_verdict_str,
                    iterations_used=iterations,
                    sub_goals_total=sub_goals_total,
                    failure_reason="cancelled during poll",
                    elapsed_s=max(
                        0.0, time.monotonic() - started,
                    ),
                )

    # Compute emitted + completed counts from the final report.
    emitted = 0
    completed = 0
    try:
        for run_record in (
            getattr(final_report, "run_records", ()) or ()
        ):
            state = getattr(run_record, "state", None)
            state_str = (
                state.value if hasattr(state, "value")
                else str(state or "")
            )
            if state_str == "emitted":
                emitted += 1
            elif state_str == "done":
                completed += 1
                emitted += 1
    except Exception:  # noqa: BLE001
        pass

    return GoalExecutionRecord(
        goal_id=goal_shim.goal_id,
        title=goal_shim.title,
        decomposition_verdict=decomp_verdict_str,
        orchestration_verdict=orch_verdict_str,
        sub_goals_emitted=emitted,
        sub_goals_completed=completed,
        sub_goals_total=sub_goals_total,
        iterations_used=iterations,
        elapsed_s=max(0.0, time.monotonic() - started),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


class _TeeRouter:
    """Internal router that forwards every ``ingest`` to the
    upstream caller-supplied router (when present) AND captures
    each envelope into a bounded list so the composer can
    inspect them for the goal-extraction step.

    This is the structural fix for "external routers don't
    expose what they captured" — by wrapping, the composer
    works with ANY duck-typed ``async ingest`` router
    (UnifiedIntakeRouter, test CapturingRouter, etc.) without
    requiring the upstream to expose a ``.captured`` attribute.

    NEVER raises — upstream router exceptions are caught and
    surfaced as the forwarded ingest's return value (empty
    string on failure).
    """

    def __init__(self, upstream: Optional[Any] = None) -> None:
        self._upstream = upstream
        self.captured: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        # Capture first so a misbehaving upstream can't strip
        # the envelope from our view.
        try:
            self.captured.append(envelope)
        except Exception:  # noqa: BLE001 — defensive
            pass
        # A1-T4 — hop 1/5 (emit): the roadmap orchestrator emits a strategic
        # GOAL into intake. First breadcrumb of the soak proof chain.
        try:
            from backend.core.ouroboros.governance.a1_trace import (  # noqa: PLC0415
                a1trace as _a1trace,
            )
            _a1trace(
                "emit",
                getattr(envelope, "causal_id", None)
                or getattr(envelope, "goal_id", "?"),
                source="roadmap",
            )
        except Exception:  # noqa: BLE001
            pass
        if self._upstream is None:
            # Persist the orphaned envelope so it is never silently dropped.
            # Lazy import avoids any circular-import risk; fail-soft so a DLQ
            # error cannot break the ingest path.
            try:
                from backend.core.ouroboros.governance import (  # noqa: PLC0415
                    intake_dlq as _dlq,
                )
                _dlq.append_dlq(envelope, reason="no_router")
            except Exception:  # noqa: BLE001
                pass
            return "captured"
        try:
            result = await self._upstream.ingest(envelope)
        except Exception as err:  # noqa: BLE001 — defensive
            logger.debug(
                "[roadmap_orchestrator] upstream router "
                "ingest raised: %r", err,
            )
            return ""
        return str(result or "")


async def execute_roadmap(
    yaml_path: Optional[Any] = None,
    *,
    router: Optional[Any] = None,
    secret_override: Optional[str] = None,
    poll_interval_s_override: Optional[float] = None,
    max_iterations_override: Optional[int] = None,
    wall_clock_cap_s_override: Optional[float] = None,
    per_goal_timeout_s_override: Optional[float] = None,
    completion_status_override: Optional[
        Dict[str, str]
    ] = None,
    now_unix: Optional[float] = None,
) -> RoadmapExecutionReport:
    """**The production composer.** Chains the canonical §41.4
    Phase 1 substrates into one operator-callable async surface.

    Pipeline:

      1. :func:`roadmap_reader.process_roadmap` — read + verify
         + parse + emit goal envelopes through ``router.ingest``.
      2. For each emitted goal envelope: project into a
         decomposer-compatible shim, call
         :func:`goal_decomposition_planner.decompose_goal`.
      3. For each decomposed plan: poll
         :func:`multi_step_orchestrator.advance_orchestration`
         until terminal verdict OR ``max_iterations`` / wall-
         clock cap reached.
      4. Aggregate per-goal records + classify final verdict.

    All envelopes flow through the same ``router`` — there's
    NO back-door dispatch. The composer is a pure chainer.

    Parameters
    ----------
    yaml_path:
        Optional path override. Forwarded to roadmap_reader as
        ``path_override``. When None, roadmap_reader resolves
        the canonical default path from its env knob.
    router:
        Operator-supplied :class:`UnifiedIntakeRouter` instance
        (or duck-typed shim with async ``ingest(envelope)``
        signature). When None, an internal capturing shim is
        used — useful for integration tests + dry-run.
    secret_override:
        HMAC secret override forwarded to roadmap_reader.
    poll_interval_s_override / max_iterations_override /
    wall_clock_cap_s_override / per_goal_timeout_s_override:
        Test/operator overrides for the orchestration polling
        loop's caps. None → env-resolved default.
    completion_status_override:
        Test fixture support — passed through to every
        ``advance_orchestration`` call. Production callers
        leave this None (the orchestrator reads the canonical
        goal_decomposition_planner ledger for real completion
        tracking).
    now_unix:
        Time override for deterministic tests.

    Returns
    -------
    :class:`RoadmapExecutionReport`
        Frozen aggregate report. NEVER raises.
    """
    started = time.monotonic()
    if not master_enabled():
        return RoadmapExecutionReport(
            verdict=RoadmapExecutionVerdict.DISABLED,
            diagnostic=(
                f"{_ENV_MASTER}=false (§33.1 default; explicit "
                f"opt-in required for production execution)"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    # Resolve effective tunables.
    eff_poll = (
        float(poll_interval_s_override)
        if poll_interval_s_override is not None
        else poll_interval_s()
    )
    eff_max_iters = (
        int(max_iterations_override)
        if max_iterations_override is not None
        else max_iterations()
    )
    eff_wall = (
        float(wall_clock_cap_s_override)
        if wall_clock_cap_s_override is not None
        else wall_clock_cap_s()
    )
    eff_per_goal = (
        float(per_goal_timeout_s_override)
        if per_goal_timeout_s_override is not None
        else per_goal_timeout_s()
    )

    # Wrap the caller-supplied router in a tee so we can capture
    # envelopes for goal-extraction regardless of whether the
    # upstream exposes a .captured attribute. When router=None
    # the tee runs in pure-capture mode (no upstream forwarding).
    active_router = _TeeRouter(upstream=router)

    # Stage 1: roadmap_reader.
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (  # noqa: E501
            process_roadmap,
        )
    except Exception as err:  # noqa: BLE001
        return RoadmapExecutionReport(
            verdict=RoadmapExecutionVerdict.INVALID_ROADMAP,
            diagnostic=(
                f"roadmap_reader import failed: "
                f"{type(err).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    try:
        roadmap_report = await process_roadmap(
            path_override=(
                Path(yaml_path) if yaml_path is not None
                else None
            ),
            secret_override=secret_override,
            router=active_router,
            now_unix=now_unix,
        )
    except Exception as err:  # noqa: BLE001
        return RoadmapExecutionReport(
            verdict=RoadmapExecutionVerdict.INVALID_ROADMAP,
            diagnostic=(
                f"process_roadmap raised: {type(err).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    roadmap_verdict_attr = getattr(
        roadmap_report, "verdict", None,
    )
    roadmap_verdict_str = (
        roadmap_verdict_attr.value
        if hasattr(roadmap_verdict_attr, "value")
        else str(roadmap_verdict_attr or "")
    )

    # Stage 1.5: extract goal envelopes from the router.
    captured_envelopes: List[Any] = list(
        getattr(active_router, "captured", []) or [],
    )
    goal_envelopes = [
        env for env in captured_envelopes
        if _is_goal_envelope(env)
    ]

    if not goal_envelopes:
        verdict, diag = _classify_aggregate_verdict(
            roadmap_verdict_value=roadmap_verdict_str,
            goal_records=(),
        )
        return RoadmapExecutionReport(
            verdict=verdict,
            roadmap_verdict=roadmap_verdict_str,
            goals_processed=0,
            diagnostic=diag,
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    # Stage 2 + 3: decompose + orchestrate each goal.
    goal_records: List[GoalExecutionRecord] = []
    total_iters = 0
    deadline = started + eff_wall
    for env in goal_envelopes:
        if time.monotonic() > deadline:
            # Surface remaining goals as not-yet-attempted.
            shim = _envelope_to_goal_shim(env)
            gid = (
                shim.goal_id if shim is not None
                else str(
                    (getattr(env, "evidence", {}) or {})
                    .get("goal_id", "<unknown>"),
                )
            )
            goal_records.append(GoalExecutionRecord(
                goal_id=gid,
                failure_reason=(
                    "wall-clock cap exhausted before goal start"
                ),
            ))
            continue
        shim = _envelope_to_goal_shim(env)
        if shim is None:
            goal_records.append(GoalExecutionRecord(
                goal_id="<envelope projection failed>",
                failure_reason=(
                    "envelope→goal shim projection returned None"
                ),
            ))
            continue
        rec = await _execute_one_goal(
            shim,
            router=active_router,
            poll_interval=eff_poll,
            max_iters=eff_max_iters,
            per_goal_cap_s=eff_per_goal,
            wall_clock_deadline=deadline,
            completion_status_override=(
                completion_status_override
            ),
        )
        goal_records.append(rec)
        total_iters += rec.iterations_used

    verdict, diag = _classify_aggregate_verdict(
        roadmap_verdict_value=roadmap_verdict_str,
        goal_records=tuple(goal_records),
    )
    return RoadmapExecutionReport(
        verdict=verdict,
        roadmap_verdict=roadmap_verdict_str,
        goals_processed=len(goal_envelopes),
        goal_executions=tuple(goal_records),
        total_iterations=total_iters,
        diagnostic=diag,
        elapsed_s=max(0.0, time.monotonic() - started),
    )


# ===========================================================================
# §33.1 — register_shipped_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    """Roadmap-orchestrator substrate invariants. AST pins
    enforce the composition contract + authority asymmetry +
    closed-verdict taxonomy."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/roadmap_orchestrator.py"
    )

    _FORBIDDEN_IMPORTS = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.policy_engine",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.urgency_router",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.auto_committer",
        "backend.core.ouroboros.governance.risk_tier_floor",
        "backend.core.ouroboros.governance.tool_executor",
        "backend.core.ouroboros.governance.plan_generator",
        "backend.core.ouroboros.governance.providers",
    )

    _EXPECTED_VERDICTS = frozenset({
        "disabled",
        "no_roadmap",
        "invalid_roadmap",
        "decomposition_failed",
        "orchestration_stalled",
        "polling_exhausted",
        "completed",
    })

    def _validate_verdict_taxonomy(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Bytes-pin the closed 7-value verdict enum."""
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "RoadmapExecutionVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, _ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], _ast.Name)
                        and isinstance(sub.value, _ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                if found != _EXPECTED_VERDICTS:
                    return (
                        f"RoadmapExecutionVerdict drift: "
                        f"got={sorted(found)} "
                        f"expected={sorted(_EXPECTED_VERDICTS)}",
                    )
                return ()
        return ("RoadmapExecutionVerdict class not found",)

    def _validate_composes_canonical(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        """Composer MUST chain all 3 canonical §41.4 substrates."""
        violations: list = []
        for needle in (
            "roadmap_reader",
            "process_roadmap",
            "goal_decomposition_planner",
            "decompose_goal",
            "multi_step_orchestrator",
            "advance_orchestration",
        ):
            if needle not in source:
                violations.append(
                    f"must compose canonical {needle!r}"
                )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in _FORBIDDEN_IMPORTS:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden import {mod!r}"
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """§33.1 — master gate default-FALSE bytes-pin."""
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in _ast.walk(node):
                    if (
                        isinstance(sub, _ast.Call)
                        and isinstance(sub.func, _ast.Name)
                        and sub.func.id == "_env_truthy"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(
                                    kw.value, _ast.Constant,
                                )
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _env_truthy(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_orchestrator_verdict_taxonomy"
            ),
            target_file=target,
            description=(
                "Closed 7-value RoadmapExecutionVerdict bytes-"
                "pinned. New values require explicit scope-doc "
                "+ AST pin update."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_orchestrator_composes_canonical"
            ),
            target_file=target,
            description=(
                "Composer chains canonical roadmap_reader + "
                "goal_decomposition_planner + multi_step_"
                "orchestrator. NO parallel substrate."
            ),
            validate=_validate_composes_canonical,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_orchestrator_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Composer MUST NOT import orchestrator / "
                "iron_gate / policy / etc. Pure substrate-"
                "chainer, not a decision authority."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_orchestrator_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate — master flag "
                "default-FALSE. Production callers must "
                "explicitly opt in."
            ),
            validate=_validate_master_default_false,
        ),
    ]


__all__ = [
    "ROADMAP_ORCHESTRATOR_SCHEMA_VERSION",
    "GoalExecutionRecord",
    "RoadmapExecutionReport",
    "RoadmapExecutionVerdict",
    "execute_roadmap",
    "master_enabled",
    "max_iterations",
    "per_goal_timeout_s",
    "poll_interval_s",
    "register_shipped_invariants",
    "wall_clock_cap_s",
]

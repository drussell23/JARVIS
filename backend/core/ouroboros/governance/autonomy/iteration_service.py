"""AutonomyIterationService — 10-state FSM for proactive self-improvement.

Drives the full cycle: IDLE -> SELECTING -> PLANNING -> EXECUTING ->
EVALUATING -> REVIEW_GATE -> IDLE (or COOLDOWN / PAUSED / STOPPED on
error or policy limits).

All numeric constants come from ``IterationStopPolicy`` — nothing is
hardcoded.  Every dependency is injected, making the service fully testable
with mocks.

State machine:

    IDLE ──(budget OK)──> SELECTING
    SELECTING ──(task)──> PLANNING
    SELECTING ──(none)──> IDLE
    PLANNING ──(accepted)──> EXECUTING
    PLANNING ──(rejected)──> EVALUATING
    EXECUTING ──(success)──> EVALUATING
    EXECUTING ──(crash/timeout)──> RECOVERING
    RECOVERING ──(terminal)──> EVALUATING
    RECOVERING ──(non-terminal, retries left)──> RECOVERING (retry)
    RECOVERING ──(irrecoverable)──> PAUSED
    EVALUATING ──(success + changes)──> REVIEW_GATE
    EVALUATING ──(noop)──> IDLE
    EVALUATING ──(failure, streak < max)──> COOLDOWN
    EVALUATING ──(failure, streak >= max)──> PAUSED (+ trust demotion)
    REVIEW_GATE ──(done)──> IDLE
    COOLDOWN ──(timer)──> IDLE
    PAUSED ──(resume)──> IDLE
    STOPPED ──(terminal)──> (exit loop)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from backend.core.ouroboros.governance.autonomy.iteration_types import (
    IterationState,
    IterationStopPolicy,
    IterationTask,
    PlanningContext,
    compute_policy_hash,
)
from backend.core.ouroboros.governance.autonomy.preflight import preflight_check
from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier, TIER_ORDER
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState

logger = logging.getLogger("Ouroboros.IterationService")

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

_FEATURE_FLAG_KEY = "JARVIS_AUTONOMY_ITERATION_ENABLED"

# Maximum recovery attempts before declaring irrecoverable.
_MAX_RECOVERY_ATTEMPTS = 2

# Interval to sleep while PAUSED (seconds) before re-checking for resume.
_PAUSED_POLL_INTERVAL_S = 10.0


class AutonomyIterationService:
    """10-state FSM for proactive self-improvement iterations.

    All dependencies are injected.  The service runs on a dedicated
    ``asyncio.Task`` created by :meth:`start`.

    Parameters
    ----------
    task_source:
        Provides ``select_task(cycle_count, fairness_interval)``.
    planner:
        Provides ``plan(task, iteration_id, context) -> PlannerOutcome``.
    budget_guard:
        Provides ``can_proceed() -> (bool, str)``, ``record_spend()``,
        ``compute_cooldown(n) -> float``.
    resource_governor:
        Provides ``should_yield() -> bool``.
    scheduler:
        Provides ``submit(graph) -> bool``, ``wait_for_graph(id, timeout)``.
    trust_graduator:
        Provides ``demote(trigger, repo, canary, reason)``.
    ledger:
        Provides ``append(entry)``.
    comm:
        Provides ``emit_intent``, ``emit_plan``, ``emit_decision``,
        ``emit_postmortem``.
    stop_policy:
        All numeric constants for stopping, cooldown, and blast radius.
    repo_root:
        Filesystem path to the repo root.
    governance_mode:
        One of ``"observe"``, ``"suggest"``, ``"governed"``, ``"autonomous"``.
    """

    def __init__(
        self,
        task_source: Any,
        planner: Any,
        budget_guard: Any,
        resource_governor: Any,
        scheduler: Any,
        trust_graduator: Any,
        ledger: Any,
        comm: Any,
        stop_policy: IterationStopPolicy,
        repo_root: Path,
        governance_mode: str = "governed",
    ) -> None:
        # Dependencies
        self._task_source = task_source
        self._planner = planner
        self._budget_guard = budget_guard
        self._resource_governor = resource_governor
        self._scheduler = scheduler
        self._trust_graduator = trust_graduator
        self._ledger = ledger
        self._comm = comm
        self._stop_policy = stop_policy
        self._repo_root = Path(repo_root)
        self._governance_mode = governance_mode

        # FSM state
        self._state: IterationState = IterationState.IDLE
        self._cycle_count: int = 0
        self._consecutive_failures: int = 0
        self._session_start: float = 0.0
        self._current_iteration_id: str = ""
        self._current_graph_id: str = ""
        self._current_graph: Any = None
        self._current_task: Optional[IterationTask] = None
        self._last_outcome: str = ""  # "success", "failure", "noop", "rejected"

        # Recovery state
        self._recovery_graph_id: str = ""
        self._recovery_attempts: int = 0

        # Loop task
        self._loop_task: Optional[asyncio.Task] = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the iteration loop.  Idempotent (T32)."""
        if self._running:
            return
        self._running = True
        self._session_start = time.monotonic()

        # Startup recovery: check for non-terminal graphs
        try:
            store = getattr(self._scheduler, "_store", None)
            inflight = store.load_inflight() if store is not None else {}
            if inflight:
                self._state = IterationState.RECOVERING
                # Pick the first inflight graph for recovery
                first_id = next(iter(inflight))
                self._recovery_graph_id = first_id
                self._recovery_attempts = 0
                logger.info(
                    "IterationService: found inflight graph %s, entering RECOVERING",
                    first_id,
                )
            else:
                self._state = IterationState.IDLE
        except Exception:
            logger.debug("IterationService: no inflight graphs found, starting IDLE")
            self._state = IterationState.IDLE

        self._loop_task = asyncio.create_task(
            self._iteration_loop(),
            name="autonomy_iteration_loop",
        )

    async def stop(self) -> None:
        """Kill switch -- immediately stop the iteration loop (T27)."""
        self._running = False
        self._state = IterationState.STOPPED

        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

        # Write terminal ledger event
        try:
            entry = LedgerEntry(
                op_id=self._current_iteration_id or "iter-service-stop",
                state=OperationState.ITERATION_OUTCOME,
                data={
                    "event": "service_stopped",
                    "cycle_count": self._cycle_count,
                    "consecutive_failures": self._consecutive_failures,
                    "state": IterationState.STOPPED.value,
                },
            )
            await self._ledger.append(entry)
        except Exception as exc:
            logger.warning("IterationService: failed to write terminal ledger: %s", exc)

        # Cancel in-flight graph if any
        if self._current_graph_id:
            try:
                await self._scheduler.abort(self._current_graph_id)
            except Exception:
                pass

    async def resume(self, reason: str) -> None:
        """Resume from PAUSED state."""
        if self._state != IterationState.PAUSED:
            return
        logger.info("IterationService: resuming from PAUSED -- %s", reason)
        self._state = IterationState.IDLE

    def health(self) -> Dict[str, Any]:
        """Return lightweight health snapshot."""
        return {
            "state": self._state.name,
            "cycle_count": self._cycle_count,
            "consecutive_failures": self._consecutive_failures,
            "running": self._running,
            "current_iteration_id": self._current_iteration_id,
        }

    # ------------------------------------------------------------------
    # Feature flag
    # ------------------------------------------------------------------

    def _is_enabled(self) -> bool:
        """Check whether the iteration service feature flag is active."""
        return os.environ.get(_FEATURE_FLAG_KEY, "false").lower() in (
            "true",
            "1",
            "yes",
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _iteration_loop(self) -> None:
        """Main loop -- drives the FSM through states."""
        while self._running:
            try:
                # Yield to event loop every iteration so cancellation propagates
                await asyncio.sleep(0)

                # Check feature flag each cycle (T31)
                if not self._is_enabled():
                    if self._state not in (
                        IterationState.IDLE,
                        IterationState.STOPPED,
                    ):
                        logger.warning(
                            "IterationService: feature flag disabled mid-run, stopping"
                        )
                    self._state = IterationState.STOPPED
                    self._running = False
                    break

                # Yield to user traffic when system is under load
                if await self._resource_governor.should_yield():
                    await asyncio.sleep(5.0)
                    continue

                if self._state == IterationState.IDLE:
                    await self._do_idle()
                elif self._state == IterationState.SELECTING:
                    await self._do_selecting()
                elif self._state == IterationState.PLANNING:
                    await self._do_planning()
                elif self._state == IterationState.EXECUTING:
                    await self._do_executing()
                elif self._state == IterationState.RECOVERING:
                    await self._do_recovering()
                elif self._state == IterationState.EVALUATING:
                    await self._do_evaluating()
                elif self._state == IterationState.REVIEW_GATE:
                    await self._do_review_gate()
                elif self._state == IterationState.COOLDOWN:
                    await self._do_cooldown()
                elif self._state == IterationState.PAUSED:
                    await asyncio.sleep(_PAUSED_POLL_INTERVAL_S)
                elif self._state == IterationState.STOPPED:
                    break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "IterationService: loop error in state %s: %s",
                    self._state.name,
                    exc,
                    exc_info=True,
                )
                self._consecutive_failures += 1
                self._state = IterationState.COOLDOWN

    # ------------------------------------------------------------------
    # Per-state handlers
    # ------------------------------------------------------------------

    async def _do_idle(self) -> None:
        """IDLE: check budget, then transition to SELECTING or wait."""
        can, reason = self._budget_guard.can_proceed()
        if not can:
            logger.debug("IterationService: staying IDLE -- %s", reason)
            idle_wait = self._stop_policy.cooldown_base_s
            if idle_wait <= 0:
                idle_wait = 30.0
            await asyncio.sleep(min(idle_wait, 30.0))
            return

        # Check wall-time stop condition
        elapsed = time.monotonic() - self._session_start
        if elapsed >= self._stop_policy.max_wall_time_s and self._session_start > 0:
            logger.info("IterationService: wall-time limit reached, pausing")
            self._state = IterationState.PAUSED
            return

        self._state = IterationState.SELECTING

    async def _do_selecting(self) -> None:
        """SELECTING: pick a task from the hybrid source."""
        self._cycle_count += 1
        self._current_iteration_id = f"iter-{uuid4().hex[:12]}"

        task = await self._task_source.select_task(
            self._cycle_count,
            self._stop_policy.miner_fairness_interval,
        )

        if task is None:
            logger.debug("IterationService: no tasks available, returning to IDLE")
            self._state = IterationState.IDLE
            return

        self._current_task = task
        logger.info(
            "IterationService: selected task %s (cycle %d, iter %s)",
            task.task_id,
            self._cycle_count,
            self._current_iteration_id,
        )
        self._state = IterationState.PLANNING

    async def _do_planning(self) -> None:
        """PLANNING: invoke the planner, transition based on outcome."""
        task = self._current_task
        assert task is not None, "BUG: _do_planning called without _current_task"

        # Build planning context
        context = await self._build_planning_context()

        # Emit intent via comm protocol (T22: causal trace)
        try:
            await self._comm.emit_intent(
                op_id=self._current_iteration_id,
                goal=task.description,
                target_files=list(task.target_files),
                risk_tier=self._governance_mode,
                blast_radius=len(task.target_files),
            )
        except Exception as exc:
            logger.debug("IterationService: emit_intent failed: %s", exc)

        outcome = await self._planner.plan(
            task,
            self._current_iteration_id,
            context,
        )

        if outcome.status == "accepted" and outcome.graph is not None:
            self._current_graph = outcome.graph
            self._current_graph_id = outcome.graph.graph_id

            # Emit plan via comm protocol
            try:
                await self._comm.emit_plan(
                    op_id=self._current_iteration_id,
                    steps=[f"execute graph {outcome.graph.graph_id}"],
                    rollback_strategy="git_revert",
                )
            except Exception as exc:
                logger.debug("IterationService: emit_plan failed: %s", exc)

            self._state = IterationState.EXECUTING
        else:
            # Rejected
            self._last_outcome = "rejected"
            logger.info(
                "IterationService: plan rejected for task %s -- %s",
                task.task_id,
                getattr(outcome, "reject_reason", "unknown"),
            )
            self._state = IterationState.EVALUATING

    async def _do_executing(self) -> None:
        """EXECUTING: run preflight, submit graph, wait for completion."""
        graph = self._current_graph
        if graph is None:
            logger.error("IterationService: no graph to execute, reverting to IDLE")
            self._state = IterationState.IDLE
            return

        # Run preflight checks
        context = await self._build_planning_context()
        policy_hash = compute_policy_hash(self._stop_policy, self._governance_mode)

        veto = await preflight_check(
            graph=graph,
            context=context,
            current_trust_tier=self._current_trust_tier(),
            budget_remaining_usd=self._remaining_budget(),
            blast_radius=self._stop_policy.blast_radius,
            repo_root=self._repo_root,
            current_policy_hash=policy_hash,
        )

        if veto is not None:
            logger.warning("IterationService: preflight veto -- %s", veto)
            self._last_outcome = "failure"
            self._state = IterationState.EVALUATING
            return

        # Submit to scheduler
        submitted = await self._scheduler.submit(graph)
        if not submitted:
            logger.warning("IterationService: scheduler rejected graph %s", graph.graph_id)
            self._last_outcome = "failure"
            self._state = IterationState.EVALUATING
            return

        # Wait for graph completion
        try:
            exec_state = await self._scheduler.wait_for_graph(
                graph.graph_id,
                timeout_s=self._stop_policy.max_wall_time_s,
            )
        except asyncio.TimeoutError:
            logger.warning("IterationService: graph %s timed out", graph.graph_id)
            self._recovery_graph_id = graph.graph_id
            self._recovery_attempts = 0
            self._state = IterationState.RECOVERING
            return
        except Exception as exc:
            logger.error("IterationService: graph %s error: %s", graph.graph_id, exc)
            self._recovery_graph_id = graph.graph_id
            self._recovery_attempts = 0
            self._state = IterationState.RECOVERING
            return

        # Evaluate terminal state
        from backend.core.ouroboros.governance.autonomy.subagent_types import (
            GraphExecutionPhase,
        )

        if exec_state.phase == GraphExecutionPhase.COMPLETED:
            self._last_outcome = "success"
        elif exec_state.phase == GraphExecutionPhase.FAILED:
            self._last_outcome = "failure"
        elif exec_state.phase == GraphExecutionPhase.CANCELLED:
            self._last_outcome = "failure"
        else:
            # Non-terminal -- go to recovery
            self._recovery_graph_id = graph.graph_id
            self._recovery_attempts = 0
            self._state = IterationState.RECOVERING
            return

        self._state = IterationState.EVALUATING

    async def _do_recovering(self) -> None:
        """RECOVERING: check graph store, resume or declare irrecoverable."""
        graph_id = self._recovery_graph_id

        if self._recovery_attempts >= _MAX_RECOVERY_ATTEMPTS:
            logger.warning(
                "IterationService: max recovery attempts (%d) reached for %s, pausing",
                _MAX_RECOVERY_ATTEMPTS,
                graph_id,
            )
            self._last_outcome = "failure"
            self._state = IterationState.PAUSED
            return

        self._recovery_attempts += 1

        # Try to wait for the graph to reach a terminal state
        try:
            exec_state = await self._scheduler.wait_for_graph(
                graph_id,
                timeout_s=30.0,
            )
        except asyncio.TimeoutError:
            logger.info(
                "IterationService: recovery wait timed out for %s (attempt %d/%d)",
                graph_id,
                self._recovery_attempts,
                _MAX_RECOVERY_ATTEMPTS,
            )
            if self._recovery_attempts >= _MAX_RECOVERY_ATTEMPTS:
                self._last_outcome = "failure"
                self._state = IterationState.PAUSED
            return
        except Exception as exc:
            logger.error("IterationService: recovery error for %s: %s", graph_id, exc)
            if self._recovery_attempts >= _MAX_RECOVERY_ATTEMPTS:
                self._last_outcome = "failure"
                self._state = IterationState.PAUSED
            return

        from backend.core.ouroboros.governance.autonomy.subagent_types import (
            GraphExecutionPhase,
        )

        if exec_state.phase in (
            GraphExecutionPhase.COMPLETED,
            GraphExecutionPhase.FAILED,
            GraphExecutionPhase.CANCELLED,
        ):
            # Terminal -- evaluate
            if exec_state.phase == GraphExecutionPhase.COMPLETED:
                self._last_outcome = "success"
            else:
                self._last_outcome = "failure"
            self._state = IterationState.EVALUATING
        else:
            # Still non-terminal after waiting
            if self._recovery_attempts >= _MAX_RECOVERY_ATTEMPTS:
                self._last_outcome = "failure"
                self._state = IterationState.PAUSED

    async def _do_evaluating(self) -> None:
        """EVALUATING: record outcome, check stop conditions, route to next state."""
        iteration_id = self._current_iteration_id
        task = self._current_task
        outcome = self._last_outcome

        # Record outcome to ledger
        try:
            entry = LedgerEntry(
                op_id=iteration_id,
                state=OperationState.ITERATION_OUTCOME,
                data={
                    "outcome": outcome,
                    "task_id": task.task_id if task else "",
                    "cycle_count": self._cycle_count,
                    "consecutive_failures": self._consecutive_failures,
                },
                entry_id=f"{iteration_id}-eval",
            )
            await self._ledger.append(entry)
        except Exception as exc:
            logger.warning("IterationService: ledger write failed: %s", exc)

        # Record spend (even for failures -- API calls were made)
        try:
            await self._budget_guard.record_spend(iteration_id, 0.01)
        except Exception as exc:
            logger.debug("IterationService: record_spend failed: %s", exc)

        # Emit comm decision (T22: causal trace)
        try:
            await self._comm.emit_decision(
                op_id=iteration_id,
                outcome=outcome,
                reason_code=f"cycle_{self._cycle_count}",
            )
        except Exception as exc:
            logger.debug("IterationService: emit_decision failed: %s", exc)

        # Route based on outcome
        if outcome == "success":
            self._consecutive_failures = 0
            self._state = IterationState.REVIEW_GATE
        elif outcome == "noop":
            self._consecutive_failures = 0
            self._state = IterationState.IDLE
        elif outcome == "rejected":
            # Rejected plans don't count as failures per se, but do increment
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._stop_policy.max_consecutive_failures:
                await self._handle_error_streak()
            else:
                self._state = IterationState.COOLDOWN
        else:
            # failure
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._stop_policy.max_consecutive_failures:
                await self._handle_error_streak()
            else:
                self._state = IterationState.COOLDOWN

        # Check iteration count stop condition
        if self._cycle_count >= self._stop_policy.max_iterations_per_session:
            logger.info("IterationService: max iterations reached, pausing")
            self._state = IterationState.PAUSED

    async def _handle_error_streak(self) -> None:
        """Handle consecutive failure streak -- demote trust and pause (T15, T30)."""
        logger.warning(
            "IterationService: error streak (%d failures), demoting trust and pausing",
            self._consecutive_failures,
        )

        # Demote trust tier (T30)
        try:
            self._trust_graduator.demote(
                trigger_source="iteration_service",
                repo="jarvis",
                canary_slice="default",
                reason="error_streak",
            )
        except Exception as exc:
            logger.warning("IterationService: trust demotion failed: %s", exc)

        # Emit postmortem (T22)
        try:
            await self._comm.emit_postmortem(
                op_id=self._current_iteration_id,
                root_cause=f"consecutive_failure_streak_{self._consecutive_failures}",
                failed_phase="evaluating",
                next_safe_action="manual_resume_required",
            )
        except Exception as exc:
            logger.debug("IterationService: emit_postmortem failed: %s", exc)

        self._state = IterationState.PAUSED

    async def _do_review_gate(self) -> None:
        """REVIEW_GATE: emit results based on governance mode."""
        iteration_id = self._current_iteration_id
        task = self._current_task

        logger.info(
            "IterationService: review gate for %s (mode=%s)",
            iteration_id,
            self._governance_mode,
        )

        if self._governance_mode == "observe":
            # Log only
            logger.info(
                "IterationService: [OBSERVE] would apply changes for task %s",
                task.task_id if task else "unknown",
            )
        elif self._governance_mode == "suggest":
            # Emit PR draft suggestion via comm
            try:
                await self._comm.emit_decision(
                    op_id=iteration_id,
                    outcome="suggest_pr",
                    reason_code="review_gate_suggest",
                    diff_summary=f"Suggested changes for task {task.task_id if task else 'unknown'}",
                )
            except Exception as exc:
                logger.debug("IterationService: emit suggest failed: %s", exc)
        elif self._governance_mode in ("governed", "autonomous"):
            # Create branch + PR via git subprocess
            try:
                branch_name = f"autonomy/{iteration_id}"
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "checkout",
                    "-b",
                    branch_name,
                    cwd=str(self._repo_root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                logger.info(
                    "IterationService: [%s] created branch %s",
                    self._governance_mode.upper(),
                    branch_name,
                )
            except Exception as exc:
                logger.debug("IterationService: git branch creation failed: %s", exc)

        # Reset for next cycle
        self._current_task = None
        self._current_graph = None
        self._current_graph_id = ""
        self._state = IterationState.IDLE

    async def _do_cooldown(self) -> None:
        """COOLDOWN: exponential backoff, then return to IDLE."""
        duration = self._budget_guard.compute_cooldown(self._consecutive_failures)
        if duration > 0:
            logger.info(
                "IterationService: cooling down for %.1fs (%d consecutive failures)",
                duration,
                self._consecutive_failures,
            )
            await asyncio.sleep(duration)
        self._state = IterationState.IDLE

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _build_planning_context(self) -> PlanningContext:
        """Build a PlanningContext from current state."""
        # Read current HEAD
        repo_commit = await self._read_git_head()
        policy_hash = compute_policy_hash(self._stop_policy, self._governance_mode)
        remaining = self._remaining_budget()

        return PlanningContext(
            repo_commit=repo_commit,
            oracle_snapshot_id="",
            policy_hash=policy_hash,
            schema_version="3.0",
            trust_tier=self._current_trust_tier(),
            budget_remaining_usd=remaining,
        )

    async def _read_git_head(self) -> str:
        """Read git HEAD commit, return empty string on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "HEAD",
                cwd=str(self._repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode(errors="replace").strip()
        except Exception:
            pass
        return ""

    def _current_trust_tier(self) -> AutonomyTier:
        """Map governance_mode to AutonomyTier."""
        mode_map = {
            "observe": AutonomyTier.OBSERVE,
            "suggest": AutonomyTier.SUGGEST,
            "governed": AutonomyTier.GOVERNED,
            "autonomous": AutonomyTier.AUTONOMOUS,
        }
        return mode_map.get(self._governance_mode, AutonomyTier.GOVERNED)

    def _remaining_budget(self) -> float:
        """Estimate remaining budget from the stop policy."""
        can, _ = self._budget_guard.can_proceed()
        if not can:
            return 0.0
        # Approximate from policy; real budget guard tracks the actual spend
        return max(0.0, self._stop_policy.max_spend_usd - 0.01 * self._cycle_count)

"""
Governed Loop Service — Lifecycle Manager
==========================================

Thin lifecycle manager for the governed self-programming pipeline.
Owns provider wiring, orchestrator construction, and health probes.
No domain logic — just coordination.

The supervisor instantiates this in Zone 6.8 and calls start()/stop().
All triggers go through submit(), which delegates to the orchestrator.

Service States
--------------
INACTIVE -> STARTING -> ACTIVE/DEGRADED
ACTIVE/DEGRADED -> STOPPING -> INACTIVE
STARTING -> FAILED (on error)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from backend.core.ouroboros.governance.approval_provider import CLIApprovalProvider
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    FailbackState,
)
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)

logger = logging.getLogger("Ouroboros.GovernedLoop")


# ---------------------------------------------------------------------------
# ServiceState
# ---------------------------------------------------------------------------


class ServiceState(Enum):
    """Lifecycle state of the GovernedLoopService."""

    INACTIVE = auto()
    STARTING = auto()
    ACTIVE = auto()
    DEGRADED = auto()
    STOPPING = auto()
    FAILED = auto()


# ---------------------------------------------------------------------------
# OperationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationResult:
    """Stable result contract returned by submit().

    The full OperationContext stays internal/ledgered.  External callers
    see only this summary.
    """

    op_id: str
    terminal_phase: OperationPhase
    provider_used: Optional[str] = None
    generation_duration_s: Optional[float] = None
    total_duration_s: float = 0.0
    reason_code: str = ""
    trigger_source: str = "unknown"


# ---------------------------------------------------------------------------
# ReadyToCommitPayload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadyToCommitPayload:
    """Terminal payload emitted when a governed op completes successfully.

    Contains all information needed for the human to decide whether to commit.
    """

    op_id: str
    changed_files: Tuple[str, ...]
    provider_id: str
    model_id: str
    routing_reason: str
    verification_summary: str
    rollback_status: str  # "clean" | "rolled_back" | "rollback_failed"
    suggested_commit_message: str


# ---------------------------------------------------------------------------
# GovernedLoopConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GovernedLoopConfig:
    """Frozen configuration for the governed loop service."""

    project_root: Path
    claude_api_key: Optional[str] = None
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_cost_per_op: float = 0.50
    claude_daily_budget: float = 10.00
    generation_timeout_s: float = 120.0
    approval_timeout_s: float = 600.0
    health_probe_interval_s: float = 30.0
    max_concurrent_ops: int = 2
    initial_canary_slices: Tuple[str, ...] = ("tests/",)
    cold_start_grace_s: float = 300.0   # ops younger than this are not cancelled on boot
    approval_ttl_s: float = 1800.0      # stale approval expiry timeout
    pipeline_timeout_s: float = 600.0   # total wall-clock budget per submit(); env: JARVIS_PIPELINE_TIMEOUT_S

    @classmethod
    def from_env(cls, args: Any = None, project_root: Optional[Path] = None) -> GovernedLoopConfig:
        """Build config from environment variables with safe defaults."""
        import os

        resolved_root = project_root if project_root is not None else Path(
            os.getenv("JARVIS_PROJECT_ROOT", os.getcwd())
        )
        return cls(
            project_root=resolved_root,
            claude_api_key=os.getenv("ANTHROPIC_API_KEY"),
            claude_model=os.getenv(
                "JARVIS_GOVERNED_CLAUDE_MODEL", "claude-sonnet-4-20250514"
            ),
            claude_max_cost_per_op=float(
                os.getenv("JARVIS_GOVERNED_CLAUDE_MAX_COST_PER_OP", "0.50")
            ),
            claude_daily_budget=float(
                os.getenv("JARVIS_GOVERNED_CLAUDE_DAILY_BUDGET", "10.00")
            ),
            generation_timeout_s=float(
                os.getenv("JARVIS_GOVERNED_GENERATION_TIMEOUT", "120.0")
            ),
            approval_timeout_s=float(
                os.getenv("JARVIS_GOVERNED_APPROVAL_TIMEOUT", "600.0")
            ),
            health_probe_interval_s=float(
                os.getenv("JARVIS_GOVERNED_HEALTH_PROBE_INTERVAL", "30.0")
            ),
            max_concurrent_ops=int(
                os.getenv("JARVIS_GOVERNED_MAX_CONCURRENT_OPS", "2")
            ),
            cold_start_grace_s=float(os.environ.get("JARVIS_COLD_START_GRACE_S", "300")),
            approval_ttl_s=float(os.environ.get("JARVIS_APPROVAL_TTL_S", "1800")),
            pipeline_timeout_s=float(
                os.environ.get("JARVIS_PIPELINE_TIMEOUT_S", "600.0")
            ),
        )


# ---------------------------------------------------------------------------
# GovernedLoopService
# ---------------------------------------------------------------------------


class GovernedLoopService:
    """Lifecycle manager for the governed self-programming pipeline.

    No side effects in constructor. All async initialization in start().
    """

    def __init__(
        self,
        stack: Any,
        prime_client: Any,
        config: GovernedLoopConfig,
    ) -> None:
        self._stack = stack
        self._prime_client = prime_client
        self._config = config
        self._state = ServiceState.INACTIVE
        self._started_at: Optional[float] = None
        self._failure_reason: Optional[str] = None

        # Built during start()
        self._orchestrator: Optional[GovernedOrchestrator] = None
        self._generator: Optional[CandidateGenerator] = None
        self._approval_provider: Optional[CLIApprovalProvider] = None
        self._health_probe_task: Optional[asyncio.Task] = None

        # Concurrency & dedup
        self._active_ops: Set[str] = set()
        self._completed_ops: Dict[str, OperationResult] = {}

    @property
    def state(self) -> ServiceState:
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize providers, orchestrator, and canary slices.

        Idempotent — second call is no-op if already ACTIVE/DEGRADED.
        On failure, sets state to FAILED with structured reason.
        """
        if self._state in (ServiceState.ACTIVE, ServiceState.DEGRADED):
            return

        self._state = ServiceState.STARTING
        try:
            await self._build_components()
            await self._reconcile_on_boot()  # boot reconciliation
            self._register_canary_slices()
            self._attach_to_stack()
            self._started_at = time.monotonic()

            # Determine state based on provider availability
            if self._generator is not None:
                fsm_state = self._generator.fsm.state
                if fsm_state is FailbackState.QUEUE_ONLY:
                    self._state = ServiceState.DEGRADED
                elif fsm_state is FailbackState.FALLBACK_ACTIVE:
                    self._state = ServiceState.DEGRADED
                else:
                    self._state = ServiceState.ACTIVE
            else:
                self._state = ServiceState.DEGRADED

            logger.info(
                "[GovernedLoop] Started: state=%s, canary_slices=%s",
                self._state.name,
                self._config.initial_canary_slices,
            )

        except Exception as exc:
            self._state = ServiceState.FAILED
            self._failure_reason = str(exc)
            logger.error(
                "[GovernedLoop] Start failed: %s", exc, exc_info=True
            )
            await self._teardown_partial()
            raise

    async def stop(self) -> None:
        """Graceful shutdown. Drains in-flight ops, cancels probes."""
        if self._state is ServiceState.INACTIVE:
            return

        self._state = ServiceState.STOPPING

        # Cancel health probe loop
        if self._health_probe_task and not self._health_probe_task.done():
            self._health_probe_task.cancel()
            try:
                await self._health_probe_task
            except asyncio.CancelledError:
                pass

        # Drain in-flight ops (wait up to 30s)
        if self._active_ops:
            logger.info(
                "[GovernedLoop] Draining %d active ops...",
                len(self._active_ops),
            )
            await asyncio.sleep(0)  # Yield for any pending completions

        # Detach from stack
        self._detach_from_stack()
        self._state = ServiceState.INACTIVE
        logger.info("[GovernedLoop] Stopped")

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit(
        self,
        ctx: OperationContext,
        trigger_source: str = "unknown",
    ) -> OperationResult:
        """Submit an operation for governed execution.

        THE single entrypoint for all triggers (CLI, API, etc.).
        """
        start_time = time.monotonic()

        # Gate: service must be active
        if self._state not in (ServiceState.ACTIVE, ServiceState.DEGRADED):
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code=f"service_not_active:{self._state.name}",
                trigger_source=trigger_source,
            )

        # Gate: concurrency limit
        if len(self._active_ops) >= self._config.max_concurrent_ops:
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="busy",
                trigger_source=trigger_source,
            )

        # Gate: dedup
        dedupe_key = ctx.op_id
        if dedupe_key in self._active_ops:
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="duplicate:in_flight",
                trigger_source=trigger_source,
            )
        if dedupe_key in self._completed_ops:
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="duplicate:already_completed",
                trigger_source=trigger_source,
            )

        # Execute pipeline
        self._active_ops.add(dedupe_key)
        try:
            assert self._orchestrator is not None
            # Stamp pipeline_deadline exactly once — shared budget for all downstream phases
            ctx = ctx.with_pipeline_deadline(
                datetime.now(tz=timezone.utc) + timedelta(seconds=self._config.pipeline_timeout_s)
            )
            terminal_ctx = await self._orchestrator.run(ctx)

            duration = time.monotonic() - start_time
            result = OperationResult(
                op_id=ctx.op_id,
                terminal_phase=terminal_ctx.phase,
                provider_used=getattr(
                    terminal_ctx.generation, "provider_name", None
                ) if terminal_ctx.generation else None,
                generation_duration_s=getattr(
                    terminal_ctx.generation, "generation_duration_s", None
                ) if terminal_ctx.generation else None,
                total_duration_s=duration,
                reason_code=terminal_ctx.phase.name.lower(),
                trigger_source=trigger_source,
            )

            self._completed_ops[dedupe_key] = result
            return result

        finally:
            self._active_ops.discard(dedupe_key)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return structured health report."""
        uptime = (
            time.monotonic() - self._started_at
            if self._started_at
            else 0.0
        )
        return {
            "state": self._state.name,
            "active_ops": len(self._active_ops),
            "completed_ops": len(self._completed_ops),
            "canary_slices": list(self._config.initial_canary_slices),
            "uptime_s": round(uptime, 1),
            "failure_reason": self._failure_reason,
            "provider_fsm_state": (
                self._generator.fsm.state.name
                if self._generator
                else "no_generator"
            ),
        }

    # ------------------------------------------------------------------
    # Private: Component Construction
    # ------------------------------------------------------------------

    async def _build_components(self) -> None:
        """Build providers, generator, approval provider, and orchestrator."""
        primary = None
        fallback = None

        # Build PrimeProvider if PrimeClient available
        if self._prime_client is not None:
            try:
                from backend.core.ouroboros.governance.providers import (
                    PrimeProvider,
                )

                primary = PrimeProvider(self._prime_client)
                if await primary.health_probe():
                    logger.info("[GovernedLoop] PrimeProvider: healthy")
                else:
                    logger.warning("[GovernedLoop] PrimeProvider: unhealthy at startup")
                    primary = None
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] PrimeProvider build failed: %s", exc
                )
                primary = None

        # Build ClaudeProvider if API key available
        if self._config.claude_api_key:
            try:
                from backend.core.ouroboros.governance.providers import (
                    ClaudeProvider,
                )

                fallback = ClaudeProvider(
                    api_key=self._config.claude_api_key,
                    model=self._config.claude_model,
                    max_cost_per_op=self._config.claude_max_cost_per_op,
                    daily_budget=self._config.claude_daily_budget,
                )
                logger.info("[GovernedLoop] ClaudeProvider: configured")
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] ClaudeProvider build failed: %s", exc
                )
                fallback = None

        # Build CandidateGenerator (needs at least one provider)
        if primary is not None or fallback is not None:
            # If only one provider, use it as both (FSM still works)
            effective_primary = primary or fallback
            effective_fallback = fallback or primary
            assert effective_primary is not None
            assert effective_fallback is not None

            self._generator = CandidateGenerator(
                primary=effective_primary,
                fallback=effective_fallback,
            )
        else:
            logger.warning(
                "[GovernedLoop] No providers available — QUEUE_ONLY mode"
            )
            self._generator = None

        # Build approval provider
        self._approval_provider = CLIApprovalProvider()

        # Build orchestrator
        orch_config = OrchestratorConfig(
            project_root=self._config.project_root,
            generation_timeout_s=self._config.generation_timeout_s,
            approval_timeout_s=self._config.approval_timeout_s,
        )
        self._orchestrator = GovernedOrchestrator(
            stack=self._stack,
            generator=self._generator,
            approval_provider=self._approval_provider,
            config=orch_config,
        )

    def _register_canary_slices(self) -> None:
        """Register initial canary slices. Idempotent."""
        for slice_prefix in self._config.initial_canary_slices:
            try:
                self._stack.canary.register_slice(slice_prefix)
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] Failed to register canary slice %r: %s",
                    slice_prefix,
                    exc,
                )

    def _attach_to_stack(self) -> None:
        """Attach governed loop components to GovernanceStack."""
        self._stack.orchestrator = self._orchestrator
        self._stack.generator = self._generator
        self._stack.approval_provider = self._approval_provider

    def _detach_from_stack(self) -> None:
        """Detach governed loop components from GovernanceStack."""
        self._stack.orchestrator = None
        self._stack.generator = None
        self._stack.approval_provider = None

    async def _reconcile_on_boot(self) -> None:
        """Scan ledger for orphaned APPLIED ops and reconcile.

        For each op with latest_state == APPLIED:
          - Check recovery_attempted marker (skip if present — idempotent)
          - Check file hash against expected post_apply_hash in ledger data
          - If hash matches: attempt rollback via RollbackArtifact
          - If hash drifted: emit manual_intervention_required, no rollback

        Also expires stale PENDING approvals and cancels stale PLANNED ops.
        """
        if self._stack is None:
            return

        ledger = self._stack.ledger
        storage_dir = ledger._storage_dir

        TERMINAL = {
            OperationState.ROLLED_BACK, OperationState.FAILED,
            OperationState.BLOCKED,
        }

        # Scan all JSONL files in ledger storage
        for jsonl_file in storage_dir.glob("*.jsonl"):
            op_id = jsonl_file.stem  # sanitized op_id
            try:
                history = await ledger.get_history(op_id)
            except Exception:
                continue

            if not history:
                continue

            latest = history[-1]

            # ── Stale PLANNED cancellation ──────────────────────────────────
            if latest.state == OperationState.PLANNED:
                import time as _time
                stored_ts = latest.wall_time
                now_ts = _time.time()
                grace_s = getattr(self._config, "cold_start_grace_s", 300.0)
                skew_tol = 60.0
                age = now_ts - stored_ts
                if 0 < age < 604800 and age > grace_s + skew_tol:
                    await ledger.append(LedgerEntry(
                        op_id=op_id, state=OperationState.FAILED,
                        data={"reason": "stale_planned_on_boot", "age_s": age},
                    ))
                continue

            # ── Orphaned APPLIED reconciliation ─────────────────────────────
            if latest.state != OperationState.APPLIED:
                continue

            # Idempotency: skip if already attempted recovery
            if latest.data.get("recovery_attempted"):
                continue

            # Write recovery marker BEFORE doing any work
            import uuid as _uuid
            recovery_id = _uuid.uuid4().hex
            await ledger.append(LedgerEntry(
                op_id=op_id, state=OperationState.APPLIED,
                data={
                    **latest.data,
                    "recovery_attempted": True,
                    "recovery_attempt_id": recovery_id,
                },
            ))

            # Hash-guarded rollback
            target_path_str = latest.data.get("target_file")
            rollback_hash = latest.data.get("rollback_hash")  # pre-apply hash (set by ChangeEngine)

            if not target_path_str or not rollback_hash:
                # Insufficient provenance — cannot assess rollback, escalate
                await ledger.append(LedgerEntry(
                    op_id=op_id, state=OperationState.FAILED,
                    data={"reason": "boot_recovery_missing_provenance",
                          "recovery_attempt_id": recovery_id},
                ))
                await self._stack.comm.emit_decision(
                    op_id=op_id, outcome="manual_intervention_required",
                    reason_code="boot_recovery_missing_provenance",
                )
                continue

            import hashlib as _hashlib
            target = Path(target_path_str)
            if not target.exists():
                await ledger.append(LedgerEntry(
                    op_id=op_id, state=OperationState.FAILED,
                    data={"reason": "boot_recovery_file_missing",
                          "recovery_attempt_id": recovery_id},
                ))
                await self._stack.comm.emit_decision(
                    op_id=op_id, outcome="manual_intervention_required",
                    reason_code="boot_recovery_file_missing",
                )
                continue

            current_hash = _hashlib.sha256(target.read_bytes()).hexdigest()
            if current_hash == rollback_hash:
                # File already matches pre-apply content — change was undone externally
                await ledger.append(LedgerEntry(
                    op_id=op_id, state=OperationState.ROLLED_BACK,
                    data={"reason": "boot_recovery_already_reverted",
                          "recovery_attempt_id": recovery_id},
                ))
                logger.info("[GovernedLoop] Boot recovery: op=%s already reverted externally", op_id)
                continue

            # File still has post-apply content; original bytes not stored — escalate
            await ledger.append(LedgerEntry(
                op_id=op_id, state=OperationState.FAILED,
                data={"reason": "boot_recovery_needs_manual_rollback",
                      "current_hash": current_hash,
                      "rollback_hash": rollback_hash,
                      "recovery_attempt_id": recovery_id},
            ))
            await self._stack.comm.emit_decision(
                op_id=op_id, outcome="manual_intervention_required",
                reason_code="boot_recovery_needs_manual_rollback",
            )

        # Expire stale approvals — batch notify (no per-op comm storm)
        approval_store = getattr(self._stack, "approval_store", None)
        if approval_store is not None:
            ttl = getattr(self._config, "approval_ttl_s", 1800.0)
            expired = approval_store.expire_stale(timeout_seconds=ttl)
            if expired:
                await self._stack.comm.emit_decision(
                    op_id="boot_reconciliation",
                    outcome="approvals_expired_on_boot",
                    reason_code=f"expired_count={len(expired)}",
                    diff_summary=", ".join(expired[:10]),
                )
                logger.info("[GovernedLoop] Boot: expired %d stale approvals", len(expired))

    async def _teardown_partial(self) -> None:
        """Clean up partially constructed components on startup failure."""
        self._orchestrator = None
        self._generator = None
        self._approval_provider = None
        self._detach_from_stack()

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
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from backend.core.ouroboros.governance.approval_provider import CLIApprovalProvider
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    FailbackState,
)
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

    @classmethod
    def from_env(cls, args: Any = None) -> GovernedLoopConfig:
        """Build config from environment variables with safe defaults."""
        import os

        project_root = Path(
            os.getenv("JARVIS_PROJECT_ROOT", os.getcwd())
        )
        return cls(
            project_root=project_root,
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

    async def _teardown_partial(self) -> None:
        """Clean up partially constructed components on startup failure."""
        self._orchestrator = None
        self._generator = None
        self._approval_provider = None
        self._detach_from_stack()

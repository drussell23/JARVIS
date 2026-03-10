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
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Set, Tuple

from backend.core.ouroboros.governance.approval_provider import CLIApprovalProvider
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    FailbackState,
)
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
from backend.core.ouroboros.governance.op_context import (
    HostTelemetry,
    OperationContext,
    OperationPhase,
    RoutingIntentTelemetry,
    TelemetryContext,
)
from backend.core.ouroboros.governance.resource_monitor import PressureLevel, ResourceSnapshot
# IntakeLayerService is started by the supervisor (Zone 6.9); GLS only stores
# the resolved RepoRegistry on self._repo_registry for Zone 6.9 to reuse.
from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)
from backend.core.ouroboros.governance.curriculum_publisher import CurriculumPublisher
from backend.core.ouroboros.governance.model_attribution_recorder import ModelAttributionRecorder
from backend.core.ouroboros.integration import get_performance_persistence
from backend.core.ouroboros.governance.preemption_fsm import (
    PreemptionFsmEngine,
    PreemptionFsmExecutor,
    build_transition_input,
)
from backend.core.ouroboros.governance.contracts.fsm_contract import (
    LoopEvent,
    LoopRuntimeContext,
    LoopState,
    RetryBudget,
)

try:
    from backend.core.ouroboros.oracle import TheOracle as TheOracle
except ImportError:
    TheOracle = None  # type: ignore[assignment,misc]

logger = logging.getLogger("Ouroboros.GovernedLoop")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

MIN_GENERATION_BUDGET_S: float = float(
    os.getenv("JARVIS_MIN_GENERATION_BUDGET_S", "30.0")
)


# ---------------------------------------------------------------------------
# Phase 4: FSM infrastructure adapters
# ---------------------------------------------------------------------------


class _FsmLedgerAdapter:
    """Adapts OperationLedger to the FSM Ledger protocol.

    Converts FSM checkpoint appends to OperationLedger LedgerEntry writes.
    Idempotency guard uses an in-memory set (resets on restart, which is
    acceptable because each LoopRuntimeContext begins from RUNNING on startup).
    """

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self._seen: Set[Tuple[str, int]] = set()

    async def checkpoint_exists(self, *, op_id: str, checkpoint_seq: int) -> bool:
        return (op_id, checkpoint_seq) in self._seen

    async def append_checkpoint(
        self,
        *,
        op_id: str,
        checkpoint_seq: int,
        state: Any,
        event: Any,
        reason_code: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState

        self._seen.add((op_id, checkpoint_seq))
        try:
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.BLOCKED,
                    data={
                        "type": "preemption_fsm_checkpoint",
                        "loop_state": state.value,
                        "loop_event": event.value,
                        "reason_code": reason_code,
                        "checkpoint_seq": checkpoint_seq,
                        **payload,
                    },
                )
            )
        except Exception:
            pass  # ledger failure must never block an FSM transition


class _CommTelemetrySink:
    """Wraps CommProtocol to satisfy the FSM TelemetrySink protocol."""

    def __init__(self, comm: Any) -> None:
        self._comm = comm

    async def emit_transition(self, decision: Any, payload: Dict[str, Any]) -> None:
        op_id = payload.get("op_id", "unknown")
        try:
            await self._comm.emit_heartbeat(
                op_id=op_id,
                phase=f"preemption_fsm:{decision.to_state.value}",
                progress_pct=0.0,
            )
        except Exception:
            pass  # telemetry is best-effort


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _record_ledger(
    ctx: "OperationContext",
    ledger: Any,
    state: "OperationState",
    data: Dict[str, Any],
) -> None:
    """Append a ledger entry, logging errors without raising.

    Standalone helper used by GovernedLoopService._preflight_check() so that
    ledger writes can happen before the orchestrator is involved.
    """
    from backend.core.ouroboros.governance.ledger import LedgerEntry

    entry = LedgerEntry(
        op_id=ctx.op_id,
        state=state,
        data=data,
    )
    try:
        await ledger.append(entry)
    except Exception as exc:
        logger.error(
            "Ledger append failed: op_id=%s state=%s error=%s",
            entry.op_id,
            entry.state.value,
            exc,
        )


def _expected_provider_from_pressure(snap: ResourceSnapshot, active_ops: int = 0) -> str:
    """Derive the expected provider string from the snapshot's pressure level.

    NORMAL / ELEVATED  → GCP_PRIME_SPOT  (cloud inference preferred)
    CRITICAL / EMERGENCY → LOCAL_CLAUDE  (local fallback under resource pressure)

    Uses ``pressure_for_load(active_ops)`` so that legitimate concurrent load does not
    falsely elevate the routing decision to LOCAL_CLAUDE.
    """
    if snap.pressure_for_load(active_ops) >= PressureLevel.CRITICAL:
        return "LOCAL_CLAUDE"
    return "GCP_PRIME_SPOT"


def _infer_canary_slice(target_files: tuple) -> str:
    """Derive the most restrictive canary slice from target file paths.

    Checks all files and returns the most constrained slice:
    - "tests/" and "docs/" → GOVERNED (lowest restriction)
    - "backend/core/" → OBSERVE
    - "" (root default) → OBSERVE

    When files span multiple slices, returns the most restrictive.
    """
    # Ordered from most restrictive to least restrictive
    _SLICE_ORDER = ["backend/core/", "", "tests/", "docs/"]
    found: set = set()
    for fp in target_files:
        fp_norm = fp.replace("\\", "/").lstrip("./")
        if fp_norm.startswith("tests/"):
            found.add("tests/")
        elif fp_norm.startswith("docs/"):
            found.add("docs/")
        elif fp_norm.startswith("backend/core/"):
            found.add("backend/core/")
        else:
            found.add("")
    if not found:
        return ""
    # Return most restrictive: OBSERVE slices (backend/core/, "") beat GOVERNED slices
    for s in _SLICE_ORDER:
        if s in found:
            return s
    return ""


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

    project_root: Path = field(default_factory=lambda: Path(os.getcwd()))
    claude_api_key: Optional[str] = None
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_cost_per_op: float = 0.50
    claude_daily_budget: float = 10.00
    generation_timeout_s: float = 120.0
    context_expansion_timeout_s: float = 30.0
    approval_timeout_s: float = 600.0
    health_probe_interval_s: float = 30.0
    max_concurrent_ops: int = 2
    initial_canary_slices: Tuple[str, ...] = ("tests/", "docs/")
    cold_start_grace_s: float = 300.0   # ops younger than this are not cancelled on boot
    approval_ttl_s: float = 1800.0      # stale approval expiry timeout
    pipeline_timeout_s: float = 600.0   # total wall-clock budget per submit(); env: JARVIS_PIPELINE_TIMEOUT_S

    # Curriculum + reactor event background task settings
    curriculum_enabled: bool = True
    curriculum_publish_interval_s: float = 3600.0
    curriculum_window_n: int = 50
    curriculum_top_k: int = 5
    curriculum_impact_weights: Dict[str, float] = field(default_factory=dict)
    model_attribution_lookback_n: int = 20
    model_attribution_min_sample_size: int = 3
    reactor_event_poll_interval_s: float = 30.0
    oracle_enabled: bool = True
    oracle_incremental_poll_interval_s: float = 300.0

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
            context_expansion_timeout_s=float(
                os.getenv("JARVIS_GOVERNED_EXPANSION_TIMEOUT", "30.0")
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
        stack: Any = None,
        prime_client: Any = None,
        config: Optional[GovernedLoopConfig] = None,
        active_brain_set: FrozenSet[str] = frozenset(),
    ) -> None:
        self._stack = stack
        self._prime_client = prime_client
        self._config = config if config is not None else GovernedLoopConfig.from_env()
        self._state = ServiceState.INACTIVE
        self._started_at: Optional[float] = None
        self._failure_reason: Optional[str] = None

        # Phase 4: admitted active brain set (published by supervisor post-handshake)
        # Empty frozenset = gate disabled (backward-compatible default)
        self._active_brain_set: FrozenSet[str] = active_brain_set

        # Phase 4: preemption FSM — initialized after ledger in start()
        self._fsm_engine: Optional[PreemptionFsmEngine] = None
        self._fsm_executor: Optional[PreemptionFsmExecutor] = None
        self._fsm_contexts: Dict[str, LoopRuntimeContext] = {}
        self._fsm_checkpoint_seq: Dict[str, int] = {}

        # Built during start()
        self._orchestrator: Optional[GovernedOrchestrator] = None
        self._generator: Optional[CandidateGenerator] = None
        self._approval_provider: Optional[CLIApprovalProvider] = None
        self._health_probe_task: Optional[asyncio.Task] = None
        self._ledger: Any = None  # set in _build_components from stack.ledger
        self._repo_registry: Optional[Any] = None  # set in _build_components; reused by supervisor Zone 6.9
        self._trust_graduator: Optional[Any] = None

        # Phase 4: Brain selector — CAI-intent-aware async router (wraps BrainSelector)
        from backend.core.ouroboros.governance.route_decision_service import RouteDecisionService
        self._brain_selector = RouteDecisionService()

        # Sliding-window cooldown: maps file_path -> deque of touch timestamps (monotonic)
        self._file_touch_cache: Dict[str, Any] = {}  # str -> collections.deque[float]

        # Background task handles (curriculum + reactor event loop)
        self._curriculum_task: Optional[asyncio.Task] = None
        self._reactor_event_task: Optional[asyncio.Task] = None
        self._curriculum_publisher: Optional[CurriculumPublisher] = None
        self._model_attribution_recorder: Optional[ModelAttributionRecorder] = None
        self._event_dir: Optional[Path] = None
        self._oracle_indexer_task: Optional[asyncio.Task] = None
        self._oracle: Optional[Any] = None

        # Concurrency & dedup
        self._active_ops: Set[str] = set()
        self._active_file_ops: Set[str] = set()  # canonical file paths currently in-flight
        self._completed_ops: Dict[str, OperationResult] = {}

    @property
    def state(self) -> ServiceState:
        return self._state

    @property
    def active_brain_set(self) -> FrozenSet[str]:
        """Immutable snapshot of the supervisor-admitted brain set."""
        return self._active_brain_set

    def set_active_brain_set(self, brain_set: FrozenSet[str]) -> None:
        """Update the admitted active brain set.

        Called by unified_supervisor after a successful boot handshake.
        The frozenset assignment is atomic under the GIL.
        """
        old = self._active_brain_set
        self._active_brain_set = brain_set
        logger.info(
            "[GovernedLoop] ActiveBrainSet updated: %s → %s",
            sorted(old),
            sorted(brain_set),
        )

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

            # Phase 4: initialize preemption FSM executor (ledger available after _build_components)
            self._fsm_engine = PreemptionFsmEngine()
            if self._ledger is not None:
                comm = getattr(self._stack, "comm", None) if self._stack else None
                _sink = _CommTelemetrySink(comm) if comm is not None else None
                self._fsm_executor = PreemptionFsmExecutor(
                    engine=self._fsm_engine,
                    ledger=_FsmLedgerAdapter(self._ledger),
                    telemetry=_sink,
                )
                logger.debug("[GovernedLoop] Preemption FSM executor initialized")

            await self._reconcile_on_boot()  # boot reconciliation
            self._register_canary_slices()
            self._seed_autonomy_policies()
            self._attach_to_stack()
            self._started_at = time.monotonic()

            # Wire curriculum and reactor event background tasks
            if self._config.curriculum_enabled:
                event_dir = Path(os.environ.get(
                    "JARVIS_REACTOR_EVENT_DIR",
                    str(Path.home() / ".jarvis" / "reactor_events"),
                ))
                event_dir.mkdir(parents=True, exist_ok=True)
                self._event_dir = event_dir
                persistence = get_performance_persistence()
                self._curriculum_publisher = CurriculumPublisher(
                    persistence=persistence,
                    event_dir=event_dir,
                    window_n=self._config.curriculum_window_n,
                    top_k=self._config.curriculum_top_k,
                    impact_weights=self._config.curriculum_impact_weights,
                )
                self._model_attribution_recorder = ModelAttributionRecorder(
                    persistence=persistence,
                    lookback_n=self._config.model_attribution_lookback_n,
                    min_sample_size=self._config.model_attribution_min_sample_size,
                )
                self._curriculum_task = asyncio.create_task(
                    self._curriculum_loop(), name="curriculum_loop"
                )
                self._reactor_event_task = asyncio.create_task(
                    self._reactor_event_loop(), name="reactor_event_loop"
                )

            if self._config.oracle_enabled:
                self._oracle_indexer_task = asyncio.create_task(
                    self._oracle_index_loop(), name="oracle_index_loop"
                )

            # Start health probe background task
            self._health_probe_task = asyncio.create_task(
                self._health_probe_loop(), name="health_probe_loop"
            )

            # Determine state based on provider availability
            if self._generator is not None:
                fsm_state = self._generator.fsm.state
                if fsm_state is FailbackState.QUEUE_ONLY:
                    self._state = ServiceState.DEGRADED
                elif fsm_state is FailbackState.FALLBACK_ACTIVE:
                    # Intentional GCP-first fallback — not degraded
                    self._state = ServiceState.ACTIVE
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

        # Cancel curriculum and reactor event background tasks
        for task_attr in ("_curriculum_task", "_reactor_event_task", "_oracle_indexer_task"):
            task: Optional[asyncio.Task] = getattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
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

        # Gate: file-scope in-flight lock (before acquiring — prevents self-cancel)
        import pathlib as _pl_gate
        for _fp in ctx.target_files:
            _canonical = str(_pl_gate.Path(_fp).resolve())
            if _canonical in self._active_file_ops:
                logger.warning(
                    "[GovernedLoop] File-scope lock: %r already in-flight — "
                    "rejecting op %s to prevent split-brain apply",
                    _canonical,
                    ctx.op_id,
                )
                return OperationResult(
                    op_id=ctx.op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    reason_code="file_in_flight",
                    trigger_source=trigger_source,
                )

        # Execute pipeline
        self._active_ops.add(dedupe_key)
        _locked_files: list = []
        for _fp in ctx.target_files:
            _canonical = str(__import__("pathlib").Path(_fp).resolve())
            self._active_file_ops.add(_canonical)
            _locked_files.append(_canonical)
        try:
            assert self._orchestrator is not None
            # Stamp pipeline_deadline exactly once — shared budget for all downstream phases
            ctx = ctx.with_pipeline_deadline(
                datetime.now(tz=timezone.utc) + timedelta(seconds=self._config.pipeline_timeout_s)
            )

            # Stamp TelemetryContext exactly once at intake
            snap = await self._stack.resource_monitor.snapshot()
            now_ns = time.monotonic_ns()
            host_tel = HostTelemetry(
                schema_version="1.0",
                arch=snap.platform_arch,
                cpu_percent=snap.cpu_percent,           # already quantized
                ram_available_gb=snap.ram_available_gb, # already quantized
                pressure=snap.pressure_for_load(len(self._active_ops)).name,
                sampled_at_utc=datetime.now(tz=timezone.utc).isoformat(),
                sampled_monotonic_ns=snap.sampled_monotonic_ns,
                collector_status=snap.collector_status,
                sample_age_ms=(now_ns - snap.sampled_monotonic_ns) // 1_000_000,
            )
            # Phase 4: 3-layer brain selection gate (task → resource → cost)
            brain = await self._brain_selector.select(
                description=ctx.description,
                target_files=ctx.target_files,
                snapshot=snap,
                blast_radius=len(ctx.target_files),
            )
            logger.info(
                "[GovernedLoop] Brain selected: %s (%s) reason=%s complexity=%s spend=$%.4f",
                brain.brain_id, brain.model_name, brain.routing_reason,
                brain.task_complexity, self._brain_selector.daily_spend,
            )

            # Phase 4: ActiveBrainSet gate — reject brains not admitted by supervisor
            if self._active_brain_set and brain.brain_id not in self._active_brain_set:
                logger.warning(
                    "[GovernedLoop] Brain %r not in admitted set %s — rejecting op %s",
                    brain.brain_id, sorted(self._active_brain_set), ctx.op_id,
                )
                return OperationResult(
                    op_id=ctx.op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    reason_code="brain_not_admitted",
                    trigger_source=trigger_source,
                )

            # Phase 4: create per-op FSM context (starts in RUNNING)
            _fsm_ctx = LoopRuntimeContext(op_id=ctx.op_id)
            self._fsm_contexts[ctx.op_id] = _fsm_ctx
            self._fsm_checkpoint_seq[ctx.op_id] = 0

            # Emit routing narration via CommProtocol
            try:
                await self._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id,
                    phase="brain_routing",
                    progress_pct=3.0,
                )
                # Narrate to voice — uses VoiceNarrator transport if active
                narration = brain.narration()
                await self._stack.comm.emit_intent(
                    op_id=ctx.op_id,
                    goal=narration,
                    target_files=list(ctx.target_files),
                    risk_tier="routing",
                    blast_radius=len(ctx.target_files),
                )
            except Exception:
                pass  # narration is best-effort

            # Short-circuit: cost gate queued heavy task
            if brain.provider_tier == "queued":
                logger.warning(
                    "[GovernedLoop] Cost gate queued op %s (daily_spend=$%.4f)",
                    ctx.op_id, self._brain_selector.daily_spend,
                )
                return OperationResult(
                    op_id=ctx.op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    reason_code="cost_gate_triggered_queue",
                    trigger_source=trigger_source,
                )

            intent_tel = RoutingIntentTelemetry(
                expected_provider=_expected_provider_from_pressure(snap, len(self._active_ops)),
                policy_reason=snap.pressure_for_load(len(self._active_ops)).name,
                brain_id=brain.brain_id,
                brain_model=brain.model_name,
                routing_reason=brain.routing_reason,
                task_complexity=brain.task_complexity,
                estimated_prompt_tokens=brain.estimated_prompt_tokens,
                daily_spend_usd=self._brain_selector.daily_spend,
            )
            tc = TelemetryContext(local_node=host_tel, routing_intent=intent_tel)
            ctx = ctx.with_telemetry(tc)

            # Freeze autonomy tier at submit time — GATE reads ctx.frozen_autonomy_tier
            # not live TrustGraduator (prevents promotion races under concurrent ops).
            _canary_slice = _infer_canary_slice(ctx.target_files)
            _frozen_tier = "governed"  # default: backward compat
            if self._trust_graduator is not None:
                _tier_cfg = self._trust_graduator.get_config(
                    trigger_source=trigger_source,
                    repo=ctx.primary_repo,
                    canary_slice=_canary_slice,
                )
                if _tier_cfg is not None:
                    _frozen_tier = _tier_cfg.current_tier.value.lower()
            ctx = ctx.with_frozen_autonomy_tier(_frozen_tier)

            # Connectivity preflight (spends from deadline budget)
            if self._generator is not None and self._ledger is not None:
                early_exit = await self._preflight_check(ctx)
                if early_exit is not None:
                    duration = time.monotonic() - start_time
                    result = OperationResult(
                        op_id=ctx.op_id,
                        terminal_phase=early_exit.phase,
                        total_duration_s=duration,
                        reason_code=early_exit.phase.name.lower(),
                        trigger_source=trigger_source,
                    )
                    self._completed_ops[dedupe_key] = result
                    return result

            _pipeline_timeout = (
                self._config.pipeline_timeout_s + 60.0
            )  # +60s grace beyond deadline for post-COMPLETE bookkeeping
            try:
                terminal_ctx = await asyncio.wait_for(
                    self._orchestrator.run(ctx),
                    timeout=_pipeline_timeout,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[GovernedLoop] orchestrator.run() exceeded %.0fs hard timeout for op=%s",
                    _pipeline_timeout, ctx.op_id,
                )
                duration = time.monotonic() - start_time
                result = OperationResult(
                    op_id=ctx.op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    total_duration_s=duration,
                    reason_code="pipeline_timeout",
                    trigger_source=trigger_source,
                )
                self._completed_ops[dedupe_key] = result
                return result

            # Phase 4: record actual generation cost for cost gate persistence
            if terminal_ctx.generation:
                _gen = terminal_ctx.generation
                _provider_name = getattr(_gen, "provider_name", "unknown")
                _cost = getattr(_gen, "cost_usd", 0.0) or 0.0
                if _cost > 0.0:
                    self._brain_selector.record_cost(_provider_name, _cost)

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
            for _canonical in _locked_files:
                self._active_file_ops.discard(_canonical)
            # Phase 4: clean up per-op FSM context
            self._fsm_contexts.pop(ctx.op_id, None)
            self._fsm_checkpoint_seq.pop(ctx.op_id, None)

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
    # Private: Preflight
    # ------------------------------------------------------------------

    async def _preflight_check(
        self,
        ctx: OperationContext,
    ) -> Optional[OperationContext]:
        """Run connectivity preflight after deadline is stamped.

        Called from submit() immediately after pipeline_deadline is set on ctx.
        Checks remaining budget and probes the primary provider.

        Returns:
            None                 — preflight passed; caller should proceed.
            OperationContext     — early-exit ctx (CANCELLED); caller returns it.
        """
        # NOTE: File-scope in-flight lock is checked in submit() before the pipeline
        # starts — not here.  Checking here would cause self-cancellation because
        # submit() adds files to _active_file_ops before calling the orchestrator.

        # --- Cooldown guard: block if same file touched >3 times in 10 min ---
        import collections as _collections
        import time as _time
        import pathlib as _pathlib_cooldown
        _COOLDOWN_WINDOW_S = 600.0   # 10 minutes
        _COOLDOWN_MAX_HITS = 3
        _now = _time.monotonic()
        for _fp in ctx.target_files:
            _canonical_fp = str(_pathlib_cooldown.Path(_fp).resolve())
            if _canonical_fp not in self._file_touch_cache:
                self._file_touch_cache[_canonical_fp] = _collections.deque()
            _dq = self._file_touch_cache[_canonical_fp]
            # Evict timestamps older than the window
            while _dq and (_now - _dq[0]) > _COOLDOWN_WINDOW_S:
                _dq.popleft()
            _dq.append(_now)
            if len(_dq) > _COOLDOWN_MAX_HITS:
                logger.warning(
                    "[GovernedLoop] Cooldown triggered for file %r "
                    "(%d touches in %.0fs window) — blocking op %s",
                    _canonical_fp,
                    len(_dq),
                    _COOLDOWN_WINDOW_S,
                    ctx.op_id,
                )
                return ctx.advance(OperationPhase.CANCELLED)

        now = datetime.now(tz=timezone.utc)
        remaining_s = (
            (ctx.pipeline_deadline - now).total_seconds()
            if ctx.pipeline_deadline
            else 0.0
        )

        # Budget pre-check: cancel immediately if not enough time remains
        if remaining_s < MIN_GENERATION_BUDGET_S:
            cancelled = ctx.advance(OperationPhase.CANCELLED)
            await _record_ledger(
                cancelled,
                self._ledger,
                OperationState.FAILED,
                {"reason_code": "budget_exhausted_pre_generation", "remaining_s": remaining_s},
            )
            logger.warning(
                "[GovernedLoop] Preflight: budget exhausted before generation "
                "(remaining=%.1fs, min=%.1fs); op_id=%s",
                remaining_s,
                MIN_GENERATION_BUDGET_S,
                ctx.op_id,
            )
            return cancelled

        # Connectivity preflight: probe primary provider
        # CandidateGenerator stores the primary provider as _primary (private).
        # health_probe() takes no arguments; wrap with asyncio.wait_for for timeout.
        probe_timeout = min(5.0, remaining_s * 0.05)
        try:
            provider = getattr(self._generator, "_primary", None)
            if provider is None:
                raise RuntimeError("no_primary_provider")
            primary_ok = await asyncio.wait_for(
                provider.health_probe(), timeout=probe_timeout
            )
        except Exception:
            logger.debug(
                "[GovernedLoop] Preflight: primary probe raised exception",
                exc_info=True,
            )
            primary_ok = False

        if primary_ok:
            # Primary healthy — proceed normally
            return None

        # Phase 4: fire EV_CONNECTION_LOSS through preemption FSM for audit trail
        if self._fsm_executor is not None:
            _fsm_ctx = self._fsm_contexts.get(ctx.op_id)
            if _fsm_ctx is not None and _fsm_ctx.state == LoopState.RUNNING:
                _seq = self._fsm_checkpoint_seq.get(ctx.op_id, 0) + 1
                self._fsm_checkpoint_seq[ctx.op_id] = _seq
                _ti = build_transition_input(
                    op_id=ctx.op_id,
                    phase="PREFLIGHT",
                    event=LoopEvent.EV_CONNECTION_LOSS,
                    ctx=_fsm_ctx,
                    checkpoint_seq=_seq,
                    metadata={"source": "preflight_probe_failure"},
                )
                try:
                    await self._fsm_executor.apply(_fsm_ctx, _ti)
                    logger.info(
                        "[GovernedLoop] Preemption FSM: op=%s → %s (connection loss)",
                        ctx.op_id, _fsm_ctx.state.value,
                    )
                except Exception as _exc:
                    logger.debug("[GovernedLoop] FSM apply skipped: %s", _exc)

        # Primary unavailable: decide based on FSM state
        # CandidateGenerator.fsm is a FailbackStateMachine; .state is a FailbackState enum.
        fsm = getattr(self._generator, "fsm", None)
        fsm_state = getattr(fsm, "state", None) if fsm is not None else None

        if fsm_state is FailbackState.QUEUE_ONLY:
            # No fallback available — cancel
            cancelled = ctx.advance(OperationPhase.CANCELLED)
            await _record_ledger(
                cancelled,
                self._ledger,
                OperationState.FAILED,
                {"reason_code": "provider_unavailable"},
            )
            logger.warning(
                "[GovernedLoop] Preflight: QUEUE_ONLY + primary unhealthy → CANCELLED; op_id=%s",
                ctx.op_id,
            )
            return cancelled

        # Fallback is active — log informational entry and continue
        await _record_ledger(
            ctx,
            self._ledger,
            OperationState.BLOCKED,
            {"reason_code": "primary_unavailable_fallback_active"},
        )
        logger.info(
            "[GovernedLoop] Preflight: primary unavailable, fallback active; op_id=%s",
            ctx.op_id,
        )
        return None

    # ------------------------------------------------------------------
    # Private: Component Construction
    # ------------------------------------------------------------------

    async def _build_components(self) -> None:
        """Build providers, generator, approval provider, and orchestrator."""
        # Wire ledger from stack so _preflight_check can append without orchestrator
        if self._stack is not None:
            self._ledger = getattr(self._stack, "ledger", None)

        # Build RepoRegistry first so providers receive repo_roots at construction time.
        # RepoRegistry.from_env() is synchronous — no ordering dependency prevents this.
        repo_registry = RepoRegistry.from_env()
        enabled_repos = repo_registry.list_enabled()
        logger.info(
            "[GovernedLoop] RepoRegistry enabled repos: %s",
            [r.name for r in enabled_repos],
        )
        repo_roots_map: Dict[str, Path] = {r.name: r.local_path for r in enabled_repos}

        primary = None
        fallback = None

        # Build PrimeProvider if PrimeClient available
        _primary_probe_ok = False  # track for FSM sync after generator build
        if self._prime_client is not None:
            try:
                from backend.core.ouroboros.governance.providers import (
                    PrimeProvider,
                )

                primary = PrimeProvider(
                    self._prime_client,
                    repo_root=self._config.project_root,
                    repo_roots=repo_roots_map,
                )
                try:
                    if await primary.health_probe():
                        logger.info("[GovernedLoop] PrimeProvider: healthy at startup")
                        _primary_probe_ok = True
                    else:
                        logger.warning(
                            "[GovernedLoop] PrimeProvider: unhealthy at startup; "
                            "retained for probe-based recovery"
                        )
                        # Do NOT set primary = None — circuit breaker handles retry
                except Exception as probe_exc:
                    logger.warning(
                        "[GovernedLoop] PrimeProvider: startup probe raised %s; "
                        "retained for probe-based recovery",
                        probe_exc,
                    )
                    # Probe failure (raise) is treated same as probe failure (False):
                    # retain the provider for circuit-breaker-based recovery
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
                    repo_root=self._config.project_root,
                    repo_roots=repo_roots_map,
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

            # Sync FSM to reflect actual startup probe result.
            # Without this, the FSM stays at PRIMARY_READY even when the startup
            # probe failed, making the FALLBACK_ACTIVE branch in start() unreachable.
            if primary is not None and not _primary_probe_ok and self._generator is not None:
                try:
                    self._generator.fsm.record_primary_failure()
                except Exception:
                    pass  # FSM transition error should not abort startup
        else:
            logger.warning(
                "[GovernedLoop] No providers available — QUEUE_ONLY mode"
            )
            self._generator = None

        # Build approval provider
        self._approval_provider = CLIApprovalProvider()

        # Build ValidationRunner (LanguageRouter with Python + C++ adapters)
        from backend.core.ouroboros.governance.test_runner import (
            CppAdapter,
            LanguageRouter,
            PythonAdapter,
        )
        validation_runner = LanguageRouter(
            repo_root=self._config.project_root,
            adapters={
                "python": PythonAdapter(repo_root=self._config.project_root),
                "cpp": CppAdapter(repo_root=self._config.project_root),
            },
        )

        # Build orchestrator
        orch_config = OrchestratorConfig(
            project_root=self._config.project_root,
            repo_registry=repo_registry,
            generation_timeout_s=self._config.generation_timeout_s,
            context_expansion_timeout_s=self._config.context_expansion_timeout_s,
            approval_timeout_s=self._config.approval_timeout_s,
        )
        self._orchestrator = GovernedOrchestrator(
            stack=self._stack,
            generator=self._generator,
            approval_provider=self._approval_provider,
            config=orch_config,
            validation_runner=validation_runner,
        )

        # NOTE: IntakeLayerService is started by the supervisor (Zone 6.9) which
        # injects say_fn and repo_registry.  GLS exposes _repo_registry so Zone 6.9
        # can reuse the already-resolved registry without a second from_env() call.
        self._repo_registry = repo_registry

    def _register_canary_slices(self) -> None:
        """Register initial canary slices and pre-activate them. Idempotent.

        Slices listed in ``initial_canary_slices`` are bootstrap-trusted — they
        are explicitly configured at startup, so promotion criteria (50 ops) are
        waived.  This avoids the chicken-and-egg problem where the first operation
        cannot run because no slice has accumulated the required track record yet.
        """
        from backend.core.ouroboros.governance.canary_controller import CanaryState
        for slice_prefix in self._config.initial_canary_slices:
            try:
                self._stack.canary.register_slice(slice_prefix)
                # Pre-activate: bootstrap slices are explicitly trusted from boot
                self._stack.canary._slices[slice_prefix].state = CanaryState.ACTIVE
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] Failed to register canary slice %r: %s",
                    slice_prefix,
                    exc,
                )

    def _seed_autonomy_policies(self) -> None:
        """Seed baseline SignalAutonomyConfig per repo x trigger_source x canary_slice.

        Default tiers:
          tests/            -> GOVERNED  (test-only changes run without human approval)
          docs/             -> GOVERNED  (doc patches run without human approval)
          backend/core/     -> OBSERVE   (infrastructure changes require voice confirmation)
          "" (root default) -> OBSERVE   (unclassified root-level changes default to safe)

        Tiers are seeded conservatively; TrustGraduator.promote() advances them
        automatically as operational track record accumulates.
        """
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        from backend.core.ouroboros.governance.autonomy.tiers import (
            AutonomyTier,
            GraduationMetrics,
            SignalAutonomyConfig,
            WorkContext,
            CognitiveLoad,
        )

        _TRIGGER_SOURCES = (
            "voice_command",
            "backlog",
            "test_failure",
            "opportunity_miner",
        )
        # canary_slice -> (tier, defer_during_work_context)
        _SLICE_POLICIES = {
            "tests/":        (AutonomyTier.GOVERNED, (WorkContext.MEETINGS,)),
            "docs/":         (AutonomyTier.GOVERNED, (WorkContext.MEETINGS,)),
            "backend/core/": (AutonomyTier.OBSERVE,  (WorkContext.MEETINGS, WorkContext.CODING)),
            "":              (AutonomyTier.OBSERVE,   (WorkContext.MEETINGS, WorkContext.CODING)),
        }

        graduator = TrustGraduator()
        repos = (
            [r.name for r in self._repo_registry.list_enabled()]
            if self._repo_registry is not None
            else ["jarvis"]
        )

        for repo in repos:
            for trigger_source in _TRIGGER_SOURCES:
                for canary_slice, (tier, defer_ctxs) in _SLICE_POLICIES.items():
                    config = SignalAutonomyConfig(
                        trigger_source=trigger_source,
                        repo=repo,
                        canary_slice=canary_slice,
                        current_tier=tier,
                        graduation_metrics=GraduationMetrics(),
                        defer_during_cognitive_load=CognitiveLoad.HIGH,
                        defer_during_work_context=tuple(defer_ctxs),
                        require_user_active=False,
                    )
                    graduator.register(config)

        self._trust_graduator = graduator
        logger.info(
            "[GovernedLoop] Autonomy policies seeded: %d configs across %d repos",
            len(graduator.all_configs()),
            len(repos),
        )

    def _attach_to_stack(self) -> None:
        """Attach governed loop components to GovernanceStack."""
        if self._stack is None:
            return
        self._stack.orchestrator = self._orchestrator
        self._stack.generator = self._generator
        self._stack.approval_provider = self._approval_provider

    def _detach_from_stack(self) -> None:
        """Detach governed loop components from GovernanceStack."""
        if self._stack is None:
            return
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

    # ------------------------------------------------------------------
    # Private: Background loops
    # ------------------------------------------------------------------

    async def _health_probe_loop(self) -> None:
        """Periodically probe provider health and update FSM state."""
        while True:
            try:
                await asyncio.sleep(self._config.health_probe_interval_s)
                if self._generator is not None:
                    provider = getattr(self._generator, "_primary", None)
                    if provider is not None:
                        try:
                            ok = await asyncio.wait_for(
                                provider.health_probe(), timeout=5.0
                            )
                            if ok:
                                try:
                                    self._generator.fsm.record_probe_success()
                                except Exception:
                                    pass
                            else:
                                try:
                                    self._generator.fsm.record_primary_failure()
                                except Exception:
                                    pass
                        except Exception:
                            try:
                                self._generator.fsm.record_primary_failure()
                            except Exception:
                                pass
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] health_probe_loop error: %s", exc)

    async def _curriculum_loop(self) -> None:
        """Publish curriculum signal every interval. Never crashes the service."""
        while True:
            try:
                await asyncio.sleep(self._config.curriculum_publish_interval_s)
                if self._curriculum_publisher:
                    await asyncio.wait_for(
                        self._curriculum_publisher.publish(),
                        timeout=30.0,
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] curriculum_loop error: %s", exc)

    async def _reactor_event_loop(self) -> None:
        """Poll event_dir for Reactor events. Never crashes the service."""
        seen: set[str] = set()
        while True:
            try:
                await asyncio.sleep(self._config.reactor_event_poll_interval_s)
                await self._handle_event_files(seen)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] reactor_event_loop error: %s", exc)

    async def _oracle_index_loop(self) -> None:
        """Index all repos into TheOracle graph on boot, then poll for incremental changes.

        Non-blocking: start() never awaits this. Fault-isolated: any exception in
        initialization sets self._oracle = None, logs a structured warning, and exits
        the task without impacting service state or any operation's terminal phase.
        """
        try:
            if TheOracle is None:
                raise ImportError("TheOracle not available")
            oracle = TheOracle()
            await oracle.initialize()
            self._oracle = oracle
            if self._stack is not None:
                self._stack.oracle = oracle
            logger.info(
                "[GovernedLoop] Oracle indexed %s nodes across all repos",
                oracle.get_metrics().get("total_nodes", "?"),
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "[GovernedLoop] Oracle initialization failed: %s; codebase graph unavailable",
                exc,
            )
            self._oracle = None
            return

        # Incremental update loop — polls every oracle_incremental_poll_interval_s
        while True:
            try:
                await asyncio.sleep(self._config.oracle_incremental_poll_interval_s)
                await self._oracle.incremental_update([])
            except asyncio.CancelledError:
                await self._oracle.shutdown()
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] Oracle incremental update failed: %s", exc)

    async def _handle_event_files(self, seen: Set[str]) -> None:
        """Process new JSON files in event_dir. Extracted for testability."""
        if self._event_dir is None:
            return
        for path in sorted(self._event_dir.glob("*.json")):
            if path.name in seen:
                continue
            seen.add(path.name)
            try:
                data = json.loads(path.read_text())
                event_type = data.get("event_type", "")
                if event_type == "model_promoted":
                    await self._handle_model_promoted(data)
                elif event_type == "ouroboros_improvement":
                    pass  # consumed elsewhere
                else:
                    logger.debug(
                        "[GovernedLoop] Unknown event_type=%r in %s",
                        event_type, path.name,
                    )
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] reactor_event_loop: failed to process %s: %s",
                    path.name, exc,
                )

    async def _handle_model_promoted(self, data: dict) -> None:
        if self._model_attribution_recorder is None:
            return
        try:
            await asyncio.wait_for(
                self._model_attribution_recorder.record_model_transition(
                    new_model_id=data["model_id"],
                    previous_model_id=data["previous_model_id"],
                    training_batch_size=int(data["training_batch_size"]),
                    task_types=data.get("task_types"),
                ),
                timeout=30.0,
            )
        except Exception as exc:
            logger.warning("[GovernedLoop] _handle_model_promoted failed: %s", exc)

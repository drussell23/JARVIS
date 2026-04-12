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
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

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
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)
from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandType as AutonomyCommandType,
    EventEnvelope as AutonomyEventEnvelope,
    EventType as AutonomyEventType,
)
from backend.core.ouroboros.governance.autonomy.safety_net import (
    ProductionSafetyNet,
    SafetyNetConfig,
)
from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus

try:
    from backend.core.ouroboros.oracle import TheOracle as TheOracle
except ImportError:
    TheOracle = None  # type: ignore[assignment,misc]

logger = logging.getLogger("Ouroboros.GovernedLoop")

# ---------------------------------------------------------------------------
# Sandbox-safe state directory — redirect ~/.jarvis when not writable
# ---------------------------------------------------------------------------
# Many subsystems write to ~/.jarvis/ouroboros/*.  In sandboxed environments
# (e.g. Claude Code, macOS sandbox, CI containers) that path may not be
# writable.  Detect once at import time and redirect to a repo-local
# fallback via JARVIS_STATE_DIR so all subsystems that resolve via
# Path.home() / ".jarvis" or JARVIS_SELF_EVOLUTION_DIR fall through cleanly.

def _ensure_writable_state_dir() -> None:
    """Set JARVIS_STATE_DIR if ~/.jarvis is not writable."""
    if os.environ.get("JARVIS_STATE_DIR"):
        return  # already explicitly set
    home_jarvis = Path.home() / ".jarvis"
    try:
        home_jarvis.mkdir(parents=True, exist_ok=True)
        # Write-test with a temp file
        _probe = home_jarvis / ".write_probe"
        _probe.write_text("ok")
        _probe.unlink()
    except OSError:
        # Not writable — fall back to repo-local .ouroboros/state/
        _fallback = Path.cwd() / ".ouroboros" / "state"
        try:
            _fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            return  # can't create fallback either — let individual modules handle it
        os.environ["JARVIS_STATE_DIR"] = str(_fallback)
        # Redirect subsystem env vars that default to ~/.jarvis paths
        _redirects = {
            "JARVIS_SELF_EVOLUTION_DIR": str(_fallback / "ouroboros" / "evolution"),
            "JARVIS_GOVERNED_L3_STATE_DIR": str(_fallback / "ouroboros" / "execution_graphs"),
            "JARVIS_GOVERNED_L4_STATE_DIR": str(_fallback / "ouroboros" / "advanced_coordination"),
            # OperationLedger (change_engine Phase 1 writes). If this is not
            # redirected, every op that reaches APPLY dies on the first
            # ledger.append() with PermissionError — see bt-2026-04-10-075150.
            "OUROBOROS_LEDGER_DIR": str(_fallback / "ouroboros" / "ledger"),
        }
        for key, val in _redirects.items():
            if not os.environ.get(key):
                os.environ[key] = val
        logger.info(
            "[GovernedLoop] ~/.jarvis not writable — redirected state to %s",
            _fallback,
        )

_ensure_writable_state_dir()

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

MIN_GENERATION_BUDGET_S: float = float(
    os.getenv("JARVIS_MIN_GENERATION_BUDGET_S", "30.0")
)

# ---------------------------------------------------------------------------
# Compute-class admission constants and helpers
# ---------------------------------------------------------------------------

_COMPUTE_RANK: dict[str, int] = {
    "cpu": 0,
    "gpu_t4": 1,
    "gpu_l4": 2,
    "gpu_v100": 3,
    "gpu_a100": 4,
}


class ComputeClassMismatch(RuntimeError):
    """Raised when VM compute_class is below the brain's min_compute_class."""


def _check_compute_admission(brain_cfg: dict, capability: dict) -> None:
    """Hard-fail if VM compute_class < brain min_compute_class.

    Raises ComputeClassMismatch if VM rank < brain minimum rank.
    """
    vm_class = capability.get("compute_class", "cpu")
    min_class = brain_cfg.get("min_compute_class", "cpu")
    vm_rank = _COMPUTE_RANK.get(vm_class, 0)
    min_rank = _COMPUTE_RANK.get(min_class, 0)
    if vm_rank < min_rank:
        raise ComputeClassMismatch(
            f"VM compute_class={vm_class!r} (rank {vm_rank}) is below "
            f"brain min_compute_class={min_class!r} (rank {min_rank}). "
            f"Route to J-Prime is denied. Upgrade VM GPU or select a lower-tier brain."
        )


class ModelArtifactMismatch(RuntimeError):
    """Raised when VM model_artifact doesn't match policy model_artifact."""


def _check_artifact_integrity(brain_cfg: dict, capability: dict) -> None:
    """Hard-fail if model loaded on VM doesn't match policy's expected artifact.

    Comparison is case-insensitive to handle filesystem conventions.
    If either artifact is unknown/empty, skips the check.

    Raises:
        ModelArtifactMismatch: if filenames don't match (case-insensitive)
    """
    policy_artifact = brain_cfg.get("model_artifact", "")
    vm_artifact = capability.get("model_artifact", "")
    if not policy_artifact or not vm_artifact:
        return  # can't check — skip
    if policy_artifact.lower() != vm_artifact.lower():
        raise ModelArtifactMismatch(
            f"Model artifact mismatch: policy expects {policy_artifact!r} "
            f"but VM reports {vm_artifact!r}. "
            f"Update policy or reload correct model on VM."
        )


class HostBindingViolation(RuntimeError):
    """Raised when telemetry_host, selector_host, and execution_host don't all match."""


def _check_host_binding(
    telemetry_host: str,
    selector_host: str,
    execution_host: str,
) -> None:
    """Enforce the invariant: all three host references must be identical.

    This prevents scenarios where routing selects VM-A but execution reaches VM-B,
    or where local psutil data is incorrectly used for a remote route.

    Raises:
        HostBindingViolation: if any host differs from the others
    """
    hosts = {telemetry_host, selector_host, execution_host}
    if len(hosts) > 1:
        raise HostBindingViolation(
            f"Host-binding invariant violated: "
            f"telemetry_host={telemetry_host!r}, "
            f"selector_host={selector_host!r}, "
            f"execution_host={execution_host!r}. "
            f"All three must be identical."
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
    """DEPRECATED — retained for backward compat only. Do not use for routing.

    Use _expected_provider_from_brain() instead, which derives expected_provider
    from the BrainSelectionResult, not from local Mac resource pressure.
    """
    # Phase 1 P0: local Mac pressure must not influence GCP routing telemetry.
    # This function is kept so callers that haven't been migrated don't break at
    # import time; all call sites inside GLS now use _expected_provider_from_brain.
    if snap.pressure_for_load(active_ops) >= PressureLevel.CRITICAL:
        return "LOCAL_CLAUDE"
    return "GCP_PRIME_SPOT"


def _expected_provider_from_brain(brain: "BrainSelectionResult") -> str:  # type: ignore[name-defined]
    """Derive expected_provider from the BrainSelectionResult, NOT from local psutil.

    Respects the host-binding invariant: routing-authority fields in telemetry
    must reflect the actual brain selection outcome, not local Mac resource state.
    """
    tier = getattr(brain, "provider_tier", "gcp_prime").upper()
    # Normalise known tiers to a canonical form
    if tier.startswith("GCP"):
        return "GCP_PRIME_SPOT"
    if tier.startswith("CLAUDE") or tier == "CLAUDE_API":
        return "CLAUDE_API"
    if tier == "QUEUED":
        return "QUEUED"
    return tier


def _policy_reason_from_brain(brain: "BrainSelectionResult") -> str:  # type: ignore[name-defined]
    """Return the causal routing_reason from BrainSelectionResult.

    Replaces the pattern of using snap.pressure_for_load().name as policy_reason,
    which incorrectly stamped LOCAL Mac pressure as the routing policy authority.
    """
    return getattr(brain, "routing_reason", "unknown")


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
# Terminal classification helpers
# ---------------------------------------------------------------------------


def _classify_terminal(
    terminal_phase: "OperationPhase",
    provider_used: "str | None",
    reason_code: str,
    is_noop: bool,
) -> str:
    """Classify operation outcome into the terminal taxonomy.

    Returns one of: PRIMARY_SUCCESS, FALLBACK_SUCCESS, DEGRADED, TIMEOUT, NOOP
    """
    from backend.core.ouroboros.governance.op_context import OperationPhase
    if is_noop:
        return "NOOP"
    if terminal_phase == OperationPhase.COMPLETE:
        if provider_used and "prime" in provider_used.lower():
            return "PRIMARY_SUCCESS"
        elif provider_used:
            return "FALLBACK_SUCCESS"
        return "PRIMARY_SUCCESS"  # default for COMPLETE with no provider info
    if "timeout" in reason_code.lower() or "deadline" in reason_code.lower():
        return "TIMEOUT"
    return "DEGRADED"


def _classify_failure_signal_class(
    reason_code: str,
    *,
    rollback_occurred: bool = False,
) -> str:
    """Map a terminal reason into a coarse failure class for event consumers."""
    if rollback_occurred:
        return "rollback"
    reason = (reason_code or "").lower()
    if not reason:
        return "unknown"
    if any(token in reason for token in ("timeout", "deadline", "expired")):
        return "timeout"
    if any(token in reason for token in ("syntax", "indent")):
        return "syntax"
    if any(token in reason for token in ("validation", "verify", "test", "candidate", "source_drift")):
        return "validation"
    if any(token in reason for token in ("gate_blocked", "approval", "brain_not_admitted", "busy", "duplicate", "file_in_flight", "cost_gate")):
        return "policy"
    if any(token in reason for token in ("saga", "promote", "drift_detected")):
        return "saga"
    if any(token in reason for token in ("provider", "compute", "artifact", "capability", "host_binding", "dependency", "permission", "disk", "env", "unavailable")):
        return "env"
    if "change_engine" in reason or "apply" in reason:
        return "apply"
    if "l2_" in reason:
        return "repair"
    return "unknown"


def _build_proof_artifact(
    op_id: str,
    terminal_phase: "OperationPhase",
    terminal_class: str,
    provider_used: "str | None",
    model_id: "str | None",
    compute_class: "str | None",
    execution_host: "str | None",
    fallback_active: bool,
    phase_trail: "list[str]",
    generation_duration_s: float,
    total_duration_s: float,
) -> dict:
    """Build a structured proof artifact for a completed operation.

    This is written to the ledger and consumed by the observability layer.
    """
    return {
        "op_id": op_id,
        "terminal_phase": terminal_phase.name if hasattr(terminal_phase, "name") else str(terminal_phase),
        "terminal_class": terminal_class,
        "provider_used": provider_used,
        "model_id": model_id,
        "compute_class": compute_class,
        "execution_host": execution_host,
        "fallback_active": fallback_active,
        "phase_trail": phase_trail,
        "generation_duration_s": round(generation_duration_s, 3),
        "total_duration_s": round(total_duration_s, 3),
        "proof_ts_utc": datetime.now(tz=timezone.utc).isoformat(),
    }


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
    routing_reason: str = ""  # BrainSelectionResult.routing_reason; empty before brain selection
    terminal_class: str = "UNKNOWN"  # PRIMARY_SUCCESS | FALLBACK_SUCCESS | DEGRADED | TIMEOUT | NOOP


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
# Lazy helpers for optional L2 types
# ---------------------------------------------------------------------------


def _lazy_repair_budget_from_env() -> Any:
    """Lazily import RepairBudget and build it from environment variables.

    Using a module-level function (not a lambda) allows ``field(default_factory=...)``
    to reference it by name, satisfying frozen-dataclass requirements while
    avoiding a circular import at module load time.
    """
    from backend.core.ouroboros.governance.repair_engine import RepairBudget  # noqa: PLC0415
    return RepairBudget.from_env()


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
    generation_timeout_s: float = 180.0
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

    # L1 tool-use settings (Manifesto §6: tools enabled by default under
    # governance — Iron Gate + risk engine + approval gates are the safety net)
    tool_use_enabled: bool = True
    max_tool_rounds: int = 15
    tool_timeout_s: float = 30.0
    max_concurrent_tools: int = 2
    # Budget-derived tool-loop timing (fixes bt-2026-04-10-045911 where
    # max_tool_rounds × tool_timeout_s could exceed the generation budget
    # and every IMMEDIATE op died with tool_loop_deadline_exceeded).
    # ``None`` means "use BudgetPlan.build defaults" which derive from
    # the actual generation budget at run() time.
    tool_min_per_round_s: Optional[float] = None
    tool_final_write_reserve_s: Optional[float] = None

    # L2 self-repair settings (RepairBudget drives the repair loop)
    repair_budget: Any = field(default_factory=_lazy_repair_budget_from_env)
    l3_enabled: bool = True  # Gap #5: worktree isolation enabled by default (Manifesto §6)
    max_concurrent_execution_graphs: int = 2
    execution_graph_state_dir: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "ouroboros" / "execution_graphs"
    )
    l4_enabled: bool = False
    l4_state_dir: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "ouroboros" / "advanced_coordination"
    )

    @classmethod
    def from_env(cls, args: Any = None, project_root: Optional[Path] = None) -> GovernedLoopConfig:
        """Build config from environment variables with safe defaults.

        Resolution order (highest priority wins):
          1. Environment variables
          2. <repo_root>/.jarvis/governance.local.yaml
          3. <repo_root>/.jarvis/governance.yaml
          4. ~/.jarvis/governance.yaml  (global defaults)
          5. Hard-coded defaults below
        """
        import os
        from backend.core.ouroboros.governance.config_loader import load_layered_config

        resolved_root = project_root if project_root is not None else Path(
            os.getenv("JARVIS_PROJECT_ROOT", os.getcwd())
        )
        _yaml_cfg = load_layered_config(global_root=Path.home(), repo_root=resolved_root)

        def _cfg(key: str, env_var: str, default: str) -> str:
            env_val = os.environ.get(env_var)
            if env_val is not None:
                return env_val
            yaml_val = _yaml_cfg.get(key)
            if yaml_val is not None:
                return str(yaml_val)
            return default

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
                _cfg("generation_timeout_s", "JARVIS_GENERATION_TIMEOUT_S", "180")
            ),
            context_expansion_timeout_s=float(
                os.getenv("JARVIS_GOVERNED_EXPANSION_TIMEOUT", "30.0")
            ),
            approval_timeout_s=float(
                _cfg("approval_timeout_s", "JARVIS_APPROVAL_TIMEOUT_S", "600")
            ),
            health_probe_interval_s=float(
                os.getenv("JARVIS_GOVERNED_HEALTH_PROBE_INTERVAL", "30.0")
            ),
            max_concurrent_ops=int(
                _cfg("max_concurrent_ops", "JARVIS_GOVERNED_MAX_CONCURRENT_OPS", "2")
            ),
            cold_start_grace_s=float(os.environ.get("JARVIS_COLD_START_GRACE_S", "300")),
            approval_ttl_s=float(os.environ.get("JARVIS_APPROVAL_TTL_S", "1800")),
            pipeline_timeout_s=float(
                _cfg("pipeline_timeout_s", "JARVIS_PIPELINE_TIMEOUT_S", "600.0")
            ),
            tool_use_enabled=os.environ.get("JARVIS_GOVERNED_TOOL_USE_ENABLED", "true").lower() == "true",
            max_tool_rounds=int(os.environ.get("JARVIS_GOVERNED_TOOL_MAX_ROUNDS", "15")),
            tool_timeout_s=float(os.environ.get("JARVIS_GOVERNED_TOOL_TIMEOUT_S", "30")),
            max_concurrent_tools=int(os.environ.get("JARVIS_GOVERNED_TOOL_MAX_CONCURRENT", "2")),
            tool_min_per_round_s=(
                float(os.environ["JARVIS_TOOL_LOOP_MIN_PER_ROUND_S"])
                if "JARVIS_TOOL_LOOP_MIN_PER_ROUND_S" in os.environ else None
            ),
            tool_final_write_reserve_s=(
                float(os.environ["JARVIS_TOOL_LOOP_FINAL_WRITE_RESERVE_S"])
                if "JARVIS_TOOL_LOOP_FINAL_WRITE_RESERVE_S" in os.environ else None
            ),
            repair_budget=_lazy_repair_budget_from_env(),
            l3_enabled=os.environ.get("JARVIS_GOVERNED_L3_ENABLED", "true").lower() == "true",
            max_concurrent_execution_graphs=int(
                os.environ.get("JARVIS_GOVERNED_L3_MAX_CONCURRENT_GRAPHS", "2")
            ),
            execution_graph_state_dir=Path(
                os.environ.get(
                    "JARVIS_GOVERNED_L3_STATE_DIR",
                    str(Path.home() / ".jarvis" / "ouroboros" / "execution_graphs"),
                )
            ),
            l4_enabled=os.environ.get("JARVIS_GOVERNED_L4_ENABLED", "false").lower() == "true",
            l4_state_dir=Path(
                os.environ.get(
                    "JARVIS_GOVERNED_L4_STATE_DIR",
                    str(Path.home() / ".jarvis" / "ouroboros" / "advanced_coordination"),
                )
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
        say_fn: Optional[Any] = None,
    ) -> None:
        self._stack = stack
        self._prime_client = prime_client
        self._say_fn = say_fn
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

        # GAP 6: user-initiated stop signal bus (created in _build_components)
        self._user_signal_bus: Optional[UserSignalBus] = None

        # Built during start()
        self._orchestrator: Optional[GovernedOrchestrator] = None
        self._generator: Optional[CandidateGenerator] = None
        self._approval_provider: Optional[CLIApprovalProvider] = None
        self._validation_runner: Optional[Any] = None
        self._health_probe_task: Optional[asyncio.Task] = None
        self._exhaustion_watcher: Any = None
        self._hibernation_prober: Any = None
        self._hibernate_bridge: Any = None
        self._wake_bridge: Any = None
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
        self._performance_persistence: Optional[Any] = None
        self._event_dir: Optional[Path] = None
        self._oracle_indexer_task: Optional[asyncio.Task] = None
        self._oracle: Optional[Any] = None

        # C+ autonomy infrastructure
        self._command_bus: Optional[CommandBus] = None
        self._event_emitter: Optional[EventEmitter] = None
        self._feedback_engine: Optional[AutonomyFeedbackEngine] = None
        self._command_consumer_task: Optional[asyncio.Task] = None
        self._feedback_loop_task: Optional[asyncio.Task] = None
        self._safety_net: Optional[ProductionSafetyNet] = None
        self._subagent_scheduler: Optional[Any] = None
        # Miner graph coalescer — wired alongside the L3 scheduler below.
        self._graph_coalescer: Optional[Any] = None
        self._advanced_autonomy: Optional[Any] = None
        self._mcp_client: Optional[Any] = None  # Phase A: GovernanceMCPClient, wired in start()

        # Compute-class admission gate (set externally after fetching /v1/capability;
        # None = gate disabled — backward-compatible default)
        self._vm_capability: Optional[dict] = None

        # Concurrency & dedup
        self._active_ops: Set[str] = set()
        self._active_file_ops: Set[str] = set()  # canonical file paths currently in-flight
        self._completed_ops: Dict[str, OperationResult] = {}
        # Cooperative cancellation: op_ids requested for cancel via REPL /cancel
        self._cancel_requested: Set[str] = set()

    @property
    def state(self) -> ServiceState:
        return self._state

    @property
    def active_brain_set(self) -> FrozenSet[str]:
        """Immutable snapshot of the supervisor-admitted brain set."""
        return self._active_brain_set

    @property
    def oracle(self):
        """TheOracle instance (None until oracle_index_loop completes initialization)."""
        return self._oracle

    @property
    def exploration_fleet(self):
        """ExplorationFleet reference (None if not yet wired or import failed)."""
        return getattr(self, "_exploration_fleet_ref", None)

    @property
    def background_pool(self):
        """BackgroundAgentPool reference (None if not yet wired)."""
        return self._bg_pool

    @property
    def doubleword_provider(self):
        """DoublewordProvider reference (None if API key not set or build failed)."""
        return getattr(self, "_doubleword_ref", None)

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

        Idempotent — second call is no-op if already ACTIVE/DEGRADED/STARTING.
        On failure, sets state to FAILED with structured reason.
        Re-entrancy guard: raises RuntimeError if called concurrently.
        """
        if self._state in (ServiceState.ACTIVE, ServiceState.DEGRADED):
            return
        if self._state == ServiceState.STARTING:
            raise RuntimeError(
                "GLS re-entrancy detected: start() called while already STARTING. "
                "This would corrupt the FSM — aborting second call."
            )

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

            # Fetch and cache VM capability contract
            if self._prime_client is not None:
                try:
                    cap = await self._prime_client.fetch_capability()
                    self._vm_capability = cap
                    logger.info(
                        "[GLS] VM capability: compute_class=%s model=%s host=%s gpu_layers=%s tok_s=%s",
                        cap.get("compute_class"), cap.get("model_id"),
                        cap.get("host"), cap.get("gpu_layers"), cap.get("tok_s_estimate"),
                    )

                    # Boot-time hard-fail: verify VM compute_class satisfies the
                    # default (tier-1) brain's requirements before completing startup.
                    # Attribute path confirmed from per-op gate at ~line 1079:
                    #   self._brain_selector           -> RouteDecisionService
                    #   ._brain_selector               -> BrainSelector
                    #   ._policy                       -> dict loaded from brain_selection_policy.yaml
                    try:
                        _boot_policy = getattr(
                            getattr(self._brain_selector, "_brain_selector", None),
                            "_policy", {},
                        ) or {}
                        _tier1_brains = (
                            _boot_policy.get("routing", {})
                            .get("task_class_map", {})
                            .get("tier1", [])
                        )
                        _default_brain_id = _tier1_brains[0] if _tier1_brains else None
                        if _default_brain_id:
                            _all_entries = (
                                _boot_policy.get("brains", {}).get("required", [])
                                + _boot_policy.get("brains", {}).get("optional", [])
                            )
                            _boot_brain_cfg: dict = {}
                            for _e in _all_entries:
                                if isinstance(_e, dict):
                                    _bid = _e.get("brain_id") or _e.get("id")
                                    if _bid == _default_brain_id:
                                        _boot_brain_cfg = {k: v for k, v in _e.items() if k not in ("brain_id", "id")}
                                        break
                            if _boot_brain_cfg:
                                # Boot-time: only gate on compute class (does VM have
                                # the minimum GPU tier?).  Artifact integrity is checked
                                # per-operation in _preflight_check() where we know
                                # exactly which brain is being routed to — validating
                                # the tier-1 default brain's artifact at boot would
                                # hard-fail whenever the VM has a different model loaded
                                # (e.g. GPU VM running qwen-7B while tier1 default is
                                # phi3-1B).
                                _check_compute_admission(_boot_brain_cfg, cap)
                                logger.info(
                                    "[GLS] Boot-time compute-class validation passed for brain=%s",
                                    _default_brain_id,
                                )
                    except ComputeClassMismatch as exc:
                        logger.error("[GLS] Boot-time compute-class validation FAILED: %s", exc)
                        raise  # hard fail — do not complete startup below minimum compute class

                except ComputeClassMismatch:
                    raise  # propagate hard-fail boot validation errors
                except Exception as exc:
                    logger.warning("[GLS] Could not fetch capability (non-fatal): %s", exc)
                    self._vm_capability = None

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
                self._performance_persistence = persistence
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

            # C+ L2/L3: CommandBus + EventEmitter + optional subagent scheduler
            if self._command_bus is None:
                self._command_bus = CommandBus(maxsize=1000)
            if self._event_emitter is None:
                self._event_emitter = EventEmitter()
            fe_config = FeedbackEngineConfig(
                event_dir=self._event_dir or Path.home() / ".jarvis" / "reactor_events",
                state_dir=Path(os.environ.get(
                    "JARVIS_AUTONOMY_STATE_DIR",
                    str(Path.home() / ".jarvis" / "ouroboros" / "state"),
                )),
            )
            self._feedback_engine = AutonomyFeedbackEngine(
                command_bus=self._command_bus,
                config=fe_config,
                event_emitter=self._event_emitter,
            )
            self._feedback_engine.register_event_handlers(self._event_emitter)
            self._feedback_loop_task = asyncio.create_task(
                self._feedback_loop(), name="feedback_loop"
            )
            self._command_consumer_task = asyncio.create_task(
                self._command_consumer_loop(), name="command_consumer_loop"
            )

            # C+ L3: ProductionSafetyNet
            self._safety_net = ProductionSafetyNet(
                command_bus=self._command_bus,
                config=SafetyNetConfig(),
            )
            self._safety_net.register_event_handlers(self._event_emitter)
            if self._subagent_scheduler is not None:
                await self._subagent_scheduler.start()
                await self._subagent_scheduler.recover_inflight()

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

        # Stop L3 scheduler before background loops so no unit outlives GLS
        if self._subagent_scheduler is not None:
            await self._subagent_scheduler.stop()

        # Cancel curriculum and reactor event background tasks
        for task_attr in ("_curriculum_task", "_reactor_event_task", "_oracle_indexer_task",
                         "_feedback_loop_task", "_command_consumer_task"):
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

        # Stop EventChannelServer (DW 3-tier webhook receiver)
        _evt_ch = getattr(self, "_event_channel", None)
        if _evt_ch is not None:
            try:
                await _evt_ch.stop()
            except Exception:
                pass

        # Detach from stack
        self._detach_from_stack()
        self._state = ServiceState.INACTIVE
        logger.info("[GovernedLoop] Stopped")

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit_background(
        self,
        ctx: OperationContext,
        trigger_source: str = "unknown",
    ) -> str:
        """Submit an operation for non-blocking background execution.

        Returns the op_id immediately. Use get_background_result(op_id)
        to poll for completion.

        Falls back to synchronous submit() if BackgroundAgentPool is not available.
        """
        if self._bg_pool is not None:
            try:
                op_id = await self._bg_pool.submit(ctx)
                logger.info(
                    "[GovernedLoop] Background op submitted: %s (trigger=%s)",
                    op_id, trigger_source,
                )
                return op_id
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] Background submit failed, falling back to sync: %s", exc
                )
        # Fallback: run synchronously
        result = await self.submit(ctx, trigger_source)
        return result.op_id

    def get_background_result(self, op_id: str) -> Any:
        """Poll for a background operation result. Returns None if not ready."""
        if self._bg_pool is not None:
            return self._bg_pool.get_result(op_id)
        return None

    def request_cancel(self, op_id: str) -> bool:
        """Request cooperative cancellation of an in-flight operation.

        The orchestrator checks ``is_cancel_requested()`` at phase transitions.
        Returns True if the op was found active and the cancel was registered.
        """
        # Match by prefix — REPL users may provide abbreviated op_ids
        matched = [k for k in self._active_ops if k.startswith(op_id)]
        if not matched:
            return False
        for m in matched:
            self._cancel_requested.add(m)
        logger.info("[GovernedLoop] Cancel requested for op(s): %s", matched)
        return True

    def is_cancel_requested(self, op_id: str) -> bool:
        """Check if cancellation was requested for this operation."""
        return op_id in self._cancel_requested

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
            result = OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code=f"service_not_active:{self._state.name}",
                trigger_source=trigger_source,
                terminal_class="DEGRADED",
            )
            await self._emit_terminal_events(ctx=ctx, result=result)
            return result

        # Gate: concurrency limit
        if len(self._active_ops) >= self._config.max_concurrent_ops:
            result = OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="busy",
                trigger_source=trigger_source,
                terminal_class="DEGRADED",
            )
            await self._emit_terminal_events(ctx=ctx, result=result)
            return result

        # Gate: dedup
        dedupe_key = ctx.op_id
        if dedupe_key in self._active_ops:
            result = OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="duplicate:in_flight",
                trigger_source=trigger_source,
                terminal_class="DEGRADED",
            )
            await self._emit_terminal_events(ctx=ctx, result=result)
            return result
        if dedupe_key in self._completed_ops:
            result = OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="duplicate:already_completed",
                trigger_source=trigger_source,
                terminal_class="DEGRADED",
            )
            await self._emit_terminal_events(ctx=ctx, result=result)
            return result

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
                result = OperationResult(
                    op_id=ctx.op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    reason_code="file_in_flight",
                    trigger_source=trigger_source,
                    terminal_class="DEGRADED",
                )
                await self._emit_terminal_events(ctx=ctx, result=result)
                return result

        # Execute pipeline
        self._active_ops.add(dedupe_key)
        # --- Proactive Drive telemetry hook (entry) ---
        _pds = getattr(self, "_proactive_drive_service", None)
        if _pds is not None:
            _pds.record_sample("jarvis", depth=len(self._active_ops), latency_ms=0.0)
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
                result = OperationResult(
                    op_id=ctx.op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    reason_code="brain_not_admitted",
                    trigger_source=trigger_source,
                    terminal_class="DEGRADED",
                )
                await self._emit_terminal_events(
                    ctx=ctx,
                    result=result,
                    brain_id=brain.brain_id,
                    model_name=brain.model_name,
                )
                return result

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
                result = OperationResult(
                    op_id=ctx.op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    reason_code="cost_gate_triggered_queue",
                    trigger_source=trigger_source,
                    routing_reason=brain.routing_reason,
                    terminal_class="DEGRADED",
                )
                await self._emit_terminal_events(
                    ctx=ctx,
                    result=result,
                    brain_id=brain.brain_id,
                    model_name=brain.model_name,
                )
                return result

            intent_tel = RoutingIntentTelemetry(
                # Phase 1 P0: use brain-derived fields, NOT local Mac pressure.
                # expected_provider and policy_reason now reflect the actual brain
                # selection outcome (host-binding invariant).
                expected_provider=_expected_provider_from_brain(brain),
                policy_reason=_policy_reason_from_brain(brain),
                brain_id=brain.brain_id,
                brain_model=brain.model_name,
                routing_reason=brain.routing_reason,
                task_complexity=brain.task_complexity,
                estimated_prompt_tokens=brain.estimated_prompt_tokens,
                daily_spend_usd=self._brain_selector.daily_spend,
                schema_capability=getattr(brain, "schema_capability", "full_content_only"),
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

            if self._advanced_autonomy is not None:
                try:
                    memory_ctx = self._advanced_autonomy.build_strategic_memory_context(
                        goal=ctx.description,
                        target_files=ctx.target_files,
                    )
                    active_intent = self._advanced_autonomy.remember_user_intent(
                        op_id=ctx.op_id,
                        description=ctx.description,
                        target_files=ctx.target_files,
                        repo_scope=ctx.repo_scope,
                    )
                    ctx = ctx.with_strategic_memory_context(
                        strategic_intent_id=active_intent.intent_id,
                        strategic_memory_fact_ids=memory_ctx.fact_ids,
                        strategic_memory_prompt=memory_ctx.prompt_block,
                        strategic_memory_digest=memory_ctx.context_digest,
                    )
                except Exception as exc:
                    logger.warning(
                        "[GovernedLoop] L4 strategic memory unavailable for op=%s: %s",
                        ctx.op_id,
                        exc,
                    )

            # ── OUROBOROS.md human instruction injection ─────────────────────────
            # Load 3-tier instruction hierarchy and stamp onto ctx before pipeline.
            # Providers prepend this block to every generation prompt.
            try:
                from backend.core.ouroboros.governance.context_memory_loader import (
                    ContextMemoryLoader,
                )
                _instructions = ContextMemoryLoader(
                    project_root=self._config.project_root,
                ).load()
                if _instructions:
                    ctx = ctx.with_human_instructions(_instructions)
            except Exception as _cml_exc:
                logger.debug(
                    "[GovernedLoop] ContextMemoryLoader error (non-fatal): %s", _cml_exc
                )

            # ── Semantic Triage (DW 35B pre-analysis) ────────────────────────
            # Cheap LLM-powered pre-scan: detects no-ops, redirects, and enriches
            # context BEFORE the expensive generation pipeline runs.
            _semantic_triage = getattr(self, "_semantic_triage", None)
            if _semantic_triage is not None and getattr(_semantic_triage, "is_available", False):
                try:
                    from backend.core.ouroboros.governance.semantic_triage import (
                        TriageDecision,
                    )
                    _triage_result = await _semantic_triage.triage(ctx)

                    if _triage_result.decision == TriageDecision.NO_OP:
                        # Change already present — skip generation entirely
                        logger.info(
                            "[GovernedLoop] Semantic triage: NO_OP for op=%s — %s",
                            ctx.op_id[:12], _triage_result.no_op_reason,
                        )
                        duration = time.monotonic() - start_time
                        result = OperationResult(
                            op_id=ctx.op_id,
                            terminal_phase=OperationPhase.COMPLETE,
                            total_duration_s=duration,
                            reason_code="semantic_triage_no_op",
                            trigger_source=trigger_source,
                            routing_reason=brain.routing_reason,
                            terminal_class="NOOP",
                        )
                        self._completed_ops[dedupe_key] = result
                        await self._emit_terminal_events(
                            ctx=ctx, result=result,
                            brain_id=brain.brain_id, model_name=brain.model_name,
                        )
                        return result

                    elif _triage_result.decision == TriageDecision.REDIRECT:
                        # Real problem is in different files — log and inject as
                        # expanded context (OperationContext is immutable on target_files
                        # after creation, so we add redirect targets to expansion list).
                        if _triage_result.redirect_files:
                            logger.info(
                                "[GovernedLoop] Semantic triage: REDIRECT op=%s "
                                "from %s → also consider %s",
                                ctx.op_id[:12], ctx.target_files,
                                _triage_result.redirect_files,
                            )
                            # Add redirect targets as expanded files so the
                            # generation model receives their context too.
                            _expanded = tuple(ctx.expanded_context_files or ()) + tuple(
                                f for f in _triage_result.redirect_files
                                if f not in ctx.target_files
                            )
                            ctx = ctx.with_expanded_files(_expanded)

                    elif _triage_result.decision == TriageDecision.ENRICH:
                        # Inject triage insights into human_instructions
                        # (strategic_memory_context requires full L4 parameters;
                        # human_instructions is a clean append-only field).
                        _insights = _semantic_triage.format_for_prompt(_triage_result)
                        if _insights:
                            _existing = getattr(ctx, "human_instructions", "") or ""
                            ctx = ctx.with_human_instructions(
                                _existing + _insights,
                            )
                            logger.info(
                                "[GovernedLoop] Semantic triage: ENRICH op=%s "
                                "confidence=%.2f (%d chars injected)",
                                ctx.op_id[:12], _triage_result.confidence,
                                len(_insights),
                            )

                    # Emit triage heartbeat for ALL decisions so the dashboard
                    # can track triage statistics (NO_OP saves, PROCEED rate, etc.)
                    try:
                        await self._stack.comm.emit_heartbeat(
                            op_id=ctx.op_id,
                            phase="semantic_triage",
                            progress_pct=5.0,
                            triage_decision=_triage_result.decision.name,
                            triage_confidence=_triage_result.confidence,
                            triage_reason=getattr(_triage_result, "no_op_reason", ""),
                        )
                    except Exception:
                        pass

                except Exception as _triage_exc:
                    logger.debug(
                        "[GovernedLoop] Semantic triage error (non-fatal): %s", _triage_exc
                    )

            # Connectivity preflight (spends from deadline budget)
            if self._generator is not None and self._ledger is not None:
                early_exit = await self._preflight_check(ctx)
                if early_exit is not None:
                    duration = time.monotonic() - start_time
                    _reason = (
                        getattr(early_exit, "terminal_reason_code", "")
                        or early_exit.phase.name.lower()
                    )
                    _tc = _classify_terminal(early_exit.phase, None, _reason, is_noop=False)
                    result = OperationResult(
                        op_id=ctx.op_id,
                        terminal_phase=early_exit.phase,
                        total_duration_s=duration,
                        reason_code=_reason,
                        trigger_source=trigger_source,
                        routing_reason=brain.routing_reason,
                        terminal_class=_tc,
                    )
                    self._completed_ops[dedupe_key] = result
                    if self._ledger is not None:
                        _proof = _build_proof_artifact(
                            op_id=ctx.op_id,
                            terminal_phase=result.terminal_phase,
                            terminal_class=result.terminal_class,
                            provider_used=result.provider_used,
                            model_id=None,
                            compute_class=self._vm_capability.get("compute_class") if self._vm_capability else None,
                            execution_host=self._vm_capability.get("host") if self._vm_capability else None,
                            fallback_active=(result.terminal_class == "FALLBACK_SUCCESS"),
                            phase_trail=[p.name for p in getattr(ctx, "phase_trail", []) if hasattr(p, "name")],
                            generation_duration_s=result.generation_duration_s or 0.0,
                            total_duration_s=result.total_duration_s or 0.0,
                        )
                        await _record_ledger(ctx, self._ledger, OperationState.FAILED, _proof)
                    await self._emit_terminal_events(
                        ctx=ctx,
                        result=result,
                        brain_id=brain.brain_id,
                        model_name=brain.model_name,
                        rollback_occurred=bool(getattr(early_exit, "rollback_occurred", False)),
                        rollback_reason=_reason,
                    )
                    return result

            _pipeline_timeout = (
                self._config.pipeline_timeout_s + 60.0
            )  # +60s grace beyond deadline for post-COMPLETE bookkeeping
            try:
                # GAP 6: race orchestrator against user stop signal when bus is present.
                # The no-bus path uses shielded_wait_for so ledger writes survive timeout.
                if self._user_signal_bus is not None:
                    _op_task = asyncio.create_task(
                        self._orchestrator.run(ctx),
                        name=f"orchestrator/{ctx.op_id}",
                    )
                    _stop_task = asyncio.create_task(
                        self._user_signal_bus.wait_for_stop(),
                        name=f"stop-signal/{ctx.op_id}",
                    )
                    try:
                        _done, _pending = await asyncio.wait(
                            [_op_task, _stop_task],
                            timeout=_pipeline_timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        if not _stop_task.done():
                            _stop_task.cancel()

                    if _stop_task in _done:
                        # User stop: cancel orchestrator, fire EV_PREEMPT, return CANCELLED.
                        # _op_task.cancel() is fire-and-forget — the task will raise
                        # CancelledError at its next await point and unwind cleanly.
                        _op_task.cancel()
                        self._user_signal_bus.reset()
                        _fsm_ctx_now = self._fsm_contexts.get(ctx.op_id)
                        if self._fsm_executor is not None and _fsm_ctx_now is not None:
                            _preempt_seq = self._fsm_checkpoint_seq.get(ctx.op_id, 0) + 1
                            self._fsm_checkpoint_seq[ctx.op_id] = _preempt_seq
                            _preempt_ti = build_transition_input(
                                op_id=ctx.op_id,
                                phase="GENERATE",
                                event=LoopEvent.EV_PREEMPT,
                                ctx=_fsm_ctx_now,
                                checkpoint_seq=_preempt_seq,
                                metadata={"source": "user_signal_bus"},
                            )
                            try:
                                await self._fsm_executor.apply(_fsm_ctx_now, _preempt_ti)
                            except Exception as _exc:
                                logger.debug("[GovernedLoop] FSM EV_PREEMPT apply failed: %s", _exc)
                        duration = time.monotonic() - start_time
                        result = OperationResult(
                            op_id=ctx.op_id,
                            terminal_phase=OperationPhase.CANCELLED,
                            total_duration_s=duration,
                            reason_code="user_stop",
                            trigger_source=trigger_source,
                            routing_reason=brain.routing_reason,
                            terminal_class=_classify_terminal(
                                OperationPhase.CANCELLED, None, "user_stop", is_noop=False
                            ),
                        )
                        self._completed_ops[dedupe_key] = result
                        await self._emit_terminal_events(
                            ctx=ctx,
                            result=result,
                            brain_id=brain.brain_id,
                            model_name=brain.model_name,
                            rollback_reason="user_stop",
                        )
                        return result

                    elif not _done:
                        # Timeout: cancel op_task to stop the orphaned orchestrator run.
                        _op_task.cancel()
                        # Reset bus in case stop was signalled just as timeout fired.
                        self._user_signal_bus.reset()
                        # Timeout: neither finished — build CANCELLED result and return.
                        duration = time.monotonic() - start_time
                        result = OperationResult(
                            op_id=ctx.op_id,
                            terminal_phase=OperationPhase.CANCELLED,
                            total_duration_s=duration,
                            reason_code="pipeline_timeout",
                            trigger_source=trigger_source,
                            routing_reason=brain.routing_reason,
                            terminal_class=_classify_terminal(
                                OperationPhase.CANCELLED, None, "pipeline_timeout", is_noop=False
                            ),
                        )
                        self._completed_ops[dedupe_key] = result
                        if self._ledger is not None:
                            _proof = _build_proof_artifact(
                                op_id=ctx.op_id,
                                terminal_phase=result.terminal_phase,
                                terminal_class=result.terminal_class,
                                provider_used=result.provider_used,
                                model_id=None,
                                compute_class=self._vm_capability.get("compute_class") if self._vm_capability else None,
                                execution_host=self._vm_capability.get("host") if self._vm_capability else None,
                                fallback_active=False,
                                phase_trail=[p.name for p in getattr(ctx, "phase_trail", []) if hasattr(p, "name")],
                                generation_duration_s=0.0,
                                total_duration_s=result.total_duration_s or 0.0,
                            )
                            await _record_ledger(ctx, self._ledger, OperationState.FAILED, _proof)
                        await self._emit_terminal_events(
                            ctx=ctx,
                            result=result,
                            brain_id=brain.brain_id,
                            model_name=brain.model_name,
                            rollback_reason="pipeline_timeout",
                        )
                        logger.error(
                            "[GovernedLoop] orchestrator.run() exceeded %.0fs hard timeout for op=%s",
                            _pipeline_timeout, ctx.op_id,
                        )
                        return result

                    else:
                        # Op completed normally — retrieve result.
                        terminal_ctx = _op_task.result()

                else:
                    # No signal bus: existing shielded path (ledger writes survive timeout).
                    from backend.core.async_safety import shielded_wait_for as _shielded_wf
                    terminal_ctx = await _shielded_wf(
                        self._orchestrator.run(ctx),
                        timeout=_pipeline_timeout,
                        name=f"orchestrator.run/{ctx.op_id}",
                    )

            except asyncio.TimeoutError:
                logger.error(
                    "[GovernedLoop] orchestrator.run() exceeded %.0fs hard timeout for op=%s"
                    " (pipeline continues in background to allow COMPLETE phase to finish)",
                    _pipeline_timeout, ctx.op_id,
                )
                duration = time.monotonic() - start_time
                result = OperationResult(
                    op_id=ctx.op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    total_duration_s=duration,
                    reason_code="pipeline_timeout",
                    trigger_source=trigger_source,
                    routing_reason=brain.routing_reason,
                    terminal_class=_classify_terminal(
                        OperationPhase.CANCELLED, None, "pipeline_timeout", is_noop=False
                    ),
                )
                self._completed_ops[dedupe_key] = result
                if self._ledger is not None:
                    _proof = _build_proof_artifact(
                        op_id=ctx.op_id,
                        terminal_phase=result.terminal_phase,
                        terminal_class=result.terminal_class,
                        provider_used=result.provider_used,
                        model_id=None,
                        compute_class=self._vm_capability.get("compute_class") if self._vm_capability else None,
                        execution_host=self._vm_capability.get("host") if self._vm_capability else None,
                        fallback_active=False,
                        phase_trail=[p.name for p in getattr(ctx, "phase_trail", []) if hasattr(p, "name")],
                        generation_duration_s=0.0,
                        total_duration_s=result.total_duration_s or 0.0,
                    )
                    await _record_ledger(ctx, self._ledger, OperationState.FAILED, _proof)
                await self._emit_terminal_events(
                    ctx=ctx,
                    result=result,
                    brain_id=brain.brain_id,
                    model_name=brain.model_name,
                    rollback_reason="pipeline_timeout",
                )
                return result

            # Phase 4: record actual generation cost for cost gate persistence
            if terminal_ctx.generation:
                _gen = terminal_ctx.generation
                _provider_name = getattr(_gen, "provider_name", "unknown")
                _cost = getattr(_gen, "cost_usd", 0.0) or 0.0
                if _cost > 0.0:
                    self._brain_selector.record_cost(_provider_name, _cost)

            duration = time.monotonic() - start_time
            _provider_used = (
                getattr(terminal_ctx.generation, "provider_name", None)
                if terminal_ctx.generation else None
            )
            _is_noop = bool(
                terminal_ctx.generation and getattr(terminal_ctx.generation, "is_noop", False)
            )
            _gen_duration = (
                getattr(terminal_ctx.generation, "generation_duration_s", None)
                if terminal_ctx.generation else None
            )
            _model_id = (
                getattr(terminal_ctx.generation, "model_id", None)
                if terminal_ctx.generation else None
            )
            _reason_code = (
                getattr(terminal_ctx, "terminal_reason_code", "")
                or terminal_ctx.phase.name.lower()
            )
            _rollback_occurred = bool(getattr(terminal_ctx, "rollback_occurred", False))
            _tc = _classify_terminal(terminal_ctx.phase, _provider_used, _reason_code, is_noop=_is_noop)
            result = OperationResult(
                op_id=ctx.op_id,
                terminal_phase=terminal_ctx.phase,
                provider_used=_provider_used,
                generation_duration_s=_gen_duration,
                total_duration_s=duration,
                reason_code=_reason_code,
                trigger_source=trigger_source,
                routing_reason=brain.routing_reason,  # Phase 1 P0: causal code in ledger
                terminal_class=_tc,
            )

            # ── Canary slice metrics ─────────────────────────────────────────────────
            # record_operation matches file paths against registered slice prefixes.
            # Must be called after duration and _rollback_occurred are computed.
            if self._stack is not None and self._stack.canary is not None:
                _canary_success = terminal_ctx.phase is OperationPhase.COMPLETE
                _canary_files = (
                    getattr(terminal_ctx, "target_files", None) or ctx.target_files
                )
                for _canary_fp in (_canary_files or ()):
                    try:
                        self._stack.canary.record_operation(
                            file_path=str(_canary_fp),
                            success=_canary_success,
                            latency_s=duration,
                            rolled_back=_rollback_occurred,
                        )
                    except Exception as _canary_exc:
                        logger.debug(
                            "[GovernedLoop] canary.record_operation error: %s", _canary_exc
                        )

            self._completed_ops[dedupe_key] = result
            if self._ledger is not None:
                _proof = _build_proof_artifact(
                    op_id=ctx.op_id,
                    terminal_phase=result.terminal_phase,
                    terminal_class=result.terminal_class,
                    provider_used=result.provider_used,
                    model_id=_model_id,
                    compute_class=self._vm_capability.get("compute_class") if self._vm_capability else None,
                    execution_host=self._vm_capability.get("host") if self._vm_capability else None,
                    fallback_active=(result.terminal_class == "FALLBACK_SUCCESS"),
                    phase_trail=[p.name for p in getattr(ctx, "phase_trail", []) if hasattr(p, "name")],
                    generation_duration_s=result.generation_duration_s or 0.0,
                    total_duration_s=result.total_duration_s or 0.0,
                )
                await _record_ledger(
                    ctx, self._ledger,
                    OperationState.APPLIED,
                    _proof,
                )

            if self._advanced_autonomy is not None and terminal_ctx.phase is OperationPhase.COMPLETE:
                try:
                    self._advanced_autonomy.record_verified_outcome(
                        op_id=terminal_ctx.op_id,
                        description=terminal_ctx.description,
                        target_files=terminal_ctx.target_files,
                        repo_scope=terminal_ctx.repo_scope,
                        strategic_intent_id=getattr(terminal_ctx, "strategic_intent_id", ""),
                        provider_used=_provider_used or "",
                        routing_reason=brain.routing_reason,
                        benchmark_result=getattr(terminal_ctx, "benchmark_result", None),
                        is_noop=_is_noop,
                    )
                except Exception as exc:
                    logger.warning(
                        "[GovernedLoop] L4 verified outcome write failed for op=%s: %s",
                        terminal_ctx.op_id,
                        exc,
                    )

            await self._emit_terminal_events(
                ctx=ctx,
                result=result,
                brain_id=brain.brain_id,
                model_name=brain.model_name,
                rollback_occurred=_rollback_occurred,
                rollback_reason=_reason_code,
            )

            # ---- MCP external tool hooks (P5, fire-and-forget) ----
            if self._mcp_client is not None:
                try:
                    if terminal_ctx.phase is OperationPhase.POSTMORTEM:
                        await asyncio.wait_for(
                            self._mcp_client.on_postmortem(terminal_ctx),
                            timeout=12.0,
                        )
                    elif terminal_ctx.phase is OperationPhase.COMPLETE:
                        _applied = list(terminal_ctx.target_files) if not _is_noop else []
                        await asyncio.wait_for(
                            self._mcp_client.on_complete(terminal_ctx, _applied),
                            timeout=12.0,
                        )
                except Exception as _mcp_exc:
                    logger.debug("[GovernedLoop] MCP hook error: %s", _mcp_exc)

            return result

        finally:
            self._active_ops.discard(dedupe_key)
            # --- Proactive Drive telemetry hook (completion) ---
            _pds = getattr(self, "_proactive_drive_service", None)
            if _pds is not None:
                _elapsed_ms = (time.monotonic() - start_time) * 1000.0
                _pds.record_sample("jarvis", depth=len(self._active_ops), latency_ms=_elapsed_ms)
            # --- Graduation tracker hook (record outcome) ---
            _grad = getattr(self, "_graduation_tracker", None)
            if _grad is not None:
                try:
                    _success = hasattr(result, "terminal_phase") and str(getattr(result.terminal_phase, "name", "")).upper() == "COMPLETE"
                    _rolled = hasattr(result, "rolled_back") and getattr(result, "rolled_back", False)
                    _grad.record_operation_outcome(
                        op_id=ctx.op_id,
                        success=_success,
                        rolled_back=_rolled,
                    )
                except Exception:
                    pass
            for _canonical in _locked_files:
                self._active_file_ops.discard(_canonical)
            # Release intake router file-overlap locks (re-ingest queued signals)
            _intake = getattr(self, "_intake_router", None)
            if _intake is not None:
                try:
                    await _intake.release_op(ctx.op_id)
                except Exception:
                    pass
            # Phase 4: clean up per-op FSM context
            self._fsm_contexts.pop(ctx.op_id, None)
            self._fsm_checkpoint_seq.pop(ctx.op_id, None)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def _emit_outcome_events(
        self,
        *,
        op_id: str,
        terminal_phase: OperationPhase,
        provider_used: str,
        duration_s: float,
        reason_code: str,
        rollback_occurred: bool = False,
        failure_class: str = "",
        affected_files: Sequence[str] = (),
        brain_id: str = "",
        model_name: str = "",
        outcome_source: str = "governed_loop",
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit a normalized terminal outcome payload to the autonomy event bus."""
        emitter = getattr(self, "_event_emitter", None)
        if emitter is None:
            return

        resolved_failure_class = (
            failure_class
            or _classify_failure_signal_class(
                reason_code,
                rollback_occurred=rollback_occurred,
            )
        )
        success = (
            terminal_phase is OperationPhase.COMPLETE
            and not rollback_occurred
        )
        payload = {
            "op_id": op_id,
            "brain_id": brain_id,
            "model_name": model_name,
            "terminal_phase": terminal_phase.name,
            "provider": provider_used or "",
            "duration_s": duration_s or 0.0,
            "duration_ms": (duration_s or 0.0) * 1000.0,
            "rollback": rollback_occurred,
            "success": success,
            "error": "" if success else reason_code,
            "failure_class": resolved_failure_class,
            "affected_files": list(affected_files),
            "outcome_source": outcome_source,
        }
        if extra_payload:
            payload.update(extra_payload)

        try:
            await emitter.emit(AutonomyEventEnvelope(
                source_layer="L1",
                event_type=AutonomyEventType.OP_COMPLETED,
                payload=payload,
                op_id=op_id,
            ))
            if rollback_occurred:
                await emitter.emit(AutonomyEventEnvelope(
                    source_layer="L1",
                    event_type=AutonomyEventType.OP_ROLLED_BACK,
                    payload={
                        **payload,
                        "rollback_reason": reason_code,
                    },
                    op_id=op_id,
                ))
        except Exception:
            pass  # fault-isolated

    async def report_external_outcome(
        self,
        *,
        op_id: str,
        terminal_phase: OperationPhase,
        reason_code: str,
        rollback_occurred: bool = False,
        affected_files: Sequence[str] = (),
        provider_used: str = "",
        brain_id: str = "",
        model_name: str = "",
        duration_s: float = 0.0,
        failure_class: str = "",
        outcome_source: str = "external",
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Report a terminal outcome that happened outside submit()/orchestrator.

        Used for boot recovery and supervisor/manual rollback flows so L4,
        SafetyNet, and Reactor attribution observe the same event contract.
        """
        await self._emit_outcome_events(
            op_id=op_id,
            terminal_phase=terminal_phase,
            provider_used=provider_used,
            duration_s=duration_s,
            reason_code=reason_code,
            rollback_occurred=rollback_occurred,
            failure_class=failure_class,
            affected_files=affected_files,
            brain_id=brain_id,
            model_name=model_name,
            outcome_source=outcome_source,
            extra_payload=extra_payload,
        )

    async def _emit_terminal_events(
        self,
        *,
        ctx: OperationContext,
        result: OperationResult,
        brain_id: str = "",
        model_name: str = "",
        rollback_occurred: bool = False,
        rollback_reason: str = "",
        failure_class: str = "",
    ) -> None:
        """Emit terminal outcome events to advisory layers with rollback fidelity."""
        reason_code = rollback_reason or result.reason_code
        await self._emit_outcome_events(
            op_id=ctx.op_id,
            terminal_phase=result.terminal_phase,
            provider_used=result.provider_used or "",
            duration_s=result.total_duration_s or 0.0,
            reason_code=reason_code,
            rollback_occurred=rollback_occurred,
            failure_class=failure_class,
            affected_files=ctx.target_files,
            brain_id=brain_id,
            model_name=model_name,
            outcome_source="governed_loop",
        )

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
            "execution_graph_scheduler": (
                self._subagent_scheduler.health()
                if self._subagent_scheduler is not None
                else {"running": False, "reason": "disabled"}
            ),
            "strategic_memory": (
                self._advanced_autonomy.memory_stats()
                if self._advanced_autonomy is not None
                else {"enabled": False, "reason": "disabled"}
            ),
            "orphan_saga_branches": self._detect_orphan_branches(),
            "saga_bus": self._saga_bus.to_dict() if getattr(self, "_saga_bus", None) else {},
        }

    def _detect_orphan_branches(self) -> List[str]:
        """Detect orphaned saga branches across registered repos."""
        try:
            from backend.core.ouroboros.governance.saga.repo_lock import RepoLockManager
            mgr = RepoLockManager()
            # Prefer live registry (self._repo_registry) over config
            registry = self._repo_registry or getattr(self._config, "repo_registry", None)
            if registry is not None:
                roots = {
                    rc.name: rc.local_path
                    for rc in registry.list_enabled()
                }
            else:
                roots = {"jarvis": self._config.project_root}
            return mgr.detect_orphan_branches(roots)
        except Exception:
            return []

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

        # ── Degradation mode gate ────────────────────────────────────────────────
        _deg_ctrl = getattr(getattr(self, "_stack", None), "degradation", None)
        if _deg_ctrl is not None:
            from backend.core.ouroboros.governance.degradation import DegradationMode
            _deg_mode = _deg_ctrl.mode
            if _deg_mode >= DegradationMode.READ_ONLY_PLANNING:
                logger.warning(
                    "[GovernedLoop] Preflight: degradation_mode=%s blocks op %s",
                    _deg_mode.name,
                    ctx.op_id,
                )
                return ctx.advance(OperationPhase.CANCELLED)
            if _deg_mode == DegradationMode.REDUCED_AUTONOMY:
                # Only SAFE_AUTO ops are allowed in reduced autonomy mode.
                # risk_tier is set during the CLASSIFY phase — it is None at preflight
                # time for the normal pipeline flow.  Treat None as non-SAFE_AUTO
                # (fail-safe): the risk has not been evaluated yet, so we cannot
                # confirm the op is safe to proceed without full autonomy.
                from backend.core.ouroboros.governance.risk_engine import RiskTier
                _risk_tier = ctx.risk_tier
                if _risk_tier not in (RiskTier.SAFE_AUTO, RiskTier.NOTIFY_APPLY):
                    logger.warning(
                        "[GovernedLoop] Preflight: REDUCED_AUTONOMY blocks non-SAFE_AUTO op %s "
                        "(risk_tier=%s — None means not yet classified; fail-safe block)",
                        ctx.op_id,
                        _risk_tier,
                    )
                    return ctx.advance(OperationPhase.CANCELLED)

        # ── Compute-class admission gate ──────────────────────────────────────
        if self._vm_capability is not None:
            _brain_id = (
                ctx.telemetry.routing_intent.brain_id
                if ctx.telemetry is not None and ctx.telemetry.routing_intent is not None
                else None
            )
            if _brain_id:
                # Policy is stored as a list under brains.required; build a lookup dict.
                _policy = getattr(
                    getattr(self._brain_selector, "_brain_selector", None), "_policy", {}
                ) or {}
                _all_brain_entries = (
                    _policy.get("brains", {}).get("required", [])
                    + _policy.get("brains", {}).get("optional", [])
                )
                _brain_cfg: dict = {}
                for _entry in _all_brain_entries:
                    if isinstance(_entry, dict) and _entry.get("brain_id") == _brain_id:
                        _brain_cfg = _entry
                        break
                try:
                    _check_compute_admission(_brain_cfg, self._vm_capability)
                except ComputeClassMismatch as exc:
                    logger.error(
                        "[GLS] Compute admission DENIED for op=%s: %s", ctx.op_id, exc
                    )
                    raise

                # ── Model artifact integrity check ───────────────────────────────────
                # Only enforce for brains that target J-Prime (GPU brains).
                # CPU brains (phi3_lightweight etc.) route to Claude fallback and
                # never consume the GPU VM's model — checking them against the VM's
                # loaded artifact would produce false mismatches.
                _brain_compute = _brain_cfg.get("compute_class", "cpu")
                if _brain_compute != "cpu":
                    try:
                        _check_artifact_integrity(_brain_cfg, self._vm_capability)
                    except ModelArtifactMismatch as exc:
                        logger.error(
                            "[GLS] Artifact integrity DENIED for op=%s: %s", ctx.op_id, exc
                        )
                        raise

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
        except Exception as _probe_exc:
            logger.debug(
                "[GovernedLoop] Preflight: primary probe failed: %s",
                type(_probe_exc).__name__,
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

        # Build ToolLoopCoordinator if tool-use is enabled via config
        _tool_coordinator = None
        if self._config.tool_use_enabled:
            from backend.core.ouroboros.governance.tool_executor import (
                AsyncProcessToolBackend as _AsyncBE,
                GoverningToolPolicy as _GTP,
                ToolLoopCoordinator as _TLC,
            )
            _rr = repo_roots_map if repo_roots_map else {"jarvis": Path.cwd()}
            _policy  = _GTP(repo_roots=_rr)
            _backend = _AsyncBE(semaphore=asyncio.Semaphore(self._config.max_concurrent_tools))
            # Stash the backend on self so late-bound deps (ExplorationFleet
            # for delegate_to_agent, MCP client, etc.) can be attached after
            # downstream components are constructed further below.
            self._tool_backend = _backend
            # Real-time tool call display callback (Manifesto §7: Absolute Observability)
            # Fires for every lifecycle event emitted by ToolLoopCoordinator:
            # start / success / error / timeout / cancelled / denied.
            # Routed via ToolNarrationChannel (backend.core.ouroboros.governance.tool_narration)
            # which builds a real CommMessage, schedules delivery on the
            # running loop, and fault-isolates transport failures.
            from backend.core.ouroboros.governance.tool_narration import (
                ToolNarrationChannel as _TNC,
            )

            # The channel needs a CommProtocol reference, but the governance
            # stack isn't built yet at this point — use a late-bound lookup
            # inside the callback so the reference resolves at fire time.
            class _LateCommProxy:
                __slots__ = ("_gls",)

                def __init__(self, gls: "GovernedLoopService") -> None:
                    self._gls = gls

                @property
                def _transports(self) -> Any:
                    _comm = getattr(self._gls, "_governance_stack", None)
                    _comm = getattr(_comm, "comm", None) if _comm is not None else None
                    return getattr(_comm, "_transports", []) if _comm is not None else []

                async def _emit(self, msg: Any) -> None:
                    _gov = getattr(self._gls, "_governance_stack", None)
                    _comm = getattr(_gov, "comm", None) if _gov is not None else None
                    if _comm is None:
                        return
                    _emit_fn = getattr(_comm, "_emit", None)
                    if _emit_fn is not None:
                        await _emit_fn(msg)
                        return
                    # Fall back to direct transport fan-out.
                    for t in getattr(_comm, "_transports", []) or []:
                        try:
                            await t.send(msg)
                        except Exception:
                            logger.debug(
                                "[GovernedLoop] tool-narration transport %r failed",
                                t, exc_info=True,
                            )

            self._tool_narration = _TNC(_LateCommProxy(self))

            def _on_tool_call_display(**kwargs: Any) -> None:
                self._tool_narration.emit(
                    op_id=kwargs.get("op_id", "") or "",
                    tool_name=kwargs.get("tool_name", "") or "",
                    round_index=int(kwargs.get("round_index", 0) or 0),
                    args_summary=kwargs.get("args_summary", "") or "",
                    result_preview=kwargs.get("result_preview", "") or "",
                    duration_ms=float(kwargs.get("duration_ms", 0.0) or 0.0),
                    status=kwargs.get("status", "") or "",
                )

            _tool_coordinator = _TLC(
                backend=_backend, policy=_policy,
                max_rounds=self._config.max_tool_rounds,
                tool_timeout_s=self._config.tool_timeout_s,
                on_tool_call=_on_tool_call_display,
                min_per_round_s=self._config.tool_min_per_round_s,
                final_write_reserve_s=self._config.tool_final_write_reserve_s,
            )

            # Streaming token callback — pipes tokens through CommProtocol
            # so SerpentFlow can render live Markdown via rich.Live.
            # Uses asyncio.get_running_loop() (the non-deprecated API) and
            # logs delivery failures at DEBUG instead of swallowing them.
            def _on_streaming_token(token: str) -> None:
                try:
                    _gov = getattr(self, "_governance_stack", None)
                    _comm = getattr(_gov, "comm", None) if _gov is not None else None
                    if _comm is None:
                        return
                    try:
                        _loop = asyncio.get_running_loop()
                    except RuntimeError:
                        # No running loop — tokens can't be delivered.
                        return
                    from backend.core.ouroboros.governance.tool_narration import (
                        _DuckMessage as _TokDuck,
                    )
                    _tok_msg = _TokDuck(
                        op_id="",
                        payload={"streaming": "token", "token": token},
                    )
                    for _t in getattr(_comm, "_transports", []) or []:
                        try:
                            _loop.create_task(_t.send(_tok_msg))
                        except Exception:
                            logger.debug(
                                "[GovernedLoop] streaming-token delivery failed",
                                exc_info=True,
                            )
                except Exception:
                    logger.debug(
                        "[GovernedLoop] streaming-token outer failure",
                        exc_info=True,
                    )
            _tool_coordinator.on_token = _on_streaming_token

            logger.info(
                "[GovernedLoop] ToolLoopCoordinator wired: max_rounds=%d, timeout=%.1fs, concurrency=%d, streaming=ON",
                self._config.max_tool_rounds,
                self._config.tool_timeout_s,
                self._config.max_concurrent_tools,
            )

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
                    tool_loop=_tool_coordinator,
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
                    tool_loop=_tool_coordinator,
                )
                # Phase 3a: hold a reference so get_provider_stats() can
                # surface prompt-cache telemetry (hit rate, $ saved).
                self._claude_ref = fallback
                _cache_stats = fallback.get_cache_stats()
                logger.info(
                    "[GovernedLoop] ClaudeProvider: configured "
                    "(prompt_cache=%s, min_chars=%d)",
                    "on" if _cache_stats["enabled"] else "off",
                    _cache_stats["min_chars"],
                )
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] ClaudeProvider build failed: %s", exc
                )
                fallback = None

        # Build DoublewordProvider (Tier 0 — batch 397B MoE) if API key set
        tier0 = None
        _dw_api_key = os.environ.get("DOUBLEWORD_API_KEY", "")
        if _dw_api_key:
            try:
                from backend.core.ouroboros.governance.doubleword_provider import (
                    DoublewordProvider,
                )
                from backend.core.ouroboros.governance.rate_limiter import (
                    RateLimitService,
                )
                _dw_rate_limiter = RateLimitService()
                # Real-time mode: use /v1/chat/completions with Venom tool loop
                # Real-time SSE mode: /v1/chat/completions with token streaming.
                # Zero polling (Manifesto §3). Battle-tested latency: 20-40s
                # (comparable to batch 16-22s) but with streaming + Venom.
                # Default: ON. Opt out via DOUBLEWORD_REALTIME_ENABLED=false.
                _dw_realtime = os.environ.get(
                    "DOUBLEWORD_REALTIME_ENABLED", "true"
                ).lower() != "false"
                # DW 3-tier architecture (Manifesto §3: Zero polling. Pure reflex.)
                # Tier 1 webhook: BatchFutureRegistry resolves futures via webhook
                # Tier 2 adaptive poll: exponential backoff fallback
                _batch_registry = None
                try:
                    from backend.core.ouroboros.governance.batch_future_registry import (
                        BatchFutureRegistry,
                    )
                    _batch_registry = BatchFutureRegistry()
                    self._batch_registry = _batch_registry
                    logger.info("[GovernedLoop] BatchFutureRegistry: wired (Tier 1 webhook)")
                except Exception as _bfr_exc:
                    logger.debug("[GovernedLoop] BatchFutureRegistry skipped: %s", _bfr_exc)
                tier0 = DoublewordProvider(
                    api_key=_dw_api_key,
                    repo_root=self._config.project_root,
                    repo_roots=repo_roots_map,
                    rate_limiter=_dw_rate_limiter,
                    tool_loop=_tool_coordinator,  # Always wire Venom (RT uses it, batch ignores it)
                    realtime_enabled=_dw_realtime,
                    batch_registry=_batch_registry,
                )
                self._doubleword_ref = tier0
                _mode = "real-time + Venom" if _dw_realtime else "batch"
                logger.info(
                    "[GovernedLoop] DoublewordProvider: configured (model=%s, mode=%s)",
                    tier0._model, _mode,
                )
                # Boot Semantic Triage Engine (DW 35B pre-analysis)
                try:
                    from backend.core.ouroboros.governance.semantic_triage import (
                        SemanticTriageEngine,
                    )
                    self._semantic_triage = SemanticTriageEngine(
                        dw_provider=tier0,
                        project_root=self._config.project_root,
                    )
                    # Verify triage model is available on DW API (non-blocking, non-fatal)
                    _model_ok = await asyncio.wait_for(
                        self._semantic_triage.verify_model(), timeout=15.0,
                    )
                    if _model_ok:
                        logger.info(
                            "[GovernedLoop] SemanticTriageEngine: booted (model=%s, verified=True)",
                            self._semantic_triage._effective_model,
                        )
                    else:
                        logger.warning(
                            "[GovernedLoop] SemanticTriageEngine: triage model unavailable — disabled",
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[GovernedLoop] SemanticTriageEngine: model verification timed out — "
                        "proceeding with unverified model %s",
                        os.environ.get("OUROBOROS_TRIAGE_MODEL", "Qwen/Qwen3.5-35B-A3B-FP8"),
                    )
                except Exception as _triage_boot_exc:
                    logger.debug(
                        "[GovernedLoop] SemanticTriageEngine boot failed (non-fatal): %s",
                        _triage_boot_exc,
                    )
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] DoublewordProvider build failed: %s", exc
                )

        # Build CandidateGenerator (needs at least one provider)
        # When J-Prime (primary) is unhealthy at startup and Doubleword (tier0)
        # is available, promote Doubleword to PRIMARY so GENERATE doesn't waste
        # time on J-Prime timeouts. Doubleword.generate() blocks until the batch
        # completes — it's a valid synchronous provider.
        if tier0 is not None and not _primary_probe_ok:
            # Doubleword becomes primary; demote J-Prime to fallback (for recovery)
            _demoted = primary
            primary = tier0
            fallback = _demoted or fallback  # Keep J-Prime as fallback if it exists
            logger.info("[GovernedLoop] Promoting DoublewordProvider to PRIMARY (J-Prime unhealthy)")

        if primary is not None or fallback is not None:
            # If only one provider, use it as both (FSM still works)
            effective_primary = primary or fallback
            effective_fallback = fallback or primary
            assert effective_primary is not None
            assert effective_fallback is not None

            _pool_size = int(os.environ.get("JARVIS_BG_POOL_SIZE", "3"))
            _fallback_concurrency = int(os.environ.get(
                "JARVIS_FALLBACK_CONCURRENCY",
                str(min(_pool_size, 4)),
            ))
            logger.info(
                "[GovernedLoop] fallback_concurrency=%d (pool_size=%d, cap=4)",
                _fallback_concurrency, _pool_size,
            )

            # HIBERNATION_MODE step 5: wire the ProviderExhaustionWatcher
            # onto the CandidateGenerator so a sustained provider outage
            # trips the SupervisorOuroborosController into HIBERNATION
            # instead of crashing the loop with all_providers_exhausted.
            _exhaustion_watcher: Any = None
            _controller = (
                getattr(self._stack, "controller", None)
                if self._stack is not None
                else None
            )
            if _controller is not None:
                try:
                    from backend.core.ouroboros.governance.provider_exhaustion_watcher import (  # noqa: PLC0415  # type: ignore[import-not-found]
                        ProviderExhaustionWatcher,
                    )
                    _watcher_instance: Any = ProviderExhaustionWatcher(
                        controller=_controller,
                    )
                    logger.info(
                        "[GovernedLoop] ProviderExhaustionWatcher wired "
                        "(threshold=%s)",
                        getattr(_watcher_instance, "threshold", "?"),
                    )
                    _exhaustion_watcher = _watcher_instance
                except Exception as _watcher_exc:
                    logger.warning(
                        "[GovernedLoop] ProviderExhaustionWatcher wiring "
                        "failed (non-fatal): %s",
                        _watcher_exc,
                    )

            self._generator = CandidateGenerator(
                primary=effective_primary,
                fallback=effective_fallback,
                fallback_concurrency=_fallback_concurrency,
                tier0=tier0,
                ledger=self._ledger,
                exhaustion_watcher=_exhaustion_watcher,
            )
            self._exhaustion_watcher = _exhaustion_watcher

            # HIBERNATION_MODE step 6: construct a HibernationProber over the
            # real provider handles and attach it to the watcher so that
            # entering HIBERNATION automatically arms a wake loop. Sequencing:
            # the watcher must exist before the CandidateGenerator is built
            # (so the generator can call record_exhaustion/record_success),
            # while the prober needs the live provider objects — hence the
            # post-construction attach_prober() hook.
            if _exhaustion_watcher is not None and _controller is not None:
                try:
                    from backend.core.ouroboros.governance.hibernation_prober import (  # noqa: PLC0415  # type: ignore[import-not-found]
                        HibernationProber,
                    )
                    # De-dupe providers: effective_primary/effective_fallback
                    # alias to the same object when only one side exists.
                    _probe_targets: list[Any] = []
                    for _candidate in (tier0, effective_primary, effective_fallback):
                        if _candidate is None:
                            continue
                        if any(existing is _candidate for existing in _probe_targets):
                            continue
                        _probe_targets.append(_candidate)
                    _prober_instance: Any = HibernationProber(
                        controller=_controller,
                        providers=_probe_targets,
                    )
                    _attach = getattr(
                        _exhaustion_watcher, "attach_prober", None,
                    )
                    if _attach is not None:
                        _attach(_prober_instance)
                    self._hibernation_prober = _prober_instance
                    logger.info(
                        "[GovernedLoop] HibernationProber wired "
                        "(providers=%d)",
                        len(_probe_targets),
                    )
                except Exception as _prober_exc:
                    logger.warning(
                        "[GovernedLoop] HibernationProber wiring failed "
                        "(non-fatal): %s",
                        _prober_exc,
                    )

            # Sync FSM to reflect actual startup probe result.
            # Without this, the FSM stays at PRIMARY_READY even when the startup
            # probe failed, making the FALLBACK_ACTIVE branch in start() unreachable.
            # SKIP if Doubleword was promoted to primary — Doubleword didn't fail
            # the probe; the original PrimeProvider did. FSM should stay PRIMARY_READY.
            _doubleword_is_primary = (tier0 is not None and effective_primary is tier0)
            self._doubleword_is_primary = _doubleword_is_primary
            if primary is not None and not _primary_probe_ok and not _doubleword_is_primary and self._generator is not None:
                try:
                    self._generator.fsm.record_primary_failure()
                except Exception:
                    pass  # FSM transition error should not abort startup
        else:
            logger.warning(
                "[GovernedLoop] No providers available — QUEUE_ONLY mode"
            )
            self._generator = None

        # Wire L2 RepairEngine if enabled
        _repair_engine = None
        if getattr(self._config.repair_budget, "enabled", False):
            try:
                from backend.core.ouroboros.governance.repair_engine import RepairEngine  # noqa: PLC0415
                if primary is not None:
                    _repair_engine = RepairEngine(
                        budget=self._config.repair_budget,
                        prime_provider=primary,
                        repo_root=self._config.project_root,
                        ledger=self._ledger,
                    )
                    logger.info(
                        "[GovernedLoop] L2 RepairEngine wired: max_iterations=%d, timebox=%.1fs",
                        self._config.repair_budget.max_iterations,
                        self._config.repair_budget.timebox_s,
                    )
                else:
                    logger.warning("[GovernedLoop] L2 disabled: primary provider unavailable")
            except Exception as exc:
                logger.warning("[GovernedLoop] L2 RepairEngine build failed: %s", exc)

        # GAP 6: instantiate user signal bus (always present; silent until request_stop() called)
        self._user_signal_bus = UserSignalBus()

        # Build approval provider
        self._approval_provider = CLIApprovalProvider(
            project_root=self._config.project_root,
        )

        # Wire approval_provider into the tool backend for ask_human tool (Gap #4).
        # The backend was created earlier without it; we inject it now.
        if _tool_coordinator is not None:
            _be = getattr(_tool_coordinator, "_backend", None)
            if _be is not None and hasattr(_be, "_approval_provider"):
                _be._approval_provider = self._approval_provider

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
        self._validation_runner = validation_runner

        if self._command_bus is None:
            self._command_bus = CommandBus(maxsize=1000)
        if self._event_emitter is None:
            self._event_emitter = EventEmitter()
        if self._config.l4_enabled:
            from backend.core.ouroboros.governance.autonomy.advanced_coordination import (
                AdvancedAutonomyService,
                AdvancedCoordinationConfig,
            )

            self._advanced_autonomy = AdvancedAutonomyService(
                command_bus=self._command_bus,
                config=AdvancedCoordinationConfig(
                    state_dir=self._config.l4_state_dir,
                ),
            )
            if self._event_emitter is not None:
                self._advanced_autonomy.register_event_handlers(self._event_emitter)
            logger.info(
                "[GovernedLoop] L4 AdvancedAutonomyService wired: state_dir=%s",
                self._config.l4_state_dir,
            )
        else:
            self._advanced_autonomy = None

        if self._config.l3_enabled and self._generator is not None:
            from backend.core.ouroboros.governance.autonomy.execution_graph_progress import (
                ExecutionGraphProgressTracker,
                install_default_tracker,
            )
            from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
                ExecutionGraphStore,
            )
            from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
                GenerationSubagentExecutor,
                SubagentScheduler,
            )
            from backend.core.ouroboros.governance.saga.merge_coordinator import (
                MergeCoordinator,
            )

            # --- Worktree isolation for parallel subagents ---
            _wt_manager = None
            if getattr(self._config, "l3_enable_worktree_isolation", True):
                try:
                    from backend.core.ouroboros.governance.worktree_manager import WorktreeManager
                    _wt_base = getattr(
                        self._config, "l3_worktree_base",
                        Path.home() / ".jarvis" / "ouroboros" / "worktrees",
                    )
                    _wt_manager = WorktreeManager(
                        repo_root=self._config.project_root,
                        worktree_base=_wt_base,
                    )
                    logger.info("[GovernedLoop] WorktreeManager wired: base=%s", _wt_base)
                except ImportError:
                    logger.debug("[GovernedLoop] WorktreeManager not available — shared repo mode")

            # ExecutionGraph progress tracker — Phase 3b operational
            # visibility. Subscribes to the scheduler's event emitter
            # and exposes a per-graph snapshot for SerpentFlow.
            self._execution_graph_tracker = ExecutionGraphProgressTracker(
                self._event_emitter
            )
            install_default_tracker(self._execution_graph_tracker)
            logger.info(
                "[GovernedLoop] ExecutionGraphProgressTracker wired (enabled=%s)",
                self._execution_graph_tracker.stats().get("enabled"),
            )

            self._subagent_scheduler = SubagentScheduler(
                store=ExecutionGraphStore(self._config.execution_graph_state_dir),
                command_bus=self._command_bus,
                event_emitter=self._event_emitter,
                executor=GenerationSubagentExecutor(
                    generator=self._generator,
                    validation_runner=validation_runner,
                    repo_roots=repo_roots_map,
                    worktree_manager=_wt_manager,
                ),
                merge_coordinator=MergeCoordinator(),
                max_concurrent_graphs=self._config.max_concurrent_execution_graphs,
                progress_tracker=self._execution_graph_tracker,
            )
            logger.info(
                "[GovernedLoop] L3 SubagentScheduler wired: state_dir=%s max_graphs=%d",
                self._config.execution_graph_state_dir,
                self._config.max_concurrent_execution_graphs,
            )

            # Manifesto §3 parallel DAG: miner signal coalescer — collapses
            # N same-strategy miner candidates into a single ExecutionGraph
            # instead of N independent ops. Lights up the Phase 3b
            # ExecutionGraphProgressTracker with a real multi-op workload.
            try:
                from backend.core.ouroboros.governance.graph_coalescer import (
                    MinerGraphCoalescer,
                )
                self._graph_coalescer: Optional[Any] = MinerGraphCoalescer(
                    scheduler=self._subagent_scheduler,
                    repo="jarvis",
                )
                logger.info(
                    "[GovernedLoop] MinerGraphCoalescer wired "
                    "(enabled=%s, auto_submit=%s)",
                    self._graph_coalescer.enabled,
                    getattr(self._graph_coalescer, "_auto_submit", False),
                )
            except Exception as _coalescer_exc:
                logger.warning(
                    "[GovernedLoop] MinerGraphCoalescer wire failed: %s",
                    _coalescer_exc,
                )
                self._graph_coalescer = None
        else:
            self._subagent_scheduler = None
            self._graph_coalescer = None

        # Create SagaMessageBus for passive saga observability
        try:
            from backend.core.ouroboros.governance.autonomy.saga_messages import SagaMessageBus
            self._saga_bus = SagaMessageBus(max_messages=500)
            logger.info("[GovernedLoop] SagaMessageBus created (max_messages=500)")
        except ImportError:
            self._saga_bus = None
            logger.debug("[GovernedLoop] SagaMessageBus unavailable — saga_messages not found")

        # Shadow harness — enabled via JARVIS_SHADOW_HARNESS_ENABLED=true
        _shadow_harness = None
        if os.environ.get("JARVIS_SHADOW_HARNESS_ENABLED", "false").lower() in ("true", "1"):
            from backend.core.ouroboros.governance.shadow_harness import ShadowHarness
            _shadow_harness = ShadowHarness(
                confidence_threshold=float(os.environ.get("JARVIS_SHADOW_CONFIDENCE_THRESHOLD", "0.7")),
                disqualify_after=int(os.environ.get("JARVIS_SHADOW_DISQUALIFY_AFTER", "3")),
            )
            logger.info(
                "[GovernedLoop] ShadowHarness wired: confidence_threshold=%.2f, disqualify_after=%d",
                float(os.environ.get("JARVIS_SHADOW_CONFIDENCE_THRESHOLD", "0.7")),
                int(os.environ.get("JARVIS_SHADOW_DISQUALIFY_AFTER", "3")),
            )

        # Build orchestrator
        # When Doubleword is primary, disable context expansion — its batch API
        # is too slow for plan() calls (~2-4 min). Sub-project B provides full
        # file context + AST index, making expansion less critical.
        _dw_primary = getattr(self, "_doubleword_is_primary", False)
        _ctx_expansion = self._config.context_expansion_enabled if hasattr(self._config, "context_expansion_enabled") else True
        if _dw_primary:
            _ctx_expansion = False
            logger.info("[GovernedLoop] Context expansion disabled (Doubleword primary — batch API too slow for plan())")

        orch_config = OrchestratorConfig(
            project_root=self._config.project_root,
            repo_registry=repo_registry,
            generation_timeout_s=self._config.generation_timeout_s,
            context_expansion_timeout_s=self._config.context_expansion_timeout_s,
            context_expansion_enabled=_ctx_expansion,
            approval_timeout_s=self._config.approval_timeout_s,
            message_bus=self._saga_bus,
            repair_engine=_repair_engine,
            execution_graph_scheduler=self._subagent_scheduler,
            shadow_harness=_shadow_harness,
        )
        self._orchestrator = GovernedOrchestrator(
            stack=self._stack,
            generator=self._generator,
            approval_provider=self._approval_provider,
            config=orch_config,
            validation_runner=validation_runner,
        )

        # ---- Wire ReasoningChainBridge (P1) ----
        try:
            from backend.core.ouroboros.governance.reasoning_chain_bridge import ReasoningChainBridge
            _reasoning_bridge = ReasoningChainBridge(comm=self._stack.comm)
            if _reasoning_bridge.is_active:
                self._orchestrator.set_reasoning_bridge(_reasoning_bridge)
                logger.info("[GLS] ReasoningChainBridge wired (phase=%s)",
                            getattr(getattr(_reasoning_bridge, '_orchestrator', None), '_config', None))
            else:
                logger.debug("[GLS] ReasoningChainBridge: chain not active (env flags not set)")
        except Exception as exc:
            logger.debug("[GLS] ReasoningChainBridge skipped: %s", exc)

        # ---- Wire InfrastructureApplicator (Boundary Principle: deterministic post-APPLY) ----
        try:
            from backend.core.ouroboros.governance.infrastructure_applicator import (
                InfrastructureApplicator,
            )
            _infra = InfrastructureApplicator(project_root=self._config.project_root)
            if _infra.is_enabled:
                self._orchestrator.set_infra_applicator(_infra)
                logger.info("[GLS] InfrastructureApplicator wired (deterministic post-APPLY)")
            else:
                logger.debug("[GLS] InfrastructureApplicator: disabled via env")
        except Exception as exc:
            logger.debug("[GLS] InfrastructureApplicator skipped: %s", exc)

        # ---- Wire GovernanceMCPClient (P5) ----
        self._mcp_client = None
        try:
            from backend.core.ouroboros.governance.mcp_tool_client import GovernanceMCPClient
            _mcp = GovernanceMCPClient()
            if _mcp.is_enabled:
                self._mcp_client = _mcp
                # Gap #7: inject MCP client into tool backend for MCP tool dispatch
                if _tool_coordinator is not None:
                    _be = getattr(_tool_coordinator, "_backend", None)
                    if _be is not None and hasattr(_be, "_mcp_client"):
                        _be._mcp_client = _mcp
                # Gap #7: inject MCP client into providers for tool section rendering
                if self._doubleword_ref is not None and hasattr(self._doubleword_ref, "_mcp_client"):
                    self._doubleword_ref._mcp_client = _mcp
                logger.info("[GLS] GovernanceMCPClient wired (tools forwarded to generation context)")
            else:
                logger.debug("[GLS] GovernanceMCPClient: no servers configured")
        except Exception as exc:
            logger.debug("[GLS] GovernanceMCPClient skipped: %s", exc)

        # ---- Wire EventChannelServer (DW 3-tier: webhook-driven batch) ----
        self._event_channel = None
        try:
            from backend.core.ouroboros.governance.event_channel import EventChannelServer
            _batch_reg = getattr(self, "_batch_registry", None)
            _intake = getattr(self, "_intake_router", None)
            if _intake is not None:
                _evt_channel = EventChannelServer(
                    router=_intake,
                    batch_registry=_batch_reg,
                )
                if _evt_channel.is_enabled:
                    await _evt_channel.start()
                    self._event_channel = _evt_channel
                    logger.info(
                        "[GLS] EventChannelServer started (batch_registry=%s)",
                        "wired" if _batch_reg is not None else "none",
                    )
                else:
                    logger.debug("[GLS] EventChannelServer: disabled via env")
        except Exception as exc:
            logger.debug("[GLS] EventChannelServer skipped: %s", exc)

        # ---- Wire ReasoningNarrator (P0 Wiring: WHY-not-WHAT explanations) ----
        try:
            from backend.core.ouroboros.governance.reasoning_narrator import ReasoningNarrator
            _say = getattr(self, "_say_fn", None)
            _narrator = ReasoningNarrator(say_fn=_say)
            self._orchestrator.set_reasoning_narrator(_narrator)
            logger.info("[GLS] ReasoningNarrator wired (explains WHY decisions were made)")
        except Exception as exc:
            logger.debug("[GLS] ReasoningNarrator skipped: %s", exc)

        # ---- Wire OperationDialogueStore (P0 Wiring: reasoning journal) ----
        try:
            from backend.core.ouroboros.governance.operation_dialogue import OperationDialogueStore
            _dialogue_store = OperationDialogueStore()
            self._orchestrator.set_dialogue_store(_dialogue_store)
            logger.info("[GLS] OperationDialogueStore wired (per-op reasoning journal)")
        except Exception as exc:
            logger.debug("[GLS] OperationDialogueStore skipped: %s", exc)

        # ---- Wire PreActionNarrator (real-time WHAT-before-action voice) ----
        try:
            from backend.core.ouroboros.governance.pre_action_narrator import PreActionNarrator
            _say = getattr(self, "_say_fn", None)
            _pan = PreActionNarrator(say_fn=_say)
            self._orchestrator.set_pre_action_narrator(_pan)
            logger.info("[GLS] PreActionNarrator wired (real-time WHAT before each phase)")
        except Exception as exc:
            logger.debug("[GLS] PreActionNarrator skipped: %s", exc)

        # ---- Wire ExplorationFleet (parallel codebase exploration) ----
        try:
            from backend.core.ouroboros.governance.exploration_fleet import ExplorationFleet
            _fleet = ExplorationFleet(jarvis_root=self._config.project_root)
            self._exploration_fleet_ref = _fleet
            self._orchestrator.set_exploration_fleet(_fleet)
            # Phase 2: wire the same fleet into the tool backend so
            # Venom's delegate_to_agent tool can spawn isolated sub-agents.
            _backend_ref = getattr(self, "_tool_backend", None)
            if _backend_ref is not None and hasattr(_backend_ref, "set_exploration_fleet"):
                _backend_ref.set_exploration_fleet(_fleet)
                logger.info(
                    "[GLS] ExplorationFleet wired (orchestrator CONTEXT_EXPANSION + Venom delegate_to_agent)"
                )
            else:
                logger.info(
                    "[GLS] ExplorationFleet wired (orchestrator CONTEXT_EXPANSION only — tool backend not present)"
                )
        except Exception as exc:
            logger.debug("[GLS] ExplorationFleet skipped: %s", exc)

        # ---- Wire Self-Critique Engine (Phase 3a — post-VERIFY quality signal) ----
        # Cheap DW critique over the applied diff against the original goal.
        # Poor ratings become FEEDBACK memories; excellent ratings reinforce
        # file reputation. Fully non-blocking — all failures are swallowed.
        self._critique_engine = None
        try:
            _self_critique_enabled = (
                os.environ.get("JARVIS_SELF_CRITIQUE_ENABLED", "true").lower() == "true"
            )
            if _self_critique_enabled:
                from backend.core.ouroboros.governance.self_critique import (
                    CritiqueEngine,
                    DoublewordCritiqueProvider,
                    set_default_engine,
                )
                from backend.core.ouroboros.governance.user_preference_memory import (
                    get_default_store,
                )
                _dw_ref = getattr(self, "_doubleword_ref", None)
                if _dw_ref is not None:
                    _critique_provider = DoublewordCritiqueProvider(
                        dw_provider=_dw_ref,
                        max_tokens=int(
                            os.environ.get("JARVIS_CRITIQUE_MAX_TOKENS", "512")
                        ),
                    )
                    _user_store = None
                    try:
                        _user_store = get_default_store()
                    except Exception:
                        _user_store = None
                    _memory_engine = None
                    try:
                        _consciousness = getattr(self, "_consciousness", None)
                        if _consciousness is not None:
                            _memory_engine = getattr(
                                _consciousness, "memory_engine", None,
                            )
                    except Exception:
                        _memory_engine = None
                    self._critique_engine = CritiqueEngine(
                        provider=_critique_provider,
                        repo_root=self._config.project_root,
                        user_preference_store=_user_store,
                        memory_engine=_memory_engine,
                    )
                    self._orchestrator.set_critique_engine(self._critique_engine)
                    set_default_engine(self._critique_engine)
                    logger.info(
                        "[GLS] Self-critique engine wired (provider=doubleword, "
                        "user_prefs=%s, memory_engine=%s)",
                        "yes" if _user_store is not None else "no",
                        "yes" if _memory_engine is not None else "no",
                    )
                else:
                    logger.info(
                        "[GLS] Self-critique skipped: DoublewordProvider not available"
                    )
            else:
                logger.info("[GLS] Self-critique disabled via JARVIS_SELF_CRITIQUE_ENABLED")
        except Exception as exc:
            logger.debug("[GLS] Self-critique wiring skipped: %s", exc)

        # ---- Wire UnlimitedFleetOrchestrator (recursive agent spawning) ----
        self._unlimited_fleet = None
        try:
            from backend.core.ouroboros.governance.unlimited_agents import UnlimitedFleetOrchestrator
            self._unlimited_fleet = UnlimitedFleetOrchestrator(
                jarvis_root=self._config.project_root,
            )
            logger.info("[GLS] UnlimitedFleetOrchestrator wired (recursive agent spawning)")
        except Exception as exc:
            logger.debug("[GLS] UnlimitedFleetOrchestrator skipped: %s", exc)

        # ---- Wire HybridTeammateExecutor (coroutine/subprocess routing) ----
        self._hybrid_executor = None
        try:
            from backend.core.ouroboros.governance.hybrid_teammate_executor import (
                HybridTeammateExecutor,
            )
            self._hybrid_executor = HybridTeammateExecutor(
                project_root=self._config.project_root,
            )
            logger.info(
                "[GLS] HybridTeammateExecutor wired (cognitive=coroutine, mutation=subprocess)"
            )
        except Exception as exc:
            logger.debug("[GLS] HybridTeammateExecutor skipped: %s", exc)

        # ---- Wire BackgroundAgentPool (non-blocking operation submission) ----
        self._bg_pool = None
        try:
            from backend.core.ouroboros.governance.background_agent_pool import (
                BackgroundAgentPool,
            )
            self._bg_pool = BackgroundAgentPool(orchestrator=self._orchestrator)
            await self._bg_pool.start()
            logger.info(
                "[GLS] BackgroundAgentPool started (pool_size=%d, queue_size=%d)",
                self._bg_pool._pool_size, self._bg_pool._queue_size,
            )
        except Exception as exc:
            logger.debug("[GLS] BackgroundAgentPool skipped: %s", exc)

        # ---- HIBERNATION_MODE step 6.5: bridge controller transitions ----
        # Register hibernation hooks on the SupervisorOuroborosController so
        # that enter_hibernation() actually pauses the BG pool and freezes
        # the idle watchdog (and wake_from_hibernation() restores them).
        # Without this bridge the controller's mode flip is purely cosmetic
        # and the organism keeps burning work during a provider outage,
        # which is the exact failure HIBERNATION is supposed to prevent.
        #
        # Hooks receive a keyword ``reason`` and may be sync — pool.pause /
        # watchdog.freeze are already sync and idempotent. Pause-first,
        # resume-last ordering mirrors lock acquisition: shut the gate
        # before freezing the clock, unfreeze the clock before reopening
        # the gate, so observers never see "unfrozen & paused".
        _ctrl_for_hooks = (
            getattr(self._stack, "controller", None)
            if self._stack is not None
            else None
        )
        if (
            _ctrl_for_hooks is not None
            and hasattr(_ctrl_for_hooks, "register_hibernation_hooks")
        ):
            _bg_ref = self._bg_pool
            _watchdog_ref = getattr(self, "_idle_watchdog", None)

            def _hibernate_bridge(*, reason: str) -> None:
                # Pause the pool first so in-flight workers drain to the
                # unpaused gate before any later hook observes state.
                if _bg_ref is not None:
                    try:
                        _bg_ref.pause(reason=f"hibernation: {reason}")
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "[GLS] bg_pool.pause failed under hibernation"
                        )
                if _watchdog_ref is not None:
                    try:
                        _watchdog_ref.freeze(reason=f"hibernation: {reason}")
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "[GLS] idle_watchdog.freeze failed under hibernation"
                        )

            def _wake_bridge(*, reason: str) -> None:
                # Unfreeze first so the watchdog resets its clock before
                # the pool starts dequeuing — avoids a spurious stale-fire
                # immediately on wake against stale _last_poke.
                if _watchdog_ref is not None:
                    try:
                        _watchdog_ref.unfreeze(reason=f"wake: {reason}")
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "[GLS] idle_watchdog.unfreeze failed under wake"
                        )
                if _bg_ref is not None:
                    try:
                        _bg_ref.resume(reason=f"wake: {reason}")
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "[GLS] bg_pool.resume failed under wake"
                        )

            try:
                _ctrl_for_hooks.register_hibernation_hooks(
                    on_hibernate=_hibernate_bridge,
                    on_wake=_wake_bridge,
                    name="governed_loop_service",
                )
                self._hibernate_bridge = _hibernate_bridge
                self._wake_bridge = _wake_bridge
                logger.info(
                    "[GLS] hibernation hooks registered on controller "
                    "(pool=%s, watchdog=%s)",
                    "yes" if _bg_ref is not None else "no",
                    "yes" if _watchdog_ref is not None else "no",
                )
            except Exception as _hook_exc:
                logger.warning(
                    "[GLS] hibernation hook registration failed "
                    "(non-fatal): %s",
                    _hook_exc,
                )

        # ---- Wire LifecycleHookEngine (P1: 15 lifecycle events) ----
        self._hook_engine = None
        try:
            from backend.core.ouroboros.governance.lifecycle_hooks import get_hook_engine, HookEvent
            self._hook_engine = get_hook_engine()
            asyncio.get_event_loop().create_task(
                self._hook_engine.fire(HookEvent.SESSION_START, {"service": "GLS"})
            )
            logger.info("[GLS] LifecycleHookEngine wired (15 event types)")
        except Exception as exc:
            logger.debug("[GLS] LifecycleHookEngine skipped: %s", exc)

        # ---- Wire ContextCompactor (P1: auto-compact large dialogues) ----
        self._compactor = None
        try:
            from backend.core.ouroboros.governance.context_compaction import ContextCompactor
            self._compactor = ContextCompactor()
            logger.info("[GLS] ContextCompactor wired (auto-compact at threshold)")
        except Exception as exc:
            logger.debug("[GLS] ContextCompactor skipped: %s", exc)

        # ---- Wire AgentMemoryStore (P1: persistent per-agent memory) ----
        self._agent_memory_factory = None
        try:
            from backend.core.ouroboros.governance.agent_memory import get_agent_memory, MemoryScope
            self._agent_memory_factory = get_agent_memory
            logger.info("[GLS] AgentMemoryStore factory wired (USER/PROJECT/LOCAL scopes)")
        except Exception as exc:
            logger.debug("[GLS] AgentMemoryStore skipped: %s", exc)

        # ---- Wire PlanModeExecutor (P1: read-only dry-run) ----
        self._plan_executor = None
        try:
            from backend.core.ouroboros.governance.plan_mode import PlanModeExecutor
            self._plan_executor = PlanModeExecutor()
            logger.info("[GLS] PlanModeExecutor wired (read-only plan mode)")
        except Exception as exc:
            logger.debug("[GLS] PlanModeExecutor skipped: %s", exc)

        # ---- Wire ScopedToolGate (P1: per-agent tool restrictions) ----
        self._scoped_tool_gate = None
        try:
            from backend.core.ouroboros.governance.scoped_tool_access import get_scope_for_role
            self._scoped_tool_gate = get_scope_for_role
            logger.info("[GLS] ScopedToolGate wired (role-based tool restrictions)")
        except Exception as exc:
            logger.debug("[GLS] ScopedToolGate skipped: %s", exc)

        # ---- Wire DeferredToolRegistry (P2: lazy tool loading) ----
        self._tool_registry = None
        try:
            from backend.core.ouroboros.governance.deferred_tool_registry import get_tool_registry
            self._tool_registry = get_tool_registry()
            logger.info(
                "[GLS] DeferredToolRegistry wired (%d tools, lazy loading)",
                len(self._tool_registry.list_available()),
            )
        except Exception as exc:
            logger.debug("[GLS] DeferredToolRegistry skipped: %s", exc)

        # ---- Wire CheckpointManager (P2: interactive rewind) ----
        self._checkpoint_mgr = None
        try:
            from backend.core.ouroboros.governance.checkpoint_rewind import CheckpointManager
            self._checkpoint_mgr = CheckpointManager(project_root=self._config.project_root)
            logger.info("[GLS] CheckpointManager wired (git-based rewind)")
        except Exception as exc:
            logger.debug("[GLS] CheckpointManager skipped: %s", exc)

        # ---- Wire ScheduledAgentRunner (P2: cron-based recurring agents) ----
        self._scheduler = None
        try:
            from backend.core.ouroboros.governance.scheduled_agents import ScheduledAgentRunner
            self._scheduler = ScheduledAgentRunner(gls=self)
            asyncio.get_event_loop().create_task(self._scheduler.start())
            logger.info("[GLS] ScheduledAgentRunner started (cron-based agent scheduling)")
        except Exception as exc:
            logger.debug("[GLS] ScheduledAgentRunner skipped: %s", exc)

        # ---- Wire MultiFileRefactorEngine (P2: atomic cross-file changes) ----
        self._refactor_engine = None
        try:
            from backend.core.ouroboros.governance.multi_file_refactor import MultiFileRefactorEngine
            self._refactor_engine = MultiFileRefactorEngine(
                project_root=self._config.project_root,
            )
            logger.info("[GLS] MultiFileRefactorEngine wired (atomic cross-file refactoring)")
        except Exception as exc:
            logger.debug("[GLS] MultiFileRefactorEngine skipped: %s", exc)

        # ---- Wire BrowserBridge (P2: visual verification in pipeline) ----
        self._browser_bridge = None
        try:
            from backend.core.ouroboros.governance.browser_bridge import get_browser_bridge
            _bridge = get_browser_bridge()
            if _bridge.is_available:
                self._browser_bridge = _bridge
                logger.info(
                    "[GLS] BrowserBridge wired (backend=%s)", _bridge.backend_name
                )
            else:
                logger.debug("[GLS] BrowserBridge: no backend available")
        except Exception as exc:
            logger.debug("[GLS] BrowserBridge skipped: %s", exc)

        # ---- Wire PromptCache (token cost reduction via prefix caching) ----
        self._prompt_cache = None
        try:
            from backend.core.ouroboros.governance.prompt_cache import get_prompt_cache
            self._prompt_cache = get_prompt_cache()
            logger.info("[GLS] PromptCache wired (system prompt prefix caching)")
        except Exception as exc:
            logger.debug("[GLS] PromptCache skipped: %s", exc)

        # ---- Wire SessionManager (multi-turn operation resume) ----
        self._session_mgr = None
        try:
            from backend.core.ouroboros.governance.session_manager import get_session_manager
            self._session_mgr = get_session_manager()
            logger.info("[GLS] SessionManager wired (multi-turn session resume/fork)")
        except Exception as exc:
            logger.debug("[GLS] SessionManager skipped: %s", exc)

        # ---- Wire PermissionClassifier (ML-based auto-approve) ----
        self._permission_clf = None
        try:
            from backend.core.ouroboros.governance.permission_classifier import get_permission_classifier
            self._permission_clf = get_permission_classifier()
            logger.info(
                "[GLS] PermissionClassifier wired (%d rules, weighted voting)",
                len(self._permission_clf._rules),
            )
        except Exception as exc:
            logger.debug("[GLS] PermissionClassifier skipped: %s", exc)

        # ---- JARVIS Tier 3: Predictive Regression Engine (background task) ----
        self._predictive_engine = None
        try:
            from backend.core.ouroboros.governance.predictive_engine import PredictiveRegressionEngine
            self._predictive_engine = PredictiveRegressionEngine(self._config.project_root)
            asyncio.get_event_loop().create_task(self._predictive_engine.start())
            logger.info("[GLS] PredictiveRegressionEngine started (JARVIS Tier 3)")
        except Exception as exc:
            logger.debug("[GLS] PredictiveRegressionEngine skipped: %s", exc)

        # ---- JARVIS Tier 4: Distributed Resilience (heartbeat + sync) ----
        self._resilience_manager = None
        try:
            from backend.core.ouroboros.governance.distributed_resilience import DistributedResilienceManager
            self._resilience_manager = DistributedResilienceManager()
            asyncio.get_event_loop().create_task(self._resilience_manager.start())
            logger.info("[GLS] DistributedResilienceManager started (JARVIS Tier 4)")
        except Exception as exc:
            logger.debug("[GLS] DistributedResilienceManager skipped: %s", exc)

        # ---- JARVIS Tier 2: Emergency Protocol Engine ----
        self._emergency_engine = None
        try:
            from backend.core.ouroboros.governance.emergency_protocols import EmergencyProtocolEngine
            _say = getattr(self, "_say_fn", None)
            self._emergency_engine = EmergencyProtocolEngine(say_fn=_say)
            logger.info("[GLS] EmergencyProtocolEngine wired (JARVIS Tier 2)")
        except Exception as exc:
            logger.debug("[GLS] EmergencyProtocolEngine skipped: %s", exc)

        # ---- JARVIS Tier 6: Personality Engine ----
        self._personality_engine = None
        try:
            from backend.core.ouroboros.governance.jarvis_intelligence import PersonalityEngine
            self._personality_engine = PersonalityEngine()
            logger.info("[GLS] PersonalityEngine wired (JARVIS Tier 6)")
        except Exception as exc:
            logger.debug("[GLS] PersonalityEngine skipped: %s", exc)

        # ---- JARVIS Tier 7: Autonomous Judgment (daily review) ----
        self._judgment_framework = None
        try:
            from backend.core.ouroboros.governance.jarvis_intelligence import AutonomousJudgmentFramework
            self._judgment_framework = AutonomousJudgmentFramework()
            logger.info("[GLS] AutonomousJudgmentFramework wired (JARVIS Tier 7)")
        except Exception as exc:
            logger.debug("[GLS] AutonomousJudgmentFramework skipped: %s", exc)

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
                await self.report_external_outcome(
                    op_id=op_id,
                    terminal_phase=OperationPhase.POSTMORTEM,
                    reason_code="boot_recovery_missing_provenance",
                    affected_files=((target_path_str,) if target_path_str else ()),
                    failure_class="env",
                    outcome_source="boot_recovery",
                    extra_payload={
                        "recovery_attempt_id": recovery_id,
                        "recovery_disposition": "manual_intervention_required",
                    },
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
                await self.report_external_outcome(
                    op_id=op_id,
                    terminal_phase=OperationPhase.POSTMORTEM,
                    reason_code="boot_recovery_file_missing",
                    affected_files=(target_path_str,),
                    failure_class="env",
                    outcome_source="boot_recovery",
                    extra_payload={
                        "recovery_attempt_id": recovery_id,
                        "recovery_disposition": "manual_intervention_required",
                    },
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
                await self.report_external_outcome(
                    op_id=op_id,
                    terminal_phase=OperationPhase.CANCELLED,
                    reason_code="boot_recovery_already_reverted",
                    rollback_occurred=True,
                    affected_files=(target_path_str,),
                    failure_class="rollback",
                    outcome_source="boot_recovery",
                    extra_payload={
                        "recovery_attempt_id": recovery_id,
                        "recovery_disposition": "already_reverted",
                    },
                )
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
            await self.report_external_outcome(
                op_id=op_id,
                terminal_phase=OperationPhase.POSTMORTEM,
                reason_code="boot_recovery_needs_manual_rollback",
                affected_files=(target_path_str,),
                failure_class="env",
                outcome_source="boot_recovery",
                extra_payload={
                    "recovery_attempt_id": recovery_id,
                    "current_hash": current_hash,
                    "rollback_hash": rollback_hash,
                    "recovery_disposition": "manual_intervention_required",
                },
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
        """Periodically probe provider health and update FSM state.

        Probe interval adapts based on recovery ETA: aggressive near
        predicted recovery, relaxed during deep backoff (Manifesto §5).
        """
        while True:
            try:
                # Adaptive interval: use FSM's recommendation, capped at 2x base
                base_interval = self._config.health_probe_interval_s
                if self._generator is not None:
                    adaptive = self._generator.fsm.recommended_probe_interval()
                    interval = max(5.0, min(adaptive, base_interval * 2))
                else:
                    interval = base_interval
                await asyncio.sleep(interval)
                if self._generator is not None:
                    provider = getattr(self._generator, "_primary", None)
                    if provider is not None:
                        ok = False  # default to failure
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
                                    self._generator.fsm.record_probe_failure()
                                except Exception:
                                    pass
                        except Exception:
                            try:
                                self._generator.fsm.record_probe_failure()
                            except Exception:
                                pass
                        # C+ L1: Emit health probe result to SafetyNet (L3)
                        if self._event_emitter is not None:
                            try:
                                _fsm = self._generator.fsm
                                _fm = _fsm._failure_mode
                                _eta = max(0, _fsm.recovery_eta() - time.monotonic()) if not ok else 0
                                await self._event_emitter.emit(AutonomyEventEnvelope(
                                    source_layer="L1",
                                    event_type=AutonomyEventType.HEALTH_PROBE_RESULT,
                                    payload={
                                        "provider": getattr(provider, "provider_name", "unknown"),
                                        "success": ok,
                                        "latency_ms": 0,
                                        "consecutive_failures": _fsm._consecutive_failures,
                                        "failure_mode": _fm.name if _fm else None,
                                        "recovery_eta_s": round(_eta, 1),
                                    },
                                ))
                            except Exception:
                                pass  # fault-isolated
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

        If self._oracle is already set (injected by an external harness or the
        governance stack), skip initialization entirely — reuse the existing instance.
        This prevents double-initialization of ChromaDB's PersistentClient which
        causes a SQLite lock contention segfault (SIGSEGV at 0x0) when two clients
        target the same persistence directory concurrently.
        """
        try:
            # Reuse injected Oracle if already available (e.g. from battle test harness)
            if self._oracle is not None:
                logger.info(
                    "[GovernedLoop] Oracle already injected (%s nodes), skipping re-init",
                    self._oracle.get_metrics().get("total_nodes", "?"),
                )
            elif TheOracle is None:
                raise ImportError("TheOracle not available")
            else:
                oracle = TheOracle()
                await oracle.initialize()
                self._oracle = oracle
            if self._stack is not None:
                self._stack.oracle = self._oracle
            logger.info(
                "[GovernedLoop] Oracle indexed %s nodes across all repos",
                self._oracle.get_metrics().get("total_nodes", "?"),
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

    # ------------------------------------------------------------------
    # C+ Autonomy: background loops
    # ------------------------------------------------------------------

    async def _feedback_loop(self) -> None:
        """Periodically run FeedbackEngine consumption loops."""
        while True:
            try:
                await asyncio.sleep(60.0)
                if self._feedback_engine:
                    await self._feedback_engine.consume_curriculum_once()
                    await self._feedback_engine.consume_reactor_events_once()
                    if self._performance_persistence is None:
                        self._performance_persistence = get_performance_persistence()
                    await self._feedback_engine.score_attribution_once(
                        self._performance_persistence,
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] feedback_loop error: %s", exc)

    async def _command_consumer_loop(self) -> None:
        """Consume commands from advisory layers and route to L1 handlers."""
        while True:
            try:
                if self._command_bus is None:
                    await asyncio.sleep(5.0)
                    continue
                cmd = await asyncio.wait_for(self._command_bus.get(), timeout=5.0)
                await self._handle_advisory_command(cmd)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] command_consumer error: %s", exc)

    async def _handle_advisory_command(self, cmd) -> None:
        """Route a command envelope to the appropriate L1 handler."""
        ct = cmd.command_type
        if ct == AutonomyCommandType.GENERATE_BACKLOG_ENTRY:
            logger.info("[GovernedLoop] L2 backlog: %s", cmd.payload.get("description", "")[:80])
        elif ct == AutonomyCommandType.ADJUST_BRAIN_HINT:
            logger.info("[GovernedLoop] L2 brain hint: brain=%s delta=%s",
                        cmd.payload.get("brain_id"), cmd.payload.get("weight_delta"))
        elif ct == AutonomyCommandType.REQUEST_MODE_SWITCH:
            logger.warning("[GovernedLoop] L3 mode switch: %s (reason: %s)",
                           cmd.payload.get("target_mode"), cmd.payload.get("reason"))
        elif ct == AutonomyCommandType.REPORT_ROLLBACK_CAUSE:
            logger.info("[GovernedLoop] L3 rollback analysis: op=%s cause=%s pattern=%s",
                        cmd.payload.get("op_id"), cmd.payload.get("root_cause_class"),
                        cmd.payload.get("pattern_match"))
        elif ct == AutonomyCommandType.SIGNAL_HUMAN_PRESENCE:
            logger.info("[GovernedLoop] L3 human presence: active=%s type=%s",
                        cmd.payload.get("is_active"), cmd.payload.get("activity_type"))
        elif ct == AutonomyCommandType.SUBMIT_EXECUTION_GRAPH:
            if self._subagent_scheduler is None:
                logger.warning("[GovernedLoop] L3 graph submit ignored: scheduler unavailable")
                return
            graph = cmd.payload.get("execution_graph")
            if graph is None:
                logger.warning("[GovernedLoop] L3 graph submit ignored: missing execution_graph")
                return
            accepted = await self._subagent_scheduler.submit(graph)
            logger.info(
                "[GovernedLoop] L3 graph submit: graph_id=%s accepted=%s",
                getattr(graph, "graph_id", "?"),
                accepted,
            )
        elif ct == AutonomyCommandType.REPORT_WORK_UNIT_RESULT:
            logger.info(
                "[GovernedLoop] L3 work unit result: graph=%s unit=%s repo=%s status=%s",
                cmd.payload.get("graph_id"),
                cmd.payload.get("unit_id"),
                cmd.payload.get("repo"),
                cmd.payload.get("status"),
            )
        elif ct == AutonomyCommandType.ABORT_EXECUTION_GRAPH:
            if self._subagent_scheduler is None:
                logger.warning("[GovernedLoop] L3 graph abort ignored: scheduler unavailable")
                return
            graph_id = str(cmd.payload.get("graph_id", ""))
            if not graph_id:
                logger.warning("[GovernedLoop] L3 graph abort ignored: missing graph_id")
                return
            aborted = await self._subagent_scheduler.abort(graph_id)
            logger.warning("[GovernedLoop] L3 graph abort: graph_id=%s aborted=%s", graph_id, aborted)
        else:
            logger.debug("[GovernedLoop] Unhandled command: %s", ct)

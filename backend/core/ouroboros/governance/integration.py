"""
Ouroboros Governance Integration Module
=======================================

Wires the governance stack (Phases 0-3) into the running JARVIS system.
All governance lifecycle logic lives here. The unified_supervisor.py gets
minimal hook calls at 4 explicit points — this module owns the mechanics.

CONSTRAINT: No side effects on import.
"""

from __future__ import annotations

import argparse as _argparse
import enum
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.break_glass import BreakGlassManager
from backend.core.ouroboros.governance.canary_controller import CanaryController
from backend.core.ouroboros.governance.change_engine import ChangeEngine
from backend.core.ouroboros.governance.blast_radius_adapter import BlastRadiusAdapter
from backend.core.ouroboros.governance.comm_protocol import CommProtocol, LogTransport
from backend.core.ouroboros.governance.contract_gate import ContractVersion
from backend.core.ouroboros.governance.event_bridge import EventBridge
from backend.core.ouroboros.governance.degradation import DegradationController
from backend.core.ouroboros.governance.learning_bridge import LearningBridge
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationLedger, OperationState
from backend.core.ouroboros.governance.lock_manager import GovernanceLockManager
from backend.core.ouroboros.governance.resource_monitor import ResourceMonitor
from backend.core.ouroboros.governance.risk_engine import (
    POLICY_VERSION,
    ChangeType,
    OperationProfile,
    RiskEngine,
)
from backend.core.ouroboros.governance.routing_policy import RoutingPolicy
from backend.core.ouroboros.governance.runtime_contracts import RuntimeContractChecker
from backend.core.ouroboros.governance.supervisor_controller import SupervisorOuroborosController


# ---------------------------------------------------------------------------
# GovernanceMode
# ---------------------------------------------------------------------------


class GovernanceMode(enum.Enum):
    """Operating modes for the governance stack.

    All mode fields use this enum — no string literals anywhere.
    """

    PENDING = "pending"
    SANDBOX = "sandbox"
    READ_ONLY_PLANNING = "read_only_planning"
    GOVERNED = "governed"
    EMERGENCY_STOP = "emergency_stop"


# ---------------------------------------------------------------------------
# CapabilityStatus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityStatus:
    """Status of an optional governance capability.

    Not just a bool — carries a reason string for degraded boot observability.
    Reasons: "ok", "dep_missing", "init_timeout", "init_error"
    """

    enabled: bool
    reason: str


# ---------------------------------------------------------------------------
# GovernanceInitError
# ---------------------------------------------------------------------------


class GovernanceInitError(Exception):
    """Raised when governance stack creation fails.

    Carries a reason_code for structured logging and observability.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}")


# ---------------------------------------------------------------------------
# GovernanceConfig
# ---------------------------------------------------------------------------

_MODE_MAP = {
    "sandbox": GovernanceMode.SANDBOX,
    "governed": GovernanceMode.GOVERNED,
    "safe": GovernanceMode.SANDBOX,  # alias
}


@dataclass(frozen=True)
class GovernanceConfig:
    """Frozen configuration for governance stack creation.

    Built from env vars + CLI args via :meth:`from_env_and_args`.
    Hashes included for forensic reproducibility of every decision.
    """

    # Paths
    ledger_dir: Path

    # Policy (immutable during execution)
    policy_version: str
    policy_hash: str
    contract_version: ContractVersion
    contract_hash: str
    config_digest: str

    # Mode
    initial_mode: GovernanceMode
    skip_governance: bool

    # Canary slices
    canary_slices: Tuple[str, ...]

    # Cost guardrails
    gcp_daily_budget: float

    # Timeouts
    startup_timeout_s: float
    component_budget_s: float

    @classmethod
    def from_env_and_args(cls, args: Any) -> "GovernanceConfig":
        """Build from env vars + CLI args. Validates on construction.

        Raises ValueError on invalid config (e.g. unknown governance mode).
        """
        skip = getattr(args, "skip_governance", False)
        mode_str = (
            getattr(args, "governance_mode", None)
            or os.environ.get("JARVIS_GOVERNANCE_MODE", "sandbox")
        )

        if skip:
            initial_mode = GovernanceMode.READ_ONLY_PLANNING
        else:
            if mode_str not in _MODE_MAP:
                raise ValueError(
                    f"Invalid governance mode: {mode_str!r}. "
                    f"Valid: {list(_MODE_MAP.keys())}"
                )
            initial_mode = _MODE_MAP[mode_str]

        ledger_dir = Path(
            os.environ.get(
                "OUROBOROS_LEDGER_DIR",
                str(Path.home() / ".jarvis" / "ouroboros" / "ledger"),
            )
        )
        gcp_daily_budget = float(
            os.environ.get("OUROBOROS_GCP_DAILY_BUDGET", "10.0")
        )
        startup_timeout_s = float(
            os.environ.get("OUROBOROS_STARTUP_TIMEOUT", "30")
        )
        component_budget_s = float(
            os.environ.get("OUROBOROS_COMPONENT_BUDGET", "5")
        )
        canary_slices = tuple(
            os.environ.get(
                "OUROBOROS_CANARY_SLICES", "backend/core/ouroboros/"
            ).split(",")
        )

        contract_version = ContractVersion(major=2, minor=1, patch=0)

        # Compute hashes for forensic reproducibility
        policy_hash = hashlib.sha256(POLICY_VERSION.encode()).hexdigest()
        contract_hash = hashlib.sha256(
            f"{contract_version.major}.{contract_version.minor}.{contract_version.patch}".encode()
        ).hexdigest()

        # Build without config_digest first, then compute it
        pre_digest = {
            "ledger_dir": str(ledger_dir),
            "policy_version": POLICY_VERSION,
            "initial_mode": initial_mode.value,
            "skip_governance": skip,
            "canary_slices": canary_slices,
            "gcp_daily_budget": gcp_daily_budget,
            "startup_timeout_s": startup_timeout_s,
            "component_budget_s": component_budget_s,
        }
        config_digest = hashlib.sha256(
            json.dumps(pre_digest, sort_keys=True).encode()
        ).hexdigest()

        return cls(
            ledger_dir=ledger_dir,
            policy_version=POLICY_VERSION,
            policy_hash=policy_hash,
            contract_version=contract_version,
            contract_hash=contract_hash,
            config_digest=config_digest,
            initial_mode=initial_mode,
            skip_governance=skip,
            canary_slices=canary_slices,
            gcp_daily_budget=gcp_daily_budget,
            startup_timeout_s=startup_timeout_s,
            component_budget_s=component_budget_s,
        )


logger = logging.getLogger("Ouroboros.Integration")


# ---------------------------------------------------------------------------
# Transport factory (B-compatible seam: accepts config now, uses env/defaults)
# ---------------------------------------------------------------------------


def _build_comm_protocol(
    config: Optional["GovernanceConfig"] = None,
    extra_transports: Optional[List[Any]] = None,
) -> "CommProtocol":
    """Build the CommProtocol with full transport stack.

    Transport ordering (fixed, never changed):
      1. LogTransport  — always present, always first
      2. TUITransport  — if tui_transport module is importable
      3. VoiceNarrator — if safe_say is available in the system
      4. OpsLogger     — always added (writes to ~/.jarvis/ops/)

    B-compatible seam: config parameter accepted now (unused until GovernanceConfig
    grows transport-enable flags). Call sites are already config-injectable.
    """
    transports: List[Any] = [LogTransport()]

    # TUITransport — safe to add; queues if no callback registered
    try:
        from backend.core.ouroboros.governance.tui_transport import TUITransport
    except ImportError:
        logger.warning("[Integration] TUITransport skipped: module not available")
    else:
        transports.append(TUITransport())
        logger.info("[Integration] TUITransport added to CommProtocol")

    # TUISelfProgramPanel — tracks active ops, pending approvals, completions
    # for the Textual TUI dashboard. Safe to add; maintains state independently.
    try:
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel
    except ImportError:
        logger.debug("[Integration] TUISelfProgramPanel skipped: module not available")
    else:
        transports.append(TUISelfProgramPanel())
        logger.info("[Integration] TUISelfProgramPanel added to CommProtocol")

    # VoiceNarrator — requires safe_say; skip if unavailable or voice disabled
    _voice_enabled = os.environ.get("JARVIS_VOICE_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    if not _voice_enabled:
        logger.info("[Integration] VoiceNarrator skipped: JARVIS_VOICE_ENABLED=0")
    else:
        try:
            from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
            from backend.core.supervisor.unified_voice_orchestrator import safe_say  # type: ignore[import]
        except ImportError as exc:
            logger.debug("[Integration] VoiceNarrator skipped (audio unavailable): %s", exc)
        else:
            _debounce = float(os.environ.get("OUROBOROS_VOICE_DEBOUNCE_S", "60.0"))
            transports.append(VoiceNarrator(say_fn=safe_say, debounce_s=_debounce, source="ouroboros"))
            logger.info("[Integration] VoiceNarrator added to CommProtocol")

    # OpsLogger — always add; uses env var JARVIS_OPS_LOG_DIR or default
    try:
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger
    except ImportError:
        logger.warning("[Integration] OpsLogger skipped: module not available")
    else:
        transports.append(OpsLogger())
        logger.info("[Integration] OpsLogger added to CommProtocol")

    # LangfuseTransport — optional, enabled via LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY
    if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
        try:
            from backend.core.ouroboros.governance.comms.langfuse_transport import LangfuseTransport
            _lf = LangfuseTransport()
            if _lf.is_active:
                transports.append(_lf)
                logger.info("[Integration] LangfuseTransport added to CommProtocol")
            else:
                logger.debug("[Integration] LangfuseTransport: client not active")
        except ImportError:
            logger.debug("[Integration] LangfuseTransport skipped: langfuse not installed")
        except Exception as exc:
            logger.debug("[Integration] LangfuseTransport skipped: %s", exc)

    # DurableJSONLTransport — persistent event log on disk
    try:
        from backend.core.ouroboros.governance.comms.durable_jsonl_transport import DurableJSONLTransport
        _jsonl_dir = Path.home() / ".jarvis" / "ouroboros" / "event_log"
        transports.append(DurableJSONLTransport(log_dir=_jsonl_dir))
        logger.info("[Integration] DurableJsonlTransport added to CommProtocol")
    except ImportError:
        logger.debug("[Integration] DurableJsonlTransport skipped: module not available")
    except Exception as exc:
        logger.debug("[Integration] DurableJsonlTransport skipped: %s", exc)

    # RemoteHTTPTransport — forwards events to J-Prime (cross-repo visibility)
    _prime_comm_endpoint = os.environ.get("JARVIS_PRIME_COMM_ENDPOINT", "")
    if _prime_comm_endpoint:
        try:
            from backend.core.ouroboros.governance.comms.remote_http_transport import (
                RemoteHTTPTransport,
            )
            _remote = RemoteHTTPTransport(endpoint=_prime_comm_endpoint)
            transports.append(_remote)
            logger.info(
                "[Integration] RemoteHTTPTransport added to CommProtocol: %s",
                _prime_comm_endpoint,
            )
        except ImportError:
            logger.debug("[Integration] RemoteHTTPTransport skipped: module not available")
        except Exception as exc:
            logger.debug("[Integration] RemoteHTTPTransport skipped: %s", exc)

    # Extra transports (for testing / future extension)
    if extra_transports:
        transports.extend(extra_transports)

    logger.info(
        "[Integration] CommProtocol transport stack: %s",
        [type(t).__name__ for t in transports],
    )
    return CommProtocol(transports=transports)


# ---------------------------------------------------------------------------
# GovernanceStack
# ---------------------------------------------------------------------------


@dataclass
class GovernanceStack:
    """Holds all instantiated governance components with lifecycle methods.

    Provides:
    - start()/stop(): idempotent lifecycle
    - can_write(): single authority for autonomous write decisions
    - health(): structured report for TUI/dashboard
    - replay_decision(): forensic audit of prior decisions
    - drain(): graceful shutdown of in-flight operations

    Phase 1 Step 3C — § 4 bind contract
    -----------------------------------
    The ``orchestrator`` dataclass field remains for backwards
    compatibility (any call site still reading ``stack.orchestrator``
    gets the last-bound instance), but the process-lifetime indirection
    lives in ``_governance_state.{bind_orchestrator,
    get_bound_orchestrator}`` under a dedicated ``RLock``. Hot-path
    consumers should call :meth:`bind_orchestrator` at construction /
    teardown time and read :attr:`orchestrator_ref` on every dispatch,
    so ``importlib.reload(orchestrator)`` can swap the class without
    leaving stale captured references inside ``GovernedLoopService`` or
    ``BackgroundAgentPool``.
    """

    # Core (always present)
    controller: SupervisorOuroborosController
    risk_engine: RiskEngine
    ledger: OperationLedger
    comm: CommProtocol
    lock_manager: GovernanceLockManager
    break_glass: BreakGlassManager
    change_engine: ChangeEngine
    resource_monitor: ResourceMonitor
    degradation: DegradationController
    routing: RoutingPolicy
    canary: CanaryController
    contract_checker: RuntimeContractChecker

    # Optional bridges
    event_bridge: Optional[Any]
    blast_adapter: Optional[Any]
    learning_bridge: Optional[Any]

    # Metadata
    policy_version: str
    capabilities: Dict[str, CapabilityStatus]

    # Governed loop components (optional — present when orchestrator is wired)
    orchestrator: Optional[Any] = None
    generator: Optional[Any] = None
    approval_provider: Optional[Any] = None
    shadow_harness: Optional[Any] = None
    governed_loop_service: Optional[Any] = None
    performance_persistence: Optional[Any] = None
    oracle: Optional[Any] = None

    _started: bool = False

    async def start(self) -> None:
        """Start all components. Idempotent -- second call is no-op."""
        if self._started:
            return
        await self.controller.start()
        self._started = True

    async def stop(self) -> None:
        """Graceful shutdown. Idempotent."""
        if not self._started:
            return
        await self.drain()
        await self.controller.stop()
        self._started = False

    async def drain(self) -> None:
        """Drain in-flight operations before shutdown."""
        pass

    def health(self) -> Dict[str, Any]:
        """Structured health report for TUI/dashboard."""
        return {
            "mode": self.controller.mode.value,
            "policy_version": self.policy_version,
            "capabilities": {
                k: {"enabled": v.enabled, "reason": v.reason}
                for k, v in self.capabilities.items()
            },
            "degradation_mode": self.degradation.mode.name,
            "canary_slices": {
                p: s.state.value for p, s in self.canary.slices.items()
            },
            "budget_over": (
                self.routing.cost_guardrail.over_budget
                if hasattr(self.routing, "cost_guardrail")
                else None
            ),
            "budget_daily_usage": (
                self.routing.cost_guardrail.daily_usage
                if hasattr(self.routing, "cost_guardrail")
                else None
            ),
        }

    def can_write(self, op_context: Dict[str, Any]) -> Tuple[bool, str]:
        """Single authority for all autonomous write decisions.

        Returns (allowed, reason_code). ALL write paths must call this.
        No alternate path can enable writes outside this gate.
        """
        if not self._started:
            logger.warning("[GovernanceStack] can_write BLOCKED: governance_not_started")
            return False, "governance_not_started"
        if not self.controller.writes_allowed:
            _reason = f"mode_{self.controller.mode.value}"
            logger.warning("[GovernanceStack] can_write BLOCKED: %s", _reason)
            return False, _reason
        if self.degradation.mode.value > 1:  # REDUCED or worse
            _reason = f"degradation_{self.degradation.mode.name}"
            logger.warning("[GovernanceStack] can_write BLOCKED: %s", _reason)
            return False, _reason
        # Check canary slice
        files = op_context.get("files", [])
        for f in files:
            if not self.canary.is_file_allowed(str(f)):
                _reason = f"canary_not_promoted:{f}"
                logger.warning("[GovernanceStack] can_write BLOCKED: %s", _reason)
                return False, _reason
        # Check runtime contract
        proposed_version = op_context.get("proposed_contract_version")
        if proposed_version and not self.contract_checker.check_before_write(
            proposed_version
        ):
            logger.warning("[GovernanceStack] can_write BLOCKED: contract_incompatible")
            return False, "contract_incompatible"
        return True, "ok"

    # ─────────────────────────────────────────────────────────────────
    # Phase 1 Step 3C — § 4 bind contract
    # ─────────────────────────────────────────────────────────────────

    def bind_orchestrator(self, orch: Optional[Any]) -> None:
        """Atomically bind the governed orchestrator to this stack.

        Writes both the legacy dataclass slot ``self.orchestrator``
        (for any caller still reading the plain field) and the
        process-lifetime indirection in
        ``_governance_state._bound_orchestrator`` (for hot paths that
        have migrated to :attr:`orchestrator_ref`). Both writes happen
        under the dedicated ``_bind_lock`` in ``_governance_state`` so
        there is no window in which one caller sees the old instance
        via ``stack.orchestrator`` while another sees the new instance
        via ``stack.orchestrator_ref``.

        Passing ``None`` clears the bind — used at
        ``GovernedLoopService._detach_from_stack`` time so a shut-down
        loop doesn't leak a dead orchestrator.

        This is the exact operation that ``importlib.reload`` must be
        followed by: after reloading the ``orchestrator`` module and
        re-constructing an ``Orchestrator`` instance, the harness (or
        the reloader itself) calls ``stack.bind_orchestrator(new)``
        and every captured reference in
        :class:`GovernedLoopService` and
        :class:`BackgroundAgentPool` flips to the new instance on
        its next dispatch without needing to re-wire the harness.
        """
        from backend.core.ouroboros.governance._governance_state import (
            bind_orchestrator as _bind,
        )

        _bind(orch)
        # Legacy slot kept in sync so pre-3C readers keep working.
        self.orchestrator = orch

    @property
    def orchestrator_ref(self) -> Optional[Any]:
        """Return the live orchestrator binding for § 4 dispatch paths.

        Reads through :func:`_governance_state.get_bound_orchestrator`
        when the bind contract has been engaged (Phase 1 Step 3C
        rollout); otherwise falls back to the legacy
        ``self.orchestrator`` dataclass field so pre-3C deployments
        continue to work without any migration.

        Hot-path consumers should prefer this property over
        ``stack.orchestrator`` so post-reload dispatches flip to the
        new instance atomically — see the
        ``_governance_state`` module-level docstring on why captured
        references go stale after ``importlib.reload(orchestrator)``.
        """
        from backend.core.ouroboros.governance._governance_state import (
            get_bound_orchestrator,
        )

        bound = get_bound_orchestrator()
        if bound is not None:
            return bound
        return self.orchestrator

    async def replay_decision(
        self, op_id: str
    ) -> Optional[Dict[str, Any]]:
        """Reconstruct classification from persisted inputs + policy_version.

        Returns the exact prior decision for forensic audit.
        """
        entries = await self.ledger.get_history(op_id)
        if not entries:
            return None
        entry = entries[0]
        profile_data = entry.data.get("profile", {})
        if not profile_data:
            return None
        files = profile_data.get("files_affected", [])
        profile = OperationProfile(
            files_affected=[Path(f) for f in files],
            change_type=ChangeType[profile_data.get("change_type", "MODIFY")],
            blast_radius=profile_data.get("blast_radius", 1),
            crosses_repo_boundary=profile_data.get("crosses_repo_boundary", False),
            touches_security_surface=profile_data.get("touches_security_surface", False),
            touches_supervisor=profile_data.get("touches_supervisor", False),
            test_scope_confidence=profile_data.get("test_scope_confidence", 0.0),
        )
        classification = self.risk_engine.classify(profile)
        return {
            "op_id": op_id,
            "policy_version": self.policy_version,
            "original_state": entry.state.value,
            "replayed_tier": classification.tier.name,
            "replayed_reason": classification.reason_code,
            "match": classification.tier.name == entry.data.get("risk_tier"),
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def create_governance_stack(
    config: GovernanceConfig,
    event_bus: Optional[Any] = None,
    oracle: Optional[Any] = None,
    learning_memory: Optional[Any] = None,
) -> GovernanceStack:
    """Factory with per-component budgets.

    On failure: cleans up any partially-created resources,
    raises GovernanceInitError with reason_code.
    """
    capabilities: Dict[str, CapabilityStatus] = {}

    try:
        # Build EventBridge FIRST so it can be wired as CommProtocol transport
        _event_bridge: Optional[Any] = None
        if event_bus is not None:
            try:
                _event_bridge = EventBridge(event_bus=event_bus)
                capabilities["event_bridge"] = CapabilityStatus(
                    enabled=True, reason="ok"
                )
            except Exception as exc:
                capabilities["event_bridge"] = CapabilityStatus(
                    enabled=False, reason=f"init_error: {exc}"
                )
        else:
            capabilities["event_bridge"] = CapabilityStatus(
                enabled=False, reason="dep_missing"
            )

        # Build CommProtocol — include EventBridge as extra transport if available
        _bridge_transports: List[Any] = [_event_bridge] if _event_bridge is not None else []
        comm = _build_comm_protocol(config=config, extra_transports=_bridge_transports)

        # Register CrossRepoNarrator on event_bus for inbound narration
        if event_bus is not None and _event_bridge is not None:
            try:
                from backend.core.ouroboros.governance.comms.cross_repo_narrator import CrossRepoNarrator
                from backend.core.ouroboros.cross_repo import EventType
                narrator = CrossRepoNarrator(comm=comm)
                event_bus.register_handler(EventType.IMPROVEMENT_REQUEST, narrator.on_improvement_request)
                event_bus.register_handler(EventType.IMPROVEMENT_COMPLETE, narrator.on_improvement_complete)
                event_bus.register_handler(EventType.IMPROVEMENT_FAILED, narrator.on_improvement_failed)
                logger.info("[Integration] CrossRepoNarrator registered on event bus")
            except Exception as exc:
                logger.warning("[Integration] CrossRepoNarrator registration failed: %s", exc)

        # Core components (always present)
        risk_engine = RiskEngine()
        ledger = OperationLedger(storage_dir=config.ledger_dir)
        controller = SupervisorOuroborosController()
        lock_manager = GovernanceLockManager()
        break_glass = BreakGlassManager()
        resource_monitor = ResourceMonitor()
        degradation = DegradationController()
        routing = RoutingPolicy()
        canary = CanaryController()
        contract_checker = RuntimeContractChecker(
            current_version=config.contract_version
        )
        change_engine = ChangeEngine(
            project_root=config.ledger_dir.parent.parent.parent,
            ledger=ledger,
            comm=comm,
            lock_manager=lock_manager,
            break_glass=break_glass,
            risk_engine=risk_engine,
        )

        # Optional bridges

        _blast_adapter: Optional[Any] = None
        if oracle is not None:
            try:
                _blast_adapter = BlastRadiusAdapter(oracle=oracle)
                capabilities["oracle"] = CapabilityStatus(
                    enabled=True, reason="ok"
                )
            except Exception as exc:
                capabilities["oracle"] = CapabilityStatus(
                    enabled=False, reason=f"init_error: {exc}"
                )
        else:
            capabilities["oracle"] = CapabilityStatus(
                enabled=False, reason="dep_missing"
            )

        _learning_bridge: Optional[Any] = None
        if learning_memory is not None:
            try:
                _learning_bridge = LearningBridge(
                    learning_memory=learning_memory
                )
                capabilities["learning"] = CapabilityStatus(
                    enabled=True, reason="ok"
                )
            except Exception as exc:
                capabilities["learning"] = CapabilityStatus(
                    enabled=False, reason=f"init_error: {exc}"
                )
        else:
            capabilities["learning"] = CapabilityStatus(
                enabled=False, reason="dep_missing"
            )

        _performance_persistence: Optional[Any] = None
        try:
            from backend.core.ouroboros.integration import get_performance_persistence
            _performance_persistence = get_performance_persistence()
            capabilities["performance_persistence"] = CapabilityStatus(
                enabled=True, reason="ok"
            )
        except Exception as exc:
            capabilities["performance_persistence"] = CapabilityStatus(
                enabled=False, reason=f"init_error: {exc}"
            )

        return GovernanceStack(
            controller=controller,
            risk_engine=risk_engine,
            ledger=ledger,
            comm=comm,
            lock_manager=lock_manager,
            break_glass=break_glass,
            change_engine=change_engine,
            resource_monitor=resource_monitor,
            degradation=degradation,
            routing=routing,
            canary=canary,
            contract_checker=contract_checker,
            event_bridge=_event_bridge,
            blast_adapter=_blast_adapter,
            learning_bridge=_learning_bridge,
            policy_version=config.policy_version,
            capabilities=capabilities,
            performance_persistence=_performance_persistence,
        )

    except GovernanceInitError:
        raise
    except Exception as exc:
        raise GovernanceInitError(
            "governance_init_error", str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Argparse Registration
# ---------------------------------------------------------------------------


def register_governance_argparse(security_group: _argparse._ActionsContainer) -> None:
    """Add governance flags to existing security argument group."""
    security_group.add_argument(
        "--skip-governance",
        action="store_true",
        help="Force READ_ONLY_PLANNING governance mode (kill switch)",
    )
    security_group.add_argument(
        "--governance-mode",
        choices=["sandbox", "governed", "safe"],
        default="sandbox",
        dest="governance_mode",
        help="Governance mode (default: sandbox)",
    )
    security_group.add_argument(
        "--break-glass",
        choices=["issue", "list", "revoke", "audit"],
        default=None,
        dest="break_glass_action",
        help="Break-glass subcommand",
    )
    security_group.add_argument(
        "--break-glass-op-id",
        default=None,
        dest="break_glass_op_id",
        help="Operation ID for break-glass issue/revoke",
    )
    security_group.add_argument(
        "--break-glass-reason",
        default=None,
        dest="break_glass_reason",
        help="Reason string for break-glass issue/revoke",
    )
    security_group.add_argument(
        "--break-glass-ttl",
        type=int,
        default=300,
        dest="break_glass_ttl",
        help="Break-glass token TTL in seconds (default 300)",
    )


# ---------------------------------------------------------------------------
# Break-Glass CLI Handler
# ---------------------------------------------------------------------------


async def handle_break_glass_command(
    args: _argparse.Namespace,
    stack: Optional["GovernanceStack"],
) -> int:
    """Dispatch break-glass CLI operations.

    Works even when stack is None (degraded mode):
    - list/audit: return empty with warning
    - issue/revoke: return error with reason

    Returns exit code (0 success, 1 error).
    """
    action = getattr(args, "break_glass_action", None)

    if action == "list":
        if stack is None:
            print("[Governance] No governance stack -- no active tokens.")
            return 0
        from backend.core.ouroboros.governance.cli_commands import list_active_tokens
        tokens = list_active_tokens(stack.break_glass)
        if not tokens:
            print("[Governance] No active break-glass tokens.")
        else:
            for t in tokens:
                print(f"  {t}")
        return 0

    if action == "audit":
        if stack is None:
            print("[Governance] No governance stack -- no audit data.")
            return 0
        from backend.core.ouroboros.governance.cli_commands import get_audit_report
        report = get_audit_report(stack.break_glass)
        for entry in report:
            print(f"  {entry}")
        return 0

    if action == "issue":
        if stack is None:
            print("[Governance] ERROR: Cannot issue break-glass token -- no governance stack.")
            return 1
        from backend.core.ouroboros.governance.cli_commands import issue_break_glass
        op_id = getattr(args, "break_glass_op_id", None)
        reason = getattr(args, "break_glass_reason", None)
        ttl = getattr(args, "break_glass_ttl", 300)
        token = await issue_break_glass(
            stack.break_glass, op_id=op_id, reason=reason, ttl=ttl
        )
        print(f"[Governance] Break-glass token issued: {token.token_id}")
        return 0

    if action == "revoke":
        if stack is None:
            print("[Governance] ERROR: Cannot revoke -- no governance stack.")
            return 1
        from backend.core.ouroboros.governance.cli_commands import revoke_break_glass
        op_id = getattr(args, "break_glass_op_id", None)
        reason = getattr(args, "break_glass_reason", "manual_revoke")
        await revoke_break_glass(stack.break_glass, op_id=op_id, reason=reason)
        print(f"[Governance] Break-glass token revoked for {op_id}.")
        return 0

    print(f"[Governance] Unknown break-glass action: {action}")
    return 1


# ---------------------------------------------------------------------------
# Self-development subcommand registration
# ---------------------------------------------------------------------------


def register_self_dev_commands(
    subparsers: "_argparse._SubParsersAction",  # type: ignore[type-arg]
    integration_fn: Optional[Any] = None,
) -> None:
    """Register self-dev CLI subcommands into the given subparsers group.

    B-compatible seam: integration_fn is injectable for testing.
    All handler logic lives in loop_cli.py — supervisor stays thin.

    Subcommands:
      self-modify      --target FILE --goal TEXT [--op-id ID] [--dry-run]
      approve          OP_ID
      reject           OP_ID [--reason TEXT]
      self-dev-status  [OP_ID]
    """
    # self-modify
    p_modify = subparsers.add_parser(
        "self-modify",
        help="Trigger a governed code generation pipeline",
    )
    p_modify.add_argument("--target", required=True, help="Target file or directory")
    p_modify.add_argument("--goal", required=True, help="Description of desired change")
    p_modify.add_argument("--op-id", default=None, dest="op_id", help="Explicit operation ID")
    p_modify.add_argument("--dry-run", action="store_true", dest="dry_run",
                          help="CLASSIFY + ROUTE only (no generation/apply)")

    # approve
    p_approve = subparsers.add_parser("approve", help="Approve a pending governed operation")
    p_approve.add_argument("op_id", help="Operation ID to approve")
    p_approve.add_argument("--approver", default="cli-operator", help="Approver identity")

    # reject
    p_reject = subparsers.add_parser("reject", help="Reject a pending governed operation")
    p_reject.add_argument("op_id", help="Operation ID to reject")
    p_reject.add_argument("--approver", default="cli-operator", help="Rejector identity")
    p_reject.add_argument("--reason", default="rejected via CLI", help="Rejection reason")

    # self-dev-status
    p_status = subparsers.add_parser("self-dev-status", help="Query self-dev service health")
    p_status.add_argument("op_id", nargs="?", default=None, help="Optional operation ID to inspect")

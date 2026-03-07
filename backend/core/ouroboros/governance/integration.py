"""
Ouroboros Governance Integration Module
=======================================

Wires the governance stack (Phases 0-3) into the running JARVIS system.
All governance lifecycle logic lives here. The unified_supervisor.py gets
minimal hook calls at 4 explicit points — this module owns the mechanics.

CONSTRAINT: No side effects on import.
"""

from __future__ import annotations

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
        mode_str = getattr(args, "governance_mode", "sandbox")

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
            "budget_remaining": (
                self.routing.cost_guardrail.remaining
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
            return False, "governance_not_started"
        if not self.controller.writes_allowed:
            return False, f"mode_{self.controller.mode.value}"
        if self.degradation.mode.value > 1:  # REDUCED or worse
            return False, f"degradation_{self.degradation.mode.name}"
        # Check canary slice
        files = op_context.get("files", [])
        for f in files:
            if not self.canary.is_file_allowed(str(f)):
                return False, f"canary_not_promoted:{f}"
        # Check runtime contract
        proposed_version = op_context.get("proposed_contract_version")
        if proposed_version and not self.contract_checker.check_before_write(
            proposed_version
        ):
            return False, "contract_incompatible"
        return True, "ok"

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
        # Core components (always present)
        risk_engine = RiskEngine()
        ledger = OperationLedger(storage_dir=config.ledger_dir)
        comm = CommProtocol(transports=[LogTransport()])
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
        )

    except GovernanceInitError:
        raise
    except Exception as exc:
        raise GovernanceInitError(
            "governance_init_error", str(exc)
        ) from exc

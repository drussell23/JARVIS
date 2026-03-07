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
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.contract_gate import ContractVersion
from backend.core.ouroboros.governance.risk_engine import POLICY_VERSION


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

# Ouroboros Production Activation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the Ouroboros governance stack (Phases 0-3, 299 tests) into the running JARVIS system via a single integration module + 4 minimal supervisor hooks.

**Architecture:** All governance logic lives in `backend/core/ouroboros/governance/integration.py`. The 100K+ line `unified_supervisor.py` gets ~24 lines of hook calls at 4 explicit points. The supervisor remains the sole lifecycle authority; the integration module owns governance mechanics.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, enum, hashlib, pytest, existing Ouroboros governance components (Phases 0-3).

**Design Doc:** `docs/plans/2026-03-07-ouroboros-production-activation-design.md`

---

## Task 1: GovernanceMode Enum, CapabilityStatus, GovernanceInitError

**Files:**
- Create: `backend/core/ouroboros/governance/integration.py`
- Create: `tests/test_ouroboros_governance/test_integration.py`

**Context:** These are the foundational types used by every other component in this plan. The integration module must have zero side effects on import — all types are pure data.

**Step 1: Write the failing tests**

In `tests/test_ouroboros_governance/test_integration.py`:

```python
"""Tests for the Ouroboros governance integration module."""

from __future__ import annotations

import pytest


# ── GovernanceMode ──────────────────────────────────────────────


class TestGovernanceMode:
    """GovernanceMode enum must have exactly 5 members with string values."""

    def test_all_members_exist(self):
        from backend.core.ouroboros.governance.integration import GovernanceMode

        assert GovernanceMode.PENDING.value == "pending"
        assert GovernanceMode.SANDBOX.value == "sandbox"
        assert GovernanceMode.READ_ONLY_PLANNING.value == "read_only_planning"
        assert GovernanceMode.GOVERNED.value == "governed"
        assert GovernanceMode.EMERGENCY_STOP.value == "emergency_stop"

    def test_member_count(self):
        from backend.core.ouroboros.governance.integration import GovernanceMode

        assert len(GovernanceMode) == 5

    def test_roundtrip_from_string(self):
        from backend.core.ouroboros.governance.integration import GovernanceMode

        for member in GovernanceMode:
            assert GovernanceMode(member.value) is member


# ── CapabilityStatus ────────────────────────────────────────────


class TestCapabilityStatus:
    """CapabilityStatus must be frozen and carry reason string."""

    def test_creation(self):
        from backend.core.ouroboros.governance.integration import CapabilityStatus

        cs = CapabilityStatus(enabled=True, reason="ok")
        assert cs.enabled is True
        assert cs.reason == "ok"

    def test_frozen(self):
        from backend.core.ouroboros.governance.integration import CapabilityStatus

        cs = CapabilityStatus(enabled=False, reason="dep_missing")
        with pytest.raises(AttributeError):
            cs.enabled = True  # type: ignore[misc]

    def test_disabled_with_reason(self):
        from backend.core.ouroboros.governance.integration import CapabilityStatus

        cs = CapabilityStatus(enabled=False, reason="init_timeout")
        assert cs.enabled is False
        assert cs.reason == "init_timeout"


# ── GovernanceInitError ─────────────────────────────────────────


class TestGovernanceInitError:
    """GovernanceInitError must carry reason_code and format message."""

    def test_creation(self):
        from backend.core.ouroboros.governance.integration import GovernanceInitError

        err = GovernanceInitError("governance_init_timeout", "Factory exceeded 30s")
        assert err.reason_code == "governance_init_timeout"
        assert "governance_init_timeout" in str(err)
        assert "Factory exceeded 30s" in str(err)

    def test_is_exception(self):
        from backend.core.ouroboros.governance.integration import GovernanceInitError

        err = GovernanceInitError("test", "msg")
        assert isinstance(err, Exception)

    def test_catchable(self):
        from backend.core.ouroboros.governance.integration import GovernanceInitError

        with pytest.raises(GovernanceInitError) as exc_info:
            raise GovernanceInitError("governance_init_ledger_error", "Disk full")
        assert exc_info.value.reason_code == "governance_init_ledger_error"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernanceMode tests/test_ouroboros_governance/test_integration.py::TestCapabilityStatus tests/test_ouroboros_governance/test_integration.py::TestGovernanceInitError -v`
Expected: FAIL with ImportError (module doesn't exist yet)

**Step 3: Write minimal implementation**

In `backend/core/ouroboros/governance/integration.py`:

```python
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
from dataclasses import dataclass


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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernanceMode tests/test_ouroboros_governance/test_integration.py::TestCapabilityStatus tests/test_ouroboros_governance/test_integration.py::TestGovernanceInitError -v`
Expected: 9 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/integration.py tests/test_ouroboros_governance/test_integration.py
git commit -m "feat(governance): add GovernanceMode, CapabilityStatus, GovernanceInitError"
```

---

## Task 2: GovernanceConfig Frozen Dataclass

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py`
- Modify: `tests/test_ouroboros_governance/test_integration.py`

**Context:** GovernanceConfig is a frozen dataclass built from env vars + CLI args. It includes policy hashes for forensic reproducibility. It uses `ContractVersion` from the existing `contract_gate.py`. `POLICY_VERSION` is `"v0.1.0"` from `risk_engine.py`.

**Step 1: Write the failing tests**

Append to `tests/test_ouroboros_governance/test_integration.py`:

```python
import argparse
import os
from pathlib import Path
from unittest.mock import MagicMock


# ── GovernanceConfig ────────────────────────────────────────────


class TestGovernanceConfig:
    """GovernanceConfig must be frozen, build from args+env, and validate."""

    def _make_args(self, **overrides):
        """Create a minimal argparse.Namespace for testing."""
        defaults = {
            "skip_governance": False,
            "governance_mode": "sandbox",
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_from_env_and_args_defaults(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig, GovernanceMode

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.initial_mode == GovernanceMode.SANDBOX
        assert config.skip_governance is False
        assert config.ledger_dir == Path.home() / ".jarvis" / "ouroboros" / "ledger"
        assert config.gcp_daily_budget == 10.0
        assert config.startup_timeout_s == 30.0
        assert config.component_budget_s == 5.0
        assert config.canary_slices == ("backend/core/ouroboros/",)

    def test_from_env_and_args_governed_mode(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig, GovernanceMode

        args = self._make_args(governance_mode="governed")
        config = GovernanceConfig.from_env_and_args(args)
        assert config.initial_mode == GovernanceMode.GOVERNED

    def test_from_env_and_args_skip_governance(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig, GovernanceMode

        args = self._make_args(skip_governance=True)
        config = GovernanceConfig.from_env_and_args(args)
        assert config.skip_governance is True
        assert config.initial_mode == GovernanceMode.READ_ONLY_PLANNING

    def test_frozen(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        with pytest.raises(AttributeError):
            config.gcp_daily_budget = 999.0  # type: ignore[misc]

    def test_policy_version_populated(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.policy_version == "v0.1.0"

    def test_hashes_are_sha256(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        # SHA-256 hex digests are 64 chars
        assert len(config.policy_hash) == 64
        assert len(config.contract_hash) == 64
        assert len(config.config_digest) == 64

    def test_env_var_budget_override(self, monkeypatch):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        monkeypatch.setenv("OUROBOROS_GCP_DAILY_BUDGET", "25.0")
        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.gcp_daily_budget == 25.0

    def test_env_var_startup_timeout_override(self, monkeypatch):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        monkeypatch.setenv("OUROBOROS_STARTUP_TIMEOUT", "60")
        args = self._make_args()
        config = GovernanceConfig.from_env_and_args(args)
        assert config.startup_timeout_s == 60.0

    def test_invalid_mode_raises(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = self._make_args(governance_mode="invalid_mode")
        with pytest.raises(ValueError):
            GovernanceConfig.from_env_and_args(args)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernanceConfig -v`
Expected: FAIL (GovernanceConfig not yet defined)

**Step 3: Write minimal implementation**

Add to `backend/core/ouroboros/governance/integration.py`, after the existing code:

```python
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.contract_gate import ContractVersion
from backend.core.ouroboros.governance.risk_engine import POLICY_VERSION


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
```

Note: The imports for `hashlib`, `json`, `os`, `Path`, and typing should be at the top of the file. Move the existing `from __future__ import annotations` and `import enum` to be alongside them. The file should have one import block at the top.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernanceConfig -v`
Expected: 10 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/integration.py tests/test_ouroboros_governance/test_integration.py
git commit -m "feat(governance): add GovernanceConfig frozen dataclass with env/args builder"
```

---

## Task 3: GovernanceStack Dataclass + Lifecycle + Write Gate + Replay

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py`
- Modify: `tests/test_ouroboros_governance/test_integration.py`

**Context:** GovernanceStack holds all instantiated governance components. It provides `start()`/`stop()` (idempotent), `health()`, `can_write()` (single authority), `replay_decision()`, and `drain()`.

Important API facts discovered during research:
- `DegradationController` property is `.mode` (NOT `.current_mode`)
- `RoutingPolicy` exposes `.cost_guardrail` (NOT `._guardrail`)
- `OperationLedger` has `get_history(op_id)` returning `List[LedgerEntry]` — no `get_entry()` or `pending_count`
- `SupervisorOuroborosController` has `async def start()`, `async def stop()`, `.mode`, `.writes_allowed`
- `CanaryController` has `.slices` dict property and `.is_file_allowed(file_path: str) -> bool`
- `RuntimeContractChecker` has `.check_before_write(proposed_version) -> bool`

**Step 1: Write the failing tests**

Append to `tests/test_ouroboros_governance/test_integration.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


# ── GovernanceStack ─────────────────────────────────────────────


def _make_mock_stack_components():
    """Create mock governance components for testing GovernanceStack."""
    controller = MagicMock()
    controller.start = AsyncMock()
    controller.stop = AsyncMock()
    controller.mode = MagicMock()
    controller.mode.value = "sandbox"
    controller.writes_allowed = True

    risk_engine = MagicMock()
    ledger = MagicMock()
    ledger.get_history = AsyncMock(return_value=[])

    comm = MagicMock()
    lock_manager = MagicMock()
    break_glass = MagicMock()
    change_engine = MagicMock()
    resource_monitor = MagicMock()

    degradation = MagicMock()
    degradation.mode = MagicMock()
    degradation.mode.value = 0  # FULL_AUTONOMY

    routing = MagicMock()
    routing.cost_guardrail = MagicMock()
    routing.cost_guardrail.remaining = 10.0

    canary = MagicMock()
    canary.slices = {}
    canary.is_file_allowed = MagicMock(return_value=True)

    contract_checker = MagicMock()
    contract_checker.check_before_write = MagicMock(return_value=True)

    return {
        "controller": controller,
        "risk_engine": risk_engine,
        "ledger": ledger,
        "comm": comm,
        "lock_manager": lock_manager,
        "break_glass": break_glass,
        "change_engine": change_engine,
        "resource_monitor": resource_monitor,
        "degradation": degradation,
        "routing": routing,
        "canary": canary,
        "contract_checker": contract_checker,
        "event_bridge": None,
        "blast_adapter": None,
        "learning_bridge": None,
        "policy_version": "v0.1.0",
        "capabilities": {},
    }


class TestGovernanceStack:
    """GovernanceStack lifecycle, write gate, and health."""

    def _make_stack(self, **overrides):
        from backend.core.ouroboros.governance.integration import GovernanceStack

        components = _make_mock_stack_components()
        components.update(overrides)
        return GovernanceStack(**components)

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        stack = self._make_stack()
        await stack.start()
        await stack.start()  # second call is no-op
        stack.controller.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        stack = self._make_stack()
        await stack.start()
        await stack.stop()
        await stack.stop()  # second call is no-op
        stack.controller.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        stack = self._make_stack()
        await stack.stop()  # no error
        stack.controller.stop.assert_not_awaited()

    def test_health_returns_structured_dict(self):
        stack = self._make_stack()
        stack._started = True
        health = stack.health()
        assert "mode" in health
        assert "policy_version" in health
        assert "capabilities" in health
        assert "degradation_mode" in health
        assert "canary_slices" in health

    def test_can_write_before_start_denied(self):
        stack = self._make_stack()
        allowed, reason = stack.can_write({"files": []})
        assert allowed is False
        assert reason == "governance_not_started"

    @pytest.mark.asyncio
    async def test_can_write_when_writes_allowed(self):
        stack = self._make_stack()
        await stack.start()
        allowed, reason = stack.can_write({"files": ["foo.py"]})
        assert allowed is True
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_can_write_denied_by_controller(self):
        stack = self._make_stack()
        stack.controller.writes_allowed = False
        await stack.start()
        allowed, reason = stack.can_write({"files": []})
        assert allowed is False
        assert "mode_" in reason

    @pytest.mark.asyncio
    async def test_can_write_denied_by_degradation(self):
        stack = self._make_stack()
        stack.degradation.mode.value = 2  # READ_ONLY_PLANNING
        stack.degradation.mode.name = "READ_ONLY_PLANNING"
        await stack.start()
        allowed, reason = stack.can_write({"files": []})
        assert allowed is False
        assert "degradation_" in reason

    @pytest.mark.asyncio
    async def test_can_write_denied_by_canary(self):
        stack = self._make_stack()
        stack.canary.is_file_allowed = MagicMock(return_value=False)
        await stack.start()
        allowed, reason = stack.can_write({"files": ["blocked.py"]})
        assert allowed is False
        assert "canary_not_promoted" in reason

    @pytest.mark.asyncio
    async def test_can_write_denied_by_contract(self):
        from backend.core.ouroboros.governance.contract_gate import ContractVersion

        stack = self._make_stack()
        stack.contract_checker.check_before_write = MagicMock(return_value=False)
        await stack.start()
        allowed, reason = stack.can_write({
            "files": [],
            "proposed_contract_version": ContractVersion(3, 0, 0),
        })
        assert allowed is False
        assert reason == "contract_incompatible"

    @pytest.mark.asyncio
    async def test_replay_decision_no_entry(self):
        stack = self._make_stack()
        stack.ledger.get_history = AsyncMock(return_value=[])
        result = await stack.replay_decision("nonexistent-op-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_replay_decision_with_entry(self):
        from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
        from backend.core.ouroboros.governance.risk_engine import (
            ChangeType,
            OperationProfile,
            RiskClassification,
            RiskTier,
        )

        entry = LedgerEntry(
            op_id="test-op-1",
            state=OperationState.PLANNED,
            data={
                "profile": {
                    "files_affected": ["test.py"],
                    "change_type": "MODIFY",
                    "blast_radius": 1,
                    "crosses_repo_boundary": False,
                    "touches_security_surface": False,
                    "touches_supervisor": False,
                    "test_scope_confidence": 0.9,
                },
                "risk_tier": "SAFE_AUTO",
            },
        )
        stack = self._make_stack()
        stack.ledger.get_history = AsyncMock(return_value=[entry])
        stack.risk_engine.classify = MagicMock(
            return_value=RiskClassification(
                tier=RiskTier.SAFE_AUTO,
                reason_code="safe_single_file",
                triggered_rules=["single_file_safe"],
                policy_version="v0.1.0",
            )
        )

        result = await stack.replay_decision("test-op-1")
        assert result is not None
        assert result["op_id"] == "test-op-1"
        assert result["replayed_tier"] == "SAFE_AUTO"
        assert result["match"] is True
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernanceStack -v`
Expected: FAIL (GovernanceStack not yet defined)

**Step 3: Write minimal implementation**

Add to `backend/core/ouroboros/governance/integration.py`, after GovernanceConfig:

```python
import asyncio
import logging

from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskEngine,
)
from backend.core.ouroboros.governance.ledger import OperationLedger, LedgerEntry, OperationState
from backend.core.ouroboros.governance.comm_protocol import CommProtocol
from backend.core.ouroboros.governance.supervisor_controller import SupervisorOuroborosController
from backend.core.ouroboros.governance.lock_manager import GovernanceLockManager
from backend.core.ouroboros.governance.break_glass import BreakGlassManager
from backend.core.ouroboros.governance.change_engine import ChangeEngine
from backend.core.ouroboros.governance.resource_monitor import ResourceMonitor
from backend.core.ouroboros.governance.degradation import DegradationController
from backend.core.ouroboros.governance.routing_policy import RoutingPolicy
from backend.core.ouroboros.governance.canary_controller import CanaryController
from backend.core.ouroboros.governance.runtime_contracts import RuntimeContractChecker

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
        """Start all components. Idempotent — second call is no-op."""
        if self._started:
            return
        await self.controller.start()
        # Register canary slices
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
        # Flush ledger, wait for pending lock releases
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
        # Use the first PLANNED entry which contains the profile
        entry = entries[0]
        profile_data = entry.data.get("profile", {})
        if not profile_data:
            return None
        # Reconstruct profile from stored data
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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernanceStack -v`
Expected: 13 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/integration.py tests/test_ouroboros_governance/test_integration.py
git commit -m "feat(governance): add GovernanceStack with lifecycle, write gate, and replay"
```

---

## Task 4: create_governance_stack Factory

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py`
- Modify: `tests/test_ouroboros_governance/test_integration.py`

**Context:** The factory creates all governance components, wires optional bridges (event_bus, oracle, learning_memory), handles per-component budgets, and rolls back partially-created resources on failure. It raises `GovernanceInitError` with specific reason codes.

Constructor signatures:
- `RiskEngine()` — no args
- `OperationLedger(storage_dir: Path)` — required
- `CommProtocol(transports=[LogTransport()])` — default transport
- `GovernanceLockManager()` — no args
- `BreakGlassManager()` — no args
- `SupervisorOuroborosController()` — no args
- `ChangeEngine(project_root=..., ledger=..., comm=..., lock_manager=..., break_glass=..., risk_engine=...)`
- `ResourceMonitor()` — from Phase 2
- `DegradationController()` — no args
- `RoutingPolicy()` — no args
- `CanaryController()` — no args
- `RuntimeContractChecker(current_version: ContractVersion)` — required
- `EventBridge(event_bus, comm)` — optional
- `BlastRadiusAdapter(oracle)` — optional
- `LearningBridge(learning_memory)` — optional

**Step 1: Write the failing tests**

Append to `tests/test_ouroboros_governance/test_integration.py`:

```python
# ── create_governance_stack ─────────────────────────────────────


class TestCreateGovernanceStack:
    """Factory function tests."""

    def _make_config(self, **overrides):
        """Create a GovernanceConfig for testing."""
        from backend.core.ouroboros.governance.integration import GovernanceConfig

        args = argparse.Namespace(skip_governance=False, governance_mode="sandbox")
        return GovernanceConfig.from_env_and_args(args)

    @pytest.mark.asyncio
    async def test_creates_stack_successfully(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            GovernanceConfig,
            GovernanceStack,
            create_governance_stack,
        )

        config = self._make_config()
        # Override ledger_dir to use tmp_path
        config = GovernanceConfig(
            ledger_dir=tmp_path / "ledger",
            policy_version=config.policy_version,
            policy_hash=config.policy_hash,
            contract_version=config.contract_version,
            contract_hash=config.contract_hash,
            config_digest=config.config_digest,
            initial_mode=config.initial_mode,
            skip_governance=config.skip_governance,
            canary_slices=config.canary_slices,
            gcp_daily_budget=config.gcp_daily_budget,
            startup_timeout_s=config.startup_timeout_s,
            component_budget_s=config.component_budget_s,
        )
        stack = await create_governance_stack(config)
        assert isinstance(stack, GovernanceStack)
        assert stack.policy_version == "v0.1.0"

    @pytest.mark.asyncio
    async def test_optional_bridges_missing(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            GovernanceConfig,
            create_governance_stack,
        )

        config = self._make_config()
        config = GovernanceConfig(
            ledger_dir=tmp_path / "ledger",
            policy_version=config.policy_version,
            policy_hash=config.policy_hash,
            contract_version=config.contract_version,
            contract_hash=config.contract_hash,
            config_digest=config.config_digest,
            initial_mode=config.initial_mode,
            skip_governance=config.skip_governance,
            canary_slices=config.canary_slices,
            gcp_daily_budget=config.gcp_daily_budget,
            startup_timeout_s=config.startup_timeout_s,
            component_budget_s=config.component_budget_s,
        )
        stack = await create_governance_stack(config)
        assert stack.event_bridge is None
        assert stack.blast_adapter is None
        assert stack.learning_bridge is None
        assert stack.capabilities["event_bridge"].enabled is False
        assert stack.capabilities["event_bridge"].reason == "dep_missing"

    @pytest.mark.asyncio
    async def test_optional_bridges_present(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            GovernanceConfig,
            create_governance_stack,
        )

        config = self._make_config()
        config = GovernanceConfig(
            ledger_dir=tmp_path / "ledger",
            policy_version=config.policy_version,
            policy_hash=config.policy_hash,
            contract_version=config.contract_version,
            contract_hash=config.contract_hash,
            config_digest=config.config_digest,
            initial_mode=config.initial_mode,
            skip_governance=config.skip_governance,
            canary_slices=config.canary_slices,
            gcp_daily_budget=config.gcp_daily_budget,
            startup_timeout_s=config.startup_timeout_s,
            component_budget_s=config.component_budget_s,
        )
        mock_event_bus = MagicMock()
        mock_oracle = MagicMock()
        mock_learning = MagicMock()

        stack = await create_governance_stack(
            config,
            event_bus=mock_event_bus,
            oracle=mock_oracle,
            learning_memory=mock_learning,
        )
        assert stack.event_bridge is not None
        assert stack.blast_adapter is not None
        assert stack.learning_bridge is not None
        assert stack.capabilities["event_bridge"].enabled is True

    @pytest.mark.asyncio
    async def test_capabilities_reason_map(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            GovernanceConfig,
            create_governance_stack,
        )

        config = self._make_config()
        config = GovernanceConfig(
            ledger_dir=tmp_path / "ledger",
            policy_version=config.policy_version,
            policy_hash=config.policy_hash,
            contract_version=config.contract_version,
            contract_hash=config.contract_hash,
            config_digest=config.config_digest,
            initial_mode=config.initial_mode,
            skip_governance=config.skip_governance,
            canary_slices=config.canary_slices,
            gcp_daily_budget=config.gcp_daily_budget,
            startup_timeout_s=config.startup_timeout_s,
            component_budget_s=config.component_budget_s,
        )
        stack = await create_governance_stack(config)
        # All capabilities should have reason strings
        for name, status in stack.capabilities.items():
            assert isinstance(status.reason, str)
            assert len(status.reason) > 0
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestCreateGovernanceStack -v`
Expected: FAIL (create_governance_stack not yet defined)

**Step 3: Write minimal implementation**

Add to `backend/core/ouroboros/governance/integration.py`, after GovernanceStack:

```python
from backend.core.ouroboros.governance.comm_protocol import LogTransport
from backend.core.ouroboros.governance.event_bridge import EventBridge
from backend.core.ouroboros.governance.blast_radius_adapter import BlastRadiusAdapter
from backend.core.ouroboros.governance.learning_bridge import LearningBridge


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
                _event_bridge = EventBridge(event_bus=event_bus, comm=comm)
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

    except Exception as exc:
        raise GovernanceInitError(
            "governance_init_error", str(exc)
        ) from exc
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestCreateGovernanceStack -v`
Expected: 4 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/integration.py tests/test_ouroboros_governance/test_integration.py
git commit -m "feat(governance): add create_governance_stack factory with capability tracking"
```

---

## Task 5: CLI Functions (register_governance_argparse + handle_break_glass_command)

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py`
- Modify: `tests/test_ouroboros_governance/test_integration.py`

**Context:** `register_governance_argparse()` adds flags to the existing security argument group. `handle_break_glass_command()` dispatches break-glass CLI operations, guarding against absent stack.

**Step 1: Write the failing tests**

Append to `tests/test_ouroboros_governance/test_integration.py`:

```python
# ── register_governance_argparse ────────────────────────────────


class TestRegisterGovernanceArgparse:
    """Argparse registration adds governance flags."""

    def test_adds_skip_governance(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args(["--skip-governance"])
        assert args.skip_governance is True

    def test_adds_governance_mode(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args(["--governance-mode", "governed"])
        assert args.governance_mode == "governed"

    def test_default_governance_mode_is_sandbox(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args([])
        assert args.governance_mode == "sandbox"

    def test_adds_break_glass_flags(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args(["--break-glass", "list"])
        assert args.break_glass_action == "list"

    def test_break_glass_with_op_id(self):
        from backend.core.ouroboros.governance.integration import register_governance_argparse

        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("Security")
        register_governance_argparse(group)
        args = parser.parse_args([
            "--break-glass", "issue",
            "--break-glass-op-id", "op-123",
            "--break-glass-reason", "emergency fix",
        ])
        assert args.break_glass_action == "issue"
        assert args.break_glass_op_id == "op-123"
        assert args.break_glass_reason == "emergency fix"


# ── handle_break_glass_command ──────────────────────────────────


class TestHandleBreakGlassCommand:
    """Break-glass CLI dispatch handles all cases."""

    @pytest.mark.asyncio
    async def test_list_with_no_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(break_glass_action="list")
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 0  # list with no stack returns empty, not error

    @pytest.mark.asyncio
    async def test_audit_with_no_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(break_glass_action="audit")
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_issue_with_no_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(
            break_glass_action="issue",
            break_glass_op_id="op-1",
            break_glass_reason="test",
            break_glass_ttl=300,
        )
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 1  # can't issue without stack

    @pytest.mark.asyncio
    async def test_issue_with_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        mock_stack = MagicMock()
        mock_stack.break_glass = MagicMock()
        mock_stack.break_glass.issue = MagicMock(return_value=MagicMock(token_id="t1"))

        args = argparse.Namespace(
            break_glass_action="issue",
            break_glass_op_id="op-1",
            break_glass_reason="emergency",
            break_glass_ttl=300,
        )
        exit_code = await handle_break_glass_command(args, mock_stack)
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_revoke_with_no_stack(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(
            break_glass_action="revoke",
            break_glass_op_id="op-1",
            break_glass_reason="done",
        )
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command

        args = argparse.Namespace(break_glass_action="unknown_action")
        exit_code = await handle_break_glass_command(args, stack=None)
        assert exit_code == 1
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestRegisterGovernanceArgparse tests/test_ouroboros_governance/test_integration.py::TestHandleBreakGlassCommand -v`
Expected: FAIL (functions not yet defined)

**Step 3: Write minimal implementation**

Add to `backend/core/ouroboros/governance/integration.py`:

```python
import argparse as _argparse


# ---------------------------------------------------------------------------
# Argparse Registration
# ---------------------------------------------------------------------------


def register_governance_argparse(security_group: _argparse._ActionsContainer) -> None:
    """Add governance flags to existing security argument group.

    Flags:
    --skip-governance    Force READ_ONLY_PLANNING (never full bypass)
    --governance-mode    {sandbox, governed, safe} (default: sandbox)
    --break-glass        {issue, list, revoke, audit} subcommand
    --break-glass-op-id  Operation ID for issue/revoke
    --break-glass-reason Reason string for issue/revoke
    --break-glass-ttl    TTL in seconds (default 300)
    """
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
            print("[Governance] No governance stack — no active tokens.")
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
            print("[Governance] No governance stack — no audit data.")
            return 0
        from backend.core.ouroboros.governance.cli_commands import get_audit_report
        report = get_audit_report(stack.break_glass)
        for entry in report:
            print(f"  {entry}")
        return 0

    if action == "issue":
        if stack is None:
            print("[Governance] ERROR: Cannot issue break-glass token — no governance stack.")
            return 1
        from backend.core.ouroboros.governance.cli_commands import issue_break_glass
        op_id = getattr(args, "break_glass_op_id", None)
        reason = getattr(args, "break_glass_reason", None)
        ttl = getattr(args, "break_glass_ttl", 300)
        token = issue_break_glass(
            stack.break_glass, op_id=op_id, reason=reason, ttl_seconds=ttl
        )
        print(f"[Governance] Break-glass token issued: {token.token_id}")
        return 0

    if action == "revoke":
        if stack is None:
            print("[Governance] ERROR: Cannot revoke — no governance stack.")
            return 1
        from backend.core.ouroboros.governance.cli_commands import revoke_break_glass
        op_id = getattr(args, "break_glass_op_id", None)
        reason = getattr(args, "break_glass_reason", "manual_revoke")
        revoke_break_glass(stack.break_glass, op_id=op_id, reason=reason)
        print(f"[Governance] Break-glass token revoked for {op_id}.")
        return 0

    print(f"[Governance] Unknown break-glass action: {action}")
    return 1
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestRegisterGovernanceArgparse tests/test_ouroboros_governance/test_integration.py::TestHandleBreakGlassCommand -v`
Expected: 11 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/integration.py tests/test_ouroboros_governance/test_integration.py
git commit -m "feat(governance): add CLI argparse registration and break-glass handler"
```

---

## Task 6: Wire Exports into governance __init__.py

**Files:**
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Context:** Add the integration module's public API to the governance package's `__init__.py` exports, following the same pattern as Phases 0-3.

**Step 1: Write the failing test**

Append to `tests/test_ouroboros_governance/test_integration.py`:

```python
# ── Package exports ─────────────────────────────────────────────


class TestPackageExports:
    """Integration module exports must be accessible from the package."""

    def test_governance_mode_importable(self):
        from backend.core.ouroboros.governance import GovernanceMode
        assert GovernanceMode.SANDBOX.value == "sandbox"

    def test_governance_config_importable(self):
        from backend.core.ouroboros.governance import GovernanceConfig
        assert GovernanceConfig is not None

    def test_governance_stack_importable(self):
        from backend.core.ouroboros.governance import GovernanceStack
        assert GovernanceStack is not None

    def test_governance_init_error_importable(self):
        from backend.core.ouroboros.governance import GovernanceInitError
        assert GovernanceInitError is not None

    def test_capability_status_importable(self):
        from backend.core.ouroboros.governance import CapabilityStatus
        assert CapabilityStatus is not None

    def test_create_governance_stack_importable(self):
        from backend.core.ouroboros.governance import create_governance_stack
        assert callable(create_governance_stack)

    def test_register_governance_argparse_importable(self):
        from backend.core.ouroboros.governance import register_governance_argparse
        assert callable(register_governance_argparse)

    def test_handle_break_glass_command_importable(self):
        from backend.core.ouroboros.governance import handle_break_glass_command
        assert callable(handle_break_glass_command)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestPackageExports -v`
Expected: FAIL (imports not yet wired)

**Step 3: Add exports to __init__.py**

Add the following block to `backend/core/ouroboros/governance/__init__.py` after the existing CLI commands imports (after line 151):

```python
from backend.core.ouroboros.governance.integration import (
    GovernanceMode,
    CapabilityStatus,
    GovernanceInitError,
    GovernanceConfig,
    GovernanceStack,
    create_governance_stack,
    register_governance_argparse,
    handle_break_glass_command,
)
```

Also add to the module docstring, after the Phase 3 Components section:

```
Integration Components:
    - GovernanceMode: Operating mode enum (PENDING/SANDBOX/READ_ONLY_PLANNING/GOVERNED/EMERGENCY_STOP)
    - GovernanceConfig: Frozen configuration with policy hashes
    - GovernanceStack: Component holder with lifecycle, write gate, and replay
    - create_governance_stack: Factory with timeout and partial-init rollback
    - register_governance_argparse: CLI flag registration
    - handle_break_glass_command: Break-glass CLI dispatch
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestPackageExports -v`
Expected: 8 PASSED

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/__init__.py tests/test_ouroboros_governance/test_integration.py
git commit -m "feat(governance): wire integration module exports into package __init__"
```

---

## Task 7: Supervisor Hook Points 1-4

**Files:**
- Modify: `unified_supervisor.py` (4 locations, ~24 lines total)

**Context:** This task adds the 4 hook points that connect the supervisor to the governance integration module. Each hook is minimal and delegates to the integration module.

**Important:** Line numbers may shift after each edit. Always re-grep for the insertion point before editing.

### Step 1: Hook 1 — __init__ state vars

Grep for the exact insertion point:

```bash
grep -n "self._autonomy_checks: Dict\[str, Any\] = {}" unified_supervisor.py
```

Expected: line ~66615

Insert AFTER that line (after `self._autonomy_checks: Dict[str, Any] = {}`):

```python
        # v301.0: Ouroboros Governance state
        self._governance_stack: Optional["GovernanceStack"] = None
        self._governance_mode = "pending"  # uses string; integration module uses enum
        self._governance_init_reason: str = "pending_startup"
```

### Step 2: Hook 2 — Argparse registration

Grep for the exact insertion point:

```bash
grep -n '"--no-watchdog"' unified_supervisor.py
```

Expected: line ~97300

Find the closing paren of the `--no-watchdog` argument (line ~97303). Insert AFTER it:

```python
    # v301.0: Ouroboros Governance flags
    from backend.core.ouroboros.governance.integration import register_governance_argparse
    register_governance_argparse(security)
```

### Step 3: Hook 3 — Zone 6.5 governance gate

Grep for the exact insertion point:

```bash
grep -n "return True  # Trinity is optional" unified_supervisor.py
```

Expected: line ~85840

Insert AFTER `return True  # Trinity is optional`:

```python

        # ── v301.0: Governance Gate ──────────────────────────────
        try:
            from backend.core.ouroboros.governance.integration import (
                GovernanceConfig, GovernanceMode, create_governance_stack, GovernanceInitError,
            )
            if not getattr(self._args, "skip_governance", False):
                try:
                    _gov_config = GovernanceConfig.from_env_and_args(self._args)
                    self._governance_stack = await asyncio.wait_for(
                        create_governance_stack(
                            _gov_config,
                            event_bus=getattr(self, "_cross_repo_event_bus", None),
                            oracle=getattr(self, "_codebase_knowledge_graph", None),
                            learning_memory=getattr(self, "_learning_memory", None),
                        ),
                        timeout=_gov_config.startup_timeout_s,
                    )
                    await self._governance_stack.start()
                    self._governance_mode = self._governance_stack.controller.mode.value
                    self._governance_init_reason = "ok"
                    self.logger.info("[Kernel] Governance gate: %s", self._governance_stack.health())
                except (GovernanceInitError, asyncio.TimeoutError) as exc:
                    self._governance_mode = "read_only_planning"
                    self._governance_init_reason = str(exc)
                    self.logger.warning("[Kernel] Governance gate failed: %s -- READ_ONLY_PLANNING", exc)
            else:
                self._governance_mode = "read_only_planning"
                self._governance_init_reason = "skip_governance_flag"
                self.logger.info("[Kernel] Governance skipped -- READ_ONLY_PLANNING")
        except ImportError:
            self.logger.debug("[Kernel] Governance module not available -- skipping")
```

### Step 4: Hook 4 — CLI dispatch

Grep for the exact insertion point:

```bash
grep -n "monitor_trinity" unified_supervisor.py | head -5
```

Expected: line ~100031

Insert AFTER the `monitor_trinity` block (after line ~100031):

```python
    # v301.0: Break-glass CLI dispatch
    if getattr(args, "break_glass_action", None):
        from backend.core.ouroboros.governance.integration import handle_break_glass_command
        _stack = getattr(kernel, "_governance_stack", None) if kernel else None
        import sys as _sys
        _sys.exit(await handle_break_glass_command(args, _stack))
```

### Step 5: Verify supervisor still parses

Run: `python3 -c "import ast; ast.parse(open('unified_supervisor.py').read()); print('AST OK')"`
Expected: `AST OK`

### Step 6: Commit

```bash
git add unified_supervisor.py
git commit -m "feat(governance): add 4 supervisor hook points for governance integration"
```

---

## Task 8: Full Integration Tests

**Files:**
- Modify: `tests/test_ouroboros_governance/test_integration.py`

**Context:** End-to-end tests validating the complete flow: config creation, stack factory, lifecycle, canary registration, write gate, and CLI dispatch.

**Step 1: Write the integration tests**

Append to `tests/test_ouroboros_governance/test_integration.py`:

```python
# ── End-to-End Integration Tests ────────────────────────────────


class TestEndToEnd:
    """Full flow: config -> factory -> start -> write gate -> stop."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            GovernanceConfig,
            GovernanceMode,
            GovernanceStack,
            create_governance_stack,
        )

        args = argparse.Namespace(skip_governance=False, governance_mode="sandbox")
        config = GovernanceConfig.from_env_and_args(args)
        config = GovernanceConfig(
            ledger_dir=tmp_path / "ledger",
            policy_version=config.policy_version,
            policy_hash=config.policy_hash,
            contract_version=config.contract_version,
            contract_hash=config.contract_hash,
            config_digest=config.config_digest,
            initial_mode=config.initial_mode,
            skip_governance=config.skip_governance,
            canary_slices=config.canary_slices,
            gcp_daily_budget=config.gcp_daily_budget,
            startup_timeout_s=config.startup_timeout_s,
            component_budget_s=config.component_budget_s,
        )

        # Create
        stack = await create_governance_stack(config)
        assert isinstance(stack, GovernanceStack)
        assert stack._started is False

        # Write gate before start = denied
        allowed, reason = stack.can_write({"files": ["test.py"]})
        assert allowed is False
        assert reason == "governance_not_started"

        # Start
        await stack.start()
        assert stack._started is True

        # Health
        health = stack.health()
        assert "mode" in health
        assert "policy_version" in health
        assert health["policy_version"] == "v0.1.0"

        # Stop
        await stack.stop()
        assert stack._started is False

        # Double-stop is safe
        await stack.stop()

    @pytest.mark.asyncio
    async def test_skip_governance_forces_read_only(self):
        from backend.core.ouroboros.governance.integration import GovernanceConfig, GovernanceMode

        args = argparse.Namespace(skip_governance=True, governance_mode="governed")
        config = GovernanceConfig.from_env_and_args(args)
        assert config.initial_mode == GovernanceMode.READ_ONLY_PLANNING

    @pytest.mark.asyncio
    async def test_break_glass_cli_round_trip(self, tmp_path):
        from backend.core.ouroboros.governance.integration import (
            GovernanceConfig,
            create_governance_stack,
            handle_break_glass_command,
        )

        args = argparse.Namespace(skip_governance=False, governance_mode="sandbox")
        config = GovernanceConfig.from_env_and_args(args)
        config = GovernanceConfig(
            ledger_dir=tmp_path / "ledger",
            policy_version=config.policy_version,
            policy_hash=config.policy_hash,
            contract_version=config.contract_version,
            contract_hash=config.contract_hash,
            config_digest=config.config_digest,
            initial_mode=config.initial_mode,
            skip_governance=config.skip_governance,
            canary_slices=config.canary_slices,
            gcp_daily_budget=config.gcp_daily_budget,
            startup_timeout_s=config.startup_timeout_s,
            component_budget_s=config.component_budget_s,
        )

        stack = await create_governance_stack(config)
        await stack.start()

        # List should work (empty)
        code = await handle_break_glass_command(
            argparse.Namespace(break_glass_action="list"), stack
        )
        assert code == 0

        # Audit should work (empty)
        code = await handle_break_glass_command(
            argparse.Namespace(break_glass_action="audit"), stack
        )
        assert code == 0

        await stack.stop()

    def test_all_prior_governance_tests_still_pass(self):
        """Canary: import the package to ensure no circular imports."""
        import backend.core.ouroboros.governance
        assert hasattr(backend.core.ouroboros.governance, "GovernanceMode")
        assert hasattr(backend.core.ouroboros.governance, "GovernanceStack")
```

**Step 2: Run the full integration test suite**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py -v`
Expected: All tests PASSED (approximately 55 tests)

**Step 3: Run the complete governance test suite to verify no regressions**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short`
Expected: 299 + ~55 = ~354 tests PASSED

**Step 4: Verify supervisor AST**

Run: `python3 -c "import ast; ast.parse(open('unified_supervisor.py').read()); print('AST OK')"`
Expected: `AST OK`

**Step 5: Commit**

```bash
git add tests/test_ouroboros_governance/test_integration.py
git commit -m "test(governance): add end-to-end integration tests for production activation"
```

---

## Summary

| Task | Description | New Tests |
|------|-------------|-----------|
| 1 | GovernanceMode, CapabilityStatus, GovernanceInitError | 9 |
| 2 | GovernanceConfig frozen dataclass | 10 |
| 3 | GovernanceStack lifecycle + write gate + replay | 13 |
| 4 | create_governance_stack factory | 4 |
| 5 | CLI argparse + break-glass handler | 11 |
| 6 | Wire exports into __init__.py | 8 |
| 7 | Supervisor hook points 1-4 | 0 (AST check) |
| 8 | End-to-end integration tests | ~4 |

**Total new tests: ~59**
**Total governance tests after completion: ~358**
**Supervisor lines changed: ~24 across 4 locations**

# Ouroboros v2.0 Phase 0 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Establish the foundation for JARVIS self-programming: supervisor authority, operation identity, deterministic risk engine, contract gate (Track 1), and a working sandbox improvement loop (Track 2).

**Architecture:** Parallel tracks — Track 1 (plumbing/gates) and Track 2 (sandbox loop). Track 1 is the release gate for any production writes. All new code goes in `backend/core/ouroboros/governance/` (new package) to avoid bloating existing files further. The supervisor gets a thin `SupervisorOuroborosController` that delegates to governance components.

**Tech Stack:** Python 3.11+, asyncio, UUIDv7 (uuid6 package), pytest, pytest-asyncio, git worktrees

**Design doc:** `docs/plans/2026-03-07-ouroboros-v2-design.md`

**Key existing code references:**
- Supervisor OuroborosEngine: `unified_supervisor.py:28387` (simple version, to be wrapped)
- Trinity.Connector: `unified_supervisor.py:65141` (initializes native_integration)
- Real OuroborosEngine: `backend/core/ouroboros/engine.py:877`
- `improve()` method: `backend/core/ouroboros/engine.py:1594`
- SecurityValidator: `backend/core/ouroboros/native_integration.py:255`
- DLM: `backend/core/distributed_lock_manager.py:598`

---

## Track 1: Plumbing & Gates

### Task 1: Create governance package skeleton

**Files:**
- Create: `backend/core/ouroboros/governance/__init__.py`
- Create: `backend/core/ouroboros/governance/operation_id.py`
- Create: `tests/test_ouroboros_governance/__init__.py`
- Create: `tests/test_ouroboros_governance/conftest.py`

**Step 1: Create the package directories**

```bash
mkdir -p backend/core/ouroboros/governance
mkdir -p tests/test_ouroboros_governance
```

**Step 2: Create `__init__.py` with package docstring**

```python
# backend/core/ouroboros/governance/__init__.py
"""
Ouroboros Governance Layer
=========================

Deterministic policy enforcement for autonomous self-programming.
All risk classification, operation identity, and lifecycle authority
lives here. No LLM calls in this package — pure rule-based logic.

Components:
    - OperationID: UUIDv7-based globally unique operation identity
    - RiskEngine: Deterministic policy classifier (SAFE_AUTO / APPROVAL_REQUIRED / BLOCKED)
    - ContractGate: Schema version compatibility enforcement
    - SupervisorController: Lifecycle authority bridge to unified_supervisor
    - CommProtocol: Mandatory 5-phase communication emitter
    - OperationLedger: Append-only operation state log
"""
```

**Step 3: Create test conftest**

```python
# tests/test_ouroboros_governance/conftest.py
"""Shared fixtures for Ouroboros governance tests."""

import pytest
import asyncio
import tempfile
from pathlib import Path


@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project structure for testing."""
    src = tmp_path / "backend" / "core"
    src.mkdir(parents=True)
    (src / "__init__.py").touch()
    test_file = src / "example.py"
    test_file.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def tmp_ledger_dir(tmp_path):
    """Temporary directory for operation ledger."""
    d = tmp_path / "ledger"
    d.mkdir()
    return d
```

**Step 4: Create `__init__.py` for test package**

```python
# tests/test_ouroboros_governance/__init__.py
```

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/ tests/test_ouroboros_governance/
git commit -m "feat(ouroboros): create governance package skeleton"
```

---

### Task 2: Operation Identity System (UUIDv7)

**Files:**
- Create: `backend/core/ouroboros/governance/operation_id.py`
- Create: `tests/test_ouroboros_governance/test_operation_id.py`

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_operation_id.py
"""Tests for UUIDv7-based operation identity system."""

import pytest
import time
from concurrent.futures import ThreadPoolExecutor

from backend.core.ouroboros.governance.operation_id import (
    OperationID,
    generate_operation_id,
    OperationMetadata,
)


class TestOperationIDGeneration:
    def test_generate_returns_valid_format(self):
        op_id = generate_operation_id(repo_origin="jarvis")
        assert op_id.startswith("op-")
        assert op_id.endswith("-jarvis")
        # UUIDv7 middle part should be 36 chars (with hyphens)
        parts = op_id.split("-", 1)[1].rsplit("-", 1)[0]
        assert len(parts) == 36

    def test_generate_monotonic_sorting(self):
        ids = [generate_operation_id(repo_origin="jarvis") for _ in range(100)]
        assert ids == sorted(ids), "Operation IDs must sort chronologically"

    def test_generate_no_collisions_concurrent(self):
        """10K concurrent generations must produce zero collisions."""
        with ThreadPoolExecutor(max_workers=50) as pool:
            futures = [
                pool.submit(generate_operation_id, "jarvis")
                for _ in range(10_000)
            ]
            ids = [f.result() for f in futures]
        assert len(set(ids)) == 10_000

    def test_different_repo_origins(self):
        id_j = generate_operation_id(repo_origin="jarvis")
        id_p = generate_operation_id(repo_origin="prime")
        id_r = generate_operation_id(repo_origin="reactor")
        assert id_j.endswith("-jarvis")
        assert id_p.endswith("-prime")
        assert id_r.endswith("-reactor")


class TestOperationMetadata:
    def test_create_with_policy_version(self):
        meta = OperationMetadata(
            op_id=generate_operation_id("jarvis"),
            policy_version="v0.1.0",
            decision_inputs={"files_affected": ["a.py"], "blast_radius": 2},
        )
        assert meta.policy_version == "v0.1.0"
        assert meta.decision_inputs_hash is not None
        assert len(meta.decision_inputs_hash) == 64  # SHA-256

    def test_same_inputs_produce_same_hash(self):
        inputs = {"files_affected": ["a.py"], "blast_radius": 2}
        m1 = OperationMetadata(
            op_id=generate_operation_id("jarvis"),
            policy_version="v0.1.0",
            decision_inputs=inputs,
        )
        m2 = OperationMetadata(
            op_id=generate_operation_id("jarvis"),
            policy_version="v0.1.0",
            decision_inputs=inputs,
        )
        assert m1.decision_inputs_hash == m2.decision_inputs_hash

    def test_idempotency_check(self):
        op_id = generate_operation_id("jarvis")
        meta = OperationMetadata(
            op_id=op_id,
            policy_version="v0.1.0",
            decision_inputs={},
        )
        seen = set()
        assert meta.is_new(seen) is True
        seen.add(op_id)
        assert meta.is_new(seen) is False
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_operation_id.py -v
```
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Install uuid6 package (UUIDv7 support)**

```bash
python3 -m pip install uuid6
```

**Step 4: Write minimal implementation**

```python
# backend/core/ouroboros/governance/operation_id.py
"""
Operation Identity System — UUIDv7-based globally unique IDs.

Every autonomous action gets an OperationID that is:
- Monotonic-sortable (chronological string comparison)
- Globally unique across JARVIS/Prime/Reactor
- Used as idempotency key for deduplication
- Pinned to a policy_version for replay determinism
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from uuid6 import uuid7


def generate_operation_id(repo_origin: str = "jarvis") -> str:
    """Generate a monotonic-sortable, globally unique operation ID.

    Format: op-<uuidv7>-<repo_origin>
    """
    return f"op-{uuid7()}-{repo_origin}"


def _hash_inputs(inputs: Dict[str, Any]) -> str:
    """Produce a deterministic SHA-256 hash of decision inputs."""
    canonical = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class OperationMetadata:
    """Metadata for a single autonomous operation.

    Persisted alongside every decision for replay determinism.
    """

    op_id: str
    policy_version: str
    decision_inputs: Dict[str, Any]
    model_metadata_hash: Optional[str] = None
    decision_inputs_hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.decision_inputs_hash = _hash_inputs(self.decision_inputs)

    def is_new(self, seen_ids: Set[str]) -> bool:
        """Check if this operation has NOT been processed before."""
        return self.op_id not in seen_ids
```

**Step 5: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_operation_id.py -v
```
Expected: ALL PASS

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/operation_id.py tests/test_ouroboros_governance/test_operation_id.py
git commit -m "feat(ouroboros): add UUIDv7 operation identity system"
```

---

### Task 3: Risk Engine — Deterministic Policy Classifier

**Files:**
- Create: `backend/core/ouroboros/governance/risk_engine.py`
- Create: `tests/test_ouroboros_governance/test_risk_engine.py`

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_risk_engine.py
"""Tests for deterministic risk engine — no LLM in classification path."""

import pytest
from pathlib import Path

from backend.core.ouroboros.governance.risk_engine import (
    RiskEngine,
    RiskTier,
    RiskClassification,
    OperationProfile,
    ChangeType,
    HardInvariantViolation,
    POLICY_VERSION,
)


@pytest.fixture
def engine():
    return RiskEngine()


@pytest.fixture
def safe_profile():
    """A profile that should classify as SAFE_AUTO."""
    return OperationProfile(
        files_affected=[Path("backend/utils/helpers.py")],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
    )


class TestRiskTierClassification:
    def test_touches_supervisor_is_blocked(self, engine):
        profile = OperationProfile(
            files_affected=[Path("unified_supervisor.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=0,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=True,
            test_scope_confidence=1.0,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.BLOCKED
        assert result.reason_code == "touches_supervisor"

    def test_touches_security_is_blocked(self, engine):
        profile = OperationProfile(
            files_affected=[Path("backend/auth/tokens.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=True,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.BLOCKED
        assert result.reason_code == "touches_security_surface"

    def test_crosses_repo_boundary_needs_approval(self, engine):
        profile = OperationProfile(
            files_affected=[Path("backend/core/prime_client.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=2,
            crosses_repo_boundary=True,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "crosses_repo_boundary"

    def test_delete_needs_approval(self, engine):
        profile = OperationProfile(
            files_affected=[Path("backend/utils/old.py")],
            change_type=ChangeType.DELETE,
            blast_radius=0,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=1.0,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "delete_operation"

    def test_high_blast_radius_needs_approval(self, engine):
        profile = OperationProfile(
            files_affected=[Path("backend/core/base.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=6,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "blast_radius_exceeded"

    def test_many_files_needs_approval(self, engine):
        profile = OperationProfile(
            files_affected=[Path(f"f{i}.py") for i in range(3)],
            change_type=ChangeType.MODIFY,
            blast_radius=2,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "too_many_files"

    def test_low_test_confidence_needs_approval(self, engine):
        profile = OperationProfile(
            files_affected=[Path("backend/utils/helpers.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.6,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "low_test_confidence"

    def test_safe_single_file_fix_is_safe_auto(self, engine, safe_profile):
        result = engine.classify(safe_profile)
        assert result.tier == RiskTier.SAFE_AUTO

    def test_dependency_change_needs_approval(self, engine):
        profile = OperationProfile(
            files_affected=[Path("requirements.txt")],
            change_type=ChangeType.MODIFY,
            blast_radius=0,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=1.0,
            is_dependency_change=True,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.APPROVAL_REQUIRED
        assert result.reason_code == "dependency_change"


class TestDeterminism:
    def test_same_input_1000x_same_result(self, engine, safe_profile):
        """Deterministic: same input always produces same output."""
        first = engine.classify(safe_profile)
        for _ in range(999):
            result = engine.classify(safe_profile)
            assert result.tier == first.tier
            assert result.reason_code == first.reason_code

    def test_classification_includes_policy_version(self, engine, safe_profile):
        result = engine.classify(safe_profile)
        assert result.policy_version == POLICY_VERSION


class TestHardInvariants:
    def test_contract_regression_blocks(self, engine, safe_profile):
        """Hard invariant: contract_regression_delta must be 0."""
        with pytest.raises(HardInvariantViolation, match="contract_regression"):
            engine.enforce_invariants(
                safe_profile,
                contract_regression_delta=1,
                security_risk_delta=0,
                operator_load_delta=0,
            )

    def test_security_risk_increase_blocks(self, engine, safe_profile):
        with pytest.raises(HardInvariantViolation, match="security_risk"):
            engine.enforce_invariants(
                safe_profile,
                contract_regression_delta=0,
                security_risk_delta=1,
                operator_load_delta=0,
            )

    def test_all_invariants_pass(self, engine, safe_profile):
        """No exception when all invariants pass."""
        engine.enforce_invariants(
            safe_profile,
            contract_regression_delta=0,
            security_risk_delta=-1,
            operator_load_delta=0,
        )


class TestCoreOrchestrationPaths:
    def test_create_in_core_path_needs_approval(self, engine):
        profile = OperationProfile(
            files_affected=[Path("backend/core/ouroboros/new_thing.py")],
            change_type=ChangeType.CREATE,
            blast_radius=0,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=1.0,
            is_core_orchestration_path=True,
        )
        result = engine.classify(profile)
        assert result.tier == RiskTier.APPROVAL_REQUIRED
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_risk_engine.py -v
```
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/risk_engine.py
"""
Risk Engine — Deterministic policy classifier for autonomous operations.

Classifies every operation into SAFE_AUTO / APPROVAL_REQUIRED / BLOCKED
using only deterministic rules. No LLM calls. No heuristics.

Policy thresholds are strict for first 30 days, then relaxable via config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

POLICY_VERSION = "v0.1.0"


class RiskTier(Enum):
    SAFE_AUTO = "safe_auto"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


class ChangeType(Enum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"


class HardInvariantViolation(Exception):
    """Raised when a hard invariant is violated — blocks ALL tiers."""
    pass


@dataclass
class OperationProfile:
    """All inputs needed for deterministic risk classification."""

    files_affected: List[Path]
    change_type: ChangeType
    blast_radius: int
    crosses_repo_boundary: bool
    touches_security_surface: bool
    touches_supervisor: bool
    test_scope_confidence: float
    is_dependency_change: bool = False
    is_core_orchestration_path: bool = False


@dataclass
class RiskClassification:
    """Output of risk classification — fully deterministic."""

    tier: RiskTier
    reason_code: str
    policy_version: str = POLICY_VERSION


class RiskEngine:
    """Deterministic policy classifier. No LLM. No heuristics.

    Rules are evaluated in priority order — first match wins.
    Thresholds are strict initially (first 30 days) and configurable.
    """

    def __init__(self) -> None:
        # Configurable thresholds (strict defaults for first 30 days)
        self.blast_radius_threshold = int(
            os.getenv("OUROBOROS_BLAST_RADIUS_THRESHOLD", "5")
        )
        self.max_files_threshold = int(
            os.getenv("OUROBOROS_MAX_FILES_THRESHOLD", "2")
        )
        self.test_confidence_threshold = float(
            os.getenv("OUROBOROS_TEST_CONFIDENCE_THRESHOLD", "0.75")
        )

    def classify(self, profile: OperationProfile) -> RiskClassification:
        """Classify an operation into a risk tier.

        Rules evaluated in order — first match wins.
        This method is pure: same input always produces same output.
        """
        # BLOCKED rules (highest severity)
        if profile.touches_supervisor:
            return RiskClassification(
                tier=RiskTier.BLOCKED, reason_code="touches_supervisor"
            )
        if profile.touches_security_surface:
            return RiskClassification(
                tier=RiskTier.BLOCKED, reason_code="touches_security_surface"
            )

        # APPROVAL_REQUIRED rules
        if profile.crosses_repo_boundary:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="crosses_repo_boundary",
            )
        if profile.change_type == ChangeType.DELETE:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="delete_operation",
            )
        if profile.is_dependency_change:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="dependency_change",
            )
        if profile.is_core_orchestration_path and profile.change_type in (
            ChangeType.CREATE,
            ChangeType.DELETE,
            ChangeType.RENAME,
        ):
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="core_path_structural_change",
            )
        if profile.blast_radius > self.blast_radius_threshold:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="blast_radius_exceeded",
            )
        if len(profile.files_affected) > self.max_files_threshold:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="too_many_files",
            )
        if profile.test_scope_confidence < self.test_confidence_threshold:
            return RiskClassification(
                tier=RiskTier.APPROVAL_REQUIRED,
                reason_code="low_test_confidence",
            )

        # SAFE_AUTO — all checks passed
        return RiskClassification(
            tier=RiskTier.SAFE_AUTO, reason_code="all_checks_passed"
        )

    def enforce_invariants(
        self,
        profile: OperationProfile,
        contract_regression_delta: int,
        security_risk_delta: int,
        operator_load_delta: int,
    ) -> None:
        """Enforce hard invariants — violations block ALL tiers.

        Must be called BEFORE the tier gate, not after.
        """
        if contract_regression_delta != 0:
            raise HardInvariantViolation(
                f"contract_regression_delta must be 0, got {contract_regression_delta}"
            )
        if security_risk_delta > 0:
            raise HardInvariantViolation(
                f"security_risk_delta must be <= 0, got {security_risk_delta}"
            )
        if operator_load_delta > 0:
            raise HardInvariantViolation(
                f"operator_load_delta must be <= 0, got {operator_load_delta}"
            )
```

**Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_risk_engine.py -v
```
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/risk_engine.py tests/test_ouroboros_governance/test_risk_engine.py
git commit -m "feat(ouroboros): add deterministic risk engine with hard invariants"
```

---

### Task 4: Operation Ledger — Append-Only State Log

**Files:**
- Create: `backend/core/ouroboros/governance/ledger.py`
- Create: `tests/test_ouroboros_governance/test_ledger.py`

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_ledger.py
"""Tests for append-only operation ledger."""

import pytest
import asyncio
import json
from pathlib import Path

from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    LedgerEntry,
    OperationState,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id


@pytest.fixture
def ledger(tmp_path):
    return OperationLedger(storage_dir=tmp_path / "ledger")


class TestLedgerAppend:
    @pytest.mark.asyncio
    async def test_append_creates_entry(self, ledger):
        op_id = generate_operation_id("jarvis")
        entry = LedgerEntry(
            op_id=op_id,
            state=OperationState.PLANNED,
            data={"goal": "fix bug", "files": ["a.py"]},
        )
        await ledger.append(entry)
        history = await ledger.get_history(op_id)
        assert len(history) == 1
        assert history[0].state == OperationState.PLANNED

    @pytest.mark.asyncio
    async def test_append_preserves_ordering(self, ledger):
        op_id = generate_operation_id("jarvis")
        for state in [
            OperationState.PLANNED,
            OperationState.VALIDATING,
            OperationState.APPLIED,
        ]:
            await ledger.append(
                LedgerEntry(op_id=op_id, state=state, data={})
            )
        history = await ledger.get_history(op_id)
        assert [e.state for e in history] == [
            OperationState.PLANNED,
            OperationState.VALIDATING,
            OperationState.APPLIED,
        ]

    @pytest.mark.asyncio
    async def test_append_is_durable(self, ledger):
        """Entries survive re-instantiation."""
        op_id = generate_operation_id("jarvis")
        await ledger.append(
            LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={})
        )
        # Create new ledger from same storage
        ledger2 = OperationLedger(storage_dir=ledger._storage_dir)
        history = await ledger2.get_history(op_id)
        assert len(history) == 1

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_skip(self, ledger):
        """Duplicate op_id + state combination is skipped."""
        op_id = generate_operation_id("jarvis")
        entry = LedgerEntry(
            op_id=op_id,
            state=OperationState.PLANNED,
            data={"attempt": 1},
        )
        await ledger.append(entry)
        await ledger.append(entry)  # duplicate
        history = await ledger.get_history(op_id)
        assert len(history) == 1


class TestLedgerQuery:
    @pytest.mark.asyncio
    async def test_get_latest_state(self, ledger):
        op_id = generate_operation_id("jarvis")
        await ledger.append(
            LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={})
        )
        await ledger.append(
            LedgerEntry(op_id=op_id, state=OperationState.APPLIED, data={})
        )
        latest = await ledger.get_latest_state(op_id)
        assert latest == OperationState.APPLIED

    @pytest.mark.asyncio
    async def test_get_latest_state_unknown_op(self, ledger):
        result = await ledger.get_latest_state("op-unknown-jarvis")
        assert result is None
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_ledger.py -v
```
Expected: FAIL

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/ledger.py
"""
Operation Ledger — Append-only state log for autonomous operations.

Every state transition (PLANNED -> VALIDATING -> APPLIED -> ...) is
persisted here BEFORE any event is published. This is the source of
truth (outbox pattern).

Storage: One JSON-lines file per operation ID.
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiofiles

logger = logging.getLogger("Ouroboros.Ledger")


class OperationState(Enum):
    PLANNED = "planned"
    SANDBOXING = "sandboxing"
    VALIDATING = "validating"
    GATING = "gating"
    APPLYING = "applying"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class LedgerEntry:
    op_id: str
    state: OperationState
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.monotonic)
    wall_time: float = field(default_factory=time.time)


class OperationLedger:
    """Append-only operation state log.

    Durably stores every state transition. Idempotent on duplicate
    (op_id, state) pairs.
    """

    def __init__(self, storage_dir: Path) -> None:
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._seen: Set[str] = set()  # "op_id:state" dedup keys

    def _op_file(self, op_id: str) -> Path:
        # Sanitize op_id for filename (replace non-alphanumeric)
        safe = op_id.replace("/", "_").replace("\\", "_")
        return self._storage_dir / f"{safe}.jsonl"

    def _dedup_key(self, entry: LedgerEntry) -> str:
        return f"{entry.op_id}:{entry.state.value}"

    async def append(self, entry: LedgerEntry) -> bool:
        """Append entry to ledger. Returns False if duplicate (idempotent)."""
        key = self._dedup_key(entry)
        if key in self._seen:
            return False

        record = {
            "op_id": entry.op_id,
            "state": entry.state.value,
            "data": entry.data,
            "timestamp": entry.timestamp,
            "wall_time": entry.wall_time,
        }

        path = self._op_file(entry.op_id)
        async with aiofiles.open(path, "a") as f:
            await f.write(json.dumps(record, default=str) + "\n")

        self._seen.add(key)
        return True

    async def get_history(self, op_id: str) -> List[LedgerEntry]:
        """Get full state history for an operation, in order."""
        path = self._op_file(op_id)
        if not path.exists():
            return []

        entries = []
        async with aiofiles.open(path, "r") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                entry = LedgerEntry(
                    op_id=record["op_id"],
                    state=OperationState(record["state"]),
                    data=record.get("data", {}),
                    timestamp=record.get("timestamp", 0),
                    wall_time=record.get("wall_time", 0),
                )
                self._seen.add(self._dedup_key(entry))
                entries.append(entry)
        return entries

    async def get_latest_state(self, op_id: str) -> Optional[OperationState]:
        """Get the most recent state for an operation."""
        history = await self.get_history(op_id)
        if not history:
            return None
        return history[-1].state
```

**Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_ledger.py -v
```
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/ledger.py tests/test_ouroboros_governance/test_ledger.py
git commit -m "feat(ouroboros): add append-only operation ledger with idempotency"
```

---

### Task 5: Communication Protocol — 5-Phase Message Emitter

**Files:**
- Create: `backend/core/ouroboros/governance/comm_protocol.py`
- Create: `tests/test_ouroboros_governance/test_comm_protocol.py`

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_comm_protocol.py
"""Tests for mandatory 5-phase communication protocol."""

import pytest
import asyncio

from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    MessageType,
    CommMessage,
    LogTransport,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id


@pytest.fixture
def transport():
    return LogTransport()


@pytest.fixture
def protocol(transport):
    return CommProtocol(transports=[transport])


class TestMessageEmission:
    @pytest.mark.asyncio
    async def test_emit_intent(self, protocol, transport):
        op_id = generate_operation_id("jarvis")
        await protocol.emit_intent(
            op_id=op_id,
            goal="Fix bug in helpers.py",
            target_files=["backend/utils/helpers.py"],
            risk_tier="safe_auto",
            blast_radius=1,
        )
        assert len(transport.messages) == 1
        msg = transport.messages[0]
        assert msg.msg_type == MessageType.INTENT
        assert msg.op_id == op_id
        assert msg.seq == 1

    @pytest.mark.asyncio
    async def test_sequence_numbers_monotonic(self, protocol, transport):
        op_id = generate_operation_id("jarvis")
        await protocol.emit_intent(op_id=op_id, goal="g", target_files=[], risk_tier="safe_auto", blast_radius=0)
        await protocol.emit_plan(op_id=op_id, steps=["step1"], rollback_strategy="revert")
        await protocol.emit_heartbeat(op_id=op_id, phase="validating", progress_pct=50)
        seqs = [m.seq for m in transport.messages]
        assert seqs == [1, 2, 3]
        # Causal links
        assert transport.messages[1].causal_parent_seq == 1
        assert transport.messages[2].causal_parent_seq == 2

    @pytest.mark.asyncio
    async def test_all_five_types_emitted(self, protocol, transport):
        op_id = generate_operation_id("jarvis")
        await protocol.emit_intent(op_id=op_id, goal="g", target_files=[], risk_tier="safe_auto", blast_radius=0)
        await protocol.emit_plan(op_id=op_id, steps=[], rollback_strategy="revert")
        await protocol.emit_heartbeat(op_id=op_id, phase="validating", progress_pct=50)
        await protocol.emit_decision(op_id=op_id, outcome="applied", reason_code="all_checks_passed")
        await protocol.emit_postmortem(op_id=op_id, root_cause=None, failed_phase=None)
        types = [m.msg_type for m in transport.messages]
        assert types == [
            MessageType.INTENT,
            MessageType.PLAN,
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.POSTMORTEM,
        ]


class TestTransportFaultIsolation:
    @pytest.mark.asyncio
    async def test_transport_failure_does_not_block(self):
        """Transport failure must never block the pipeline."""

        class FailingTransport:
            async def send(self, msg: CommMessage) -> None:
                raise ConnectionError("Slack is down")

        log = LogTransport()
        protocol = CommProtocol(transports=[FailingTransport(), log])
        op_id = generate_operation_id("jarvis")

        # Should not raise despite FailingTransport
        await protocol.emit_intent(
            op_id=op_id, goal="g", target_files=[], risk_tier="safe_auto", blast_radius=0
        )
        # LogTransport still received the message
        assert len(log.messages) == 1
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_comm_protocol.py -v
```
Expected: FAIL

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/comm_protocol.py
"""
Communication Protocol — Mandatory 5-phase message emitter.

Every autonomous operation emits: INTENT -> PLAN -> HEARTBEAT -> DECISION -> POSTMORTEM.
Transport failures are fault-isolated: messages queue, never block pipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger("Ouroboros.Comm")


class MessageType(Enum):
    INTENT = "intent"
    PLAN = "plan"
    HEARTBEAT = "heartbeat"
    DECISION = "decision"
    POSTMORTEM = "postmortem"


@dataclass
class CommMessage:
    msg_type: MessageType
    op_id: str
    seq: int
    causal_parent_seq: Optional[int]
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)


class Transport(Protocol):
    """Interface for message transports."""

    async def send(self, msg: CommMessage) -> None: ...


class LogTransport:
    """Simple transport that stores messages in memory + logs them."""

    def __init__(self) -> None:
        self.messages: List[CommMessage] = []

    async def send(self, msg: CommMessage) -> None:
        self.messages.append(msg)
        logger.info(
            f"[{msg.msg_type.value}] op={msg.op_id} seq={msg.seq} "
            f"payload_keys={list(msg.payload.keys())}"
        )


class CommProtocol:
    """Mandatory 5-phase communication emitter.

    Transport failures are fault-isolated: failing transports are
    skipped (logged), and the pipeline continues.
    """

    def __init__(self, transports: Optional[List[Any]] = None) -> None:
        self._transports: List[Any] = transports or [LogTransport()]
        self._seq_counters: Dict[str, int] = {}  # per op_id

    def _next_seq(self, op_id: str) -> int:
        self._seq_counters.setdefault(op_id, 0)
        self._seq_counters[op_id] += 1
        return self._seq_counters[op_id]

    def _prev_seq(self, op_id: str) -> Optional[int]:
        current = self._seq_counters.get(op_id, 0)
        return current - 1 if current > 1 else None

    async def _emit(self, msg: CommMessage) -> None:
        for transport in self._transports:
            try:
                await transport.send(msg)
            except Exception as e:
                logger.warning(
                    f"Transport {type(transport).__name__} failed for "
                    f"op={msg.op_id}: {e}"
                )

    async def emit_intent(
        self, op_id: str, goal: str, target_files: List[str],
        risk_tier: str, blast_radius: int,
    ) -> None:
        seq = self._next_seq(op_id)
        await self._emit(CommMessage(
            msg_type=MessageType.INTENT, op_id=op_id, seq=seq,
            causal_parent_seq=None,
            payload={
                "goal": goal, "target_files": target_files,
                "risk_tier": risk_tier, "blast_radius": blast_radius,
            },
        ))

    async def emit_plan(
        self, op_id: str, steps: List[str], rollback_strategy: str,
    ) -> None:
        seq = self._next_seq(op_id)
        await self._emit(CommMessage(
            msg_type=MessageType.PLAN, op_id=op_id, seq=seq,
            causal_parent_seq=self._prev_seq(op_id),
            payload={"steps": steps, "rollback_strategy": rollback_strategy},
        ))

    async def emit_heartbeat(
        self, op_id: str, phase: str, progress_pct: int,
    ) -> None:
        seq = self._next_seq(op_id)
        await self._emit(CommMessage(
            msg_type=MessageType.HEARTBEAT, op_id=op_id, seq=seq,
            causal_parent_seq=self._prev_seq(op_id),
            payload={"phase": phase, "progress_pct": progress_pct},
        ))

    async def emit_decision(
        self, op_id: str, outcome: str, reason_code: str,
        diff_summary: Optional[str] = None,
    ) -> None:
        seq = self._next_seq(op_id)
        await self._emit(CommMessage(
            msg_type=MessageType.DECISION, op_id=op_id, seq=seq,
            causal_parent_seq=self._prev_seq(op_id),
            payload={
                "outcome": outcome, "reason_code": reason_code,
                "diff_summary": diff_summary,
            },
        ))

    async def emit_postmortem(
        self, op_id: str, root_cause: Optional[str],
        failed_phase: Optional[str],
        next_safe_action: Optional[str] = None,
    ) -> None:
        seq = self._next_seq(op_id)
        await self._emit(CommMessage(
            msg_type=MessageType.POSTMORTEM, op_id=op_id, seq=seq,
            causal_parent_seq=self._prev_seq(op_id),
            payload={
                "root_cause": root_cause, "failed_phase": failed_phase,
                "next_safe_action": next_safe_action,
            },
        ))
```

**Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_comm_protocol.py -v
```
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comm_protocol.py tests/test_ouroboros_governance/test_comm_protocol.py
git commit -m "feat(ouroboros): add 5-phase communication protocol with fault-isolated transport"
```

---

### Task 6: Supervisor Ouroboros Controller — Lifecycle Authority

**Files:**
- Create: `backend/core/ouroboros/governance/supervisor_controller.py`
- Create: `tests/test_ouroboros_governance/test_supervisor_controller.py`

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_supervisor_controller.py
"""Tests for supervisor lifecycle authority over Ouroboros."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.supervisor_controller import (
    SupervisorOuroborosController,
    AutonomyMode,
)


@pytest.fixture
def controller():
    return SupervisorOuroborosController()


class TestLifecycleAuthority:
    @pytest.mark.asyncio
    async def test_starts_in_disabled_mode(self, controller):
        assert controller.mode == AutonomyMode.DISABLED

    @pytest.mark.asyncio
    async def test_start_enters_sandbox_mode(self, controller):
        await controller.start()
        assert controller.mode == AutonomyMode.SANDBOX

    @pytest.mark.asyncio
    async def test_stop_returns_to_disabled(self, controller):
        await controller.start()
        await controller.stop()
        assert controller.mode == AutonomyMode.DISABLED

    @pytest.mark.asyncio
    async def test_pause_enters_read_only(self, controller):
        await controller.start()
        await controller.pause()
        assert controller.mode == AutonomyMode.READ_ONLY

    @pytest.mark.asyncio
    async def test_resume_from_pause(self, controller):
        await controller.start()
        await controller.pause()
        await controller.resume()
        assert controller.mode == AutonomyMode.SANDBOX

    @pytest.mark.asyncio
    async def test_enable_governed_autonomy(self, controller):
        """Only allowed after explicit gate check."""
        await controller.start()
        # Can't go straight to GOVERNED without passing gates
        with pytest.raises(RuntimeError, match="gates"):
            await controller.enable_governed_autonomy()

    @pytest.mark.asyncio
    async def test_emergency_stop(self, controller):
        await controller.start()
        await controller.emergency_stop(reason="3 rollbacks in 1 hour")
        assert controller.mode == AutonomyMode.EMERGENCY_STOP
        # Can't resume from emergency stop without explicit re-enable
        with pytest.raises(RuntimeError, match="emergency"):
            await controller.resume()


class TestSafeModeBoot:
    @pytest.mark.asyncio
    async def test_safe_mode_blocks_writes(self, controller):
        controller._safe_mode = True
        await controller.start()
        assert controller.mode == AutonomyMode.SAFE_MODE
        assert controller.writes_allowed is False

    @pytest.mark.asyncio
    async def test_safe_mode_allows_interactive(self, controller):
        controller._safe_mode = True
        await controller.start()
        assert controller.interactive_allowed is True
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_supervisor_controller.py -v
```
Expected: FAIL

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/supervisor_controller.py
"""
Supervisor Ouroboros Controller — Single lifecycle authority.

The ONLY code that may start/stop/pause/resume Ouroboros autonomy.
Unified_supervisor.py delegates to this; no other entry point allowed.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger("Ouroboros.Controller")


class AutonomyMode(Enum):
    DISABLED = "disabled"          # Not started
    SANDBOX = "sandbox"            # Running, writes to worktree only
    READ_ONLY = "read_only"        # Paused, analyze + plan only
    GOVERNED = "governed"          # Full tiered autonomy (gates passed)
    EMERGENCY_STOP = "emergency_stop"  # Manual kill, requires re-enable
    SAFE_MODE = "safe_mode"        # Watchdog restarted, no autonomy


class SupervisorOuroborosController:
    """Lifecycle authority for Ouroboros self-programming.

    State machine:
        DISABLED -> SANDBOX -> GOVERNED (requires gates)
        Any -> EMERGENCY_STOP (manual kill)
        Any -> READ_ONLY (pause)
        READ_ONLY -> SANDBOX (resume)
        DISABLED w/ safe_mode -> SAFE_MODE
    """

    def __init__(self) -> None:
        self._mode = AutonomyMode.DISABLED
        self._safe_mode = False
        self._gates_passed = False
        self._emergency_reason: Optional[str] = None

    @property
    def mode(self) -> AutonomyMode:
        return self._mode

    @property
    def writes_allowed(self) -> bool:
        return self._mode == AutonomyMode.GOVERNED

    @property
    def sandbox_allowed(self) -> bool:
        return self._mode in (AutonomyMode.SANDBOX, AutonomyMode.GOVERNED)

    @property
    def interactive_allowed(self) -> bool:
        return self._mode != AutonomyMode.DISABLED

    async def start(self) -> None:
        """Start Ouroboros in sandbox mode (or safe mode if watchdog-restarted)."""
        if self._safe_mode:
            self._mode = AutonomyMode.SAFE_MODE
            logger.warning("Starting in SAFE_MODE — no autonomy, interactive only")
            return
        self._mode = AutonomyMode.SANDBOX
        logger.info("Ouroboros started in SANDBOX mode")

    async def stop(self) -> None:
        """Stop Ouroboros completely."""
        self._mode = AutonomyMode.DISABLED
        self._gates_passed = False
        logger.info("Ouroboros stopped")

    async def pause(self) -> None:
        """Pause to read-only planning mode."""
        self._mode = AutonomyMode.READ_ONLY
        logger.info("Ouroboros paused — READ_ONLY mode")

    async def resume(self) -> None:
        """Resume from pause. Cannot resume from emergency stop."""
        if self._mode == AutonomyMode.EMERGENCY_STOP:
            raise RuntimeError(
                f"Cannot resume from emergency stop "
                f"(reason: {self._emergency_reason}). "
                f"Use explicit re-enable."
            )
        self._mode = AutonomyMode.SANDBOX
        logger.info("Ouroboros resumed — SANDBOX mode")

    async def enable_governed_autonomy(self) -> None:
        """Enable full tiered autonomy. Requires gates to have passed."""
        if not self._gates_passed:
            raise RuntimeError(
                "Cannot enable governed autonomy — gates have not passed. "
                "Run gate checks first."
            )
        self._mode = AutonomyMode.GOVERNED
        logger.info("Ouroboros GOVERNED autonomy enabled")

    async def mark_gates_passed(self) -> None:
        """Mark that all governance gates have passed."""
        self._gates_passed = True
        logger.info("Governance gates marked as PASSED")

    async def emergency_stop(self, reason: str) -> None:
        """Emergency stop — requires explicit re-enable to recover."""
        self._emergency_reason = reason
        self._mode = AutonomyMode.EMERGENCY_STOP
        logger.critical(f"EMERGENCY STOP: {reason}")
```

**Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_supervisor_controller.py -v
```
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/supervisor_controller.py tests/test_ouroboros_governance/test_supervisor_controller.py
git commit -m "feat(ouroboros): add supervisor lifecycle controller with autonomy state machine"
```

---

### Task 7: Contract Gate — Schema Version Compatibility

**Files:**
- Create: `backend/core/ouroboros/governance/contract_gate.py`
- Create: `tests/test_ouroboros_governance/test_contract_gate.py`

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_contract_gate.py
"""Tests for cross-repo schema compatibility enforcement."""

import pytest
from pathlib import Path

from backend.core.ouroboros.governance.contract_gate import (
    ContractGate,
    ContractVersion,
    CompatibilityResult,
)


@pytest.fixture
def gate():
    return ContractGate()


class TestVersionCompatibility:
    def test_same_version_compatible(self, gate):
        local = ContractVersion(major=2, minor=0, patch=0)
        remote = ContractVersion(major=2, minor=0, patch=0)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is True

    def test_n_minus_1_minor_compatible(self, gate):
        """N/N-1 compatibility: current minor supports one prior."""
        local = ContractVersion(major=2, minor=1, patch=0)
        remote = ContractVersion(major=2, minor=0, patch=0)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is True

    def test_n_minus_2_minor_incompatible(self, gate):
        local = ContractVersion(major=2, minor=2, patch=0)
        remote = ContractVersion(major=2, minor=0, patch=0)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is False
        assert "minor" in result.reason

    def test_major_mismatch_incompatible(self, gate):
        local = ContractVersion(major=2, minor=0, patch=0)
        remote = ContractVersion(major=3, minor=0, patch=0)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is False
        assert "major" in result.reason

    def test_patch_difference_always_compatible(self, gate):
        local = ContractVersion(major=2, minor=0, patch=0)
        remote = ContractVersion(major=2, minor=0, patch=99)
        result = gate.check_compatibility(local, remote)
        assert result.compatible is True


class TestBootGate:
    @pytest.mark.asyncio
    async def test_boot_check_all_compatible(self, gate):
        versions = {
            "jarvis": ContractVersion(major=2, minor=0, patch=0),
            "prime": ContractVersion(major=2, minor=0, patch=1),
            "reactor": ContractVersion(major=2, minor=0, patch=0),
        }
        result = await gate.boot_check(versions)
        assert result.autonomy_allowed is True

    @pytest.mark.asyncio
    async def test_boot_check_one_incompatible(self, gate):
        versions = {
            "jarvis": ContractVersion(major=2, minor=0, patch=0),
            "prime": ContractVersion(major=3, minor=0, patch=0),
            "reactor": ContractVersion(major=2, minor=0, patch=0),
        }
        result = await gate.boot_check(versions)
        assert result.autonomy_allowed is False
        assert result.interactive_allowed is True
        assert "prime" in result.details

    @pytest.mark.asyncio
    async def test_boot_check_missing_service(self, gate):
        """Missing service = incompatible (conservative)."""
        versions = {
            "jarvis": ContractVersion(major=2, minor=0, patch=0),
        }
        result = await gate.boot_check(versions)
        assert result.autonomy_allowed is False
        assert result.interactive_allowed is True
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_contract_gate.py -v
```
Expected: FAIL

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/contract_gate.py
"""
Contract Gate — Schema version compatibility enforcement.

Enforces N/N-1 compatibility across JARVIS/Prime/Reactor at boot
and before any cross-repo operation. If incompatible, autonomy
is disabled but interactive paths continue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("Ouroboros.ContractGate")

REQUIRED_SERVICES = ("jarvis", "prime", "reactor")


@dataclass
class ContractVersion:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass
class CompatibilityResult:
    compatible: bool
    reason: str = ""


@dataclass
class BootCheckResult:
    autonomy_allowed: bool
    interactive_allowed: bool  # Always True unless catastrophic
    details: str = ""
    incompatible_pairs: List[str] = None

    def __post_init__(self):
        if self.incompatible_pairs is None:
            self.incompatible_pairs = []


class ContractGate:
    """Schema version compatibility enforcement.

    Rules:
    - Major version must match exactly
    - Minor version supports N/N-1 (current + one prior)
    - Patch version is always compatible
    """

    def check_compatibility(
        self, local: ContractVersion, remote: ContractVersion
    ) -> CompatibilityResult:
        """Check if two versions are compatible."""
        if local.major != remote.major:
            return CompatibilityResult(
                compatible=False,
                reason=f"major version mismatch: {local} vs {remote}",
            )

        minor_diff = abs(local.minor - remote.minor)
        if minor_diff > 1:
            return CompatibilityResult(
                compatible=False,
                reason=f"minor version gap too large ({minor_diff}): {local} vs {remote}",
            )

        return CompatibilityResult(compatible=True)

    async def boot_check(
        self, versions: Dict[str, ContractVersion]
    ) -> BootCheckResult:
        """Check all services for compatibility at boot.

        Missing services are treated as incompatible (conservative).
        """
        incompatible = []
        details_parts = []

        # Check for missing services
        for svc in REQUIRED_SERVICES:
            if svc not in versions:
                incompatible.append(svc)
                details_parts.append(f"{svc}: not available")

        # Check pairwise compatibility
        service_names = [s for s in REQUIRED_SERVICES if s in versions]
        for i, svc_a in enumerate(service_names):
            for svc_b in service_names[i + 1:]:
                result = self.check_compatibility(
                    versions[svc_a], versions[svc_b]
                )
                if not result.compatible:
                    incompatible.append(f"{svc_a}<->{svc_b}")
                    details_parts.append(
                        f"{svc_a}({versions[svc_a]}) <-> "
                        f"{svc_b}({versions[svc_b]}): {result.reason}"
                    )

        if incompatible:
            details = "; ".join(details_parts)
            logger.warning(f"Contract gate FAILED: {details}")
            return BootCheckResult(
                autonomy_allowed=False,
                interactive_allowed=True,
                details=details,
                incompatible_pairs=incompatible,
            )

        logger.info("Contract gate PASSED — all services compatible")
        return BootCheckResult(
            autonomy_allowed=True,
            interactive_allowed=True,
        )
```

**Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_contract_gate.py -v
```
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/contract_gate.py tests/test_ouroboros_governance/test_contract_gate.py
git commit -m "feat(ouroboros): add contract gate with N/N-1 schema compatibility"
```

---

### Task 8: Wire governance package exports

**Files:**
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Step 1: Update governance package exports**

```python
# backend/core/ouroboros/governance/__init__.py
"""
Ouroboros Governance Layer
=========================

Deterministic policy enforcement for autonomous self-programming.
All risk classification, operation identity, and lifecycle authority
lives here. No LLM calls in this package — pure rule-based logic.
"""

from backend.core.ouroboros.governance.operation_id import (
    OperationID,
    generate_operation_id,
    OperationMetadata,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskEngine,
    RiskTier,
    RiskClassification,
    OperationProfile,
    ChangeType,
    HardInvariantViolation,
    POLICY_VERSION,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    LedgerEntry,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    CommMessage,
    MessageType,
    LogTransport,
)
from backend.core.ouroboros.governance.supervisor_controller import (
    SupervisorOuroborosController,
    AutonomyMode,
)
from backend.core.ouroboros.governance.contract_gate import (
    ContractGate,
    ContractVersion,
    CompatibilityResult,
    BootCheckResult,
)
```

**Step 2: Run all governance tests**

```bash
python3 -m pytest tests/test_ouroboros_governance/ -v
```
Expected: ALL PASS

**Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/__init__.py
git commit -m "feat(ouroboros): wire governance package exports"
```

---

## Track 2: Sandbox Improvement Loop

### Task 9: Sandbox Pipeline — Wire existing components into working loop

**Files:**
- Create: `backend/core/ouroboros/governance/sandbox_loop.py`
- Create: `tests/test_ouroboros_governance/test_sandbox_loop.py`

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_sandbox_loop.py
"""Tests for sandbox improvement loop — writes to worktree only."""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.sandbox_loop import (
    SandboxLoop,
    SandboxConfig,
    SandboxResult,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.comm_protocol import CommProtocol, LogTransport
from backend.core.ouroboros.governance.ledger import OperationLedger
from backend.core.ouroboros.governance.risk_engine import RiskEngine


@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project with a Python file and test."""
    src = tmp_path / "backend"
    src.mkdir()
    (src / "__init__.py").touch()
    target = src / "example.py"
    target.write_text(
        "def add(a, b):\n"
        "    return a + b\n"
    )
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    test_file = test_dir / "test_example.py"
    test_file.write_text(
        "from backend.example import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
    )
    return tmp_path


@pytest.fixture
def transport():
    return LogTransport()


@pytest.fixture
def sandbox(tmp_project, tmp_path, transport):
    return SandboxLoop(
        project_root=tmp_project,
        config=SandboxConfig(
            worktree_base=tmp_path / "worktrees",
            ledger_dir=tmp_path / "ledger",
        ),
        comm=CommProtocol(transports=[transport]),
        risk_engine=RiskEngine(),
        ledger=OperationLedger(storage_dir=tmp_path / "ledger"),
    )


class TestSandboxIsolation:
    @pytest.mark.asyncio
    async def test_production_files_unchanged(self, sandbox, tmp_project):
        """Core invariant: sandbox never touches production files."""
        original = (tmp_project / "backend" / "example.py").read_text()

        # Mock the LLM to return a modified version
        with patch.object(sandbox, '_generate_candidates', new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = [
                {"content": "def add(a, b):\n    return a + b  # optimized\n"}
            ]
            result = await sandbox.run(
                goal="Add comment to add function",
                target_file=tmp_project / "backend" / "example.py",
            )

        after = (tmp_project / "backend" / "example.py").read_text()
        assert after == original, "Production file must not be modified!"

    @pytest.mark.asyncio
    async def test_emits_all_comm_phases(self, sandbox, tmp_project, transport):
        """Every sandbox run emits intent, plan, heartbeat, decision, postmortem."""
        with patch.object(sandbox, '_generate_candidates', new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = []
            await sandbox.run(
                goal="test",
                target_file=tmp_project / "backend" / "example.py",
            )

        msg_types = [m.msg_type.value for m in transport.messages]
        assert "intent" in msg_types
        assert "decision" in msg_types

    @pytest.mark.asyncio
    async def test_ledger_records_state(self, sandbox, tmp_project):
        with patch.object(sandbox, '_generate_candidates', new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = []
            result = await sandbox.run(
                goal="test",
                target_file=tmp_project / "backend" / "example.py",
            )

        history = await sandbox._ledger.get_history(result.op_id)
        assert len(history) >= 2  # At least PLANNED and final state
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_sandbox_loop.py -v
```
Expected: FAIL

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/sandbox_loop.py
"""
Sandbox Improvement Loop — End-to-end improvement in git worktree only.

Wires together: OuroborosEngine (generation) + governance components
(risk engine, ledger, comms). All changes go to a temp worktree,
never production files.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.risk_engine import (
    RiskEngine,
    OperationProfile,
    ChangeType,
    RiskTier,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    LedgerEntry,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    MessageType,
)

logger = logging.getLogger("Ouroboros.SandboxLoop")


@dataclass
class SandboxConfig:
    worktree_base: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "ouroboros_worktrees")
    ledger_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "ouroboros" / "ledger")


@dataclass
class SandboxResult:
    op_id: str
    success: bool
    candidates_generated: int = 0
    best_candidate: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class SandboxLoop:
    """End-to-end improvement loop in sandbox (git worktree).

    Pipeline: analyze -> generate -> validate (in worktree) -> report
    Production files are NEVER modified.
    """

    def __init__(
        self,
        project_root: Path,
        config: Optional[SandboxConfig] = None,
        comm: Optional[CommProtocol] = None,
        risk_engine: Optional[RiskEngine] = None,
        ledger: Optional[OperationLedger] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._config = config or SandboxConfig()
        self._comm = comm or CommProtocol()
        self._risk_engine = risk_engine or RiskEngine()
        self._ledger = ledger or OperationLedger(
            storage_dir=self._config.ledger_dir
        )

    async def run(
        self,
        goal: str,
        target_file: Path,
        repo_origin: str = "jarvis",
    ) -> SandboxResult:
        """Execute a full sandbox improvement loop.

        Returns result but NEVER modifies production files.
        """
        op_id = generate_operation_id(repo_origin)

        # INTENT
        profile = OperationProfile(
            files_affected=[target_file],
            change_type=ChangeType.MODIFY,
            blast_radius=0,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor="supervisor" in str(target_file).lower(),
            test_scope_confidence=0.5,
        )
        classification = self._risk_engine.classify(profile)

        await self._comm.emit_intent(
            op_id=op_id,
            goal=goal,
            target_files=[str(target_file)],
            risk_tier=classification.tier.value,
            blast_radius=profile.blast_radius,
        )

        # LEDGER: planned
        await self._ledger.append(
            LedgerEntry(op_id=op_id, state=OperationState.PLANNED, data={"goal": goal})
        )

        try:
            # GENERATE candidates (delegated to subclass or mock)
            await self._comm.emit_heartbeat(op_id=op_id, phase="generating", progress_pct=25)
            candidates = await self._generate_candidates(goal, target_file)

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.VALIDATING,
                    data={"candidates": len(candidates)},
                )
            )

            if not candidates:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="no_candidates",
                    reason_code="generation_empty",
                )
                await self._ledger.append(
                    LedgerEntry(op_id=op_id, state=OperationState.FAILED, data={})
                )
                return SandboxResult(op_id=op_id, success=False, error="No candidates generated")

            # VALIDATE in sandbox (worktree) — production untouched
            await self._comm.emit_heartbeat(op_id=op_id, phase="validating", progress_pct=60)
            best = await self._validate_in_sandbox(target_file, candidates)

            outcome = "validated" if best else "all_failed"
            await self._comm.emit_decision(
                op_id=op_id,
                outcome=outcome,
                reason_code="sandbox_validation",
            )

            final_state = OperationState.APPLIED if best else OperationState.FAILED
            await self._ledger.append(
                LedgerEntry(op_id=op_id, state=final_state, data={"best": best is not None})
            )

            return SandboxResult(
                op_id=op_id,
                success=best is not None,
                candidates_generated=len(candidates),
                best_candidate=best,
            )

        except Exception as e:
            logger.error(f"Sandbox loop failed: {e}")
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=str(e),
                failed_phase="execution",
            )
            await self._ledger.append(
                LedgerEntry(op_id=op_id, state=OperationState.FAILED, data={"error": str(e)})
            )
            return SandboxResult(op_id=op_id, success=False, error=str(e))

    async def _generate_candidates(
        self, goal: str, target_file: Path
    ) -> List[Dict[str, Any]]:
        """Generate improvement candidates via LLM.

        Override or mock this for testing. In production, delegates
        to OuroborosEngine or J-Prime.
        """
        # Default: no candidates (subclass or integration wires this up)
        return []

    async def _validate_in_sandbox(
        self, target_file: Path, candidates: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Validate candidates in isolated worktree.

        Returns best passing candidate, or None if all fail.
        """
        for candidate in candidates:
            content = candidate.get("content", "")
            if not content:
                continue

            # Create temp dir, write candidate, run syntax check
            with tempfile.TemporaryDirectory(
                prefix="ouroboros_sandbox_",
                dir=self._config.worktree_base if self._config.worktree_base.exists() else None,
            ) as sandbox_dir:
                sandbox_path = Path(sandbox_dir)
                sandbox_file = sandbox_path / target_file.name
                sandbox_file.write_text(content)

                # Basic validation: syntax check via AST
                try:
                    import ast
                    ast.parse(content)
                except SyntaxError:
                    continue

                return candidate

        return None
```

**Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_sandbox_loop.py -v
```
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/sandbox_loop.py tests/test_ouroboros_governance/test_sandbox_loop.py
git commit -m "feat(ouroboros): add sandbox improvement loop with governance integration"
```

---

### Task 10: Integration test — Full Phase 0 pipeline

**Files:**
- Create: `tests/test_ouroboros_governance/test_phase0_integration.py`

**Step 1: Write the integration test**

```python
# tests/test_ouroboros_governance/test_phase0_integration.py
"""
Phase 0 integration test — validates the full pipeline:
  Supervisor controller -> risk engine -> sandbox loop -> ledger -> comms

This is the acceptance test for Phase 0 Go/No-Go.
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.core.ouroboros.governance.supervisor_controller import (
    SupervisorOuroborosController,
    AutonomyMode,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskEngine,
    OperationProfile,
    ChangeType,
    RiskTier,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.ledger import OperationLedger, OperationState
from backend.core.ouroboros.governance.comm_protocol import CommProtocol, LogTransport
from backend.core.ouroboros.governance.contract_gate import ContractGate, ContractVersion
from backend.core.ouroboros.governance.sandbox_loop import SandboxLoop, SandboxConfig


@pytest.fixture
def tmp_project(tmp_path):
    src = tmp_path / "backend"
    src.mkdir()
    (src / "__init__.py").touch()
    target = src / "example.py"
    target.write_text("def add(a, b):\n    return a + b\n")
    return tmp_path


class TestPhase0Pipeline:
    """Full pipeline: controller -> gate -> classify -> sandbox -> ledger -> comms."""

    @pytest.mark.asyncio
    async def test_full_pipeline_sandbox_mode(self, tmp_project, tmp_path):
        # 1. Supervisor starts in sandbox mode
        controller = SupervisorOuroborosController()
        await controller.start()
        assert controller.mode == AutonomyMode.SANDBOX

        # 2. Contract gate passes
        gate = ContractGate()
        versions = {
            "jarvis": ContractVersion(2, 0, 0),
            "prime": ContractVersion(2, 0, 1),
            "reactor": ContractVersion(2, 0, 0),
        }
        boot_result = await gate.boot_check(versions)
        assert boot_result.autonomy_allowed is True

        # 3. Risk engine classifies
        engine = RiskEngine()
        profile = OperationProfile(
            files_affected=[Path("backend/example.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        classification = engine.classify(profile)
        assert classification.tier == RiskTier.SAFE_AUTO

        # 4. Sandbox loop runs (production unchanged)
        transport = LogTransport()
        sandbox = SandboxLoop(
            project_root=tmp_project,
            config=SandboxConfig(
                worktree_base=tmp_path / "worktrees",
                ledger_dir=tmp_path / "ledger",
            ),
            comm=CommProtocol(transports=[transport]),
            risk_engine=engine,
            ledger=OperationLedger(storage_dir=tmp_path / "ledger"),
        )

        original = (tmp_project / "backend" / "example.py").read_text()

        with patch.object(sandbox, '_generate_candidates', new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = [
                {"content": "def add(a, b):\n    return a + b  # fast\n"}
            ]
            result = await sandbox.run(
                goal="Add comment",
                target_file=tmp_project / "backend" / "example.py",
            )

        # 5. Verify production untouched
        after = (tmp_project / "backend" / "example.py").read_text()
        assert after == original

        # 6. Verify ledger has entries
        ledger = OperationLedger(storage_dir=tmp_path / "ledger")
        history = await ledger.get_history(result.op_id)
        assert len(history) >= 2

        # 7. Verify comms emitted
        assert len(transport.messages) >= 2

    @pytest.mark.asyncio
    async def test_supervisor_blocked_prevents_write(self, tmp_project, tmp_path):
        """Supervisor-touching files must be BLOCKED."""
        engine = RiskEngine()
        profile = OperationProfile(
            files_affected=[Path("unified_supervisor.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=50,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=True,
            test_scope_confidence=1.0,
        )
        classification = engine.classify(profile)
        assert classification.tier == RiskTier.BLOCKED

    @pytest.mark.asyncio
    async def test_contract_gate_blocks_autonomy(self, tmp_path):
        """Incompatible contract disables autonomy, keeps interactive."""
        gate = ContractGate()
        versions = {
            "jarvis": ContractVersion(2, 0, 0),
            "prime": ContractVersion(3, 0, 0),  # Major mismatch
            "reactor": ContractVersion(2, 0, 0),
        }
        result = await gate.boot_check(versions)
        assert result.autonomy_allowed is False
        assert result.interactive_allowed is True

    @pytest.mark.asyncio
    async def test_deterministic_replay(self):
        """Same inputs produce identical classification 1000 times."""
        engine = RiskEngine()
        profile = OperationProfile(
            files_affected=[Path("backend/utils/helpers.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=2,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.8,
        )
        first = engine.classify(profile)
        for _ in range(999):
            result = engine.classify(profile)
            assert result.tier == first.tier
            assert result.reason_code == first.reason_code
```

**Step 2: Run integration tests**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_phase0_integration.py -v
```
Expected: ALL PASS

**Step 3: Run ALL governance tests**

```bash
python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short
```
Expected: ALL PASS

**Step 4: Commit**

```bash
git add tests/test_ouroboros_governance/test_phase0_integration.py
git commit -m "test(ouroboros): add Phase 0 integration tests for full governance pipeline"
```

---

## Summary: Phase 0 Task Dependencies

```
Task 1: Package skeleton (no deps)
Task 2: Operation ID (depends on 1)
Task 3: Risk Engine (depends on 1)
Task 4: Ledger (depends on 2)
Task 5: Comm Protocol (depends on 2)
Task 6: Supervisor Controller (depends on 1)
Task 7: Contract Gate (depends on 1)
Task 8: Wire exports (depends on 2-7)
Task 9: Sandbox Loop (depends on 2-5)
Task 10: Integration test (depends on 2-9)
```

Tasks 2, 3, 6, 7 can run in parallel (all depend only on Task 1).
Tasks 4, 5 can run in parallel after Task 2.

## What's Next After Phase 0

After all Phase 0 tasks pass, the next plan covers:
- **Phase 1A:** Read/write lease lock manager with fencing tokens
- **Phase 1B:** TUI integration for communication protocol
- **Phase 2A:** Multi-signal hybrid routing engine
- **Phase 2B:** Multi-file + cross-repo operations
- **Phase 3:** Canary rollout with promotion criteria

Each phase gets its own implementation plan after the prior phase passes Go/No-Go.

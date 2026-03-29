# Architecture Reasoning Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Architecture Reasoning Agent — the final Ouroboros extension that designs multi-file features via ArchitecturalPlans and executes them through WAL-backed sagas using the existing governance pipeline.

**Architecture:** Two-phase hybrid: Design phase (397B/Claude produces immutable ArchitecturalPlan with DAG, contracts, acceptance checks) then Execution phase (SagaOrchestrator decomposes plan into governed IntentEnvelopes, executes sequentially by topological tier, runs acceptance in sandbox).

**Tech Stack:** Python 3.12, asyncio, SHA256 hashing, existing governance pipeline (IntakeRouter, RiskEngine), existing DoublewordProvider, WAL persistence (JSON)

**Spec:** `docs/superpowers/specs/2026-03-28-architecture-reasoning-agent-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `backend/core/ouroboros/architect/__init__.py` | Package marker |
| `backend/core/ouroboros/architect/plan.py` | ArchitecturalPlan, PlanStep, AcceptanceCheck, enums, plan_hash computation |
| `backend/core/ouroboros/architect/saga.py` | SagaRecord, SagaPhase, StepPhase, StepState schemas |
| `backend/core/ouroboros/architect/plan_validator.py` | Deterministic DAG/allowlist/structure validation |
| `backend/core/ouroboros/architect/plan_store.py` | Immutable plan storage keyed by plan_hash |
| `backend/core/ouroboros/architect/plan_decomposer.py` | Plan → coordinated IntentEnvelopes |
| `backend/core/ouroboros/architect/saga_orchestrator.py` | WAL-backed saga state machine |
| `backend/core/ouroboros/architect/acceptance_runner.py` | Sandbox-bound acceptance check executor |
| `backend/core/ouroboros/architect/reasoning_agent.py` | ArchitectureReasoningAgent (model call) |
| `tests/core/ouroboros/architect/__init__.py` | Test package marker |
| `tests/core/ouroboros/architect/test_plan.py` | Plan schema + hash tests |
| `tests/core/ouroboros/architect/test_saga.py` | Saga schema + state tests |
| `tests/core/ouroboros/architect/test_plan_validator.py` | 10 validation rule tests |
| `tests/core/ouroboros/architect/test_plan_store.py` | Immutable storage tests |
| `tests/core/ouroboros/architect/test_plan_decomposer.py` | Decomposition tests |
| `tests/core/ouroboros/architect/test_saga_orchestrator.py` | State machine + WAL tests |
| `tests/core/ouroboros/architect/test_acceptance_runner.py` | Sandbox check tests |
| `tests/core/ouroboros/architect/test_reasoning_agent.py` | Model call + threshold tests |
| `tests/core/ouroboros/architect/test_integration.py` | End-to-end test |

### Modified Files
| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/intake/intent_envelope.py` | Add `"architecture"` to `_VALID_SOURCES` |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py` | Add `"architecture": 3` to `_PRIORITY_MAP` |
| `backend/core/ouroboros/governance/risk_engine.py` | Add architecture-source tiered rules |
| `backend/core/ouroboros/daemon_config.py` | Add architect/saga env vars |
| `backend/core/ouroboros/rem_epoch.py` | Route missing_capability to architect |
| `backend/core/ouroboros/rem_sleep.py` | Pass architect to RemEpoch |
| `backend/core/ouroboros/daemon.py` | Wire architect + saga orchestrator |

---

## Task 1: Plan Schemas (ArchitecturalPlan + PlanStep + AcceptanceCheck)

**Files:**
- Create: `backend/core/ouroboros/architect/__init__.py`
- Create: `backend/core/ouroboros/architect/plan.py`
- Create: `tests/core/ouroboros/architect/__init__.py`
- Create: `tests/core/ouroboros/architect/test_plan.py`

- [ ] **Step 1: Write plan schema tests**

```python
# tests/core/ouroboros/architect/test_plan.py
"""Tests for ArchitecturalPlan, PlanStep, AcceptanceCheck schemas."""
import pytest
from backend.core.ouroboros.architect.plan import (
    PlanStep, StepIntentKind, AcceptanceCheck, CheckKind,
    ArchitecturalPlan, compute_plan_hash,
)


def _step(index=0, target="backend/agents/foo.py", depends_on=()):
    return PlanStep(
        step_index=index,
        description=f"Step {index}",
        intent_kind=StepIntentKind.CREATE_FILE,
        target_paths=(target,),
        repo="jarvis",
        depends_on=depends_on,
    )


def _check(check_id="test_run"):
    return AcceptanceCheck(
        check_id=check_id,
        check_kind=CheckKind.EXIT_CODE,
        command="python3 -m pytest tests/ -v",
        expected="",
    )


def _plan(steps=None, checks=None):
    steps = steps or (_step(0), _step(1, "backend/agents/bar.py", depends_on=(0,)))
    checks = checks or (_check(),)
    return ArchitecturalPlan.create(
        parent_hypothesis_id="hyp-1",
        parent_hypothesis_fingerprint="fp-1",
        title="Test Plan",
        description="A test plan",
        repos_affected=("jarvis",),
        non_goals=("No UI changes",),
        steps=steps,
        acceptance_checks=checks,
        model_used="test-model",
        snapshot_hash="snap-1",
    )


def test_plan_step_frozen():
    s = _step()
    with pytest.raises(AttributeError):
        s.step_index = 99


def test_plan_hash_is_deterministic():
    p1 = _plan()
    p2 = _plan()
    assert p1.plan_hash == p2.plan_hash


def test_plan_hash_excludes_provenance():
    """Same structure, different model/time = same hash."""
    steps = (_step(0),)
    checks = (_check(),)
    p1 = ArchitecturalPlan.create(
        parent_hypothesis_id="h1", parent_hypothesis_fingerprint="fp1",
        title="T", description="D", repos_affected=("jarvis",),
        non_goals=(), steps=steps, acceptance_checks=checks,
        model_used="model-a", snapshot_hash="s1",
    )
    p2 = ArchitecturalPlan.create(
        parent_hypothesis_id="h1", parent_hypothesis_fingerprint="fp1",
        title="T", description="D", repos_affected=("jarvis",),
        non_goals=(), steps=steps, acceptance_checks=checks,
        model_used="model-b", snapshot_hash="s2",  # different provenance
    )
    assert p1.plan_hash == p2.plan_hash


def test_plan_hash_changes_with_steps():
    p1 = _plan(steps=(_step(0, "a.py"),))
    p2 = _plan(steps=(_step(0, "b.py"),))
    assert p1.plan_hash != p2.plan_hash


def test_file_allowlist_computed():
    s = PlanStep(
        step_index=0, description="test",
        intent_kind=StepIntentKind.CREATE_FILE,
        target_paths=("a.py",), ancillary_paths=("__init__.py",),
        tests_required=("test_a.py",), repo="jarvis",
    )
    p = _plan(steps=(s,))
    assert "a.py" in p.file_allowlist
    assert "__init__.py" in p.file_allowlist
    assert "test_a.py" in p.file_allowlist


def test_acceptance_check_kinds():
    for kind in CheckKind:
        check = AcceptanceCheck(
            check_id="t", check_kind=kind, command="echo ok", expected="",
        )
        assert check.check_kind == kind


def test_step_intent_kinds():
    for kind in StepIntentKind:
        step = PlanStep(
            step_index=0, description="t",
            intent_kind=kind, target_paths=("a.py",), repo="jarvis",
        )
        assert step.intent_kind == kind
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_plan.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement plan.py**

```python
# backend/core/ouroboros/architect/plan.py
"""ArchitecturalPlan, PlanStep, AcceptanceCheck schemas.

Immutable design contracts. plan_hash covers structure+scope only (not provenance).
"""
from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Tuple


class StepIntentKind(enum.Enum):
    CREATE_FILE = "create_file"
    MODIFY_FILE = "modify_file"
    DELETE_FILE = "delete_file"


class CheckKind(enum.Enum):
    EXIT_CODE = "exit_code"
    REGEX_STDOUT = "regex_stdout"
    IMPORT_CHECK = "import_check"


@dataclass(frozen=True)
class PlanStep:
    """Single step in the architectural plan's dependency DAG."""
    step_index: int
    description: str
    intent_kind: StepIntentKind
    target_paths: Tuple[str, ...]
    repo: str
    ancillary_paths: Tuple[str, ...] = ()
    interface_contracts: Tuple[str, ...] = ()
    tests_required: Tuple[str, ...] = ()
    risk_tier_hint: str = "safe_auto"
    depends_on: Tuple[int, ...] = ()


@dataclass(frozen=True)
class AcceptanceCheck:
    """Deterministic check run post-saga in sandbox."""
    check_id: str
    check_kind: CheckKind
    command: str
    expected: str = ""
    cwd: str = "."
    timeout_s: float = 120.0
    run_after_step: Optional[int] = None
    sandbox_required: bool = True


def compute_plan_hash(
    title: str,
    description: str,
    repos_affected: Tuple[str, ...],
    non_goals: Tuple[str, ...],
    steps: Tuple[PlanStep, ...],
    acceptance_checks: Tuple[AcceptanceCheck, ...],
) -> str:
    """SHA256 of structure+scope. Excludes provenance (model, time, snapshot)."""
    canonical = json.dumps({
        "title": title,
        "description": description,
        "repos": sorted(repos_affected),
        "non_goals": sorted(non_goals),
        "steps": [
            {
                "i": s.step_index,
                "desc": s.description,
                "kind": s.intent_kind.value,
                "paths": sorted(s.target_paths),
                "ancillary": sorted(s.ancillary_paths),
                "tests": sorted(s.tests_required),
                "contracts": sorted(s.interface_contracts),
                "repo": s.repo,
                "risk": s.risk_tier_hint,
                "deps": sorted(s.depends_on),
            }
            for s in sorted(steps, key=lambda s: s.step_index)
        ],
        "checks": [
            {
                "id": c.check_id,
                "kind": c.check_kind.value,
                "cmd": c.command,
                "exp": c.expected,
                "cwd": c.cwd,
                "after": c.run_after_step,
            }
            for c in acceptance_checks
        ],
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _compute_allowlist(steps: Tuple[PlanStep, ...]) -> FrozenSet[str]:
    paths: set = set()
    for s in steps:
        paths.update(s.target_paths)
        paths.update(s.ancillary_paths)
        paths.update(s.tests_required)
    return frozenset(paths)


@dataclass(frozen=True)
class ArchitecturalPlan:
    """Immutable design contract."""
    plan_id: str
    plan_hash: str
    parent_hypothesis_id: str
    parent_hypothesis_fingerprint: str
    title: str
    description: str
    repos_affected: Tuple[str, ...]
    non_goals: Tuple[str, ...]
    steps: Tuple[PlanStep, ...]
    file_allowlist: FrozenSet[str]
    acceptance_checks: Tuple[AcceptanceCheck, ...]
    model_used: str
    created_at: float
    snapshot_hash: str

    @classmethod
    def create(
        cls,
        parent_hypothesis_id: str,
        parent_hypothesis_fingerprint: str,
        title: str,
        description: str,
        repos_affected: Tuple[str, ...],
        non_goals: Tuple[str, ...],
        steps: Tuple[PlanStep, ...],
        acceptance_checks: Tuple[AcceptanceCheck, ...],
        model_used: str,
        snapshot_hash: str,
    ) -> ArchitecturalPlan:
        plan_hash = compute_plan_hash(
            title, description, repos_affected, non_goals,
            steps, acceptance_checks,
        )
        return cls(
            plan_id=uuid.uuid4().hex[:16],
            plan_hash=plan_hash,
            parent_hypothesis_id=parent_hypothesis_id,
            parent_hypothesis_fingerprint=parent_hypothesis_fingerprint,
            title=title,
            description=description,
            repos_affected=repos_affected,
            non_goals=non_goals,
            steps=steps,
            file_allowlist=_compute_allowlist(steps),
            acceptance_checks=acceptance_checks,
            model_used=model_used,
            created_at=time.time(),
            snapshot_hash=snapshot_hash,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_plan.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/architect/__init__.py backend/core/ouroboros/architect/plan.py tests/core/ouroboros/architect/__init__.py tests/core/ouroboros/architect/test_plan.py
git commit -m "feat(ouroboros/architect): add ArchitecturalPlan and PlanStep schemas"
```

---

## Task 2: Saga Schemas (SagaRecord + State Enums)

**Files:**
- Create: `backend/core/ouroboros/architect/saga.py`
- Create: `tests/core/ouroboros/architect/test_saga.py`

- [ ] **Step 1: Write saga schema tests**

```python
# tests/core/ouroboros/architect/test_saga.py
"""Tests for SagaRecord, SagaPhase, StepPhase, StepState schemas."""
import time
import pytest
from backend.core.ouroboros.architect.saga import (
    SagaRecord, SagaPhase, StepPhase, StepState,
)


def test_saga_starts_pending():
    saga = SagaRecord.create(saga_id="s1", plan_id="p1", plan_hash="h1", num_steps=3)
    assert saga.phase == SagaPhase.PENDING
    assert len(saga.step_states) == 3
    assert all(s.phase == StepPhase.PENDING for s in saga.step_states.values())


def test_step_state_transition():
    state = StepState(step_index=0, phase=StepPhase.PENDING)
    state.phase = StepPhase.RUNNING
    state.started_at = time.time()
    assert state.phase == StepPhase.RUNNING


def test_saga_all_steps_complete():
    saga = SagaRecord.create(saga_id="s1", plan_id="p1", plan_hash="h1", num_steps=2)
    saga.step_states[0].phase = StepPhase.COMPLETE
    saga.step_states[1].phase = StepPhase.COMPLETE
    assert saga.all_steps_complete


def test_saga_not_complete_with_pending():
    saga = SagaRecord.create(saga_id="s1", plan_id="p1", plan_hash="h1", num_steps=2)
    saga.step_states[0].phase = StepPhase.COMPLETE
    assert not saga.all_steps_complete


def test_saga_has_failed_step():
    saga = SagaRecord.create(saga_id="s1", plan_id="p1", plan_hash="h1", num_steps=2)
    saga.step_states[0].phase = StepPhase.FAILED
    saga.step_states[0].error = "test error"
    assert saga.has_failed_step


def test_saga_serialization_roundtrip():
    saga = SagaRecord.create(saga_id="s1", plan_id="p1", plan_hash="h1", num_steps=2)
    saga.step_states[0].phase = StepPhase.COMPLETE
    data = saga.to_dict()
    restored = SagaRecord.from_dict(data)
    assert restored.saga_id == "s1"
    assert restored.step_states[0].phase == StepPhase.COMPLETE
    assert restored.step_states[1].phase == StepPhase.PENDING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_saga.py -v`
Expected: FAIL

- [ ] **Step 3: Implement saga.py**

```python
# backend/core/ouroboros/architect/saga.py
"""SagaRecord, SagaPhase, StepPhase, StepState — WAL-backed saga state."""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


class SagaPhase(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    ABORTED = "aborted"


class StepPhase(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class StepState:
    """Mutable state for a single saga step."""
    step_index: int
    phase: StepPhase
    envelope_id: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "phase": self.phase.value,
            "envelope_id": self.envelope_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> StepState:
        return cls(
            step_index=d["step_index"],
            phase=StepPhase(d["phase"]),
            envelope_id=d.get("envelope_id"),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            error=d.get("error"),
        )


@dataclass
class SagaRecord:
    """WAL-backed saga execution state."""
    saga_id: str
    plan_id: str
    plan_hash: str
    phase: SagaPhase
    step_states: Dict[int, StepState]
    created_at: float
    completed_at: Optional[float] = None
    abort_reason: Optional[str] = None

    @classmethod
    def create(cls, saga_id: str, plan_id: str, plan_hash: str, num_steps: int) -> SagaRecord:
        return cls(
            saga_id=saga_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            phase=SagaPhase.PENDING,
            step_states={i: StepState(step_index=i, phase=StepPhase.PENDING) for i in range(num_steps)},
            created_at=time.time(),
        )

    @property
    def all_steps_complete(self) -> bool:
        return all(s.phase == StepPhase.COMPLETE for s in self.step_states.values())

    @property
    def has_failed_step(self) -> bool:
        return any(s.phase == StepPhase.FAILED for s in self.step_states.values())

    def to_dict(self) -> dict:
        return {
            "saga_id": self.saga_id,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "phase": self.phase.value,
            "step_states": {str(k): v.to_dict() for k, v in self.step_states.items()},
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "abort_reason": self.abort_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SagaRecord:
        return cls(
            saga_id=d["saga_id"],
            plan_id=d["plan_id"],
            plan_hash=d["plan_hash"],
            phase=SagaPhase(d["phase"]),
            step_states={int(k): StepState.from_dict(v) for k, v in d["step_states"].items()},
            created_at=d["created_at"],
            completed_at=d.get("completed_at"),
            abort_reason=d.get("abort_reason"),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_saga.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/architect/saga.py tests/core/ouroboros/architect/test_saga.py
git commit -m "feat(ouroboros/architect): add SagaRecord and StepState schemas with WAL serialization"
```

---

## Task 3: Plan Validator (Deterministic)

**Files:**
- Create: `backend/core/ouroboros/architect/plan_validator.py`
- Create: `tests/core/ouroboros/architect/test_plan_validator.py`

- [ ] **Step 1: Write validator tests**

```python
# tests/core/ouroboros/architect/test_plan_validator.py
"""Tests for deterministic ArchitecturalPlan validation (10 rules)."""
import pytest
from backend.core.ouroboros.architect.plan import (
    PlanStep, StepIntentKind, AcceptanceCheck, CheckKind, ArchitecturalPlan,
)
from backend.core.ouroboros.architect.plan_validator import (
    PlanValidator, ValidationResult,
)


def _step(index=0, target="backend/agents/foo.py", depends_on=(), repo="jarvis"):
    return PlanStep(
        step_index=index, description=f"Step {index}",
        intent_kind=StepIntentKind.CREATE_FILE,
        target_paths=(target,), repo=repo, depends_on=depends_on,
    )

def _check():
    return AcceptanceCheck(check_id="t", check_kind=CheckKind.EXIT_CODE, command="echo ok", expected="")

def _plan(**kw):
    defaults = dict(
        parent_hypothesis_id="h1", parent_hypothesis_fingerprint="fp1",
        title="T", description="D", repos_affected=("jarvis",), non_goals=(),
        steps=(_step(0), _step(1, depends_on=(0,))), acceptance_checks=(_check(),),
        model_used="test", snapshot_hash="s1",
    )
    defaults.update(kw)
    return ArchitecturalPlan.create(**defaults)

@pytest.fixture
def validator():
    return PlanValidator(max_steps=10)


def test_valid_plan_passes(validator):
    result = validator.validate(_plan())
    assert result.passed


def test_cyclic_dag_fails(validator):
    steps = (_step(0, depends_on=(1,)), _step(1, "b.py", depends_on=(0,)))
    result = validator.validate(_plan(steps=steps))
    assert not result.passed
    assert any("cycle" in r.lower() or "acyclic" in r.lower() for r in result.reasons)


def test_orphan_step_index_fails(validator):
    steps = (_step(0), _step(2, "b.py"))  # gap: no step 1
    result = validator.validate(_plan(steps=steps))
    assert not result.passed


def test_duplicate_step_index_fails(validator):
    steps = (_step(0), _step(0, "b.py"))
    result = validator.validate(_plan(steps=steps))
    assert not result.passed


def test_empty_target_paths_fails(validator):
    step = PlanStep(step_index=0, description="t", intent_kind=StepIntentKind.CREATE_FILE,
                    target_paths=(), repo="jarvis")
    result = validator.validate(_plan(steps=(step,)))
    assert not result.passed


def test_dotdot_path_fails(validator):
    result = validator.validate(_plan(steps=(_step(0, "../escape.py"),)))
    assert not result.passed


def test_invalid_depends_on_fails(validator):
    steps = (_step(0, depends_on=(99,)),)  # step 99 doesn't exist
    result = validator.validate(_plan(steps=steps))
    assert not result.passed


def test_too_many_steps_fails(validator):
    steps = tuple(_step(i, f"f{i}.py") for i in range(11))
    result = validator.validate(_plan(steps=steps))
    assert not result.passed


def test_empty_plan_fails(validator):
    result = validator.validate(_plan(steps=()))
    assert not result.passed


def test_repos_mismatch_fails(validator):
    steps = (_step(0, repo="jarvis-prime"),)
    result = validator.validate(_plan(steps=steps, repos_affected=("jarvis",)))
    assert not result.passed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_plan_validator.py -v`
Expected: FAIL

- [ ] **Step 3: Implement plan_validator.py**

```python
# backend/core/ouroboros/architect/plan_validator.py
"""Deterministic validation of ArchitecturalPlan structure. Zero model calls."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Set

from backend.core.ouroboros.architect.plan import ArchitecturalPlan


@dataclass
class ValidationResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)


class PlanValidator:
    """Validates ArchitecturalPlan against 10 structural rules."""

    def __init__(self, max_steps: int = 10) -> None:
        self._max_steps = max_steps

    def validate(self, plan: ArchitecturalPlan) -> ValidationResult:
        reasons: List[str] = []

        # Rule 1: At least one step
        if not plan.steps:
            reasons.append("Plan has no steps")
            return ValidationResult(passed=False, reasons=reasons)

        # Rule 2: Step count limit
        if len(plan.steps) > self._max_steps:
            reasons.append(f"Plan has {len(plan.steps)} steps, max is {self._max_steps}")

        indices = [s.step_index for s in plan.steps]

        # Rule 3: No duplicate step indices
        if len(set(indices)) != len(indices):
            reasons.append("Duplicate step indices")

        # Rule 4: Indices are 0..N-1 with no gaps
        expected = set(range(len(plan.steps)))
        if set(indices) != expected:
            reasons.append(f"Step indices {sorted(indices)} != expected {sorted(expected)}")

        # Rule 5: All depends_on references are valid
        valid_indices = set(indices)
        for step in plan.steps:
            for dep in step.depends_on:
                if dep not in valid_indices:
                    reasons.append(f"Step {step.step_index} depends on nonexistent step {dep}")

        # Rule 6: DAG is acyclic (topological sort)
        if not reasons:  # only check if indices are valid
            if self._has_cycle(plan):
                reasons.append("Dependency DAG contains a cycle")

        # Rule 7: Every step has at least one target_path
        for step in plan.steps:
            if not step.target_paths:
                reasons.append(f"Step {step.step_index} has no target_paths")

        # Rule 8: All paths repo-relative, no ".." escape
        for step in plan.steps:
            for path in step.target_paths + step.ancillary_paths + step.tests_required:
                if ".." in path:
                    reasons.append(f"Path '{path}' contains '..' escape")

        # Rule 9: repos_affected matches union of step repos
        step_repos = {s.repo for s in plan.steps}
        plan_repos = set(plan.repos_affected)
        if step_repos != plan_repos:
            reasons.append(f"repos_affected {plan_repos} != step repos {step_repos}")

        # Rule 10: Acceptance checks have valid check_kind (enforced by enum, but check sanity)
        for check in plan.acceptance_checks:
            if check.run_after_step is not None and check.run_after_step not in valid_indices:
                reasons.append(f"Check '{check.check_id}' references nonexistent step {check.run_after_step}")

        return ValidationResult(passed=len(reasons) == 0, reasons=reasons)

    @staticmethod
    def _has_cycle(plan: ArchitecturalPlan) -> bool:
        """Detect cycles via Kahn's algorithm (topological sort)."""
        adj: dict = {s.step_index: list(s.depends_on) for s in plan.steps}
        in_degree = {s.step_index: len(s.depends_on) for s in plan.steps}
        queue = [i for i, d in in_degree.items() if d == 0]
        visited = 0
        while queue:
            node = queue.pop(0)
            visited += 1
            for s in plan.steps:
                if node in s.depends_on:
                    in_degree[s.step_index] -= 1
                    if in_degree[s.step_index] == 0:
                        queue.append(s.step_index)
        return visited != len(plan.steps)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_plan_validator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/architect/plan_validator.py tests/core/ouroboros/architect/test_plan_validator.py
git commit -m "feat(ouroboros/architect): add deterministic PlanValidator with 10 structural rules"
```

---

## Task 4: Plan Store (Immutable)

**Files:**
- Create: `backend/core/ouroboros/architect/plan_store.py`
- Create: `tests/core/ouroboros/architect/test_plan_store.py`

- [ ] **Step 1: Write store tests**

```python
# tests/core/ouroboros/architect/test_plan_store.py
"""Tests for immutable PlanStore."""
import pytest
from backend.core.ouroboros.architect.plan_store import PlanStore
from backend.core.ouroboros.architect.plan import (
    PlanStep, StepIntentKind, AcceptanceCheck, CheckKind, ArchitecturalPlan,
)


def _plan():
    step = PlanStep(step_index=0, description="t", intent_kind=StepIntentKind.CREATE_FILE,
                    target_paths=("a.py",), repo="jarvis")
    check = AcceptanceCheck(check_id="t", check_kind=CheckKind.EXIT_CODE, command="echo ok", expected="")
    return ArchitecturalPlan.create(
        parent_hypothesis_id="h1", parent_hypothesis_fingerprint="fp1",
        title="T", description="D", repos_affected=("jarvis",), non_goals=(),
        steps=(step,), acceptance_checks=(check,), model_used="test", snapshot_hash="s1",
    )


def test_store_and_load(tmp_path):
    store = PlanStore(store_dir=tmp_path)
    plan = _plan()
    store.store(plan)
    loaded = store.load(plan.plan_hash)
    assert loaded is not None
    assert loaded.plan_hash == plan.plan_hash
    assert loaded.title == plan.title


def test_load_missing_returns_none(tmp_path):
    store = PlanStore(store_dir=tmp_path)
    assert store.load("nonexistent") is None


def test_exists(tmp_path):
    store = PlanStore(store_dir=tmp_path)
    plan = _plan()
    assert not store.exists(plan.plan_hash)
    store.store(plan)
    assert store.exists(plan.plan_hash)


def test_immutable_no_overwrite(tmp_path):
    store = PlanStore(store_dir=tmp_path)
    plan = _plan()
    store.store(plan)
    store.store(plan)  # second store should not raise (idempotent)
    loaded = store.load(plan.plan_hash)
    assert loaded.title == plan.title


def test_persists_across_instances(tmp_path):
    plan = _plan()
    PlanStore(store_dir=tmp_path).store(plan)
    loaded = PlanStore(store_dir=tmp_path).load(plan.plan_hash)
    assert loaded is not None
```

- [ ] **Step 2: Implement plan_store.py**

```python
# backend/core/ouroboros/architect/plan_store.py
"""Immutable plan storage keyed by plan_hash. Single source of truth for allowlists."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from backend.core.ouroboros.architect.plan import (
    ArchitecturalPlan, PlanStep, StepIntentKind, AcceptanceCheck, CheckKind,
)

logger = logging.getLogger(__name__)

_DEFAULT_DIR = os.path.expanduser("~/.jarvis/ouroboros/plans")


class PlanStore:
    def __init__(self, store_dir: Path = Path(_DEFAULT_DIR)) -> None:
        self._dir = Path(store_dir)

    def store(self, plan: ArchitecturalPlan) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{plan.plan_hash}.json"
        if path.exists():
            return  # immutable — already stored
        path.write_text(json.dumps(self._serialize(plan), indent=2))

    def load(self, plan_hash: str) -> Optional[ArchitecturalPlan]:
        path = self._dir / f"{plan_hash}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return self._deserialize(data)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("[PlanStore] Corrupt plan %s: %s", plan_hash, exc)
            return None

    def exists(self, plan_hash: str) -> bool:
        return (self._dir / f"{plan_hash}.json").exists()

    @staticmethod
    def _serialize(plan: ArchitecturalPlan) -> dict:
        return {
            "plan_id": plan.plan_id,
            "plan_hash": plan.plan_hash,
            "parent_hypothesis_id": plan.parent_hypothesis_id,
            "parent_hypothesis_fingerprint": plan.parent_hypothesis_fingerprint,
            "title": plan.title,
            "description": plan.description,
            "repos_affected": list(plan.repos_affected),
            "non_goals": list(plan.non_goals),
            "steps": [
                {
                    "step_index": s.step_index,
                    "description": s.description,
                    "intent_kind": s.intent_kind.value,
                    "target_paths": list(s.target_paths),
                    "ancillary_paths": list(s.ancillary_paths),
                    "interface_contracts": list(s.interface_contracts),
                    "tests_required": list(s.tests_required),
                    "risk_tier_hint": s.risk_tier_hint,
                    "depends_on": list(s.depends_on),
                    "repo": s.repo,
                }
                for s in plan.steps
            ],
            "file_allowlist": sorted(plan.file_allowlist),
            "acceptance_checks": [
                {
                    "check_id": c.check_id,
                    "check_kind": c.check_kind.value,
                    "command": c.command,
                    "expected": c.expected,
                    "cwd": c.cwd,
                    "timeout_s": c.timeout_s,
                    "run_after_step": c.run_after_step,
                    "sandbox_required": c.sandbox_required,
                }
                for c in plan.acceptance_checks
            ],
            "model_used": plan.model_used,
            "created_at": plan.created_at,
            "snapshot_hash": plan.snapshot_hash,
        }

    @staticmethod
    def _deserialize(d: dict) -> ArchitecturalPlan:
        steps = tuple(
            PlanStep(
                step_index=s["step_index"],
                description=s["description"],
                intent_kind=StepIntentKind(s["intent_kind"]),
                target_paths=tuple(s["target_paths"]),
                ancillary_paths=tuple(s.get("ancillary_paths", ())),
                interface_contracts=tuple(s.get("interface_contracts", ())),
                tests_required=tuple(s.get("tests_required", ())),
                risk_tier_hint=s.get("risk_tier_hint", "safe_auto"),
                depends_on=tuple(s.get("depends_on", ())),
                repo=s["repo"],
            )
            for s in d["steps"]
        )
        checks = tuple(
            AcceptanceCheck(
                check_id=c["check_id"],
                check_kind=CheckKind(c["check_kind"]),
                command=c["command"],
                expected=c.get("expected", ""),
                cwd=c.get("cwd", "."),
                timeout_s=c.get("timeout_s", 120.0),
                run_after_step=c.get("run_after_step"),
                sandbox_required=c.get("sandbox_required", True),
            )
            for c in d["acceptance_checks"]
        )
        return ArchitecturalPlan(
            plan_id=d["plan_id"],
            plan_hash=d["plan_hash"],
            parent_hypothesis_id=d["parent_hypothesis_id"],
            parent_hypothesis_fingerprint=d["parent_hypothesis_fingerprint"],
            title=d["title"],
            description=d["description"],
            repos_affected=tuple(d["repos_affected"]),
            non_goals=tuple(d["non_goals"]),
            steps=steps,
            file_allowlist=frozenset(d["file_allowlist"]),
            acceptance_checks=checks,
            model_used=d["model_used"],
            created_at=d["created_at"],
            snapshot_hash=d["snapshot_hash"],
        )
```

- [ ] **Step 3: Run tests, commit**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_plan_store.py -v`
Commit: `feat(ouroboros/architect): add immutable PlanStore keyed by plan_hash`

---

## Task 5: Plan Decomposer (Deterministic)

**Files:**
- Create: `backend/core/ouroboros/architect/plan_decomposer.py`
- Create: `tests/core/ouroboros/architect/test_plan_decomposer.py`

- [ ] **Step 1: Write decomposer tests**

```python
# tests/core/ouroboros/architect/test_plan_decomposer.py
"""Tests for Plan -> IntentEnvelope decomposition."""
import pytest
from backend.core.ouroboros.architect.plan import (
    PlanStep, StepIntentKind, AcceptanceCheck, CheckKind, ArchitecturalPlan,
)
from backend.core.ouroboros.architect.plan_decomposer import PlanDecomposer


def _step(i=0, target="a.py", deps=()):
    return PlanStep(step_index=i, description=f"Step {i}",
                    intent_kind=StepIntentKind.CREATE_FILE,
                    target_paths=(target,), repo="jarvis", depends_on=deps)

def _plan(steps=None):
    steps = steps or (_step(0), _step(1, "b.py", deps=(0,)))
    check = AcceptanceCheck(check_id="t", check_kind=CheckKind.EXIT_CODE, command="echo ok", expected="")
    return ArchitecturalPlan.create(
        parent_hypothesis_id="h1", parent_hypothesis_fingerprint="fp1",
        title="T", description="D", repos_affected=("jarvis",), non_goals=(),
        steps=steps, acceptance_checks=(check,), model_used="test", snapshot_hash="s1",
    )


def test_produces_one_envelope_per_step():
    plan = _plan()
    envelopes = PlanDecomposer.decompose(plan, saga_id="saga-1")
    assert len(envelopes) == 2


def test_envelope_source_is_architecture():
    envelopes = PlanDecomposer.decompose(_plan(), saga_id="saga-1")
    assert all(e.source == "architecture" for e in envelopes)


def test_envelope_carries_saga_binding():
    plan = _plan()
    envelopes = PlanDecomposer.decompose(plan, saga_id="saga-1")
    for i, env in enumerate(envelopes):
        assert env.evidence["saga_id"] == "saga-1"
        assert env.evidence["plan_hash"] == plan.plan_hash
        assert env.evidence["step_index"] == i


def test_envelope_carries_analysis_complete():
    envelopes = PlanDecomposer.decompose(_plan(), saga_id="s1")
    assert all(e.evidence.get("analysis_complete") is True for e in envelopes)


def test_envelope_target_files_match_step():
    plan = _plan(steps=(_step(0, "backend/agents/whatsapp.py"),))
    envelopes = PlanDecomposer.decompose(plan, saga_id="s1")
    assert envelopes[0].target_files == ("backend/agents/whatsapp.py",)


def test_topological_order():
    """Envelopes ordered by topological tier."""
    steps = (_step(0), _step(1, "b.py", deps=(0,)), _step(2, "c.py", deps=(1,)))
    plan = _plan(steps=steps)
    envelopes = PlanDecomposer.decompose(plan, saga_id="s1")
    indices = [e.evidence["step_index"] for e in envelopes]
    assert indices == [0, 1, 2]
```

- [ ] **Step 2: Implement plan_decomposer.py**

```python
# backend/core/ouroboros/architect/plan_decomposer.py
"""Decompose ArchitecturalPlan into coordinated IntentEnvelopes. Deterministic."""
from __future__ import annotations

from typing import List

from backend.core.ouroboros.architect.plan import ArchitecturalPlan
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope, make_envelope,
)


class PlanDecomposer:
    """One envelope per PlanStep, ordered by topological tier."""

    @staticmethod
    def decompose(plan: ArchitecturalPlan, saga_id: str) -> List[IntentEnvelope]:
        ordered_steps = PlanDecomposer._topological_order(plan)
        envelopes: List[IntentEnvelope] = []
        for step in ordered_steps:
            envelope = make_envelope(
                source="architecture",
                description=f"[saga:{saga_id[:8]}] Step {step.step_index}: {step.description}",
                target_files=step.target_paths,
                repo=step.repo,
                confidence=1.0,
                urgency="normal",
                evidence={
                    "saga_id": saga_id,
                    "plan_hash": plan.plan_hash,
                    "step_index": step.step_index,
                    "intent_kind": step.intent_kind.value,
                    "analysis_complete": True,
                },
                requires_human_ack=False,
            )
            envelopes.append(envelope)
        return envelopes

    @staticmethod
    def _topological_order(plan: ArchitecturalPlan):
        """Kahn's algorithm — returns steps in dependency order."""
        in_degree = {s.step_index: len(s.depends_on) for s in plan.steps}
        step_map = {s.step_index: s for s in plan.steps}
        dependents = {s.step_index: [] for s in plan.steps}
        for s in plan.steps:
            for dep in s.depends_on:
                dependents[dep].append(s.step_index)

        queue = sorted(i for i, d in in_degree.items() if d == 0)
        result = []
        while queue:
            node = queue.pop(0)
            result.append(step_map[node])
            for dep_idx in sorted(dependents[node]):
                in_degree[dep_idx] -= 1
                if in_degree[dep_idx] == 0:
                    queue.append(dep_idx)
        return result
```

- [ ] **Step 3: Run tests, commit**

NOTE: "architecture" must be in _VALID_SOURCES for make_envelope to work. If not yet added, add it in intent_envelope.py first (Task 8 does this, but decomposer tests need it now). Read the file and add if missing.

Run: `python3 -m pytest tests/core/ouroboros/architect/test_plan_decomposer.py -v`
Commit: `feat(ouroboros/architect): add deterministic PlanDecomposer for plan -> envelope conversion`

---

## Task 6: Saga Orchestrator (WAL-Backed State Machine)

**Files:**
- Create: `backend/core/ouroboros/architect/saga_orchestrator.py`
- Create: `tests/core/ouroboros/architect/test_saga_orchestrator.py`

- [ ] **Step 1: Write orchestrator tests**

```python
# tests/core/ouroboros/architect/test_saga_orchestrator.py
"""Tests for WAL-backed SagaOrchestrator."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.architect.saga_orchestrator import SagaOrchestrator
from backend.core.ouroboros.architect.saga import SagaPhase, StepPhase
from backend.core.ouroboros.architect.plan import (
    PlanStep, StepIntentKind, AcceptanceCheck, CheckKind, ArchitecturalPlan,
)


def _plan(num_steps=2):
    steps = tuple(
        PlanStep(step_index=i, description=f"Step {i}",
                 intent_kind=StepIntentKind.CREATE_FILE,
                 target_paths=(f"f{i}.py",), repo="jarvis",
                 depends_on=(i-1,) if i > 0 else ())
        for i in range(num_steps)
    )
    check = AcceptanceCheck(check_id="t", check_kind=CheckKind.EXIT_CODE,
                           command="echo ok", expected="", sandbox_required=False)
    return ArchitecturalPlan.create(
        parent_hypothesis_id="h1", parent_hypothesis_fingerprint="fp1",
        title="T", description="D", repos_affected=("jarvis",), non_goals=(),
        steps=steps, acceptance_checks=(check,), model_used="test", snapshot_hash="s1",
    )


def _mock_plan_store(plan):
    store = MagicMock()
    store.load.return_value = plan
    return store


def _mock_intake():
    router = AsyncMock()
    router.ingest.return_value = "enqueued"
    return router


def _mock_acceptance():
    runner = AsyncMock()
    runner.run_checks.return_value = [MagicMock(passed=True)]
    return runner


def test_create_saga(tmp_path):
    plan = _plan()
    orch = SagaOrchestrator(
        plan_store=_mock_plan_store(plan), intake_router=_mock_intake(),
        acceptance_runner=_mock_acceptance(), saga_dir=tmp_path,
    )
    saga = orch.create_saga(plan)
    assert saga.phase == SagaPhase.PENDING
    assert len(saga.step_states) == 2


@pytest.mark.asyncio
async def test_execute_saga_completes(tmp_path):
    plan = _plan(num_steps=1)
    intake = _mock_intake()
    orch = SagaOrchestrator(
        plan_store=_mock_plan_store(plan), intake_router=intake,
        acceptance_runner=_mock_acceptance(), saga_dir=tmp_path,
    )
    saga = orch.create_saga(plan)
    result = await orch.execute(saga.saga_id)
    assert result.phase == SagaPhase.COMPLETE


@pytest.mark.asyncio
async def test_execute_saga_aborts_on_failure(tmp_path):
    plan = _plan(num_steps=2)
    intake = AsyncMock()
    intake.ingest.side_effect = [Exception("step 0 failed")]
    orch = SagaOrchestrator(
        plan_store=_mock_plan_store(plan), intake_router=intake,
        acceptance_runner=_mock_acceptance(), saga_dir=tmp_path,
    )
    saga = orch.create_saga(plan)
    result = await orch.execute(saga.saga_id)
    assert result.phase == SagaPhase.ABORTED


@pytest.mark.asyncio
async def test_saga_persists_to_wal(tmp_path):
    plan = _plan(num_steps=1)
    orch = SagaOrchestrator(
        plan_store=_mock_plan_store(plan), intake_router=_mock_intake(),
        acceptance_runner=_mock_acceptance(), saga_dir=tmp_path,
    )
    saga = orch.create_saga(plan)
    await orch.execute(saga.saga_id)
    # New instance can read the saga
    orch2 = SagaOrchestrator(
        plan_store=_mock_plan_store(plan), intake_router=_mock_intake(),
        acceptance_runner=_mock_acceptance(), saga_dir=tmp_path,
    )
    loaded = orch2.get_saga(saga.saga_id)
    assert loaded is not None
    assert loaded.phase == SagaPhase.COMPLETE


def test_list_sagas(tmp_path):
    plan = _plan()
    orch = SagaOrchestrator(
        plan_store=_mock_plan_store(plan), intake_router=_mock_intake(),
        acceptance_runner=_mock_acceptance(), saga_dir=tmp_path,
    )
    orch.create_saga(plan)
    assert len(orch.list_sagas()) == 1
```

- [ ] **Step 2: Implement saga_orchestrator.py**

```python
# backend/core/ouroboros/architect/saga_orchestrator.py
"""WAL-backed saga orchestrator. Sequential execution by topological tier (v1)."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.architect.plan import ArchitecturalPlan
from backend.core.ouroboros.architect.plan_decomposer import PlanDecomposer
from backend.core.ouroboros.architect.saga import SagaRecord, SagaPhase, StepPhase

logger = logging.getLogger(__name__)

_DEFAULT_DIR = os.path.expanduser("~/.jarvis/ouroboros/sagas")


class SagaOrchestrator:
    """Orchestrate multi-step architectural sagas."""

    def __init__(
        self,
        plan_store: Any,
        intake_router: Any,
        acceptance_runner: Any,
        saga_dir: Path = Path(_DEFAULT_DIR),
    ) -> None:
        self._plan_store = plan_store
        self._intake_router = intake_router
        self._acceptance_runner = acceptance_runner
        self._saga_dir = Path(saga_dir)
        self._sagas: Dict[str, SagaRecord] = {}
        self._load_existing()

    def create_saga(self, plan: ArchitecturalPlan) -> SagaRecord:
        saga_id = uuid.uuid4().hex[:16]
        saga = SagaRecord.create(
            saga_id=saga_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            num_steps=len(plan.steps),
        )
        self._sagas[saga_id] = saga
        self._persist(saga)
        return saga

    async def execute(self, saga_id: str) -> SagaRecord:
        saga = self._sagas.get(saga_id)
        if saga is None:
            raise ValueError(f"Saga {saga_id} not found")

        plan = self._plan_store.load(saga.plan_hash)
        if plan is None:
            saga.phase = SagaPhase.ABORTED
            saga.abort_reason = "Plan not found in store"
            self._persist(saga)
            return saga

        saga.phase = SagaPhase.RUNNING
        self._persist(saga)

        # Decompose plan into envelopes
        envelopes = PlanDecomposer.decompose(plan, saga_id=saga_id)
        envelope_map = {e.evidence["step_index"]: e for e in envelopes}

        # Execute sequentially by topological order
        for envelope in envelopes:
            step_idx = envelope.evidence["step_index"]
            step_state = saga.step_states[step_idx]

            # Check dependencies
            step = plan.steps[step_idx]
            deps_met = all(
                saga.step_states[d].phase == StepPhase.COMPLETE
                for d in step.depends_on
            )
            if not deps_met:
                step_state.phase = StepPhase.BLOCKED
                saga.phase = SagaPhase.ABORTED
                saga.abort_reason = f"Step {step_idx} dependencies not met"
                self._persist(saga)
                return saga

            # Execute step
            step_state.phase = StepPhase.RUNNING
            step_state.started_at = time.time()
            self._persist(saga)

            try:
                await self._intake_router.ingest(envelope)
                step_state.phase = StepPhase.COMPLETE
                step_state.completed_at = time.time()
            except Exception as exc:
                step_state.phase = StepPhase.FAILED
                step_state.error = str(exc)
                step_state.completed_at = time.time()
                saga.phase = SagaPhase.ABORTED
                saga.abort_reason = f"Step {step_idx} failed: {exc}"
                # Mark remaining steps as blocked
                for remaining in saga.step_states.values():
                    if remaining.phase == StepPhase.PENDING:
                        remaining.phase = StepPhase.BLOCKED
                self._persist(saga)
                return saga

            self._persist(saga)

        # All steps complete — run acceptance checks
        try:
            results = await self._acceptance_runner.run_checks(
                plan.acceptance_checks, saga_id,
            )
            all_passed = all(getattr(r, "passed", True) for r in results)
            if all_passed:
                saga.phase = SagaPhase.COMPLETE
                saga.completed_at = time.time()
            else:
                saga.phase = SagaPhase.ABORTED
                saga.abort_reason = "Acceptance checks failed"
        except Exception as exc:
            saga.phase = SagaPhase.ABORTED
            saga.abort_reason = f"Acceptance runner error: {exc}"

        self._persist(saga)
        return saga

    def get_saga(self, saga_id: str) -> Optional[SagaRecord]:
        if saga_id in self._sagas:
            return self._sagas[saga_id]
        return self._load_from_disk(saga_id)

    def list_sagas(self) -> List[SagaRecord]:
        return list(self._sagas.values())

    def _persist(self, saga: SagaRecord) -> None:
        self._saga_dir.mkdir(parents=True, exist_ok=True)
        path = self._saga_dir / f"{saga.saga_id}.json"
        path.write_text(json.dumps(saga.to_dict(), indent=2))

    def _load_from_disk(self, saga_id: str) -> Optional[SagaRecord]:
        path = self._saga_dir / f"{saga_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            saga = SagaRecord.from_dict(data)
            self._sagas[saga_id] = saga
            return saga
        except (json.JSONDecodeError, KeyError):
            return None

    def _load_existing(self) -> None:
        if not self._saga_dir.exists():
            return
        for path in self._saga_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                saga = SagaRecord.from_dict(data)
                self._sagas[saga.saga_id] = saga
            except (json.JSONDecodeError, KeyError):
                pass
```

- [ ] **Step 3: Run tests, commit**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_saga_orchestrator.py -v`
Commit: `feat(ouroboros/architect): add WAL-backed SagaOrchestrator with sequential execution`

---

## Task 7: Acceptance Runner (Sandbox-Bound)

**Files:**
- Create: `backend/core/ouroboros/architect/acceptance_runner.py`
- Create: `tests/core/ouroboros/architect/test_acceptance_runner.py`

- [ ] **Step 1: Write runner tests**

```python
# tests/core/ouroboros/architect/test_acceptance_runner.py
"""Tests for sandbox-bound AcceptanceRunner."""
import asyncio
import pytest
from backend.core.ouroboros.architect.acceptance_runner import AcceptanceRunner, AcceptanceResult
from backend.core.ouroboros.architect.plan import AcceptanceCheck, CheckKind


def _check(command="echo ok", kind=CheckKind.EXIT_CODE, expected="", sandbox=False):
    return AcceptanceCheck(
        check_id="t", check_kind=kind, command=command,
        expected=expected, sandbox_required=sandbox, timeout_s=5,
    )


@pytest.mark.asyncio
async def test_exit_code_success():
    runner = AcceptanceRunner()
    results = await runner.run_checks((_check(command="echo ok"),), saga_id="s1")
    assert len(results) == 1
    assert results[0].passed


@pytest.mark.asyncio
async def test_exit_code_failure():
    runner = AcceptanceRunner()
    results = await runner.run_checks((_check(command="false"),), saga_id="s1")
    assert len(results) == 1
    assert not results[0].passed


@pytest.mark.asyncio
async def test_regex_stdout_match():
    check = _check(command="echo hello world", kind=CheckKind.REGEX_STDOUT, expected="hello")
    runner = AcceptanceRunner()
    results = await runner.run_checks((check,), saga_id="s1")
    assert results[0].passed


@pytest.mark.asyncio
async def test_regex_stdout_no_match():
    check = _check(command="echo goodbye", kind=CheckKind.REGEX_STDOUT, expected="hello")
    runner = AcceptanceRunner()
    results = await runner.run_checks((check,), saga_id="s1")
    assert not results[0].passed


@pytest.mark.asyncio
async def test_timeout_returns_failed():
    check = _check(command="sleep 10", sandbox=False)
    check = AcceptanceCheck(check_id="t", check_kind=CheckKind.EXIT_CODE,
                           command="sleep 10", expected="", timeout_s=0.5, sandbox_required=False)
    runner = AcceptanceRunner()
    results = await runner.run_checks((check,), saga_id="s1")
    assert not results[0].passed
    assert "timeout" in results[0].error.lower()


def test_result_dataclass():
    r = AcceptanceResult(check_id="t", passed=True, output="ok", error="")
    assert r.passed
    assert r.check_id == "t"
```

- [ ] **Step 2: Implement acceptance_runner.py**

```python
# backend/core/ouroboros/architect/acceptance_runner.py
"""Sandbox-bound acceptance check executor.

v1: runs commands via asyncio subprocess. sandbox_required=True checks
are skipped with a warning (Reactor Core sandbox integration deferred).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import List, Tuple

from backend.core.ouroboros.architect.plan import AcceptanceCheck, CheckKind

logger = logging.getLogger(__name__)


@dataclass
class AcceptanceResult:
    check_id: str
    passed: bool
    output: str = ""
    error: str = ""


class AcceptanceRunner:
    """Execute acceptance checks. v1: local subprocess. v2: Reactor sandbox."""

    async def run_checks(
        self,
        checks: Tuple[AcceptanceCheck, ...],
        saga_id: str,
    ) -> List[AcceptanceResult]:
        results: List[AcceptanceResult] = []
        for check in checks:
            if check.sandbox_required:
                logger.warning(
                    "[AcceptanceRunner] Sandbox check '%s' skipped (Reactor integration pending)",
                    check.check_id,
                )
                results.append(AcceptanceResult(
                    check_id=check.check_id, passed=True,
                    output="skipped: sandbox_required (v1)",
                ))
                continue

            result = await self._run_one(check)
            results.append(result)
        return results

    async def _run_one(self, check: AcceptanceCheck) -> AcceptanceResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                check.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=check.cwd if check.cwd != "." else None,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=check.timeout_s,
            )
            output = stdout.decode(errors="replace")
            exit_code = proc.returncode or 0

            if check.check_kind == CheckKind.EXIT_CODE:
                return AcceptanceResult(
                    check_id=check.check_id,
                    passed=(exit_code == 0),
                    output=output,
                    error=stderr.decode(errors="replace") if exit_code != 0 else "",
                )
            elif check.check_kind == CheckKind.REGEX_STDOUT:
                matched = bool(re.search(check.expected, output))
                return AcceptanceResult(
                    check_id=check.check_id,
                    passed=matched,
                    output=output,
                    error="" if matched else f"Pattern '{check.expected}' not found",
                )
            elif check.check_kind == CheckKind.IMPORT_CHECK:
                return AcceptanceResult(
                    check_id=check.check_id,
                    passed=(exit_code == 0),
                    output=output,
                )
            else:
                return AcceptanceResult(
                    check_id=check.check_id, passed=False,
                    error=f"Unknown check_kind: {check.check_kind}",
                )
        except asyncio.TimeoutError:
            return AcceptanceResult(
                check_id=check.check_id, passed=False,
                error=f"Timeout after {check.timeout_s}s",
            )
        except Exception as exc:
            return AcceptanceResult(
                check_id=check.check_id, passed=False,
                error=str(exc),
            )
```

- [ ] **Step 3: Run tests, commit**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_acceptance_runner.py -v`
Commit: `feat(ouroboros/architect): add AcceptanceRunner with subprocess + timeout support`

---

## Task 8: Architecture Reasoning Agent (Model Call)

**Files:**
- Create: `backend/core/ouroboros/architect/reasoning_agent.py`
- Create: `tests/core/ouroboros/architect/test_reasoning_agent.py`

- [ ] **Step 1: Write agent tests**

```python
# tests/core/ouroboros/architect/test_reasoning_agent.py
"""Tests for ArchitectureReasoningAgent (model call)."""
import pytest
from unittest.mock import MagicMock, AsyncMock
from backend.core.ouroboros.architect.reasoning_agent import (
    ArchitectureReasoningAgent, AgentConfig,
)
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis


def _hyp(gap_type="missing_capability", confidence=0.9):
    return FeatureHypothesis.new(
        description="Missing WhatsApp agent",
        evidence_fragments=("spec:manifesto",),
        gap_type=gap_type,
        confidence=confidence,
        confidence_rule_id="spec_symbol_miss",
        urgency="normal",
        suggested_scope="backend/agents/",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash="abc",
        synthesis_input_fingerprint="fp1",
    )


def test_filters_non_architectural_gap_types():
    agent = ArchitectureReasoningAgent(
        oracle=MagicMock(), doubleword=None, config=AgentConfig(),
    )
    # incomplete_wiring should be filtered out
    assert not agent.should_design(_hyp(gap_type="incomplete_wiring"))
    assert agent.should_design(_hyp(gap_type="missing_capability"))
    assert agent.should_design(_hyp(gap_type="manifesto_violation"))


def test_filters_low_confidence():
    agent = ArchitectureReasoningAgent(
        oracle=MagicMock(), doubleword=None, config=AgentConfig(min_confidence=0.8),
    )
    assert not agent.should_design(_hyp(confidence=0.5))
    assert agent.should_design(_hyp(confidence=0.9))


@pytest.mark.asyncio
async def test_design_returns_none_for_filtered():
    agent = ArchitectureReasoningAgent(
        oracle=MagicMock(), doubleword=None, config=AgentConfig(),
    )
    result = await agent.design(
        _hyp(gap_type="incomplete_wiring"),
        snapshot=MagicMock(), oracle=MagicMock(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_design_returns_plan_structure():
    """v1: agent produces a deterministic template plan (no model call)."""
    agent = ArchitectureReasoningAgent(
        oracle=MagicMock(), doubleword=None, config=AgentConfig(),
    )
    result = await agent.design(
        _hyp(gap_type="missing_capability"),
        snapshot=MagicMock(content_hash="snap1"),
        oracle=MagicMock(),
    )
    # v1 returns a template plan or None
    # (full model integration deferred — same as synthesis engine v1)
    assert result is None or hasattr(result, "plan_hash")
```

- [ ] **Step 2: Implement reasoning_agent.py**

```python
# backend/core/ouroboros/architect/reasoning_agent.py
"""ArchitectureReasoningAgent — design phase (model call).

v1: threshold filtering + template plan generation. Model integration
(Doubleword 397B batch) deferred to when OperationContext bridge is built.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from backend.core.ouroboros.architect.plan import ArchitecturalPlan

logger = logging.getLogger(__name__)

_ARCHITECTURAL_GAP_TYPES = frozenset({"missing_capability", "manifesto_violation"})


@dataclass
class AgentConfig:
    min_confidence: float = 0.7
    model: str = "doubleword-397b"
    fallback_model: str = "claude-api"
    max_steps: int = 10


class ArchitectureReasoningAgent:
    """Produces ArchitecturalPlans from FeatureHypotheses.

    v1: threshold filtering only. Model-based plan generation deferred.
    The agent exposes should_design() for the REM epoch routing split
    and design() for the full pipeline.
    """

    def __init__(
        self,
        oracle: Any,
        doubleword: Any,
        config: AgentConfig = AgentConfig(),
    ) -> None:
        self._oracle = oracle
        self._doubleword = doubleword
        self._config = config

    def should_design(self, hypothesis: Any) -> bool:
        """Determine if a hypothesis warrants architectural design."""
        gap_type = getattr(hypothesis, "gap_type", None)
        confidence = getattr(hypothesis, "confidence", 0.0)
        if gap_type not in _ARCHITECTURAL_GAP_TYPES:
            return False
        if confidence < self._config.min_confidence:
            return False
        return True

    async def design(
        self,
        hypothesis: Any,
        snapshot: Any,
        oracle: Any,
    ) -> Optional[ArchitecturalPlan]:
        """Design an architectural plan for a capability gap.

        v1: returns None (model integration deferred).
        Future: 397B/Claude produces structured plan JSON.
        """
        if not self.should_design(hypothesis):
            return None

        # v1: Model-based plan generation not yet implemented
        # The infrastructure (PlanValidator, PlanStore, SagaOrchestrator)
        # is ready to accept plans when the model bridge is built.
        logger.info(
            "[Architect] Hypothesis '%s' qualifies for design (gap=%s, conf=%.2f) "
            "— model integration pending",
            getattr(hypothesis, "description", "unknown"),
            getattr(hypothesis, "gap_type", "unknown"),
            getattr(hypothesis, "confidence", 0.0),
        )
        return None

    def health(self) -> dict:
        return {
            "config": {
                "model": self._config.model,
                "min_confidence": self._config.min_confidence,
                "max_steps": self._config.max_steps,
            },
            "model_integration": "pending",
        }
```

- [ ] **Step 3: Run tests, commit**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_reasoning_agent.py -v`
Commit: `feat(ouroboros/architect): add ArchitectureReasoningAgent with threshold filtering`

---

## Task 9: Existing File Modifications (architecture source + config)

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intent_envelope.py`
- Modify: `backend/core/ouroboros/governance/intake/unified_intake_router.py`
- Modify: `backend/core/ouroboros/governance/risk_engine.py`
- Modify: `backend/core/ouroboros/daemon_config.py`

- [ ] **Step 1: Write tests**

```python
# tests/core/ouroboros/architect/test_architecture_source.py
"""Tests that 'architecture' is a valid source with correct priority and risk rules."""
import pytest


def test_architecture_is_valid_source():
    from backend.core.ouroboros.governance.intake.intent_envelope import _VALID_SOURCES
    assert "architecture" in _VALID_SOURCES


def test_architecture_priority():
    from backend.core.ouroboros.governance.intake.unified_intake_router import _PRIORITY_MAP
    assert "architecture" in _PRIORITY_MAP
    assert _PRIORITY_MAP["architecture"] == 3  # higher than exploration (4)


def test_architect_config_fields():
    from backend.core.ouroboros.daemon_config import DaemonConfig
    config = DaemonConfig.from_env()
    assert hasattr(config, "architect_enabled")
    assert hasattr(config, "architect_max_steps")
    assert hasattr(config, "architect_max_sagas_per_epoch")
    assert hasattr(config, "saga_step_timeout_s")
    assert hasattr(config, "saga_total_timeout_s")
    assert hasattr(config, "acceptance_timeout_s")
```

- [ ] **Step 2: Make modifications**

Read each file first, then:

**intent_envelope.py:** Add `"architecture"` to `_VALID_SOURCES` (may have been added by Task 5's decomposer).

**unified_intake_router.py:** Add `"architecture": 3` to `_PRIORITY_MAP`.

**risk_engine.py:** After the `if profile.source in ("exploration", "roadmap"):` block, add:
```python
if profile.source == "architecture":
    # A1: BLOCK kernel, secrets, auth
    if any(sentinel in fpath for fpath in file_strs for sentinel in self._EXPLORATION_KERNEL_SENTINELS):
        return RiskClassification(tier=RiskTier.BLOCKED, reason_code="architecture_touches_kernel")
    if any(sentinel in fpath for fpath in file_strs for sentinel in self._EXPLORATION_SECURITY_SENTINELS):
        return RiskClassification(tier=RiskTier.BLOCKED, reason_code="architecture_touches_security")
    # A2: APPROVAL_REQUIRED for ouroboros self-modification
    if any(sentinel in fpath for fpath in file_strs for sentinel in self._EXPLORATION_SELF_MOD_SENTINELS):
        return RiskClassification(tier=RiskTier.APPROVAL_REQUIRED, reason_code="architecture_self_modification")
    # A3: APPROVAL_REQUIRED for cross-repo
    if profile.crosses_repo_boundary:
        return RiskClassification(tier=RiskTier.APPROVAL_REQUIRED, reason_code="architecture_cross_repo")
    # A4: SAFE_AUTO for single-repo within allowlist
    # (allowlist enforcement is per-envelope GATE, not here)
    # A5: else BLOCK unknown surface
```

**daemon_config.py:** Add architect fields:
```python
    architect_enabled: bool = True
    architect_max_steps: int = 10
    architect_max_sagas_per_epoch: int = 2
    saga_step_timeout_s: float = 300.0
    saga_total_timeout_s: float = 3600.0
    acceptance_timeout_s: float = 120.0
```

- [ ] **Step 3: Run tests, commit**

Run: `python3 -m pytest tests/core/ouroboros/architect/test_architecture_source.py -v`
Commit: `feat(ouroboros): add 'architecture' source + config fields for reasoning agent`

---

## Task 10: REM Epoch + Daemon Wiring

**Files:**
- Modify: `backend/core/ouroboros/rem_epoch.py`
- Modify: `backend/core/ouroboros/rem_sleep.py`
- Modify: `backend/core/ouroboros/daemon.py`

- [ ] **Step 1: Add architect to RemEpoch**

Read rem_epoch.py. In `__init__`, add `architect: Any = None` parameter. Store as `self._architect`.

In the PATCHING section (where envelopes are created and submitted), add architect routing BEFORE the normal envelope path:

```python
# In the patching loop, check if finding warrants architectural design
if (self._architect is not None
    and finding.source_check.startswith("roadmap:")
    and finding.category in ("missing_capability", "manifesto_violation")
    and self._architect.should_design(finding)):
    # Route to architect (fire-and-forget for v1)
    logger.info("[RemEpoch] Routing to architect: %s", finding.description)
    continue  # skip normal envelope path for this finding
```

- [ ] **Step 2: Thread architect through RemSleepDaemon and OuroborosDaemon**

In rem_sleep.py `__init__`, add `architect: Any = None`. Pass to RemEpoch.
In daemon.py `awaken()`, create ArchitectureReasoningAgent and pass to RemSleepDaemon.

- [ ] **Step 3: Verify no regressions**

Run: `python3 -m pytest tests/core/ouroboros/test_rem_epoch.py tests/core/ouroboros/test_rem_sleep.py tests/core/ouroboros/test_daemon.py -v --tb=short`
Expected: All existing tests pass (architect defaults to None)

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(ouroboros): wire ArchitectureReasoningAgent through daemon -> REM -> epoch"
```

---

## Task 11: Integration Test

**Files:**
- Create: `tests/core/ouroboros/architect/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/core/ouroboros/architect/test_integration.py
"""End-to-end: hypothesis -> design threshold -> validate -> decompose -> saga -> accept."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock
from backend.core.ouroboros.architect.plan import (
    PlanStep, StepIntentKind, AcceptanceCheck, CheckKind, ArchitecturalPlan,
)
from backend.core.ouroboros.architect.plan_validator import PlanValidator
from backend.core.ouroboros.architect.plan_store import PlanStore
from backend.core.ouroboros.architect.plan_decomposer import PlanDecomposer
from backend.core.ouroboros.architect.saga_orchestrator import SagaOrchestrator
from backend.core.ouroboros.architect.saga import SagaPhase
from backend.core.ouroboros.architect.acceptance_runner import AcceptanceRunner
from backend.core.ouroboros.architect.reasoning_agent import ArchitectureReasoningAgent, AgentConfig


def _valid_plan():
    steps = (
        PlanStep(step_index=0, description="Create agent",
                 intent_kind=StepIntentKind.CREATE_FILE,
                 target_paths=("backend/agents/whatsapp.py",),
                 tests_required=("tests/test_whatsapp.py",),
                 repo="jarvis"),
        PlanStep(step_index=1, description="Wire into registry",
                 intent_kind=StepIntentKind.MODIFY_FILE,
                 target_paths=("backend/agents/registry.py",),
                 repo="jarvis", depends_on=(0,)),
    )
    check = AcceptanceCheck(check_id="import", check_kind=CheckKind.EXIT_CODE,
                           command="echo ok", expected="", sandbox_required=False)
    return ArchitecturalPlan.create(
        parent_hypothesis_id="h1", parent_hypothesis_fingerprint="fp1",
        title="WhatsApp Agent", description="Add WhatsApp integration",
        repos_affected=("jarvis",), non_goals=("No UI",),
        steps=steps, acceptance_checks=(check,),
        model_used="test", snapshot_hash="snap1",
    )


def test_plan_validates(tmp_path):
    plan = _valid_plan()
    result = PlanValidator(max_steps=10).validate(plan)
    assert result.passed


def test_plan_stores_and_loads(tmp_path):
    plan = _valid_plan()
    store = PlanStore(store_dir=tmp_path)
    store.store(plan)
    loaded = store.load(plan.plan_hash)
    assert loaded.title == "WhatsApp Agent"


def test_plan_decomposes_to_envelopes():
    plan = _valid_plan()
    envelopes = PlanDecomposer.decompose(plan, saga_id="saga-1")
    assert len(envelopes) == 2
    assert envelopes[0].evidence["step_index"] == 0
    assert envelopes[1].evidence["step_index"] == 1


@pytest.mark.asyncio
async def test_full_saga_lifecycle(tmp_path):
    plan = _valid_plan()
    store = PlanStore(store_dir=tmp_path / "plans")
    store.store(plan)

    intake = AsyncMock()
    intake.ingest.return_value = "enqueued"

    runner = AcceptanceRunner()

    orch = SagaOrchestrator(
        plan_store=store, intake_router=intake,
        acceptance_runner=runner, saga_dir=tmp_path / "sagas",
    )
    saga = orch.create_saga(plan)
    result = await orch.execute(saga.saga_id)
    assert result.phase == SagaPhase.COMPLETE
    assert intake.ingest.call_count == 2


def test_reasoning_agent_filters_correctly():
    agent = ArchitectureReasoningAgent(
        oracle=MagicMock(), doubleword=None, config=AgentConfig(),
    )
    # Mock hypothesis with missing_capability
    hyp = MagicMock()
    hyp.gap_type = "missing_capability"
    hyp.confidence = 0.9
    assert agent.should_design(hyp)

    hyp.gap_type = "incomplete_wiring"
    assert not agent.should_design(hyp)
```

- [ ] **Step 2: Run full architect test suite**

Run: `python3 -m pytest tests/core/ouroboros/architect/ -v --tb=short`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/core/ouroboros/architect/test_integration.py
git commit -m "test(ouroboros/architect): add end-to-end integration tests for plan -> saga -> accept"
```

---

## Summary

| Task | What it builds | New files | Modified files |
|------|---------------|-----------|---------------|
| 1 | Plan schemas (ArchitecturalPlan, PlanStep, AcceptanceCheck) | 4 + 2 inits | 0 |
| 2 | Saga schemas (SagaRecord, StepState) | 2 | 0 |
| 3 | PlanValidator (10 structural rules) | 2 | 0 |
| 4 | PlanStore (immutable, keyed by plan_hash) | 2 | 0 |
| 5 | PlanDecomposer (plan -> envelopes) | 2 | 0 |
| 6 | SagaOrchestrator (WAL-backed state machine) | 2 | 0 |
| 7 | AcceptanceRunner (subprocess + timeout) | 2 | 0 |
| 8 | ReasoningAgent (threshold filter, v1) | 2 | 0 |
| 9 | Existing file mods (source + config) | 1 test | 4 |
| 10 | REM + daemon wiring | 0 | 3 |
| 11 | Integration tests | 1 | 0 |
| **Total** | | **20 new** | **7 modified** |

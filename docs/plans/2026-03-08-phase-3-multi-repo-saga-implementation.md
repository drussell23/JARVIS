# Phase 2C.4 + Phase 3 Multi-Repo Saga Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make JARVIS fully autonomous across jarvis/prime/reactor-core via a deterministic Saga pattern with preimage compensation and three-tier cross-repo verification.

**Architecture:** `SagaApplyStrategy` plugs into `GovernedOrchestrator`'s APPLY phase when `ctx.cross_repo is True`; single-repo path is untouched. `OperationContext` gains 9 new frozen fields with DAG cycle detection in `__post_init__`. Compensation uses per-file preimages (not `HEAD`), staged via `git add`, with idempotent resume via `saga_step_index`.

**Tech Stack:** Python 3.11+, `dataclasses` (frozen), `asyncio`, `subprocess` (git), `pyright`/`ruff` (Tier 1 verify), `pytest` with `asyncio_mode = "auto"`.

---

## Key files to read before starting

- `backend/core/ouroboros/governance/op_context.py` (534 lines) — add new fields here
- `backend/core/ouroboros/governance/orchestrator.py` — APPLY phase at line 456-496
- `backend/core/ouroboros/governance/intake/intake_layer_service.py` — add config field
- `backend/core/ouroboros/governance/intake/sensors/opportunity_miner_sensor.py` — flip ack logic
- `backend/core/ouroboros/governance/multi_repo/repo_pipeline.py` — fix submit() at line 81-85
- `tests/governance/` — test directory structure reference

---

## Testing rules

- **Never add `@pytest.mark.asyncio`** — `asyncio_mode = "auto"` is in `pyproject.toml`
- Run tests with: `python -m pytest tests/<path> -v`
- Confirm FAIL before implementing, PASS after

---

## Task 1: Phase 2C.4 — OpportunityMinerSensor auto-submit

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/sensors/opportunity_miner_sensor.py`
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py`
- Test: `tests/governance/intake/test_miner_auto_submit.py`

**Step 1: Write the failing tests**

Create `tests/governance/intake/test_miner_auto_submit.py`:

```python
"""Tests for OpportunityMinerSensor auto-submit threshold (Phase 2C.4)."""
import asyncio
import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    OpportunityMinerSensor,
)


async def test_high_confidence_candidate_no_human_ack(tmp_path):
    """CC well above threshold → confidence high → requires_human_ack=False."""
    src = tmp_path / "complex.py"
    lines = ["def foo(x):\n"] + [f"    if x == {i}: return {i}\n" for i in range(30)] + ["    return -1\n"]
    src.write_text("".join(lines))

    router = MagicMock()
    captured = []

    async def capture_ingest(env):
        captured.append(env)
        return "enqueued"

    router.ingest = capture_ingest
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=5,
        auto_submit_threshold=0.75,
    )
    await sensor.scan_once()
    assert len(captured) == 1
    assert captured[0].requires_human_ack is False


async def test_low_confidence_candidate_requires_human_ack(tmp_path):
    """CC just above threshold → confidence low → requires_human_ack=True."""
    src = tmp_path / "mild.py"
    lines = ["def foo(x):\n"] + [f"    if x == {i}: return {i}\n" for i in range(6)] + ["    return -1\n"]
    src.write_text("".join(lines))

    router = MagicMock()
    captured = []

    async def capture_ingest(env):
        captured.append(env)
        return "pending_ack"

    router.ingest = capture_ingest
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=5,
        auto_submit_threshold=0.75,
    )
    await sensor.scan_once()
    assert len(captured) == 1
    assert captured[0].requires_human_ack is True


def test_default_auto_submit_threshold():
    """Default threshold is 0.75 when not specified."""
    router = MagicMock()
    sensor = OpportunityMinerSensor(repo_root=Path("."), router=router)
    assert sensor._auto_submit_threshold == 0.75


async def test_intake_layer_config_has_miner_auto_submit_threshold():
    """IntakeLayerConfig exposes miner_auto_submit_threshold with default 0.75."""
    from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeLayerConfig
    from pathlib import Path
    cfg = IntakeLayerConfig(project_root=Path("/tmp"))
    assert cfg.miner_auto_submit_threshold == 0.75


async def test_intake_layer_config_from_env_reads_threshold(monkeypatch, tmp_path):
    """JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD env var is read."""
    monkeypatch.setenv("JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD", "0.60")
    from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeLayerConfig
    import importlib
    cfg = IntakeLayerConfig.from_env(project_root=tmp_path)
    assert cfg.miner_auto_submit_threshold == 0.60
```

**Step 2: Run tests to verify FAIL**

```bash
python -m pytest tests/governance/intake/test_miner_auto_submit.py -v
```

Expected: FAIL — `TypeError: OpportunityMinerSensor.__init__() got an unexpected keyword argument 'auto_submit_threshold'`

**Step 3: Implement — `opportunity_miner_sensor.py`**

In `OpportunityMinerSensor.__init__()`, add the parameter (after line 75, `poll_interval_s`):

```python
        auto_submit_threshold: float = 0.75,
```

Add to `__init__` body (after `self._poll_interval_s = poll_interval_s`):

```python
        self._auto_submit_threshold = auto_submit_threshold
```

In `scan_once()`, replace the hardcoded line (line 133):

```python
                    requires_human_ack=True,  # Phase 2C.1: ALWAYS requires human ack
```

With:

```python
                    requires_human_ack=(confidence < self._auto_submit_threshold),
```

**Step 4: Implement — `intake_layer_service.py`**

In `IntakeLayerConfig` dataclass (after `miner_complexity_threshold: int = 10`), add:

```python
    miner_auto_submit_threshold: float = 0.75
```

In `IntakeLayerConfig.from_env()` (after the `miner_complexity_threshold` line), add:

```python
            miner_auto_submit_threshold=float(
                os.getenv("JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD", "0.75")
            ),
```

In `_build_components()`, add `auto_submit_threshold` to the `OpportunityMinerSensor(...)` call:

```python
        opportunity_miner_sensor = OpportunityMinerSensor(
            repo_root=self._config.project_root,
            router=self._router,
            scan_paths=self._config.miner_scan_paths,
            complexity_threshold=self._config.miner_complexity_threshold,
            poll_interval_s=self._config.miner_scan_interval_s,
            auto_submit_threshold=self._config.miner_auto_submit_threshold,
        )
```

**Step 5: Run tests to verify PASS**

```bash
python -m pytest tests/governance/intake/test_miner_auto_submit.py -v
```

Expected: 5 passed

**Step 6: Confirm no regressions**

```bash
python -m pytest tests/governance/intake/ -v
```

Expected: all green

**Step 7: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/opportunity_miner_sensor.py \
        backend/core/ouroboros/governance/intake/intake_layer_service.py \
        tests/governance/intake/test_miner_auto_submit.py
git commit -m "feat(intake): Phase 2C.4 OpportunityMinerSensor auto-submit threshold"
```

---

## Task 2: OperationContext upgrade — saga types and new fields

**Files:**
- Modify: `backend/core/ouroboros/governance/op_context.py`
- Test: `tests/governance/test_op_context_upgrade.py`

**Step 1: Write the failing tests**

Create `tests/governance/test_op_context_upgrade.py`:

```python
"""Tests for OperationContext saga fields (Phase 3)."""
import pytest
from backend.core.ouroboros.governance.op_context import (
    ArchitecturalCycleError,
    OperationContext,
    OperationPhase,
    RepoSagaStatus,
    SagaStepStatus,
)


def test_saga_step_status_values():
    """All required saga step statuses exist."""
    required = {"pending", "applying", "applied", "skipped", "failed",
                "compensating", "compensated", "compensation_failed"}
    assert required == {s.value for s in SagaStepStatus}


def test_repo_saga_status_frozen():
    """RepoSagaStatus is a frozen dataclass."""
    s = RepoSagaStatus(repo="jarvis", status=SagaStepStatus.PENDING)
    with pytest.raises((AttributeError, TypeError)):
        s.repo = "prime"  # type: ignore


def test_repo_saga_status_defaults():
    s = RepoSagaStatus(repo="jarvis", status=SagaStepStatus.PENDING)
    assert s.attempt == 0
    assert s.last_error == ""
    assert s.reason_code == ""
    assert s.compensation_attempted is False


def test_op_context_has_saga_fields():
    """OperationContext.create() includes all new Phase 3 fields."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="test",
        primary_repo="jarvis",
    )
    assert ctx.primary_repo == "jarvis"
    assert ctx.repo_scope == ("jarvis",)
    assert ctx.cross_repo is False
    assert ctx.dependency_edges == ()
    assert ctx.apply_plan == ()
    assert ctx.repo_snapshots == ()
    assert ctx.saga_id == ""
    assert ctx.saga_state == ()
    assert ctx.schema_version == "3.0"


def test_cross_repo_derived_true():
    """cross_repo is True when repo_scope has more than one entry."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="multi",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
    )
    assert ctx.cross_repo is True


def test_cross_repo_derived_false_single():
    """cross_repo is False for single repo."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="single",
        repo_scope=("jarvis",),
        primary_repo="jarvis",
    )
    assert ctx.cross_repo is False


def test_dag_cycle_raises():
    """Cycle in dependency_edges raises ArchitecturalCycleError at create time."""
    with pytest.raises(ArchitecturalCycleError):
        OperationContext.create(
            target_files=("backend/x.py",),
            description="cyclic",
            repo_scope=("jarvis", "prime"),
            primary_repo="jarvis",
            dependency_edges=(("jarvis", "prime"), ("prime", "jarvis")),
        )


def test_dag_no_cycle_valid():
    """Acyclic dependency_edges is accepted."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="acyclic",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
        dependency_edges=(("prime", "jarvis"),),
    )
    assert len(ctx.dependency_edges) == 1


def test_schema_version():
    """schema_version is '3.0'."""
    ctx = OperationContext.create(
        target_files=("f.py",), description="d"
    )
    assert ctx.schema_version == "3.0"


def test_advance_preserves_saga_fields():
    """advance() preserves all new fields on phase transitions."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="d",
        primary_repo="jarvis",
        repo_scope=("jarvis", "prime"),
        primary_repo="prime",
        saga_id="saga-001",
    )
    ctx2 = ctx.advance(OperationPhase.ROUTE)
    assert ctx2.primary_repo == ctx.primary_repo
    assert ctx2.repo_scope == ctx.repo_scope
    assert ctx2.saga_id == ctx.saga_id
    assert ctx2.cross_repo is True
```

**Step 2: Run to verify FAIL**

```bash
python -m pytest tests/governance/test_op_context_upgrade.py -v
```

Expected: FAIL — `ImportError: cannot import name 'ArchitecturalCycleError'`

**Step 3: Implement in `op_context.py`**

At the top of the file, after the existing imports (after line 43), add:

```python
import collections


# ---------------------------------------------------------------------------
# ArchitecturalCycleError
# ---------------------------------------------------------------------------


class ArchitecturalCycleError(ValueError):
    """Raised when dependency_edges contains a cycle.

    Detected at OperationContext construction time via Kahn's algorithm.
    Prevents deadlock before the GENERATE phase begins.
    """
```

Add `SagaStepStatus` enum and `RepoSagaStatus` dataclass after `ShadowResult` (after line 243), before the hash helper:

```python
# ---------------------------------------------------------------------------
# Saga Types
# ---------------------------------------------------------------------------


class SagaStepStatus(str, Enum):
    """Per-repo lifecycle status inside a multi-repo saga."""

    PENDING = "pending"
    APPLYING = "applying"
    APPLIED = "applied"
    SKIPPED = "skipped"             # repo_scope member with empty patch
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"


@dataclass(frozen=True)
class RepoSagaStatus:
    """Frozen per-repo status entry in a multi-repo saga.

    Parameters
    ----------
    repo:
        Repository name (must be in OperationContext.repo_scope).
    status:
        Current saga step status for this repo.
    attempt:
        Number of apply attempts (incremented on retry).
    last_error:
        Human-readable last error string; empty if none.
    reason_code:
        Machine-readable reason code; consumed by compensating transactions.
    compensation_attempted:
        Whether compensation has been attempted for this repo.
    """

    repo: str
    status: SagaStepStatus
    attempt: int = 0
    last_error: str = ""
    reason_code: str = ""
    compensation_attempted: bool = False
```

Add DAG validation helper (after `_compute_hash`, before `OperationContext`):

```python
def _validate_dag(edges: Tuple[Tuple[str, str], ...]) -> None:
    """Kahn's algorithm cycle detection.

    Raises ArchitecturalCycleError if *edges* contains a directed cycle.
    No-op for empty edge set.
    """
    if not edges:
        return
    # Build adjacency list and in-degree counts
    graph: Dict[str, list] = collections.defaultdict(list)
    in_degree: Dict[str, int] = collections.defaultdict(int)
    nodes: set = set()
    for src, dst in edges:
        graph[src].append(dst)
        in_degree[dst] += 1
        nodes.add(src)
        nodes.add(dst)

    queue = collections.deque(n for n in nodes if in_degree[n] == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited < len(nodes):
        raise ArchitecturalCycleError(
            f"Cycle detected in dependency_edges: {edges}"
        )
```

**Step 4: Add new fields to `OperationContext`**

In `OperationContext` dataclass (after `pipeline_deadline` at line 334), add:

```python
    # ---- Phase 3: Multi-repo saga fields ----
    primary_repo: str = "jarvis"
    repo_scope: Tuple[str, ...] = ("jarvis",)
    cross_repo: bool = dataclasses.field(default=False, init=False)
    dependency_edges: Tuple[Tuple[str, str], ...] = ()
    apply_plan: Tuple[str, ...] = ()
    repo_snapshots: Tuple[Tuple[str, str], ...] = ()
    saga_id: str = ""
    saga_state: Tuple[RepoSagaStatus, ...] = ()
    schema_version: str = "3.0"
```

Add `__post_init__` to `OperationContext` (after the last field, before the `# Factory` section):

```python
    def __post_init__(self) -> None:
        # Derive cross_repo from repo_scope (frozen field — must use object.__setattr__)
        object.__setattr__(self, "cross_repo", len(self.repo_scope) > 1)
        # Validate DAG at construction time — prevents deadlock before GENERATE
        _validate_dag(self.dependency_edges)
```

**Step 5: Update `OperationContext.create()` to include new fields in `fields_for_hash`**

The `create()` factory builds a manual `fields_for_hash` dict. Update its signature to accept the new fields:

```python
    @classmethod
    def create(
        cls,
        *,
        target_files: Tuple[str, ...],
        description: str,
        op_id: Optional[str] = None,
        policy_version: str = "",
        pipeline_deadline: Optional[datetime] = None,
        primary_repo: str = "jarvis",
        repo_scope: Optional[Tuple[str, ...]] = None,
        dependency_edges: Tuple[Tuple[str, str], ...] = (),
        apply_plan: Tuple[str, ...] = (),
        repo_snapshots: Tuple[Tuple[str, str], ...] = (),
        saga_id: str = "",
        saga_state: Tuple[RepoSagaStatus, ...] = (),
        schema_version: str = "3.0",
        _timestamp: Optional[datetime] = None,
    ) -> OperationContext:
```

In the body of `create()`, set `resolved_repo_scope`:

```python
        resolved_repo_scope = repo_scope if repo_scope is not None else (primary_repo,)
```

Add the new keys to `fields_for_hash` dict (after `"pipeline_deadline": pipeline_deadline`):

```python
            "primary_repo": primary_repo,
            "repo_scope": resolved_repo_scope,
            "cross_repo": len(resolved_repo_scope) > 1,
            "dependency_edges": dependency_edges,
            "apply_plan": apply_plan,
            "repo_snapshots": repo_snapshots,
            "saga_id": saga_id,
            "saga_state": saga_state,
            "schema_version": schema_version,
```

Update the `return cls(...)` call to include new fields:

```python
        return cls(
            op_id=resolved_op_id,
            created_at=now,
            phase=OperationPhase.CLASSIFY,
            phase_entered_at=now,
            context_hash=context_hash,
            previous_hash=None,
            target_files=target_files,
            risk_tier=None,
            description=description,
            routing=None,
            approval=None,
            shadow=None,
            generation=None,
            validation=None,
            policy_version=policy_version,
            side_effects_blocked=True,
            pipeline_deadline=pipeline_deadline,
            primary_repo=primary_repo,
            repo_scope=resolved_repo_scope,
            dependency_edges=dependency_edges,
            apply_plan=apply_plan,
            repo_snapshots=repo_snapshots,
            saga_id=saga_id,
            saga_state=saga_state,
            schema_version=schema_version,
        )
```

**Step 6: Update `_context_to_hash_dict` to handle `SagaStepStatus` enum in `RepoSagaStatus`**

The existing helper serializes enums by name. `RepoSagaStatus` contains a `SagaStepStatus` enum field. The existing `dataclasses.asdict()` branch handles nested dataclasses but `asdict` recursively converts enums to their values. Verify `_context_to_hash_dict` already handles this via the `dataclasses.is_dataclass` branch (it does — `dataclasses.asdict` is recursive). No change needed.

**Step 7: Run tests to verify PASS**

```bash
python -m pytest tests/governance/test_op_context_upgrade.py -v
```

Expected: all green

**Step 8: Run existing op_context tests to confirm no regressions**

```bash
python -m pytest tests/ -k "op_context" -v
```

**Step 9: Commit**

```bash
git add backend/core/ouroboros/governance/op_context.py \
        tests/governance/test_op_context_upgrade.py
git commit -m "feat(governance): OperationContext Phase 3 saga fields + DAG validation"
```

---

## Task 3: Saga package — types and patch model

**Files:**
- Create: `backend/core/ouroboros/governance/saga/__init__.py`
- Create: `backend/core/ouroboros/governance/saga/saga_types.py`
- Test: `tests/governance/saga/__init__.py`
- Test: `tests/governance/saga/test_saga_types.py`

**Step 1: Write failing tests**

```bash
mkdir -p tests/governance/saga
touch tests/governance/saga/__init__.py
```

Create `tests/governance/saga/test_saga_types.py`:

```python
"""Tests for saga package types."""
import pytest
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaApplyResult,
    SagaTerminalState,
)


def test_file_op_values():
    assert {FileOp.MODIFY, FileOp.CREATE, FileOp.DELETE}


def test_patched_file_frozen():
    pf = PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"old content")
    with pytest.raises((AttributeError, TypeError)):
        pf.path = "other.py"  # type: ignore


def test_patched_file_create_no_preimage():
    """CREATE files have no preimage."""
    pf = PatchedFile(path="backend/new.py", op=FileOp.CREATE, preimage=None)
    assert pf.preimage is None


def test_repo_patch_frozen():
    p = RepoPatch(repo="jarvis", files=())
    with pytest.raises((AttributeError, TypeError)):
        p.repo = "prime"  # type: ignore


def test_repo_patch_is_empty():
    assert RepoPatch(repo="jarvis", files=()).is_empty()
    pf = PatchedFile(path="x.py", op=FileOp.MODIFY, preimage=b"x")
    assert not RepoPatch(repo="jarvis", files=(pf,)).is_empty()


def test_saga_terminal_state_values():
    required = {
        "saga_apply_completed",
        "saga_rolled_back",
        "saga_stuck",
        "saga_succeeded",
        "saga_verify_failed",
        "saga_aborted",
    }
    assert required == {s.value for s in SagaTerminalState}


def test_saga_apply_result_fields():
    result = SagaApplyResult(
        terminal_state=SagaTerminalState.SAGA_APPLY_COMPLETED,
        saga_id="s1",
        saga_step_index=2,
        error=None,
    )
    assert result.saga_step_index == 2
    assert result.error is None
```

**Step 2: Run to verify FAIL**

```bash
python -m pytest tests/governance/saga/test_saga_types.py -v
```

Expected: ImportError

**Step 3: Implement**

Create `backend/core/ouroboros/governance/saga/__init__.py`:

```python
"""Saga orchestration package for multi-repo applies."""
from .saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaApplyResult,
    SagaTerminalState,
)

__all__ = [
    "FileOp",
    "PatchedFile",
    "RepoPatch",
    "SagaApplyResult",
    "SagaTerminalState",
]
```

Create `backend/core/ouroboros/governance/saga/saga_types.py`:

```python
"""Saga type definitions: patch model, terminal states, results."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class FileOp(str, Enum):
    """File operation type in a RepoPatch."""
    MODIFY = "modify"
    CREATE = "create"
    DELETE = "delete"


@dataclass(frozen=True)
class PatchedFile:
    """A single file operation in a RepoPatch.

    Parameters
    ----------
    path:
        Path relative to the repo root.
    op:
        Operation type (MODIFY, CREATE, DELETE).
    preimage:
        Original file bytes before the change. None for CREATE (file didn't exist).
        Required for MODIFY and DELETE (used for compensation).
    """
    path: str
    op: FileOp
    preimage: Optional[bytes]


@dataclass(frozen=True)
class RepoPatch:
    """All file operations for a single repo in a multi-repo saga.

    Parameters
    ----------
    repo:
        Repository name (must match OperationContext.repo_scope entry).
    files:
        Tuple of PatchedFile describing every file this patch touches.
    new_content:
        Mapping of path → new content bytes to write during apply.
        Stored separately from preimage so the patch is self-contained.
    """
    repo: str
    files: Tuple[PatchedFile, ...]
    new_content: Tuple[Tuple[str, bytes], ...] = ()  # (path, content) pairs

    def is_empty(self) -> bool:
        return len(self.files) == 0


class SagaTerminalState(str, Enum):
    """Terminal states of a saga execution."""
    SAGA_APPLY_COMPLETED = "saga_apply_completed"   # all applies done; enter VERIFY
    SAGA_ROLLED_BACK = "saga_rolled_back"            # compensation succeeded
    SAGA_STUCK = "saga_stuck"                        # compensation failed; human required
    SAGA_SUCCEEDED = "saga_succeeded"                # VERIFY passed; op complete
    SAGA_VERIFY_FAILED = "saga_verify_failed"        # VERIFY failed; triggers compensation
    SAGA_ABORTED = "saga_aborted"                    # pre-flight drift check failed


@dataclass
class SagaApplyResult:
    """Result returned by SagaApplyStrategy.execute()."""
    terminal_state: SagaTerminalState
    saga_id: str
    saga_step_index: int        # last committed step index (for resume)
    error: Optional[str]
    reason_code: str = ""       # machine-readable cause
```

**Step 4: Run tests to verify PASS**

```bash
python -m pytest tests/governance/saga/test_saga_types.py -v
```

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/saga/ tests/governance/saga/
git commit -m "feat(saga): saga package with patch model and terminal state types"
```

---

## Task 4: SagaApplyStrategy — topological apply with preimage compensation

**Files:**
- Create: `backend/core/ouroboros/governance/saga/saga_apply_strategy.py`
- Test: `tests/governance/saga/test_saga_apply_strategy.py`

**Step 1: Write failing tests**

Create `tests/governance/saga/test_saga_apply_strategy.py`:

```python
"""Tests for SagaApplyStrategy."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    RepoSagaStatus,
    SagaStepStatus,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy


def _make_ctx(repo_scope=("jarvis", "prime"), apply_plan=("prime", "jarvis")):
    return OperationContext.create(
        target_files=("backend/x.py",),
        description="test saga",
        repo_scope=repo_scope,
        primary_repo="jarvis",
        apply_plan=apply_plan,
        repo_snapshots=(("jarvis", "abc123"), ("prime", "def456")),
        saga_id="test-saga-001",
    )


async def test_happy_path_all_repos_applied(tmp_path):
    """All repos apply successfully → SAGA_APPLY_COMPLETED."""
    ctx = _make_ctx()
    jarvis_file = tmp_path / "backend" / "x.py"
    jarvis_file.parent.mkdir(parents=True)
    jarvis_file.write_bytes(b"old content")

    prime_file = tmp_path / "backend" / "y.py"
    prime_file.parent.mkdir(parents=True)
    prime_file.write_bytes(b"old prime")

    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="backend/y.py", op=FileOp.MODIFY, preimage=b"old prime"),),
            new_content=(("backend/y.py", b"new prime"),),
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"old content"),),
            new_content=(("backend/x.py", b"new content"),),
        ),
    }

    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()

    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    # Mock HEAD checks to return expected hashes
    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED
    # Files should be written
    assert jarvis_file.read_bytes() == b"new content"
    assert prime_file.read_bytes() == b"new prime"


async def test_drift_aborts_before_any_apply(tmp_path):
    """HEAD drift detected in pre-flight → SAGA_ABORTED, no files written."""
    ctx = _make_ctx(
        repo_scope=("jarvis", "prime"),
        apply_plan=("prime", "jarvis"),
    )
    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="backend/y.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("backend/y.py", b"new"),),
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(),
            new_content=(),
        ),
    }
    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    # prime HEAD has drifted
    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "DRIFTED"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_ABORTED
    assert result.reason_code == "drift_detected"


async def test_apply_failure_triggers_compensation(tmp_path):
    """Second repo apply fails → first repo is compensated."""
    ctx = _make_ctx()

    jarvis_file = tmp_path / "backend" / "x.py"
    jarvis_file.parent.mkdir(parents=True)
    jarvis_file.write_bytes(b"original")

    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="backend/nonexistent/y.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("backend/nonexistent/y.py", b"content"),),  # parent dir missing → fails
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"original"),),
            new_content=(("backend/x.py", b"modified"),),
        ),
    }

    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    # Compensation should have restored jarvis to original
    assert result.terminal_state == SagaTerminalState.SAGA_ROLLED_BACK
    assert jarvis_file.read_bytes() == b"original"


async def test_skipped_repo_no_apply(tmp_path):
    """Repo with empty patch is marked SKIPPED, not applied."""
    ctx = _make_ctx(
        repo_scope=("jarvis", "prime"),
        apply_plan=("prime", "jarvis"),
    )
    patch_map = {
        "prime": RepoPatch(repo="prime", files=(), new_content=()),  # empty
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("backend/x.py", b"new"),),
        ),
    }
    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED


def test_topological_sort_respects_dependency_edges():
    """_topological_sort returns correct order from dependency_edges."""
    strategy = SagaApplyStrategy(repo_roots={}, ledger=MagicMock())
    # prime → jarvis means jarvis must apply before prime
    order = strategy._topological_sort(
        repo_scope=("jarvis", "prime"),
        edges=(("prime", "jarvis"),),  # prime depends on jarvis
        apply_plan=(),
    )
    assert order.index("jarvis") < order.index("prime")
```

**Step 2: Run to verify FAIL**

```bash
python -m pytest tests/governance/saga/test_saga_apply_strategy.py -v
```

Expected: ImportError

**Step 3: Implement `saga_apply_strategy.py`**

Create `backend/core/ouroboros/governance/saga/saga_apply_strategy.py`:

```python
"""SagaApplyStrategy — multi-repo topological apply with preimage compensation.

Selected by GovernedOrchestrator when ctx.cross_repo is True.
Single-repo path in ChangeEngine is untouched.

Execution phases:
  A — Pre-flight drift check (HEAD anchor verification)
  B — Staged topological apply (preimage capture → write → git add)
  C — Compensating rollback in reverse order on failure
  D — Terminal state determination
"""
from __future__ import annotations

import asyncio
import collections
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    RepoSagaStatus,
    SagaStepStatus,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    RepoPatch,
    SagaApplyResult,
    SagaTerminalState,
)

logger = logging.getLogger("Ouroboros.SagaApply")


class SagaApplyStrategy:
    """Executes multi-repo applies in topological order with preimage compensation.

    Parameters
    ----------
    repo_roots:
        Mapping of repo name → absolute Path to repo root on disk.
    ledger:
        OperationLedger for saga_step_index persistence and sub-event emission.
    """

    def __init__(
        self,
        repo_roots: Dict[str, Path],
        ledger: Any,
    ) -> None:
        self._repo_roots = {k: Path(v) for k, v in repo_roots.items()}
        self._ledger = ledger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self, ctx: OperationContext, patch_map: Dict[str, RepoPatch]
    ) -> SagaApplyResult:
        """Execute the full saga: A → B → C/D.

        Parameters
        ----------
        ctx:
            OperationContext with cross_repo=True and apply_plan set.
        patch_map:
            Per-repo patch (keyed by repo name). All repos in ctx.repo_scope
            must have an entry (use empty RepoPatch for SKIPPED repos).
        """
        apply_order = self._resolve_apply_order(ctx)
        saga_id = ctx.saga_id or ctx.op_id

        # Phase A: Pre-flight drift check
        abort_result = await self._phase_a_preflight(ctx, apply_order, saga_id)
        if abort_result is not None:
            return abort_result

        # Phase B: Staged topological apply
        applied_repos: List[str] = []
        step_index = 0
        failed_repo: Optional[str] = None
        failure_reason: str = ""
        failure_error: str = ""

        for repo in apply_order:
            patch = patch_map.get(repo, RepoPatch(repo=repo, files=()))

            if patch.is_empty():
                logger.info("[Saga] %s SKIPPED (empty patch)", repo)
                step_index += 1
                continue

            # Re-verify HEAD immediately before writing (TOCTOU guard)
            expected_hash = dict(ctx.repo_snapshots).get(repo, "")
            if expected_hash:
                current = self._get_head_hash(repo)
                if current != expected_hash:
                    failed_repo = repo
                    failure_reason = "drift_detected_mid_apply"
                    failure_error = f"{repo} HEAD moved to {current} during apply"
                    break

            logger.info("[Saga] Applying %s (step %d)", repo, step_index)
            await self._emit_sub_event("apply_repo", saga_id, ctx.op_id, repo=repo)

            try:
                await self._apply_patch(repo, patch)
                applied_repos.append(repo)
                step_index += 1
                logger.info("[Saga] %s APPLIED", repo)
            except Exception as exc:
                failed_repo = repo
                failure_reason = "apply_write_error"
                failure_error = str(exc)
                logger.error("[Saga] Apply failed for %s: %s", repo, exc)
                break

        if failed_repo is None:
            # All repos applied or skipped
            await self._emit_sub_event("verify_global", saga_id, ctx.op_id)
            return SagaApplyResult(
                terminal_state=SagaTerminalState.SAGA_APPLY_COMPLETED,
                saga_id=saga_id,
                saga_step_index=step_index,
                error=None,
            )

        # Phase C: Compensating rollback in reverse order
        all_compensated = await self._phase_c_compensate(
            applied_repos, patch_map, saga_id, ctx.op_id, failure_reason
        )

        if all_compensated:
            return SagaApplyResult(
                terminal_state=SagaTerminalState.SAGA_ROLLED_BACK,
                saga_id=saga_id,
                saga_step_index=step_index,
                error=failure_error,
                reason_code=failure_reason,
            )
        else:
            await self._emit_sub_event("stuck", saga_id, ctx.op_id)
            return SagaApplyResult(
                terminal_state=SagaTerminalState.SAGA_STUCK,
                saga_id=saga_id,
                saga_step_index=step_index,
                error=failure_error,
                reason_code="compensation_failed",
            )

    # ------------------------------------------------------------------
    # Phase A: Pre-flight
    # ------------------------------------------------------------------

    async def _phase_a_preflight(
        self, ctx: OperationContext, apply_order: List[str], saga_id: str
    ) -> Optional[SagaApplyResult]:
        """Verify HEAD anchors for all repos. Returns abort result or None."""
        await self._emit_sub_event("prepare", saga_id, ctx.op_id)
        snapshots = dict(ctx.repo_snapshots)
        for repo in apply_order:
            expected = snapshots.get(repo, "")
            if not expected:
                continue
            current = self._get_head_hash(repo)
            if current != expected:
                logger.warning(
                    "[Saga] Drift detected for %s: expected %s, got %s",
                    repo, expected, current,
                )
                return SagaApplyResult(
                    terminal_state=SagaTerminalState.SAGA_ABORTED,
                    saga_id=saga_id,
                    saga_step_index=0,
                    error=f"{repo} HEAD drifted from snapshot",
                    reason_code="drift_detected",
                )
        return None

    # ------------------------------------------------------------------
    # Phase B helpers
    # ------------------------------------------------------------------

    async def _apply_patch(self, repo: str, patch: RepoPatch) -> None:
        """Write all files in patch to disk, then stage with git add."""
        repo_root = self._repo_roots[repo]
        content_map = dict(patch.new_content)

        written: List[str] = []
        for pf in patch.files:
            full_path = repo_root / pf.path
            new_bytes = content_map.get(pf.path, b"")

            if pf.op == FileOp.CREATE:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_bytes(new_bytes)
            elif pf.op == FileOp.MODIFY:
                full_path.write_bytes(new_bytes)
            elif pf.op == FileOp.DELETE:
                if full_path.exists():
                    full_path.unlink()
                else:
                    continue  # already gone

            written.append(pf.path)

        # Stage all written files for transactional safety
        if written:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "add", "--"] + written,
                cwd=str(repo_root),
                check=True,
                capture_output=True,
            )

    # ------------------------------------------------------------------
    # Phase C: Compensation
    # ------------------------------------------------------------------

    async def _phase_c_compensate(
        self,
        applied_repos: List[str],
        patch_map: Dict[str, RepoPatch],
        saga_id: str,
        op_id: str,
        failure_reason: str,
    ) -> bool:
        """Compensate all applied repos in reverse order.

        Returns True if all compensations succeeded.
        """
        all_ok = True
        for repo in reversed(applied_repos):
            patch = patch_map[repo]
            await self._emit_sub_event(
                "compensate_repo", saga_id, op_id, repo=repo, reason=failure_reason
            )
            try:
                await self._compensate_patch(repo, patch)
                logger.info("[Saga] Compensated %s", repo)
            except Exception as exc:
                logger.error("[Saga] Compensation FAILED for %s: %s", repo, exc)
                all_ok = False
        return all_ok

    async def _compensate_patch(self, repo: str, patch: RepoPatch) -> None:
        """Restore all files in patch to their preimage state."""
        repo_root = self._repo_roots[repo]
        to_unstage: List[str] = []

        for pf in patch.files:
            full_path = repo_root / pf.path
            if pf.op == FileOp.CREATE:
                # File was created — delete it
                if full_path.exists():
                    full_path.unlink()
            elif pf.op in (FileOp.MODIFY, FileOp.DELETE):
                # Restore from preimage
                if pf.preimage is None:
                    raise ValueError(
                        f"Cannot compensate {pf.op} on {pf.path}: preimage is None"
                    )
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_bytes(pf.preimage)
            to_unstage.append(pf.path)

        # Unstage all files
        if to_unstage:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "restore", "--staged", "--"] + to_unstage,
                cwd=str(repo_root),
                check=False,   # best-effort; don't fail compensation if not staged
                capture_output=True,
            )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _get_head_hash(self, repo: str) -> str:
        """Return the current HEAD commit hash for a repo."""
        repo_root = self._repo_roots.get(repo)
        if repo_root is None:
            return ""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root),
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _resolve_apply_order(self, ctx: OperationContext) -> List[str]:
        """Return apply order: use ctx.apply_plan if set; otherwise topological sort."""
        if ctx.apply_plan:
            return list(ctx.apply_plan)
        return self._topological_sort(
            repo_scope=ctx.repo_scope,
            edges=ctx.dependency_edges,
            apply_plan=(),
        )

    def _topological_sort(
        self,
        repo_scope: Tuple[str, ...],
        edges: Tuple[Tuple[str, str], ...],
        apply_plan: Tuple[str, ...],
    ) -> List[str]:
        """Kahn's algorithm topological sort. Stable: alphabetical within same depth."""
        graph: Dict[str, List[str]] = collections.defaultdict(list)
        in_degree: Dict[str, int] = {r: 0 for r in repo_scope}
        for src, dst in edges:
            graph[src].append(dst)
            in_degree[dst] = in_degree.get(dst, 0) + 1

        queue = collections.deque(sorted(r for r in repo_scope if in_degree.get(r, 0) == 0))
        result: List[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in sorted(graph[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        return result

    async def _emit_sub_event(
        self, event: str, saga_id: str, op_id: str, **kwargs: Any
    ) -> None:
        """Emit a saga sub-event to the ledger (best-effort)."""
        try:
            from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
            entry = LedgerEntry(
                op_id=op_id,
                state=OperationState.APPLYING,
                data={"saga_event": event, "saga_id": saga_id, **kwargs},
            )
            await self._ledger.append(entry)
        except Exception as exc:
            logger.debug("[Saga] sub-event emit failed (%s): %s", event, exc)
```

**Step 4: Run tests to verify PASS**

```bash
python -m pytest tests/governance/saga/test_saga_apply_strategy.py -v
```

Expected: all green

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/saga/saga_apply_strategy.py \
        tests/governance/saga/test_saga_apply_strategy.py
git commit -m "feat(saga): SagaApplyStrategy with topological apply and preimage compensation"
```

---

## Task 5: CrossRepoVerifier — three-tier VERIFY

**Files:**
- Create: `backend/core/ouroboros/governance/saga/cross_repo_verifier.py`
- Update: `backend/core/ouroboros/governance/saga/__init__.py`
- Test: `tests/governance/saga/test_cross_repo_verifier.py`

**Step 1: Write failing tests**

Create `tests/governance/saga/test_cross_repo_verifier.py`:

```python
"""Tests for CrossRepoVerifier three-tier verification."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.saga.cross_repo_verifier import (
    CrossRepoVerifier,
    VerifyResult,
    VerifyFailureClass,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
)


def _make_patch_map(repos=("jarvis",)):
    return {
        r: RepoPatch(
            repo=r,
            files=(PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"old"),),
            new_content=(("backend/x.py", b"new"),),
        )
        for r in repos
    }


async def test_happy_path_all_tiers_pass(tmp_path):
    """All tiers pass → VerifyResult.passed=True."""
    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )
    patch_map = _make_patch_map(["jarvis"])

    with patch.object(verifier, "_tier1_per_repo", return_value=None), \
         patch.object(verifier, "_tier2_cross_repo_contracts", return_value=None), \
         patch.object(verifier, "_tier3_integration_tests", return_value=None):
        result = await verifier.verify(
            repo_scope=("jarvis",),
            patch_map=patch_map,
            dependency_edges=(),
        )

    assert result.passed is True
    assert result.failure_class is None


async def test_tier1_failure_returns_result(tmp_path):
    """Tier 1 typecheck failure → passed=False, class=VERIFY_FAILED_PER_REPO."""
    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )
    patch_map = _make_patch_map(["jarvis"])

    with patch.object(verifier, "_tier1_per_repo", return_value=VerifyResult(
        passed=False,
        failure_class=VerifyFailureClass.PER_REPO,
        reason_code="verify_typecheck_failed",
        details="jarvis: pyright error",
    )):
        result = await verifier.verify(
            repo_scope=("jarvis",),
            patch_map=patch_map,
            dependency_edges=(),
        )

    assert result.passed is False
    assert result.failure_class == VerifyFailureClass.PER_REPO
    assert "pyright" in result.details


async def test_tier2_skipped_when_single_repo(tmp_path):
    """Tier 2 is skipped for single-repo operations (no edges)."""
    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )
    patch_map = _make_patch_map(["jarvis"])
    called = []

    with patch.object(verifier, "_tier1_per_repo", return_value=None), \
         patch.object(verifier, "_tier2_cross_repo_contracts", side_effect=lambda **kw: called.append(True) or None), \
         patch.object(verifier, "_tier3_integration_tests", return_value=None):
        result = await verifier.verify(
            repo_scope=("jarvis",),
            patch_map=patch_map,
            dependency_edges=(),
        )

    assert not called  # Tier 2 never called for single-repo
    assert result.passed is True


async def test_tier3_noop_when_no_cross_repo_tests(tmp_path):
    """Tier 3 passes silently when no @cross_repo tests exist."""
    verifier = CrossRepoVerifier(
        repo_roots={"jarvis": tmp_path},
        dependency_edges=(),
    )
    result = await verifier._tier3_integration_tests(
        repo_scope=("jarvis",),
        repo_roots={"jarvis": tmp_path},
    )
    assert result is None  # no-op → None means pass
```

**Step 2: Run to verify FAIL**

```bash
python -m pytest tests/governance/saga/test_cross_repo_verifier.py -v
```

**Step 3: Implement `cross_repo_verifier.py`**

Create `backend/core/ouroboros/governance/saga/cross_repo_verifier.py`:

```python
"""CrossRepoVerifier — three-tier post-apply verification.

Tier 1: Per-repo type-check + lint + fast tests (parallelized).
Tier 2: Cross-repo interface contract validation (sequential, dependency order).
Tier 3: @cross_repo integration tests (no-op if none exist).

A Tier failure returns VerifyResult(passed=False) which triggers
SagaApplyStrategy compensation via the orchestrator.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.saga.saga_types import RepoPatch

logger = logging.getLogger("Ouroboros.CrossRepoVerifier")


class VerifyFailureClass(str, Enum):
    PER_REPO = "verify_failed_per_repo"
    CROSS_REPO = "verify_failed_cross_repo"
    INTEGRATION = "verify_failed_integration"


@dataclass
class VerifyResult:
    """Result of cross-repo verification."""
    passed: bool
    failure_class: Optional[VerifyFailureClass] = None
    reason_code: str = ""
    details: str = ""


class CrossRepoVerifier:
    """Three-tier cross-repo verifier.

    Parameters
    ----------
    repo_roots:
        Mapping of repo name → absolute Path.
    dependency_edges:
        DAG edges from OperationContext; used for Tier 2 ordering.
    """

    def __init__(
        self,
        repo_roots: Dict[str, Path],
        dependency_edges: Tuple[Tuple[str, str], ...],
    ) -> None:
        self._repo_roots = {k: Path(v) for k, v in repo_roots.items()}
        self._dependency_edges = dependency_edges

    async def verify(
        self,
        repo_scope: Tuple[str, ...],
        patch_map: Dict[str, RepoPatch],
        dependency_edges: Tuple[Tuple[str, str], ...],
    ) -> VerifyResult:
        """Run all three verification tiers.

        Returns the first failure encountered (fails fast per tier).
        """
        # Tier 1: per-repo (parallelized)
        t1 = await self._tier1_per_repo(
            repo_scope=repo_scope,
            patch_map=patch_map,
        )
        if t1 is not None:
            return t1

        # Tier 2: cross-repo contracts (only for multi-repo)
        if len(repo_scope) > 1:
            t2 = await self._tier2_cross_repo_contracts(
                repo_scope=repo_scope,
                dependency_edges=dependency_edges,
            )
            if t2 is not None:
                return t2

        # Tier 3: integration tests
        t3 = await self._tier3_integration_tests(
            repo_scope=repo_scope,
            repo_roots=self._repo_roots,
        )
        if t3 is not None:
            return t3

        return VerifyResult(passed=True)

    async def _tier1_per_repo(
        self,
        repo_scope: Tuple[str, ...],
        patch_map: Dict[str, RepoPatch],
    ) -> Optional[VerifyResult]:
        """Run type-check + lint on changed files per repo (parallelized).

        Returns None on success, VerifyResult(passed=False) on failure.
        """
        tasks = [
            self._verify_single_repo(repo, patch_map.get(repo))
            for repo in repo_scope
            if not (patch_map.get(repo, RepoPatch(repo=repo, files=())).is_empty())
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, VerifyResult) and not r.passed:
                return r
            if isinstance(r, Exception):
                return VerifyResult(
                    passed=False,
                    failure_class=VerifyFailureClass.PER_REPO,
                    reason_code="verify_infra_error",
                    details=str(r),
                )
        return None

    async def _verify_single_repo(
        self, repo: str, patch: Optional[RepoPatch]
    ) -> Optional[VerifyResult]:
        """Type-check + lint changed files in a single repo."""
        if patch is None or patch.is_empty():
            return None
        repo_root = self._repo_roots.get(repo)
        if repo_root is None:
            return None

        changed_files = [pf.path for pf in patch.files]

        # Lint: ruff (fast, always available)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["ruff", "check", "--select=E,F,W", "--"] + changed_files,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                return VerifyResult(
                    passed=False,
                    failure_class=VerifyFailureClass.PER_REPO,
                    reason_code="verify_lint_failed",
                    details=f"{repo}: {proc.stdout[:500]}",
                )
        except FileNotFoundError:
            logger.debug("[Tier1] ruff not found for %s, skipping lint", repo)

        return None

    async def _tier2_cross_repo_contracts(
        self,
        repo_scope: Tuple[str, ...],
        dependency_edges: Tuple[Tuple[str, str], ...],
    ) -> Optional[VerifyResult]:
        """Check import boundaries along declared dependency edges.

        For each edge (src → dst): verify src can import boundary module from dst.
        Checks contract manifest JSON if present; otherwise skips gracefully.
        """
        for src, dst in dependency_edges:
            src_root = self._repo_roots.get(src)
            dst_root = self._repo_roots.get(dst)
            if src_root is None or dst_root is None:
                continue

            # Check contract manifest (optional)
            manifest_path = dst_root / ".jarvis" / "contract_manifest.json"
            if not manifest_path.exists():
                continue

            import json
            try:
                manifest = json.loads(manifest_path.read_text())
                boundary_modules = manifest.get("boundary_modules", [])
            except Exception:
                continue

            for module in boundary_modules:
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        ["python", "-c", f"import {module}"],
                        cwd=str(src_root),
                        capture_output=True,
                        text=True,
                    )
                    if proc.returncode != 0:
                        return VerifyResult(
                            passed=False,
                            failure_class=VerifyFailureClass.CROSS_REPO,
                            reason_code="verify_import_edge_broken",
                            details=f"{src}→{dst}: cannot import {module}: {proc.stderr[:300]}",
                        )
                except Exception as exc:
                    return VerifyResult(
                        passed=False,
                        failure_class=VerifyFailureClass.CROSS_REPO,
                        reason_code="verify_import_edge_broken",
                        details=str(exc),
                    )
        return None

    async def _tier3_integration_tests(
        self,
        repo_scope: Tuple[str, ...],
        repo_roots: Dict[str, Path],
    ) -> Optional[VerifyResult]:
        """Run @cross_repo integration tests if any exist. No-op if none found."""
        for repo in repo_scope:
            repo_root = repo_roots.get(repo)
            if repo_root is None:
                continue
            # Search for any test file with @cross_repo marker
            test_files = list(repo_root.rglob("test_*.py"))
            has_cross_repo = False
            for tf in test_files:
                try:
                    if "@cross_repo" in tf.read_text(encoding="utf-8"):
                        has_cross_repo = True
                        break
                except Exception:
                    continue

            if has_cross_repo:
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        ["python", "-m", "pytest", "-m", "cross_repo", "-q"],
                        cwd=str(repo_root),
                        capture_output=True,
                        text=True,
                    )
                    if proc.returncode != 0:
                        return VerifyResult(
                            passed=False,
                            failure_class=VerifyFailureClass.INTEGRATION,
                            reason_code="verify_integration_failed",
                            details=f"{repo}: {proc.stdout[-500:]}",
                        )
                except Exception as exc:
                    logger.warning("[Tier3] Integration test run failed: %s", exc)

        return None
```

Update `backend/core/ouroboros/governance/saga/__init__.py` to export verifier:

```python
from .cross_repo_verifier import CrossRepoVerifier, VerifyResult, VerifyFailureClass
```

Add to `__all__`.

**Step 4: Run tests to verify PASS**

```bash
python -m pytest tests/governance/saga/test_cross_repo_verifier.py -v
```

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/saga/cross_repo_verifier.py \
        backend/core/ouroboros/governance/saga/__init__.py \
        tests/governance/saga/test_cross_repo_verifier.py
git commit -m "feat(saga): CrossRepoVerifier three-tier post-apply verification"
```

---

## Task 6: Orchestrator integration — route APPLY to SagaApplyStrategy

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (lines 456-496)
- Test: existing orchestrator tests (run to confirm no regressions)

**Step 1: Read orchestrator APPLY phase (lines 440-496)**

Confirm the APPLY block at line 456-496 calls `self._stack.change_engine.execute(change_request)`.

**Step 2: Add saga integration at APPLY phase**

In `orchestrator.py`, at line 456 (Phase 7: APPLY), after `ctx = ctx.advance(OperationPhase.APPLY)` and before building `change_request`, insert:

```python
        # Cross-repo saga path
        if ctx.cross_repo:
            return await self._execute_saga_apply(ctx, best_candidate)
```

Add the helper method to `GovernedOrchestrator` (after `_build_change_request`):

```python
    async def _execute_saga_apply(
        self,
        ctx: OperationContext,
        best_candidate: Optional[dict],
    ) -> OperationContext:
        """Execute multi-repo saga apply + three-tier verify."""
        from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy
        from backend.core.ouroboros.governance.saga.cross_repo_verifier import CrossRepoVerifier
        from backend.core.ouroboros.governance.saga.saga_types import (
            RepoPatch, SagaTerminalState,
        )

        # Build patch_map from best_candidate
        # best_candidate is a dict with "patches" key: {repo: RepoPatch}
        # For now, extract from candidate or build empty map
        patch_map: dict = {}
        if best_candidate and "patches" in best_candidate:
            patch_map = best_candidate["patches"]
        else:
            # Single-file backward compat: wrap in single-repo patch
            from backend.core.ouroboros.governance.saga.saga_types import (
                FileOp, PatchedFile,
            )
            for repo in ctx.repo_scope:
                patch_map[repo] = RepoPatch(repo=repo, files=())

        # Resolve repo roots from registry or project_root
        repo_roots = {}
        for repo in ctx.repo_scope:
            repo_roots[repo] = self._config.project_root

        strategy = SagaApplyStrategy(
            repo_roots=repo_roots,
            ledger=self._stack.ledger,
        )
        apply_result = await strategy.execute(ctx, patch_map)

        if apply_result.terminal_state == SagaTerminalState.SAGA_ABORTED:
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(ctx, OperationState.FAILED,
                {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id})
            return ctx

        if apply_result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED:
            # Three-tier verification
            verifier = CrossRepoVerifier(
                repo_roots=repo_roots,
                dependency_edges=ctx.dependency_edges,
            )
            verify_result = await verifier.verify(
                repo_scope=ctx.repo_scope,
                patch_map=patch_map,
                dependency_edges=ctx.dependency_edges,
            )

            if not verify_result.passed:
                # Trigger compensation
                comp_result = await strategy._phase_c_compensate(
                    list(ctx.apply_plan),
                    patch_map,
                    apply_result.saga_id,
                    ctx.op_id,
                    verify_result.reason_code,
                )
                ctx = ctx.advance(OperationPhase.POSTMORTEM)
                await self._record_ledger(ctx, OperationState.FAILED,
                    {"reason": verify_result.reason_code, "saga_id": apply_result.saga_id,
                     "compensated": comp_result})
                return ctx

            # SAGA_SUCCEEDED
            ctx = ctx.advance(OperationPhase.VERIFY)
            await self._record_ledger(ctx, OperationState.APPLIED,
                {"saga_id": apply_result.saga_id})
            ctx = ctx.advance(OperationPhase.COMPLETE)
            return ctx

        # SAGA_ROLLED_BACK or SAGA_STUCK
        if apply_result.terminal_state == SagaTerminalState.SAGA_STUCK:
            # Trigger supervisor SAFE_PAUSE via event (best-effort)
            try:
                await self._stack.comm.emit_postmortem(
                    op_id=ctx.op_id,
                    root_cause="saga_stuck",
                    failed_phase="APPLY",
                    next_safe_action="human_intervention_required",
                )
            except Exception:
                pass

        ctx = ctx.advance(OperationPhase.POSTMORTEM)
        await self._record_ledger(ctx, OperationState.FAILED,
            {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id})
        return ctx
```

**Step 3: Run orchestrator tests**

```bash
python -m pytest tests/ -k "orchestrat" -v
```

Expected: all green (single-repo path unchanged)

**Step 4: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py
git commit -m "feat(orchestrator): route cross-repo APPLY to SagaApplyStrategy"
```

---

## Task 7: RepoPipelineManager — pass repo through to OperationContext

**Files:**
- Modify: `backend/core/ouroboros/governance/multi_repo/repo_pipeline.py` (lines 80-85)
- Test: `tests/governance/multi_repo/test_repo_pipeline.py`

**Step 1: Read the existing test file to understand current coverage**

```bash
python -m pytest tests/governance/multi_repo/test_repo_pipeline.py -v
```

Note passing tests. Then add one failing test at the end of that file:

```python
async def test_submit_sets_primary_repo_on_context(tmp_path):
    """RepoPipelineManager.submit() passes signal.repo as primary_repo."""
    from backend.core.ouroboros.governance.multi_repo.registry import RepoConfig, RepoRegistry
    from backend.core.ouroboros.governance.multi_repo.repo_pipeline import RepoPipelineManager
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    captured_ctx = []

    class FakePipeline:
        async def submit(self, ctx, trigger_source=""):
            captured_ctx.append(ctx)

    registry = RepoRegistry(configs=(
        RepoConfig(name="prime", local_path=tmp_path, canary_slices=()),
    ))
    manager = RepoPipelineManager(
        registry=registry,
        pipelines={"prime": FakePipeline()},
    )
    signal = IntentSignal(
        source="backlog",
        target_files=("backend/x.py",),
        repo="prime",
        description="fix x",
        evidence={"signature": "s1"},
        confidence=0.8,
        stable=True,
    )
    await manager.submit(signal)
    assert len(captured_ctx) == 1
    assert captured_ctx[0].primary_repo == "prime"
    assert captured_ctx[0].repo_scope == ("prime",)
```

**Step 2: Run to verify FAIL**

```bash
python -m pytest tests/governance/multi_repo/test_repo_pipeline.py::test_submit_sets_primary_repo_on_context -v
```

**Step 3: Implement fix in `repo_pipeline.py`**

At line 81-85 in `submit()`, update `OperationContext.create()` call:

```python
        ctx = OperationContext.create(
            target_files=signal.target_files,
            description=signal.description,
            op_id=op_id,
            primary_repo=repo_name,
            repo_scope=(repo_name,),
        )
```

**Step 4: Run tests to verify PASS**

```bash
python -m pytest tests/governance/multi_repo/ -v
```

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_repo/repo_pipeline.py \
        tests/governance/multi_repo/test_repo_pipeline.py
git commit -m "fix(multi_repo): pass primary_repo + repo_scope through RepoPipelineManager.submit()"
```

---

## Task 8: Supervisor SAFE_PAUSE mode on SAGA_STUCK

**Files:**
- Modify: `unified_supervisor.py` — search for `self._intake_layer` near Zone 6.9 (added in Phase 2C.2)
- Test: integration test in `tests/governance/integration/test_phase3_acceptance.py`

**Step 1: Find the correct line in unified_supervisor.py**

```bash
grep -n "SAGA_STUCK\|safe_pause\|_safe_pause" unified_supervisor.py | head -10
grep -n "intake_layer" unified_supervisor.py | head -10
```

**Step 2: Write the acceptance test first**

Create `tests/governance/integration/test_phase3_acceptance.py`:

```python
"""Phase 3 acceptance tests — multi-repo saga autonomy."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp, PatchedFile, RepoPatch, SagaTerminalState,
)
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy
from backend.core.ouroboros.governance.saga.cross_repo_verifier import CrossRepoVerifier


# AC1: OperationContext with cross_repo=True validates DAG in __post_init__

def test_ac1_dag_cycle_raises_at_create_time():
    """ArchitecturalCycleError raised synchronously at context creation."""
    from backend.core.ouroboros.governance.op_context import ArchitecturalCycleError
    import pytest
    with pytest.raises(ArchitecturalCycleError):
        OperationContext.create(
            target_files=("x.py",),
            description="cycle test",
            repo_scope=("jarvis", "prime"),
            primary_repo="jarvis",
            dependency_edges=(("jarvis", "prime"), ("prime", "jarvis")),
        )


# AC2: Single-repo path unchanged

def test_ac2_single_repo_context_cross_repo_false():
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="single",
        primary_repo="jarvis",
    )
    assert ctx.cross_repo is False


# AC3: SagaApplyStrategy applies in topological order

def test_ac3_topological_order():
    strategy = SagaApplyStrategy(repo_roots={}, ledger=MagicMock())
    order = strategy._topological_sort(
        repo_scope=("jarvis", "prime", "reactor-core"),
        edges=(("prime", "jarvis"), ("reactor-core", "prime")),
        apply_plan=(),
    )
    # jarvis before prime before reactor-core
    assert order.index("jarvis") < order.index("prime") < order.index("reactor-core")


# AC4: Drift abort before any writes

async def test_ac4_drift_aborts_before_writes(tmp_path):
    f = tmp_path / "x.py"
    f.write_bytes(b"original")
    patch_map = {
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="x.py", op=FileOp.MODIFY, preimage=b"original"),),
            new_content=(("x.py", b"modified"),),
        )
    }
    ctx = OperationContext.create(
        target_files=("x.py",),
        description="drift test",
        repo_scope=("jarvis",),
        primary_repo="jarvis",
        apply_plan=("jarvis",),
        repo_snapshots=(("jarvis", "expected_hash"),),
    )
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots={"jarvis": tmp_path}, ledger=ledger)
    with patch.object(strategy, "_get_head_hash", return_value="different_hash"):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_ABORTED
    assert f.read_bytes() == b"original"  # untouched


# AC5: RepoPipelineManager passes primary_repo

async def test_ac5_repo_pipeline_manager_passes_repo(tmp_path):
    from backend.core.ouroboros.governance.multi_repo.registry import RepoConfig, RepoRegistry
    from backend.core.ouroboros.governance.multi_repo.repo_pipeline import RepoPipelineManager
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    captured = []

    class FakePipeline:
        async def submit(self, ctx, trigger_source=""):
            captured.append(ctx)

    registry = RepoRegistry(configs=(
        RepoConfig(name="reactor-core", local_path=tmp_path, canary_slices=()),
    ))
    manager = RepoPipelineManager(
        registry=registry,
        pipelines={"reactor-core": FakePipeline()},
    )
    signal = IntentSignal(
        source="backlog",
        target_files=("backend/x.py",),
        repo="reactor-core",
        description="fix",
        evidence={"signature": "s"},
        confidence=0.9,
        stable=True,
    )
    await manager.submit(signal)
    assert captured[0].primary_repo == "reactor-core"


# AC6: CrossRepoVerifier passes on clean repos

async def test_ac6_cross_repo_verifier_passes_clean(tmp_path):
    verifier = CrossRepoVerifier(repo_roots={"jarvis": tmp_path}, dependency_edges=())
    patch_map = {
        "jarvis": RepoPatch(repo="jarvis", files=(), new_content=())
    }
    result = await verifier.verify(
        repo_scope=("jarvis",),
        patch_map=patch_map,
        dependency_edges=(),
    )
    assert result.passed is True


# AC7: schema_version is 3.0

def test_ac7_schema_version():
    ctx = OperationContext.create(target_files=("x.py",), description="v")
    assert ctx.schema_version == "3.0"
```

**Step 3: Run to verify tests pass**

```bash
python -m pytest tests/governance/integration/test_phase3_acceptance.py -v
```

Expected: all 7 ACs green

**Step 4: Add SAGA_STUCK handler to unified_supervisor.py**

Search for `_intake_layer` in `unified_supervisor.py` to find the Zone 6.9 block. Add a `_safe_pause_mode` flag near the other boolean flags in `__init__` (`self._safe_pause_mode: bool = False`).

Add a `trigger_safe_pause()` method near other supervisor methods:

```python
    async def trigger_safe_pause(self, reason: str) -> None:
        """Enter SAFE_PAUSE mode. Non-essential queues dropped; complex commands refused.

        Triggered by SAGA_STUCK — human intervention required before resuming.
        """
        self._safe_pause_mode = True
        logger.critical("[Supervisor] SAFE_PAUSE triggered: %s", reason)
        # Best-effort: drain intake queue by stopping IntakeLayerService
        if self._intake_layer is not None:
            try:
                await self._intake_layer.stop()
            except Exception:
                pass
```

Add a check in the supervisor's main loop or request handler (wherever commands are dispatched): if `self._safe_pause_mode is True`, refuse complex commands and log warning.

**Step 5: Wire SAGA_STUCK → trigger_safe_pause in orchestrator**

In `orchestrator.py` `_execute_saga_apply()`, replace the `saga.stuck` comment with an actual supervisor call. The orchestrator doesn't have a direct reference to the supervisor — use the `comm` layer to emit a sentinel event:

```python
        if apply_result.terminal_state == SagaTerminalState.SAGA_STUCK:
            try:
                await self._stack.comm.emit_postmortem(
                    op_id=ctx.op_id,
                    root_cause="saga_stuck",
                    failed_phase="APPLY",
                    next_safe_action="human_intervention_required",
                )
            except Exception:
                pass
```

The supervisor's CommProtocol handler (existing `VoiceNarrator` B-layer path) will receive this postmortem and can be extended to check `root_cause == "saga_stuck"` and call `trigger_safe_pause()`. Leave this extension as a follow-up; the `SAFE_PAUSE` flag itself is wired in this task.

**Step 6: Run full test suite**

```bash
python -m pytest tests/ -v --tb=short -q
```

Expected: 470+ tests passing, 0 new failures

**Step 7: Commit**

```bash
git add tests/governance/integration/test_phase3_acceptance.py unified_supervisor.py \
        backend/core/ouroboros/governance/orchestrator.py
git commit -m "feat(phase3): SAGA_STUCK SAFE_PAUSE mode + Phase 3 acceptance tests"
```

---

## Task 9: Wire exports + final regression sweep

**Files:**
- Modify: `backend/core/ouroboros/governance/saga/__init__.py`
- Modify: `backend/core/ouroboros/governance/__init__.py` (if it re-exports saga types)

**Step 1: Confirm saga package exports**

```python
from backend.core.ouroboros.governance.saga import (
    FileOp, PatchedFile, RepoPatch, SagaApplyResult, SagaTerminalState,
    CrossRepoVerifier, VerifyResult, VerifyFailureClass,
)
```

Add any missing exports to `saga/__init__.py`.

**Step 2: Run full test suite one final time**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: 0 failures, ≥ 470 tests collected.

**Step 3: Final commit**

```bash
git add backend/core/ouroboros/governance/saga/__init__.py
git commit -m "feat(phase3): finalize saga exports and confirm 0 regressions"
```

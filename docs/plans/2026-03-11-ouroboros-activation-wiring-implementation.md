# Ouroboros Activation Wiring — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve 4 P0 activation blockers so JARVIS can autonomously self-develop across jarvis/prime/reactor-core with J-Prime generation.

**Architecture:** Config activation (env var), passive SagaMessageBus observer wiring into strategy + orchestrator, TestFailureSensor watcher wiring, and config propagation fixes. No architectural changes — wiring only.

**Tech Stack:** Python 3.12+, asyncio, pytest

**Design doc:** `docs/plans/2026-03-11-ouroboros-activation-wiring-design.md`

---

### Task 1: Add `JARVIS_SAGA_BRANCH_ISOLATION=true` to `.env`

**Files:**
- Modify: `.env`

**Step 1: Read `.env` to find governance section**

Read `.env` and locate the `JARVIS_GOVERNANCE_MODE=governed` line (currently line 182).

**Step 2: Add the env var**

After `JARVIS_GOVERNANCE_MODE=governed`, add:
```
JARVIS_SAGA_BRANCH_ISOLATION=true
```

Also add (for P0-4a forensics branch control):
```
JARVIS_SAGA_KEEP_FORENSICS_BRANCHES=true
```

**Step 3: Verify**

Run: `python3 -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.getenv('JARVIS_SAGA_BRANCH_ISOLATION'))"`
Expected: `true`

**Step 4: Commit**

```bash
git add .env
git commit -m "config(env): activate B+ branch isolation and forensics branches"
```

---

### Task 2: Add new SagaMessageType values for B+ events

**Files:**
- Modify: `backend/core/ouroboros/governance/autonomy/saga_messages.py:42-61`
- Test: `tests/governance/autonomy/test_saga_messages_bplus.py` (create)

**Step 1: Write failing test**

Create `tests/governance/autonomy/test_saga_messages_bplus.py`:

```python
"""Tests for B+ saga message types and schema-pinned payloads."""
from backend.core.ouroboros.governance.autonomy.saga_messages import (
    SagaMessage,
    SagaMessageType,
    MessagePriority,
)


def test_partial_promote_type_exists():
    assert hasattr(SagaMessageType, "SAGA_PARTIAL_PROMOTE")
    assert SagaMessageType.SAGA_PARTIAL_PROMOTE.value == "saga_partial_promote"


def test_target_moved_type_exists():
    assert hasattr(SagaMessageType, "TARGET_MOVED")
    assert SagaMessageType.TARGET_MOVED.value == "target_moved"


def test_ancestry_violation_type_exists():
    assert hasattr(SagaMessageType, "ANCESTRY_VIOLATION")
    assert SagaMessageType.ANCESTRY_VIOLATION.value == "ancestry_violation"


def test_schema_version_in_payload():
    msg = SagaMessage(
        message_type=SagaMessageType.SAGA_CREATED,
        saga_id="test-saga",
        payload={
            "schema_version": "1.0",
            "op_id": "op-001",
            "reason_code": "",
        },
    )
    assert msg.payload["schema_version"] == "1.0"
    assert msg.payload["op_id"] == "op-001"
    d = msg.to_dict()
    assert d["payload"]["schema_version"] == "1.0"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_saga_messages_bplus.py -v`
Expected: FAIL — `AttributeError: SAGA_PARTIAL_PROMOTE`

**Step 3: Add new enum members**

In `backend/core/ouroboros/governance/autonomy/saga_messages.py`, after line 50 (`SAGA_ROLLED_BACK`), add:

```python
    SAGA_PARTIAL_PROMOTE = "saga_partial_promote"
    TARGET_MOVED = "target_moved"
    ANCESTRY_VIOLATION = "ancestry_violation"
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_saga_messages_bplus.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/saga_messages.py tests/governance/autonomy/test_saga_messages_bplus.py
git commit -m "feat(saga-bus): add SAGA_PARTIAL_PROMOTE, TARGET_MOVED, ANCESTRY_VIOLATION message types"
```

---

### Task 3: Wire SagaMessageBus into SagaApplyStrategy as passive observer

**Files:**
- Modify: `backend/core/ouroboros/governance/saga/saga_apply_strategy.py:55-82, 100-120, 130-180, 620-680`
- Test: `tests/test_ouroboros_governance/test_saga_bus_observer.py` (create)

**Context:** Strategy gets an optional `message_bus` parameter. A `_bus_emit()` helper wraps all sends in try/except. Emit at: SAGA_CREATED (prepare), SAGA_ADVANCED (per-repo apply), SAGA_ROLLED_BACK (compensation), SAGA_COMPLETED / SAGA_PARTIAL_PROMOTE / TARGET_MOVED (promote_all).

**Step 1: Write failing test**

Create `tests/test_ouroboros_governance/test_saga_bus_observer.py`:

```python
"""Tests for SagaMessageBus passive observer wiring in SagaApplyStrategy."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Tuple

import pytest

from backend.core.ouroboros.governance.autonomy.saga_messages import (
    SagaMessageBus,
    SagaMessageType,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init", "--no-verify"], cwd=str(path), check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    (path / ".jarvis").mkdir(exist_ok=True)
    return result.stdout.strip()


@pytest.fixture
def bus_and_repos(tmp_path: Path) -> Tuple[SagaMessageBus, Dict[str, Path], Dict[str, str]]:
    bus = SagaMessageBus(max_messages=100)
    roots: Dict[str, Path] = {}
    shas: Dict[str, str] = {}
    for name in ("jarvis", "prime"):
        root = tmp_path / name
        root.mkdir()
        sha = _init_repo(root)
        roots[name] = root
        shas[name] = sha
    return bus, roots, shas


def _make_ctx(repo_scope, repo_snapshots, op_id="bus-test-001"):
    return OperationContext.create(
        target_files=("test.py",),
        description="Bus observer test",
        op_id=op_id,
        repo_scope=repo_scope,
        repo_snapshots=repo_snapshots,
        saga_id=f"saga-{op_id}",
    )


def _make_patch(repo, path="src/new.py", content="# new\n"):
    return RepoPatch(
        repo=repo,
        files=(PatchedFile(path=path, op=FileOp.CREATE, preimage=None),),
        new_content=((path, content.encode()),),
    )


class TestBusReceivesLifecycleEvents:
    async def test_apply_emits_created_and_advanced(self, bus_and_repos) -> None:
        bus, roots, shas = bus_and_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True, message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        result = await strategy.execute(ctx, {"jarvis": _make_patch("jarvis")})
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        msgs = bus.get_messages(saga_id=f"saga-bus-test-001")
        types = [m.message_type for m in msgs]
        assert SagaMessageType.SAGA_CREATED in types
        assert SagaMessageType.SAGA_ADVANCED in types

    async def test_promote_emits_completed(self, bus_and_repos) -> None:
        bus, roots, shas = bus_and_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True, message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        await strategy.execute(ctx, {"jarvis": _make_patch("jarvis")})
        state, promoted = await strategy.promote_all(
            apply_order=["jarvis"], saga_id=f"saga-bus-test-001", op_id="bus-test-001",
        )
        assert state == SagaTerminalState.SAGA_SUCCEEDED

        msgs = bus.get_messages(saga_id=f"saga-bus-test-001")
        types = [m.message_type for m in msgs]
        assert SagaMessageType.SAGA_COMPLETED in types

    async def test_schema_version_in_all_payloads(self, bus_and_repos) -> None:
        bus, roots, shas = bus_and_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True, message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        await strategy.execute(ctx, {"jarvis": _make_patch("jarvis")})

        for msg in bus.get_messages():
            assert msg.payload.get("schema_version") == "1.0", (
                f"Missing schema_version in {msg.message_type}"
            )


class TestBusIsOptional:
    async def test_no_bus_works_fine(self, bus_and_repos) -> None:
        _, roots, shas = bus_and_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True, message_bus=None,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        result = await strategy.execute(ctx, {"jarvis": _make_patch("jarvis")})
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED


class TestBusFailureIsolation:
    async def test_broken_bus_does_not_break_saga(self, bus_and_repos) -> None:
        _, roots, shas = bus_and_repos

        class BrokenBus:
            def send(self, msg):
                raise RuntimeError("bus on fire")

        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True, message_bus=BrokenBus(),
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        result = await strategy.execute(ctx, {"jarvis": _make_patch("jarvis")})
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_saga_bus_observer.py -v`
Expected: FAIL — `TypeError: SagaApplyStrategy.__init__() got an unexpected keyword argument 'message_bus'`

**Step 3: Implement bus wiring in SagaApplyStrategy**

In `backend/core/ouroboros/governance/saga/saga_apply_strategy.py`:

**3a.** Add import at top (after existing saga_types import, around line 30):
```python
from backend.core.ouroboros.governance.autonomy.saga_messages import (
    SagaMessage,
    SagaMessageType,
    MessagePriority,
)
```

Wrap in try/except for import safety:
```python
try:
    from backend.core.ouroboros.governance.autonomy.saga_messages import (
        SagaMessage,
        SagaMessageType,
        MessagePriority,
    )
    _BUS_IMPORTS_OK = True
except ImportError:
    _BUS_IMPORTS_OK = False
```

**3b.** Add `message_bus` parameter to `__init__` (after `keep_failed_saga_branches`):
```python
        message_bus: Any = None,
```
And in the body:
```python
        self._bus = message_bus
```

**3c.** Add `_bus_emit` helper method (after `__init__`, before `execute`):
```python
    def _bus_emit(
        self, msg_type: str, saga_id: str, op_id: str, **kwargs: Any,
    ) -> None:
        """Emit a saga lifecycle event to the message bus (fire-and-forget)."""
        if self._bus is None or not _BUS_IMPORTS_OK:
            return
        try:
            self._bus.send(SagaMessage(
                message_type=SagaMessageType(msg_type),
                saga_id=saga_id,
                source_repo=kwargs.pop("repo", "*"),
                correlation_id=saga_id,
                priority=(
                    MessagePriority.HIGH
                    if "fail" in msg_type or "partial" in msg_type or "moved" in msg_type
                    else MessagePriority.NORMAL
                ),
                payload={
                    "schema_version": "1.0",
                    "op_id": op_id,
                    "saga_id": saga_id,
                    "reason_code": kwargs.get("reason_code", ""),
                    **kwargs,
                },
            ))
        except Exception:
            logger.debug("[Saga] bus emit failed for %s (non-fatal)", msg_type)
```

**3d.** Add `_bus_emit` calls in `_execute_bplus`:
- After ephemeral branches created (after the prepare sub-event): `self._bus_emit("saga_created", saga_id, ctx.op_id)`
- After each successful repo apply: `self._bus_emit("saga_advanced", saga_id, ctx.op_id, repo=repo, step_index=step_index)`
- In the failure branch (before `_bplus_compensate_all`): `self._bus_emit("saga_failed", saga_id, ctx.op_id, repo=failed_repo, reason_code=failure_reason)`
- After `_bplus_compensate_all`: `self._bus_emit("saga_rolled_back", saga_id, ctx.op_id, reason_code=failure_reason)`

**3e.** Add `_bus_emit` calls in `promote_all`:
- After successful promote per repo: `self._bus_emit("saga_advanced", saga_id, op_id, repo=repo, promoted_sha=sha)`
- After full success: `self._bus_emit("saga_completed", saga_id, op_id)`
- On promote failure: `self._bus_emit("saga_partial_promote", saga_id, op_id, repo=repo, reason_code=str(exc))`
- On TARGET_MOVED specifically (check exc message): `self._bus_emit("target_moved", saga_id, op_id, repo=repo, reason_code=str(exc))`

**3f.** Also add `_bus_emit` in `_execute_legacy` at same lifecycle points for consistency.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_saga_bus_observer.py -v`
Expected: ALL 5 tests PASS

Run: `python3 -m pytest tests/test_ouroboros_governance/ -k saga -v --tb=short`
Expected: All existing saga tests still pass (bus=None by default)

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/saga/saga_apply_strategy.py tests/test_ouroboros_governance/test_saga_bus_observer.py
git commit -m "feat(saga-bus): wire SagaMessageBus as passive observer in SagaApplyStrategy"
```

---

### Task 4: Emit saga events from orchestrator boundary paths

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py:1089-1092, 1140-1170`
- Test: `tests/test_ouroboros_governance/test_orchestrator_bus_emit.py` (create)

**Context:** The orchestrator constructs `SagaApplyStrategy` at line 1089. It also handles post-verify failures and postmortem paths. We need to: (1) pass `message_bus` to strategy, (2) emit SAGA_FAILED from orchestrator when verify fails or postmortem fires.

**Step 1: Write failing test**

Create `tests/test_ouroboros_governance/test_orchestrator_bus_emit.py`:

```python
"""Tests for orchestrator-level bus emit on saga failure boundaries."""
from backend.core.ouroboros.governance.autonomy.saga_messages import (
    SagaMessageBus,
    SagaMessageType,
)


def test_bus_has_saga_failed_type():
    assert SagaMessageType.SAGA_FAILED.value == "saga_failed"


def test_bus_stores_messages():
    bus = SagaMessageBus(max_messages=10)
    from backend.core.ouroboros.governance.autonomy.saga_messages import SagaMessage
    bus.send(SagaMessage(
        message_type=SagaMessageType.SAGA_FAILED,
        saga_id="test",
        payload={"schema_version": "1.0", "op_id": "test-op", "reason_code": "verify_failed"},
    ))
    msgs = bus.get_messages(saga_id="test")
    assert len(msgs) == 1
    assert msgs[0].payload["reason_code"] == "verify_failed"
```

**Step 2: Run test**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_orchestrator_bus_emit.py -v`
Expected: PASS (smoke test)

**Step 3: Modify orchestrator**

In `backend/core/ouroboros/governance/orchestrator.py`:

**3a.** Add `message_bus` to orchestrator config/construction. Find where `SagaApplyStrategy` is constructed (line 1089) and pass `message_bus`:

Replace:
```python
        strategy = SagaApplyStrategy(
            repo_roots=repo_roots,
            ledger=self._stack.ledger,
        )
```

With:
```python
        strategy = SagaApplyStrategy(
            repo_roots=repo_roots,
            ledger=self._stack.ledger,
            message_bus=getattr(self._config, "message_bus", None),
            keep_failed_saga_branches=os.environ.get(
                "JARVIS_SAGA_KEEP_FORENSICS_BRANCHES", "true"
            ).lower() in ("1", "true", "yes"),
        )
```

Make sure `import os` is at the top of the file (add if not present).

**3b.** In the verify-failure path (the `compensate_after_verify_failure` call), add bus emit BEFORE compensation:

Find the verify failure handling and add:
```python
                # Emit to bus if available
                _bus = getattr(strategy, "_bus", None)
                if _bus is not None:
                    try:
                        from backend.core.ouroboros.governance.autonomy.saga_messages import (
                            SagaMessage, SagaMessageType, MessagePriority,
                        )
                        _bus.send(SagaMessage(
                            message_type=SagaMessageType.SAGA_FAILED,
                            saga_id=apply_result.saga_id,
                            correlation_id=apply_result.saga_id,
                            priority=MessagePriority.HIGH,
                            payload={
                                "schema_version": "1.0",
                                "op_id": ctx.op_id,
                                "saga_id": apply_result.saga_id,
                                "reason_code": "verify_failed",
                                "failed_phase": "VERIFY",
                            },
                        ))
                    except Exception:
                        pass
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -k orchestrator -v --tb=short`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/test_ouroboros_governance/test_orchestrator_bus_emit.py
git commit -m "feat(orchestrator): pass message_bus + keep_forensics to SagaApplyStrategy, emit on verify failure"
```

---

### Task 5: Wire TestFailureSensor with real TestWatcher

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py:353-356` (and line 377)
- Test: `tests/governance/intake/test_test_failure_sensor_watcher_wiring.py` (create)

**Step 1: Write failing test**

Create `tests/governance/intake/test_test_failure_sensor_watcher_wiring.py`:

```python
"""Tests for TestFailureSensor watcher wiring in IntakeLayerService."""
from pathlib import Path
from unittest.mock import MagicMock


def test_sensor_has_watcher_when_constructed_with_one():
    from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
        TestFailureSensor,
    )
    watcher = MagicMock()
    watcher.poll_interval_s = 300
    sensor = TestFailureSensor(repo="jarvis", router=MagicMock(), test_watcher=watcher)
    assert sensor._watcher is not None
    assert sensor._watcher is watcher


def test_sensor_without_watcher_has_none():
    from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
        TestFailureSensor,
    )
    sensor = TestFailureSensor(repo="jarvis", router=MagicMock())
    assert sensor._watcher is None


def test_test_watcher_exists_and_accepts_repo_path():
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    watcher = TestWatcher(
        repo="jarvis",
        repo_path="/tmp/fake-repo",
        poll_interval_s=300.0,
    )
    assert watcher.poll_interval_s == 300.0
```

**Step 2: Run test**

Run: `python3 -m pytest tests/governance/intake/test_test_failure_sensor_watcher_wiring.py -v`
Expected: PASS (these test existing constructors)

**Step 3: Modify IntakeLayerService to wire TestWatcher**

In `backend/core/ouroboros/governance/intake/intake_layer_service.py`:

**3a.** Add import near top (after existing sensor imports):
```python
from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
```

**3b.** Replace lines 353-356 (multi-repo path):

From:
```python
            test_failure_sensors = [
                TestFailureSensor(repo=rc.name, router=self._router)
                for rc in enabled_repos
            ]
```

To:
```python
            _test_poll_s = float(os.environ.get("JARVIS_INTENT_TEST_INTERVAL_S", "300"))
            test_failure_sensors = [
                TestFailureSensor(
                    repo=rc.name,
                    router=self._router,
                    test_watcher=TestWatcher(
                        repo=rc.name,
                        repo_path=str(rc.local_path),
                        poll_interval_s=_test_poll_s,
                    ),
                )
                for rc in enabled_repos
            ]
```

**3c.** Replace line 377 (single-repo fallback):

From:
```python
            test_failure_sensors = [TestFailureSensor(repo="jarvis", router=self._router)]
```

To:
```python
            _test_poll_s = float(os.environ.get("JARVIS_INTENT_TEST_INTERVAL_S", "300"))
            test_failure_sensors = [
                TestFailureSensor(
                    repo="jarvis",
                    router=self._router,
                    test_watcher=TestWatcher(
                        repo="jarvis",
                        repo_path=str(self._config.project_root),
                        poll_interval_s=_test_poll_s,
                    ),
                )
            ]
```

Make sure `import os` is present at the top of the file.

**Step 4: Run tests**

Run: `python3 -m pytest tests/governance/intake/test_test_failure_sensor_watcher_wiring.py -v`
Expected: PASS

Run: `python3 -m pytest tests/governance/intake/ -v --tb=short`
Expected: All intake tests pass

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/intake_layer_service.py tests/governance/intake/test_test_failure_sensor_watcher_wiring.py
git commit -m "feat(intake): wire TestFailureSensor with real TestWatcher for active polling"
```

---

### Task 6: Fix orphan detection repo path + GLS bus creation

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py:1287-1301`
- Test: (covered by existing health tests + manual verification)

**Step 1: Read current `_detect_orphan_branches` implementation**

Read `governed_loop_service.py` lines 1287-1301.

**Step 2: Fix the repo registry attribute path**

Replace:
```python
    def _detect_orphan_branches(self) -> List[str]:
        """Detect orphaned saga branches across registered repos."""
        try:
            from backend.core.ouroboros.governance.saga.repo_lock import RepoLockManager
            mgr = RepoLockManager()
            if self._config.repo_registry is not None:
                roots = {
                    rc.name: rc.local_path
                    for rc in self._config.repo_registry.list_enabled()
                }
            else:
                roots = {"jarvis": self._config.project_root}
            return mgr.detect_orphan_branches(roots)
        except Exception:
            return []
```

With:
```python
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
```

**Step 3: Add SagaMessageBus creation in GLS**

Find the GLS `_build_components` or startup section where orchestrator config is assembled. Add bus creation:

```python
        # Create SagaMessageBus for passive saga observability
        try:
            from backend.core.ouroboros.governance.autonomy.saga_messages import SagaMessageBus
            self._saga_bus = SagaMessageBus(max_messages=500)
        except ImportError:
            self._saga_bus = None
```

Then pass `self._saga_bus` to the orchestrator config so it can pass it to `SagaApplyStrategy`. Find where OrchestratorConfig or similar is constructed and add `message_bus=self._saga_bus`.

**Step 4: Add bus telemetry to health()**

In the `health()` return dict, add:
```python
            "saga_bus": self._saga_bus.to_dict() if getattr(self, "_saga_bus", None) else {},
```

**Step 5: Run tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short 2>&1 | tail -20`
Expected: All pass

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py
git commit -m "fix(gls): use live repo_registry for orphan detection, create SagaMessageBus at startup"
```

---

### Task 7: Full Regression + Activation Verification

**Step 1: Run all governance tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short`
Expected: All pass

**Step 2: Run all intake tests**

Run: `python3 -m pytest tests/governance/intake/ -v --tb=short`
Expected: All pass

**Step 3: Run E2E Gate 1**

Run: `python3 -m pytest tests/e2e/test_gate1_sentinel.py -v --tb=short`
Expected: All 10 pass

**Step 4: Run new bus observer tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_saga_bus_observer.py tests/governance/autonomy/test_saga_messages_bplus.py -v`
Expected: All pass

**Step 5: Verify env var activation**

Run: `python3 -c "from dotenv import load_dotenv; load_dotenv(); import os; print('BRANCH_ISOLATION:', os.getenv('JARVIS_SAGA_BRANCH_ISOLATION')); print('GOVERNANCE_MODE:', os.getenv('JARVIS_GOVERNANCE_MODE')); print('KEEP_FORENSICS:', os.getenv('JARVIS_SAGA_KEEP_FORENSICS_BRANCHES'))"`

Expected:
```
BRANCH_ISOLATION: true
GOVERNANCE_MODE: governed
KEEP_FORENSICS: true
```

---

## Execution Summary

| Task | Files | What |
|------|-------|------|
| 1 | `.env` | Activate B+ branch isolation + forensics flag |
| 2 | `saga_messages.py` + test | Add 3 new message types |
| 3 | `saga_apply_strategy.py` + test | Wire bus as passive observer (largest task) |
| 4 | `orchestrator.py` + test | Pass bus + forensics config, emit on verify failure |
| 5 | `intake_layer_service.py` + test | Wire TestWatcher into TestFailureSensor |
| 6 | `governed_loop_service.py` | Fix orphan detection, create bus at startup |
| 7 | (verification) | Full regression + env activation check |

**Total: 7 tasks, ~200 lines of production code + tests**

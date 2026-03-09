# Single-Repo Connective Tissue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the three remaining connective-tissue pieces that complete single-repo JARVIS self-development: safe-pause on stuck sagas, canary telemetry recording after every apply, and learning-bridge outcome publishing after every terminal state.

**Architecture:** All three changes are additive call-sites in `GovernedOrchestrator._execute_operation()` and `_execute_saga_apply()`. No new files, no new classes — each task adds 3–10 lines at already-identified insertion points. Tests use the existing `_mock_stack()` helper pattern from `test_orchestrator.py` extended with the relevant mocks.

**Tech Stack:** Python 3.9, asyncio, pytest (asyncio_mode=auto — **never add @pytest.mark.asyncio**)

---

## Key files to know

- `backend/core/ouroboros/governance/orchestrator.py` — all 3 tasks modify this file
- `backend/core/ouroboros/governance/supervisor_controller.py:110-114` — `async def pause()` switches to READ_ONLY
- `backend/core/ouroboros/governance/canary_controller.py:126-144` — `record_operation(file_path, success, latency_s, rolled_back=False)`
- `backend/core/ouroboros/governance/learning_bridge.py:31-44,85-114` — `OperationOutcome` dataclass + `async def publish(outcome)`
- `backend/core/ouroboros/governance/integration.py:297-298` — `GovernanceStack.controller: SupervisorOuroborosController` and `learning_bridge: Optional[Any]`
- `tests/test_ouroboros_governance/test_orchestrator.py` — existing test helpers, use as reference for `_mock_stack()` pattern

### Orchestrator code map (read before editing)

```
_execute_operation() single-repo apply path:
  line 504: # ---- Phase 7: APPLY ----
  line 507: if ctx.cross_repo: return await self._execute_saga_apply(...)
  line 510: change_request = self._build_change_request(...)
  line 512: try:
  line 513:   change_result = await self._stack.change_engine.execute(change_request)
  line 514: except Exception as exc:
  line 518:   ctx = ctx.advance(POSTMORTEM)  ← failure terminal
  line 519:   await self._record_ledger(... FAILED ...)
  line 524:   return ctx
  line 526: if not change_result.success:
  line 527:   ctx = ctx.advance(POSTMORTEM)  ← failure terminal
  line 528:   await self._record_ledger(... FAILED ...)
  line 536:   return ctx
  line 538: # ---- Phase 8: VERIFY ----
  line 539: ctx = ctx.advance(VERIFY)
  line 540: await self._record_ledger(... APPLIED ...)
  line 546: ctx = ctx.advance(COMPLETE)  ← success terminal
  line 547: return ctx

_execute_saga_apply() path:
  line 822: apply_result = await strategy.execute(ctx, patch_map)
  line 824: if SAGA_ABORTED:              ← failure terminal (line 825-831)
  line 833: if SAGA_APPLY_COMPLETED:
  line 843:   if not verify_result.passed:  ← failure terminal (line 850-860)
  line 862:   # SAGA_SUCCEEDED            ← success terminal (line 863-870)
  line 872: if SAGA_STUCK:               ← failure terminal (line 872-889)
  line 891: # SAGA_ROLLED_BACK           ← failure terminal (line 893-898)
```

---

## Task 1: SAGA_STUCK triggers controller.pause()

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py:872-889`
- Test: `tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py` (create)

### Step 1: Write the failing test

Create `tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py`:

```python
"""Tests for the three connective-tissue wiring items in GovernedOrchestrator.

asyncio_mode = auto — never add @pytest.mark.asyncio.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskClassification,
    RiskTier,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    SagaApplyResult,
    SagaTerminalState,
)
from backend.core.ouroboros.governance.learning_bridge import (
    OperationOutcome,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        project_root=tmp_path,
        generation_timeout_s=5.0,
        apply_timeout_s=5.0,
        approval_timeout_s=5.0,
        max_retries=1,
    )


def _make_ctx(tmp_path: Path, op_id: str = "op-001") -> OperationContext:
    return OperationContext.create(
        target_files=(str(tmp_path / "backend/core/utils.py"),),
        description="Add utility function",
        op_id=op_id,
    )


def _mock_stack() -> MagicMock:
    stack = MagicMock()
    stack.can_write.return_value = (True, "ok")
    stack.risk_engine.classify.return_value = RiskClassification(
        tier=RiskTier.SAFE_AUTO, reason_code="safe"
    )
    stack.ledger.append = AsyncMock(return_value=True)
    stack.comm = AsyncMock()
    stack.controller.pause = AsyncMock()
    stack.canary.record_operation = MagicMock()
    stack.learning_bridge = None  # disabled by default; override per test
    stack.change_engine.execute = AsyncMock(
        return_value=MagicMock(success=True, rolled_back=False, op_id="op-001")
    )
    return stack


def _mock_generator(tmp_path: Path) -> MagicMock:
    gen = MagicMock()
    candidate = {
        "file_path": str(tmp_path / "backend/core/utils.py"),
        "full_content": "def hello():\n    pass\n",
    }
    gen.generate = AsyncMock(
        return_value=GenerationResult(
            candidates=(candidate,),
            provider_name="mock",
            generation_duration_s=0.1,
        )
    )
    gen.validate = AsyncMock(
        return_value=ValidationResult(
            passed=True,
            best_candidate=candidate,
            validation_duration_s=0.05,
        )
    )
    return gen


def _make_orchestrator(tmp_path, stack=None) -> GovernedOrchestrator:
    if stack is None:
        stack = _mock_stack()
    return GovernedOrchestrator(
        stack=stack,
        generator=_mock_generator(tmp_path),
        config=_config(tmp_path),
    )


# ---------------------------------------------------------------------------
# Task 1: SAGA_STUCK → controller.pause()
# ---------------------------------------------------------------------------

async def test_saga_stuck_triggers_controller_pause(tmp_path):
    """When _execute_saga_apply returns SAGA_STUCK, controller.pause() must be called."""
    stack = _mock_stack()
    orch = _make_orchestrator(tmp_path, stack=stack)

    stuck_result = SagaApplyResult(
        terminal_state=SagaTerminalState.SAGA_STUCK,
        saga_id="saga-001",
        reason_code="compensation_failed",
        applied_patches={},
        rolled_back_patches={},
    )

    # Patch _execute_saga_apply to return a SAGA_STUCK result directly
    with patch.object(orch, "_execute_saga_apply", new=AsyncMock(return_value=None)) as mock_saga:
        # We actually need the outer _execute_operation to call saga path.
        # Easier: patch strategy.execute inside _execute_saga_apply.
        # Instead, patch _execute_operation to reach SAGA_STUCK inline.
        pass

    # Simpler approach: call _execute_saga_apply directly with a patched strategy
    ctx = _make_ctx(tmp_path)
    ctx_apply = ctx.advance(OperationPhase.APPLY)

    with patch(
        "backend.core.ouroboros.governance.orchestrator.SagaApplyStrategy"
    ) as MockStrategy:
        mock_strategy = MagicMock()
        mock_strategy.execute = AsyncMock(return_value=stuck_result)
        MockStrategy.return_value = mock_strategy

        await orch._execute_saga_apply(ctx_apply, {"mock_candidate": {}})

    stack.controller.pause.assert_awaited_once()


async def test_saga_stuck_pause_failure_does_not_propagate(tmp_path):
    """If controller.pause() raises, _execute_saga_apply still returns ctx (no re-raise)."""
    stack = _mock_stack()
    stack.controller.pause = AsyncMock(side_effect=RuntimeError("controller down"))
    orch = _make_orchestrator(tmp_path, stack=stack)

    stuck_result = SagaApplyResult(
        terminal_state=SagaTerminalState.SAGA_STUCK,
        saga_id="saga-002",
        reason_code="compensation_failed",
        applied_patches={},
        rolled_back_patches={},
    )

    ctx = _make_ctx(tmp_path)
    ctx_apply = ctx.advance(OperationPhase.APPLY)

    with patch(
        "backend.core.ouroboros.governance.orchestrator.SagaApplyStrategy"
    ) as MockStrategy:
        mock_strategy = MagicMock()
        mock_strategy.execute = AsyncMock(return_value=stuck_result)
        MockStrategy.return_value = mock_strategy

        # Should NOT raise even though pause() raised
        result = await orch._execute_saga_apply(ctx_apply, {"mock_candidate": {}})

    assert result is not None
    assert result.phase == OperationPhase.POSTMORTEM
```

### Step 2: Run to verify failure

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py::test_saga_stuck_triggers_controller_pause -v --tb=short 2>&1 | tail -20
```

Expected: FAIL — `assert_awaited_once` fails because `pause()` is never called.

### Step 3: Implement

In `backend/core/ouroboros/governance/orchestrator.py`, find the SAGA_STUCK block (around line 872). Add the `controller.pause()` call **after** the postmortem try/except, wrapped in its own try/except:

**Current (lines 872-889):**
```python
        if apply_result.terminal_state == SagaTerminalState.SAGA_STUCK:
            # Compensation failed: data may be inconsistent — emit postmortem
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
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id},
            )
            return ctx
```

**Replace with:**
```python
        if apply_result.terminal_state == SagaTerminalState.SAGA_STUCK:
            # Compensation failed: data may be inconsistent — emit postmortem
            try:
                await self._stack.comm.emit_postmortem(
                    op_id=ctx.op_id,
                    root_cause="saga_stuck",
                    failed_phase="APPLY",
                    next_safe_action="human_intervention_required",
                )
            except Exception:
                pass
            # Halt intake: dirty state requires human review before next op
            try:
                await self._stack.controller.pause()
                logger.warning(
                    "[Orchestrator] Safe pause triggered after SAGA_STUCK on %s",
                    ctx.op_id,
                )
            except Exception:
                logger.exception(
                    "[Orchestrator] controller.pause() failed for stuck saga %s; "
                    "manual pause may be required",
                    ctx.op_id,
                )
            ctx = ctx.advance(OperationPhase.POSTMORTEM)
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": apply_result.reason_code, "saga_id": apply_result.saga_id},
            )
            return ctx
```

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py -k "stuck" -v --tb=short
```

Expected: 2 PASSED.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): trigger controller.pause() on SAGA_STUCK to halt intake

Dirty saga state requires human review. Pause is fault-isolated so a
broken controller doesn't block the ledger record.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: CanaryController telemetry after every apply

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (single-repo and saga apply paths)
- Test: `tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py` (append)

### Background on canary.record_operation

```python
# canary_controller.py
def record_operation(
    self,
    file_path: str,
    success: bool,
    latency_s: float,
    rolled_back: bool = False,
) -> None
```

`file_path` is matched against registered slice prefixes (`"tests/"`, etc.). Use `ctx.target_files[0]` as the representative file — it's already stored as a string. For saga ops, call once per entry in `ctx.target_files`.

`self._stack.canary` is always present (non-Optional field on GovernanceStack). No None guard needed.

### Step 1: Append failing tests

Add to `tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py`:

```python
# ---------------------------------------------------------------------------
# Task 2: CanaryController record_operation
# ---------------------------------------------------------------------------

async def test_canary_record_on_single_repo_success(tmp_path):
    """canary.record_operation called with success=True after single-repo apply succeeds."""
    stack = _mock_stack()
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)

    await orch._execute_operation(ctx)

    assert stack.canary.record_operation.called
    call_kwargs = stack.canary.record_operation.call_args
    assert call_kwargs.kwargs.get("success") is True or call_kwargs.args[1] is True


async def test_canary_record_on_single_repo_failure(tmp_path):
    """canary.record_operation called with success=False when change_engine fails."""
    stack = _mock_stack()
    stack.change_engine.execute = AsyncMock(
        return_value=MagicMock(success=False, rolled_back=True, op_id="op-001")
    )
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)

    await orch._execute_operation(ctx)

    assert stack.canary.record_operation.called
    call_kwargs = stack.canary.record_operation.call_args
    # success=False
    success_val = call_kwargs.kwargs.get("success", call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
    assert success_val is False
    # rolled_back=True
    rb_val = call_kwargs.kwargs.get("rolled_back", False)
    assert rb_val is True


async def test_canary_record_on_saga_stuck(tmp_path):
    """canary.record_operation called with success=False after SAGA_STUCK."""
    stack = _mock_stack()
    orch = _make_orchestrator(tmp_path, stack=stack)

    stuck_result = SagaApplyResult(
        terminal_state=SagaTerminalState.SAGA_STUCK,
        saga_id="saga-003",
        reason_code="compensation_failed",
        applied_patches={},
        rolled_back_patches={},
    )

    ctx = _make_ctx(tmp_path)
    ctx_apply = ctx.advance(OperationPhase.APPLY)

    with patch(
        "backend.core.ouroboros.governance.orchestrator.SagaApplyStrategy"
    ) as MockStrategy:
        mock_strategy = MagicMock()
        mock_strategy.execute = AsyncMock(return_value=stuck_result)
        MockStrategy.return_value = mock_strategy

        await orch._execute_saga_apply(ctx_apply, {"mock_candidate": {}})

    assert stack.canary.record_operation.called
    call_kwargs = stack.canary.record_operation.call_args
    success_val = call_kwargs.kwargs.get("success", call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
    assert success_val is False
```

### Step 2: Run to verify failure

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py -k "canary" -v --tb=short 2>&1 | tail -20
```

Expected: 3 FAIL — `assert stack.canary.record_operation.called` fails.

### Step 3: Implement — single-repo path

In `orchestrator.py`, find the single-repo APPLY block (around line 503). Add timing capture and `record_operation` calls.

**Key pattern — add before the `try:` at line ~512:**
```python
        _t_apply = time.monotonic()
```

**After the `except Exception` block that advances to POSTMORTEM (line ~518-524), add canary call:**
```python
            # canary telemetry
            _latency = time.monotonic() - _t_apply
            _file = str(ctx.target_files[0]) if ctx.target_files else "unknown"
            self._stack.canary.record_operation(
                file_path=_file, success=False, latency_s=_latency
            )
            return ctx
```

**After the `if not change_result.success:` block (line ~526-536), add canary call:**
```python
            # canary telemetry
            _latency = time.monotonic() - _t_apply
            _file = str(ctx.target_files[0]) if ctx.target_files else "unknown"
            self._stack.canary.record_operation(
                file_path=_file,
                success=False,
                latency_s=_latency,
                rolled_back=change_result.rolled_back,
            )
            return ctx
```

**After `ctx = ctx.advance(OperationPhase.COMPLETE)` (line ~546), BEFORE `return ctx`, add:**
```python
        # canary telemetry
        _latency = time.monotonic() - _t_apply
        _file = str(ctx.target_files[0]) if ctx.target_files else "unknown"
        self._stack.canary.record_operation(
            file_path=_file, success=True, latency_s=_latency
        )
        return ctx
```

### Step 4: Implement — saga path

In `_execute_saga_apply()`, add timing before `strategy.execute()` (line ~822):
```python
        _t_saga = time.monotonic()
        apply_result = await strategy.execute(ctx, patch_map)
```

Then add `record_operation` at each terminal branch. Use `ctx.target_files` to record per-file:

```python
# Helper (call at each terminal point):
def _record_canary_for_ctx(self, ctx, success: bool, latency_s: float, rolled_back: bool = False) -> None:
    for f in ctx.target_files:
        self._stack.canary.record_operation(
            file_path=str(f), success=success, latency_s=latency_s, rolled_back=rolled_back,
        )
```

**Add this private helper method** to `GovernedOrchestrator` (at the end of the Private helpers section, around line 550+).

Then replace each terminal return in `_execute_saga_apply` with a call before return:

- `SAGA_ABORTED` (before `return ctx` at ~831): `self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)`
- verify failure (before `return ctx` at ~860): `self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga, rolled_back=comp_ok)`
- `SAGA_SUCCEEDED` (before `return ctx` at ~870): `self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_saga)`
- `SAGA_STUCK` (before `return ctx` at ~889): `self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)`
- `SAGA_ROLLED_BACK` (before `return ctx` at ~898): `self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga, rolled_back=True)`

### Step 5: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py -v --tb=short 2>&1 | tail -20
```

Expected: all tests pass (5 so far).

### Step 6: Regression check

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py -q --tb=short 2>&1 | tail -10
```

Expected: same failure count as before (pre-existing failures only, no new ones).

### Step 7: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): record canary telemetry after every apply (single-repo + saga)

Adds timing around change_engine.execute() and strategy.execute() so
CanaryController.record_operation() receives accurate latency_s at
every terminal outcome (success, failure, rollback, stuck).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: LearningBridge outcome publishing at every terminal state

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (add import + publish calls)
- Test: `tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py` (append)

### Background on learning_bridge.publish

```python
# learning_bridge.py
@dataclass
class OperationOutcome:
    op_id: str
    goal: str
    target_files: List[str]
    final_state: OperationState          # OperationState.APPLIED or FAILED
    error_pattern: Optional[str] = None  # short code like "change_engine_failed"
    solution_pattern: Optional[str] = None

async def publish(self, outcome: OperationOutcome) -> None:
    # Fault-isolated: exceptions logged, never propagated
```

`self._stack.learning_bridge` is `Optional[Any]` — always guard with `if self._stack.learning_bridge is not None`.

`OperationState` is already imported in `orchestrator.py` from `backend.core.ouroboros.governance.ledger`. Add `OperationOutcome` import.

### Step 1: Append failing tests

Add to `tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py`:

```python
# ---------------------------------------------------------------------------
# Task 3: LearningBridge outcome publishing
# ---------------------------------------------------------------------------

async def test_learning_bridge_publish_on_single_repo_success(tmp_path):
    """learning_bridge.publish called with APPLIED outcome on single-repo success."""
    stack = _mock_stack()
    stack.learning_bridge = AsyncMock()
    stack.learning_bridge.publish = AsyncMock()
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)

    await orch._execute_operation(ctx)

    stack.learning_bridge.publish.assert_awaited_once()
    outcome: OperationOutcome = stack.learning_bridge.publish.call_args.args[0]
    assert outcome.final_state == OperationState.APPLIED
    assert outcome.op_id == ctx.op_id


async def test_learning_bridge_publish_on_single_repo_failure(tmp_path):
    """learning_bridge.publish called with FAILED outcome when change_engine fails."""
    stack = _mock_stack()
    stack.learning_bridge = AsyncMock()
    stack.learning_bridge.publish = AsyncMock()
    stack.change_engine.execute = AsyncMock(
        return_value=MagicMock(success=False, rolled_back=False, op_id="op-001")
    )
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)

    await orch._execute_operation(ctx)

    stack.learning_bridge.publish.assert_awaited_once()
    outcome: OperationOutcome = stack.learning_bridge.publish.call_args.args[0]
    assert outcome.final_state == OperationState.FAILED
    assert outcome.error_pattern == "change_engine_failed"


async def test_learning_bridge_skipped_when_none(tmp_path):
    """No error when learning_bridge is None (disabled)."""
    stack = _mock_stack()
    stack.learning_bridge = None
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)

    # Should not raise
    await orch._execute_operation(ctx)


async def test_learning_bridge_publish_on_saga_stuck(tmp_path):
    """learning_bridge.publish called with FAILED+saga_stuck error on SAGA_STUCK."""
    stack = _mock_stack()
    stack.learning_bridge = AsyncMock()
    stack.learning_bridge.publish = AsyncMock()
    orch = _make_orchestrator(tmp_path, stack=stack)

    stuck_result = SagaApplyResult(
        terminal_state=SagaTerminalState.SAGA_STUCK,
        saga_id="saga-004",
        reason_code="compensation_failed",
        applied_patches={},
        rolled_back_patches={},
    )

    ctx = _make_ctx(tmp_path)
    ctx_apply = ctx.advance(OperationPhase.APPLY)

    with patch(
        "backend.core.ouroboros.governance.orchestrator.SagaApplyStrategy"
    ) as MockStrategy:
        mock_strategy = MagicMock()
        mock_strategy.execute = AsyncMock(return_value=stuck_result)
        MockStrategy.return_value = mock_strategy

        await orch._execute_saga_apply(ctx_apply, {"mock_candidate": {}})

    stack.learning_bridge.publish.assert_awaited_once()
    outcome: OperationOutcome = stack.learning_bridge.publish.call_args.args[0]
    assert outcome.final_state == OperationState.FAILED
    assert outcome.error_pattern == "saga_stuck"
```

### Step 2: Run to verify failure

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py -k "learning" -v --tb=short 2>&1 | tail -20
```

Expected: 3-4 FAIL — `publish` never awaited.

### Step 3: Add import

In `orchestrator.py`, after the existing imports, add:

```python
from backend.core.ouroboros.governance.learning_bridge import OperationOutcome
```

### Step 4: Add private helper method

Add to `GovernedOrchestrator` private helpers (after `_record_canary_for_ctx`):

```python
    async def _publish_outcome(
        self,
        ctx: OperationContext,
        final_state: OperationState,
        error_pattern: Optional[str] = None,
    ) -> None:
        """Publish operation outcome to LearningBridge. Fault-isolated."""
        if self._stack.learning_bridge is None:
            return
        outcome = OperationOutcome(
            op_id=ctx.op_id,
            goal=ctx.description,
            target_files=list(ctx.target_files),
            final_state=final_state,
            error_pattern=error_pattern,
        )
        await self._stack.learning_bridge.publish(outcome)
```

### Step 5: Wire publish at all terminal points

**Single-repo path** — add `await self._publish_outcome(...)` BEFORE each `return ctx`:

Exception path (line ~518-524):
```python
            await self._publish_outcome(ctx, OperationState.FAILED, "change_engine_error")
            return ctx
```

Failure path (line ~527-536):
```python
            await self._publish_outcome(ctx, OperationState.FAILED, "change_engine_failed")
            return ctx
```

Success path (line ~546-547):
```python
        await self._publish_outcome(ctx, OperationState.APPLIED)
        return ctx
```

**Saga path** — add before each `return ctx`:

SAGA_ABORTED (line ~831):
```python
            await self._publish_outcome(ctx, OperationState.FAILED, apply_result.reason_code)
            return ctx
```

Verify failure (line ~860):
```python
                await self._publish_outcome(ctx, OperationState.FAILED, verify_result.reason_code)
                return ctx
```

SAGA_SUCCEEDED (line ~870):
```python
            await self._publish_outcome(ctx, OperationState.APPLIED)
            return ctx
```

SAGA_STUCK (line ~889):
```python
            await self._publish_outcome(ctx, OperationState.FAILED, "saga_stuck")
            return ctx
```

SAGA_ROLLED_BACK (line ~898):
```python
        await self._publish_outcome(ctx, OperationState.FAILED, apply_result.reason_code)
        return ctx
```

### Step 6: Run full test file

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py -v --tb=short 2>&1 | tail -20
```

Expected: all tests PASS (9 total).

### Step 7: Run full governance regression

```bash
python3 -m pytest tests/test_ouroboros_governance/ tests/governance/ -q --tb=short 2>&1 | tail -10
```

Expected: 0 new failures (pre-existing 30 unchanged).

### Step 8: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        tests/test_ouroboros_governance/test_orchestrator_connective_tissue.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): publish OperationOutcome to LearningBridge at all terminal states

Adds _publish_outcome() helper + calls at every success/failure/stuck/rollback
terminal in both single-repo and saga apply paths. Guarded by learning_bridge
None check so disabling the bridge never breaks the pipeline.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Summary

| Task | What changes | Effect |
|------|-------------|--------|
| 1 | `SAGA_STUCK` block calls `controller.pause()` | Intake halts on dirty saga state |
| 2 | `record_operation()` after every apply outcome | Canary learns pass/fail/latency rates |
| 3 | `_publish_outcome()` at every terminal state | J-Prime learns from every outcome |

After all 3 tasks: single-repo JARVIS self-development loop is complete.

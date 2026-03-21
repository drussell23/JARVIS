# Ouroboros Tier 2 Gap Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire three new Ouroboros capabilities: GAP 6 (UserSignalBus — user-initiated stop races against in-flight orchestrator), GAP 4 (SkillRegistry — domain-specific instructions injected by target file pattern), and GAP 8 (CorrectionWriter — auto-appends rejection reasons to OUROBOROS.md).

**Architecture:**
- GAP 6: New `UserSignalBus` (asyncio.Event wrapper) wired into `GovernedLoopService.submit()` so a voice "stop" command races against the running orchestrator task. `EV_PREEMPT` added to `LoopEvent`; FSM handles RUNNING × EV_PREEMPT → SUSPENDED_PREEMPTED. `VoiceCommandSensor` detects stop/cancel phrases and calls `bus.request_stop()`.
- GAP 4: New `SkillRegistry` loads `<repo>/.jarvis/skills/*.yaml` (each with `name`, `filePattern`, `instructions`). `ContextExpander.expand()` calls `skill_registry.match(ctx.target_files)` after the expansion loop and merges matched instructions into `ctx.human_instructions`.
- GAP 8: New `correction_writer.write_correction()` appends rejection reasons to `<repo>/OUROBOROS.md` under `## Auto-Learned Corrections`. `CLIApprovalProvider.reject()` calls it when `project_root` is injected at construction.

**Tech Stack:** Python asyncio, PyYAML (already in deps), pathlib, fnmatch, pytest

---

## File Structure

### New files
- `backend/core/ouroboros/governance/user_signal_bus.py` — UserSignalBus class (~35 lines)
- `backend/core/ouroboros/governance/skill_registry.py` — SkillRegistry class (~65 lines)
- `backend/core/ouroboros/governance/correction_writer.py` — write_correction() function (~45 lines)
- `tests/governance/test_user_signal_bus.py` — unit tests for UserSignalBus
- `tests/governance/test_gap6_fsm_preempt.py` — FSM EV_PREEMPT transition tests
- `tests/governance/test_gap6_gls_race.py` — GLS submit() race structural tests
- `tests/governance/test_gap6_voice_stop.py` — VoiceCommandSensor stop detection tests
- `tests/governance/test_skill_registry.py` — SkillRegistry unit tests
- `tests/governance/test_gap4_expander_skills.py` — ContextExpander + SkillRegistry integration tests
- `tests/governance/test_correction_writer.py` — write_correction() unit tests
- `tests/governance/test_gap8_approval_wire.py` — CLIApprovalProvider auto-memory tests

### Modified files
- `backend/core/ouroboros/governance/contracts/fsm_contract.py:23-34` — add `EV_PREEMPT` to `LoopEvent`
- `backend/core/ouroboros/governance/preemption_fsm.py:196-238` — add RUNNING × EV_PREEMPT → SUSPENDED_PREEMPTED branch in `_from_running()`
- `backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py:55-67` — add optional `signal_bus` param; detect stop phrases
- `backend/core/ouroboros/governance/governed_loop_service.py:678-684,1317-1322,2075` — add `_user_signal_bus` attr; race in submit(); pass `project_root` to CLIApprovalProvider
- `backend/core/ouroboros/governance/context_expander.py:48-57,159-170` — add optional `skill_registry` param; call `match()` at end of `expand()`
- `backend/core/ouroboros/governance/orchestrator.py:341-345` — pass `skill_registry` to `ContextExpander()`
- `backend/core/ouroboros/governance/approval_provider.py:255-256,343-360` — inject `project_root` into `CLIApprovalProvider`; call `write_correction()` in `reject()`

---

## Task 1: UserSignalBus + EV_PREEMPT FSM Event

**Files:**
- Create: `backend/core/ouroboros/governance/user_signal_bus.py`
- Modify: `backend/core/ouroboros/governance/contracts/fsm_contract.py:23-34`
- Modify: `backend/core/ouroboros/governance/preemption_fsm.py:196-238`
- Test: `tests/governance/test_user_signal_bus.py`
- Test: `tests/governance/test_gap6_fsm_preempt.py`

- [ ] **Step 1: Write the failing tests for UserSignalBus**

```python
# tests/governance/test_user_signal_bus.py
import asyncio
import pytest
from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus


def test_initial_state_not_set():
    bus = UserSignalBus()
    assert not bus.is_stop_requested()


def test_request_stop_sets_flag():
    bus = UserSignalBus()
    bus.request_stop()
    assert bus.is_stop_requested()


def test_reset_clears_flag():
    bus = UserSignalBus()
    bus.request_stop()
    bus.reset()
    assert not bus.is_stop_requested()


@pytest.mark.asyncio
async def test_wait_for_stop_resolves_after_request_stop():
    bus = UserSignalBus()

    async def trigger():
        await asyncio.sleep(0.01)
        bus.request_stop()

    asyncio.create_task(trigger())
    await asyncio.wait_for(bus.wait_for_stop(), timeout=1.0)
    assert bus.is_stop_requested()


@pytest.mark.asyncio
async def test_wait_for_stop_does_not_resolve_without_stop():
    bus = UserSignalBus()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.wait_for_stop(), timeout=0.05)
```

- [ ] **Step 2: Run test to confirm failure**

Run: `python -m pytest tests/governance/test_user_signal_bus.py -v`
Expected: ImportError — `user_signal_bus` not found

- [ ] **Step 3: Create `user_signal_bus.py`**

```python
# backend/core/ouroboros/governance/user_signal_bus.py
"""UserSignalBus — asyncio.Event wrapper for user-initiated stop signals.

GAP 6: provides the missing link between voice/CLI input and the
GovernedLoopService orchestrator race.  One bus per GLS instance.
"""
from __future__ import annotations

import asyncio


class UserSignalBus:
    """Thread-safe (event-loop-safe) stop signal for in-flight operations.

    Usage:
        bus = UserSignalBus()
        # In voice sensor / CLI handler:
        bus.request_stop()
        # In submit() race:
        await asyncio.wait([op_task, asyncio.create_task(bus.wait_for_stop())], ...)
        # After stop detected:
        bus.reset()  # clear for next op
    """

    def __init__(self) -> None:
        self._stop: asyncio.Event = asyncio.Event()

    def request_stop(self) -> None:
        """Signal all waiters that a stop has been requested."""
        self._stop.set()

    def is_stop_requested(self) -> bool:
        """Non-blocking check — True if stop has been requested since last reset."""
        return self._stop.is_set()

    async def wait_for_stop(self) -> None:
        """Await until request_stop() is called."""
        await self._stop.wait()

    def reset(self) -> None:
        """Clear the stop signal so future operations are not immediately stopped."""
        self._stop.clear()
```

- [ ] **Step 4: Run UserSignalBus tests to confirm pass**

Run: `python -m pytest tests/governance/test_user_signal_bus.py -v`
Expected: 5 passed

- [ ] **Step 5: Write failing tests for EV_PREEMPT FSM transition**

```python
# tests/governance/test_gap6_fsm_preempt.py
"""Verify EV_PREEMPT is in LoopEvent and triggers RUNNING → SUSPENDED_PREEMPTED."""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.governance.contracts.fsm_contract import (
    LoopEvent, LoopState, LoopRuntimeContext, RetryBudget,
)
from backend.core.ouroboros.governance.preemption_fsm import (
    PreemptionFsmEngine, build_transition_input,
)


def test_ev_preempt_exists_in_loop_event():
    assert hasattr(LoopEvent, "EV_PREEMPT"), "EV_PREEMPT must be added to LoopEvent"
    assert LoopEvent.EV_PREEMPT.value == "EV_PREEMPT"


def test_running_ev_preempt_transitions_to_suspended_preempted():
    engine = PreemptionFsmEngine()
    ctx = LoopRuntimeContext(op_id="test-op-1")
    budget = RetryBudget()

    ti = build_transition_input(
        op_id="test-op-1",
        phase="GENERATE",
        event=LoopEvent.EV_PREEMPT,
        ctx=ctx,
        checkpoint_seq=1,
        metadata={"source": "user_signal_bus"},
    )
    decision = engine.decide(ctx, ti, budget)

    assert decision.from_state == LoopState.RUNNING
    assert decision.to_state == LoopState.SUSPENDED_PREEMPTED
    assert not decision.terminal


def test_suspended_ev_preempt_is_noop():
    """EV_PREEMPT on already-suspended op does nothing (unhandled = noop)."""
    engine = PreemptionFsmEngine()
    ctx = LoopRuntimeContext(op_id="test-op-2", state=LoopState.SUSPENDED_PREEMPTED)
    budget = RetryBudget()

    ti = build_transition_input(
        op_id="test-op-2",
        phase="GENERATE",
        event=LoopEvent.EV_PREEMPT,
        ctx=ctx,
        checkpoint_seq=1,
        metadata={},
    )
    decision = engine.decide(ctx, ti, budget)

    # Unhandled event in SUSPENDED_PREEMPTED → same state, not terminal
    assert decision.to_state == LoopState.SUSPENDED_PREEMPTED
    assert not decision.terminal
```

- [ ] **Step 6: Run FSM tests to confirm failure**

Run: `python -m pytest tests/governance/test_gap6_fsm_preempt.py -v`
Expected: FAIL — `AttributeError: EV_PREEMPT`

- [ ] **Step 7: Add `EV_PREEMPT` to `LoopEvent` in `fsm_contract.py`**

In `backend/core/ouroboros/governance/contracts/fsm_contract.py`, locate `LoopEvent` class (lines 23–34). Add after `EV_CANCELLED`:

```python
    EV_PREEMPT = "EV_PREEMPT"          # user-initiated preemption (voice/CLI stop)
```

The updated class body:
```python
class LoopEvent(str, Enum):
    EV_GENERATE_START = "EV_GENERATE_START"
    EV_GENERATE_SUCCESS = "EV_GENERATE_SUCCESS"
    EV_GENERATE_TIMEOUT = "EV_GENERATE_TIMEOUT"
    EV_CONNECTION_LOSS = "EV_CONNECTION_LOSS"
    EV_SPOT_TERMINATED = "EV_SPOT_TERMINATED"
    EV_REHYDRATE_STARTED = "EV_REHYDRATE_STARTED"
    EV_REHYDRATE_HEALTHY = "EV_REHYDRATE_HEALTHY"
    EV_REHYDRATE_FAILED = "EV_REHYDRATE_FAILED"
    EV_RETRY_BUDGET_EXHAUSTED = "EV_RETRY_BUDGET_EXHAUSTED"
    EV_ABORT_POLICY_VIOLATION = "EV_ABORT_POLICY_VIOLATION"
    EV_CANCELLED = "EV_CANCELLED"
    EV_PREEMPT = "EV_PREEMPT"          # user-initiated preemption (voice/CLI stop)
```

- [ ] **Step 8: Add RUNNING × EV_PREEMPT → SUSPENDED_PREEMPTED in `preemption_fsm.py`**

In `backend/core/ouroboros/governance/preemption_fsm.py`, find `_from_running()` method (around line 196). Locate the `# Preemption events → suspend` comment block (around line 209). Add a new branch for EV_PREEMPT **before** the `# Policy violation` block:

```python
        # User-initiated preemption → suspend (allow rehydrate/resume later)
        if ev == LoopEvent.EV_PREEMPT:
            backoff = _compute_backoff_ms(ctx.retry_index, budget)
            return TransitionDecision(
                from_state=cs,
                to_state=LoopState.SUSPENDED_PREEMPTED,
                event=ev,
                reason_code=ReasonCode.FSM_SUSPENDED_PREEMPTED,
                retry_index=ctx.retry_index,
                backoff_ms=backoff,
                terminal=False,
                actions=list(_STATE_CHANGE_ACTIONS),
            )
```

Place this after the existing preemption events block (EV_GENERATE_TIMEOUT / EV_CONNECTION_LOSS / EV_SPOT_TERMINATED) and before the `# Policy violation → permanent failure` block.

- [ ] **Step 9: Run FSM tests to confirm pass**

Run: `python -m pytest tests/governance/test_gap6_fsm_preempt.py -v`
Expected: 3 passed

- [ ] **Step 10: Run full governance test suite to confirm no regressions**

Run: `python -m pytest tests/governance/ -v --tb=short 2>&1 | tail -30`
Expected: All existing tests pass (9 pre-existing failures in test_preflight.py/test_e2e.py are known and acceptable)

- [ ] **Step 11: Commit**

```bash
git add backend/core/ouroboros/governance/user_signal_bus.py \
        backend/core/ouroboros/governance/contracts/fsm_contract.py \
        backend/core/ouroboros/governance/preemption_fsm.py \
        tests/governance/test_user_signal_bus.py \
        tests/governance/test_gap6_fsm_preempt.py
git commit -m "feat(gap6): add UserSignalBus and EV_PREEMPT FSM event

- UserSignalBus: asyncio.Event wrapper with request_stop/wait_for_stop/reset
- EV_PREEMPT added to LoopEvent enum
- PreemptionFsmEngine: RUNNING × EV_PREEMPT → SUSPENDED_PREEMPTED
- Tests: 8 new passing"
```

---

## Task 2: Wire UserSignalBus into GLS submit() Race

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Test: `tests/governance/test_gap6_gls_race.py`

- [ ] **Step 1: Write failing structural tests for GLS race wiring**

```python
# tests/governance/test_gap6_gls_race.py
"""Structural tests: GovernedLoopService wires UserSignalBus and races in submit()."""
import inspect
import pytest
from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus


def test_gls_has_user_signal_bus_attribute():
    """GLS __init__ must declare _user_signal_bus attribute."""
    source = inspect.getsource(GovernedLoopService.__init__)
    assert "_user_signal_bus" in source, "_user_signal_bus must be initialized in __init__"


def test_gls_submit_references_user_signal_bus():
    """submit() must reference _user_signal_bus for the race path."""
    source = inspect.getsource(GovernedLoopService.submit)
    assert "_user_signal_bus" in source


def test_gls_submit_uses_asyncio_wait():
    """submit() must use asyncio.wait for the race (not just shielded_wait_for)."""
    source = inspect.getsource(GovernedLoopService.submit)
    assert "asyncio.wait" in source


def test_gls_submit_fires_ev_preempt():
    """submit() must fire EV_PREEMPT through the FSM on user stop."""
    source = inspect.getsource(GovernedLoopService.submit)
    assert "EV_PREEMPT" in source


def test_gls_submit_resets_bus_after_stop():
    """submit() must reset the bus after a stop so next op is not pre-stopped."""
    source = inspect.getsource(GovernedLoopService.submit)
    assert ".reset()" in source
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/governance/test_gap6_gls_race.py -v`
Expected: 5 failures (attributes not yet wired)

- [ ] **Step 3: Add `_user_signal_bus` to `GovernedLoopService.__init__()`**

In `governed_loop_service.py`, find the FSM attribute block around line 679–683:
```python
        # Phase 4: preemption FSM — initialized after ledger in start()
        self._fsm_engine: Optional[PreemptionFsmEngine] = None
        self._fsm_executor: Optional[PreemptionFsmExecutor] = None
        self._fsm_contexts: Dict[str, LoopRuntimeContext] = {}
        self._fsm_checkpoint_seq: Dict[str, int] = {}
```

Add immediately after (around line 684):
```python
        # GAP 6: user-initiated stop signal bus (created in _build_components)
        self._user_signal_bus: Optional["UserSignalBus"] = None
```

Also add the import at the top of the file (with other governance imports):
```python
from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus
```

And fix the type annotation on line ~684 to use the actual class (not string):
```python
        self._user_signal_bus: Optional[UserSignalBus] = None
```

- [ ] **Step 4: Wire `UserSignalBus` in `_build_components()`**

Find the `_build_components()` method. Find where `self._approval_provider = CLIApprovalProvider()` is set (around line 2075). Add immediately before it:

```python
        # GAP 6: instantiate user signal bus (always present; silent until request_stop() called)
        self._user_signal_bus = UserSignalBus()
```

- [ ] **Step 5: Modify `submit()` to race op_task vs stop signal**

Find the block in `submit()` around lines 1310–1322:
```python
            try:
                # P1-6: shielded_wait_for ...
                from backend.core.async_safety import shielded_wait_for as _shielded_wf
                terminal_ctx = await _shielded_wf(
                    self._orchestrator.run(ctx),
                    timeout=_pipeline_timeout,
                    name=f"orchestrator.run/{ctx.op_id}",
                )
            except asyncio.TimeoutError:
                ...
                return result
```

Replace the entire `try/except asyncio.TimeoutError` block (from the `try:` line through the `return result` of the timeout path) with the following. **Keep everything after this block unchanged.**

```python
            try:
                # GAP 6: race orchestrator against user stop signal when bus is present
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
                        # User stop: cancel orchestrator, fire EV_PREEMPT, return CANCELLED
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
                        # Timeout: neither finished — mirror existing timeout path
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
                        # Op finished normally — retrieve result
                        terminal_ctx = _op_task.result()

                else:
                    # No signal bus: existing shielded path (ledger writes survive timeout)
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
```

**Important note:** The `except asyncio.TimeoutError:` block that follows is the existing handler for the **no-bus** `_shielded_wf` path. It must remain unchanged. The `asyncio.wait()` path handles its own timeout inline (no TimeoutError raised) and returns early before reaching this except.

Also verify that `build_transition_input` and `LoopEvent` are already imported at the top of GLS (they are — at lines 56–63). No new imports needed for the race code.

- [ ] **Step 6: Run structural tests to confirm pass**

Run: `python -m pytest tests/governance/test_gap6_gls_race.py -v`
Expected: 5 passed

- [ ] **Step 7: Run full governance tests to confirm no regressions**

Run: `python -m pytest tests/governance/ -v --tb=short 2>&1 | tail -30`
Expected: All previously passing tests still pass

- [ ] **Step 8: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/test_gap6_gls_race.py
git commit -m "feat(gap6): wire UserSignalBus into GLS submit() race

- _user_signal_bus instantiated in _build_components()
- submit() races op_task vs stop_task when bus present
- User stop path: fires EV_PREEMPT, resets bus, returns CANCELLED result
- Timeout path: mirrors existing handling (inline, no TimeoutError raised)
- No-bus path: preserves existing shielded_wait_for behavior unchanged
- Structural tests: 5 passing"
```

---

## Task 3: VoiceCommandSensor Stop Detection

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py`
- Test: `tests/governance/test_gap6_voice_stop.py`

- [ ] **Step 1: Write failing tests for VoiceCommandSensor stop detection**

```python
# tests/governance/test_gap6_voice_stop.py
"""VoiceCommandSensor: optional signal_bus fires on stop/cancel commands."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandSensor, VoiceCommandPayload,
)
from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus


def make_sensor(bus=None):
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    return VoiceCommandSensor(router=router, repo="jarvis", signal_bus=bus)


@pytest.mark.asyncio
async def test_stop_command_fires_request_stop():
    bus = UserSignalBus()
    sensor = make_sensor(bus=bus)
    payload = VoiceCommandPayload(
        description="JARVIS stop",
        target_files=["backend/foo.py"],
        repo="jarvis",
        stt_confidence=0.95,
    )
    result = await sensor.handle_voice_command(payload)
    assert result == "stopped"
    assert bus.is_stop_requested()


@pytest.mark.asyncio
async def test_cancel_command_fires_request_stop():
    bus = UserSignalBus()
    sensor = make_sensor(bus=bus)
    payload = VoiceCommandPayload(
        description="JARVIS cancel that",
        target_files=["backend/foo.py"],
        repo="jarvis",
        stt_confidence=0.90,
    )
    result = await sensor.handle_voice_command(payload)
    assert result == "stopped"
    assert bus.is_stop_requested()


@pytest.mark.asyncio
async def test_normal_command_does_not_fire_stop():
    bus = UserSignalBus()
    sensor = make_sensor(bus=bus)
    payload = VoiceCommandPayload(
        description="fix the import in backend/foo.py",
        target_files=["backend/foo.py"],
        repo="jarvis",
        stt_confidence=0.95,
    )
    await sensor.handle_voice_command(payload)
    assert not bus.is_stop_requested()


@pytest.mark.asyncio
async def test_no_bus_stop_command_returns_error():
    """Without a bus, stop command should return 'error' (can't stop without bus)."""
    sensor = make_sensor(bus=None)
    payload = VoiceCommandPayload(
        description="JARVIS stop",
        target_files=["backend/foo.py"],
        repo="jarvis",
        stt_confidence=0.95,
    )
    result = await sensor.handle_voice_command(payload)
    assert result == "error"
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/governance/test_gap6_voice_stop.py -v`
Expected: TypeError — `VoiceCommandSensor.__init__` has no `signal_bus` param

- [ ] **Step 3: Modify `VoiceCommandSensor` to detect stop commands**

In `voice_command_sensor.py`, update `__init__` to accept an optional `signal_bus` parameter, and add stop-phrase detection in `handle_voice_command()`.

Update `__init__` (currently at lines 55–66):
```python
    def __init__(
        self,
        router: Any,
        repo: str,
        stt_confidence_threshold: float = 0.82,
        rate_limit_per_hour: int = 3,
        signal_bus: Any = None,           # Optional[UserSignalBus]
    ) -> None:
        self._router = router
        self._repo = repo
        self._threshold = stt_confidence_threshold
        self._rate_limit = rate_limit_per_hour
        self._op_timestamps: List[float] = []
        self._signal_bus = signal_bus
```

Add `_STOP_PHRASES` constant after the imports:
```python
_STOP_PHRASES = frozenset({"stop", "cancel", "abort", "halt"})
```

Add a `_is_stop_command()` helper method inside the class:
```python
    @staticmethod
    def _is_stop_command(description: str) -> bool:
        """Return True if the description matches a stop/cancel phrase."""
        words = description.lower().split()
        return any(w in _STOP_PHRASES for w in words)
```

At the top of `handle_voice_command()`, before the `if not payload.target_files:` check, add stop detection:

```python
        # GAP 6: detect stop/cancel commands and fire UserSignalBus
        if self._is_stop_command(payload.description):
            if self._signal_bus is not None:
                self._signal_bus.request_stop()
                logger.info("VoiceCommandSensor: stop command detected — bus.request_stop() fired")
                return "stopped"
            else:
                logger.warning("VoiceCommandSensor: stop command received but no signal_bus wired")
                return "error"
```

- [ ] **Step 4: Run voice stop tests to confirm pass**

Run: `python -m pytest tests/governance/test_gap6_voice_stop.py -v`
Expected: 4 passed

- [ ] **Step 5: Run full governance tests to confirm no regressions**

Run: `python -m pytest tests/governance/ -v --tb=short 2>&1 | tail -20`
Expected: All previously passing tests still pass

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py \
        tests/governance/test_gap6_voice_stop.py
git commit -m "feat(gap6): VoiceCommandSensor detects stop/cancel commands

- Optional signal_bus param added to __init__
- _STOP_PHRASES: stop/cancel/abort/halt → request_stop() + return 'stopped'
- No bus + stop command → return 'error' (safe fallback)
- Normal commands unaffected
- Tests: 4 passing"
```

---

## Task 4: SkillRegistry + ContextExpander Wiring

**Files:**
- Create: `backend/core/ouroboros/governance/skill_registry.py`
- Modify: `backend/core/ouroboros/governance/context_expander.py`
- Modify: `backend/core/ouroboros/governance/orchestrator.py:341-345`
- Test: `tests/governance/test_skill_registry.py`
- Test: `tests/governance/test_gap4_expander_skills.py`

- [ ] **Step 1: Write failing tests for SkillRegistry**

```python
# tests/governance/test_skill_registry.py
import pytest
import yaml
from pathlib import Path
from backend.core.ouroboros.governance.skill_registry import SkillRegistry


@pytest.fixture
def skills_dir(tmp_path):
    skills = tmp_path / ".jarvis" / "skills"
    skills.mkdir(parents=True)
    return skills


def write_skill(skills_dir, name, file_pattern, instructions):
    (skills_dir / f"{name}.yaml").write_text(
        yaml.dump({"name": name, "filePattern": file_pattern, "instructions": instructions})
    )


def test_empty_dir_returns_empty_match(tmp_path):
    registry = SkillRegistry(tmp_path)
    assert registry.match(("backend/foo.py",)) == ""


def test_matching_skill_returns_instructions(tmp_path, skills_dir):
    write_skill(skills_dir, "migrations", "migrations/**", "Always wrap in transaction.")
    registry = SkillRegistry(tmp_path)
    result = registry.match(("migrations/0001_create.py",))
    assert "Always wrap in transaction." in result


def test_non_matching_skill_returns_empty(tmp_path, skills_dir):
    write_skill(skills_dir, "migrations", "migrations/**", "Always wrap in transaction.")
    registry = SkillRegistry(tmp_path)
    result = registry.match(("backend/core/foo.py",))
    assert result == ""


def test_multiple_skills_combined(tmp_path, skills_dir):
    write_skill(skills_dir, "migrations", "migrations/**", "Wrap in transaction.")
    write_skill(skills_dir, "tests", "tests/**", "Always use pytest fixtures.")
    registry = SkillRegistry(tmp_path)
    result = registry.match(("migrations/001.py", "tests/test_foo.py"))
    assert "Wrap in transaction." in result
    assert "Always use pytest fixtures." in result


def test_malformed_yaml_skipped_gracefully(tmp_path, skills_dir):
    (skills_dir / "bad.yaml").write_text("{{not: valid: yaml:")
    registry = SkillRegistry(tmp_path)
    assert registry.match(("foo.py",)) == ""


def test_missing_required_fields_skipped(tmp_path, skills_dir):
    (skills_dir / "incomplete.yaml").write_text(yaml.dump({"name": "incomplete"}))
    registry = SkillRegistry(tmp_path)
    assert registry.match(("foo.py",)) == ""


def test_no_skills_dir_is_not_an_error(tmp_path):
    registry = SkillRegistry(tmp_path)  # .jarvis/skills does not exist
    assert registry.match(("foo.py",)) == ""
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/governance/test_skill_registry.py -v`
Expected: ImportError — `skill_registry` not found

- [ ] **Step 3: Create `skill_registry.py`**

```python
# backend/core/ouroboros/governance/skill_registry.py
"""SkillRegistry — loads domain-specific instruction files from .jarvis/skills/*.yaml.

GAP 4: matches operation target files against per-skill filePattern globs.
Matching skills have their instructions concatenated and returned for injection
into OperationContext.human_instructions via ContextExpander.

YAML schema (each .jarvis/skills/<name>.yaml):
    name: migration_safety
    filePattern: "migrations/**"
    instructions: |
      Always create the migration in a transaction.
      Never drop columns in the same migration that removes all usages.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Skill:
    name: str
    file_pattern: str
    instructions: str


class SkillRegistry:
    """Loads and matches domain skills against operation target files.

    Parameters
    ----------
    repo_root:
        Root of the repository; skills are loaded from
        ``<repo_root>/.jarvis/skills/*.yaml``.
    """

    def __init__(self, repo_root: Path) -> None:
        self._skills: Tuple[_Skill, ...] = tuple(
            self._load_skills(Path(repo_root) / ".jarvis" / "skills")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(self, file_paths: Sequence[str]) -> str:
        """Return concatenated instructions for all skills that match any of the target files.

        Returns empty string when no skills match.
        """
        matched: List[str] = []
        for skill in self._skills:
            if any(self._matches(fp, skill.file_pattern) for fp in file_paths):
                matched.append(f"### Skill: {skill.name}\n\n{skill.instructions.strip()}")
        return "\n\n".join(matched)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(file_path: str, pattern: str) -> bool:
        """fnmatch-based glob matching (supports *, ?, ** via path normalization)."""
        # Normalise separators to forward slash for consistent matching
        fp = file_path.replace("\\", "/")
        return fnmatch.fnmatch(fp, pattern) or fnmatch.fnmatch(fp.split("/")[-1], pattern)

    @staticmethod
    def _load_skills(skills_dir: Path) -> List[_Skill]:
        if not skills_dir.is_dir():
            return []
        try:
            import yaml  # type: ignore[import]
        except ImportError:
            logger.warning("[SkillRegistry] PyYAML not installed — skills disabled")
            return []

        skills = []
        for path in sorted(skills_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                name = data.get("name", "")
                pattern = data.get("filePattern", "")
                instructions = data.get("instructions", "")
                if not (name and pattern and instructions):
                    logger.debug("[SkillRegistry] Skipping incomplete skill: %s", path.name)
                    continue
                skills.append(_Skill(name=str(name), file_pattern=str(pattern), instructions=str(instructions)))
                logger.debug("[SkillRegistry] Loaded skill '%s' (pattern: %s)", name, pattern)
            except Exception as exc:
                logger.warning("[SkillRegistry] Skipping malformed skill %s: %s", path.name, exc)
        return skills
```

- [ ] **Step 4: Run SkillRegistry tests to confirm pass**

Run: `python -m pytest tests/governance/test_skill_registry.py -v`
Expected: 7 passed

- [ ] **Step 5: Write failing tests for ContextExpander + SkillRegistry integration**

```python
# tests/governance/test_gap4_expander_skills.py
"""ContextExpander appends matched skill instructions to human_instructions."""
import inspect
import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.governance.context_expander import ContextExpander
from backend.core.ouroboros.governance.skill_registry import SkillRegistry


def test_context_expander_accepts_skill_registry_param():
    """ContextExpander.__init__ must accept skill_registry keyword arg."""
    sig = inspect.signature(ContextExpander.__init__)
    assert "skill_registry" in sig.parameters


def test_context_expander_calls_skill_match_in_expand_source():
    """expand() must call skill_registry.match()."""
    source = inspect.getsource(ContextExpander.expand)
    assert "skill_registry" in source
    assert "match" in source


@pytest.mark.asyncio
async def test_expand_appends_skill_instructions_when_oracle_not_ready(tmp_path):
    """When oracle not ready, skill instructions still injected via human_instructions."""
    # Write a skill
    skills_dir = tmp_path / ".jarvis" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "migs.yaml").write_text(
        yaml.dump({"name": "migs", "filePattern": "migrations/**", "instructions": "Use transactions."})
    )

    from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
    from datetime import datetime, timezone, timedelta

    ctx = OperationContext(
        op_id="test-op-skills",
        description="add migration",
        target_files=("migrations/0001_add_col.py",),
    )

    oracle_mock = MagicMock()
    oracle_mock.is_ready.return_value = False

    registry = SkillRegistry(tmp_path)
    generator = MagicMock()
    expander = ContextExpander(
        generator=generator,
        repo_root=tmp_path,
        oracle=oracle_mock,
        skill_registry=registry,
    )

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    result_ctx = await expander.expand(ctx, deadline)

    assert "Use transactions." in result_ctx.human_instructions
```

- [ ] **Step 6: Run to confirm failure**

Run: `python -m pytest tests/governance/test_gap4_expander_skills.py -v`
Expected: 2 structural tests pass (inspect checks), 1 asyncio test fails (instructions not yet injected)

- [ ] **Step 7: Add `skill_registry` param to `ContextExpander.__init__()`**

In `context_expander.py`, update `__init__` (currently at lines 48–56):

```python
    def __init__(
        self,
        generator: Any,
        repo_root: Path,
        oracle: Optional[Any] = None,
        skill_registry: Optional[Any] = None,   # Optional[SkillRegistry]
    ) -> None:
        self._generator = generator
        self._repo_root = repo_root
        self._oracle = oracle
        self._skill_registry = skill_registry
```

- [ ] **Step 8: Call `skill_registry.match()` at end of `expand()`**

In `context_expander.py`, find the end of `expand()` (around line 159):
```python
        if not accumulated:
            return ctx

        # Deduplicate while preserving insertion order
        ...
        return ctx.with_expanded_files(tuple(deduped))
```

Replace the final two lines of `expand()` with:
```python
        if accumulated:
            # Deduplicate while preserving insertion order
            seen: set = set()
            deduped: List[str] = []
            for p in accumulated:
                if p not in seen:
                    seen.add(p)
                    deduped.append(p)
            ctx = ctx.with_expanded_files(tuple(deduped))

        # GAP 4: inject matching skill instructions into human_instructions
        if self._skill_registry is not None:
            try:
                _skill_instr = self._skill_registry.match(ctx.target_files)
                if _skill_instr:
                    existing = getattr(ctx, "human_instructions", "") or ""
                    combined = (existing.strip() + "\n\n" + _skill_instr).strip() if existing.strip() else _skill_instr
                    ctx = ctx.with_human_instructions(combined)
                    logger.debug(
                        "[ContextExpander] op=%s: injected %d char skill instructions",
                        ctx.op_id, len(_skill_instr),
                    )
            except Exception as exc:
                logger.warning("[ContextExpander] op=%s skill_registry.match failed: %s", ctx.op_id, exc)

        return ctx
```

**Important:** Remove the old separate `if not accumulated: return ctx` early return and the separate dedup+return block. The new code handles both accumulated and non-accumulated paths (when no files accumulated, `ctx` is unchanged before skills injection).

- [ ] **Step 9: Wire `SkillRegistry` in `orchestrator.py` CONTEXT_EXPANSION**

In `orchestrator.py`, find the CONTEXT_EXPANSION block (around lines 341–345):
```python
                expander = ContextExpander(
                    generator=self._generator,
                    repo_root=self._config.project_root,
                    oracle=getattr(self._stack, "oracle", None),
                )
```

Replace with:
```python
                from backend.core.ouroboros.governance.skill_registry import SkillRegistry as _SkillRegistry
                _skill_registry = _SkillRegistry(self._config.project_root)
                expander = ContextExpander(
                    generator=self._generator,
                    repo_root=self._config.project_root,
                    oracle=getattr(self._stack, "oracle", None),
                    skill_registry=_skill_registry,
                )
```

- [ ] **Step 10: Run integration tests to confirm pass**

Run: `python -m pytest tests/governance/test_gap4_expander_skills.py -v`
Expected: 3 passed

- [ ] **Step 11: Run full governance tests to confirm no regressions**

Run: `python -m pytest tests/governance/ -v --tb=short 2>&1 | tail -20`
Expected: All previously passing tests still pass

- [ ] **Step 12: Commit**

```bash
git add backend/core/ouroboros/governance/skill_registry.py \
        backend/core/ouroboros/governance/context_expander.py \
        backend/core/ouroboros/governance/orchestrator.py \
        tests/governance/test_skill_registry.py \
        tests/governance/test_gap4_expander_skills.py
git commit -m "feat(gap4): SkillRegistry injects domain instructions via ContextExpander

- SkillRegistry: loads .jarvis/skills/*.yaml; match() returns instructions for target files
- ContextExpander: skill_registry param; appends matched skills to human_instructions
- Orchestrator: SkillRegistry instantiated per-op in CONTEXT_EXPANSION
- Composes naturally with GAP 3 (ContextMemoryLoader) via with_human_instructions()
- Tests: 10 new passing"
```

---

## Task 5: CorrectionWriter + CLIApprovalProvider Auto-Memory

**Files:**
- Create: `backend/core/ouroboros/governance/correction_writer.py`
- Modify: `backend/core/ouroboros/governance/approval_provider.py`
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py:2075`
- Test: `tests/governance/test_correction_writer.py`
- Test: `tests/governance/test_gap8_approval_wire.py`

- [ ] **Step 1: Write failing tests for CorrectionWriter**

```python
# tests/governance/test_correction_writer.py
import pytest
from datetime import datetime, timezone
from pathlib import Path
from backend.core.ouroboros.governance.correction_writer import write_correction


def test_creates_ouroboros_md_if_missing(tmp_path):
    write_correction(
        project_root=tmp_path,
        op_id="op-001",
        reason="Don't use subprocess.run in async context",
        timestamp=datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    md = tmp_path / "OUROBOROS.md"
    assert md.exists()
    content = md.read_text()
    assert "## Auto-Learned Corrections" in content
    assert "op:op-001" in content
    assert "Don't use subprocess.run" in content


def test_appends_to_existing_section(tmp_path):
    md = tmp_path / "OUROBOROS.md"
    md.write_text("# Project Config\n\n## Auto-Learned Corrections\n- old correction\n")
    write_correction(
        project_root=tmp_path,
        op_id="op-002",
        reason="Use pathlib not os.path",
        timestamp=datetime(2026, 3, 20, 13, 0, 0, tzinfo=timezone.utc),
    )
    content = md.read_text()
    assert "old correction" in content
    assert "op:op-002" in content
    assert "Use pathlib not os.path" in content


def test_creates_section_in_existing_file_without_it(tmp_path):
    md = tmp_path / "OUROBOROS.md"
    md.write_text("# Project Config\n\nSome project notes.\n")
    write_correction(
        project_root=tmp_path,
        op_id="op-003",
        reason="new lesson",
        timestamp=datetime(2026, 3, 20, 14, 0, 0, tzinfo=timezone.utc),
    )
    content = md.read_text()
    assert "## Auto-Learned Corrections" in content
    assert "op:op-003" in content


def test_empty_reason_is_skipped(tmp_path):
    write_correction(project_root=tmp_path, op_id="op-004", reason="  ", timestamp=datetime.now(timezone.utc))
    md = tmp_path / "OUROBOROS.md"
    assert not md.exists() or "op:op-004" not in md.read_text()


def test_write_error_does_not_raise(tmp_path):
    """IO failures must be silently swallowed — never crash the approval path."""
    # Pass a file path as project_root — write will fail gracefully
    fake_root = tmp_path / "nonexistent_dir" / "deep"
    write_correction(project_root=fake_root, op_id="op-005", reason="test", timestamp=datetime.now(timezone.utc))
    # No exception raised
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/governance/test_correction_writer.py -v`
Expected: ImportError — `correction_writer` not found

- [ ] **Step 3: Create `correction_writer.py`**

```python
# backend/core/ouroboros/governance/correction_writer.py
"""CorrectionWriter — appends human rejection reasons to OUROBOROS.md.

GAP 8: when CLIApprovalProvider.reject() is called with a reason, the
correction is appended to <project_root>/OUROBOROS.md under a persistent
## Auto-Learned Corrections section.  This feeds directly into GAP 3
(ContextMemoryLoader) so the AI learns from human corrections automatically.

This module is intentionally standalone (no imports from governance core)
so it can be called from approval_provider.py without circular imports.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SECTION_HEADER = "## Auto-Learned Corrections"


def write_correction(
    project_root: Path,
    op_id: str,
    reason: str,
    timestamp: Optional[datetime] = None,
) -> None:
    """Append a human correction to <project_root>/OUROBOROS.md.

    Silently swallows all IO errors so approval_provider.reject() never raises
    due to a filesystem issue.

    Parameters
    ----------
    project_root:
        Root of the repository; OUROBOROS.md is at project_root/OUROBOROS.md.
    op_id:
        The operation ID (used as a reference in the correction entry).
    reason:
        Free-text rejection reason provided by the human approver.
    timestamp:
        Timestamp of the rejection (defaults to UTC now).
    """
    if not reason or not reason.strip():
        return

    ts = timestamp or datetime.now(tz=timezone.utc)
    date_str = ts.strftime("%Y-%m-%d")
    entry = f"- {date_str} op:{op_id}: {reason.strip()}"

    try:
        md_path = Path(project_root) / "OUROBOROS.md"

        if md_path.exists():
            existing = md_path.read_text(encoding="utf-8")
        else:
            existing = ""

        if _SECTION_HEADER in existing:
            # Append entry after the section header (preserving following content)
            updated = existing.rstrip() + "\n" + entry + "\n"
        else:
            # Create section at end of file
            separator = "\n\n" if existing.strip() else ""
            updated = existing.rstrip() + separator + f"\n{_SECTION_HEADER}\n{entry}\n"

        md_path.write_text(updated, encoding="utf-8")
        logger.info("[CorrectionWriter] Appended correction for op=%s to %s", op_id, md_path)

    except Exception as exc:
        logger.warning("[CorrectionWriter] Failed to write correction for op=%s: %s", op_id, exc)
```

- [ ] **Step 4: Run CorrectionWriter tests to confirm pass**

Run: `python -m pytest tests/governance/test_correction_writer.py -v`
Expected: 5 passed

- [ ] **Step 5: Write failing tests for CLIApprovalProvider auto-memory wiring**

```python
# tests/governance/test_gap8_approval_wire.py
"""CLIApprovalProvider: rejection writes correction to OUROBOROS.md when project_root set."""
import asyncio
import inspect
import pytest
from pathlib import Path
from backend.core.ouroboros.governance.approval_provider import CLIApprovalProvider
from backend.core.ouroboros.governance.op_context import OperationContext


def test_cli_approval_provider_accepts_project_root():
    sig = inspect.signature(CLIApprovalProvider.__init__)
    assert "project_root" in sig.parameters


@pytest.mark.asyncio
async def test_reject_writes_correction_when_project_root_set(tmp_path):
    provider = CLIApprovalProvider(project_root=tmp_path)
    ctx = OperationContext(
        op_id="op-abc",
        description="add feature",
        target_files=("backend/foo.py",),
    )
    await provider.request(ctx)
    await provider.reject(request_id="op-abc", approver="human", reason="Don't use global state here")

    md = tmp_path / "OUROBOROS.md"
    assert md.exists()
    content = md.read_text()
    assert "op:op-abc" in content
    assert "Don't use global state here" in content


@pytest.mark.asyncio
async def test_reject_no_correction_when_project_root_none():
    """Without project_root, rejection succeeds but no file is written."""
    provider = CLIApprovalProvider()  # no project_root
    ctx = OperationContext(
        op_id="op-xyz",
        description="refactor",
        target_files=("backend/bar.py",),
    )
    await provider.request(ctx)
    result = await provider.reject(request_id="op-xyz", approver="human", reason="some reason")
    from backend.core.ouroboros.governance.approval_provider import ApprovalStatus
    assert result.status == ApprovalStatus.REJECTED


@pytest.mark.asyncio
async def test_reject_empty_reason_does_not_crash(tmp_path):
    provider = CLIApprovalProvider(project_root=tmp_path)
    ctx = OperationContext(op_id="op-empty", description="x", target_files=("a.py",))
    await provider.request(ctx)
    # Empty reason: should not raise, no file created
    result = await provider.reject(request_id="op-empty", approver="human", reason="  ")
    from backend.core.ouroboros.governance.approval_provider import ApprovalStatus
    assert result.status == ApprovalStatus.REJECTED
    md = tmp_path / "OUROBOROS.md"
    assert not md.exists() or "op:op-empty" not in md.read_text()
```

- [ ] **Step 6: Run to confirm failure**

Run: `python -m pytest tests/governance/test_gap8_approval_wire.py -v`
Expected: TypeError — `CLIApprovalProvider.__init__` takes no `project_root` param

- [ ] **Step 7: Modify `CLIApprovalProvider.__init__()` to accept `project_root`**

In `approval_provider.py`, update the `CLIApprovalProvider` class (currently at line 255):

```python
    def __init__(self, project_root: Optional["Path"] = None) -> None:
        self._requests: Dict[str, _PendingRequest] = {}
        self._project_root = project_root
```

Add `from pathlib import Path` to the imports at the top of `approval_provider.py` if not already present. (It's not — add it.)

- [ ] **Step 8: Call `write_correction()` in `CLIApprovalProvider.reject()`**

In `approval_provider.py`, find the `reject()` method (line 318). Locate where `result = ApprovalResult(status=ApprovalStatus.REJECTED, ...)` is built (around line 343). After the result is constructed and before `pending.event.set()`, add the correction write:

```python
        result = ApprovalResult(
            status=ApprovalStatus.REJECTED,
            approver=approver,
            reason=reason,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )
        # GAP 8: auto-memory — persist rejection reason to OUROBOROS.md
        if self._project_root is not None:
            try:
                from backend.core.ouroboros.governance.correction_writer import write_correction
                write_correction(
                    project_root=self._project_root,
                    op_id=request_id,
                    reason=reason,
                )
            except Exception as _exc:
                logger.warning("[Approval] correction_writer failed for op=%s: %s", request_id, _exc)
        pending.result = result
        pending.event.set()
        logger.info("[Approval] REJECTED: %s by %s reason=%r", request_id, approver, reason)
        return result
```

- [ ] **Step 9: Wire `project_root` into `CLIApprovalProvider` in `_build_components()`**

In `governed_loop_service.py`, find line ~2075:
```python
        self._approval_provider = CLIApprovalProvider()
```

Replace with:
```python
        self._approval_provider = CLIApprovalProvider(
            project_root=self._config.project_root,
        )
```

- [ ] **Step 10: Run auto-memory tests to confirm pass**

Run: `python -m pytest tests/governance/test_gap8_approval_wire.py -v`
Expected: 3 passed

- [ ] **Step 11: Run full governance tests to confirm no regressions**

Run: `python -m pytest tests/governance/ -v --tb=short 2>&1 | tail -30`
Expected: All previously passing tests still pass

- [ ] **Step 12: Commit**

```bash
git add backend/core/ouroboros/governance/correction_writer.py \
        backend/core/ouroboros/governance/approval_provider.py \
        backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/test_correction_writer.py \
        tests/governance/test_gap8_approval_wire.py
git commit -m "feat(gap8): CorrectionWriter auto-appends rejections to OUROBOROS.md

- correction_writer.write_correction(): appends under ## Auto-Learned Corrections
- Creates section if absent; gracefully handles IO failures
- CLIApprovalProvider: project_root param; calls write_correction() on reject()
- GLS _build_components: passes project_root to CLIApprovalProvider
- Feeds directly into GAP 3 ContextMemoryLoader (OUROBOROS.md is already loaded)
- Tests: 8 new passing"
```

---

## Final Verification

- [ ] **Run entire governance test suite**

```bash
python -m pytest tests/governance/ -v --tb=short 2>&1 | tail -40
```

Expected: All new tests pass; 9 known pre-existing failures unchanged (test_preflight.py uses `__new__`, test_e2e.py, test_pipeline_deadline.py, test_phase2c_acceptance.py).

- [ ] **Verify new test count**

```bash
python -m pytest tests/governance/ --collect-only -q 2>&1 | tail -5
```

Expected: ~40+ tests collected (was ~29 before this batch; adding ~27 new tests across 7 new files).

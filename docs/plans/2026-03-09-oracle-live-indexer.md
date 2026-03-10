# Oracle Live Indexer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire `TheOracle` GraphRAG (already built, never started) into `GovernedLoopService` as a non-blocking background task that indexes all three repos on boot, keeps the graph live after every applied patch, and feeds `ContextExpander` a real file manifest so J-Prime stops guessing paths blind.

**Architecture:** A 5th background task (`_oracle_index_loop`) in `GovernedLoopService` calls `oracle.initialize()` non-blocking after service start, sets `GovernanceStack.oracle` once ready, and polls for incremental changes every 5 minutes. `ContextExpander` checks readiness before every expansion and injects top-20 relevant files into the planning prompt. `GovernedOrchestrator` calls `oracle.incremental_update()` after every successful COMPLETE in both the single-repo and saga paths. All oracle calls are fault-isolated; failure at any point never changes service state or operation terminal state.

**Tech Stack:** Python 3.9+, asyncio, NetworkX (optional — oracle gracefully skips if absent), pytest (asyncio_mode=auto — NEVER use `@pytest.mark.asyncio`).

**Design doc:** `docs/plans/2026-03-09-oracle-live-indexer-design.md`

---

## Task 1: GovernanceStack.oracle field + GovernedLoopConfig oracle settings

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py` (~line 326, GovernanceStack dataclass)
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (~line 228, GovernedLoopConfig dataclass)
- Test: `tests/test_ouroboros_governance/test_governed_loop_service.py`

### Context

`GovernanceStack` is a dataclass in `backend/core/ouroboros/governance/integration.py` lines 285–327. It already has an optional `performance_persistence` field (line ~326) added in Phase 2. Add `oracle` the same way. `GovernedLoopConfig` is a frozen dataclass in `governed_loop_service.py` lines 163–228. The most recently added fields are the curriculum/reactor fields (lines 181–189). Add oracle config the same way.

### Step 1: Write the failing tests

Add to `tests/test_ouroboros_governance/test_governed_loop_service.py` (at the end of the file):

```python
class TestOracleConfig:
    """Tests for oracle fields on GovernedLoopConfig and GovernanceStack."""

    def test_governed_loop_config_oracle_enabled_default(self):
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        from pathlib import Path
        config = GovernedLoopConfig(project_root=Path("/tmp"))
        assert config.oracle_enabled is True

    def test_governed_loop_config_oracle_poll_interval_default(self):
        from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
        from pathlib import Path
        config = GovernedLoopConfig(project_root=Path("/tmp"))
        assert config.oracle_incremental_poll_interval_s == 300.0

    def test_governance_stack_oracle_defaults_none(self):
        from backend.core.ouroboros.governance.integration import GovernanceStack
        stack = GovernanceStack.__new__(GovernanceStack)
        # oracle field must exist and default to None
        assert getattr(stack, "oracle", "MISSING") == "MISSING" or True
        # After Task 1, oracle should be an explicit field defaulting to None
```

Run to confirm failure:
```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestOracleConfig -v 2>&1 | head -20
```
Expected: AttributeError or AssertionError (fields don't exist yet).

### Step 2: Add `oracle_enabled` and `oracle_incremental_poll_interval_s` to GovernedLoopConfig

In `backend/core/ouroboros/governance/governed_loop_service.py`, find the `GovernedLoopConfig` dataclass (lines 163–228). After the `reactor_event_poll_interval_s` field (last line of the dataclass, currently ~line 189), add:

```python
    oracle_enabled: bool = True
    oracle_incremental_poll_interval_s: float = 300.0
```

### Step 3: Add `oracle` field to GovernanceStack

In `backend/core/ouroboros/governance/integration.py`, find `GovernanceStack` (lines 285–327). After the `performance_persistence` field (last optional field before `_started`), add:

```python
    oracle: Optional[Any] = None
```

`Optional[Any]` matches the existing `performance_persistence` pattern and avoids a new import. `TheOracle` is in a different package and using `Any` keeps this clean.

### Step 4: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestOracleConfig -v
```
Expected: All 3 tests PASS. (Rewrite `test_governance_stack_oracle_defaults_none` to actually assert `stack_instance.oracle is None` once you can construct one cleanly — or just assert the field exists via dataclass fields introspection.)

Better test for GovernanceStack (replace the placeholder):
```python
    def test_governance_stack_oracle_defaults_none(self):
        import dataclasses
        from backend.core.ouroboros.governance.integration import GovernanceStack
        field_names = {f.name for f in dataclasses.fields(GovernanceStack)}
        assert "oracle" in field_names
        defaults = {f.name: f.default for f in dataclasses.fields(GovernanceStack) if f.name == "oracle"}
        assert defaults["oracle"] is None
```

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/integration.py \
        backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_governed_loop_service.py
git commit -m "feat(oracle): add GovernanceStack.oracle field and oracle config to GovernedLoopConfig"
```

---

## Task 2: GovernedLoopService — oracle background task lifecycle

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Test: `tests/test_ouroboros_governance/test_governed_loop_service.py` (extend TestOracleConfig or add TestOracleIndexerLifecycle)

### Context

`GovernedLoopService.__init__` adds instance variables at lines 242–272. Background tasks are declared at lines 264–268 (`_curriculum_task`, `_reactor_event_task`, etc.). Tasks are created in `start()` at lines 320–325 (curriculum/reactor) and 328–330 (health probe). `stop()` cancels tasks at lines 376–383 using a for-loop over task attribute names. Follow the EXACT same pattern for the oracle task.

### Step 1: Write the failing tests

Add `TestOracleIndexerLifecycle` to `tests/test_ouroboros_governance/test_governed_loop_service.py`:

```python
class TestOracleIndexerLifecycle:
    """Oracle indexer task starts non-blocking and failure never fails the service."""

    async def test_oracle_indexer_failure_does_not_fail_service_start(self):
        """If oracle.initialize() raises, service still becomes ACTIVE/DEGRADED."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig,
        )
        from pathlib import Path
        config = GovernedLoopConfig(
            project_root=Path("/tmp"),
            oracle_enabled=True,
            curriculum_enabled=False,
        )
        service = GovernedLoopService(config=config)
        with (
            patch.object(service, "_build_components", new=AsyncMock()),
            patch.object(service, "_reconcile_on_boot", new=AsyncMock()),
            patch.object(service, "_register_canary_slices"),
            patch.object(service, "_attach_to_stack"),
            patch.object(service, "_health_probe_loop", new=AsyncMock()),
            patch(
                "backend.core.ouroboros.governance.governed_loop_service.TheOracle",
                side_effect=RuntimeError("oracle boom"),
            ),
        ):
            service._generator = None
            await service.start()
            # Service must have started despite oracle failure
            assert service._oracle_indexer_task is not None
            # Wait briefly for the background task to run and fail
            import asyncio
            await asyncio.sleep(0.05)
            # Task should have exited (done) after the exception
            assert service._oracle_indexer_task.done()
            # oracle must be None (not set)
            assert service._oracle is None
            await service.stop()

    async def test_oracle_indexer_task_cancelled_on_stop(self):
        """oracle_indexer_task is cancelled when service stops."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig,
        )
        from pathlib import Path
        import asyncio
        config = GovernedLoopConfig(
            project_root=Path("/tmp"),
            oracle_enabled=True,
            oracle_incremental_poll_interval_s=9999.0,  # never polls during test
            curriculum_enabled=False,
        )
        service = GovernedLoopService(config=config)
        mock_oracle = MagicMock()
        mock_oracle.initialize = AsyncMock()
        mock_oracle.incremental_update = AsyncMock()
        mock_oracle.shutdown = AsyncMock()
        mock_oracle.get_status = MagicMock(return_value={"running": True})
        with (
            patch.object(service, "_build_components", new=AsyncMock()),
            patch.object(service, "_reconcile_on_boot", new=AsyncMock()),
            patch.object(service, "_register_canary_slices"),
            patch.object(service, "_attach_to_stack"),
            patch.object(service, "_health_probe_loop", new=AsyncMock()),
            patch(
                "backend.core.ouroboros.governance.governed_loop_service.TheOracle",
                return_value=mock_oracle,
            ),
        ):
            service._generator = None
            await service.start()
            await asyncio.sleep(0.05)  # let oracle initialize
            oracle_task = service._oracle_indexer_task
            await service.stop()
            assert oracle_task.done()
```

Run:
```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestOracleIndexerLifecycle -v 2>&1 | head -30
```
Expected: AttributeError on `_oracle_indexer_task` (not yet added).

### Step 2: Add `_oracle_indexer_task` and `_oracle` instance variables to `__init__`

In `governed_loop_service.py`, in `__init__` after the `_event_dir` line (~line 268), add:

```python
        self._oracle_indexer_task: Optional[asyncio.Task] = None
        self._oracle: Optional[Any] = None
```

### Step 3: Add oracle task creation to `start()`

In `start()`, after the curriculum task block (~line 325) and BEFORE the health probe task (line 328), add:

```python
        if self._config.oracle_enabled:
            self._oracle_indexer_task = asyncio.create_task(
                self._oracle_index_loop(), name="oracle_index_loop"
            )
```

### Step 4: Add oracle task cancellation to `stop()`

In `stop()`, find the for-loop that cancels `_curriculum_task` and `_reactor_event_task` (lines 376–383):

```python
        for task_attr in ("_curriculum_task", "_reactor_event_task"):
```

Change to:

```python
        for task_attr in ("_curriculum_task", "_reactor_event_task", "_oracle_indexer_task"):
```

### Step 5: Add import and `_oracle_index_loop()` method

At the top of `governed_loop_service.py`, after the existing governance imports (around line 49–51 where CurriculumPublisher and ModelAttributionRecorder are imported), add nothing yet — use a local import inside the method to keep oracle optional.

Add `_oracle_index_loop()` as a new method near `_curriculum_loop` and `_reactor_event_loop`:

```python
    async def _oracle_index_loop(self) -> None:
        """Index all 3 repos into TheOracle graph on boot, then poll for changes.

        Non-blocking: start() never awaits this. Fault-isolated: any exception
        sets self._oracle = None, logs a structured warning, and exits the task
        without impacting service state or any operation's terminal phase.
        """
        try:
            from backend.core.ouroboros.oracle import TheOracle
            oracle = TheOracle()
            await oracle.initialize()
            self._oracle = oracle
            if self._stack is not None:
                self._stack.oracle = oracle
            logger.info(
                "[GovernedLoop] Oracle indexed %s nodes across all repos",
                oracle.get_metrics().get("total_nodes", "?"),
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "[GovernedLoop] Oracle initialization failed: %s; codebase graph unavailable",
                exc,
            )
            self._oracle = None
            return

        # Incremental update loop — polls every oracle_incremental_poll_interval_s
        while True:
            try:
                await asyncio.sleep(self._config.oracle_incremental_poll_interval_s)
                await self._oracle.incremental_update([])
            except asyncio.CancelledError:
                await self._oracle.shutdown()
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] Oracle incremental update failed: %s", exc)
```

### Step 6: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestOracleIndexerLifecycle -v
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py -v 2>&1 | tail -10
```
Expected: both new tests PASS, no regressions.

### Step 7: Commit

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_governed_loop_service.py
git commit -m "feat(oracle): add _oracle_index_loop background task to GovernedLoopService"
```

---

## Task 3: ContextExpander — oracle manifest injection

**Files:**
- Modify: `backend/core/ouroboros/governance/context_expander.py` (lines 47–90)
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (find ContextExpander instantiation)
- Test: `tests/test_ouroboros_governance/test_context_expander.py`

### Context

`ContextExpander.__init__` (lines 47–49) currently takes `generator: Any` and `repo_root: Path`. The `expand()` method (lines 51–122) loops MAX_ROUNDS=2 times, building a planning prompt at each round and calling `generator.plan()`. We add an optional `oracle` parameter to `__init__`. At the start of `expand()`, BEFORE the round loop (before line 75), we query the oracle for a file manifest and inject it into the prompt template.

To find where ContextExpander is instantiated in the orchestrator, run:
```bash
grep -n "ContextExpander(" backend/core/ouroboros/governance/orchestrator.py
```
Then pass `oracle=getattr(self._stack, "oracle", None)` at that call site.

### Step 1: Write the failing tests

Add `TestContextExpanderOracleManifest` to `tests/test_ouroboros_governance/test_context_expander.py`:

```python
class TestContextExpanderOracleManifest:
    """Oracle manifest injection into planning prompt."""

    async def test_oracle_manifest_injected_when_ready(self, tmp_path):
        """When oracle is ready, planning prompt includes available files list."""
        from unittest.mock import AsyncMock, MagicMock
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.governance.op_context import OperationContext
        from datetime import datetime, timezone, timedelta

        oracle = MagicMock()
        oracle.get_status = MagicMock(return_value={"running": True})
        oracle.get_relevant_files_for_query = AsyncMock(
            return_value=[tmp_path / "foo.py", tmp_path / "bar.py"]
        )

        captured_prompts = []
        mock_gen = MagicMock()
        async def fake_plan(prompt, deadline):
            captured_prompts.append(prompt)
            return '{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        mock_gen.plan = fake_plan

        expander = ContextExpander(generator=mock_gen, repo_root=tmp_path, oracle=oracle)
        ctx = OperationContext.create(
            target_files=("foo.py",), description="fix async timeout"
        )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        await expander.expand(ctx, deadline)

        assert len(captured_prompts) > 0
        assert "Available files" in captured_prompts[0]
        assert "foo.py" in captured_prompts[0] or "bar.py" in captured_prompts[0]

    async def test_oracle_fallback_when_not_ready(self, tmp_path):
        """When oracle.get_status() returns running=False, expand() runs without raising."""
        from unittest.mock import AsyncMock, MagicMock
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.governance.op_context import OperationContext
        from datetime import datetime, timezone, timedelta

        oracle = MagicMock()
        oracle.get_status = MagicMock(return_value={"running": False})
        oracle.get_relevant_files_for_query = AsyncMock(return_value=[])

        mock_gen = MagicMock()
        mock_gen.plan = AsyncMock(
            return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        )

        expander = ContextExpander(generator=mock_gen, repo_root=tmp_path, oracle=oracle)
        ctx = OperationContext.create(
            target_files=("foo.py",), description="fix async timeout"
        )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        result = await expander.expand(ctx, deadline)
        # Must not raise; oracle.get_relevant_files_for_query must NOT have been called
        oracle.get_relevant_files_for_query.assert_not_called()

    async def test_oracle_fallback_when_none(self, tmp_path):
        """When oracle=None, expand() runs exactly as before."""
        from unittest.mock import AsyncMock, MagicMock
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.governance.op_context import OperationContext
        from datetime import datetime, timezone, timedelta

        mock_gen = MagicMock()
        mock_gen.plan = AsyncMock(
            return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        )

        expander = ContextExpander(generator=mock_gen, repo_root=tmp_path)  # no oracle
        ctx = OperationContext.create(
            target_files=("foo.py",), description="fix async timeout"
        )
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        result = await expander.expand(ctx, deadline)  # must not raise
```

Run:
```bash
python3 -m pytest tests/test_ouroboros_governance/test_context_expander.py::TestContextExpanderOracleManifest -v 2>&1 | head -20
```
Expected: TypeError (unexpected `oracle` keyword arg) — `__init__` doesn't accept it yet.

### Step 2: Add `oracle` parameter to `ContextExpander.__init__`

In `context_expander.py`, find `__init__` (lines 47–49). Change it to:

```python
    def __init__(
        self,
        generator: Any,
        repo_root: Path,
        oracle: Optional[Any] = None,
    ) -> None:
        self._generator = generator
        self._repo_root = repo_root
        self._oracle = oracle
```

Add `Optional` to imports if not already present. Check line 17+ for the typing import; `Optional` is in the standard typing import.

### Step 3: Inject oracle manifest before the round loop

In `context_expander.py`, find `expand()` (lines 51–122). BEFORE the `for round_num in range(MAX_ROUNDS):` line (~line 75), add:

```python
        # Oracle manifest — real file paths for J-Prime to choose from
        oracle_files: list[str] = []
        if self._oracle is not None:
            try:
                if self._oracle.get_status().get("running", False):
                    raw_paths = await self._oracle.get_relevant_files_for_query(
                        ctx.description, limit=20
                    )
                    oracle_files = [
                        str(p.relative_to(self._repo_root)) if hasattr(p, "relative_to") else str(p)
                        for p in raw_paths
                    ]
            except Exception:
                oracle_files = []  # fall back silently
```

Then pass `oracle_files` into the prompt-building function. Find the function/call that builds the planning prompt (look for where `description` and `target_files` are formatted into a string). Extend it to include an "Available files" section when `oracle_files` is non-empty:

```python
        # Inside the round loop, when building the prompt:
        available_section = ""
        if oracle_files:
            available_section = (
                "\nAvailable files related to this task (real paths — choose from these):\n"
                + "".join(f"  - {f}\n" for f in oracle_files)
                + "\nWhich of these (if any) would help you generate a correct patch?\n"
            )
        # Append available_section to the planning prompt string before calling plan()
```

**Important:** Read the actual `_build_expansion_prompt` or equivalent function in context_expander.py carefully. The exact place to inject is wherever the prompt string is assembled. Add `oracle_files` as a parameter to that helper if it's a separate function, or inline if the prompt is built inline.

### Step 4: Pass oracle to ContextExpander in the orchestrator

Run:
```bash
grep -n "ContextExpander(" backend/core/ouroboros/governance/orchestrator.py
```

At that line, change:
```python
ContextExpander(generator=..., repo_root=...)
```
to:
```python
ContextExpander(
    generator=...,
    repo_root=...,
    oracle=getattr(self._stack, "oracle", None),
)
```

### Step 5: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_context_expander.py -v 2>&1 | tail -15
```
Expected: all tests PASS including 3 new ones, no regressions.

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/context_expander.py \
        backend/core/ouroboros/governance/orchestrator.py \
        tests/test_ouroboros_governance/test_context_expander.py
git commit -m "feat(oracle): inject oracle manifest into ContextExpander planning prompt"
```

---

## Task 4: GovernedOrchestrator — COMPLETE-phase incremental update

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py`
- Test: `tests/test_ouroboros_governance/test_orchestrator.py`

### Context

Two VERIFY→COMPLETE paths in the orchestrator must call `oracle.incremental_update()` after successful patch application, fault-isolated. This mirrors exactly how `_run_benchmark` and `_persist_performance_record` are wired — same position, same exception handling pattern.

**Single-repo path** — currently (after Phase 2):
```python
# line 615
ctx = await self._run_benchmark(ctx, [])
# line 616
ctx = ctx.advance(OperationPhase.COMPLETE)
# line 617
self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_apply)
# line 618
await self._publish_outcome(ctx, OperationState.APPLIED)
# line 619
await self._persist_performance_record(ctx)
# line 620
return ctx
```

**Saga path** — currently (after Phase 2):
```python
# line 1047
ctx = await self._run_benchmark(ctx, [])
# line 1048
ctx = ctx.advance(OperationPhase.COMPLETE)
# ...
# line 1051
await self._persist_performance_record(ctx)
```

The incremental update must go AFTER `_run_benchmark` and BEFORE or AFTER `ctx.advance(COMPLETE)` — either position is fine since it's fault-isolated. Put it AFTER `_persist_performance_record` to keep benchmark/persist as the last telemetry actions, oracle update as a separate post-telemetry step.

### Step 1: Write the failing tests

Add `TestOracleIncrementalUpdate` to `tests/test_ouroboros_governance/test_orchestrator.py`:

```python
class TestOracleIncrementalUpdate:
    """Oracle.incremental_update called after COMPLETE; exceptions never alter terminal state."""

    async def test_incremental_update_called_after_single_repo_complete(self):
        """After single-repo VERIFY→COMPLETE, oracle.incremental_update is called."""
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from backend.core.ouroboros.governance.op_context import OperationPhase
        from unittest.mock import AsyncMock, MagicMock, patch
        from pathlib import Path

        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = False
        config.project_root = Path("/tmp")
        config.context_expansion_enabled = False

        mock_oracle = MagicMock()
        mock_oracle.incremental_update = AsyncMock()

        stack = MagicMock()
        stack.oracle = mock_oracle
        stack.can_write.return_value = (True, "ok")
        stack.risk_engine.classify.return_value = MagicMock(
            tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
        )
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine.execute = AsyncMock(
            return_value=MagicMock(success=True, rolled_back=False, op_id="op-t")
        )
        stack.learning_bridge = None
        stack.canary.record_operation = MagicMock()
        stack.performance_persistence = None

        mock_gen = MagicMock()
        from backend.core.ouroboros.governance.orchestrator import GenerationResult
        mock_gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=({"candidate_id": "c1", "file_path": "foo.py",
                         "full_content": "x=1\n", "rationale": "r",
                         "candidate_hash": "h", "source_hash": "", "source_path": "foo.py"},),
            provider_name="mock", generation_duration_s=0.1,
        ))

        orch = Orchestrator(stack=stack, generator=mock_gen, approval_provider=None, config=config)

        from backend.core.ouroboros.governance.op_context import OperationContext
        ctx = OperationContext.create(target_files=("foo.py",), description="test oracle update")
        result = await orch.run(ctx)

        mock_oracle.incremental_update.assert_called_once()

    async def test_oracle_update_exception_does_not_change_terminal_state(self):
        """Even if incremental_update raises, ctx.phase == COMPLETE."""
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
        from unittest.mock import AsyncMock, MagicMock
        from pathlib import Path

        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = False
        config.project_root = Path("/tmp")
        config.context_expansion_enabled = False

        mock_oracle = MagicMock()
        mock_oracle.incremental_update = AsyncMock(side_effect=RuntimeError("oracle exploded"))

        stack = MagicMock()
        stack.oracle = mock_oracle
        stack.can_write.return_value = (True, "ok")
        stack.risk_engine.classify.return_value = MagicMock(
            tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
        )
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine.execute = AsyncMock(
            return_value=MagicMock(success=True, rolled_back=False, op_id="op-u")
        )
        stack.learning_bridge = None
        stack.canary.record_operation = MagicMock()
        stack.performance_persistence = None

        mock_gen = MagicMock()
        from backend.core.ouroboros.governance.orchestrator import GenerationResult
        mock_gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=({"candidate_id": "c1", "file_path": "foo.py",
                         "full_content": "x=1\n", "rationale": "r",
                         "candidate_hash": "h", "source_hash": "", "source_path": "foo.py"},),
            provider_name="mock", generation_duration_s=0.1,
        ))

        orch = Orchestrator(stack=stack, generator=mock_gen, approval_provider=None, config=config)

        ctx = OperationContext.create(target_files=("foo.py",), description="test oracle boom")
        result = await orch.run(ctx)
        assert result.phase == OperationPhase.COMPLETE
```

Run:
```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py::TestOracleIncrementalUpdate -v 2>&1 | head -20
```
Expected: AssertionError on `incremental_update.assert_called_once()` — not yet wired.

### Step 2: Add `_oracle_incremental_update` helper to GovernedOrchestrator

Add this private method near `_persist_performance_record`:

```python
    async def _oracle_incremental_update(
        self,
        applied_files: list,
    ) -> None:
        """Notify Oracle of changed files. Fault-isolated — never raises."""
        oracle = getattr(self._stack, "oracle", None)
        if oracle is None:
            return
        try:
            await oracle.incremental_update(applied_files)
        except asyncio.CancelledError:
            pass  # swallow — benchmark is non-blocking; don't abort COMPLETE
        except Exception as exc:
            logger.warning(
                "[Orchestrator] Oracle incremental_update failed for op=%s: %s",
                "unknown", exc,
            )
```

### Step 3: Wire single-repo COMPLETE path

In `orchestrator.py`, find the single-repo VERIFY→COMPLETE block (~lines 615–620). After `await self._persist_performance_record(ctx)` (line 619), add:

```python
        applied_files = [Path(p).resolve() for p in ctx.target_files]
        await self._oracle_incremental_update(applied_files)
```

### Step 4: Wire saga COMPLETE path

In `orchestrator.py`, find the saga VERIFY→COMPLETE block (~lines 1047–1051). After `await self._persist_performance_record(ctx)`, add:

```python
        try:
            saga_applied = [
                (self._config.repo_registry.get(repo).local_path / rel_path).resolve()
                for repo, patch in patch_map.items()
                for rel_path, _ in getattr(patch, "new_content", [])
            ]
        except Exception:
            saga_applied = []
        await self._oracle_incremental_update(saga_applied)
```

**Note on `patch.new_content`:** `RepoPatch` is defined in `backend/core/ouroboros/governance/multi_repo/`. Before implementing Step 4, read that file and verify the correct attribute name for the list of (relative_path, content) pairs. The plan uses `new_content` as specified in the design constraints, but confirm the actual field name in the dataclass.

### Step 5: Run tests

```bash
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py::TestOracleIncrementalUpdate -v
python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py -v 2>&1 | tail -15
```
Expected: 2 new tests PASS, pre-existing failures (10 in TestApprovalFlow / TestValidationRetries / TestLedgerRecording) are unchanged.

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        tests/test_ouroboros_governance/test_orchestrator.py
git commit -m "feat(oracle): wire COMPLETE-phase incremental_update in single-repo and saga paths"
```

---

## Final Verification

Run the full Phase 2 + Oracle test suite to confirm no regressions:

```bash
python3 -m pytest \
  tests/test_ouroboros_governance/test_performance_storage_v2.py \
  tests/test_ouroboros_governance/test_patch_benchmarker.py \
  tests/test_ouroboros_governance/test_model_attribution_recorder.py \
  tests/test_ouroboros_governance/test_curriculum_publisher.py \
  tests/test_ouroboros_governance/test_op_context.py \
  tests/test_ouroboros_governance/test_orchestrator.py::TestBenchmarkWiring \
  tests/test_ouroboros_governance/test_orchestrator.py::TestOracleIncrementalUpdate \
  tests/test_ouroboros_governance/test_governed_loop_service.py::TestBackgroundTaskLifecycle \
  tests/test_ouroboros_governance/test_governed_loop_service.py::TestOracleConfig \
  tests/test_ouroboros_governance/test_governed_loop_service.py::TestOracleIndexerLifecycle \
  tests/test_ouroboros_governance/test_context_expander.py \
  -v 2>&1 | tail -20
```

Expected: all new tests PASS, 104 existing Phase 2 tests still PASS, pre-existing failures unchanged.

---

## Pre-existing failures (do not fix)

These tests fail before this work and are not your responsibility:
- `tests/test_ouroboros_governance/test_integration.py::TestGovernedPipelineEndToEnd::test_full_pipeline_sandbox_mode`
- `tests/test_ouroboros_governance/test_providers.py` (29 failures)
- `tests/test_ouroboros_governance/test_orchestrator.py` — 10 failures in TestApprovalFlow / TestValidationRetries / TestLedgerRecording

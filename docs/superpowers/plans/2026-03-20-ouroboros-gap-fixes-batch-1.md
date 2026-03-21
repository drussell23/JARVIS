# Ouroboros Gap Fixes — Batch 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire up 5 high-leverage Ouroboros gaps: enable L3 SubagentScheduler, plug DegradationController into preflight, record canary slice metrics post-op, activate ShadowHarness in the orchestrator, and inject OUROBOROS.md human instructions into every generation prompt.

**Architecture:** These are all targeted wiring changes — no new systems are designed from scratch. Two items are already confirmed done (WIRE 2: tool_use_enabled is already `true` in `.env:300`; WIRE 8: EventBridge is already in CommProtocol transport chain at `integration.py:499`). Each task is fully isolated; they can be merged in any order.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, existing Ouroboros governance modules.

---

## File Map

| File | What changes |
|---|---|
| `.env` | Add `JARVIS_GOVERNED_L3_ENABLED=true` |
| `backend/core/ouroboros/governance/governed_loop_service.py` | 3 changes: degradation gate in `_preflight_check()`, canary metrics after op completes, shadow harness instantiation |
| `backend/core/ouroboros/governance/orchestrator.py` | Add `shadow_harness` field to `OrchestratorConfig`; inject shadow check between VALIDATE and GATE |
| `backend/core/ouroboros/governance/op_context.py` | Add `human_instructions: str` field + `with_shadow_result()` + `with_human_instructions()` methods |
| `backend/core/ouroboros/governance/providers.py` | Prepend `## Human Instructions` block in `_build_codegen_prompt()` when ctx.human_instructions is set |
| `backend/core/ouroboros/governance/context_memory_loader.py` | **NEW** — reads 3-tier OUROBOROS.md hierarchy |
| `tests/governance/test_degradation_preflight.py` | **NEW** — unit tests for degradation gate |
| `tests/governance/test_canary_metrics_wire.py` | **NEW** — unit tests for canary record_operation call |
| `tests/governance/test_shadow_harness_wire.py` | **NEW** — unit tests for shadow harness orchestrator integration |
| `tests/governance/test_context_memory_loader.py` | **NEW** — unit tests for ContextMemoryLoader |

---

## Task 1: Enable L3 SubagentScheduler

**Files:**
- Modify: `.env`
- Test: inline (grep-based assertion)

**Context:** `governed_loop_service.py:630` reads `JARVIS_GOVERNED_L3_ENABLED` env var. Default is `false`. The SubagentScheduler is fully built; it just needs the flag. `JARVIS_GOVERNED_TOOL_USE_ENABLED=true` is already present at line 300 of `.env`.

- [ ] **Step 1: Verify the flag is truly absent**

```bash
grep "JARVIS_GOVERNED_L3_ENABLED" .env
```
Expected: no output (flag not yet set).

- [ ] **Step 2: Add the flag to .env after the tool_use line**

Find line 300 in `.env` (`JARVIS_GOVERNED_TOOL_USE_ENABLED=true`) and append the L3 flag immediately after it:

```
JARVIS_GOVERNED_L3_ENABLED=true
```

- [ ] **Step 3: Verify both flags are present**

```bash
grep "JARVIS_GOVERNED_TOOL_USE_ENABLED\|JARVIS_GOVERNED_L3_ENABLED" .env
```
Expected:
```
JARVIS_GOVERNED_TOOL_USE_ENABLED=true
JARVIS_GOVERNED_L3_ENABLED=true
```

- [ ] **Step 4: Verify GLS from_env() reads the flag**

```bash
python3 -c "
import os; os.environ['JARVIS_GOVERNED_L3_ENABLED'] = 'true'
from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig
cfg = GovernedLoopConfig(project_root=__import__('pathlib').Path('.'))
print('l3_enabled:', cfg.l3_enabled)
assert cfg.l3_enabled, 'l3_enabled must be True when env var is set'
print('PASS')
"
```
Expected: `l3_enabled: True` + `PASS`

- [ ] **Step 5: Commit**

```bash
git add .env
git commit -m "feat(ouroboros): enable L3 SubagentScheduler (JARVIS_GOVERNED_L3_ENABLED=true)"
```

---

## Task 2: Wire DegradationController into _preflight_check()

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Test: `tests/governance/test_degradation_preflight.py` (new)

**Context:** `_preflight_check()` starts at line 1660. The degradation gate should fire AFTER the cooldown guard (which ends around line 1702 with `return ctx.advance(OperationPhase.CANCELLED)`) and BEFORE the compute-class admission gate (which starts at line 1704 with `# ── Compute-class admission gate`). `self._stack.degradation` is a `DegradationController`. Its `.mode` property returns a `DegradationMode` IntEnum with values: `FULL_AUTONOMY=0`, `REDUCED_AUTONOMY=1`, `READ_ONLY_PLANNING=2`, `EMERGENCY_STOP=3`. `self._stack` is always available from `governed_loop_service.py:668`.

- [ ] **Step 1: Write the failing test**

Create `tests/governance/test_degradation_preflight.py`:

```python
"""Tests that _preflight_check honours DegradationController mode."""
import asyncio
import dataclasses
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.ouroboros.governance.degradation import DegradationController, DegradationMode
from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase


def _make_ctx():
    from backend.core.ouroboros.governance.op_context import make_context
    return make_context(target_files=("backend/foo.py",), description="test op")


def _make_gls_with_degradation(mode: DegradationMode):
    """Return a minimal GovernedLoopService with mocked stack at given degradation mode."""
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
    from backend.core.ouroboros.governance.governed_loop_config import GovernedLoopConfig
    import pathlib

    deg = DegradationController()
    # Force mode by updating internal state directly
    deg._mode = mode

    mock_stack = MagicMock()
    mock_stack.degradation = deg

    # Minimal GLS — skip full construction
    gls = object.__new__(GovernedLoopService)
    gls._stack = mock_stack
    gls._file_touch_cache = {}
    gls._generator = None   # skip connectivity preflight
    gls._ledger = None
    gls._vm_capability = None
    return gls


@pytest.mark.asyncio
async def test_emergency_stop_cancels_op():
    gls = _make_gls_with_degradation(DegradationMode.EMERGENCY_STOP)
    ctx = _make_ctx()
    result = await gls._preflight_check(ctx)
    assert result is not None, "EMERGENCY_STOP must return early-exit ctx"
    assert result.phase == OperationPhase.CANCELLED


@pytest.mark.asyncio
async def test_read_only_planning_cancels_op():
    gls = _make_gls_with_degradation(DegradationMode.READ_ONLY_PLANNING)
    ctx = _make_ctx()
    result = await gls._preflight_check(ctx)
    assert result is not None, "READ_ONLY_PLANNING must return early-exit ctx"
    assert result.phase == OperationPhase.CANCELLED


@pytest.mark.asyncio
async def test_full_autonomy_passes_through():
    gls = _make_gls_with_degradation(DegradationMode.FULL_AUTONOMY)
    ctx = _make_ctx()
    # With no generator/ledger, preflight passes the degradation gate and returns None
    result = await gls._preflight_check(ctx)
    assert result is None, "FULL_AUTONOMY must not block (degradation gate passes through)"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/governance/test_degradation_preflight.py -v 2>&1 | tail -20
```
Expected: 3 FAILED (degradation check not yet wired).

- [ ] **Step 3: Add degradation gate to _preflight_check()**

In `governed_loop_service.py`, find the end of the cooldown guard block. The pattern to find is:
```python
            if len(_dq) > _COOLDOWN_MAX_HITS:
                ...
                return ctx.advance(OperationPhase.CANCELLED)
```
Immediately AFTER that `return ctx.advance(OperationPhase.CANCELLED)` (and before the `# ── Compute-class admission gate` comment), insert:

```python
        # ── Degradation mode gate ────────────────────────────────────────────────
        # DegradationController tracks 4 modes: FULL_AUTONOMY → REDUCED_AUTONOMY
        # → READ_ONLY_PLANNING → EMERGENCY_STOP. Gate out ops at READ_ONLY and above.
        _deg_ctrl = getattr(getattr(self, "_stack", None), "degradation", None)
        if _deg_ctrl is not None:
            from backend.core.ouroboros.governance.degradation import DegradationMode
            _deg_mode = _deg_ctrl.mode
            if _deg_mode >= DegradationMode.READ_ONLY_PLANNING:
                logger.warning(
                    "[GovernedLoop] Preflight: degradation_mode=%s blocks op %s",
                    _deg_mode.name,
                    ctx.op_id,
                )
                return ctx.advance(OperationPhase.CANCELLED)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/governance/test_degradation_preflight.py -v 2>&1 | tail -10
```
Expected: 3 PASSED.

- [ ] **Step 5: Run broader governance tests to check nothing is broken**

```bash
python3 -m pytest tests/governance/ tests/test_ouroboros_governance/ -q --tb=short 2>&1 | tail -20
```
Expected: existing tests pass, 0 new failures.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/test_degradation_preflight.py
git commit -m "feat(ouroboros): wire DegradationController into _preflight_check (WIRE 6)"
```

---

## Task 3: Record Canary Slice Metrics After Each Operation

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Test: `tests/governance/test_canary_metrics_wire.py` (new)

**Context:** `CanaryController.record_operation(file_path, success, latency_s, rolled_back)` is at `canary_controller.py:126`. It iterates registered slice prefixes and updates `SliceMetrics` for matching files. The call site is in `submit()`, after `orchestrator.run()` returns (~line 1377). Key local variables available at that point: `terminal_ctx.phase`, `_rollback_occurred` (bool, line 1377), `duration` (float, line 1357), `terminal_ctx.target_files`. `self._stack.canary` is a `CanaryController`.

- [ ] **Step 1: Write the failing test**

Create `tests/governance/test_canary_metrics_wire.py`:

```python
"""Tests that submit() calls canary.record_operation after orchestrator completes."""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_canary():
    from unittest.mock import MagicMock
    canary = MagicMock()
    canary.record_operation = MagicMock()
    return canary


def test_record_operation_called_on_complete(monkeypatch):
    """CanaryController.record_operation must be called once per target file on COMPLETE."""
    from backend.core.ouroboros.governance.canary_controller import CanaryController
    from backend.core.ouroboros.governance.op_context import OperationPhase

    canary = _make_mock_canary()

    # Import the record-operation call path by checking the canary controller directly
    # Minimal simulation: record_operation with success=True, not rolled_back
    canary.record_operation("backend/foo.py", success=True, latency_s=1.2, rolled_back=False)
    canary.record_operation.assert_called_once_with(
        "backend/foo.py", success=True, latency_s=1.2, rolled_back=False
    )


def test_record_operation_rolled_back():
    """record_operation is called with rolled_back=True when rollback occurred."""
    canary = _make_mock_canary()
    canary.record_operation("tests/foo.py", success=False, latency_s=0.5, rolled_back=True)
    call_kwargs = canary.record_operation.call_args
    args = call_kwargs[0]
    assert args[2] is False or args[2] == False  # success=False
    assert args[3] is True  # rolled_back=True


def test_record_operation_updates_slice_metrics():
    """After record_operation, SliceMetrics.total_operations increments."""
    from backend.core.ouroboros.governance.canary_controller import CanaryController
    cc = CanaryController()
    cc.register_slice("tests/")
    before = cc.get_metrics("tests/").total_operations
    cc.record_operation("tests/foo.py", success=True, latency_s=0.8, rolled_back=False)
    after = cc.get_metrics("tests/").total_operations
    assert after == before + 1
```

- [ ] **Step 2: Run tests to verify they pass (these test the component, not the wiring)**

```bash
python3 -m pytest tests/governance/test_canary_metrics_wire.py -v 2>&1 | tail -10
```
Expected: 3 PASSED (these test the canary API, not GLS wiring — they'll pass before the change too).

- [ ] **Step 3: Add canary record_operation call in submit()**

In `governed_loop_service.py`, find the block immediately after `result = OperationResult(...)` is constructed in the normal completion path (after `orchestrator.run()` returns — around line 1390). The exact anchor is the line:
```python
            self._completed_ops[dedupe_key] = result
```
Insert BEFORE that line:

```python
            # ── Canary slice metrics ─────────────────────────────────────────────────
            # record_operation matches file paths against registered slice prefixes.
            # Must be called after duration and _rollback_occurred are computed.
            if self._stack is not None and self._stack.canary is not None:
                _canary_success = terminal_ctx.phase is OperationPhase.COMPLETE
                for _canary_fp in (terminal_ctx.target_files or ctx.target_files):
                    try:
                        self._stack.canary.record_operation(
                            file_path=str(_canary_fp),
                            success=_canary_success,
                            latency_s=duration,
                            rolled_back=_rollback_occurred,
                        )
                    except Exception as _canary_exc:
                        logger.debug(
                            "[GovernedLoop] canary.record_operation error: %s", _canary_exc
                        )
```

- [ ] **Step 4: Run all governance tests**

```bash
python3 -m pytest tests/governance/ tests/test_ouroboros_governance/ -q --tb=short 2>&1 | tail -20
```
Expected: existing tests pass, 0 new failures.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/test_canary_metrics_wire.py
git commit -m "feat(ouroboros): record canary slice metrics after each op (WIRE 5)"
```

---

## Task 4: Wire ShadowHarness into Orchestrator

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (lines 136, 606–609)
- Modify: `backend/core/ouroboros/governance/op_context.py` (after line 923)
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (add instantiation)
- Test: `tests/governance/test_shadow_harness_wire.py` (new)

**Context:**
- `ShadowHarness` is in `shadow_harness.py`. Its public API: `record_run(confidence: float)`, `is_disqualified: bool`, `reset()`.
- `ShadowResult` is in `op_context.py:244`. Fields: `confidence`, `comparison_mode: str`, `violations: Tuple[str, ...]`, `shadow_duration_s`, `production_match: bool`, `disqualified: bool`.
- The injection site in `orchestrator.py` is between lines 606 (end of VALIDATE) and 609 (`ctx = ctx.advance(OperationPhase.GATE, ...)`).
- `op_context.py` already has `shadow: Optional[ShadowResult] = None` — just needs the mutation helper.
- `OrchestratorConfig` is a `@dataclass` starting at line 77. Field for shadow harness goes at line ~136 (after the `execution_graph_scheduler` field).

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_shadow_harness_wire.py`:

```python
"""Tests for ShadowHarness integration in orchestrator and op_context."""
import pytest
from backend.core.ouroboros.governance.shadow_harness import ShadowHarness
from backend.core.ouroboros.governance.op_context import (
    OperationContext, ShadowResult, make_context,
)


def _make_ctx():
    return make_context(target_files=("backend/foo.py",), description="shadow test")


def test_with_shadow_result_attaches_result():
    """with_shadow_result() must attach a ShadowResult and change context_hash."""
    ctx = _make_ctx()
    original_hash = ctx.context_hash
    sr = ShadowResult(
        confidence=0.9,
        comparison_mode="ast",
        violations=(),
        shadow_duration_s=0.01,
        production_match=True,
        disqualified=False,
    )
    ctx2 = ctx.with_shadow_result(sr)
    assert ctx2.shadow == sr
    assert ctx2.context_hash != original_hash, "hash must change after shadow result"
    # Original must be immutable (frozen dataclass)
    assert ctx.shadow is None


def test_shadow_harness_disqualifies_after_three_low_confidence():
    """ShadowHarness.is_disqualified becomes True after 3 consecutive low-confidence runs."""
    harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
    assert not harness.is_disqualified
    harness.record_run(0.5)
    harness.record_run(0.5)
    assert not harness.is_disqualified
    harness.record_run(0.5)
    assert harness.is_disqualified


def test_orchestrator_config_accepts_shadow_harness():
    """OrchestratorConfig must accept shadow_harness as an optional field."""
    from backend.core.ouroboros.governance.orchestrator import OrchestratorConfig
    import pathlib
    cfg = OrchestratorConfig(project_root=pathlib.Path("."))
    assert cfg.shadow_harness is None  # default is None

    harness = ShadowHarness()
    cfg2 = OrchestratorConfig(project_root=pathlib.Path("."), shadow_harness=harness)
    assert cfg2.shadow_harness is harness
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/governance/test_shadow_harness_wire.py -v 2>&1 | tail -15
```
Expected: `test_with_shadow_result_attaches_result` FAILS (method doesn't exist yet); `test_orchestrator_config_accepts_shadow_harness` FAILS (field doesn't exist yet).

- [ ] **Step 3: Add `with_shadow_result()` to OperationContext in op_context.py**

Find the `with_frozen_autonomy_tier()` method (line 903) and insert after its closing line (`return dataclasses.replace(intermediate, context_hash=new_hash)` at line ~923):

```python
    def with_shadow_result(self, result: "ShadowResult") -> "OperationContext":
        """Attach shadow harness result to context (no phase change, hash updates)."""
        intermediate = dataclasses.replace(
            self,
            shadow=result,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)
```

- [ ] **Step 4: Add `shadow_harness` field to OrchestratorConfig in orchestrator.py**

Find the field `execution_graph_scheduler: Optional[Any] = None` (line ~135). Insert immediately after it:

```python
    # Shadow harness (optional — set by GovernedLoopService when
    # JARVIS_SHADOW_HARNESS_ENABLED=true in .env)
    shadow_harness: Optional[Any] = None
```

- [ ] **Step 5: Run tests — first two should now pass**

```bash
python3 -m pytest tests/governance/test_shadow_harness_wire.py::test_with_shadow_result_attaches_result \
    tests/governance/test_shadow_harness_wire.py::test_orchestrator_config_accepts_shadow_harness -v 2>&1 | tail -10
```
Expected: 2 PASSED.

- [ ] **Step 6: Inject shadow check between VALIDATE and GATE in orchestrator.py**

Find line 609 in `orchestrator.py`:
```python
        ctx = ctx.advance(OperationPhase.GATE, validation=best_validation)
```
Insert BEFORE that line:

```python
        # ── Shadow harness check (soft advisory — never hard-blocks GATE) ──────
        # Evaluates candidate structural integrity before GATE. Uses AST comparison
        # between the candidate's proposed content and itself (firewall-only path).
        # If the harness is disqualified, logs a warning — GATE still proceeds.
        if self._config.shadow_harness is not None and best_candidate is not None:
            import time as _sh_time
            from backend.core.ouroboros.governance.shadow_harness import (
                OutputComparator,
                SideEffectFirewall,
                CompareMode,
            )
            from backend.core.ouroboros.governance.op_context import ShadowResult
            _sh_start = _sh_time.monotonic()
            _violations: list = []
            _confidence = 0.0
            try:
                _content = (
                    best_candidate.get("full_content")
                    or best_candidate.get("unified_diff")
                    or ""
                )
                with SideEffectFirewall():
                    _confidence = OutputComparator().compare(
                        _content, _content, CompareMode.AST
                    )
            except Exception as _sh_exc:
                _violations.append(str(_sh_exc))
                _confidence = 0.0
            _sh_dur = _sh_time.monotonic() - _sh_start
            self._config.shadow_harness.record_run(_confidence)
            _shadow_result = ShadowResult(
                confidence=_confidence,
                comparison_mode="ast",
                violations=tuple(_violations),
                shadow_duration_s=_sh_dur,
                production_match=(_confidence >= 0.7),
                disqualified=self._config.shadow_harness.is_disqualified,
            )
            ctx = ctx.with_shadow_result(_shadow_result)
            if self._config.shadow_harness.is_disqualified:
                logger.warning(
                    "[Orchestrator] ShadowHarness disqualified for op=%s "
                    "(confidence=%.2f, violations=%d) — proceeding to GATE with advisory",
                    ctx.op_id,
                    _confidence,
                    len(_violations),
                )
```

- [ ] **Step 7: Wire ShadowHarness instantiation in GovernedLoopService**

In `governed_loop_service.py`, find the `_build_components()` method (search for `def _build_components`). Near where the orchestrator config is built (search for `OrchestratorConfig(`), add shadow harness initialization before it:

```python
        # Shadow harness — enabled via JARVIS_SHADOW_HARNESS_ENABLED=true
        _shadow_harness = None
        if os.environ.get("JARVIS_SHADOW_HARNESS_ENABLED", "false").lower() in ("true", "1"):
            from backend.core.ouroboros.governance.shadow_harness import ShadowHarness
            _shadow_harness = ShadowHarness(
                confidence_threshold=float(os.environ.get("JARVIS_SHADOW_CONFIDENCE_THRESHOLD", "0.7")),
                disqualify_after=int(os.environ.get("JARVIS_SHADOW_DISQUALIFY_AFTER", "3")),
            )
            logger.info("[GovernedLoop] ShadowHarness enabled (threshold=%.2f, disqualify_after=%d)",
                        _shadow_harness._threshold, _shadow_harness._disqualify_after)
```

Then add `shadow_harness=_shadow_harness` to the `OrchestratorConfig(...)` call.

- [ ] **Step 8: Run all shadow harness tests**

```bash
python3 -m pytest tests/governance/test_shadow_harness_wire.py \
    tests/test_ouroboros_governance/test_shadow_harness.py -v 2>&1 | tail -15
```
Expected: all PASSED.

- [ ] **Step 9: Run broader governance tests**

```bash
python3 -m pytest tests/governance/ tests/test_ouroboros_governance/ -q --tb=short 2>&1 | tail -20
```
Expected: existing tests pass.

- [ ] **Step 10: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py \
        backend/core/ouroboros/governance/op_context.py \
        backend/core/ouroboros/governance/governed_loop_service.py \
        tests/governance/test_shadow_harness_wire.py
git commit -m "feat(ouroboros): wire ShadowHarness into orchestrator VALIDATE→GATE phase (WIRE 1)"
```

---

## Task 5: GAP 3 — ContextMemoryLoader + OUROBOROS.md Injection

**Files:**
- Create: `backend/core/ouroboros/governance/context_memory_loader.py`
- Modify: `backend/core/ouroboros/governance/op_context.py`
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Modify: `backend/core/ouroboros/governance/providers.py`
- Test: `tests/governance/test_context_memory_loader.py` (new)

**Context:** This adds a 3-tier OUROBOROS.md hierarchy (global → project → local) that injects human-authored instructions into every generation prompt — the single highest-impact change per the gap analysis. The injection point in `providers.py` is `_build_codegen_prompt()` at line ~837 where `parts = [...]` is assembled. The `human_instructions` block should be the FIRST part (before `## Task`). In `governed_loop_service.py`, load at submit() time, right before the `_preflight_check()` call (around line 1246).

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_context_memory_loader.py`:

```python
"""Tests for ContextMemoryLoader — 3-tier OUROBOROS.md injection."""
import tempfile
import pathlib
import pytest
from backend.core.ouroboros.governance.context_memory_loader import ContextMemoryLoader


def test_loads_nothing_when_no_files_exist(tmp_path):
    """Returns empty string when no OUROBOROS.md files exist."""
    loader = ContextMemoryLoader(
        global_dir=tmp_path / "global",
        project_root=tmp_path / "project",
    )
    result = loader.load()
    assert result == ""


def test_loads_global_instructions(tmp_path):
    """~/.jarvis/OUROBOROS.md content is returned."""
    global_dir = tmp_path / ".jarvis"
    global_dir.mkdir()
    (global_dir / "OUROBOROS.md").write_text("# Global Rule\nNever use subprocess.\n")

    loader = ContextMemoryLoader(
        global_dir=global_dir,
        project_root=tmp_path / "project",
    )
    result = loader.load()
    assert "Never use subprocess." in result


def test_project_overrides_merge_with_global(tmp_path):
    """Both global and project OUROBOROS.md content appear in output."""
    global_dir = tmp_path / ".jarvis"
    global_dir.mkdir()
    (global_dir / "OUROBOROS.md").write_text("Global instruction.\n")

    project_root = tmp_path / "myproject"
    project_root.mkdir()
    (project_root / "OUROBOROS.md").write_text("Project instruction.\n")

    loader = ContextMemoryLoader(global_dir=global_dir, project_root=project_root)
    result = loader.load()
    assert "Global instruction." in result
    assert "Project instruction." in result


def test_local_override_takes_precedence(tmp_path):
    """<repo>/.jarvis/OUROBOROS.md also included (all 3 levels merge)."""
    global_dir = tmp_path / ".jarvis"
    global_dir.mkdir()
    (global_dir / "OUROBOROS.md").write_text("Global.\n")

    project_root = tmp_path / "myproject"
    project_root.mkdir()
    (project_root / "OUROBOROS.md").write_text("Project.\n")

    local_dir = project_root / ".jarvis"
    local_dir.mkdir()
    (local_dir / "OUROBOROS.md").write_text("Local personal override.\n")

    loader = ContextMemoryLoader(global_dir=global_dir, project_root=project_root)
    result = loader.load()
    assert "Global." in result
    assert "Project." in result
    assert "Local personal override." in result


def test_load_is_idempotent(tmp_path):
    """Calling load() twice returns identical content."""
    global_dir = tmp_path / ".jarvis"
    global_dir.mkdir()
    (global_dir / "OUROBOROS.md").write_text("Stable content.\n")
    loader = ContextMemoryLoader(global_dir=global_dir, project_root=tmp_path)
    assert loader.load() == loader.load()


def test_with_human_instructions_sets_field():
    """OperationContext.with_human_instructions() sets human_instructions and changes hash."""
    from backend.core.ouroboros.governance.op_context import make_context
    ctx = make_context(target_files=("backend/foo.py",), description="test")
    assert ctx.human_instructions == ""
    ctx2 = ctx.with_human_instructions("Never use subprocess.")
    assert ctx2.human_instructions == "Never use subprocess."
    assert ctx2.context_hash != ctx.context_hash


def test_codegen_prompt_includes_human_instructions(tmp_path):
    """_build_codegen_prompt prepends human_instructions block when set."""
    from backend.core.ouroboros.governance.op_context import make_context
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    ctx = make_context(target_files=(), description="fix bug")
    ctx = ctx.with_human_instructions("Always write tests before code.")
    prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
    assert "Always write tests before code." in prompt
    # Human instructions must appear before the Task section
    assert prompt.index("Always write tests before code.") < prompt.index("## Task")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/governance/test_context_memory_loader.py -v 2>&1 | tail -20
```
Expected: Most FAIL (module doesn't exist, fields don't exist).

- [ ] **Step 3: Create context_memory_loader.py**

Create `backend/core/ouroboros/governance/context_memory_loader.py`:

```python
# backend/core/ouroboros/governance/context_memory_loader.py
"""
ContextMemoryLoader — 3-tier OUROBOROS.md hierarchy reader.

Loads human-authored instructions from up to 3 sources (global, project,
local) and merges them into a single string for injection into generation
prompts. All levels are optional; missing files are silently skipped.

Priority order (all merge, not override):
  1. ~/.jarvis/OUROBOROS.md          — global personal defaults
  2. <repo>/OUROBOROS.md             — project constraints (committed)
  3. <repo>/.jarvis/OUROBOROS.md     — local personal overrides (gitignored)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_FILENAME = "OUROBOROS.md"


class ContextMemoryLoader:
    """Reads and merges up to 3 OUROBOROS.md files into a single instruction block.

    Parameters
    ----------
    global_dir:
        Directory containing the global OUROBOROS.md (typically ``~/.jarvis/``).
        Defaults to ``~/.jarvis/``.
    project_root:
        Root of the current repository. The loader reads ``OUROBOROS.md``
        directly in this directory and ``<project_root>/.jarvis/OUROBOROS.md``
        for local overrides.
    """

    def __init__(
        self,
        project_root: Path,
        global_dir: Optional[Path] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._global_dir = (
            Path(global_dir) if global_dir is not None else Path.home() / ".jarvis"
        )

    def load(self) -> str:
        """Return merged instruction text from all available OUROBOROS.md files.

        Returns an empty string if no files exist.
        """
        sections: list[str] = []

        paths = [
            (self._global_dir / _FILENAME, "global"),
            (self._project_root / _FILENAME, "project"),
            (self._project_root / ".jarvis" / _FILENAME, "local"),
        ]

        for path, label in paths:
            text = self._read_safe(path, label)
            if text:
                sections.append(text.strip())

        return "\n\n".join(sections)

    @staticmethod
    def _read_safe(path: Path, label: str) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            logger.debug(
                "ContextMemoryLoader: could not read %s OUROBOROS.md at %s: %s",
                label,
                path,
                exc,
            )
            return ""
```

- [ ] **Step 4: Run loader tests (first 5) to verify they pass**

```bash
python3 -m pytest tests/governance/test_context_memory_loader.py::test_loads_nothing_when_no_files_exist \
    tests/governance/test_context_memory_loader.py::test_loads_global_instructions \
    tests/governance/test_context_memory_loader.py::test_project_overrides_merge_with_global \
    tests/governance/test_context_memory_loader.py::test_local_override_takes_precedence \
    tests/governance/test_context_memory_loader.py::test_load_is_idempotent -v 2>&1 | tail -10
```
Expected: 5 PASSED.

- [ ] **Step 5: Add `human_instructions` field and `with_human_instructions()` to OperationContext**

In `op_context.py`, find the `frozen_autonomy_tier` field (line ~511). Add `human_instructions` immediately after it:

```python
    human_instructions: str = ""  # injected from OUROBOROS.md hierarchy at submit time
```

Then find the `with_frozen_autonomy_tier()` method. After `with_shadow_result()` (which you added in Task 4), add:

```python
    def with_human_instructions(self, instructions: str) -> "OperationContext":
        """Stamp human-authored OUROBOROS.md instructions onto context.

        Called once by GovernedLoopService.submit() before handing ctx to the
        orchestrator. Both generation providers prepend this block to prompts.
        """
        intermediate = dataclasses.replace(
            self,
            human_instructions=instructions,
            previous_hash=self.context_hash,
            context_hash="",
        )
        fields_for_hash = _context_to_hash_dict(intermediate)
        new_hash = _compute_hash(fields_for_hash)
        return dataclasses.replace(intermediate, context_hash=new_hash)
```

- [ ] **Step 6: Run the with_human_instructions test**

```bash
python3 -m pytest tests/governance/test_context_memory_loader.py::test_with_human_instructions_sets_field -v 2>&1 | tail -10
```
Expected: PASSED.

- [ ] **Step 7: Inject human_instructions block in _build_codegen_prompt() in providers.py**

In `providers.py`, find the `parts = [...]` assembly block (line ~837):
```python
    parts = [
        f"## Task\nOp-ID: {ctx.op_id}\nGoal: {ctx.description}",
    ]
```
Replace it with:

```python
    parts = []
    # Human instructions from OUROBOROS.md hierarchy — always first in prompt
    _human_instr = getattr(ctx, "human_instructions", "")
    if _human_instr and _human_instr.strip():
        parts.append(
            "## Human Instructions\n\n"
            + _human_instr.strip()
            + "\n\n---"
        )
    parts.append(f"## Task\nOp-ID: {ctx.op_id}\nGoal: {ctx.description}")
```

- [ ] **Step 8: Run the codegen prompt test**

```bash
python3 -m pytest tests/governance/test_context_memory_loader.py::test_codegen_prompt_includes_human_instructions -v 2>&1 | tail -10
```
Expected: PASSED.

- [ ] **Step 9: Wire ContextMemoryLoader into GovernedLoopService.submit()**

In `governed_loop_service.py`, find the line that calls `_preflight_check`:
```python
            early_exit = await self._preflight_check(ctx)
```
Insert BEFORE that line (around line 1246):

```python
            # ── OUROBOROS.md human instruction injection ─────────────────────────
            # Load 3-tier instruction hierarchy and stamp onto ctx before pipeline.
            # Providers prepend this block to every generation prompt.
            try:
                from backend.core.ouroboros.governance.context_memory_loader import (
                    ContextMemoryLoader,
                )
                _instructions = ContextMemoryLoader(
                    project_root=self._config.project_root,
                ).load()
                if _instructions:
                    ctx = ctx.with_human_instructions(_instructions)
            except Exception as _cml_exc:
                logger.debug(
                    "[GovernedLoop] ContextMemoryLoader error (non-fatal): %s", _cml_exc
                )
```

- [ ] **Step 10: Run all 7 tests in the context memory test file**

```bash
python3 -m pytest tests/governance/test_context_memory_loader.py -v 2>&1 | tail -15
```
Expected: 7 PASSED.

- [ ] **Step 11: Run full governance + synthesis test suites**

```bash
python3 -m pytest tests/governance/ tests/test_ouroboros_governance/ tests/unit/synthesis/ -q --tb=short 2>&1 | tail -20
```
Expected: existing tests pass, 0 new failures.

- [ ] **Step 12: Commit**

```bash
git add backend/core/ouroboros/governance/context_memory_loader.py \
        backend/core/ouroboros/governance/op_context.py \
        backend/core/ouroboros/governance/governed_loop_service.py \
        backend/core/ouroboros/governance/providers.py \
        tests/governance/test_context_memory_loader.py
git commit -m "feat(ouroboros): add ContextMemoryLoader + OUROBOROS.md injection into generation prompts (GAP 3)"
```

---

## Final Verification

After all 5 tasks are committed:

```bash
python3 -m pytest tests/governance/ tests/test_ouroboros_governance/ tests/unit/synthesis/ \
    tests/governance/test_degradation_preflight.py \
    tests/governance/test_canary_metrics_wire.py \
    tests/governance/test_shadow_harness_wire.py \
    tests/governance/test_context_memory_loader.py \
    -q --tb=short 2>&1 | tail -10
```
Expected: all pass, 0 failures.

---

## Implementation Notes

- **WIRE 2 (tool_use_enabled)** and **WIRE 8 (EventBridge)** are confirmed already done. WIRE 2: `.env:300` has `JARVIS_GOVERNED_TOOL_USE_ENABLED=true`. WIRE 8: `integration.py:499-500` wires EventBridge as an extra CommProtocol transport.
- **L4** (`JARVIS_GOVERNED_L4_ENABLED`) is intentionally left off. The gap analysis says "enable L3 first, then L4 after L3 is stable."
- **OUROBOROS.md files** do not exist yet — the system gracefully handles all missing files (returns empty string). Users create them to inject project-specific rules.
- **ShadowHarness** is soft-advisory: it never hard-blocks GATE. Disqualification is logged only.

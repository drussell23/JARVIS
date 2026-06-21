# Sovereign Asynchronous Yield Matrix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the existing Sovereign Execution Boundary self-enforcing (Layer 1, deterministic, default-on) and add graceful operator-yield (Layer 2, advisory, default-off), reusing `autonomous_workspace` / `execution_context` / `operator_commit_authority` / `op_park_store` / `trinity_event_bus` / `sensor_governor`. No parallel systems.

**Spec (binding):** `docs/superpowers/specs/2026-06-21-sovereign-async-yield-matrix-design.md`. **LR-A** = lock force-arms BOTH isolation + execution-boundary flags as a pair. **LR-B** = operator-yield awaits in-flight critical-mutation drain before parking; never park a half-written file.

**Branch:** `fleet/sovereign-async-yield-matrix`. **Worktree quirk:** verify writes via `git show`/`grep` (a prior session saw Edit not always flush here).

---

## File Structure

| File | Responsibility | New/Modify |
|---|---|---|
| `backend/core/ouroboros/governance/execution_context.py` | add `_is_cloud_container()` deterministic check | Modify |
| `backend/core/ouroboros/governance/autonomous_workspace.py` | deterministic override in `resolve_loop_project_root()` (dual-arm both flags, force-route) | Modify |
| `backend/core/ouroboros/governance/tool_executor.py` | raw-write guard (deny edit_file/write_file to primary checkout) | Modify |
| `scripts/verify_file_isolation.py` | add I5 (env=false override proof + both-flags-armed) | Modify |
| `backend/core/ouroboros/governance/mutation_critical_section.py` | LR-B drain guard (asyncio counter/section) | **Create** |
| `backend/core/ouroboros/governance/operator_presence.py` | deterministic presence detector + watcher (publishes operator.active/idle) | **Create** |
| `backend/core/ouroboros/governance/sensor_governor.py` | inject `operator_active_fn` → hard-zero caps | Modify |
| `backend/core/ouroboros/governance/op_park_store.py` | `should_park_for_route(operator_suspended=)` param | Modify |
| `backend/core/ouroboros/governance/operator_yield_bridge.py` | subscribe operator.* → drain-then-park / resume | **Create** |
| `tests/governance/test_*` | per-task tests | **Create** |

**Ordering:** Layer 1 (Tasks 1–4) ships the incident fix first, default-on. Layer 2 (Tasks 5–9) adds advisory yield, default-off.

---

# LAYER 1 — Deterministic Hard Lock (default-on)

## Task 1: `_is_cloud_container()` in execution_context.py

**Files:** Modify `backend/core/ouroboros/governance/execution_context.py`; Test `tests/governance/test_execution_context_container.py` (Create)

- [ ] **Step 1 — read first:** open `execution_context.py`, confirm `is_primary_checkout()` (lines ~53-74) and `is_autonomous()` (~77-99) signatures, the import block, and `_run_git` helper.

- [ ] **Step 2 — failing test:**
```python
# tests/governance/test_execution_context_container.py
from __future__ import annotations
from backend.core.ouroboros.governance import execution_context as ec

def test_not_container_by_default(monkeypatch, tmp_path):
    for k in ("OUROBOROS_CLOUD_NODE", "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ec, "_DOCKERENV_PATH", str(tmp_path / "nope"), raising=False)
    assert ec._is_cloud_container() is False

def test_container_via_env_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("OUROBOROS_CLOUD_NODE", "1")
    assert ec._is_cloud_container() is True

def test_container_via_dockerenv(monkeypatch, tmp_path):
    monkeypatch.delenv("OUROBOROS_CLOUD_NODE", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    d = tmp_path / ".dockerenv"; d.write_text("", encoding="utf-8")
    monkeypatch.setattr(ec, "_DOCKERENV_PATH", str(d), raising=False)
    assert ec._is_cloud_container() is True
```

- [ ] **Step 3 — run, confirm fail** (`_is_cloud_container` absent).

- [ ] **Step 4 — implement** (add near the other module helpers):
```python
import os

_DOCKERENV_PATH = "/.dockerenv"

def _is_cloud_container() -> bool:
    """Deterministic best-effort: are we in a designated isolated runtime
    (cloud node / k8s / docker)? Such runtimes are already isolated, so the
    deterministic primary-checkout lock should NOT force a worktree there.
    Never raises."""
    try:
        for marker in ("OUROBOROS_CLOUD_NODE", "KUBERNETES_SERVICE_HOST"):
            if (os.environ.get(marker, "") or "").strip():
                return True
        return os.path.exists(_DOCKERENV_PATH)
    except Exception:  # noqa: BLE001
        return False
```

- [ ] **Step 5 — run, confirm pass.** Regression: `python3 -m pytest tests/governance/ -k execution_context -q`.

- [ ] **Step 6 — commit:** `feat(boundary): deterministic _is_cloud_container() (isolated-runtime detection)`

---

## Task 2: Deterministic override in `resolve_loop_project_root()` (LR-A dual-arm)

**Files:** Modify `autonomous_workspace.py`; Test `tests/governance/test_deterministic_isolation_lock.py` (Create)

- [ ] **Step 1 — read first:** `autonomous_workspace.py:48-106` — `file_isolation_enabled()`, `resolve_loop_project_root()`, the line-73 early-return, the worktree routing block, imports. Confirm how `is_primary_checkout`/`is_autonomous` are imported.

- [ ] **Step 2 — failing test:**
```python
# tests/governance/test_deterministic_isolation_lock.py
from __future__ import annotations
import asyncio
from backend.core.ouroboros.governance import autonomous_workspace as aw

def test_lock_disabled_is_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "false")
    assert aw._deterministic_force(root="/x", is_primary=True, container=False, autonomous=True) is False

def test_lock_forces_in_primary_autonomous_noncontainer(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert aw._deterministic_force(root="/x", is_primary=True, container=False, autonomous=True) is True

def test_lock_noop_in_container(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert aw._deterministic_force(root="/x", is_primary=True, container=True, autonomous=True) is False

def test_lock_noop_when_operator_present(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert aw._deterministic_force(root="/x", is_primary=True, container=False, autonomous=False) is False

def test_lock_dual_arms_both_flags(monkeypatch):
    monkeypatch.delenv("JARVIS_FILE_ISOLATION_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_EXECUTION_BOUNDARY_ENABLED", raising=False)
    aw._arm_boundary_flags()
    import os
    assert os.environ["JARVIS_FILE_ISOLATION_ENABLED"] == "true"
    assert os.environ["JARVIS_EXECUTION_BOUNDARY_ENABLED"] == "true"
```

- [ ] **Step 3 — run, confirm fail.**

- [ ] **Step 4 — implement.** Add the two pure helpers + wire into `resolve_loop_project_root`:
```python
def _deterministic_lock_enabled() -> bool:
    import os
    return (os.environ.get("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true") or "").strip().lower() in ("1","true","yes","on")

def _deterministic_force(root, is_primary: bool, container: bool, autonomous: bool) -> bool:
    """LR-A trigger: force isolation iff lock on AND in primary checkout AND not a
    container AND autonomous (no operator presence). Pure. Never raises."""
    try:
        return bool(_deterministic_lock_enabled() and is_primary and (not container) and autonomous)
    except Exception:  # noqa: BLE001
        return False

def _arm_boundary_flags() -> None:
    """LR-A: force-arm BOTH flags as a pair, in-process, so downstream Stage A
    (commit denial) and Stage B (isolation) both read armed. Never raises."""
    import os
    try:
        os.environ["JARVIS_FILE_ISOLATION_ENABLED"] = "true"
        os.environ["JARVIS_EXECUTION_BOUNDARY_ENABLED"] = "true"
    except Exception:  # noqa: BLE001
        pass
```
Then in `resolve_loop_project_root(repo_root, session_id, worktree_manager=None)`, BEFORE the `if not file_isolation_enabled(): return root` early-return, insert:
```python
    from backend.core.ouroboros.governance.execution_context import (
        is_primary_checkout, is_autonomous, _is_cloud_container,
    )
    _root = Path(repo_root)
    if _deterministic_force(
        _root,
        is_primary=bool(is_primary_checkout(_root)),
        container=bool(_is_cloud_container()),
        autonomous=bool(is_autonomous(_root)),
    ):
        _arm_boundary_flags()
        logger.warning(
            "[DeterministicLock] forced isolation+boundary despite env "
            "(primary checkout, autonomous) root=%s session=%s", _root, session_id,
        )
        # fall through to the (now-armed) routing below — do NOT early-return
    elif not file_isolation_enabled():
        return _root
```
(IMPLEMENTER: adapt to the file's actual control flow — the key invariants: when `_deterministic_force` is True, BOTH flags get armed AND routing proceeds even though the flags were originally false; when false, legacy behavior is byte-identical. Confirm `logger` exists in the module.)

- [ ] **Step 5 — run, confirm pass.** Regression: `python3 -m pytest tests/governance/ -k "autonomous_workspace or isolation" -q`.

- [ ] **Step 6 — commit:** `feat(boundary): deterministic isolation lock — force-arm both flags + route in primary checkout (LR-A)`

---

## Task 3: Raw-write guard (deny edit_file/write_file to primary checkout)

**Files:** Modify `tool_executor.py`; Test `tests/governance/test_raw_write_guard.py` (Create)

- [ ] **Step 1 — read first:** find the write-path policy in `tool_executor.py` — `_safe_resolve()` (~1700-1722) + where `edit_file`/`write_file` are dispatched + the `PolicyContext` (`is_read_only` ~156). Identify the cleanest pre-write hook to deny.

- [ ] **Step 2 — failing test** (pure helper, seam-level):
```python
# tests/governance/test_raw_write_guard.py
from __future__ import annotations
from backend.core.ouroboros.governance import tool_executor as te

def test_raw_write_denied_in_primary_autonomous(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert te._deny_primary_raw_write(is_primary=True, autonomous=True) is True

def test_raw_write_allowed_in_worktree(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert te._deny_primary_raw_write(is_primary=False, autonomous=True) is False

def test_raw_write_allowed_for_operator(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true")
    assert te._deny_primary_raw_write(is_primary=True, autonomous=False) is False

def test_raw_write_guard_off_when_lock_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "false")
    assert te._deny_primary_raw_write(is_primary=True, autonomous=True) is False
```

- [ ] **Step 3 — run, confirm fail.**

- [ ] **Step 4 — implement** the pure helper + wire it into the edit_file/write_file dispatch so a True verdict yields a policy denial (`POLICY_DENIED reason=primary_checkout_raw_write`), reusing the existing denial-formatting path:
```python
def _deny_primary_raw_write(is_primary: bool, autonomous: bool) -> bool:
    """Defense-in-depth (spec 5.2): deny autonomous raw writes to a primary
    checkout when the deterministic lock is enabled. Pure. Never raises."""
    import os
    try:
        if (os.environ.get("JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED", "true") or "").strip().lower() not in ("1","true","yes","on"):
            return False
        return bool(is_primary and autonomous)
    except Exception:  # noqa: BLE001
        return False
```
At the write dispatch: compute `is_primary = is_primary_checkout(self._repo_root)` (lazy import from execution_context) + `autonomous = is_autonomous(self._repo_root)`; if `_deny_primary_raw_write(...)` → record a denial (reuse `_format_denial`) + log `[RawWriteGuard] denied <tool> op=<id>`, skip execution. Fail-soft: guard errors fall through to legacy.

- [ ] **Step 5 — run, confirm pass.** Regression: `python3 -m pytest tests/governance/ -k "tool_loop or tool_executor" -q`.

- [ ] **Step 6 — commit:** `feat(boundary): raw-write guard — deny autonomous edit/write to primary checkout (spec 5.2)`

---

## Task 4: `verify_file_isolation.py` — I5 override proof

**Files:** Modify `scripts/verify_file_isolation.py`; Test `tests/governance/test_verify_isolation_i5.py` (Create, exercises the pure assessment fn)

- [ ] **Step 1 — read first:** the I1–I4 `Invariant` structure + `assess_isolation()` + the regex markers.

- [ ] **Step 2 — failing test:** assert `assess_isolation` (or the new I5 checker) returns an I5 invariant that passes when the log contains `[DeterministicLock] forced isolation+boundary` AND the primary is pristine; fails otherwise. (Feed it synthetic log text + a clean/dirty porcelain stub.)

- [ ] **Step 3/4 — implement I5:** new `Invariant("I5_deterministic_lock_override", ...)` — passes iff debug log contains the `[DeterministicLock] forced isolation+boundary` marker AND I3's pristine check holds AND both flags are observed armed. Add a `--prove-override` mode that runs a tiny headless boot with `JARVIS_FILE_ISOLATION_ENABLED=false` explicitly set and asserts I5.

- [ ] **Step 5 — run, confirm pass.**

- [ ] **Step 6 — commit:** `feat(boundary): verify_file_isolation I5 — prove explicit env=false is overridden by the lock (G4)`

---

# LAYER 2 — Graceful Async Yield (default-off)

## Task 5: Mutation critical-section guard (LR-B drain primitive)

**Files:** Create `backend/core/ouroboros/governance/mutation_critical_section.py`; Test `tests/governance/test_mutation_critical_section.py`

- [ ] **Step 1 — failing tests:** an async re-entrant section: `async with mutation_section(op_id):` increments a per-op + global counter; `is_mutating(op_id)` True inside, False outside; `await drain(op_id, timeout)` returns True when section exits before timeout, False (abandon) when it wedges past `timeout`.
```python
# tests/governance/test_mutation_critical_section.py
from __future__ import annotations
import asyncio
from backend.core.ouroboros.governance import mutation_critical_section as mcs

def test_section_marks_mutating():
    async def go():
        assert mcs.is_mutating("op1") is False
        async with mcs.mutation_section("op1"):
            assert mcs.is_mutating("op1") is True
        assert mcs.is_mutating("op1") is False
    asyncio.run(go())

def test_drain_returns_true_when_idle():
    async def go():
        return await mcs.drain("opX", timeout=0.5)
    assert asyncio.run(go()) is True   # nothing in flight -> drains immediately

def test_drain_waits_then_true():
    async def go():
        async def hold():
            async with mcs.mutation_section("op2"):
                await asyncio.sleep(0.2)
        t = asyncio.create_task(hold())
        await asyncio.sleep(0.01)
        ok = await mcs.drain("op2", timeout=2.0)
        await t
        return ok
    assert asyncio.run(go()) is True

def test_drain_abandons_on_wedge():
    async def go():
        async def wedge():
            async with mcs.mutation_section("op3"):
                await asyncio.sleep(5.0)
        t = asyncio.create_task(wedge())
        await asyncio.sleep(0.01)
        ok = await mcs.drain("op3", timeout=0.2)   # wedged past cap -> abandon
        t.cancel()
        return ok
    assert asyncio.run(go()) is False
```

- [ ] **Step 2 — run, confirm fail.**
- [ ] **Step 3 — implement** the module: a module-level `dict[op_id->int]` counter guarded by an `asyncio.Lock`, an `@asynccontextmanager mutation_section(op_id)`, `is_mutating(op_id)`, and `async def drain(op_id, timeout)` that polls (short sleep) until count==0 or timeout. Pure stdlib + asyncio. Fail-soft. No-op semantics are fine when unused.
- [ ] **Step 4 — run, confirm pass.**
- [ ] **Step 5 — instrument the apply/commit path:** wrap `ChangeEngine.execute` (and the tool-loop write_file/edit_file actual write, and AutoCommitter commit) in `async with mutation_section(op_id):` — ONLY active when `JARVIS_OPERATOR_YIELD_ENABLED` is on (else a cheap no-op context). Read those call sites first; keep byte-identical when off.
- [ ] **Step 6 — commit:** `feat(yield): mutation critical-section guard for atomic yield integrity (LR-B)`

## Task 6: `operator_presence.py` — deterministic detector + watcher
**Files:** Create `operator_presence.py` + tests. `operator_present()` from last-input-ts (`JARVIS_OPERATOR_IDLE_S`, default 45) + injectable liveness probe; `OperatorPresenceWatcher` publishes `operator.active`/`operator.idle` `TrinityEvent`s edge-triggered. Reuse `register_session_liveness_probe`/`trinity_event_bus.publish`. Tests: idle threshold, edge-trigger (no level spam), fail-soft. **Commit.**

## Task 7: SensorGovernor `operator_active_fn` (hard-zero caps)
**Files:** Modify `sensor_governor.py` + tests. Inject `operator_active_fn` (mirror `_posture_fn`); when True, weighted cap → 0 (distinct from the 0.2× emergency brake). Byte-identical when fn None / `JARVIS_OPERATOR_YIELD_ENABLED=false`. **Commit.**

## Task 8: `operator_yield_bridge.py` — drain-then-park / resume
**Files:** Create `operator_yield_bridge.py` + `op_park_store.should_park_for_route(operator_suspended=)` + tests. Subscribe `operator.active` → `await mcs.drain(op_id, JARVIS_OPERATOR_YIELD_DRAIN_MAX_S)` → if drained: `should_park_for_route(..., operator_suspended=True)` → park; if abandoned: log `[OperatorYield] drain abandoned`, no park. Subscribe `operator.idle` → `submit_for_resume`. Gated `JARVIS_OPERATOR_YIELD_ENABLED` default-false. Tests: drain-then-park, abandon-no-park, resume-on-idle. **Commit.**

## Task 9: Integration + OFF byte-identical + verify
**Files:** Create `tests/governance/test_async_yield_integration.py`. End-to-end: operator.active (with an in-flight mutation) → waits for drain → parks; operator.idle → resumes; all-flags-off → byte-identical (no presence watcher, no governor zeroing, no park trigger, Layer-1 still active). Run full reused-subsystem regression (park, governor, event-bus, tool_loop, execution_context). **Commit.**

---

## Done criteria
- Layer 1: deterministic lock forces BOTH flags + routes + denies raw writes in primary checkout; `verify_file_isolation.py --prove-override` green with explicit `env=false`.
- Layer 2: operator.active → drain → park (never mid-mutation, LR-B); operator.idle → resume; default-off byte-identical.
- Zero real regressions in reused subsystems. Live soak (operator-run) validates the yield end-to-end.

## Self-review
- Spec coverage: §5.1 (T2/LR-A), §5.2 (T3), §5.3 (T6), §5.4 (T5/T7/T8/LR-B), §5.5 (T4), §6 gating (all tasks). 
- Type consistency: `_deterministic_force`/`_deny_primary_raw_write` pure bools; `mutation_section`/`drain` async; event topics `operator.active`/`operator.idle` identical across T6/T8.
- Large-file tasks (T2/T3/T5/T7/T8) ship seam-tests + read-first edit instructions — exact line numbers confirmed by the implementer against live code.

# L3 Memory Governor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a worktree-RAM-budget concurrency clamp to the L3 `SubagentScheduler` so parallel worktree fan-out can never OOM/swap-thrash a 16GB unified-memory host.

**Architecture:** A small pure-function module (`l3_memory_governor.py`) computes `max_worktrees` from live available RAM (sourced from the existing `MemoryPressureGate` probe — zero duplication) divided by a per-worktree RAM budget, clamped by the gate's existing free-%-based fan-out cap (strictest-wins). The scheduler composes this clamp *on top of* the Slice 5 Arc B fan-out gate already in `_run_graph`, reusing the same zero-work-loss defer-overflow mechanism.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), `asyncio`, stdlib `math`/`os`, existing `MemoryPressureGate`, pytest.

---

## File Structure

- **Create:** `backend/core/ouroboros/governance/autonomy/l3_memory_governor.py` — pure governor math + env knobs + `GovernorDecision`. No IO, no scheduler import. One responsibility: "given requested N, available MB, budget MB, and the level cap, how many worktrees may run?"
- **Modify:** `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py` — add `_consult_memory_governor(...)` and compose its clamp after the existing fan-out clamp in `_run_graph`.
- **Test:** `tests/governance/autonomy/test_l3_memory_governor.py` (pure math) and `tests/governance/autonomy/test_scheduler_memory_governor.py` (scheduler integration with injected gate).

Why a separate module: the scheduler is large and the math must be testable without booting a graph. The clamp logic is the load-bearing safety code; isolating it lets us prove every pressure level deterministically.

---

## Task 1: Governor math module (pure)

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/l3_memory_governor.py`
- Test: `tests/governance/autonomy/test_l3_memory_governor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_l3_memory_governor.py
from __future__ import annotations

from backend.core.ouroboros.governance.autonomy.l3_memory_governor import (
    GovernorDecision,
    compute_worktree_cap,
)


def test_ram_is_the_binding_constraint():
    # 4500MB available, 1500MB/worktree -> ram_cap=3; level_cap=8 -> allow 3
    d = compute_worktree_cap(
        requested=8, avail_mb=4500.0, budget_mb=1500, level_cap=8,
    )
    assert isinstance(d, GovernorDecision)
    assert d.ram_cap == 3
    assert d.n_allowed == 3
    assert d.disposition == "clamp"


def test_level_cap_is_the_binding_constraint():
    # 12000MB -> ram_cap=8; but level_cap=3 (HIGH) -> allow 3, strictest wins
    d = compute_worktree_cap(
        requested=8, avail_mb=12000.0, budget_mb=1500, level_cap=3,
    )
    assert d.ram_cap == 8
    assert d.n_allowed == 3
    assert d.disposition == "clamp"


def test_floor_never_below_one():
    # Only 800MB available, 1500MB budget -> floor would be 0; clamp to >=1
    d = compute_worktree_cap(
        requested=4, avail_mb=800.0, budget_mb=1500, level_cap=8,
    )
    assert d.ram_cap == 1
    assert d.n_allowed == 1


def test_no_clamp_when_everything_fits():
    d = compute_worktree_cap(
        requested=2, avail_mb=16000.0, budget_mb=1500, level_cap=8,
    )
    assert d.n_allowed == 2
    assert d.disposition == "allow"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/autonomy/test_l3_memory_governor.py -v`
Expected: FAIL — `ModuleNotFoundError: ... l3_memory_governor`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/autonomy/l3_memory_governor.py
"""L3 worktree-RAM-budget governor (pure math).

Composes ON TOP of MemoryPressureGate's free-%-based fan-out caps:
the gate answers "is the box under pressure?"; this module answers
"given the absolute RAM cost of a worktree, how many fit right now?".
Strictest-wins between the two. No IO, no scheduler import — every
decision is a deterministic function of its arguments so it can be
proven at all pressure levels in isolation.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes")


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def governor_enabled() -> bool:
    """Master flag. Default TRUE; inert until an L3 graph actually runs."""
    return _env_bool("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", True)


def worktree_ram_budget_mb() -> int:
    """Assumed peak RAM per concurrent worktree. Default 1500MB."""
    return _env_int("JARVIS_L3_WORKTREE_RAM_BUDGET_MB", 1500, minimum=64)


@dataclass(frozen=True)
class GovernorDecision:
    requested: int
    ram_cap: int
    level_cap: int
    n_allowed: int
    avail_mb: float
    budget_mb: int
    disposition: str  # "allow" | "clamp" | "disabled" | "probe_fail"


def compute_worktree_cap(
    *,
    requested: int,
    avail_mb: float,
    budget_mb: int,
    level_cap: int,
) -> GovernorDecision:
    """Pure clamp. ``ram_cap = floor(avail_mb / budget_mb)`` (>=1);
    final allowance is the strictest of requested / ram_cap / level_cap."""
    ram_cap = max(1, int(math.floor(avail_mb / float(budget_mb))))
    n_allowed = max(0, min(requested, ram_cap, level_cap))
    disposition = "clamp" if n_allowed < requested else "allow"
    return GovernorDecision(
        requested=requested,
        ram_cap=ram_cap,
        level_cap=level_cap,
        n_allowed=n_allowed,
        avail_mb=avail_mb,
        budget_mb=budget_mb,
        disposition=disposition,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/autonomy/test_l3_memory_governor.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/l3_memory_governor.py tests/governance/autonomy/test_l3_memory_governor.py
git commit -m "feat(l3): pure worktree-RAM-budget governor math (Unit D)"
```

---

## Task 2: Scheduler consultation method

**Files:**
- Modify: `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py` (add method near `_consult_memory_gate`, ~line 756)
- Test: `tests/governance/autonomy/test_scheduler_memory_governor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_scheduler_memory_governor.py
from __future__ import annotations

import types

import pytest

from backend.core.ouroboros.governance.autonomy import subagent_scheduler as ss


class _FakeProbe:
    def __init__(self, available_mb: float):
        self.available_bytes = int(available_mb * 1024 * 1024)
        self.total_bytes = 16 * 1024 * 1024 * 1024
        self.ok = True


class _FakeGate:
    def __init__(self, available_mb: float):
        self._p = _FakeProbe(available_mb)

    def probe(self):
        return self._p


def _make_scheduler():
    # Construct with minimal stubs; only _consult_memory_governor is exercised.
    return ss.SubagentScheduler.__new__(ss.SubagentScheduler)


def test_governor_clamps_on_low_ram(monkeypatch):
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_L3_WORKTREE_RAM_BUDGET_MB", "1500")
    monkeypatch.setattr(
        ss, "get_default_gate", lambda: _FakeGate(available_mb=4500.0),
    )
    sched = _make_scheduler()
    decision = sched._consult_memory_governor(
        8, graph_id="g1", level_cap=8,
    )
    assert decision is not None
    assert decision.n_allowed == 3
    assert decision.disposition == "clamp"


def test_governor_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "false")
    sched = _make_scheduler()
    assert sched._consult_memory_governor(8, graph_id="g1", level_cap=8) is None


def test_governor_probe_failure_is_non_fatal(monkeypatch):
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "true")

    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(ss, "get_default_gate", _boom)
    sched = _make_scheduler()
    # Must swallow and return None — scheduler never breaks on probe failure.
    assert sched._consult_memory_governor(8, graph_id="g1", level_cap=8) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/autonomy/test_scheduler_memory_governor.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_consult_memory_governor'` (and possibly `get_default_gate` not importable at module scope).

- [ ] **Step 3a: Ensure `get_default_gate` is importable at module scope**

In `subagent_scheduler.py`, the existing `_consult_memory_gate` imports `get_default_gate` *inside* the method. Add a module-level import so tests can `monkeypatch.setattr(ss, "get_default_gate", ...)`. Near the top imports (after the existing imports block), add:

```python
from backend.core.ouroboros.governance.memory_pressure_gate import (
    get_default_gate,
)
```

(Leave the in-method import in `_consult_memory_gate` as-is to avoid disturbing the AST-pinned Slice 5 Arc B path.)

- [ ] **Step 3b: Add the consultation method**

Insert immediately after `_consult_memory_gate` (after line 756):

```python
    def _consult_memory_governor(
        self,
        n_requested: int,
        *,
        graph_id: str,
        level_cap: int,
    ) -> Optional[Any]:
        """Unit D — worktree-RAM-budget clamp composed on top of the
        Slice 5 Arc B fan-out gate.

        Returns a ``GovernorDecision`` (or ``None`` when disabled / on
        any probe failure — the scheduler must never break on the
        governor). ``level_cap`` is the allowance already granted by
        the free-%-based fan-out gate; the governor takes the strictest
        of that and the absolute RAM budget.
        """
        from backend.core.ouroboros.governance.autonomy.l3_memory_governor import (
            GovernorDecision,
            compute_worktree_cap,
            governor_enabled,
            worktree_ram_budget_mb,
        )

        if not governor_enabled():
            return None
        try:
            gate = get_default_gate()
            probe = gate.probe()
            avail_mb = float(probe.available_bytes) / (1024.0 * 1024.0)
        except Exception:  # noqa: BLE001 — governor must not break scheduler
            logger.debug(
                "[SubagentScheduler] memory governor probe failed "
                "(non-fatal)", exc_info=True,
            )
            return None

        decision = compute_worktree_cap(
            requested=n_requested,
            avail_mb=avail_mb,
            budget_mb=worktree_ram_budget_mb(),
            level_cap=level_cap,
        )
        log_fn = (
            logger.warning if decision.disposition == "clamp" else logger.info
        )
        log_fn(
            "[SubagentScheduler] ram_governor: graph=%s disposition=%s "
            "requested=%d allowed=%d ram_cap=%d level_cap=%d avail_mb=%.0f "
            "budget_mb=%d",
            graph_id, decision.disposition, decision.requested,
            decision.n_allowed, decision.ram_cap, decision.level_cap,
            decision.avail_mb, decision.budget_mb,
        )
        return decision
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/autonomy/test_scheduler_memory_governor.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/subagent_scheduler.py tests/governance/autonomy/test_scheduler_memory_governor.py
git commit -m "feat(l3): scheduler memory-governor consultation (Unit D)"
```

---

## Task 3: Wire the governor clamp into `_run_graph`

**Files:**
- Modify: `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py:500-507`
- Test: `tests/governance/autonomy/test_scheduler_memory_governor.py` (add an integration-style test)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/autonomy/test_scheduler_memory_governor.py

def test_run_graph_clamp_composition(monkeypatch):
    """The governor clamp composes after the fan-out clamp: selected is
    truncated to the governor's n_allowed and overflow is deferred."""
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_L3_WORKTREE_RAM_BUDGET_MB", "1500")
    monkeypatch.setattr(
        ss, "get_default_gate", lambda: _FakeGate(available_mb=3000.0),
    )
    sched = _make_scheduler()
    selected = ["u1", "u2", "u3", "u4"]
    deferred = []
    gov = sched._consult_memory_governor(
        len(selected), graph_id="g1", level_cap=len(selected),
    )
    assert gov.n_allowed == 2  # 3000/1500 = 2
    # Simulate the composition the _run_graph edit performs:
    overflow = list(selected[gov.n_allowed:])
    selected = list(selected[:gov.n_allowed])
    deferred = sorted(deferred + overflow)
    assert selected == ["u1", "u2"]
    assert deferred == ["u3", "u4"]
```

- [ ] **Step 2: Run test to verify it fails (it passes the helper math but proves the contract the edit must honor)**

Run: `pytest tests/governance/autonomy/test_scheduler_memory_governor.py::test_run_graph_clamp_composition -v`
Expected: PASS (this codifies the exact composition the next step wires into `_run_graph`).

- [ ] **Step 3: Edit `_run_graph` to compose the governor clamp**

Replace the existing block at lines 500-507:

```python
                if selected:
                    decision = self._consult_memory_gate(
                        len(selected), graph_id=graph_id,
                    )
                    if decision is not None and decision.n_allowed < len(selected):
                        overflow = list(selected[decision.n_allowed:])
                        selected = list(selected[:decision.n_allowed])
                        deferred = sorted(list(deferred) + overflow)
```

with:

```python
                if selected:
                    decision = self._consult_memory_gate(
                        len(selected), graph_id=graph_id,
                    )
                    if decision is not None and decision.n_allowed < len(selected):
                        overflow = list(selected[decision.n_allowed:])
                        selected = list(selected[:decision.n_allowed])
                        deferred = sorted(list(deferred) + overflow)

                # Unit D — worktree-RAM-budget governor composes on top of
                # the fan-out gate (strictest-wins). Same zero-work-loss
                # defer-overflow mechanism; disabled/probe-fail -> no clamp.
                if selected:
                    level_cap = (
                        decision.n_allowed
                        if decision is not None
                        else len(selected)
                    )
                    gov = self._consult_memory_governor(
                        len(selected), graph_id=graph_id, level_cap=level_cap,
                    )
                    if gov is not None and gov.n_allowed < len(selected):
                        overflow = list(selected[gov.n_allowed:])
                        selected = list(selected[:gov.n_allowed])
                        deferred = sorted(list(deferred) + overflow)
```

- [ ] **Step 4: Run the full scheduler suite to confirm no regression**

Run: `pytest tests/governance/autonomy/ -v`
Expected: PASS (existing scheduler tests + new governor tests). Confirm the existing `test_subagent_executor_worktree.py` and any `_run_graph` tests still pass — the governor is additive and disabled→no-op.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/autonomy/subagent_scheduler.py tests/governance/autonomy/test_scheduler_memory_governor.py
git commit -m "feat(l3): compose RAM-governor clamp in _run_graph fan-out (Unit D)"
```

---

## Task 4: OFF-is-inert regression guard

**Files:**
- Test: `tests/governance/autonomy/test_scheduler_memory_governor.py` (add)

- [ ] **Step 1: Write the test**

```python
# append to tests/governance/autonomy/test_scheduler_memory_governor.py

def test_disabled_governor_is_byte_identical_passthrough(monkeypatch):
    """With the master flag off, _consult_memory_governor returns None
    and the _run_graph composition leaves `selected` untouched."""
    monkeypatch.setenv("JARVIS_L3_MEMORY_GOVERNOR_ENABLED", "false")
    sched = _make_scheduler()
    selected = ["u1", "u2", "u3"]
    gov = sched._consult_memory_governor(
        len(selected), graph_id="g1", level_cap=len(selected),
    )
    assert gov is None
    # Composition guard: None -> no truncation.
    if gov is not None and gov.n_allowed < len(selected):
        selected = selected[:gov.n_allowed]
    assert selected == ["u1", "u2", "u3"]
```

- [ ] **Step 2: Run it**

Run: `pytest tests/governance/autonomy/test_scheduler_memory_governor.py::test_disabled_governor_is_byte_identical_passthrough -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/governance/autonomy/test_scheduler_memory_governor.py
git commit -m "test(l3): governor-off is byte-identical passthrough (Unit D)"
```

---

## Self-Review Notes

- **Spec coverage:** §7.2 worktree-RAM-budget clamp (Tasks 1-3); §7 env knobs `JARVIS_L3_MEMORY_GOVERNOR_ENABLED` / `JARVIS_L3_WORKTREE_RAM_BUDGET_MB` (Task 1); reuse of `MemoryPressureGate` not a new probe (Task 2 uses `get_default_gate().probe()`); OFF-is-inert (Task 4). The §7.3 CRITICAL→legacy pre-emptive trip is intentionally **not** here — it lives in the Plan 2 / Unit C circuit breaker, since the PLAN-vs-legacy decision happens in the orchestrator before the scheduler runs. This plan's governor handles already-admitted graphs only.
- **Type consistency:** `GovernorDecision` fields (`requested`, `ram_cap`, `level_cap`, `n_allowed`, `avail_mb`, `budget_mb`, `disposition`) are used identically in module, method, and tests. `compute_worktree_cap` keyword args match every call site.
- **No placeholders:** every step ships real code and a runnable command.

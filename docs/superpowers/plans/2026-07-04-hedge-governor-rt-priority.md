# Hedge-Governor RT-Priority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the hedge-governor routing defect so write-intent ops always get the real-time Venom tool loop (the A1 mutation prerequisite), with the batch arm structurally decoupled (deferred ignition — never fired unless RT fails), killing both the "hedge WON by batch → 0 TOOL OUTPUT" defect and the double-billing leak.

**Architecture:** Three coupled root-cause fixes. (1) Replace the complexity-string-only `prefer_fast` predicate with a dynamic `HedgeArmPolicy` resolver in `dw_transport_hedge.py` consuming workload complexity + write-intent + route + Tier-4 target-stratification metrics at runtime. (2) Add a deferred-ignition mode to `hedged_race` — when the RT arm is prioritized, the stable (batch) arm is NOT fired at race start; it ignites event-driven on RT failure/rupture only (structural zero-double-billing; no sleeps, no padding). (3) Lift the route-based Venom suppression for BACKGROUND write-intent ops in `compute_tool_loop_suppressed` — a mutation op needs tools; the preload-credit path stays for read-only/SPECULATIVE.

**Tech Stack:** Python 3.9+ asyncio (no `asyncio.timeout`; `asyncio.wait_for` only), existing `dw_transport_hedge.hedged_race`, `exploration_engine.exploration_gate_demands_tools`/`compute_tool_loop_suppressed`, `route_predicates.should_skip_venom_for_route`, Tier-4 `target_stratification` (`coverage_index_ready`, `file_has_test_coverage`, `stratification_penalty_multiplier`), `observability_registry` hedge counters. pytest + pytest-asyncio.

## Root Cause (evidence, verified in source)

1. **`prefer_fast` predicate too narrow + fails-closed on empty** — `doubleword_provider.py:3030-3040`: `_s227_prefer_fast = exploration_gate_demands_tools(str(context.task_complexity))`; `exploration_engine.py:487` returns `False` for `complexity in ("trivial", "")` and on any exception. Write-intent, route, and stratification never enter the decision → for any op with unset/trivial complexity the batch arm pre-empts the RT/tool arm → live log `hedge WON by batch`, 0 TOOL OUTPUT.
2. **Route-based Venom suppression makes the RT arm toolless for BACKGROUND write ops** — `exploration_engine.compute_tool_loop_suppressed` preserves the route skip ("BACKGROUND keeps its preload-credit path"). Campaign fix #7 (preloaded exploration credit) lets these ops PASS the Iron Gate *without tools* — solving death-at-gate but institutionalizing empty mutations (candidates generated blind → `read_only_complete`, `files_changed=0` across 53 sessions). The asymmetry is backwards: write ops need tools MORE than read-only ops.
3. **prefer_fast eager-buffer mode double-runs both arms** — `dw_transport_hedge.py:107` fires `t_stable` at race start; in prefer_fast mode a completed batch result is buffered while RT continues (line 179). When RT then succeeds, the batch spend is 100% wasted — a structural double-billing leak on every gate-op.

## Global Constraints

- Python 3.9+ only — never `asyncio.timeout`; use `asyncio.wait_for`. `from __future__ import annotations` at top of every touched module (already present in all four).
- NO hardcoded latency padding, NO artificial sleeps, NO arbitrary timeout extensions to make RT win. The deferral is event-driven (RT terminal failure ignites batch), not timed.
- All new thresholds/policy knobs env-resolved at call time with sensible defaults; kill switches revert byte-identical to legacy: `JARVIS_HEDGE_POLICY_RESOLVER_ENABLED` (default `true`), `JARVIS_HEDGE_DEFER_STABLE_ENABLED` (default `true`), `JARVIS_VENOM_WRITE_INTENT_LIFT_ENABLED` (default `true`).
- DRY: extend `hedged_race` in place (no fork); reuse `exploration_gate_demands_tools`, `should_skip_venom_for_route`, Tier-4 stratification APIs, `observability_registry.record_hedge_*`. No duplicated execution handlers.
- Fail-soft everywhere: any resolver/stratification error degrades to the legacy decision, never raises into the dispatch path. Stratification is consulted ONLY when `coverage_index_ready()` (never trigger a build on the hot path).
- Transactional isolation (mandate #4): with `defer_stable=True`, at most ONE arm's result is ever claimed; the batch arm is not created unless RT terminally fails; the `finally` cancels whichever tasks exist. Rupture-protection is preserved: RT rupture → batch ignites with the op's remaining deadline (same profile as the legacy sequential RT→batch fallback that already exists at `doubleword_provider.py:3065+`).
- ASCII-only source. `context` is duck-typed — read attrs via `getattr(context, "...", default)` exactly like the existing provider code (`task_complexity`, `provider_route`, `is_read_only`, `target_files`).

---

## Task 1: `HedgeArmPolicy` resolver (dynamic, stratification-aware)

**Files:**
- Modify: `backend/core/ouroboros/governance/dw_transport_hedge.py` (append after `should_skip_race_for_storm`, before `hedged_race`)
- Test: `tests/governance/test_hedge_arm_policy.py` (new)

**Interfaces:**
- Consumes: `exploration_gate_demands_tools(complexity, *, gate_enabled=None) -> bool` (`exploration_engine.py:487`); `coverage_index_ready(repo_root) -> bool`, `file_has_test_coverage(file_path, repo_root) -> bool` (`target_stratification.py:236,472`).
- Produces (later tasks rely on these exact names):
  - `@dataclass(frozen=True) class HedgeArmPolicy`: `prefer_fast: bool`, `defer_stable: bool`, `reason: str`.
  - `hedge_policy_resolver_enabled() -> bool` — env `JARVIS_HEDGE_POLICY_RESOLVER_ENABLED`, default True.
  - `hedge_defer_stable_enabled() -> bool` — env `JARVIS_HEDGE_DEFER_STABLE_ENABLED`, default True.
  - `resolve_hedge_arm_policy(*, complexity: str, route: str, is_read_only: bool, target_files: tuple, repo_root: Optional[str]) -> HedgeArmPolicy`.

**Decision matrix (the root fix for hole #1):**
- Resolver disabled → legacy: `prefer_fast = exploration_gate_demands_tools(complexity)`, `defer_stable=False`, reason `"legacy_s227"`.
- Write-intent (`not is_read_only`) with real `target_files` → `prefer_fast=True` even when complexity is empty/unknown (fail-SAFE toward the tool arm for mutations — inverts the fail-closed hole), reason `"write_intent"`.
- Gate-demanding complexity (existing predicate) → `prefer_fast=True`, reason `"gate_demands"`.
- Stratification refinement (only when `coverage_index_ready(repo_root)`): a write-intent op whose first target file LACKS test coverage (`file_has_test_coverage` False) keeps `prefer_fast=True` and appends `"+uncovered_target"` to reason (uncovered targets need exploration most; this is the runtime metric feed — no penalty math needed on this path, the boolean is the signal).
- `defer_stable = prefer_fast and hedge_defer_stable_enabled()`.
- Read-only + non-gate complexity → `prefer_fast=False` (legacy race preserved — cheap BG reflex ops keep the fast batch win).
- ANY exception → legacy result, reason `"fail_soft_legacy"`. NEVER raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_hedge_arm_policy.py
from __future__ import annotations

from backend.core.ouroboros.governance.dw_transport_hedge import (
    HedgeArmPolicy,
    hedge_policy_resolver_enabled,
    resolve_hedge_arm_policy,
)


def test_write_intent_prefers_fast_even_with_empty_complexity(monkeypatch):
    monkeypatch.delenv("JARVIS_HEDGE_POLICY_RESOLVER_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_HEDGE_DEFER_STABLE_ENABLED", raising=False)
    p = resolve_hedge_arm_policy(
        complexity="", route="background", is_read_only=False,
        target_files=("backend/foo.py",), repo_root=None,
    )
    assert p.prefer_fast is True          # the fail-closed hole is inverted for writes
    assert p.defer_stable is True         # structural double-billing kill
    assert "write_intent" in p.reason


def test_read_only_trivial_keeps_legacy_race(monkeypatch):
    monkeypatch.delenv("JARVIS_HEDGE_POLICY_RESOLVER_ENABLED", raising=False)
    p = resolve_hedge_arm_policy(
        complexity="trivial", route="background", is_read_only=True,
        target_files=(), repo_root=None,
    )
    assert p.prefer_fast is False
    assert p.defer_stable is False


def test_gate_demanding_complexity_prefers_fast(monkeypatch):
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    p = resolve_hedge_arm_policy(
        complexity="moderate", route="standard", is_read_only=True,
        target_files=(), repo_root=None,
    )
    assert p.prefer_fast is True


def test_resolver_disabled_reverts_to_legacy_s227(monkeypatch):
    monkeypatch.setenv("JARVIS_HEDGE_POLICY_RESOLVER_ENABLED", "false")
    p = resolve_hedge_arm_policy(
        complexity="", route="background", is_read_only=False,
        target_files=("backend/foo.py",), repo_root=None,
    )
    assert p.prefer_fast is False         # legacy: "" -> False (byte-identical s227)
    assert p.defer_stable is False
    assert p.reason == "legacy_s227"


def test_defer_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_HEDGE_DEFER_STABLE_ENABLED", "false")
    p = resolve_hedge_arm_policy(
        complexity="moderate", route="background", is_read_only=False,
        target_files=("backend/foo.py",), repo_root=None,
    )
    assert p.prefer_fast is True
    assert p.defer_stable is False        # eager-buffer mode preserved as fallback


def test_never_raises_on_garbage(monkeypatch):
    p = resolve_hedge_arm_policy(
        complexity=None, route=None, is_read_only=None,  # type: ignore[arg-type]
        target_files=None, repo_root=123,  # type: ignore[arg-type]
    )
    assert isinstance(p, HedgeArmPolicy)  # fail-soft, never raises
```

- [ ] **Step 2: Run test — verify it fails** (`python3 -m pytest tests/governance/test_hedge_arm_policy.py -v` → ImportError)

- [ ] **Step 3: Implement in `dw_transport_hedge.py`** (append; add `from dataclasses import dataclass` and `from typing import Tuple` to the existing imports)

```python
def hedge_policy_resolver_enabled() -> bool:
    """Master for the dynamic arm-policy resolver (supersedes the inline
    complexity-only s227 predicate). Default TRUE. OFF -> byte-identical
    legacy s227 behavior. NEVER raises."""
    return os.environ.get(
        "JARVIS_HEDGE_POLICY_RESOLVER_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def hedge_defer_stable_enabled() -> bool:
    """Master for deferred-ignition of the stable arm when the fast arm is
    prioritized (structural double-billing kill). Default TRUE. OFF -> the
    s227 eager-buffer mode. NEVER raises."""
    return os.environ.get(
        "JARVIS_HEDGE_DEFER_STABLE_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class HedgeArmPolicy:
    """Resolved per-op hedge routing decision. prefer_fast: hold/defer the
    stable arm so the tool-loop RT arm gets the slot. defer_stable: do not
    even FIRE the stable arm unless RT terminally fails (zero double-spend).
    reason: telemetry string for the decision audit trail."""

    prefer_fast: bool
    defer_stable: bool
    reason: str


def resolve_hedge_arm_policy(
    *,
    complexity: str,
    route: str,
    is_read_only: bool,
    target_files: "Tuple[str, ...]",
    repo_root: "Optional[str]",
) -> HedgeArmPolicy:
    """Dynamic hedge arm policy: workload complexity + write-intent +
    target-stratification metrics at runtime. Zero hardcoded routing.
    NEVER raises -- any error degrades to the legacy s227 decision."""
    def _legacy() -> HedgeArmPolicy:
        try:
            from backend.core.ouroboros.governance.exploration_engine import (
                exploration_gate_demands_tools,
            )
            pf = exploration_gate_demands_tools(str(complexity or ""))
        except Exception:  # noqa: BLE001
            pf = False
        return HedgeArmPolicy(prefer_fast=pf, defer_stable=False, reason="legacy_s227")

    try:
        if not hedge_policy_resolver_enabled():
            return _legacy()
        from backend.core.ouroboros.governance.exploration_engine import (
            exploration_gate_demands_tools,
        )
        reason_parts = []
        targets = tuple(target_files or ())
        write_intent = (not bool(is_read_only)) and bool(targets)
        if write_intent:
            # Fail-SAFE toward the tool arm for mutations: an unknown/empty
            # complexity must not starve a write op of exploration (the
            # exact fail-closed hole behind "hedge WON by batch").
            reason_parts.append("write_intent")
        if exploration_gate_demands_tools(str(complexity or "")):
            reason_parts.append("gate_demands")
        # Stratification refinement -- consulted ONLY when the Tier-4 index
        # is already warm (never triggers a build on the dispatch hot path).
        if write_intent and repo_root:
            try:
                from pathlib import Path
                from backend.core.ouroboros.governance.target_stratification import (
                    coverage_index_ready,
                    file_has_test_coverage,
                )
                if coverage_index_ready(repo_root) and not file_has_test_coverage(
                    targets[0], Path(repo_root),
                ):
                    reason_parts.append("uncovered_target")
            except Exception:  # noqa: BLE001 -- refinement only, never decisive
                pass
        prefer_fast = bool(reason_parts)
        defer = prefer_fast and hedge_defer_stable_enabled()
        return HedgeArmPolicy(
            prefer_fast=prefer_fast,
            defer_stable=defer,
            reason="+".join(reason_parts) if reason_parts else "no_signal",
        )
    except Exception:  # noqa: BLE001 -- fail-soft to legacy, never raise
        p = _legacy()
        return HedgeArmPolicy(p.prefer_fast, False, "fail_soft_legacy")
```

- [ ] **Step 4: Run test — verify PASS** (6 passed)
- [ ] **Step 5: Commit** — `git add backend/core/ouroboros/governance/dw_transport_hedge.py tests/governance/test_hedge_arm_policy.py && git commit -m "feat(hedge): dynamic HedgeArmPolicy resolver -- write-intent + stratification-aware, fail-safe toward the tool arm"`

---

## Task 2: Deferred-ignition stable arm in `hedged_race`

**Files:**
- Modify: `backend/core/ouroboros/governance/dw_transport_hedge.py` (`hedged_race`, lines 68-204)
- Test: `tests/governance/test_hedged_race_defer.py` (new)

**Interfaces:**
- Produces: `hedged_race(..., prefer_fast: bool = False, defer_stable: bool = False)` — new kwarg, default False = byte-identical legacy (both eager modes preserved).

**Semantics (the root fix for hole #3):** when `defer_stable=True`: only `t_fast` is created at race start. If fast SUCCEEDS → claim it; the stable thunk was **never called** (zero batch spend, structurally impossible to double-bill). If fast terminally FAILS (rupture or any exception) → ignite `t_stable = loop.create_task(stable())` at that moment (event-driven, not timed) and continue the same wait loop; stable's success is claimed normally, its failure raises via the existing abandoned path with both arms' exceptions. No sleeps, no padding — the deferred arm inherits the op's remaining deadline exactly like the pre-existing sequential RT→batch fallback (`doubleword_provider.py:3065+`), which is the established latency profile for this failure class.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_hedged_race_defer.py
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.dw_transport_hedge import hedged_race

pytestmark = pytest.mark.asyncio


class Boom(RuntimeError):
    pass


async def test_defer_stable_never_fires_batch_when_fast_wins():
    fired = {"stable": 0}

    async def fast():
        return "rt-result"

    async def stable():
        fired["stable"] += 1
        return "batch-result"

    out = await hedged_race(fast, stable, prefer_fast=True, defer_stable=True)
    assert out == "rt-result"
    await asyncio.sleep(0)  # drain any stray scheduling
    assert fired["stable"] == 0  # STRUCTURAL: batch never ignited -> zero double-spend


async def test_defer_stable_ignites_on_fast_failure_and_wins():
    fired = {"stable": 0}

    async def fast():
        raise Boom("rt ruptured")

    async def stable():
        fired["stable"] += 1
        return "batch-result"

    out = await hedged_race(
        fast, stable, prefer_fast=True, defer_stable=True,
        is_rupture=lambda e: isinstance(e, Boom),
    )
    assert out == "batch-result"
    assert fired["stable"] == 1  # ignited exactly once, event-driven


async def test_defer_stable_both_fail_raises_and_reports_abandoned():
    seen = {}

    async def fast():
        raise Boom("rt dead")

    async def stable():
        raise ValueError("batch dead")

    def on_abandoned(fe, se):
        seen["fast"], seen["stable"] = fe, se

    with pytest.raises(ValueError):
        await hedged_race(
            fast, stable, prefer_fast=True, defer_stable=True,
            is_rupture=lambda e: isinstance(e, Boom),
            on_abandoned=on_abandoned,
        )
    assert isinstance(seen["fast"], Boom)
    assert isinstance(seen["stable"], ValueError)


async def test_legacy_mode_unchanged_first_completed_wins():
    async def fast():
        await asyncio.sleep(0.05)
        return "rt"

    async def stable():
        return "batch"

    out = await hedged_race(fast, stable)  # defer_stable default False
    assert out == "batch"  # legacy FIRST_COMPLETED byte-identical


async def test_eager_prefer_fast_buffer_mode_still_works():
    async def fast():
        await asyncio.sleep(0.05)
        return "rt"

    async def stable():
        return "batch"

    out = await hedged_race(fast, stable, prefer_fast=True, defer_stable=False)
    assert out == "rt"  # batch buffered, RT success supersedes (s227 preserved)
```

- [ ] **Step 2: Run — verify fails** (TypeError: unexpected keyword `defer_stable`)

- [ ] **Step 3: Implement.** Modify `hedged_race`: add `defer_stable: bool = False` kwarg; replace lines 105-108 and the fast-failure branch. Exact changes (the rest of the function body is unchanged):

```python
    loop = asyncio.get_event_loop()
    t_fast = loop.create_task(fast())
    # Deferred-ignition (mandate #4): when the RT/tool arm is prioritized,
    # the stable arm is NOT fired at race start -- it ignites event-driven
    # on RT terminal failure only. Structurally zero double-spend; the
    # deferred arm inherits the remaining op deadline (same profile as the
    # legacy sequential RT->batch fallback). No sleeps, no padding.
    _defer = bool(defer_stable and prefer_fast)
    t_stable: "Optional[asyncio.Task]" = (
        None if _defer else loop.create_task(stable())
    )
    pending = {t_fast} if t_stable is None else {t_fast, t_stable}
```

In the fast-exception branch (after `fast_ruptured` is set, replacing the buffered-stable claim block at lines 158-161):

```python
                        if buffered_stable is not _UNSET:
                            return await _claim(buffered_stable, stable_label)
                        if _defer and t_stable is None:
                            # IGNITE the deferred stable arm now -- the RT arm
                            # terminally failed; this is the rupture fallback
                            # the hedge exists for (event-driven, not timed).
                            t_stable = loop.create_task(stable())
                            pending = pending | {t_stable}
                        # otherwise wait for the stable arm (still pending)
                        continue
```

Guard the identity checks: `t is t_fast` stays; `t is t_stable` comparisons must tolerate `t_stable is None` (they already do — `t is t_stable` is False when t_stable is None). The `finally` block becomes:

```python
    finally:
        for t in (t_fast, t_stable):
            if t is not None and not t.done():
                t.cancel()
```

Also update the `while pending:` loop: after `done, pending = await asyncio.wait(...)`, `pending` reassignment already handles the ignited-arm set union (the union happens inside the for-loop body before `continue`, so the outer `while pending:` re-enters with the new task). Verify with the tests — if the union is lost because `pending` was reassigned by `asyncio.wait` before ignition, restructure minimally: track ignition in a local and re-add before the loop re-check (the tests in Step 1 catch this exactly — `test_defer_stable_ignites_on_fast_failure_and_wins` hangs/fails if the ignited task never enters the wait set).

- [ ] **Step 4: Run new tests + the existing hedge suite** — `python3 -m pytest tests/governance/test_hedged_race_defer.py tests/governance/ -k "hedge" -v` → all pass (legacy byte-identical proven by `test_legacy_mode_unchanged_first_completed_wins` + any pre-existing hedge tests).
- [ ] **Step 5: Commit** — `git commit -m "feat(hedge): deferred-ignition stable arm -- batch never fires unless RT terminally fails (zero double-spend)"`

---

## Task 3: Write-intent lift of the route-based Venom suppression

**Files:**
- Modify: `backend/core/ouroboros/governance/exploration_engine.py` (`compute_tool_loop_suppressed`)
- Test: `tests/governance/test_venom_write_intent_lift.py` (new)

**Interfaces:**
- Produces: `compute_tool_loop_suppressed(..., is_read_only: bool = True)` — new keyword-only param, default `True` = byte-identical legacy (existing callers unchanged until Task 4 threads the real value).
- New env: `JARVIS_VENOM_WRITE_INTENT_LIFT_ENABLED` (default `true`).

**Semantics (the root fix for hole #2):** a BACKGROUND-route op with write intent (`not is_read_only`) and gate-demanding complexity gets the ROUTE-based skip lifted (tools ON) — a mutation op cannot explore-and-write blind; the preload-credit path was a gate-pass workaround, not an execution strategy. SPECULATIVE keeps its skip unconditionally (fire-and-forget precompute never mutates). Read-only BACKGROUND keeps the legacy preload-credit path (cheap). The complexity-based skip and L2 repair skip are untouched. Kill switch reverts byte-identical.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_venom_write_intent_lift.py
from __future__ import annotations

from backend.core.ouroboros.governance.exploration_engine import (
    compute_tool_loop_suppressed,
)


def _base(**over):
    kw = dict(
        complexity="moderate", route="background",
        is_bg_terminal_worker=False, has_repair_context=False,
        gate_enabled=True,
    )
    kw.update(over)
    return kw


def test_background_write_intent_gets_tools(monkeypatch):
    monkeypatch.delenv("JARVIS_VENOM_WRITE_INTENT_LIFT_ENABLED", raising=False)
    assert compute_tool_loop_suppressed(**_base(), is_read_only=False) is False


def test_background_read_only_keeps_preload_credit_skip():
    assert compute_tool_loop_suppressed(**_base(), is_read_only=True) is True


def test_speculative_write_intent_still_skipped():
    assert compute_tool_loop_suppressed(
        **_base(route="speculative"), is_read_only=False,
    ) is True


def test_kill_switch_reverts_to_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_WRITE_INTENT_LIFT_ENABLED", "false")
    assert compute_tool_loop_suppressed(**_base(), is_read_only=False) is True


def test_default_param_is_byte_identical_legacy():
    # No is_read_only passed -> defaults True -> legacy decision for all callers
    assert compute_tool_loop_suppressed(**_base()) is True


def test_trivial_write_intent_stays_skipped():
    # Complexity skip is untouched -- the lift is route-skip-only
    assert compute_tool_loop_suppressed(
        **_base(complexity="trivial"), is_read_only=False,
    ) is True
```

- [ ] **Step 2: Run — verify fails** (TypeError: unexpected keyword `is_read_only`)

- [ ] **Step 3: Implement.** In `compute_tool_loop_suppressed`: add keyword-only `is_read_only: bool = True` to the signature and docstring. Inside the existing `try` body, after `route_skip` is computed and before the final `suppressed` composition, add:

```python
        # Write-intent lift (2026-07-04): a BACKGROUND op that intends a real
        # mutation (not is_read_only) and faces a gate-demanding complexity
        # MUST keep the Venom tool loop -- generating a mutation blind behind
        # preload-credit is the empty-mutation machine (53 read-only sessions).
        # SPECULATIVE keeps its skip (fire-and-forget precompute never mutates).
        # Kill switch reverts byte-identical.
        _lift_on = os.environ.get(
            "JARVIS_VENOM_WRITE_INTENT_LIFT_ENABLED", "true",
        ).strip().lower() in ("1", "true", "yes", "on")
        if (
            _lift_on
            and route_skip
            and r == "background"
            and not is_read_only
            and exploration_gate_demands_tools(c, gate_enabled=gate_enabled)
        ):
            route_skip = False
```

(Adapt variable names to the function's actual locals — the implementer reads the full function first; `r`, `c`, `route_skip` are the names visible in the current source. The composition below the insertion point then uses the lifted `route_skip` naturally.)

- [ ] **Step 4: Run new tests + existing exploration-engine suite** — `python3 -m pytest tests/governance/test_venom_write_intent_lift.py tests/governance/ -k "exploration or slice226" -v` → all pass.
- [ ] **Step 5: Commit** — `git commit -m "feat(venom): write-intent lift of the BACKGROUND route skip -- mutation ops keep the tool loop"`

---

## Task 4: Wire the DW provider — resolver + defer + `is_read_only` threading + telemetry

**Files:**
- Modify: `backend/core/ouroboros/governance/doubleword_provider.py` (the s227 inline block, lines 3019-3063; and the `compute_tool_loop_suppressed` call site(s) — find via `grep -n "compute_tool_loop_suppressed" doubleword_provider.py`)
- Test: `tests/governance/test_hedge_governor_wiring.py` (new)

**Interfaces:**
- Consumes: `resolve_hedge_arm_policy` + `HedgeArmPolicy` (Task 1), `hedged_race(..., defer_stable=)` (Task 2), `compute_tool_loop_suppressed(..., is_read_only=)` (Task 3).

**Changes:**
1. Replace the inline `_s227_prefer_fast` computation (lines 3030-3040) with:

```python
                from backend.core.ouroboros.governance.dw_transport_hedge import (
                    resolve_hedge_arm_policy as _policy_resolve,
                )
                _arm_policy = _policy_resolve(
                    complexity=str(getattr(context, "task_complexity", "") or ""),
                    route=str(getattr(context, "provider_route", "") or ""),
                    is_read_only=bool(getattr(context, "is_read_only", False)),
                    target_files=tuple(getattr(context, "target_files", ()) or ()),
                    repo_root=str(getattr(context, "repo_root", "") or "") or None,
                )
                _s227_prefer_fast = _arm_policy.prefer_fast
```

(Keep the existing `_s227_governor_on()` master as the outer gate — resolver-off is handled inside `resolve_hedge_arm_policy` itself, and `hedge_gate_aware_enabled()` off forces `_s227_prefer_fast = False` exactly as today. Verify `repo_root` exists on the context via `grep -n "repo_root" doubleword_provider.py`; if the context does not carry it, pass `None` — the stratification refinement simply stays dormant.)

2. Update the INFO log to include `_arm_policy.reason` and `defer=` state.
3. Pass `defer_stable=_arm_policy.defer_stable` to the `hedged_race` call (line 3051-3064).
4. Thread `is_read_only` into every `compute_tool_loop_suppressed` call site in this file: `is_read_only=bool(getattr(context, "is_read_only", False))` (find call sites first; there is at least one in `_generate_realtime`).
5. Telemetry: inside the existing `_s190_hedge_outcome`, nothing changes (winner labels unchanged). Add one `record_hedge_dispatch`-adjacent structured log where the policy resolves: `logger.info("[Cortex] hedge policy op=%s prefer_fast=%s defer=%s reason=%s", ...)` — reuse the existing logger; no new telemetry system (DRY).

- [ ] **Step 1: Write the failing test** (unit-level, faking the context and the two arms — no network):

```python
# tests/governance/test_hedge_governor_wiring.py
from __future__ import annotations

from types import SimpleNamespace

from backend.core.ouroboros.governance.dw_transport_hedge import (
    resolve_hedge_arm_policy,
)


def _ctx(**over):
    base = dict(
        op_id="op-test", task_complexity="", provider_route="background",
        is_read_only=False, target_files=("backend/foo.py",), repo_root=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_a1_shape_op_resolves_to_rt_priority_with_defer():
    """The exact A1 failure shape: BACKGROUND write-intent op, complexity
    unset. Pre-fix: prefer_fast=False -> batch preempts -> 0 TOOL OUTPUT.
    Post-fix: RT priority + deferred batch."""
    c = _ctx()
    p = resolve_hedge_arm_policy(
        complexity=str(getattr(c, "task_complexity", "") or ""),
        route=str(getattr(c, "provider_route", "") or ""),
        is_read_only=bool(getattr(c, "is_read_only", False)),
        target_files=tuple(getattr(c, "target_files", ()) or ()),
        repo_root=getattr(c, "repo_root", None),
    )
    assert p.prefer_fast is True
    assert p.defer_stable is True


def test_read_only_bg_op_keeps_legacy_batch_speed():
    c = _ctx(is_read_only=True, target_files=())
    p = resolve_hedge_arm_policy(
        complexity="", route="background", is_read_only=True,
        target_files=(), repo_root=None,
    )
    assert p.prefer_fast is False
```

Plus a source-level wiring assertion (the wired-not-inert guard — the pattern that has bitten this repo 3x):

```python
def test_provider_call_site_passes_defer_stable():
    import inspect
    from backend.core.ouroboros.governance import doubleword_provider as dw
    src = inspect.getsource(dw)
    assert "resolve_hedge_arm_policy" in src, "resolver not wired into the provider"
    assert "defer_stable=_arm_policy.defer_stable" in src, "defer not threaded to hedged_race"
    assert src.count("is_read_only=bool(getattr(context") >= 2, (
        "is_read_only not threaded into compute_tool_loop_suppressed call sites"
    )
```

- [ ] **Step 2: Run — verify the wiring assertions fail** (resolver not yet referenced in provider source).
- [ ] **Step 3: Implement changes 1-5 above.**
- [ ] **Step 4: Run** — `python3 -m pytest tests/governance/test_hedge_governor_wiring.py -v` then the provider's own suite: `python3 -m pytest tests/governance/ -k "hedge or transport_hedge or slice227 or slice226" -v` → all pass.
- [ ] **Step 5: Commit** — `git commit -m "feat(dw): wire HedgeArmPolicy resolver + deferred batch + is_read_only threading into the dispatch path"`

---

## Task 5: Race-lifecycle integration matrix + ledger

**Files:**
- Test: `tests/governance/test_hedge_a1_lifecycle.py` (new)
- Modify: `.superpowers/sdd/progress.md` (ledger entry)

**The load-bearing matrix** (all through the REAL `hedged_race`, fake arms, no network):

- [ ] **Step 1: Write the matrix test**

```python
# tests/governance/test_hedge_a1_lifecycle.py
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.dw_transport_hedge import (
    hedged_race,
    resolve_hedge_arm_policy,
)

pytestmark = pytest.mark.asyncio


async def test_full_a1_path_rt_tool_arm_wins_batch_never_billed():
    """A1 write-intent op end-to-end at the race layer: policy resolves RT-
    priority+defer; RT (tool-loop) arm succeeds; batch arm NEVER fires."""
    p = resolve_hedge_arm_policy(
        complexity="moderate", route="background", is_read_only=False,
        target_files=("backend/foo.py",), repo_root=None,
    )
    assert (p.prefer_fast, p.defer_stable) == (True, True)
    billed = {"batch": 0}

    async def rt_arm():
        await asyncio.sleep(0.01)  # tool rounds take time
        return {"content": "patch", "tool_calls": 3}

    async def batch_arm():
        billed["batch"] += 1
        return {"content": "blind-patch", "tool_calls": 0}

    out = await hedged_race(
        rt_arm, batch_arm,
        prefer_fast=p.prefer_fast, defer_stable=p.defer_stable,
    )
    assert out["tool_calls"] == 3          # the tool-loop candidate won
    assert billed["batch"] == 0            # transactional isolation: single arm billed


async def test_rupture_fallback_preserved_with_defer():
    class Rupture(RuntimeError):
        pass

    async def rt_arm():
        raise Rupture("stream severed")

    async def batch_arm():
        return {"content": "stable", "tool_calls": 0}

    out = await hedged_race(
        rt_arm, batch_arm, prefer_fast=True, defer_stable=True,
        is_rupture=lambda e: isinstance(e, Rupture),
    )
    assert out["content"] == "stable"      # hedge's raison d'etre intact


async def test_read_only_op_keeps_legacy_concurrent_race():
    p = resolve_hedge_arm_policy(
        complexity="trivial", route="background", is_read_only=True,
        target_files=(), repo_root=None,
    )
    assert (p.prefer_fast, p.defer_stable) == (False, False)

    async def rt_arm():
        await asyncio.sleep(0.05)
        return "rt"

    async def batch_arm():
        return "batch"

    out = await hedged_race(rt_arm, batch_arm, prefer_fast=p.prefer_fast,
                            defer_stable=p.defer_stable)
    assert out == "batch"                  # cheap reflex ops keep the fast batch win
```

- [ ] **Step 2: Run full governance hedge surface** — `python3 -m pytest tests/governance/test_hedge_arm_policy.py tests/governance/test_hedged_race_defer.py tests/governance/test_venom_write_intent_lift.py tests/governance/test_hedge_governor_wiring.py tests/governance/test_hedge_a1_lifecycle.py -v` → all pass; then the broad regression sweep: `python3 -m pytest tests/governance/ -k "hedge or venom or exploration or slice226 or slice227 or slice188" -q`.
- [ ] **Step 3: Ledger + commit** — append to `.superpowers/sdd/progress.md`: hedge-governor defect CLOSED (3 holes: predicate/route-skip/double-billing), commits listed, next = live A1 validation ignition watching `TOOL OUTPUT BEGIN` + `hedge policy ... reason=write_intent` + `files_changed>0`. `git commit -m "test(hedge): A1 race-lifecycle matrix -- RT tool arm wins, batch never billed, rupture fallback intact"`

---

## Post-merge live validation (not a plan task — the A1 ignition itself)

After merge, the proof is the quiet-loop validation run (per the campaign ledger): `nohup env JARVIS_COGNITIVE_PERSISTENCE_ENABLED=true ... python3 scripts/ignite_a1_soak.py --max-wall-seconds 5000 ... & disown`, watching for: `[Cortex] hedge policy ... prefer_fast=True defer=True reason=write_intent`, then `TOOL OUTPUT BEGIN` (the never-yet-seen signal), then `files_changed>0` + AutoCommit. That run is the Level-4 clinch — dispatched separately on your go.

## Self-Review

1. **Spec coverage:** Mandate 1 (root-cause, no padding/sleeps) → Tasks 1-3 fix the predicate, the route skip, and the eager double-run; deferral is event-driven. Mandate 2 (dynamic, complexity+stratification at runtime, zero hardcoded routing) → Task 1 resolver, env-tunable, stratification consulted when warm. Mandate 3 (DRY: existing hedge infra, no duplicated handlers) → `hedged_race` extended in place; `_generate_realtime`/`_generate_via_batch` untouched; existing telemetry reused. Mandate 4 (bulletproof isolation, no double-billing) → Task 2 structural (batch not created unless RT fails) + Task 5 matrix proof.
2. **Placeholder scan:** none; Task 4's grep-first instructions are lookup steps with exact commands.
3. **Type consistency:** `HedgeArmPolicy` fields, `resolve_hedge_arm_policy` kwargs, `defer_stable=` kwarg, and `is_read_only=` kwarg are used with identical names across Tasks 1-5.

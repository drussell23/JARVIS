# Slice 231 — Telemetry-Driven Budget Synthesizer (Dynamic Allocation Kernel)

**Date:** 2026-06-12
**Branch:** `topology/slice-231-budget-synthesis`
**Author:** Derek J. Russell (design via Claude)
**Manifesto principles:** §5 (intelligence-driven routing), §7 (absolute observability), Zero-shortcut mandate

> Numbering note: "Slice 229" is already taken (route elevation, `bdf022d91a`). This
> work is logged as **Slice 231** to keep the ledger collision-free. 230/230b are committed.

---

## 1. Problem (root cause, verified in code)

A roadmap goal with `priority: critical` routes to **IMMEDIATE**, whose budget profile is a
**static constant** (`urgency_router.py:617-622`):

```python
if route is ProviderRoute.IMMEDIATE:
    return {"tier0_fraction": 0.0, "tier1_reserve_s": 0.0, "max_dw_wait_s": 0.0}
```

DoubleWord (DW) — the **funded primary provider** — is structurally allocated **zero** budget.
The route assumes Claude (the premium fallback tier) is always available. When Claude is
**economically unavailable** (out of credits → 402 → breaker OPEN), two failures compound:

1. **No pre-dispatch awareness.** The route decision is blind to live provider state, so it
   keeps selecting a Claude-direct profile against a dead lane.
2. **The reactive corrective is itself starved.** A Slice 127 P2.1 gate
   (`candidate_generator.py:4035-4061`) reroutes IMMEDIATE→DW — but only *after* the breaker
   has already tripped, and when it fires it calls DW carrying the IMMEDIATE budget that
   allocated DW `max_dw_wait_s: 0.0`. DW inherits a blown deadline →
   `deadline_exhausted_pre_fallback`, observed ~23× in the live soak.

**Net:** the 7 slices that hardened the DW agentic path (A1, 225–230b) are never *reached*,
because the op dies at budget allocation before dispatch.

**The fix is not another corrective patch.** It is to make budget allocation a **function of
live provider availability**, decided at the ROUTE phase before dispatch — eliminating the
static lookup table that blinds routing to infrastructure reality.

---

## 2. Goals / Non-goals

**Goals**
- Eliminate the hardcoded `{0.0, 0.0, 0.0}` IMMEDIATE budget as a *blind* constant; make it the
  output of a deterministic synthesis over a live provider-availability snapshot.
- When Claude is unavailable and DW is healthy, IMMEDIATE ops receive a **DW-primary, fully
  funded** execution window — and, for ops that demand the agentic tool loop, the **timeout
  class is lifted** (60s reflex → up to the COMPLEX 180s window) so tool-loop work isn't starved.
- Self-heal: when Claude is funded/available again, IMMEDIATE returns to Claude-direct fast reflex
  with **zero residual change** (byte-identical to legacy).
- Generalize beyond IMMEDIATE: any route's reserve/cap can adapt to a dead fallback lane.

**Non-goals**
- No new provider-health *sensing* infrastructure. We **consume** existing state
  (`claude_circuit_breaker`, `dw_surface_health`, Slice 22 `JARVIS_PROVIDER_CLAUDE_DISABLED`).
- No change to dispatch/cascade mechanics. The existing Slice 127 P2.1 reroute stays; it simply
  now receives a funded budget and fires proactively because the budget already favors DW.
- No LLM, no network, no I/O in the synthesis path. Pure, deterministic, sub-millisecond.

---

## 3. Design

### 3.1 Units & boundaries

Two small, independently testable units plus two wiring points:

**Unit A — `provider_availability.py` (NEW)**
- `ProviderAvailabilitySnapshot` (frozen dataclass): immutable read-only view.
  - `claude_available: bool`
  - `claude_reason: str` — `"closed"` | `"breaker_open_economic"` | `"breaker_open_transport"` | `"half_open_probing"` | `"structurally_disabled"`
  - `dw_healthy: bool`
  - `dw_reason: str` — `"healthy"` | `"transport_degraded"` | `"upstream_degraded"` | `"unknown"`
- `collect_provider_availability() -> ProviderAvailabilitySnapshot`
  - Reads `get_claude_circuit_breaker().state` (`CircuitState.CLOSED` → available; `OPEN`/`HALF_OPEN` → unavailable) honoring `is_enabled()`; folds in Slice 22 `JARVIS_PROVIDER_CLAUDE_DISABLED`.
  - Reads `SurfaceHealthLedger.verdict_for(SurfaceKind.DIRECT_STREAMING)` (HEALTHY/UPSTREAM_DEGRADED → usable; TRANSPORT_DEGRADED → degraded).
  - **Fail-soft invariant:** any exception → conservative default `claude_available=True, dw_healthy=True` (i.e. preserve legacy behavior; never let a sensing bug starve dispatch).
  - **Purity invariant:** read-only. MUST NOT call `should_allow_request()` (consumes a HALF_OPEN probe slot — the exact Slice 162 bug) or any state-mutating method.

**Unit B — `urgency_router.route_budget_profile()` (EXTENDED, backward compatible)**
- New signature: `route_budget_profile(route, snapshot=None, *, tool_loop_demanded=False) -> Dict[str, float]`
- `snapshot is None` **→ exact legacy static table, byte-identical** (the OFF path / all existing callers untouched).
- `snapshot is not None` and master flag ON → delegate to `synthesize_budget_profile(route, snapshot, tool_loop_demanded)`:

  | Condition | Synthesized profile |
  |---|---|
  | IMMEDIATE, Claude available | `{0.0, 0.0, 0.0}` (legacy Claude-direct reflex — self-heal) |
  | IMMEDIATE, Claude unavailable, DW healthy | `{tier0_fraction: 1.0, tier1_reserve_s: 0.0, max_dw_wait_s: ceiling}` where `ceiling = 180.0 if tool_loop_demanded else 60.0` |
  | IMMEDIATE, Claude unavailable, DW degraded | DW-primary but `max_dw_wait_s` clamped to a degraded floor (e.g. 60s) — try DW, don't over-commit |
  | STANDARD/COMPLEX, Claude unavailable | reuse legacy profile but `tier1_reserve_s → 0.0`, fold reserved seconds into `max_dw_wait_s` |
  | Any route, Claude available | legacy profile unchanged |

  `WIRING_VALIDATION`, `BACKGROUND`, `SPECULATIVE` → legacy profile unchanged (DW-only or fixture
  routes; nothing to adapt).

### 3.2 Tool-loop predicate (timeout-class lift)

`tool_loop_demanded` is computed at the call site from the **existing** Slice 229 predicate
`exploration_engine.exploration_gate_demands_tools(task_complexity)`. `task_complexity` is known
at ROUTE phase (stamped at CLASSIFY), so no new context plumbing is required. This is what lifts
a critical, agentic op from the 60s reflex window to the 180s COMPLEX window on the DW lane.

### 3.3 Wiring points

At the two live ROUTE-phase stamp sites, build the snapshot once and pass it + the predicate:

- `phase_runners/route_runner.py:256` (canonical ROUTE phase runner)
- `orchestrator.py:3207` (orchestrator inline stamp)

```python
_snap = collect_provider_availability() if budget_synthesis_enabled() else None
_tld = exploration_gate_demands_tools(str(getattr(ctx, "task_complexity", "")))
budget_profile = _UR.route_budget_profile(_provider_route, _snap, tool_loop_demanded=_tld)
```

`risk_command_preview.py` (cost estimate) keeps the 1-arg call → legacy profile (estimates stay
stable; not a dispatch path). `orchestrator 2.py` is a non-canonical variant and is left untouched.

### 3.4 Flag & rollback

- Master: `JARVIS_BUDGET_SYNTHESIS_ENABLED`, **default TRUE** (this is the root fix the operator
  wants live). OFF → all callers pass `snapshot=None` → **byte-identical legacy behavior**, proving
  clean rollback.
- The synthesis function is pure and independently unit-tested across the full truth table, so the
  default-TRUE risk is bounded and reversible.

### 3.5 Observability (§7)

Structured log at synthesis when a non-legacy profile is produced:
```
[BudgetSynth] route=IMMEDIATE claude=unavailable:breaker_open_economic dw=healthy \
  tool_loop=True → dw_wait=180.0s tier0=1.0 reserve=0.0 op=<id>
```
No new SSE surface in v1 (YAGNI); the log line is grep-able and sufficient for the soak.

---

## 4. Error handling & invariants

- **Fail-soft sensing:** snapshot collection never raises into the router; exceptions → legacy-safe
  defaults.
- **Purity:** synthesizer + collector are side-effect-free; no breaker mutation, no probe consumption
  (asserted by test that mocks the breaker and verifies no mutating method is called).
- **Watchdog isolation (unchanged):** this touches in-band provider budget only; the out-of-band
  wall-clock watchdog (`harness.py`) remains blind to provider state per the Slice 47 invariant.
- **Determinism:** identical (route, snapshot, tool_loop_demanded) → identical profile.

---

## 5. Testing (TDD — tests written first)

**Unit — `tests/test_ouroboros_governance/test_budget_synthesis.py` (NEW)**
1. `collect_provider_availability`: each (breaker state × dw verdict × structural-disable) → correct
   snapshot fields; exception in any source → legacy-safe defaults; **no mutating breaker method
   called** (purity).
2. `synthesize_budget_profile`: full truth table (route × claude_available × dw_healthy ×
   tool_loop_demanded) → asserted profiles, incl. the 60s→180s lift and STANDARD reserve-fold.
3. Legacy parity: `route_budget_profile(route)` (1-arg) and `route_budget_profile(route, None)`
   return **byte-identical** dicts to the current constants for every route.
4. Self-heal: IMMEDIATE + Claude available → `{0.0, 0.0, 0.0}` regardless of snapshot presence.

**Integration — `tests/governance/test_budget_synthesis_immediate_reroute.py` (NEW)**
5. IMMEDIATE + Claude breaker OPEN (economic) + DW healthy + tool_loop → stamped budget_profile has
   `max_dw_wait_s=180.0`, `tier0_fraction=1.0` → DW receives a funded window (no
   `deadline_exhausted_pre_fallback`).

**Regression / integrity (Phase 4 gate)**
6. Existing `tests/test_ouroboros_governance/test_urgency_router.py` stays green (legacy callers).
7. `tests/governance/test_slice208_epistemic_integrity.py` + antivenom suite stay green
   (no structural-integrity regressions).

---

## 6. Acceptance criteria

- [ ] Master OFF → byte-identical legacy budgets (proven by parity test).
- [ ] IMMEDIATE + Claude unavailable + DW healthy → DW-primary funded window; tool-loop ops get 180s.
- [ ] IMMEDIATE + Claude available → unchanged Claude-direct reflex (self-heal).
- [ ] Collector + synthesizer pure & fail-soft (asserted).
- [ ] Slice 208 / antivenom / urgency_router suites green.
- [ ] New unit + integration tests green.

---

## 7. Files

| File | Change |
|---|---|
| `backend/core/ouroboros/governance/provider_availability.py` | NEW — snapshot + collector |
| `backend/core/ouroboros/governance/urgency_router.py` | EXTEND `route_budget_profile` + flag + `synthesize_budget_profile` |
| `backend/core/ouroboros/governance/phase_runners/route_runner.py` | WIRE snapshot at stamp site |
| `backend/core/ouroboros/governance/orchestrator.py` | WIRE snapshot at stamp site (:3207) |
| `tests/test_ouroboros_governance/test_budget_synthesis.py` | NEW unit |
| `tests/governance/test_budget_synthesis_immediate_reroute.py` | NEW integration |
| `progress.txt` | append graduation ledger line |

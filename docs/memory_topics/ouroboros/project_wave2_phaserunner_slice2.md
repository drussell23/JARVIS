---
title: Wave 2 (5) Slice 2 — Result
modules: [backend/core/ouroboros/governance/phase_runners/classify_runner.py, backend/core/ouroboros/governance/phase_runners/__init__.py, backend/core/ouroboros/governance/orchestrator.py, tests/governance/phase_runner/test_classify_runner_parity.py, test_orchestrator.py]
status: historical
source: project_wave2_phaserunner_slice2.md
---

# Wave 2 (5) Slice 2 — Result

**Status:** implementation complete, parity tests + orchestrator regression green, flag stays **default off** pre-graduation.

## What landed

| Artifact | Path | Notes |
|---|---|---|
| Pilot runner | `backend/core/ouroboros/governance/phase_runners/classify_runner.py` | verbatim transcription of orchestrator.py:1235-1994; constructor takes `(orchestrator, serpent)` |
| Package export | `backend/core/ouroboros/governance/phase_runners/__init__.py` | adds `CLASSIFYRunner` |
| Delegation gate | `backend/core/ouroboros/governance/orchestrator.py` (`_phase_runner_classify_extracted()` helper + if/else wrap at ~line 1245) | default false; inline block wrapped in `else:` branch with +4 indent |
| Parity tests | `tests/governance/phase_runner/test_classify_runner_parity.py` | 22 tests covering 4 exit paths + advisory threading + narrator/dialogue + heartbeat + hash chain + exception swallow invariants |

## Parity test outcomes

**Both paths against both suites — 89/89 green:**

- flag=false: `pytest test_orchestrator.py + phase_runner/` → 89 passed
- flag=true:  `JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED=true JARVIS_PHASE_RUNNER_COMPLETE_EXTRACTED=true pytest ...` → 89 passed

The parity contract (4 exit paths, pinned in test docstring) covers:

1. **Emergency ORANGE+** → `CANCELLED` + `reason=emergency_<level>` (advisory=None in artifacts)
2. **Advisor BLOCK** → `CANCELLED` + `reason=advisor_blocked` (advisory set in artifacts)
3. **Risk BLOCKED** (risk engine or policy engine override) → `CANCELLED` + `reason=<classification.reason_code>` + ledger entry with `OperationState.BLOCKED`
4. **OK** → advance to ROUTE with `risk_tier` stamped + advisory threaded + narrator/dialogue start hooks

## The leak audit — two genuine leaks, threaded via artifacts

Initial analysis spotted only `_advisory` (line 2779 Tier 6 personality voice). Flag-on regression on `test_orchestrator.py` surfaced two more:

- **`_consciousness_bridge`** (lines 3030 + 4513): fragile-file memory injection, both initial path and L2 retry path. Threaded via `artifacts["consciousness_bridge"]`.
- **`risk_tier`** (lines 5497, 5515, 5538, 5628, 5731, 5737, 5809, 5821, 5868, 5985, 6008, 6035, 6067, ...): carried across phases as a function-scoped local, mutated by later phases. Since `advance(ROUTE, risk_tier=...)` stamps it onto ctx, the delegation hook rebinds `risk_tier = ctx.risk_tier` after successful runner return.

All three are documented in-line in the orchestrator hook:

```python
if _phase_runner_classify_extracted():
    ...
    _advisory = _classify_result.artifacts.get("advisory")
    _consciousness_bridge = _classify_result.artifacts.get("consciousness_bridge")
    if _classify_result.next_phase is None:
        return _classify_result.next_ctx
    ctx = _classify_result.next_ctx
    risk_tier = ctx.risk_tier  # rebind function-scoped local
```

## Pre-existing bug preserved verbatim — `phase` double-kwarg TypeError

Inline code at ~line 1868 builds `_chain_payload = {"phase": "intent_chain", ...}` then calls `emit_heartbeat(op_id=..., phase="intent_chain", progress_pct=10.0, **_chain_payload)`. Python raises `TypeError: got multiple values for keyword argument 'phase'`; the surrounding `try/except: pass` silently swallows it. Net effect: the intent_chain heartbeat never actually lands on the comm surface.

The runner preserves this bug verbatim (parity contract requires it). One test explicitly asserts the observable-empty-heartbeats behavior with a loud comment that anyone fixing the double-kwarg bug MUST delete this test AND update the inline path in the same commit.

## Diff size

The `else:` reindent of the 760-line inline block shows in `git diff` as 760 lines removed + 760 lines added. `git blame` on those lines will assign blame to Slice 2, not the original authors. This is the ONE tradeoff of option (a) (reindent) over option (b) (helper-method extraction) — chosen deliberately because (a) keeps the inline block byte-identical to pre-extraction, and graduation will DELETE the else: branch entirely (the scope doc's Slice 6 "dispatcher cutover" explicitly plans this).

## Graduation criteria (per scope doc)

- ✅ Slice 2 runner + parity tests + orchestrator regression green on both paths
- ⬜ Slice 2 stable for **3 clean battle-test sessions** with flag=true
- ⬜ Only then: flip `JARVIS_PHASE_RUNNER_CLASSIFY_EXTRACTED` default false → true in orchestrator helper
- ⬜ Later (post-slice-6 dispatcher cutover): delete the inline 1253-2012 block entirely

## Authority invariant (grep-pinned)

Runner imports `ledger.OperationState`, `op_context.{OperationContext,OperationPhase}`, `phase_runner.{PhaseResult,PhaseRunner}`, `policy_engine.{PolicyDecision,PolicyEngine}`, `risk_engine.RiskTier`. These match the inline CLASSIFY block's imports — **no authority widening**. Test `test_classify_runner_module_bans_execution_authority_imports` grep-pins the ban on `candidate_generator` / `iron_gate` / `change_engine` / `gate`.

## What's NOT changed

- **Zero behavior change** at default env. The delegation branch is skipped; inline block runs verbatim (only the indentation changed).
- No §6 Iron Gate semantics touched
- No §1 execution authority widened
- The pre-existing double-kwarg TypeError bug is preserved exactly
- pyright diagnostics about unresolved imports (`classify_runner`, `phase_runner`) are stale-cache artifacts — runtime imports verified via `python3 -c` and the full test suite passing.

## Next slices (scope doc order)

- Slice 3: **ROUTE + CONTEXT_EXPANSION + PLAN** (~315 lines together; three small phases batched)
- Slice 4: VALIDATE + GATE + APPROVE + APPLY + VERIFY (~2500 lines)
- Slice 5: GENERATE (1926 lines)
- Slice 6: dispatcher cutover (orchestrator becomes a thin registry loop; delete inline blocks)

Each slice gets its own 3-clean-session graduation arc.

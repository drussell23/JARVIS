---
title: Wave 2 (5) Slice 3 — Result
modules: [backend/core/ouroboros/governance/phase_runners/route_runner.py, orchestrator.py, test_orchestrator.py]
status: historical
source: project_wave2_phaserunner_slice3.md
---

# Wave 2 (5) Slice 3 — Result

**Status:** implementation complete, parity green, all three flags default **off**.

## What landed

| Artifact | Path | Notes |
|---|---|---|
| ROUTERunner | `backend/core/ouroboros/governance/phase_runners/route_runner.py` | Verbatim ROUTE body + transition-to-CTX-or-PLAN dispatch |
| ContextExpansionRunner | `phase_runners/context_expansion_runner.py` | Verbatim CTX body; `ContextExpander` resolved through orchestrator module namespace for test-patch compat |
| PLANRunner | `phase_runners/plan_runner.py` | Verbatim PLAN body (~750 lines); 5 terminal paths; advisory arg for Tier 6 personality |
| Helpers + combined gate | `orchestrator.py` | `_phase_runner_route_extracted`, `_phase_runner_context_expansion_extracted`, `_phase_runner_plan_extracted`, `_phase_runner_slice3_fully_extracted` |
| Delegation hook | `orchestrator.py` ~line 2048 | All-or-nothing gate wrapping 965-line inline block in `else:` branch |
| Parity tests | `tests/governance/phase_runner/test_{route,context_expansion,plan}_runner_parity.py` | 29 new tests (13 ROUTE + 5 CTX + 11 PLAN) |

## Parity test outcomes

**Both paths against both suites — 118/118 green:**

- flag=false: `pytest test_orchestrator.py + phase_runner/` → 118 passed
- slice3+slice2+slice1 flags=true: → 118 passed

## The combined-gate design choice

ROUTE, CTX, and PLAN are currently interleaved in the inline pipeline:

```
ROUTE body → if expansion_enabled: advance(CTX) → CTX body → advance(PLAN)
           else: advance(PLAN)
PLAN body → advance(GENERATE)
```

Wiring each runner independently behind its own flag would require splitting the interleaving (advance points + PreActionNarrator position change). Instead, Slice 3 uses a combined `_phase_runner_slice3_fully_extracted()` helper that only engages runners when **all three** per-phase flags are set. Per-phase flags remain visible for env-var discoverability + future per-phase independence once Slice 6 (dispatcher cutover) decouples them entirely.

## Five PLAN terminal paths, all covered by tests

| Reason code | Trigger | Exit state |
|---|---|---|
| `plan_required_unavailable` | JARVIS_SHOW_PLAN_BEFORE_EXECUTE=true + plan skipped/missing | CANCELLED + FAILED ledger |
| `plan_review_unavailable` | review required + approval provider missing OR gate infra fail in strict mode | CANCELLED + FAILED ledger |
| `plan_rejected` | human reviewer REJECTED + session lesson recorded | CANCELLED + FAILED ledger |
| `plan_approval_expired` | strict mode + timeout | EXPIRED + FAILED ledger |
| `user_cancelled` | `_is_cancel_requested` pre-GENERATE | CANCELLED + FAILED ledger |

## Advisory artifact threading (CLASSIFY → PLAN)

PLAN's Tier 6 personality voice line (orchestrator.py ~line 2831) reads `_advisory.chronic_entropy`. PLANRunner accepts `advisory` via constructor arg; the orchestrator hook plumbs it through from the CLASSIFY `artifacts` dict (which is set by CLASSIFYRunner in Slice 2).

## Preserved latent bugs (parity contract)

- **`expanded_files` attribute typo** (line 2220 inline): ctx has `expanded_context_files` not `expanded_files`; the `with_expanded_files` append would AttributeError on this read. The inline code's surrounding `try/except` swallows it. Runner preserves verbatim.
- **`_plan_gate_applied` dead-store** (line 2470/2293 inline): the flag is set but never read. Verbatim preserved.
- **`_fleet_text` unused** (line 2219 inline): formatted but never assigned to ctx. Verbatim preserved.

## Resolvable-through-orchestrator-namespace pattern

`ContextExpansionRunner` imports `ContextExpander` lazily via the orchestrator module (`from backend.core.ouroboros.governance import orchestrator as _orch_mod` then `_orch_mod.ContextExpander`). This ensures test code that patches `orchestrator.ContextExpander` reaches the runner path. Pattern may be needed by future runners when tests target orchestrator module attributes.

## Graduation criteria (per scope doc)

- ✅ All three runners + parity tests + full regression green on both paths
- ⬜ Slice 3 stable for **3 clean battle-test sessions** with all three flags=true
- ⬜ Flip all three defaults false→true together in orchestrator helpers
- ⬜ Post-slice-6: delete the inline ROUTE/CTX/PLAN blocks entirely

## Diff size

Slice 3 reindent: 965-line inline block wrapped in `else:` +4 indent.
+ 3 runner files (~1900 lines total).
+ 3 parity test files (~850 lines).

## Authority invariant (grep-pinned)

All three runners: no imports from `candidate_generator` / `iron_gate` / `change_engine` / `gate`. Test files include `test_*_bans_execution_authority_imports` regression. PLAN imports `approval_provider` + `plan_approval` because the inline block does (approval is read-only routing, not execution authority).

## Next slices (scope doc order)

- Slice 4: VALIDATE + GATE + APPROVE + APPLY + VERIFY (~2500 lines)
- Slice 5: GENERATE (1926 lines; likely sub-extracted)
- Slice 6: dispatcher cutover

Each slice gets its own 3-clean-session graduation arc.

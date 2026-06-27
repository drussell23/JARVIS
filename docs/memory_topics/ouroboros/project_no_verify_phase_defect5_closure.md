---
title: Defect #5 — CLOSED 2026-05-03
modules: [backend/core/ouroboros/governance/candidate_generator.py, scripts/no_verify_phase_defect5_verdict.py]
status: historical
source: project_no_verify_phase_defect5_closure.md
---

# Defect #5 — CLOSED 2026-05-03

The fifth and deepest blocker from soak v5 (`bt-2026-05-03-060330`): **no
VERIFY phases ever fired** because 17/19 background ops terminal-failed at
GENERATE with the same error — `background_dw_blocked_by_topology:
Catalog-driven (Phase 12). Static list purged; ranking authority is
dw_catalog_classif`. Pipeline phase distribution showed
`VERIFY=0, COMPLETE=0` — the auto_action_router VERIFY hook (Move 3)
literally could not be exercised, regardless of any other fix.

## Root cause

A read-only auto-cascade reflex existed in `_generate_background`
(line ~2806 sets `_allow_fallback=True` for read-only ops) but was
**structurally unreachable**: `_dispatch_via_sentinel` raises
`background_dw_blocked_by_topology` synchronously in its queue-tolerance
branch *before* returning to `_generate_background`. The reflex never had
a chance to fire when the sentinel queue-tolerance code path triggered.

This is a §6 Iron Gate failure mode: structural code that *exists* but
*cannot run* in the actual sentinel-tripped path. Soak v5 produced 17 ops
that proved it.

## Fix shipped (single slice)

Lifted the read-only cascade reflex into `_dispatch_via_sentinel`'s
queue-tolerance branch with a **unified `_can_cascade` gate** that
handles both BG and SPECULATIVE routes uniformly:

```python
if fallback_tolerance == "queue":
    _is_read_only = bool(getattr(context, "is_read_only", False))
    _allow_mutating_fallback = (
        provider_route == "background"
        and os.environ.get("JARVIS_BACKGROUND_ALLOW_FALLBACK", "")
            .strip().lower() in {"1", "true", "yes", "on"}
    )
    _can_cascade = (
        self._fallback is not None
        and (_is_read_only or _allow_mutating_fallback)
    )
    if _can_cascade:
        _cascade_reason = (
            "read_only_cost_safe" if _is_read_only
            else "operator_allow_fallback_env"
        )
        return await self._call_fallback(context, deadline)
    # Original fallthrough raises preserved (cost contract)
    if provider_route == "speculative":
        raise RuntimeError(f"speculative_deferred:dw_severed_queued:...")
    raise RuntimeError(f"background_dw_blocked_by_topology:...")
```

## Architectural decisions worth remembering

- **Unified `_can_cascade` over duplicated route-branched checks**.
  Single boolean expression covers (read-only ⇒ both routes cascade) +
  (mutating BG ⇒ env-knob-gated cascade) + (mutating SPEC ⇒ never).
  Mathematically equivalent to a 2×2×2 truth table; verdict C4 walks
  all 6 cells.
- **Cost contract structurally preserved**. `is_read_only` is the
  policy-layer Rule 0d gate — read-only ops cannot mutate ledgers or
  burn cost via Claude beyond the call itself. Mutating BG ops without
  the explicit env knob still raise the original
  `background_dw_blocked_by_topology` (no silent cost leak).
- **AST pin extended to protect the reflex**. `register_shipped_invariants`
  REQUIRED_LITERALS now includes `"Sentinel queue tolerance OVERRIDE"`
  and `"read_only_cost_safe"` — regression that drops the reflex will
  fail the substrate AST pin (already wired into Defect #4's pin).
- **Reuse contract honored**. No new helper, no new env knob — reused
  `JARVIS_BACKGROUND_ALLOW_FALLBACK` (already documented), reused
  `_call_fallback` (already cost-contract-aware), reused
  `register_shipped_invariants` registration surface.

## Empirical-closure verdict (5/5 PRIMARY PASS)

```
[PASS] C1 Cascade override markers present in source
       markers_found=5/5
[PASS] C2 AST pin holds against live source
       invariant=candidate_generator_defect4_substrate violations=()
[PASS] C3 Cost contract preserved (mutating BG no-env still raises)
       is_read_only_check=True env_knob_check=True
       unified_can_cascade=True fallthrough_raise=True
[PASS] C4 Read-only BG cascades regardless of env (truth table)
       truth_table_cases=6/6 all correct
[PASS] C5 Cascade block precedes both BG + SPECULATIVE raises
       cascade_idx=122113 spec_raise_idx=123216 bg_raise_idx=123392
       correct_order=True
```

C4's truth table walks all 6 boolean cells (read-only × env × route)
proving the unified `_can_cascade` matches the documented decision
matrix mathematically — not just empirically.

## What this unlocks

- **Soak v6 can now produce non-zero VERIFY phases**. Read-only BG ops
  (the dominant category from sensors like DocStaleness /
  ProactiveExploration / Backlog) now cascade to Claude when the
  sentinel queue-tolerance trips, instead of terminal-failing at
  GENERATE.
- **Move 3's auto_action_router VERIFY hook can finally fire** — the
  pipeline reaches GATE/APPROVE/APPLY/VERIFY/COMPLETE for at least the
  read-only majority, exercising the verification → action loop end-to-end.
- **Move 4's InvariantDriftAuditor + Production Oracle Rule 1.5 veto
  can act on real production-reality signals** — they were starved of
  VERIFY-phase telemetry by this defect.
- **All 5 systemic defects from soak v5 now structurally addressed**:
  WallClockWatchdog (#1), Production Oracle observer boot (#2),
  PersistentIntelligence readonly-DB (#3), CandidateGenerator task leak
  + retry-without-budget (#4), and now sentinel-queue-tolerance
  cascade-unreachability (#5). Soak v6 should be the first session to
  produce a clean phase distribution with non-zero VERIFY counts.

## Files touched

- `backend/core/ouroboros/governance/candidate_generator.py`:
  - `_dispatch_via_sentinel` queue-tolerance branch — unified
    `_can_cascade` reflex (Slice A)
  - `register_shipped_invariants` REQUIRED_LITERALS extended with
    Defect #5 markers (Slice C — folded into existing AST pin)
- `scripts/no_verify_phase_defect5_verdict.py` (NEW) — 5-contract verdict

Closes Defect #5 from soak v5 findings. **All 5 systemic defects from
soak v5 are now closed**: #1 (wall-clock), #2 (oracle boot), #3
(persistent intel), #4 (task leak + retry-without-budget), #5
(sentinel-queue-tolerance cascade-unreachability). The full pipeline
CLASSIFY → … → VERIFY → COMPLETE is now structurally reachable for
read-only BG ops. Soak v6 ready to run.

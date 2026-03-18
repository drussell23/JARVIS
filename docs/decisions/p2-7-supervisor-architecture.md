# Decision: P2-7 — Control-Plane Supervisor Architecture

**Date:** 2026-03-17
**Status:** ACCEPTED
**Supersedes:** none
**Related:** `docs/plans/2026-03-05-unified-supervisor-hardening-plan.md`

---

## Context

The hardening audit raised a conflict between two views of `unified_supervisor.py`:

| View | Position |
|------|----------|
| Existing plan (2026-03-05) | "without breaking up the file" — monolith is intentional |
| Critique | 96K+ line god-object creates maintenance risk, difficult to isolate failure domains |

Both views are correct. This decision reconciles them.

---

## Decision

**Keep `unified_supervisor.py` as a single file.** Introduce explicit **extension point contracts** at the existing Zone boundaries instead of physical file splits.

### Rationale

**Why NOT split the file:**

1. **Atomic visibility.** The supervisor is the single source of truth for JARVIS system state. Splitting it across modules creates cross-import cycles that are harder to reason about than a large but cohesive file.

2. **Existing Zone discipline.** The file already uses a structured Zone numbering system (Zone 1–10+) that provides logical separation without physical file boundaries.

3. **Test isolation is already achievable.** Subsystems (GCPVMManager, PrimeRouter, GovernedLoopService) are already in separate files. The supervisor is the *wiring layer*, not the *logic layer*.

4. **Breaking the file would invalidate the hardening plan** that has already been partially executed (P0-2 through P1-6), creating unnecessary churn.

**Why NOT keep it completely unaddressed:**

The god-object critique is valid regarding **extension point opacity** — it's not obvious where new subsystems should wire in without reading the full file. This is the real risk, not file size.

---

## Mitigation: Explicit Extension Point Contracts

Instead of splitting the file, we define **four named extension points** with documented contracts:

### EP-1: Boot Wiring (Zone 5.0–5.5)
- **What wires here:** StartupOrchestrator, VerdictAuthority, boot epoch
- **Contract:** Must complete before any async tasks are scheduled
- **Example:** `_startup_orchestrator`, `_verdict_authority`, `_deferred_prober`

### EP-2: Governance Wiring (Zone 6.8–6.9)
- **What wires here:** GovernedLoopService (GLS), IntakeLayerService (ILS)
- **Contract:** GLS and ILS receive `_repo_registry` from each other; no double `from_env()`
- **Example:** `_governed_loop_service.set_repo_registry(_repo_registry)`

### EP-3: Routing Wiring (Zone 7.x)
- **What wires here:** PrimeRouter, hybrid router, boot routing policy
- **Contract:** `wire_boot_routing_policy()` called after orchestrator is created; `set_prime_router()` called after GCP VM manager is available
- **Example:** `_pr_mod.wire_boot_routing_policy(orchestrator._routing_policy)`

### EP-4: Health Monitor Wiring (Zone 8.x)
- **What wires here:** Health monitors, circuit breakers, DMS watchdog
- **Contract:** All health monitors register with `_restart_deduplicator` to prevent re-entrancy storms

---

## Consequences

| Area | Impact |
|------|--------|
| File size | Unchanged (~96K lines) |
| New complexity | Four documented extension point names (none are code changes) |
| Maintenance | Engineers adding new subsystems have clear guidance on where to wire |
| Test isolation | No change — subsystem tests remain in their own files |
| Future | If the file grows beyond 150K lines, revisit physical split — but only after defining clear module boundaries at EP-1 through EP-4 |

---

## Acceptance Criteria

- [ ] This document is committed and linked from the hardening plan
- [ ] Each Zone boundary in `unified_supervisor.py` has a one-line comment referencing its EP contract
- [ ] New subsystems added in P3+ must cite which EP they wire into in their PR description

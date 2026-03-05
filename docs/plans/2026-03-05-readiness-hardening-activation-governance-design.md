# Readiness Hardening + Activation Governance Design

## Overview

**Goal:** Harden GCP readiness semantics (8 items), then build schema-enforced service governance (Wave 0), then activate the first 8 Immune-tier services under promoted-mode contracts (Wave 1).

**Architecture:** Extend-in-place (Approach A+). Extend existing `ComponentDefinition` and `ComponentRegistry` with governance fields. Two-tier validation: legacy (warn-only) vs promoted (fail-fast). No new abstraction layers — builds on proven `ComponentRegistry`, `LifecycleEngine`, and `StartupStateMachine`.

**Sequencing:** Phase 1 (hardening) -> Phase 2 (Wave 0 governance) -> Phase 3 (Wave 1 activation). Each phase has an explicit exit gate.

**Implementation Plan:** `docs/plans/2026-03-05-readiness-hardening-activation-governance-plan.md`

---

## Key Invariants

- **INV-G1:** Promoted services cannot register without complete contracts
- **INV-G2:** One writer per state domain (EXCLUSIVE_WRITE conflict detection)
- **INV-G3:** No upward cross-tier dependencies without explicit allowlist
- **INV-G4:** Kill-switch hierarchy: global > tier > service
- **INV-G5:** Constructor purity: no I/O/network/threads in `__init__` for promoted services
- **INV-G6:** REQUIRED criticality cannot be DEFERRED_AFTER_READY

---

## Data Model

See implementation plan for full enums, dataclasses, and validation rules.

### Kill-Switch Truth Table

| Global | Tier | Service | Result |
|--------|------|---------|--------|
| unset  | unset | unset  | **enabled** |
| false  | any   | any    | **disabled** |
| true   | false | any    | **disabled** |
| true   | true  | false  | **disabled** |
| true   | true  | true/unset | **enabled** |

---

## Phase Summary

| Phase | Scope | Exit Gate |
|-------|-------|-----------|
| Phase 1 | 8 readiness hardening items | No progress->readiness coupling, no flap, no stale-session acceptance |
| Phase 2 | Wave 0 governance infrastructure | Promoted registration fails if contract incomplete |
| Phase 3 | Wave 1 immune-tier activation (8 services) | Soak passes without restart oscillation or event storms |

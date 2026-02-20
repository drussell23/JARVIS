# Enterprise Capability Activation — Design Document

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the enterprise hardening stack into unified_supervisor.py, fix cross-repo integration gaps, and activate sleeping capabilities across all three repos.

**Architecture:** Hybrid integration — enterprise stack runs alongside existing zone-based startup (not replacing it). The supervisor remains the control plane; backend/core modules stay the implementation. ~150-200 lines added to the supervisor, calling into existing modules.

**Tech Stack:** Python 3.9+, asyncio, existing backend/core enterprise modules, Trinity IPC (file-based cross-repo), HTTP health polling

---

## Context

### The Problem
Across the three JARVIS repos:
- **JARVIS-AI-Agent**: 7 enterprise modules completely disconnected from unified_supervisor.py
- **jarvis-prime**: 56 of 69 core modules sleeping (reasoning engine, AGI models, continuous learning, etc.)
- **reactor-core**: 70% of capabilities not wired (curriculum learning, active learning, deployment gate, etc.)

The enterprise hardening stack (`enterprise_supervisor_integration.py` + 7 dependencies) was designed to manage all of this, but it has internal bugs and was never wired in.

### The Disease (Not the Symptom)
The root issues, in priority order:
1. Cross-repo wiring is incomplete (bridges not verified, no orchestration guards)
2. Enterprise stack has internal bugs (API mismatches, wrong import paths, competing DAG systems)
3. No framework exists to activate sleeping capabilities
4. Sleeping capabilities remain dormant

### Design Principles
- Cure the disease, not apply band-aids
- Fix bugs before wiring in buggy code
- Surgical additions to the 82K-line supervisor (call into modules, don't inline logic)
- No unnecessary file duplication
- No hardcoding — env var overrides everywhere
- Async-first, parallel where possible

---

## Phase 1: Enterprise Stack Bug Fixes (Prerequisite)

Fix the enterprise stack so it's internally coherent before touching the supervisor.

### Bug 1: ComponentStatus API Mismatch
**File:** `backend/core/enterprise_hooks.py`
- Uses `ComponentStatus.RUNNING` — doesn't exist (should be `ComponentStatus.HEALTHY`)
- Uses `update_status()` — doesn't exist (should be `mark_status()`)
- Uses `defn.status` — doesn't exist (should use `get_state(component).status`)

### Bug 2: Wrong Import Path
**File:** `backend/core/cross_repo_startup_orchestrator.py`
- Imports `EnterpriseProcessManager` from `backend.supervisor.enterprise_process_manager`
- Actual path: `backend.core.enterprise_process_manager`

### Bug 3: Two Competing DAG Systems
- `unified_supervisor.py` uses `backend.core.startup_state_machine` (772 lines, its own StartupPhase/ComponentStatus/StartupStateMachine)
- Enterprise stack has `startup_dag.py` (165 lines)
- **Decision:** Keep `startup_state_machine` as the supervisor's DAG (it's already wired). Have enterprise stack's `ComponentRegistry` delegate to it for ordering, or operate independently as an overlay that tracks component health without controlling ordering.

---

## Phase 2: Unified Supervisor Additions (~150-200 lines)

### Addition 1: Bridge Completeness Check (30-40 lines)
After `_wait_for_health()` succeeds for jarvis-prime, query `/health` and check bridge fields:
- `bridge_enabled`, `reactor_bridge_enabled`, `jarvis_bridge_enabled`, `neural_routing_enabled`
- Log degraded-state warning if critical bridges missing (not a failure — Prime is still useful)

### Addition 2: NightShift Training Trigger (60-80 lines)
Background task that periodically checks experience count via `ReactorCoreClient` and triggers training when thresholds are met. Replaces manual `POST /api/v1/pipeline/run`.

### Addition 3: Health Contract Enforcement (20-30 lines)
Import `get_health_aggregator()` from `backend.core.health_contracts`. Feed it health responses from Prime, Reactor, and body. Let it determine system-level health.

### Addition 4: Recovery Engine in Trinity Monitor (30-40 lines)
In Trinity health monitor loop, instead of just `_start_component()` on failure, call recovery engine to classify failure and decide action (restart, fallback, skip, escalate).

### Addition 5: Capability Activation After Trinity (20-30 lines)
After Trinity health checks pass, HTTP call to Prime to activate additional capabilities. Requires Prime to expose `POST /api/v1/capabilities/activate` endpoint.

---

## Phase 3: Cross-Repo Integration Fixes

### Fix 1: jarvis_prime_bridge Init (~30 lines in jarvis-prime)
In `run_server.py`'s `_background_init()`, initialize `jarvis_prime_bridge` so unified inference routing works when started by unified supervisor.

### Fix 2: Orchestration Guard (~10 lines each repo)
In jarvis-prime's `run_server.py` and reactor-core's `run_supervisor.py`, check `JARVIS_ORCHESTRATED_BY` env var. If set, skip repo-level supervisor features that conflict with unified_supervisor.py.

### Fix 3: Deprecation Warnings (~5 lines each)
When repo-level supervisors detect they're being orchestrated, log a deprecation notice.

---

## Phase 4: Capability Activation

With enterprise framework in place, register sleeping capabilities:

| Capability | Repo | Priority | Effort |
|-----------|------|----------|--------|
| Reasoning engine + AGI models | jarvis-prime | HIGH | 1 day |
| NightShift pipeline auto-trigger | reactor-core | HIGH | Already done in Phase 2 |
| Curriculum learning | reactor-core | MEDIUM | 1 day |
| Continuous learning | jarvis-prime | MEDIUM | 1 day |
| Active learning | reactor-core | MEDIUM | 1 day |
| Cross-repo health contracts | JARVIS | LOW | Already done in Phase 2 |

---

## Edge Cases & Failure Vectors

### 1. Two DAG Systems Fighting
`startup_state_machine` and `StartupDAG` both want to own ordering. Fix: enterprise stack operates as health/recovery overlay, not ordering controller.

### 2. Recovery Engine vs Existing try/except
1,767 except clauses already handle errors. Fix: recovery engine wraps Trinity-level restarts (not individual try/except blocks).

### 3. ComponentRegistry vs Supervisor Internal State
Multiple state tracking systems. Fix: ComponentRegistry is the overlay authority; existing state mechanisms write to it (not parallel state).

### 4. Enterprise Stack Not Designed for 82K Monolith
Fix: Clean interface (protocol/ABC) that supervisor implements, enterprise stack consumes. ~20-30 lines of integration, not inlined logic.

---

## Execution Order

1. **Enterprise stack bug fixes** (prerequisite, ~1 day)
2. **Unified supervisor additions** (Phase 2, ~2-3 days)
3. **Cross-repo fixes** (Phase 3, ~1 day)
4. **Capability activation** (Phase 4, ~1-2 days per capability)

Total: ~1-2 weeks for full enterprise activation.

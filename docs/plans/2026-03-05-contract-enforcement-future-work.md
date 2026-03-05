# Contract Enforcement — Future Work (Deferred Projects)

**Date:** 2026-03-05
**Parent:** Disease 4 — Advisory Contracts = No Contracts
**Status:** Deferred — separate projects, not in current implementation scope

These items were identified during Disease 4 design as real gaps that require their own design cycles. Each is a standalone project with its own dependencies and risk profile.

---

## 1. Capability Proof Checks (Active Probes)

**Problem:** Endpoints can claim capabilities they can't actually execute. Static `/capabilities` declarations are trust-based.

**What's needed:**
- Define a "capability proof" protocol: supervisor sends a minimal test payload, component returns proof of execution
- Example: Prime claims `inference` capability → supervisor sends a trivial prompt, expects a valid response within timeout
- Proof checks run at CONTRACT_GATE and periodically at runtime
- Failed proof = BLOCK_BEFORE_READY or DEGRADED_ALLOWED depending on capability criticality

**Dependencies:**
- Requires Prime and Reactor to implement proof endpoints (`/lifecycle/prove_capability/{name}`)
- Requires defining what "proof" means per capability (inference = generate, training = accept batch, etc.)
- Must not add significant latency to startup

**Estimated scope:** ~500 lines across 3 files + endpoint changes in Prime/Reactor repos

---

## 2. Port Ownership Lease / Epoch Fencing

**Problem:** Parallel launches can race for the same port. Current detection is "try to bind, fail if taken" which is TOCTOU-vulnerable.

**What's needed:**
- Atomic lease/claim mechanism: write `{port, pid, epoch, timestamp}` to a lockfile before bind attempt
- Epoch fencing: stale claims from dead processes are expired based on PID liveness + timestamp
- Lease renewal during operation (heartbeat)
- Integration with the existing `StartupLock` mechanism

**Dependencies:**
- Requires defining lease file location and format
- Must handle `kill -9` (no cleanup) — epoch + PID liveness check
- Cross-platform considerations (macOS vs Linux)

**Estimated scope:** ~300 lines, new `PortLeaseManager` class

---

## 3. Semantic Contract Tests + Field-Level Compatibility Map

**Problem:** N/N-1 version compatibility says "these versions can talk" but doesn't guarantee behavioral compatibility for every field. A field might exist in both versions but have different semantics.

**What's needed:**
- Field-level compatibility annotations in contract definitions
- Example: `health.ready_for_inference` field exists in v1 and v2 but v2 adds a `warming_up` state that v1 callers don't understand
- Compatibility map: `{field, version_range, breaking_change_description}`
- Validation at CONTRACT_GATE: if remote version is in-window but has known field-level breaks, warn or block

**Dependencies:**
- Requires contract versioning infrastructure with per-field annotations
- Requires maintaining a compatibility database as contracts evolve
- Heavy ongoing maintenance burden — may not be worth the cost until contracts change frequently

**Estimated scope:** ~800 lines, new `SemanticCompatibilityMap` module

---

## 4. Bootstrap Watchdog Separation

**Problem:** The existing DMS (StartupWatchdog) at Zone 5.6 serves as both bootstrap monitor AND orchestration policy enforcer. If the orchestration layer itself is degraded mid-gate, the watchdog can't independently escalate.

**What's needed:**
- Minimal bootstrap watchdog that runs in a separate thread/process
- Only responsibility: detect if boot sequence stalls or the supervisor process itself becomes unresponsive
- Escalation: write diagnostic dump, restart supervisor, or alert
- Must be simpler than the thing it's watching (no dependency on supervisor infrastructure)

**Dependencies:**
- Requires defining "stall" criteria independent of supervisor state
- Must not create a "who watches the watchman" infinite regress
- The existing DMS has graduated escalation and progress-awareness — separating it loses those features

**Estimated scope:** ~400 lines, new `BootstrapWatchdog` class + thread management

---

## 5. Distributed Contract State (Multi-Instance)

**Problem:** Current `ContractStateAuthority` is per-process. In multi-instance deployments, contract violations on one instance aren't visible to others.

**What's needed:**
- Contract violation state published to shared store (Redis, shared filesystem)
- Other instances can query "did any peer fail contract validation?"
- Useful for canary deployments: one instance detects schema break, others avoid rolling forward

**Dependencies:**
- Requires shared state infrastructure (Redis or equivalent)
- Must handle network partitions (can't let shared state unavailability block startup)
- Only relevant when JARVIS runs multi-instance (not current architecture)

**Estimated scope:** ~600 lines, `DistributedContractState` adapter

---

## Priority Ranking

| Project | Impact | Effort | Priority |
|---------|--------|--------|----------|
| Capability Proof Checks | High | Medium | P1 — most impactful gap |
| Port Ownership Lease | Medium | Low | P2 — simple, prevents real race |
| Bootstrap Watchdog Separation | Medium | Medium | P3 — architectural cleanliness |
| Semantic Contract Tests | Medium | High | P4 — only needed when contracts evolve |
| Distributed Contract State | Low | High | P5 — only for multi-instance |

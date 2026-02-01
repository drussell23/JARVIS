# Enterprise Hardening Design - unified_supervisor.py
## Date: 2026-02-01
## Version: 1.0.0

---

## Executive Summary

Harden `unified_supervisor.py` to resolve critical lifecycle gaps regarding GCP Spot VMs, crash recovery, and startup timeouts. This design implements a "Clean Slate" policy and aligns wait times with GCP realities.

---

## The 4 Critical Subsystems

### 1. Intelligent Crash Recovery ("Clean Slate")

**Gap Addressed:** #1, #5, #6, #7

**Logic:** Before acquiring `StartupLock` or starting *any* process, run a pre-flight crash recovery routine.

**Implementation:**
- Import/Port `_intelligent_state_recovery` logic from `run_supervisor.py`
- Clear `cloud_lock.json`, `memory_pressure.json`, and stale `.lock` files if they exist from a previous dirty exit
- Call `cleanup_orphaned_semaphores()` from `backend.core.resilience.graceful_shutdown`
- Call `prevent_multiple_jarvis_instances()` from `backend.process_cleanup_manager`

**Crucial:** This must happen in **Phase 0**, before the lock is acquired.

**Files to modify:**
- `unified_supervisor.py` - Add Phase 0 crash recovery before lock acquisition

**Files to import from:**
- `backend.core.resilience.graceful_shutdown.cleanup_orphaned_semaphores`
- `backend.process_cleanup_manager.prevent_multiple_jarvis_instances`
- Port `_intelligent_state_recovery` and `_detect_crash_markers` patterns from `run_supervisor.py`

---

### 2. Trinity & Cross-Repo Integration ("Mind")

**Gap Addressed:** #4

**Logic:** Replace the simple inline `TrinityIntegrator` with the battle-tested `cross_repo_startup_orchestrator`.

**Implementation:**
- In **Phase 5 (Cross-Repo)**, call `backend.supervisor.cross_repo_startup_orchestrator.initialize_cross_repo_orchestration()`
- This module already handles:
  - "GCP Pre-warm" for SLIM hardware profiles
  - "Hollow Client" env vars (`JARVIS_GCP_OFFLOAD_ACTIVE`, `GCP_PRIME_ENDPOINT`)
  - "Memory Gating" for 16GB M1 Macs
  - Cloud lock management
  - Trinity Protocol sequencing

**Why:** Do not reimplement this; invoke the expert module.

**Files to modify:**
- `unified_supervisor.py` - Replace inline TrinityIntegrator with cross_repo orchestrator calls

**Files to import from:**
- `backend.supervisor.cross_repo_startup_orchestrator.initialize_cross_repo_orchestration`
- `backend.supervisor.cross_repo_startup_orchestrator.start_all_repos`

---

### 3. Realistic Timeouts ("Patience")

**Gap Addressed:** #3

**Logic:** Startup takes time. The default 120s timeout causes false failures with Trinity/GCP.

**Implementation:**
- Set `DEFAULT_TRINITY_TIMEOUT` to **600s** (10 minutes)
- Set `DEFAULT_STARTUP_TIMEOUT` to **900s** (15 minutes) when Trinity is enabled
- Ensure the Supervisor waits at least this long before declaring "Crash" or "GCP Unreachable"
- Use `JARVIS_STARTUP_TIMEOUT` env var to allow override

**Timeout Hierarchy:**
```
Trinity disabled:  DEFAULT_STARTUP_TIMEOUT = 120s (unchanged)
Trinity enabled:   DEFAULT_STARTUP_TIMEOUT = max(config.startup_timeout, 600s)
GCP provisioning:  DEFAULT_STARTUP_TIMEOUT = max(config.startup_timeout, 900s)
```

**Files to modify:**
- `unified_supervisor.py` - Update timeout constants and dynamic calculation

---

### 4. GCP Lifecycle Hooks ("Cleanup")

**Gap Addressed:** #2, #8

**Logic:** Spot VMs must be deleted if the Supervisor crashes. Emergency shutdown must stop Trinity.

**Implementation:**
- **Startup:** Call `backend.scripts.shutdown_hook.register_handlers()` in kernel `__init__`
- **Emergency Shutdown:** Add `await self._trinity.stop()` to `_emergency_shutdown()`
- **Cleanup:** In master `cleanup()` method, explicitly invoke GCP VM cleanup
- Ensure signal handlers are registered early for crash recovery

**Files to modify:**
- `unified_supervisor.py` - Update `_emergency_shutdown()` and `cleanup()`

**Files to import from:**
- `backend.scripts.shutdown_hook.register_handlers`

---

## Implementation Order

| Phase | Subsystem | Gaps | Risk if Skipped |
|-------|-----------|------|-----------------|
| 1 | Crash Recovery | 1, 5, 6, 7 | Stale state causes startup failures |
| 2 | Realistic Timeouts | 3 | False timeout kills during GCP provision |
| 3 | Trinity Integration | 4 | No Hollow Client, OOM on 16GB systems |
| 4 | GCP Lifecycle Hooks | 2, 8 | Orphaned VMs, billing continues |

---

## Success Criteria

1. **Clean Slate:** `python3 unified_supervisor.py` on a dirty system (stale locks, zombie processes) succeeds on first try
2. **No False Timeouts:** Trinity + GCP startup completes without timeout on 16GB M1 Mac
3. **Hollow Client Works:** On RAM < 32GB, J-Prime runs as Hollow Client routing to GCP
4. **Clean Shutdown:** Ctrl+C or crash leaves no orphaned processes or VMs
5. **Single Entry Point:** All functionality accessible via `python3 unified_supervisor.py`

---

## Non-Goals

- Rewriting cross_repo_startup_orchestrator (reuse it)
- Adding new CLI flags (use existing ones)
- Creating new modules (extend existing)
- Changing Trinity protocol (just wire it in)

---

## Implementation Summary (v181.0)

### Completed Changes

| Task | Status | Lines Changed |
|------|--------|---------------|
| Enterprise hardening imports | COMPLETE | +40 lines |
| Timeout constants + dynamic calculation | COMPLETE | +35 lines |
| Phase -1: Clean Slate recovery | COMPLETE | +180 lines |
| Emergency shutdown Trinity/GCP | COMPLETE | +55 lines |
| Cross-repo orchestration in Phase 5 | COMPLETE | +25 lines |

### Key Files Modified

- `unified_supervisor.py` - All 4 subsystems implemented

### New Constants

```python
DEFAULT_STARTUP_TIMEOUT = 120.0  # Base timeout
DEFAULT_TRINITY_TIMEOUT = 600.0  # 10 minutes for Trinity
DEFAULT_GCP_STARTUP_TIMEOUT = 900.0  # 15 minutes for GCP
```

### New Imports

```python
from backend.process_cleanup_manager import prevent_multiple_jarvis_instances
from backend.core.resilience.graceful_shutdown import cleanup_orphaned_semaphores
from backend.supervisor.cross_repo_startup_orchestrator import initialize_cross_repo_orchestration
from backend.scripts.shutdown_hook import register_handlers, cleanup_orphaned_semaphores_on_startup
```

### New Methods

- `_phase_clean_slate()` - Phase -1: Intelligent crash recovery
- `_calculate_effective_startup_timeout()` - Dynamic timeout based on features

### Enhanced Methods

- `_emergency_shutdown()` - Now stops Trinity + GCP VMs
- `_phase_trinity()` - Now initializes cross_repo_orchestration first
- `startup()` - Uses dynamic timeout calculation

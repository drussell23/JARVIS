# J-Prime Lifecycle Hardening — Design Spec

> **Date**: 2026-03-19
> **Sub-project**: 1 of 3 (J-Prime Lifecycle > Neural Mesh Boot > TUI Dashboard)
> **Status**: Approved, ready for implementation

---

## Problem

J-Prime at `136.113.252.164:8000` returns 404s — the service process is down on the GCP VM. The existing lifecycle infrastructure (`ensure_static_vm_ready()`, APARS, golden image) is mature but:

1. No continuous health monitoring after boot — if J-Prime dies post-boot, nobody restarts it
2. No restart storm control — a crash loop could hammer GCP APIs
3. No single-authority fencing — concurrent callers could race on VM operations
4. No deterministic health model — callers can't distinguish "slow" from "dead"
5. `.env` port conflict — line 76 says 8000, line 272 says 8002
6. `ensure_static_vm_ready()` is not idempotent under concurrent calls

## Solution: JprimeLifecycleController

A new class that owns the entire J-Prime lifecycle with a formal state machine, single-authority fencing, restart storm control, and deterministic downstream notifications.

### New file: `backend/core/jprime_lifecycle_controller.py`

Single responsibility: manage J-Prime's lifecycle from UNKNOWN to READY/TERMINAL, with continuous monitoring and auto-recovery.

---

## State Machine

### States

| State | Liveness | Readiness | Routable | Description |
|---|---|---|---|---|
| `UNKNOWN` | ? | ? | no | Initial. Haven't probed yet. |
| `PROBING` | ? | ? | no | Active health check in flight. |
| `VM_STARTING` | no | no | no | GCP VM booting (not yet RUNNING). |
| `SVC_STARTING` | yes | no | no | VM running, J-Prime loading model. APARS progress 0-99%. |
| `READY` | yes | yes | yes | J-Prime healthy, `ready_for_inference=true`. |
| `DEGRADED` | yes | partial | yes (lower priority) | J-Prime responding but slow/errors. |
| `UNHEALTHY` | no | no | no | Not responding to health checks. |
| `RECOVERING` | no | no | no | Restart attempt in progress. |
| `COOLDOWN` | no | no | no | Between restart attempts. Backoff timer running. |
| `TERMINAL` | no | no | no | Circuit open. Max restarts exhausted. |

### Transition Table

| From | To | Trigger | Guard | Action |
|---|---|---|---|---|
| `UNKNOWN` | `PROBING` | Boot or `ensure_ready()` | - | Start health probe |
| `PROBING` | `READY` | Health: `ready_for_inference=true` | - | `notify_ready()`, emit telemetry |
| `PROBING` | `SVC_STARTING` | Health: HTTP 200, `ready=false` | - | Begin APARS polling |
| `PROBING` | `VM_STARTING` | VM status: STOPPED/TERMINATED | - | `ensure_static_vm_ready()` |
| `PROBING` | `UNHEALTHY` | Health: refused/timeout | - | Record failure, eval restart budget |
| `PROBING` | `TERMINAL` | VM creation failed (budget/capacity/permanent GCP error) | - | Emit TERMINAL telemetry |
| `PROBING` | `UNHEALTHY` | Transient GCP API error (500, timeout) | - | Route through UNHEALTHY->RECOVERING for retry |
| `VM_STARTING` | `SVC_STARTING` | VM RUNNING, HTTP reachable | APARS responding | Switch to APARS polling |
| `VM_STARTING` | `UNHEALTHY` | VM start timeout (transient) | recycle_count < 2 | Increment recycle counter, route to RECOVERING |
| `VM_STARTING` | `TERMINAL` | VM start failed after 2 recycles | recycle_count >= 2 | `root_cause=vm_start_failed` |
| `SVC_STARTING` | `READY` | APARS: `ready_for_inference=true` | - | `notify_ready()`, unblock boot gate |
| `SVC_STARTING` | `UNHEALTHY` | APARS stalled >60s or startup timeout | progress delta <2% for 60s | `root_cause=startup_stall` |
| `SVC_STARTING` | `TERMINAL` | Golden image broken, 2 recycles | - | `root_cause=image_broken` |
| `READY` | `DEGRADED` | 3 consecutive slow/error responses | - | `notify_degraded()` |
| `READY` | `UNHEALTHY` | 3 consecutive health failures | - | `notify_unhealthy()` |
| `DEGRADED` | `READY` | 3 of last 5 health checks healthy (rolling window) | - | `notify_ready()` |
| `DEGRADED` | `UNHEALTHY` | 3 more consecutive failures | - | `notify_unhealthy()` |
| `DEGRADED` | `RECOVERING` | Degraded >5 min | restarts remaining | Initiate restart |
| `UNHEALTHY` | `RECOVERING` | Auto-recovery | `restarts_in_window < MAX` | Initiate restart |
| `UNHEALTHY` | `TERMINAL` | No restarts remaining | `restarts_in_window >= MAX` | Circuit open |
| `RECOVERING` | `SVC_STARTING` | Restart issued successfully | - | Poll APARS |
| `RECOVERING` | `COOLDOWN` | Restart attempt failed | - | Start backoff timer |
| `RECOVERING` | `TERMINAL` | Budget exhausted during recovery | - | Circuit open |
| `COOLDOWN` | `RECOVERING` | Backoff timer expired | restarts remaining | Retry |
| `COOLDOWN` | `TERMINAL` | Window exceeded while waiting | - | Circuit open |
| `TERMINAL` | `PROBING` | 30-min cooldown expired | - | Auto-reset, fresh probe |
| `TERMINAL` | `PROBING` | Manual reset | - | Clear counters |

### Restart Policy

| Parameter | Value | Env Override |
|---|---|---|
| `BASE_BACKOFF` | 10s | `JPRIME_RESTART_BASE_BACKOFF_S` |
| `BACKOFF_MULTIPLIER` | 2x | - |
| `MAX_BACKOFF` | 300s (5 min) | `JPRIME_RESTART_MAX_BACKOFF_S` |
| `MAX_RESTARTS` | 5 | `JPRIME_MAX_RESTARTS_PER_WINDOW` |
| `RESTART_WINDOW` | 1800s (30 min) | `JPRIME_RESTART_WINDOW_S` |
| `TERMINAL_COOLDOWN` | 1800s (30 min) | `JPRIME_TERMINAL_COOLDOWN_S` |
| `DEGRADED_PATIENCE` | 300s (5 min) | `JPRIME_DEGRADED_PATIENCE_S` |
| `HEALTH_INTERVAL` | 15s | `JPRIME_HEALTH_INTERVAL_S` |
| `DEGRADE_THRESHOLD` | 3 consecutive | `JPRIME_DEGRADE_THRESHOLD` |
| `FAILURE_THRESHOLD` | 3 consecutive | `JPRIME_FAILURE_THRESHOLD` |
| `RECOVERY_THRESHOLD` | 3 consecutive | `JPRIME_RECOVERY_THRESHOLD` |
| `SLOW_RESPONSE_MS` | 5000 | `JPRIME_SLOW_RESPONSE_MS` |

Backoff sequence: 10s, 20s, 40s, 80s, 160s.

Note: With default MAX_RESTARTS=5, MAX_BACKOFF (300s) is never reached since attempt 5 backoff is 160s < 300s. The cap exists for operators who increase MAX_RESTARTS via env override (attempt 6 would be min(320, 300) = 300s).

Storm control invariant: at most 5 restart attempts in any 30-min sliding window. The 6th transitions to TERMINAL.

---

## Fencing

JARVIS runs as a single process (unified_supervisor.py). The fencing model protects against intra-process coroutine races, not multi-process contention.

### Coroutine-Level Idempotency

```python
# _boot_future: Optional[asyncio.Future]
# First caller creates Future, concurrent callers await same one
# Resolves when READY, DEGRADED, or TERMINAL reached
#
# TERMINAL behavior: _boot_future is NOT cleared.
# Callers of ensure_ready() during TERMINAL immediately receive LEVEL_2.
# _boot_future is only cleared when TERMINAL -> PROBING transition fires
# (auto-reset after 30-min cooldown or manual reset).
```

### Transition Lock

```python
# asyncio.Lock guards all state transitions
# Only one transition executes at a time
# Prevents race between health monitor and manual ensure_ready()
```

### Singleton

```python
# Module-level _controller_instance with get_jprime_lifecycle_controller()
# No PID file needed (single-process model)
# Controller created once, lives for process lifetime
```

---

## Telemetry Contract

Every state transition emits:

```python
{
    "event": "jprime_lifecycle_transition",
    "timestamp": float,
    "from_state": str,
    "to_state": str,
    "trigger": str,
    "reason_code": str,
    "root_cause_id": Optional[str],   # groups related transitions
    "attempt": int,                     # restart attempt (0 if not restart)
    "backoff_ms": Optional[int],
    "restarts_in_window": int,
    "apars_progress": Optional[float],  # 0-100 if SVC_STARTING
    "vm_zone": Optional[str],
    "elapsed_in_prev_state_ms": float,
}
```

Forwarded to Reactor Core via `cross_repo_experience_forwarder` (best-effort, fire-and-forget).

---

## Boot Contract Gate

```
Supervisor Zone 5.7 MUST block until one of:
  READY         -> proceed normally (LEVEL_0)
  DEGRADED      -> proceed with warning (LEVEL_1)
  TERMINAL      -> proceed with reflex only (LEVEL_2)
  Timeout       -> proceed with LEVEL_2

TIMEOUT DERIVATION (must coordinate with DMS):
  boot_gate_timeout = effective_trinity_timeout - 30s (safety margin)
  effective_trinity_timeout = GCP_VM_STARTUP_TIMEOUT(300) + fallback_buffer(120) + orchestration_buffer(90) = 510s
  boot_gate_timeout = 510 - 30 = 480s

  Override: JPRIME_BOOT_GATE_TIMEOUT_S (env var, defaults to computed value)
  INVARIANT: boot_gate_timeout < DMS timeout to prevent DMS escalation during boot

No downstream phase (Zone 6+) may assume J-Prime is available
unless the gate resolved to READY.
```

---

## Downstream Notifications

```python
# PrimeRouter receives deterministic signals:
READY    -> notify_gcp_vm_ready(host, port)     -> LEVEL_0 (PRIMARY)
DEGRADED -> notify_gcp_vm_degraded(host, port)  -> LEVEL_1 (DEGRADED)
UNHEALTHY/TERMINAL -> notify_gcp_vm_unhealthy() -> LEVEL_2 (REFLEX)
```

`notify_gcp_vm_degraded()` is NEW — PrimeRouter currently only has ready/unhealthy. This adds a middle tier where requests are still attempted but with shorter timeout and fallback priority lowered.

---

## Continuous Health Monitor

After boot gate resolves, the controller starts a background `_health_loop()`:

```
Every HEALTH_INTERVAL (15s):
  1. HTTP GET http://{host}:{port}/v1/reason/health (timeout 5s)
  2. Parse response: ready_for_inference, response_time_ms
  3. Update consecutive counters (success/failure/slow)
  4. Evaluate transition rules
  5. If transition: acquire lock, execute transition, emit telemetry
```

The health loop runs for the lifetime of the controller. It respects the state machine — no health checks in RECOVERING, COOLDOWN, or TERMINAL states (those have their own timers).

---

## .env Port Conflict Fix

**Current conflict:**
- Line 76: `JARVIS_PRIME_URL=http://136.113.252.164:8000`
- Line 272: `JARVIS_PRIME_PORT=8002`

**Resolution:** Remove conflicting line, add endpoint sync.

```
1. Remove JARVIS_PRIME_PORT=8002 (line 272 of .env)
   - 40+ files read this var but all default to 8000 when unset
   - With line 272 removed, all resolve to 8000 (correct)

2. Keep JARVIS_PRIME_URL=http://136.113.252.164:8000 as canonical source

3. MindClient endpoint synchronization:
   - Add MindClient.update_endpoint(host, port) method
   - Controller calls this when endpoint changes (READY/DEGRADED/UNHEALTHY)
   - MindClient rebuilds _base_url from new host:port
   - This replaces the current MindClient._resolve_prime_host/port() at-init-only pattern

4. Notification flow:
   Controller -> PrimeRouter.notify_ready(host, port)   -> routes inference
   Controller -> MindClient.update_endpoint(host, port)  -> rebuilds _base_url
   Both called atomically in the transition action
```

---

## Integration Points

### Supervisor Boot (Zone 5.7)

```python
# In unified_supervisor.py, Zone 5.7:
controller = get_jprime_lifecycle_controller()
level = await controller.ensure_ready(timeout=600)
# level is LEVEL_0 (READY), LEVEL_1 (DEGRADED), or LEVEL_2 (TERMINAL/timeout)
# Downstream phases use this level for routing decisions
```

### Existing Code Reuse

| Existing Component | How It's Used |
|---|---|
| `gcp_vm_manager.ensure_static_vm_ready()` | Called by controller for VM_STARTING -> SVC_STARTING |
| `MindClient._health_task` | Replaced by controller's health loop (single authority) |
| `PrimeRouter.notify_gcp_vm_ready/unhealthy()` | Called by controller on transitions |
| `supervisor_gcp_controller.request_vm()` | Budget/churn checks before VM operations |

### MindClient Health Loop Dedup

MindClient currently has its own `_health_task` (30s interval). With the lifecycle controller, MindClient's health loop becomes redundant. The controller is the single authority for health. MindClient receives notifications via PrimeRouter.

**Migration:** MindClient's `_health_task` is disabled when controller is active (`JPRIME_LIFECYCLE_CONTROLLER_ENABLED=true`). Controller owns all health probing.

---

## Acceptance Criteria

1. J-Prime port/env conflict eliminated — single canonical `JARVIS_PRIME_URL`
2. Single lifecycle authority enforced — PID file + asyncio.Lock + Future collapse
3. Health monitor auto-recovers J-Prime without duplicate supervisors
4. No duplicate start/restart operations under concurrent triggers
5. Restart storms prevented — max 5 in 30-min window, exponential backoff
6. Downstream systems receive deterministic `READY/DEGRADED/UNHEALTHY` notifications
7. Boot gate blocks Zone 6+ until J-Prime readiness resolved
8. DEGRADED state notifies PrimeRouter with lower routing priority (new)
9. All transitions emit telemetry with `from_state`, `to_state`, `reason_code`, `root_cause_id`
10. TERMINAL auto-resets after 30-min cooldown
11. Passes failure-injection tests:
    - Health timeout during SVC_STARTING (APARS stall) -> UNHEALTHY
    - Restart failure during RECOVERING -> COOLDOWN with correct backoff
    - 5 concurrent ensure_ready() callers -> all await same Future
    - TERMINAL auto-reset after 30-min cooldown -> back to PROBING
    - Backoff progression: 10s, 20s, 40s, 80s, 160s verified
    - VM_STARTING timeout with retry (recycle_count < 2) -> UNHEALTHY -> RECOVERING
    - DEGRADED flapping (alternating healthy/unhealthy) -> rolling window recovery
12. MindClient endpoint stays synchronized with controller's discovered endpoint

---

## Files Changed

| File | Change |
|---|---|
| `backend/core/jprime_lifecycle_controller.py` | **NEW** — state machine, health monitor, fencing, restart policy |
| `backend/core/prime_router.py` | **MODIFY** — add `notify_gcp_vm_degraded()` |
| `backend/core/mind_client.py` | **MODIFY** — add `update_endpoint()`, disable `_health_task` when controller active |
| `unified_supervisor.py` | **MODIFY** — Zone 5.7: use controller.ensure_ready() as boot gate |
| `.env` | **MODIFY** — remove line 272 (`JARVIS_PRIME_PORT=8002`) |
| `tests/core/test_jprime_lifecycle_controller.py` | **NEW** — state machine, fencing, restart policy, failure injection |

## Out of Scope

- Changes to gcp_vm_manager.py internals (already mature, used as-is)
- Changes to J-Prime server code (server-side)
- Neural Mesh agent initialization (Sub-project 2)
- TUI dashboard (Sub-project 3)

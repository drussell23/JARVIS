# Lifecycle Resilience & Runtime Hardening (Phase 2 — Items 9-20)

**Date:** 2026-03-05
**Status:** Approved (with refinements)
**Repo:** JARVIS-AI-Agent (primary), reactor-core (item 18)
**Approach:** C+ (Phased with Contract-First Gates)
**Prerequisite:** Phase 1 (items 1-8) complete on `feat/routing-unification-type-safety`

## Problem Statement

Phase 1 fixed type-safety crashes and routing split-brain. Phase 2 addresses the remaining 12 items from the enterprise gap list: lifecycle resilience, concurrency hazards, data integrity, and runtime hygiene. These are slower-burn risks — they don't crash on every request, but they cause silent degradation, resource leaks, and unpredictable behavior under load.

## Prioritized Issue Map (Post-Research)

Research agents explored the codebase and confirmed/revised severity for each item:

| # | Issue | Confirmed Severity | Key Finding |
|---|-------|-------------------|-------------|
| 9 | No supervisor liveness watchdog | HIGH | launchd exists but only detects PID death, not hangs/deadlocks |
| 10 | Re-entrant restart hazards | CRITICAL | RestartCoordinator has re-entrant guard but NO cooldown — restart loop possible |
| 11 | Mutable pressure limits unsynchronized | MEDIUM | Broker designed for single-event-loop; observer list unprotected during iteration |
| 12 | RLock in async contexts | LOW (downgraded) | All RLock usage is in sync contexts — safe by accident. Guard test added. |
| 13 | Feedback oscillation (memory/model) | HIGH | 60s cooldown insufficient if model load + pressure rise < 60s; no hysteresis deadband |
| 14 | Unowned in-flight tasks during hot-swap | HIGH | `del _local._model` while executor thread still references it; no drain/cancel |
| 15 | Pickle cache no versioning | HIGH | 36 files use pickle with zero version envelopes; CACHE_SCHEMA_VERSION defined but unused |
| 16 | Inconsistent time sources | HIGH | `datetime.now()` for VM durations in supervisor_gcp_controller.py; corrupted by NTP |
| 17 | Case-sensitive path portability | LOW | All paths use consistent `reactor-core` casing; 2 files hardcode without env override |
| 18 | No Reactor-Core contract endpoint | MEDIUM | No /capabilities or /contract_version endpoint; version negotiation code exists but unused |
| 19 | Subprocess lifecycle leak (proxy) | HIGH | NO atexit registration; proxy lingers on crash; PID file exists but not cleaned up |
| 20 | Unbounded executor backpressure | LOW (downgraded) | BoundedAsyncQueue used in critical path; unbounded only in event logging |

## Cross-Cutting: time_utils Abstraction

Before any phase work, introduce a small `time_utils` module to prevent new mixed-time calls while the backlog remains:

**File:** `backend/core/time_utils.py`

```python
"""Monotonic time helpers — prevents new datetime.now() duration bugs."""
import time

def monotonic_ms() -> int:
    """Current monotonic time in milliseconds."""
    return int(time.monotonic() * 1000)

def monotonic_s() -> float:
    """Current monotonic time in seconds."""
    return time.monotonic()

def elapsed_since_s(start_mono: float) -> float:
    """Seconds elapsed since a monotonic start time."""
    return time.monotonic() - start_mono

def elapsed_since_ms(start_mono_ms: int) -> int:
    """Milliseconds elapsed since a monotonic start time."""
    return int(time.monotonic() * 1000) - start_mono_ms
```

New code MUST use `time_utils` for durations. Existing `datetime.now()` duration sites are fixed in Phase G.

---

## Execution Phases

### Phase E: Lifecycle Resilience (Items 9-10, 14, 19) — Blast Radius: System Death

**Why first:** These are "silent death" scenarios. A deadlocked supervisor, restart loop, orphaned inference task, or zombie proxy all result in a system that appears alive but is functionally dead.

#### E.1: Supervisor Heartbeat Liveness (Item 9)

**Current state:** launchd monitors PID death only. If the event loop deadlocks, the process stays alive but JARVIS is unresponsive.

**Fix:** Rich heartbeat file from main event loop with identity validation. External watchdog checks freshness + writer identity.

**Files:**
- Edit: `unified_supervisor.py` — add `_heartbeat_loop()` async task
- Edit: `backend/voice_unlock/com.jarvis.voiceunlock.plist` — add WatchPaths or custom healthcheck

**Heartbeat Payload:**
```json
{
  "boot_id": "a3f7c2e1-...",
  "pid": 48201,
  "ts_mono": 142857.392,
  "monotonic_age_ms": 10023,
  "phase": "ready",
  "loop_iteration": 14285,
  "written_at_wall": "2026-03-05T07:15:33.123Z"
}
```

- `boot_id`: UUID4 generated at supervisor `__init__` — uniquely identifies this process lifecycle. Stale heartbeat from a previous boot is immediately detectable.
- `pid`: Writer's PID. External checker validates `os.getpid() == heartbeat.pid` AND `boot_id` matches expected session.
- `ts_mono`: `time.monotonic()` at write time.
- `monotonic_age_ms`: Milliseconds since last heartbeat write (`current_mono - previous_mono`). If this exceeds 2x the heartbeat interval, the event loop is stalling even if the file is fresh.
- `phase`: Current supervisor phase (boot, loading, ready, degraded, shutdown).
- `loop_iteration`: Monotonically increasing counter — proves the event loop is actually advancing, not just a scheduled timer firing.

**Atomic Write Protocol:**
```python
def _write_heartbeat(self, payload: dict) -> None:
    """Atomic heartbeat: write → fsync → rename."""
    tmp = self._heartbeat_path.with_suffix(".tmp")
    data = json.dumps(payload).encode()
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)  # Ensure data hits disk before rename
    finally:
        os.close(fd)
    os.replace(str(tmp), str(self._heartbeat_path))  # Atomic rename
```

**External Validation (reader side):**
```
Read heartbeat.json
  ├─ boot_id mismatch → stale file from dead process → SIGTERM
  ├─ pid != running supervisor PID → orphaned heartbeat → SIGTERM
  ├─ mtime > 30s stale → event loop hung → SIGTERM
  ├─ mtime > 60s stale → hard hang → SIGKILL
  └─ monotonic_age_ms > 20000 (2x interval) → event loop stalling → log warning
```

#### E.2: Restart Cooldown & Backoff (Item 10)

**Current state:** `RestartCoordinator._is_restarting` prevents re-entrant restarts, but after a restart completes (process exits and relaunches), there's no cooldown. A crash-on-startup loop can fire 60+ restarts/minute.

**Fix:** Reason-classed exponential backoff with jitter and quarantine mode.

**Files:**
- Edit: `backend/core/supervisor/restart_coordinator.py`

**Restart Reason Classes:**
```python
class RestartReason(Enum):
    CRASH = "crash"                    # Unhandled exception / segfault
    DEPENDENCY_OUTAGE = "dependency"   # GCP/network/DB unavailable
    OOM = "oom"                        # Memory pressure triggered
    USER_REQUESTED = "user"            # Manual restart
    UPGRADE = "upgrade"                # Code update
```

**Backoff Parameters by Reason:**
```python
BACKOFF_PROFILES = {
    RestartReason.CRASH: BackoffProfile(base_s=5.0, max_s=300.0, jitter_pct=0.25),
    RestartReason.DEPENDENCY_OUTAGE: BackoffProfile(base_s=15.0, max_s=600.0, jitter_pct=0.50),
    RestartReason.OOM: BackoffProfile(base_s=30.0, max_s=300.0, jitter_pct=0.10),
    RestartReason.USER_REQUESTED: BackoffProfile(base_s=0.0, max_s=0.0, jitter_pct=0.0),  # No backoff
    RestartReason.UPGRADE: BackoffProfile(base_s=2.0, max_s=10.0, jitter_pct=0.0),
}
```

**Design:**
```python
# In RestartCoordinator.__init__:
self._restart_count: Dict[RestartReason, int] = defaultdict(int)
self._last_restart_time = 0.0
self._quarantine_until = 0.0  # monotonic time; 0 = not quarantined
self._backoff_reset_healthy_s = 120.0  # Reset count after 2 min healthy

QUARANTINE_THRESHOLD = 5  # Total restarts across all reasons in window
QUARANTINE_DURATION_S = 600.0  # 10 min quarantine

# In request_restart(reason: RestartReason):
now = time.monotonic()

# Quarantine check
if now < self._quarantine_until:
    remaining = self._quarantine_until - now
    logger.error(f"Restart QUARANTINED ({remaining:.0f}s remaining). "
                 f"System requires manual intervention or quarantine expiry.")
    return

# Reset if healthy long enough
if now - self._last_restart_time > self._backoff_reset_healthy_s:
    self._restart_count.clear()

# User/upgrade bypass backoff entirely
profile = BACKOFF_PROFILES[reason]
if profile.base_s == 0.0:
    self._last_restart_time = now
    # ... proceed with restart
    return

self._restart_count[reason] += 1
total_restarts = sum(self._restart_count.values())

# Quarantine after too many total restarts
if total_restarts >= QUARANTINE_THRESHOLD:
    self._quarantine_until = now + QUARANTINE_DURATION_S
    logger.error(f"Entering QUARANTINE: {total_restarts} restarts in window. "
                 f"No restarts for {QUARANTINE_DURATION_S}s.")
    return

# Calculate backoff with jitter
count = self._restart_count[reason]
base_delay = min(profile.base_s * (2 ** (count - 1)), profile.max_s)
jitter = base_delay * profile.jitter_pct * (random.random() * 2 - 1)  # +/- jitter_pct
cooldown = max(0.0, base_delay + jitter)

elapsed = now - self._last_restart_time
if elapsed < cooldown:
    logger.warning(f"Restart cooldown: {cooldown:.1f}s (reason={reason.value}, "
                   f"attempt #{count}, elapsed={elapsed:.1f}s)")
    return  # Defer restart

self._last_restart_time = now
# ... proceed with restart
```

#### E.3: In-Flight Task Drain Before Model Unload (Item 14)

**Current state:** `_unload_local_model()` calls `del _local._model` while `_inference_executor` may have a running task. The executor thread holds a reference to the model object. Result: segfault or AttributeError.

**Fix:** Bounded drain deadline with cancellation and forced teardown. Track per-task ownership and disposition.

**Files:**
- Edit: `backend/intelligence/unified_model_serving.py` — `_unload_local_model()` method (~line 3002)

**Design:**
```python
class TaskDisposition(Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"  # Drain deadline exceeded

async def _unload_local_model(self, reason: str = "", arm_recovery: bool = True):
    unload_start = time.monotonic()
    drain_deadline_s = 30.0  # Hard ceiling on drain wait

    # 1. Stop accepting new inference (trip circuit breaker FIRST)
    self._circuit_breaker.record_failure(ModelProvider.PRIME_LOCAL.value)

    # 2. Drain in-flight inference with bounded deadline
    local_client = self._clients.get(ModelProvider.PRIME_LOCAL)
    disposition = TaskDisposition.COMPLETED
    if local_client and hasattr(local_client, '_inference_executor'):
        executor = local_client._inference_executor
        # Signal no new tasks
        executor.shutdown(wait=False, cancel_futures=True)

        # Wait for currently-running task with timeout
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, executor.shutdown, True),
                timeout=drain_deadline_s
            )
            disposition = TaskDisposition.COMPLETED
        except asyncio.TimeoutError:
            logger.warning(
                f"Inference drain timed out after {drain_deadline_s}s — "
                f"forcing teardown (task abandoned)"
            )
            disposition = TaskDisposition.ABANDONED

        # Record disposition
        elapsed = time.monotonic() - unload_start
        logger.info(
            f"Model unload drain: disposition={disposition.value}, "
            f"elapsed={elapsed:.1f}s, reason={reason}"
        )

        # Recreate executor for future use (after recovery)
        local_client._inference_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llm-inference"
        )

    # 3. NOW safe to delete model (or force-delete if abandoned)
    if local_client and hasattr(local_client, '_model') and local_client._model is not None:
        del local_client._model
        local_client._model = None
        local_client._loaded = False
        gc.collect()
```

#### E.4: Cloud SQL Proxy Cleanup on Exit (Item 19)

**Current state:** `cloud_sql_proxy_manager.py` manages PID file at `temp_dir/cloud-sql-proxy.pid` but has NO atexit registration. Proxy survives supervisor crash.

**Fix:** Dual cleanup — atexit + startup reconciliation with identity-safe PID checks.

**Files:**
- Edit: `backend/intelligence/cloud_sql_proxy_manager.py`

**Design:**
```python
# At proxy start, after PID recorded:
import atexit
atexit.register(self._cleanup_proxy_atexit)

def _cleanup_proxy_atexit(self):
    """Best-effort cleanup on normal exit. Does NOT run on SIGKILL/SIGSEGV."""
    if self._process and self._process.returncode is None:
        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except Exception:
            pass
    if self.pid_path and self.pid_path.exists():
        try:
            self.pid_path.unlink()
        except Exception:
            pass

# At startup, BEFORE launching new proxy:
PROXY_FINGERPRINTS = ("cloud-sql-proxy", "cloud_sql_proxy")

async def _cleanup_stale_proxy(self):
    """Kill stale proxy from previous crashed session with identity verification."""
    if not (self.pid_path and self.pid_path.exists()):
        return

    try:
        stale_pid = int(self.pid_path.read_text().strip())
    except (ValueError, OSError):
        self.pid_path.unlink(missing_ok=True)
        return

    # Identity-safe PID check: verify process name AND command line
    try:
        proc = psutil.Process(stale_pid)
        proc_name = proc.name().lower()
        proc_cmdline = " ".join(proc.cmdline()).lower()

        is_proxy = any(fp in proc_name or fp in proc_cmdline for fp in PROXY_FINGERPRINTS)
        if not is_proxy:
            logger.info(
                f"PID {stale_pid} exists but is not a proxy "
                f"(name={proc.name()!r}) — PID reuse detected, removing stale PID file"
            )
            self.pid_path.unlink(missing_ok=True)
            return

        # Verify process group ownership (same user)
        if proc.uids().real != os.getuid():
            logger.warning(
                f"PID {stale_pid} is a proxy but owned by uid={proc.uids().real} "
                f"(expected {os.getuid()}) — skipping kill"
            )
            self.pid_path.unlink(missing_ok=True)
            return

        # Safe to kill — it's our stale proxy
        os.kill(stale_pid, signal.SIGTERM)
        logger.info(f"Killed stale proxy (PID {stale_pid}, name={proc.name()!r})")

        # Wait briefly for clean exit
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            os.kill(stale_pid, signal.SIGKILL)
            logger.warning(f"Force-killed stale proxy (PID {stale_pid})")

    except (psutil.NoSuchProcess, ProcessLookupError):
        pass  # Process already gone

    self.pid_path.unlink(missing_ok=True)
```

### Gate E Criteria
- [ ] Heartbeat file written every 10s with `boot_id`, `pid`, `monotonic_age_ms`, `phase`, `loop_iteration`
- [ ] Heartbeat write is atomic: write → fsync → rename
- [ ] External reader validates writer identity via `boot_id` + `pid`
- [ ] Restart cooldown: reason-classed backoff with jitter
- [ ] Quarantine mode: 5+ restarts in window → 10 min lockout
- [ ] User/upgrade restarts bypass backoff
- [ ] Model unload drains in-flight inference with 30s bounded deadline
- [ ] Task disposition recorded (completed/cancelled/abandoned)
- [ ] Forced teardown path on drain timeout — no infinite hang
- [ ] Proxy atexit cleanup registered at start
- [ ] Startup reconciliation with identity-safe PID check (name + cmdline + uid)
- [ ] PID reuse detected and handled (don't kill wrong process)

---

### Phase F: Concurrency & Stability (Items 11, 13, 20) — Blast Radius: Performance Degradation

**Why second:** These cause oscillation, resource waste, and slow degradation rather than instant death.

#### F.1: Observer List Protection in MemoryBudgetBroker (Item 11)

**Current state:** Observer list mutated via append/remove without protection. Notification loop iterates list while registration can happen concurrently.

**Fix:** Snapshot pattern — copy observer list before iteration.

**Files:**
- Edit: `backend/core/memory_budget_broker.py` (~lines 893-938)

**Design:**
```python
# In notification method:
observers = list(self._pressure_observers)  # Snapshot — safe to iterate
for callback in observers:
    try:
        await asyncio.wait_for(callback(tier, snapshot), timeout=2.0)
    except asyncio.TimeoutError:
        logger.warning(f"Observer {callback.__qualname__} timed out (>2s)")
    except Exception as e:
        logger.debug(f"Observer error: {e}")
```

No lock needed — single event loop guarantees no concurrent modification during the synchronous `list()` copy. The `await` points only yield between observer calls, not during the copy.

#### F.2: Feedback Oscillation Guard with Hysteresis & Telemetry (Item 13)

**Current state:** Memory pressure → unload model → pressure drops → recovery callback → reload model → pressure rises → unload. The 60s cooldown is insufficient if model_load_time + pressure_rise_time < 60s. No hysteresis deadband between tier thresholds.

**Fix:** Three-pronged:
1. **Committed unload state**: After unload, block recovery for `max(cooldown, model_load_time * 2)`.
2. **Oscillation counter**: Track unload/reload cycles. After 3 cycles in 10 minutes, enter "committed local-off" state.
3. **Root-cause telemetry**: Emit structured events on every state transition for diagnostics.

**Files:**
- Edit: `backend/intelligence/unified_model_serving.py` — memory monitor + recovery callback

**Design:**
```python
# In UnifiedModelServing:
self._model_lifecycle_cycles = 0  # unload/reload count
self._model_lifecycle_window_start = 0.0
self._model_committed_off = False  # True = stop trying to reload
self._model_committed_off_time = 0.0

OSCILLATION_CYCLE_LIMIT = 3
OSCILLATION_WINDOW_S = 600.0  # 10 minutes
COMMITTED_OFF_COOLDOWN_S = 300.0  # 5 min minimum before retry

# Hysteresis deadband: unload at tier >= CRITICAL, re-enable at tier <= NOMINAL
UNLOAD_PRESSURE_TIER = "critical"
RELOAD_PRESSURE_TIER = "nominal"

# In _unload_local_model():
now = time.monotonic()
if now - self._model_lifecycle_window_start > OSCILLATION_WINDOW_S:
    self._model_lifecycle_cycles = 0
    self._model_lifecycle_window_start = now
self._model_lifecycle_cycles += 1

# Emit structured telemetry event
self._emit_lifecycle_event({
    "event": "model_unload",
    "reason": reason,
    "cycle_count": self._model_lifecycle_cycles,
    "window_elapsed_s": now - self._model_lifecycle_window_start,
    "pressure_tier": current_pressure_tier,
    "memory_percent": current_memory_percent,
    "committed_off": self._model_committed_off,
})

if self._model_lifecycle_cycles >= OSCILLATION_CYCLE_LIMIT:
    self._model_committed_off = True
    self._model_committed_off_time = now
    logger.warning(
        f"Model oscillation detected ({self._model_lifecycle_cycles} cycles in "
        f"{OSCILLATION_WINDOW_S}s). Entering committed-off state."
    )
    self._emit_lifecycle_event({
        "event": "oscillation_guard_triggered",
        "total_cycles": self._model_lifecycle_cycles,
        "quarantine_duration_s": COMMITTED_OFF_COOLDOWN_S,
    })

# In recovery callback:
if self._model_committed_off:
    elapsed = time.monotonic() - self._model_committed_off_time
    if elapsed < COMMITTED_OFF_COOLDOWN_S:
        logger.debug(f"Recovery blocked: committed-off ({COMMITTED_OFF_COOLDOWN_S - elapsed:.0f}s remaining)")
        return
    self._model_committed_off = False
    self._model_lifecycle_cycles = 0
    self._emit_lifecycle_event({"event": "committed_off_expired"})

# Hysteresis: only allow reload if pressure has dropped to NOMINAL (not just below CRITICAL)
if current_pressure_tier != RELOAD_PRESSURE_TIER:
    logger.debug(f"Recovery blocked: pressure tier is {current_pressure_tier}, need {RELOAD_PRESSURE_TIER}")
    return

def _emit_lifecycle_event(self, event: dict) -> None:
    """Emit structured lifecycle event for diagnostics."""
    event["ts"] = time.monotonic()
    event["component"] = "unified_model_serving"
    logger.info(f"LIFECYCLE_EVENT: {json.dumps(event)}")
```

The "committed-off" state is NOT permanent — it auto-expires after 5 minutes. GCP arrival also clears it (GCP makes local model unnecessary).

#### F.3: Event Log Bounding with Spill-to-Disk (Item 20)

**Current state:** `self._event_log` in MemoryBudgetBroker grows unboundedly. Under oscillation, can reach 100KB+/hour.

**Fix:** Ring buffer with fixed capacity + optional spill-to-disk on critical faults.

**Files:**
- Edit: `backend/core/memory_budget_broker.py` (~line 400)

**Design:**
```python
from collections import deque

self._event_log: deque = deque(maxlen=1000)  # Fixed capacity ring buffer
self._critical_event_log_path = Path.home() / ".jarvis" / "critical_events.jsonl"

def _log_event(self, event: dict) -> None:
    """Add event to ring buffer. Spill critical events to disk."""
    self._event_log.append(event)
    if event.get("severity") == "critical":
        try:
            with open(self._critical_event_log_path, "a") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            pass  # Best-effort disk spill
```

Drop-in replacement for hot diagnostics. Critical events survive ring buffer eviction via append-only JSONL file.

### Gate F Criteria
- [ ] Observer snapshot pattern: no ConcurrentModificationError under concurrent registration + notification
- [ ] Oscillation guard: 3 cycles in 10 min → committed-off state
- [ ] Hysteresis deadband: unload at CRITICAL, reload only at NOMINAL
- [ ] Committed-off auto-expires after 5 min
- [ ] Structured lifecycle telemetry emitted on every unload/reload/guard-trigger
- [ ] Event log bounded to 1000 entries (deque maxlen)
- [ ] Critical events spill to disk (append-only JSONL)

---

### Phase G: Data & Time Integrity (Items 15-16) — Blast Radius: Silent Corruption

**Why third:** These cause incorrect decisions but don't crash or loop.

#### G.1: Pickle Cache Versioned Envelope (Item 15)

**Current state:** 36 files use `pickle.load/dump` with zero version checking. `CACHE_SCHEMA_VERSION = 2` exists but is unused.

**Fix:** Centralized versioned envelope with magic bytes, payload hash, migration support, and quarantine for corrupted/unknown caches.

**Files:**
- Create: `backend/vision/intelligence/cache_envelope.py`
- Edit: `backend/vision/intelligence/predictive_precomputation_engine.py`
- Edit: `backend/vision/lazy_vision_engine.py`
- Edit: `backend/vision/space_screenshot_cache.py`

**Design:**
```python
# backend/vision/intelligence/cache_envelope.py
"""Versioned pickle envelope with integrity checking."""
import hashlib
import pickle
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("jarvis.cache_envelope")

ENVELOPE_MAGIC = b"JCACHE01"  # 8-byte magic identifier

# Migration registry: (from_version, to_version) -> migration_fn
_MIGRATIONS: dict[tuple[int, int], Callable[[Any], Any]] = {}

def register_migration(from_v: int, to_v: int, fn: Callable[[Any], Any]) -> None:
    """Register a data migration handler between versions."""
    _MIGRATIONS[(from_v, to_v)] = fn


def save_versioned(path: Path, data: Any, version: int) -> None:
    """Save data wrapped in version envelope with integrity hash."""
    payload = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
    payload_hash = hashlib.sha256(payload).hexdigest()
    envelope = {
        "magic": ENVELOPE_MAGIC.decode(),
        "schema_version": version,
        "payload_hash": payload_hash,
        "data": data,
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(ENVELOPE_MAGIC)  # Write magic prefix for fast rejection
        pickle.dump(envelope, f, protocol=pickle.HIGHEST_PROTOCOL)
        f.flush()
        import os
        os.fsync(f.fileno())
    tmp.rename(path)  # Atomic on same filesystem


def load_versioned(path: Path, expected_version: int) -> Optional[Any]:
    """Load data, returning None if version mismatch, corruption, or unknown major version.

    On failure, quarantines the corrupted file to <path>.quarantine with reason code.
    """
    try:
        with open(path, "rb") as f:
            # Fast-reject: check magic bytes
            magic = f.read(len(ENVELOPE_MAGIC))
            if magic != ENVELOPE_MAGIC:
                _quarantine(path, "missing_magic")
                return None
            envelope = pickle.load(f)

        if not isinstance(envelope, dict):
            _quarantine(path, "invalid_envelope_type")
            return None

        file_version = envelope.get("schema_version")
        if file_version is None:
            _quarantine(path, "missing_version")
            return None

        # Fail closed on unknown MAJOR version (higher than expected)
        if isinstance(file_version, int) and file_version > expected_version:
            _quarantine(path, f"unknown_major_version_{file_version}_vs_{expected_version}")
            logger.warning(
                f"Cache at {path}: unknown version {file_version} > expected {expected_version}. "
                f"Quarantined — refusing to load forward-incompatible data."
            )
            return None

        # Version match — verify integrity
        if file_version == expected_version:
            data = envelope.get("data")
            stored_hash = envelope.get("payload_hash")
            if stored_hash:
                actual_hash = hashlib.sha256(
                    pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
                ).hexdigest()
                if actual_hash != stored_hash:
                    _quarantine(path, "payload_hash_mismatch")
                    return None
            return data

        # Version mismatch (older) — try migration chain
        current_data = envelope.get("data")
        current_version = file_version
        while current_version < expected_version:
            next_version = current_version + 1
            migration = _MIGRATIONS.get((current_version, next_version))
            if migration is None:
                logger.warning(
                    f"No migration path from v{current_version} to v{next_version} for {path}"
                )
                _quarantine(path, f"no_migration_{current_version}_to_{next_version}")
                return None
            try:
                current_data = migration(current_data)
                current_version = next_version
            except Exception as e:
                _quarantine(path, f"migration_failed_{current_version}_to_{next_version}")
                logger.warning(f"Migration failed for {path}: {e}")
                return None

        # Migration successful — save upgraded version
        save_versioned(path, current_data, expected_version)
        logger.info(f"Migrated cache {path} from v{file_version} to v{expected_version}")
        return current_data

    except Exception as e:
        _quarantine(path, f"load_exception_{type(e).__name__}")
        logger.warning(f"Cache load failed at {path}: {e}")
        return None


def _quarantine(path: Path, reason: str) -> None:
    """Move corrupted cache to quarantine with reason suffix."""
    quarantine_path = path.with_suffix(f".quarantine.{reason}")
    try:
        if path.exists():
            shutil.move(str(path), str(quarantine_path))
            logger.info(f"Quarantined cache {path} -> {quarantine_path}")
    except OSError as e:
        logger.debug(f"Quarantine failed for {path}: {e}")
```

Apply to the 4 highest-risk caches. Remaining 32 files get the envelope in a follow-up pass (not in this plan — diminishing returns).

#### G.2: Time Source Standardization (Item 16)

**Current state:** `supervisor_gcp_controller.py` uses `datetime.now()` for VM lifecycle durations (lines 500, 713, 771). NTP adjustments corrupt elapsed time calculations.

**Fix:** Replace `datetime.now()` duration calculations with `time_utils` monotonic helpers.

**Files:**
- Edit: `backend/core/supervisor_gcp_controller.py` (~lines 500, 713, 771)

**Design:**
```python
from backend.core.time_utils import monotonic_s, elapsed_since_s

# Pattern replacement:
# BEFORE (wrong):
elapsed = (datetime.now() - self._last_vm_created).total_seconds() / 60

# AFTER (correct):
elapsed_min = elapsed_since_s(self._last_vm_created_mono) / 60

# Store monotonic timestamp alongside datetime:
self._last_vm_created_mono = monotonic_s()
self._last_vm_created = datetime.now()  # Keep for logging/display only
```

Scope: Only the 3 duration-calculation sites in supervisor_gcp_controller.py. The broader 316-file time standardization is out of scope (diminishing returns), but the `time_utils` module prevents new violations.

### Gate G Criteria
- [ ] Cache envelope has magic bytes, schema_version, payload_hash
- [ ] Unknown major version → fail closed (quarantine, don't load)
- [ ] Corrupted payloads quarantined with reason codes
- [ ] Migration handler registry supports version-to-version upgrades
- [ ] Cache save is atomic (write → fsync → rename)
- [ ] VM duration calculations use `time_utils.monotonic_s()` / `elapsed_since_s()`
- [ ] No `datetime.now()` used for elapsed time in supervisor_gcp_controller.py
- [ ] `time_utils` module exists and is used by all new duration code

---

### Phase H: Cross-Repo & Portability (Items 17-18) — Blast Radius: Deployment Failures

**Why last:** Only affects deployment to new environments, not running systems.

#### H.1: Hardcoded Path Env Override (Item 17)

**Current state:** 2 files hardcode reactor-core path without env override.

**Files:**
- Edit: `backend/core/trinity_bridge.py` (~line 109)
- Edit: `backend/core/trinity_event_bus.py` (~line 173)

**Fix:** Replace hardcoded path with `os.environ.get("REACTOR_CORE_PATH", str(Path.home() / "Documents/repos/reactor-core"))`.

#### H.2: Reactor-Core Contract Endpoint (Item 18)

**Current state:** No /capabilities or /contract_version endpoint. Version negotiation code exists in JARVIS but is unused.

**Files:**
- Edit: `/Users/djrussell23/Documents/repos/reactor-core/reactor_core/api/server.py`

**Fix:** Add `/capabilities` endpoint mirroring JARVIS-Prime pattern with versioning and hashing.

**Endpoint Response:**
```json
{
  "provider_id": "reactor-core",
  "capabilities": ["event_bus", "trinity_bridge", "state_sync"],
  "schema_version": [0, 1, 0],
  "capability_hash": "sha256:abc123...",
  "etag": "\"v0.1.0-abc123\"",
  "timestamp": "2026-03-05T12:00:00Z"
}
```

- `schema_version`: Semantic version array `[major, minor, patch]` — major bump = breaking change.
- `capability_hash`: SHA-256 of sorted capability list — supervisor can detect drift with single string comparison.
- `etag`: HTTP ETag header for conditional requests — supervisor caches and only re-validates when ETag changes.
- **Ownership**: Reactor-Core publishes authoritative capability data. Supervisor consumes validated snapshots (stored in memory, refreshed on ETag mismatch).

**Supervisor integration:** Wire `_validate_cross_repo_contracts()` (added in Phase 1) to also check Reactor-Core alongside Prime.

### Gate H Criteria
- [ ] No hardcoded paths without env override in trinity_bridge.py, trinity_event_bus.py
- [ ] Reactor-Core serves `/capabilities` endpoint with `schema_version`, `capability_hash`, `etag`
- [ ] Supervisor contract gate checks both Prime and Reactor-Core
- [ ] ETag-based conditional refresh in supervisor (no redundant fetches)

---

## Item 12: RLock Guard Test (Deferred Fix, Active Guard)

Research confirmed all RLock usage is in sync contexts — safe by accident. Rather than leave fully deferred, we add a guard test proving no async re-entrancy hazard in current paths.

**File:** `tests/contracts/test_rlock_safety.py`

```python
"""Guard test: verify RLock usage is only in synchronous call paths."""
import ast
from pathlib import Path
import pytest

class TestRLockSafety:
    def test_no_rlock_in_async_functions(self):
        """RLock must not be acquired inside async def functions."""
        backend = Path(__file__).parent.parent.parent / "backend"
        violations = []
        for py_file in backend.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.AsyncFunctionDef,)):
                    # Check for .acquire() calls on RLock-like names
                    for child in ast.walk(node):
                        if (isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and child.func.attr == "acquire"
                            and isinstance(child.func.value, ast.Name)
                            and "lock" in child.func.value.id.lower()):
                            violations.append(
                                f"{py_file.relative_to(backend)}:{child.lineno} "
                                f"— {child.func.value.id}.acquire() in async def {node.name}"
                            )
        assert not violations, f"RLock in async contexts:\n" + "\n".join(violations)
```

---

## Items Explicitly Deferred

| # | Item | Reason |
|---|------|--------|
| 12 | RLock in async contexts | Safe by accident. Guard test prevents regression. |
| — | Broad time standardization (316 files) | `time_utils` module prevents new violations. Fix only 3 critical sites. |
| — | Remaining 32 pickle caches | Apply envelope to 4 highest-risk caches. Follow-up pass for the rest. |

## Phase-Gate Summary

| Gate | After | Criteria | Items |
|------|-------|----------|-------|
| E | Phase E | Heartbeat liveness, restart backoff + quarantine, inference drain, proxy cleanup | 9, 10, 14, 19 |
| F | Phase F | Observer snapshot, oscillation guard + hysteresis + telemetry, event log bound + spill | 11, 13, 20 |
| G | Phase G | Versioned cache envelope + quarantine + migration, monotonic time via time_utils | 15, 16 |
| H | Phase H | Path env overrides, Reactor-Core /capabilities with ETag | 17, 18 |

## Verification Strategy

Each gate runs:
1. Unit tests for new/modified code
2. Targeted grep/AST scan for prohibited patterns (e.g., `datetime.now()` in duration calculations)
3. Guard tests for deferred items (item 12 RLock)
4. Manual smoke test where automated testing isn't feasible (e.g., heartbeat liveness)

## File Manifest

| Phase | File | Repo | New/Edit |
|-------|------|------|----------|
| — | backend/core/time_utils.py | JARVIS | New |
| E | unified_supervisor.py | JARVIS | Edit |
| E | backend/core/supervisor/restart_coordinator.py | JARVIS | Edit |
| E | backend/intelligence/unified_model_serving.py | JARVIS | Edit |
| E | backend/intelligence/cloud_sql_proxy_manager.py | JARVIS | Edit |
| F | backend/core/memory_budget_broker.py | JARVIS | Edit |
| F | backend/intelligence/unified_model_serving.py | JARVIS | Edit |
| F | backend/core/memory_budget_broker.py | JARVIS | Edit |
| G | backend/vision/intelligence/cache_envelope.py | JARVIS | New |
| G | backend/vision/intelligence/predictive_precomputation_engine.py | JARVIS | Edit |
| G | backend/vision/lazy_vision_engine.py | JARVIS | Edit |
| G | backend/vision/space_screenshot_cache.py | JARVIS | Edit |
| G | backend/core/supervisor_gcp_controller.py | JARVIS | Edit |
| H | backend/core/trinity_bridge.py | JARVIS | Edit |
| H | backend/core/trinity_event_bus.py | JARVIS | Edit |
| H | reactor_core/api/server.py | reactor-core | Edit |
| — | tests/contracts/test_rlock_safety.py | JARVIS | New |

3 new files, 14 edited files across 2 repos.

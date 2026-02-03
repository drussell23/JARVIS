# Unified Voice Orchestrator & VBIA Lock Fix - Design Document

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate voice hallucinations (overlapping voices, rapid-fire repetition) and fix VBIA lock errors at the root cause level.

**Architecture:** Single playback authority via IPC socket, intelligent coalescing, serialized speaker with exclusive lock, and OS-level file locking using fcntl.flock().

**Tech Stack:** Python 3.9+, asyncio, fcntl, Unix domain sockets, JSON-lines protocol

---

## Problem Statement

### Voice Issues
1. **Overlapping voices** - Multiple TTS utterances playing simultaneously during startup
2. **Rapid-fire repetition** - Same announcement spoken multiple times in quick succession
3. **No coordination** - JARVIS, Prime, and Reactor all call `announce()` independently

### VBIA Lock Issues
1. **"Temp file size mismatch"** - Race condition in temp file creation
2. **"No such file or directory"** - Directory deleted between check and write
3. **Cross-process races** - Per-process semaphores don't protect across JARVIS/Prime/Reactor

---

## Part 1: Unified Voice Orchestrator

### 1.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         VOICE ORCHESTRATOR (Single Process)                 │
│                    Lives in: unified_supervisor.py (the kernel)             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  IPC RECEIVER (Unix Domain Socket: ~/.jarvis/voice.sock)            │   │
│  │  ├── Accepts connections from ANY process (JARVIS/Prime/Reactor)    │   │
│  │  ├── Protocol: JSON-lines {priority, category, text, source, ts}    │   │
│  │  └── Thread-safe: asyncio.Queue fed via call_soon_threadsafe        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│                                    ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  BOUNDED COLLECTOR (Thread-Safe)                                    │   │
│  │  ├── Max size: VOICE_QUEUE_MAX_SIZE (env, default: 50)              │   │
│  │  ├── Drop policy: Oldest LOW priority first, then MEDIUM            │   │
│  │  ├── Per-source rate limit: VOICE_RATE_LIMIT_PER_SOURCE (5/sec)     │   │
│  │  ├── CRITICAL/ERROR: Bypass queue entirely → immediate path         │   │
│  │  └── Dedup key: hash(text + category) with TTL from env             │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│                         ┌──────────┴──────────┐                             │
│                         ▼                      ▼                            │
│  ┌──────────────────────────────┐  ┌──────────────────────────────────┐    │
│  │  IMMEDIATE PATH (Critical)   │  │  COALESCING PATH (Normal)        │    │
│  │  ├── No batching             │  │  ├── Window: VOICE_COALESCE_MS   │    │
│  │  ├── Interrupts current      │  │  │    (env, default: 2000ms)     │    │
│  │  └── Speaks NOW              │  │  ├── Groups by category          │    │
│  └──────────────────────────────┘  │  ├── Generates summary text      │    │
│                         │          │  └── Respects recency (stale=drop)│    │
│                         │          └──────────────────────────────────┘    │
│                         │                      │                            │
│                         └──────────┬───────────┘                            │
│                                    ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  SERIALIZED SPEAKER (Exclusive Lock = FULL PLAY-THROUGH)            │   │
│  │  ├── Lock acquired BEFORE audio starts                              │   │
│  │  ├── Lock released AFTER audio completes (or interrupted)           │   │
│  │  ├── Interruption: stop_playback() → release lock → acquire → speak │   │
│  │  ├── Interrupted utterance: DISCARD (don't re-queue stale info)     │   │
│  │  └── Uses: asyncio.Lock (NOT threading.Lock)                        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  OBSERVABILITY (Metrics)                                            │   │
│  │  ├── queue_depth: Current items waiting                             │   │
│  │  ├── coalesced_count: Messages merged into summaries                │   │
│  │  ├── spoken_count: Actual utterances played                         │   │
│  │  ├── dropped_count: Messages dropped (overflow/dedup/stale)         │   │
│  │  ├── interrupt_count: High-priority interruptions                   │   │
│  │  └── Exposed via: IPC command + /api/voice/metrics endpoint         │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Configuration (All From Environment)

```python
# Socket configuration
VOICE_SOCKET_PATH = "~/.jarvis/voice.sock"  # Expanded at runtime
VOICE_SOCKET_MODE = 0o600  # Owner-only access
VOICE_MAX_CONNECTIONS = 20
VOICE_READ_TIMEOUT_MS = 5000
VOICE_MAX_MESSAGE_LENGTH = 1000

# Queue configuration
VOICE_QUEUE_MAX_SIZE = 50
VOICE_RATE_LIMIT_PER_SOURCE = 5  # per second
VOICE_DEDUP_WINDOW_MS = 10000

# Coalescing configuration
VOICE_COALESCE_WINDOW_MS = 2000
VOICE_COALESCE_IDLE_MS = 300  # Flush early if idle

# Playback configuration
VOICE_PLAYBACK_TIMEOUT_S = 30
VOICE_STOP_GRACE_S = 2.0
VOICE_SHUTDOWN_GOODBYE_TIMEOUT_S = 5.0

# Client configuration
VOICE_CLIENT_QUEUE_MAX = 20
VOICE_CLIENT_MAX_BACKOFF_S = 30
VOICE_CLIENT_DRAIN_RATE_MS = 100

# Templates (category-based)
VOICE_TEMPLATE_INIT = "{count} components initialized: {names}"
VOICE_TEMPLATE_HEALTH = "Health update: {summary}"
VOICE_TEMPLATE_PROGRESS = "Progress: {latest}"
VOICE_TEMPLATE_READY = "System ready: {names}"
VOICE_TEMPLATE_ERROR = "{count} errors: {names}"
```

### 1.3 IPC Socket Contract

**Startup Sequence:**
```
1. unlink(VOICE_SOCKET_PATH) if exists  # Prevents "Address already in use"
2. socket.bind(VOICE_SOCKET_PATH)
3. os.chmod(VOICE_SOCKET_PATH, VOICE_SOCKET_MODE)  # 0600 = owner only
4. Start IPC receiver async task
5. Start Collector consumer async task
6. Set _voice_orchestrator_ready = True
7. THEN allow announce() calls to proceed
```

**Protocol: Line-Buffered JSON-Lines**
```
Client sends: {"priority":"NORMAL","category":"init","text":"Backend ready","source":"jarvis"}\n
                                                                                              ↑ newline required

Server behavior:
- Read until \n (with VOICE_READ_TIMEOUT_MS timeout)
- On timeout mid-line: discard partial, close connection
- On JSON parse error: log warning, skip line, continue reading
- On text > VOICE_MAX_MESSAGE_LENGTH: truncate, log warning
- On client disconnect: close connection, no crash
```

**Connection Limiting:**
- Track active connection count
- When at VOICE_MAX_CONNECTIONS, reject new connections with `{"error":"busy","retry_after_ms":1000}\n`
- Decrement count when connection closes

### 1.4 Critical Flood Protection

```
State: _critical_in_flight: bool = False
       _critical_pending: Optional[Message] = None

On CRITICAL message arrival:
  IF NOT _critical_in_flight:
    → Set _critical_in_flight = True
    → Interrupt current playback (stop_playback())
    → Speak this message
    → On completion: _critical_in_flight = False
                     Check _critical_pending, speak if set
  ELSE:
    → Coalesce with _critical_pending (e.g., "2 errors")
    → Or replace if newer is higher priority

Result: No critical-interrupting-critical storm
```

### 1.5 stop_playback() Contract

```python
async def stop_playback(self, timeout_s: float = None) -> bool:
    """
    Stop current playback immediately.

    Contract:
    1. Sets self._stop_event (threading.Event) - visible to executor thread
    2. Awaits self._playback_done_event with timeout
    3. Returns True if stopped cleanly, False if timeout

    GUARANTEE: Returns only AFTER executor has signaled completion
               (or timeout expired).
    """
```

### 1.6 Playback Execution (Lock Held Full Duration)

```python
async def _speak(self, text: str) -> bool:
    """Speak with exclusive lock held for FULL play-through."""
    async with self._playback_lock:  # asyncio.Lock
        self._stop_event.clear()
        self._playback_done_event.clear()

        loop = asyncio.get_running_loop()
        executor_future = loop.run_in_executor(
            self._tts_executor,
            self._blocking_tts_playback,
            text
        )

        # Wait for completion OR timeout (DO NOT use wait_for - it cancels!)
        done, pending = await asyncio.wait(
            [executor_future],
            timeout=VOICE_PLAYBACK_TIMEOUT_S
        )

        if pending:
            # Timeout - request stop and wait for grace period
            self._stop_event.set()
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(executor_future),
                    timeout=VOICE_STOP_GRACE_S
                )
            except asyncio.TimeoutError:
                logger.error("[Voice] Executor failed to stop within grace period")
            return False

        return True
        # Lock released HERE, AFTER executor has returned
```

### 1.7 Coalescer Specification

**Category Priority (for supersession):**
```python
CATEGORY_PRIORITY = {
    "shutdown": 100,
    "error": 90,
    "critical": 90,
    "warning": 70,
    "ready": 50,
    "init": 40,
    "health": 30,
    "progress": 20,
    "general": 10,
}
```

**Window Behavior:**
- Fixed window from first message (VOICE_COALESCE_WINDOW_MS)
- Flush early if idle for VOICE_COALESCE_IDLE_MS
- Summary = template-based by category
- "Stale" = superseded by priority/recency (e.g., Shutdown > Ready)

**Names in Templates:**
- First N message texts/sources (max_count=3)
- Comma-separated, max length 100 chars
- Truncated with "... and N more" if needed

### 1.8 Client Library (Single Implementation)

**Location:** `/backend/core/voice_client.py` (THE authoritative implementation)

**Prime/Reactor import:** `from jarvis.backend.core.voice_client import VoiceClient`
(via PYTHONPATH or symlink, NOT copy-paste)

**Features:**
- Bounded local queue with drop-oldest policy (deque maxlen)
- Reconnect loop with exponential backoff
- Drain on reconnect: coalesce if >5 pending, else drain individually
- On send failure: set _connected=False, re-queue or drop

### 1.9 Lifecycle

**Startup:**
```
1. unlink(voice.sock) if exists
2. bind(voice.sock)
3. chmod(voice.sock, 0600)
4. Start IPC receiver task
5. Start Collector consumer task
6. Start Coalescer timer task
7. Set _orchestrator_ready = True
```

**Shutdown:**
```
1. Set _shutting_down = True
2. Stop accepting new IPC connections
3. Cancel Coalescer timer (flush current batch immediately)
4. Wait for current playback to complete (max 5s) OR stop_playback()
5. Speak: "JARVIS shutting down" (best-effort, short timeout)
6. Close socket
7. unlink(voice.sock)
```

### 1.10 In-Process Announce

Kernel code calls `announce()` directly into Collector queue (no socket hop).
Two producers for same queue: IPC receiver + in-process.
Thread-safe via `call_soon_threadsafe` if called from non-async context.

### 1.11 Metrics Exposure

```python
# Kernel maintains:
_metrics = {
    "queue_depth": 0,
    "coalesced_count": 0,
    "spoken_count": 0,
    "dropped_count": 0,
    "interrupt_count": 0,
    "last_spoken_at": None,
}

# IPC command: {"command": "metrics"}\n → {"metrics": {...}}\n
# HTTP: GET /api/voice/metrics → 503 if kernel unavailable
```

---

## Part 2: VBIA Lock Fix

### 2.1 Problem

Current lock implementation uses temp files + atomic rename, which fails:
- **Cross-process races**: Per-process semaphores don't protect across processes
- **Temp file corruption**: "Temp file size mismatch" errors
- **Directory races**: "No such file or directory" errors

### 2.2 Solution: fcntl.flock()

Use OS-level file locking - the same mechanism databases use.

**Guarantees:**
- ATOMIC: Lock acquisition is atomic at the kernel level
- EPHEMERAL: Lock automatically released on process death
- NO TEMP FILES: No temp files, no rename, no size checks
- CROSS-PROCESS: Works across all processes on same machine

### 2.3 Configuration

```python
JARVIS_LOCK_DIR = "~/.jarvis/cross_repo/locks"  # Expanded at runtime
LOCK_ACQUIRE_TIMEOUT_S = 5.0
LOCK_POLL_INTERVAL_S = 0.05
LOCK_STALE_WARNING_S = 30.0  # Log warning if lock held longer
```

### 2.4 Implementation

```python
class RobustFileLock:
    """
    OS-level file lock using fcntl.flock().
    All blocking I/O runs in executor to avoid blocking the event loop.
    """

    def __init__(self, lock_name: str, source: str = "jarvis"):
        # Expand path at runtime (handles ~ and $VAR)
        self._lock_dir = Path(os.path.expanduser(os.path.expandvars(LOCK_DIR_RAW)))
        self._lock_file = self._lock_dir / f"{lock_name}.lock"

    async def acquire(self, timeout_s: float = None) -> bool:
        # Reentrancy check (prevents same-process deadlock)
        # Ensure directory exists (retry on ENOENT)
        # Open lock file in executor
        # Poll with fcntl.flock(LOCK_EX | LOCK_NB) until acquired or timeout
        # Write metadata (for debugging only)
        # Return True if acquired

    async def release(self) -> None:
        # fcntl.flock(LOCK_UN) in executor
        # Close fd
        # Remove from held_locks set
```

### 2.5 Contracts and Limitations

**PLATFORM:** POSIX-only (Linux, macOS). Windows not supported.

**FILESYSTEM:** LOCK_DIR must be on a LOCAL filesystem. NFS flock() semantics are inconsistent.

**REENTRANCY:** NOT reentrant. Same process must not acquire same lock twice before release.

**FORKING:** Do NOT fork while holding the lock. Lock is process-bound.

**EVENT LOOP:** All blocking I/O runs in executor. Does not block asyncio loop.

**ON ACQUIRE FAILURE:** Caller should skip the critical section or retry later. Must NOT assume state was updated.

### 2.6 Integration

```python
# BEFORE (broken):
async with self._lock_manager.acquire("vbia_state", timeout=5.0) as acquired:
    ...

# AFTER (fixed):
from backend.core.robust_file_lock import RobustFileLock

async with RobustFileLock("vbia_state", source="jarvis") as acquired:
    if acquired:
        await self._write_vbia_state(state)
    else:
        logger.warning("Could not acquire vbia_state lock - skipping update")
```

---

## Implementation Tasks

### Task 1: RobustFileLock Implementation
- Create `/backend/core/robust_file_lock.py`
- Implement fcntl.flock()-based locking
- All blocking I/O in executor
- Reentrancy guard
- ENOENT retry
- Stale lock warning

### Task 2: VoiceClient Implementation
- Create `/backend/core/voice_client.py`
- Unix socket client with reconnect loop
- Bounded local queue with drop-oldest
- Drain on reconnect with coalescing

### Task 3: VoiceOrchestrator Core
- Create `/backend/core/voice_orchestrator.py`
- IPC server with connection limiting
- Bounded collector queue
- Thread-safe announce()

### Task 4: Coalescer Implementation
- Add to voice_orchestrator.py
- Fixed window + idle flush
- Category priority supersession
- Template-based summaries

### Task 5: Serialized Speaker
- Add to voice_orchestrator.py
- Exclusive asyncio.Lock for full play-through
- stop_playback() with threading.Event
- Executor-based TTS playback

### Task 6: Critical Path Handler
- Add to voice_orchestrator.py
- Immediate path bypassing coalescer
- Only one critical in flight
- Critical coalescing (not storm)

### Task 7: Integration - VBIA Lock Migration
- Update cross_repo_state_initializer.py
- Replace old lock with RobustFileLock
- Update all vbia_state lock usages

### Task 8: Integration - Voice Orchestrator Startup
- Add to unified_supervisor.py
- Initialize orchestrator in kernel startup
- In-process announce() for kernel messages

### Task 9: Cross-Repo Voice Client Setup
- Add voice_client import to jarvis-prime
- Add voice_client import to reactor-core
- Test cross-repo announcements

### Task 10: Metrics and Observability
- Add /api/voice/metrics endpoint
- IPC metrics command
- 503 handling when kernel unavailable

---

## Success Criteria

1. **No overlapping voices** - Only one voice plays at any time
2. **No rapid-fire repetition** - Deduplication prevents repeats
3. **Intelligent startup narration** - "5 components initialized" not 5 separate announcements
4. **No VBIA lock errors** - fcntl.flock() eliminates temp file issues
5. **Cross-repo coordination** - Prime/Reactor announcements go through same orchestrator
6. **Observable** - Metrics show queue depth, coalesced/spoken/dropped counts

---

## Files to Create/Modify

**Create:**
- `/backend/core/robust_file_lock.py` - New fcntl-based lock
- `/backend/core/voice_client.py` - Cross-repo voice client
- `/backend/core/voice_orchestrator.py` - Main orchestrator

**Modify:**
- `/backend/core/cross_repo_state_initializer.py` - Use RobustFileLock
- `/unified_supervisor.py` - Initialize voice orchestrator
- `/backend/main.py` - Add /api/voice/metrics endpoint

**Cross-Repo (symlink or PYTHONPATH):**
- `jarvis-prime` - Import voice_client
- `reactor-core` - Import voice_client

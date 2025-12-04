# GlobalSessionManager - Always-Available Session Tracking

> **Version:** 1.0.0
> **Added in:** v17.9.7
> **Location:** `start_system.py:1449-1941`
> **Author:** JARVIS Development Team

## Overview

`GlobalSessionManager` is a **thread-safe singleton** that provides guaranteed session tracking availability throughout the JARVIS application lifecycle. It solves the critical problem of session tracking being unavailable during cleanup when the hybrid coordinator fails to initialize or is unavailable.

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Solution Architecture](#solution-architecture)
3. [Singleton Pattern Implementation](#singleton-pattern-implementation)
4. [API Reference](#api-reference)
5. [Usage Examples](#usage-examples)
6. [Session Files & Registry](#session-files--registry)
7. [Multi-Terminal Safety](#multi-terminal-safety)
8. [Cleanup Integration](#cleanup-integration)
9. [Configuration](#configuration)
10. [Debugging & Troubleshooting](#debugging--troubleshooting)

---

## Problem Statement

### The Original Issue

The cleanup code relied on accessing the session tracker through a global reference to the hybrid coordinator:

```python
# Old approach (unreliable)
def cleanup_gcp_vms():
    coordinator_ref = globals().get("_hybrid_coordinator")

    if (coordinator_ref
        and hasattr(coordinator_ref, "workload_router")
        and hasattr(coordinator_ref.workload_router, "session_tracker")):
        # Use session tracker
        session_tracker = coordinator_ref.workload_router.session_tracker
        my_vm = session_tracker.get_my_vm()
    else:
        # FALLBACK: Legacy cleanup (less reliable)
        print("Session tracker not available, falling back to legacy cleanup")
        # ... legacy code ...
```

This approach failed when:

1. **Early Failure**: If the startup failed before the coordinator was initialized
2. **Coordinator Error**: If the hybrid coordinator had an exception during init
3. **Global Not Set**: If `_hybrid_coordinator` wasn't properly assigned
4. **Async Timing**: If cleanup ran before coordinator setup completed

### Symptoms Observed

```
⚠️  Session tracker not available, falling back to legacy cleanup
🧹 Intelligent Cache Manager initializing...
```

This warning indicated that GCP VM cleanup couldn't properly identify which VMs belonged to the current session, potentially leading to:
- Orphaned VMs still running and incurring costs
- Incorrect VMs being deleted (belonging to other sessions)
- Manual cleanup required

---

## Solution Architecture

### Core Design Principles

1. **Singleton Pattern**: One instance globally, guaranteed
2. **Thread-Safe Init**: `threading.Lock` protects initialization
3. **Async-Safe Ops**: `asyncio.Lock` protects all state changes
4. **Dual API**: Both sync and async methods for all operations
5. **Always Available**: No dependency on other components
6. **File-Based Registry**: Persistent tracking across processes

### Class Structure

```
GlobalSessionManager (Singleton)
├── Identity
│   ├── session_id: str (UUID)
│   ├── pid: int
│   ├── hostname: str
│   └── created_at: float (timestamp)
├── Locking
│   ├── _lock: asyncio.Lock (for async ops)
│   └── _sync_lock: threading.Lock (for sync ops)
├── File Tracking
│   ├── session_file: Path (/tmp/jarvis_session_{pid}.json)
│   ├── vm_registry: Path (/tmp/jarvis_vm_registry.json)
│   └── global_tracker_file: Path (/tmp/jarvis_global_session.json)
├── VM Tracking
│   └── _current_vm: Optional[Dict[str, Any]]
└── Statistics
    ├── vms_registered: int
    ├── vms_unregistered: int
    ├── registry_cleanups: int
    └── stale_sessions_removed: int
```

### Module-Level Accessors

```python
# Singleton instance (private)
_global_session_manager: Optional[GlobalSessionManager] = None
_session_manager_lock = threading.Lock()

# Public accessor
def get_session_manager() -> GlobalSessionManager:
    """Get the singleton, initializing if needed."""

# Availability check
def is_session_manager_available() -> bool:
    """Check if singleton has been initialized."""
```

---

## Singleton Pattern Implementation

### Double-Checked Locking

The singleton uses double-checked locking for thread safety:

```python
class GlobalSessionManager:
    _instance: Optional['GlobalSessionManager'] = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return  # Already initialized

        # Initialize once
        self._lock = asyncio.Lock()
        self._sync_lock = threading.Lock()
        # ... rest of init ...
        self._initialized = True
```

### Why Double-Checked Locking?

1. **First Check (No Lock)**: Avoids lock overhead in common case
2. **Lock Acquisition**: Protects against race conditions
3. **Second Check (With Lock)**: Ensures another thread didn't initialize while waiting

### Module-Level Accessor

```python
_global_session_manager: Optional[GlobalSessionManager] = None
_session_manager_lock = threading.Lock()

def get_session_manager() -> GlobalSessionManager:
    global _global_session_manager

    if _global_session_manager is None:
        with _session_manager_lock:
            if _global_session_manager is None:
                _global_session_manager = GlobalSessionManager()

    return _global_session_manager
```

---

## API Reference

### Module Functions

#### `get_session_manager() -> GlobalSessionManager`

Get the singleton instance, initializing if needed.

```python
session_mgr = get_session_manager()
```

#### `is_session_manager_available() -> bool`

Check if the singleton has been initialized.

```python
if is_session_manager_available():
    # Safe to use without triggering init
    pass
```

### Instance Properties

| Property | Type | Description |
|----------|------|-------------|
| `session_id` | str | UUID for this session |
| `pid` | int | Process ID |
| `hostname` | str | Machine hostname |
| `created_at` | float | Creation timestamp |
| `session_file` | Path | Per-session JSON file path |
| `vm_registry` | Path | Global VM registry path |
| `global_tracker_file` | Path | Global session tracker path |

### Async Methods

#### `register_vm(vm_id, zone, components, metadata=None) -> bool`

Register a VM as owned by this session.

```python
success = await session_mgr.register_vm(
    vm_id="jarvis-auto-12345",
    zone="us-central1-a",
    components=["voice", "vision", "ml"],
    metadata={"trigger": "HIGH_RAM"}
)
```

#### `get_my_vm() -> Optional[Dict[str, Any]]`

Get the VM owned by this session.

```python
vm = await session_mgr.get_my_vm()
if vm:
    print(f"VM: {vm['vm_id']} in {vm['zone']}")
```

#### `unregister_vm() -> bool`

Unregister VM ownership and clean up files.

```python
success = await session_mgr.unregister_vm()
```

#### `get_all_active_sessions() -> Dict[str, Dict[str, Any]]`

Get all active sessions with stale filtering.

```python
sessions = await session_mgr.get_all_active_sessions()
for sid, data in sessions.items():
    print(f"Session {sid[:8]}: PID {data['pid']}, VM {data.get('vm_id')}")
```

#### `cleanup_stale_sessions() -> int`

Proactively clean up stale sessions.

```python
removed = await session_mgr.cleanup_stale_sessions()
print(f"Removed {removed} stale sessions")
```

### Sync Methods (For Cleanup)

#### `get_my_vm_sync() -> Optional[Dict[str, Any]]`

Synchronous version for use during cleanup when event loop may not be available.

```python
vm = session_mgr.get_my_vm_sync()
```

#### `unregister_vm_sync() -> bool`

Synchronous version for cleanup.

```python
success = session_mgr.unregister_vm_sync()
```

#### `get_statistics() -> Dict[str, Any]`

Get session manager statistics.

```python
stats = session_mgr.get_statistics()
# Returns:
# {
#     "session_id": "abc123...",
#     "pid": 12345,
#     "hostname": "macbook.local",
#     "uptime_seconds": 3600.0,
#     "has_vm": True,
#     "vm_id": "jarvis-auto-12345",
#     "vms_registered": 1,
#     "vms_unregistered": 0,
#     "registry_cleanups": 2,
#     "stale_sessions_removed": 3,
# }
```

---

## Usage Examples

### Basic Initialization

```python
from start_system import get_session_manager, is_session_manager_available

# Check if already initialized
print(f"Available: {is_session_manager_available()}")  # False

# Get singleton (initializes on first access)
session_mgr = get_session_manager()

print(f"Available: {is_session_manager_available()}")  # True
print(f"Session: {session_mgr.session_id[:8]}...")
print(f"PID: {session_mgr.pid}")
```

### VM Registration Flow

```python
async def deploy_to_gcp(components: List[str]):
    session_mgr = get_session_manager()

    # Create VM via gcloud
    vm_id = await create_gcp_vm(components)
    zone = "us-central1-a"

    # Register with session manager
    await session_mgr.register_vm(
        vm_id=vm_id,
        zone=zone,
        components=components,
        metadata={
            "trigger": "HIGH_RAM",
            "ram_percent": 87.5,
        }
    )

    print(f"Registered VM {vm_id} to session {session_mgr.session_id[:8]}")
```

### Cleanup Integration

```python
def cleanup_gcp_vms():
    """Called during shutdown to cleanup session's VMs."""

    # Always available - no dependency on coordinator
    if is_session_manager_available():
        session_mgr = get_session_manager()
        my_vm = session_mgr.get_my_vm_sync()

        if my_vm:
            vm_id = my_vm["vm_id"]
            zone = my_vm["zone"]

            # Delete VM
            delete_result = subprocess.run([
                "gcloud", "compute", "instances", "delete",
                vm_id, "--zone", zone, "--quiet"
            ])

            if delete_result.returncode == 0:
                session_mgr.unregister_vm_sync()
                print(f"Deleted VM {vm_id}")
        else:
            print("No VM registered to this session")
    else:
        # Initialize now (late init)
        session_mgr = get_session_manager()
        # ... same cleanup logic ...
```

### Multi-Session Monitoring

```python
async def monitor_sessions():
    """Monitor all active JARVIS sessions."""
    session_mgr = get_session_manager()

    sessions = await session_mgr.get_all_active_sessions()

    print(f"Active JARVIS Sessions: {len(sessions)}")
    for sid, data in sessions.items():
        is_me = "* " if sid == session_mgr.session_id else "  "
        vm_status = data.get("vm_id", "no VM")
        print(f"{is_me}Session {sid[:8]}: PID {data['pid']}, {vm_status}")
```

---

## Session Files & Registry

### File Locations

| File | Path | Purpose |
|------|------|---------|
| Session File | `/tmp/jarvis_session_{pid}.json` | Per-process session data |
| VM Registry | `/tmp/jarvis_vm_registry.json` | Global registry of all sessions |
| Global Tracker | `/tmp/jarvis_global_session.json` | Current session quick reference |

### Session File Format

```json
{
    "session_id": "8f16bfd8-1234-5678-9abc-def012345678",
    "pid": 12345,
    "hostname": "macbook.local",
    "vm_id": "jarvis-auto-12345",
    "zone": "us-central1-a",
    "components": ["voice", "vision", "ml"],
    "metadata": {
        "trigger": "HIGH_RAM",
        "ram_percent": 87.5
    },
    "created_at": 1699999999.123,
    "registered_at": 1699999999.456,
    "status": "active"
}
```

### VM Registry Format

```json
{
    "8f16bfd8-1234-5678-9abc-def012345678": {
        "session_id": "8f16bfd8-1234-5678-9abc-def012345678",
        "pid": 12345,
        "hostname": "macbook.local",
        "vm_id": "jarvis-auto-12345",
        "zone": "us-central1-a",
        "components": ["voice", "vision", "ml"],
        "created_at": 1699999999.123,
        "registered_at": 1699999999.456,
        "status": "active"
    },
    "another-session-uuid": {
        // ... another session's data ...
    }
}
```

### Global Tracker Format

```json
{
    "session_id": "8f16bfd8-1234-5678-9abc-def012345678",
    "pid": 12345,
    "hostname": "macbook.local",
    "created_at": 1699999999.123,
    "vm_id": "jarvis-auto-12345",
    "zone": "us-central1-a",
    "status": "active"
}
```

---

## Multi-Terminal Safety

### The Problem

Running JARVIS in multiple terminals can cause conflicts:
- Both sessions might try to manage the same VM
- Cleanup in one terminal might delete another session's VM
- Session files could be overwritten

### The Solution

The `GlobalSessionManager` uses multiple layers of validation:

1. **Session ID**: UUID unique to each JARVIS instance
2. **Process ID**: Ensures file belongs to correct process
3. **Hostname**: Prevents cross-machine conflicts
4. **Staleness Check**: Sessions older than 12 hours are ignored

### Validation Flow

```python
def _validate_ownership(self, data: Dict[str, Any]) -> bool:
    # Check session ID matches
    if data.get("session_id") != self.session_id:
        return False

    # Check PID matches
    if data.get("pid") != self.pid:
        return False

    # Check hostname matches
    if data.get("hostname") != self.hostname:
        return False

    # Check age (expire after 12 hours)
    age_hours = (time.time() - data.get("created_at", 0)) / 3600
    if age_hours > 12:
        return False

    return True
```

### Stale Session Detection

```python
def _is_pid_running(self, pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
        cmdline = proc.cmdline()
        return "start_system.py" in " ".join(cmdline)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
```

---

## Cleanup Integration

### Before (Unreliable)

```python
# Old cleanup code
coordinator_ref = globals().get("_hybrid_coordinator")

if (coordinator_ref
    and hasattr(coordinator_ref, "workload_router")
    and hasattr(coordinator_ref.workload_router, "session_tracker")):
    session_tracker = coordinator_ref.workload_router.session_tracker
    my_vm = session_tracker.get_my_vm()
    # ... cleanup ...
else:
    print("Session tracker not available")  # THE WARNING
    # Legacy fallback...
```

### After (Always Available)

```python
# New cleanup code
if is_session_manager_available():
    session_mgr = get_session_manager()
    my_vm = session_mgr.get_my_vm_sync()
else:
    # Initialize now (late init)
    session_mgr = get_session_manager()
    my_vm = session_mgr.get_my_vm_sync()

if my_vm:
    # ... cleanup ...
else:
    print("No VM registered to this session")
```

The key difference: **`get_session_manager()` always works**, even if called for the first time during cleanup.

---

## Configuration

### Environment Variables

There are no environment variables for the session manager. All configuration is automatic based on:
- Process ID (from `os.getpid()`)
- Hostname (from `socket.gethostname()`)
- Session ID (UUID generated at initialization)

### Hardcoded Values

| Setting | Value | Rationale |
|---------|-------|-----------|
| Session expiry | 12 hours | Reasonable max session length |
| Temp directory | `/tmp` | Standard Unix temp location |

---

## Debugging & Troubleshooting

### Common Issues

#### Issue: Multiple sessions showing same VM

**Cause:** Registry not properly updated when VM ownership changes.

**Solution:** Call `unregister_vm_sync()` before re-registering.

#### Issue: Stale sessions not being cleaned

**Cause:** PIDs are reused by OS for new processes.

**Solution:** The `_is_pid_running()` check looks for "start_system.py" in cmdline, not just the PID.

#### Issue: Session files persisting after crash

**Cause:** Cleanup didn't run due to crash.

**Solution:** `get_all_active_sessions()` automatically cleans stale sessions.

### Diagnostic Commands

```bash
# Check session files
ls -la /tmp/jarvis_session_*.json

# View registry
cat /tmp/jarvis_vm_registry.json | python3 -m json.tool

# View current session
cat /tmp/jarvis_global_session.json | python3 -m json.tool

# Test session manager
python3 -c "
from start_system import get_session_manager
mgr = get_session_manager()
print(f'Session: {mgr.session_id}')
print(f'PID: {mgr.pid}')
print(f'Stats: {mgr.get_statistics()}')
"
```

### Manual Cleanup

```bash
# Remove all session files (use with caution!)
rm /tmp/jarvis_session_*.json
rm /tmp/jarvis_vm_registry.json
rm /tmp/jarvis_global_session.json
```

---

## Cross-References

- **Related:** [CacheStatisticsTracker](./cache-statistics-tracker.md)
- **Consumer:** [HybridWorkloadRouter](../cloud/hybrid-workload-router.md)
- **Consumer:** [Cleanup Code](../startup/cleanup.md)
- **Architecture:** [v17.9.7 Release Notes](../../README.md#-new-in-v1797-async-safe-statistics--global-session-management)

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2024-12 | Initial release with thread-safe singleton |


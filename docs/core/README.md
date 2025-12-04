# Core Infrastructure Documentation

> **Version:** 17.9.7
> **Last Updated:** December 2024

This directory contains documentation for JARVIS's core infrastructure components - the foundational classes and systems that ensure reliability, consistency, and robustness across the entire application.

---

## Quick Reference

| Component | Purpose | Added In |
|-----------|---------|----------|
| [CacheStatisticsTracker](./cache-statistics-tracker.md) | Async-safe statistics with self-healing | v17.9.7 |
| [GlobalSessionManager](./global-session-manager.md) | Always-available session tracking singleton | v17.9.7 |

---

## v17.9.7 Fixes Summary

### Problem → Solution Matrix

| Problem | Root Cause | Solution | Docs |
|---------|------------|----------|------|
| `AsyncSystemManager has no attribute 'backend_port'` | Missing backwards compatibility | Added port aliases | [README](../../README.md#asyncsystemmanager-port-compatibility) |
| Statistics consistency false positives | Race conditions in async code | `CacheStatisticsTracker` | [Deep Dive](./cache-statistics-tracker.md) |
| `Session tracker not available` warning | Coordinator-dependent init | `GlobalSessionManager` | [Deep Dive](./global-session-manager.md) |
| Statistics drift over time | No self-healing | Automatic invariant validation | [Deep Dive](./cache-statistics-tracker.md#self-healing-mechanism) |
| Multi-terminal session conflicts | No global coordination | Thread-safe registry | [Deep Dive](./global-session-manager.md#multi-terminal-safety) |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Application Layer                         │
├─────────────────────────────────────────────────────────────┤
│  SemanticVoiceCacheManager ───uses──► CacheStatisticsTracker│
│  HybridWorkloadRouter ─────────uses──► GlobalSessionManager │
│  AsyncSystemManager ───────────has──► Port Aliases          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Core Infrastructure                       │
├─────────────────────────────────────────────────────────────┤
│  CacheStatisticsTracker                                     │
│  ├─ Thread Safety: asyncio.Lock                             │
│  ├─ Self-Healing: 4 mathematical invariants                 │
│  ├─ Event Logging: Rolling 100-event window                 │
│  └─ Dual API: Async methods + sync properties               │
├─────────────────────────────────────────────────────────────┤
│  GlobalSessionManager                                        │
│  ├─ Pattern: Thread-safe singleton                          │
│  ├─ Thread Safety: threading.Lock + asyncio.Lock            │
│  ├─ Persistence: JSON files in /tmp                         │
│  └─ Dual API: Async methods + sync methods                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Code Locations

| Component | File | Line Range |
|-----------|------|------------|
| `CacheStatisticsTracker` | `start_system.py` | 3477-3840 |
| `GlobalSessionManager` | `start_system.py` | 1449-1910 |
| `get_session_manager()` | `start_system.py` | 1917-1935 |
| `is_session_manager_available()` | `start_system.py` | 1938-1940 |
| Port aliases in `AsyncSystemManager` | `start_system.py` | 5202-5206 |

---

## Usage Patterns

### Pattern 1: Statistics Tracking in Cache Managers

```python
class MyCacheManager:
    def __init__(self):
        self._stats = CacheStatisticsTracker()

    async def query(self, key):
        result = await self._lookup(key)
        if result:
            await self._stats.record_hit()
            return result
        else:
            await self._stats.record_miss()
            return None

    async def get_statistics(self):
        validation = await self._stats.validate_consistency(auto_heal=True)
        return validation["current_state"]
```

### Pattern 2: Session-Aware Cloud Operations

```python
async def deploy_cloud_component():
    session_mgr = get_session_manager()

    # Deploy
    vm_id = await create_vm()

    # Track ownership
    await session_mgr.register_vm(vm_id, zone, components)

    return vm_id

def cleanup_on_exit():
    session_mgr = get_session_manager()
    vm = session_mgr.get_my_vm_sync()

    if vm:
        delete_vm(vm["vm_id"])
        session_mgr.unregister_vm_sync()
```

### Pattern 3: Backwards-Compatible Port Access

```python
manager = AsyncSystemManager()

# New style (dictionary)
port = manager.ports["main_api"]

# Old style (attribute) - both work
port = manager.backend_port
```

---

## Testing

### Quick Verification

```bash
# Test CacheStatisticsTracker
python3 -c "
import asyncio
from start_system import CacheStatisticsTracker

async def test():
    t = CacheStatisticsTracker()
    await t.record_hit()
    await t.record_miss()
    v = await t.validate_consistency()
    print(f'Consistent: {v[\"consistent\"]}')

asyncio.run(test())
"

# Test GlobalSessionManager
python3 -c "
from start_system import get_session_manager
m = get_session_manager()
print(f'Session: {m.session_id[:8]}')
print(f'PID: {m.pid}')
"

# Test port aliases
python3 -c "
from start_system import AsyncSystemManager
m = AsyncSystemManager()
print(f'backend_port: {m.backend_port}')
"
```

### Integration Test

```bash
# Full startup test
python3 start_system.py --help

# Should show no warnings about:
# - "Session tracker not available"
# - "AttributeError: backend_port"
# - Statistics inconsistency
```

---

## Troubleshooting

### Statistics Issues

| Symptom | Cause | Solution |
|---------|-------|----------|
| `auto_heal_count` increasing | Race conditions in old code | Ensure all updates use `record_*()` |
| Negative counters | Integer underflow | Self-heals automatically |
| `consistent: False` after heal | Issues list still has historical issues | Check `current_state` for actual values |

### Session Issues

| Symptom | Cause | Solution |
|---------|-------|----------|
| Stale session files | Crash without cleanup | Call `cleanup_stale_sessions()` |
| Wrong VM deleted | Multi-terminal conflict | Check `session_id` matches |
| `is_session_manager_available()` returns False | Never accessed | Call `get_session_manager()` first |

---

## Migration Guide

### From Manual Counter Updates

```python
# Before
self.cache_hits += 1
self.cache_misses += 1
self.total_queries += 1

# After
await self._stats.record_hit()
await self._stats.record_miss()
# total_queries updated automatically
```

### From Coordinator-Based Session Tracking

```python
# Before
coordinator_ref = globals().get("_hybrid_coordinator")
if coordinator_ref and hasattr(...):
    session_tracker = coordinator_ref.workload_router.session_tracker
    # ...

# After
session_mgr = get_session_manager()  # Always works
# ...
```

---

## Related Documentation

- [README - v17.9.7 Section](../../README.md#-new-in-v1797-async-safe-statistics--global-session-management)
- [Semantic Voice Cache](../voice/semantic-voice-cache.md)
- [Hybrid Cloud Architecture](../cloud/hybrid-architecture.md)
- [Startup System](../startup/start-system.md)


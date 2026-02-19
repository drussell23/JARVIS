# Enterprise Async & Resilience Hardening Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate systemic async hazards, consolidate fragmented resilience primitives, and harden cross-component coordination — using existing production-grade primitives that are already in the codebase but under-adopted.

**Architecture:** Apply-what-exists strategy. The codebase already has TaskRegistry, PersistentCircuitBreaker, RetryPolicy with jitter, TimeoutConfig with 30+ named timeouts, and BoundedAsyncQueue. The problem is fragmentation (5 circuit breaker implementations) and non-adoption (50+ fire-and-forget tasks ignore TaskRegistry). We consolidate to canonical primitives and systematically apply them.

**Tech Stack:** asyncio, existing `backend/core/async_safety.py`, `backend/kernel/circuit_breaker.py`, `backend/core/coding_council/async_tools/task_registry.py`, `backend/core/coding_council/framework/retry.py`

---

## Problem Statement

Deep audit of the JARVIS codebase revealed 5 systemic failure categories:

| Category | Count | Risk | Root Cause |
|----------|-------|------|------------|
| Fire-and-forget tasks | 50+ | CRITICAL | `asyncio.create_task()` without TaskRegistry |
| Unshielded `wait_for()` | 15+ | CRITICAL | Missing `asyncio.shield()` on singleton/resource init |
| CancelledError swallowed | 20+ | CRITICAL | `except Exception` on Python 3.9+ where CancelledError is BaseException |
| Singleton half-init | 5-8 | CRITICAL | No init lock, no rollback on failure |
| Silent `except ImportError` | 30+ | HIGH | Dependencies silently missing, crashes later |
| Circuit breaker fragmentation | 5 impls | HIGH | Each module invented its own |
| Env var coordination fragility | 7 clear pts | HIGH | Manual `os.environ.pop()` in 7 exception paths |
| Retry without jitter | GCP VM | MEDIUM | Thundering herd on VM creation |
| In-memory state loss | managed_vms | MEDIUM | No persistence, relies on GCP API rediscovery |

## Existing Primitives (Already in Codebase)

These are production-ready and MUST be adopted instead of reinvented:

| Primitive | Location | Key API |
|-----------|----------|---------|
| TaskRegistry | `backend/core/coding_council/async_tools/task_registry.py` | `register()`, `tracked_task()`, `@register_task()`, `shutdown()` |
| PersistentCircuitBreaker | `backend/core/async_safety.py` | `PersistentCircuitBreaker.get(name)`, `.call()` |
| Canonical CircuitBreaker | `backend/kernel/circuit_breaker.py` | `get_circuit_breaker(name, config)`, `.execute()`, `.can_execute()` |
| RetryPolicy + jitter | `backend/core/coding_council/framework/retry.py` | `retry_async()`, `@retry()`, `RetryBudget` |
| RetryWithBackoff | `backend/kernel/circuit_breaker.py` | `.execute(coro_factory)`, `.get_delay()` |
| TimeoutConfig | `backend/core/async_safety.py` | `TimeoutConfig.get(name)`, `.adaptive()` |
| BoundedAsyncQueue | `backend/core/bounded_queue.py` | `BoundedAsyncQueue(maxsize, policy)` |
| AdaptiveRateLimiter | `backend/core/resilience/adaptive_rate_limiter.py` | `limiter.acquire(client)` |
| ErrorContext + classify_error | `backend/core/async_safety.py` | `classify_error(exc)` → ErrorCategory |

## Design: 4 Tiers

### Tier 1: Async Safety Hardening (CRITICAL)

**1a. `safe_create_task()` — Drop-in replacement for `asyncio.create_task()`**

New utility in `backend/core/async_safety.py`:

```python
_global_task_registry: Optional[TaskRegistry] = None

def safe_create_task(
    coro: Coroutine,
    *,
    name: str,
    group: Optional[str] = None,
    fire_and_forget: bool = False,
    on_error: Optional[Callable[[Exception], Awaitable[None]]] = None,
) -> asyncio.Task:
    """Create a tracked task. Exceptions logged, never silently swallowed."""
    task = asyncio.create_task(coro, name=name)

    def _done_callback(t: asyncio.Task) -> None:
        if t.cancelled():
            logger.debug(f"Task '{name}' cancelled")
            return
        exc = t.exception()
        if exc is not None:
            logger.error(f"Task '{name}' failed: {exc}", exc_info=exc)
            if on_error is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(on_error(exc))
                except RuntimeError:
                    pass

    task.add_done_callback(_done_callback)

    # Register with global TaskRegistry if available
    if _global_task_registry is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_global_task_registry.register(
                name=name, group_id=group,
            ))
        except RuntimeError:
            pass

    return task
```

**Adoption plan:** Find-and-replace across:
- `backend/core/gcp_vm_manager.py` (lines 2677, 2684, 5486)
- `backend/core/ouroboros/cross_repo.py` (lines 284, 629, 900, 1171, 1271)
- `backend/core/ouroboros/neural_mesh.py` (lines 325, 486, 614, 617)
- `backend/core/ouroboros/integration.py` (lines 365, 1911, 4334)
- `unified_supervisor.py` (multiple locations)

**1b. `shielded_wait_for()` — Safe timeout that doesn't cancel inner task**

```python
async def shielded_wait_for(
    coro_or_task: Union[Coroutine, asyncio.Task],
    timeout: float,
    *,
    name: str = "shielded_op",
) -> Any:
    """wait_for with shield. Inner task continues on timeout."""
    if asyncio.iscoroutine(coro_or_task):
        task = asyncio.ensure_future(coro_or_task)
    else:
        task = coro_or_task
    return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
```

**Adoption plan:** Replace unshielded `wait_for()` in:
- `loading_server.py` (lines 3302, 3470)
- `unified_supervisor.py` (lines 3776, 3787, 3824, 3838, 7622+)
- Subprocess cleanup patterns (add `process.kill()` in timeout handler)

**1c. CancelledError guard — Fix except Exception blocks**

Pattern to apply across 20+ locations:

```python
# BEFORE (broken on Python 3.9+):
try:
    result = await some_operation()
except Exception:
    pass  # Swallows CancelledError!

# AFTER:
try:
    result = await some_operation()
except asyncio.CancelledError:
    raise  # NEVER swallow CancelledError
except Exception:
    pass
```

**Files to fix:**
- `unified_supervisor.py` (lines 7622, 6353)
- `loading_server.py` (lines 711, 771, 1155, 1541, 1548, 1604, 1611, 1618, 3353)

### Tier 2: Circuit Breaker Consolidation (HIGH)

**Canonical implementation:** `backend/kernel/circuit_breaker.py`

This is the most complete: has `CircuitBreakerConfig`, `RetryWithBackoff` with jitter, `CircuitBreakerRegistry`, thundering-herd prevention, backward-compatible API.

**Migration plan:**

| Current Implementation | File | Action |
|----------------------|------|--------|
| `backend/core/resilience/circuit_breaker.py` | 315 lines | Replace with import from kernel |
| `backend/core/resilience/distributed_circuit_breaker.py` | Full distributed | Keep as extension of kernel CB |
| `backend/core/connection/circuit_breaker.py` | CAS-based | Migrate to kernel CB + config |
| `backend/core/prime_router.py::_EndpointAwareCircuitBreaker` | 4-state | Migrate to kernel CB + cold state extension |
| `backend/core/coding_council/*` | Multiple | Replace with kernel CB imports |

**Approach:** Create backward-compatible adapter where needed:

```python
# In backend/core/resilience/circuit_breaker.py (after migration):
from backend.kernel.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerState,
    get_circuit_breaker,
    get_registry,
)

# Re-export for backward compatibility
__all__ = [
    "CircuitBreaker", "CircuitBreakerConfig",
    "CircuitBreakerState", "CircuitOpen",
    "get_circuit_breaker", "get_registry",
]

# CircuitOpen exception preserved for existing callers
class CircuitOpen(Exception):
    pass
```

### Tier 3: Coordination Hardening (HIGH)

**3a. Env var coordination context manager**

Replace 7-point manual `os.environ.pop()` with:

```python
# backend/core/coordination_flags.py (NEW, ~60 lines)

import os
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

@asynccontextmanager
async def coordination_flag(name: str, value: str = "true"):
    """
    Set an environment variable for cross-component coordination.
    Guaranteed cleanup via finally block — no manual pop() needed.

    Usage:
        async with coordination_flag("JARVIS_INVINCIBLE_NODE_BOOTING"):
            await boot_invincible_node()
        # Flag automatically cleared on exit, error, or cancellation
    """
    os.environ[name] = value
    logger.debug(f"[CoordFlag] SET {name}={value}")
    try:
        yield
    finally:
        os.environ.pop(name, None)
        logger.debug(f"[CoordFlag] CLEARED {name}")


def is_flag_set(name: str) -> bool:
    """Check if a coordination flag is currently set."""
    return os.getenv(name, "").lower() in ("true", "1", "yes")
```

**Adoption:** Replace the 7-location manual clearing in `unified_supervisor.py` with a single `async with` block.

**3b. Import guard utility**

```python
# In backend/core/async_safety.py (add to existing file)

def guarded_import(module_path: str, *names: str, fallback=None, required: bool = False):
    """
    Import with explicit logging instead of silent failure.

    Usage:
        create_safe_task = guarded_import(
            "backend.core.async_safety", "create_safe_task",
            required=False,
        )
    """
    try:
        module = importlib.import_module(module_path)
        if names:
            result = tuple(getattr(module, name) for name in names)
            return result[0] if len(result) == 1 else result
        return module
    except ImportError as e:
        if required:
            raise
        logger.warning(f"[ImportGuard] Optional import failed: {module_path} — {e}")
        return fallback
```

**3c. Singleton init guard**

```python
# In backend/core/async_safety.py (add to existing file)

class AsyncSingletonMeta(type):
    """
    Metaclass for async singletons with double-checked locking.
    Prevents half-initialization races.

    Usage:
        class MyService(metaclass=AsyncSingletonMeta):
            async def _async_init(self):
                # Heavy async initialization here
                pass
    """
    _instances: Dict[type, Any] = {}
    _locks: Dict[type, asyncio.Lock] = {}
    _init_complete: Dict[type, bool] = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
            cls._init_complete[cls] = False
        return cls._instances[cls]

    @classmethod
    async def get_instance(mcs, cls):
        if cls not in mcs._locks:
            mcs._locks[cls] = asyncio.Lock()

        if mcs._init_complete.get(cls, False):
            return mcs._instances[cls]

        async with mcs._locks[cls]:
            if not mcs._init_complete.get(cls, False):
                instance = cls()
                if hasattr(instance, '_async_init'):
                    try:
                        await instance._async_init()
                        mcs._init_complete[cls] = True
                    except Exception:
                        # Rollback — allow retry
                        mcs._instances.pop(cls, None)
                        mcs._init_complete.pop(cls, None)
                        raise
                else:
                    mcs._init_complete[cls] = True
            return mcs._instances[cls]
```

### Tier 4: Retry & State Hardening (MEDIUM)

**4a. GCP VM creation retry with jitter**

In `backend/core/gcp_vm_manager.py`, replace:
```python
# BEFORE:
await asyncio.sleep(2 ** attempt)  # No jitter!

# AFTER:
from backend.core.coding_council.framework.retry import RetryPolicy
_vm_create_retry = RetryPolicy(
    max_attempts=3,
    base_delay=2.0,
    max_delay=30.0,
    exponential_base=2.0,
    jitter=0.25,  # ±25% jitter prevents thundering herd
)
```

**4b. Critical state persistence checkpoint**

Add periodic checkpoint for `managed_vms` to a local JSON file (not Cloud SQL — too heavy for this):

```python
# In gcp_vm_manager.py, add checkpoint methods:
_CHECKPOINT_PATH = "/tmp/jarvis_managed_vms_checkpoint.json"

async def _checkpoint_state(self) -> None:
    """Persist critical VM state for crash recovery."""
    state = {
        vm_name: {
            "ip": vm.ip,
            "zone": vm.zone,
            "created_at": vm.created_at,
            "status": vm.status,
        }
        for vm_name, vm in self.managed_vms.items()
    }
    # Atomic write via temp file + rename
    tmp = _CHECKPOINT_PATH + ".tmp"
    async with aiofiles.open(tmp, 'w') as f:
        await f.write(json.dumps(state, indent=2))
    os.rename(tmp, _CHECKPOINT_PATH)

async def _restore_checkpoint(self) -> int:
    """Restore VM state from checkpoint (supplements GCP API discovery)."""
    if not os.path.exists(_CHECKPOINT_PATH):
        return 0
    # ... restore logic with GCP API verification ...
```

## Scope & Non-Goals

**In scope:**
- Files in `backend/core/`, `backend/kernel/`, `unified_supervisor.py`, `loading_server.py`
- Async safety hardening (Tier 1)
- Circuit breaker consolidation (Tier 2)
- Coordination hardening (Tier 3)
- Retry & state improvements (Tier 4)

**Out of scope (future work):**
- Cross-repo changes to jarvis-prime or reactor-core
- New feature development
- Test infrastructure (no unit tests exist for most of these)
- Ouroboros module deep refactoring (too large, separate effort)
- Audio pipeline changes (completed in prior phase)

## Risk Assessment

| Change | Risk | Mitigation |
|--------|------|------------|
| `safe_create_task()` adoption | LOW | Drop-in compatible, adds tracking |
| `shielded_wait_for()` | LOW | Wraps existing pattern |
| CancelledError guards | LOW | Additive (add `except CancelledError: raise` before existing handler) |
| Circuit breaker consolidation | MEDIUM | Backward-compatible re-exports, test each migration |
| Env var context manager | LOW | Strictly fewer code paths |
| Singleton init guard | MEDIUM | Only apply to new/broken singletons initially |
| State persistence | LOW | Additive, supplements existing GCP API discovery |

## Success Criteria

1. Zero fire-and-forget tasks — all `asyncio.create_task()` wrapped in `safe_create_task()`
2. Zero unshielded `wait_for()` on singleton/resource init
3. Zero `except Exception` blocks that can swallow CancelledError
4. Single canonical circuit breaker implementation (kernel) with adapters
5. All env var coordination flags use context manager pattern
6. GCP VM retry uses jitter
7. Critical state has persistence checkpoint

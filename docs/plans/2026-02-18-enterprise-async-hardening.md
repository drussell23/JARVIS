# Enterprise Async & Resilience Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate systemic async hazards by adopting existing safety primitives (`create_safe_task()`, `shielded_wait_for()`), fixing CancelledError swallowing, replacing fragile env var coordination with context managers, and adding jitter to GCP retries.

**Architecture:** The codebase already has production-grade primitives in `backend/core/async_safety.py` (including `create_safe_task()` at line 1722) — but they're not used by 50+ fire-and-forget tasks. This plan systematically adopts them and fixes 3 categories of async hazard.

**Tech Stack:** asyncio, `backend/core/async_safety.py` (existing), `backend/core/coding_council/framework/retry.py` (existing)

---

### Task 1: Create `coordination_flags.py` Utility

**Files:**
- Create: `backend/core/coordination_flags.py`

**Context:** `JARVIS_INVINCIBLE_NODE_BOOTING` env var is set at 1 location and manually cleared at 7 locations via `os.environ.pop()`. Missing any clear path leaves the flag stuck. A context manager guarantees cleanup.

**Step 1: Create the coordination_flags module**

```python
"""
Coordination Flags — Context-managed environment variable flags.

Replaces manual os.environ.pop() in 7+ exception paths with a single
async with block that guarantees cleanup on exit, error, or cancellation.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger(__name__)


@asynccontextmanager
async def coordination_flag(name: str, value: str = "true"):
    """
    Set an environment variable for cross-component coordination.
    Guaranteed cleanup via finally — no manual pop() in each exception path.

    Usage:
        async with coordination_flag("JARVIS_INVINCIBLE_NODE_BOOTING"):
            result = await boot_invincible_node()
        # Flag automatically cleared on success, error, timeout, or cancellation
    """
    os.environ[name] = value
    logger.debug("[CoordFlag] SET %s=%s", name, value)
    try:
        yield
    finally:
        os.environ.pop(name, None)
        logger.debug("[CoordFlag] CLEARED %s", name)


def is_flag_set(name: str) -> bool:
    """Check if a coordination flag is currently set."""
    return os.getenv(name, "").lower() in ("true", "1", "yes")
```

**Step 2: Verify the module imports correctly**

Run: `python3 -c "from backend.core.coordination_flags import coordination_flag, is_flag_set; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add backend/core/coordination_flags.py
git commit -m "feat: add coordination_flags context manager for env var cleanup"
```

---

### Task 2: Add `shielded_wait_for()` to async_safety.py

**Files:**
- Modify: `backend/core/async_safety.py` (add near line 1690, before fire-and-forget section)
- Modify: `backend/core/async_safety.py` (add to `__all__` list near line 2767)

**Context:** `asyncio.wait_for()` CANCELS the inner task on timeout. 15+ call sites need `asyncio.shield()` wrapping. A utility function prevents developers from forgetting the shield.

**Step 1: Add `shielded_wait_for()` function**

Find the comment/section break before the fire-and-forget section (around line 1695-1697) and insert BEFORE it:

```python
async def shielded_wait_for(
    coro_or_task,
    timeout: float,
    *,
    name: str = "shielded_op",
):
    """
    Like asyncio.wait_for() but shields the inner task from cancellation.

    When wait_for() times out, it CANCELS the inner coroutine/task.
    This wrapper uses asyncio.shield() so the inner task continues
    running even if the timeout fires.

    Use this for:
    - Singleton initialization (half-init is worse than slow-init)
    - Resource cleanup (must complete even if parent times out)
    - Background tasks that should outlive the waiter

    Raises asyncio.TimeoutError on timeout (inner task keeps running).
    """
    if asyncio.iscoroutine(coro_or_task):
        task = asyncio.ensure_future(coro_or_task)
    else:
        task = coro_or_task
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[AsyncSafety] shielded_wait_for '%s' timed out after %.1fs (task continues)", name, timeout)
        raise
```

**Step 2: Add to `__all__` list**

In the `__all__` list (around line 2700-2767), add `"shielded_wait_for"`.

**Step 3: Verify**

Run: `python3 -c "from backend.core.async_safety import shielded_wait_for; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add backend/core/async_safety.py
git commit -m "feat: add shielded_wait_for() utility to prevent task cancellation on timeout"
```

---

### Task 3: Replace Env Var Manual Clearing with Context Manager

**Files:**
- Modify: `unified_supervisor.py` (~lines 62452-62616)

**Context:** `JARVIS_INVINCIBLE_NODE_BOOTING` is set at line 62625 and manually cleared at 7 locations. We replace this with `async with coordination_flag(...)` that wraps the entire boot logic. The flag is set at the `async with` entry and automatically cleared on any exit path.

**Step 1: Read the current code block**

Read `unified_supervisor.py` lines 62620-62620 (the set point) and lines 62440-62620 (the function containing all 7 clear points). Understand the control flow:
- Line 62625: SET flag
- Line 62456: CLEAR on CancelledError
- Line 62462: CLEAR on TimeoutError
- Line 62468: CLEAR on Exception
- Line 62565: CLEAR on success
- Line 62598: CLEAR on VM not ready
- Line 62606: CLEAR on ImportError
- Line 62612: CLEAR on Exception (inner)

**Step 2: Wrap the boot logic in coordination_flag context manager**

Replace the pattern. The key transformation is:

```python
# BEFORE (7 manual clear points):
os.environ["JARVIS_INVINCIBLE_NODE_BOOTING"] = "true"
# ... 160 lines of code with 7 os.environ.pop() calls ...

# AFTER (single context manager):
from backend.core.coordination_flags import coordination_flag

async with coordination_flag("JARVIS_INVINCIBLE_NODE_BOOTING"):
    # ... same logic but WITHOUT any os.environ.pop() calls ...
```

Remove ALL 7 `os.environ.pop("JARVIS_INVINCIBLE_NODE_BOOTING", None)` lines. The context manager handles cleanup.

**Important:** The CancelledError handler at line 62456 must still `raise` after cleanup — the context manager's `finally` runs before the re-raise, so this works correctly.

**Step 3: Verify the function still works syntactically**

Run: `python3 -c "import ast; ast.parse(open('unified_supervisor.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

**Step 4: Commit**

```bash
git add unified_supervisor.py
git commit -m "refactor: replace 7 manual env var clears with coordination_flag context manager"
```

---

### Task 4: Adopt `create_safe_task()` for Fire-and-Forget Tasks

**Files:**
- Modify: `backend/core/gcp_vm_manager.py` (line 5486)
- Modify: `unified_supervisor.py` (lines 59722, 59728, 59871, 59877, 66699)
- Modify: `loading_server.py` (lines 7214, 7296, 7328)

**Context:** `create_safe_task()` already exists at `backend/core/async_safety.py:1722`. It tracks tasks in a set, adds done callbacks for exception logging, and supports graceful shutdown via `wait_for_fire_and_forget_tasks()`. 8 fire-and-forget call sites don't use it.

**Step 1: Fix gcp_vm_manager.py**

At line 5486, replace:
```python
asyncio.create_task(
    self._trigger_golden_image_rebuild(image_name, reason)
)
```
With:
```python
from backend.core.async_safety import create_safe_task
create_safe_task(
    self._trigger_golden_image_rebuild(image_name, reason),
    name=f"golden_image_rebuild_{image_name}",
)
```

Move the import to the top of the file (with the other imports) or use a local import if the module has circular import risk.

**Step 2: Fix unified_supervisor.py fire-and-forget tasks**

At line 59722, replace:
```python
asyncio.ensure_future(self._notify_prime_router_of_gcp(node_ip, port))
```
With:
```python
create_safe_task(
    self._notify_prime_router_of_gcp(node_ip, port),
    name="notify_prime_router_gcp_up",
)
```

At line 59728, replace:
```python
asyncio.ensure_future(
    self._notify_model_serving_of_gcp(node_ip, port)
)
```
With:
```python
create_safe_task(
    self._notify_model_serving_of_gcp(node_ip, port),
    name="notify_model_serving_gcp_up",
)
```

At line 59871, replace:
```python
asyncio.ensure_future(self._notify_prime_router_demote())
```
With:
```python
create_safe_task(
    self._notify_prime_router_demote(),
    name="notify_prime_router_demote",
)
```

At line 59877, replace:
```python
asyncio.ensure_future(self._notify_model_serving_demote())
```
With:
```python
create_safe_task(
    self._notify_model_serving_demote(),
    name="notify_model_serving_demote",
)
```

At line 66699, replace:
```python
_heartbeat_task = asyncio.ensure_future(_progress_heartbeat())
```
With:
```python
_heartbeat_task = create_safe_task(
    _progress_heartbeat(),
    name="startup_progress_heartbeat",
)
```

Add import near the top of the relevant section:
```python
from backend.core.async_safety import create_safe_task
```

**Step 3: Fix loading_server.py fire-and-forget tasks**

At lines 7214, 7296, 7328, replace each:
```python
asyncio.create_task(self._send_with_retry(payload))
```
With:
```python
create_safe_task(
    self._send_with_retry(payload),
    name="telemetry_send",
)
```

Add import at top of file or at the class level.

**Step 4: Verify all imports resolve**

Run: `python3 -c "import ast; ast.parse(open('unified_supervisor.py').read()); print('OK')"`
Run: `python3 -c "import ast; ast.parse(open('loading_server.py').read()); print('OK')"`
Run: `python3 -c "import ast; ast.parse(open('backend/core/gcp_vm_manager.py').read()); print('OK')"`
Expected: All `OK`

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py unified_supervisor.py loading_server.py
git commit -m "fix: adopt create_safe_task() for 8 fire-and-forget task sites"
```

---

### Task 5: Fix CancelledError Swallowing in unified_supervisor.py

**Files:**
- Modify: `unified_supervisor.py` (lines 6358, 60493-60502, 63418-63434, 7695-7730+)

**Context:** On Python 3.9+, `CancelledError` is a `BaseException`, NOT caught by `except Exception`. But on older Python it IS an `Exception`. Several `except (asyncio.TimeoutError, Exception)` blocks swallow CancelledError, breaking cancellation propagation. Fix: add explicit `except asyncio.CancelledError: raise` BEFORE `except Exception`.

**Step 1: Fix event handler timeout (line ~6358)**

Find and replace:
```python
except (asyncio.TimeoutError, Exception):
    pass
```
With:
```python
except asyncio.CancelledError:
    raise
except (asyncio.TimeoutError, Exception):
    pass
```

**Step 2: Fix visual pipeline teardown (lines ~60493-60502)**

For each of the N-Optic Nerve and Ghost Hands stop blocks, replace:
```python
except (asyncio.TimeoutError, Exception) as e:
    self.logger.debug(f"[Kernel] N-Optic stop error: {e}")
```
With:
```python
except asyncio.CancelledError:
    raise
except (asyncio.TimeoutError, Exception) as e:
    self.logger.debug(f"[Kernel] N-Optic stop error: {e}")
```

Same pattern for Ghost Hands block.

**Step 3: Fix Trinity startup narration (lines ~63418-63434)**

Replace both `except (asyncio.TimeoutError, Exception): pass` blocks with:
```python
except asyncio.CancelledError:
    raise
except (asyncio.TimeoutError, Exception):
    pass
```

**Step 4: Fix Docker health checks (lines ~7695-7730+)**

For each subprocess `except Exception: return False` block, replace:
```python
except Exception:
    return False
```
With:
```python
except asyncio.CancelledError:
    raise
except Exception:
    return False
```

**Step 5: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('unified_supervisor.py').read()); print('OK')"`
Expected: `OK`

**Step 6: Commit**

```bash
git add unified_supervisor.py
git commit -m "fix: prevent CancelledError swallowing in 10+ except Exception blocks"
```

---

### Task 6: Fix CancelledError Swallowing in loading_server.py

**Files:**
- Modify: `loading_server.py`

**Context:** Same pattern as Task 5 — `except Exception: pass` blocks that can swallow CancelledError.

**Step 1: Search for all vulnerable patterns**

Search `loading_server.py` for `except Exception` and `except (asyncio.TimeoutError, Exception)` blocks that are near `await` calls. For each one, add `except asyncio.CancelledError: raise` BEFORE the Exception handler.

Pattern to apply everywhere:
```python
# BEFORE:
except Exception:
    pass

# AFTER:
except asyncio.CancelledError:
    raise
except Exception:
    pass
```

**Step 2: Fix WebSocket broadcast (line ~3353)**

Replace:
```python
except (asyncio.TimeoutError, Exception):
    pass
```
With:
```python
except asyncio.CancelledError:
    raise
except (asyncio.TimeoutError, Exception):
    pass
```

**Step 3: Fix remaining bare `except Exception` blocks near async operations**

Apply the same pattern to all `except Exception` blocks at lines ~711, 771, 1155, 1541, 1548, 1604, 1611, 1618 that are inside async functions and could receive CancelledError.

**Important:** Only fix blocks that are in `async def` functions. Synchronous functions don't get CancelledError.

**Step 4: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('loading_server.py').read()); print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add loading_server.py
git commit -m "fix: prevent CancelledError swallowing in loading_server async handlers"
```

---

### Task 7: Shield Critical wait_for() Calls in unified_supervisor.py

**Files:**
- Modify: `unified_supervisor.py`

**Context:** `asyncio.wait_for()` cancels the inner task on timeout. For singleton init, resource cleanup, and subprocess management, this is dangerous. Use `shielded_wait_for()` (added in Task 2) for critical operations.

**Step 1: Import shielded_wait_for**

Add near relevant usage sites (or at module level if not circular):
```python
from backend.core.async_safety import shielded_wait_for
```

**Step 2: Shield visual pipeline teardown (lines ~60493-60502)**

Replace:
```python
await asyncio.wait_for(self._n_optic_nerve.stop(), timeout=5.0)
```
With:
```python
await shielded_wait_for(self._n_optic_nerve.stop(), timeout=5.0, name="n_optic_stop")
```

Same for Ghost Hands:
```python
await shielded_wait_for(self._ghost_hands_orchestrator.stop(), timeout=5.0, name="ghost_hands_stop")
```

**Step 3: Shield Trinity startup narration (lines ~63418-63434)**

Replace both:
```python
await asyncio.wait_for(
    self._narrator.narrate_phase_start("trinity"),
    timeout=_narrator_timeout,
)
```
With:
```python
await shielded_wait_for(
    self._narrator.narrate_phase_start("trinity"),
    timeout=_narrator_timeout,
    name="narrator_trinity_start",
)
```

Same pattern for `_startup_narrator.announce_trinity_init()`.

**Step 4: Shield event handler timeout (line ~6358)**

Replace:
```python
await asyncio.wait_for(result, timeout=2.0)
```
With:
```python
await shielded_wait_for(result, timeout=2.0, name="event_handler")
```

**Step 5: Add subprocess cleanup to Docker health checks**

For subprocess patterns at lines ~7695-7730, add `proc.kill()` in the timeout handler:

```python
try:
    proc = await asyncio.create_subprocess_exec(...)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    return proc.returncode == 0
except asyncio.TimeoutError:
    proc.kill()  # Clean up zombie process
    await proc.wait()
    return False
except asyncio.CancelledError:
    proc.kill()
    await proc.wait()
    raise
except Exception:
    return False
```

**Step 6: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('unified_supervisor.py').read()); print('OK')"`
Expected: `OK`

**Step 7: Commit**

```bash
git add unified_supervisor.py
git commit -m "fix: shield critical wait_for() calls and add subprocess cleanup on timeout"
```

---

### Task 8: Shield wait_for() Calls in loading_server.py

**Files:**
- Modify: `loading_server.py`

**Context:** WebSocket operations (`send_str`, `close`) without shield can leave protocol in corrupted state.

**Step 1: Shield WebSocket broadcast send (line ~3302)**

Replace:
```python
await asyncio.wait_for(ws.send_str(message), timeout=5.0)
```
With:
```python
from backend.core.async_safety import shielded_wait_for
await shielded_wait_for(ws.send_str(message), timeout=5.0, name="ws_broadcast")
```

**Step 2: Shield WebSocket close (lines ~3468-3475)**

Replace:
```python
close_tasks.append(
    asyncio.wait_for(
        ws.close(code=WSCloseCode.GOING_AWAY, message=b'Server shutting down'),
        timeout=2.0
    )
)
```
With:
```python
close_tasks.append(
    shielded_wait_for(
        ws.close(code=WSCloseCode.GOING_AWAY, message=b'Server shutting down'),
        timeout=2.0,
        name="ws_close",
    )
)
```

**Step 3: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('loading_server.py').read()); print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add loading_server.py
git commit -m "fix: shield WebSocket wait_for() calls to prevent protocol corruption"
```

---

### Task 9: Add Jitter to GCP VM Retry

**Files:**
- Modify: `backend/core/gcp_vm_manager.py` (lines ~2880 and ~2963)

**Context:** Two exponential backoff patterns use `await asyncio.sleep(2 ** attempt)` with NO jitter. This creates thundering herd when multiple retries align. The codebase already has `RetryPolicy` with jitter in `backend/core/coding_council/framework/retry.py`.

**Step 1: Add jitter to firewall verification retry (line ~2880)**

Replace:
```python
await asyncio.sleep(2 ** attempt)  # Exponential backoff
```
With:
```python
import random
_delay = (2 ** attempt) * (1.0 + random.uniform(-0.25, 0.25))
await asyncio.sleep(_delay)
```

**Step 2: Add jitter to firewall creation retry (line ~2963)**

Replace:
```python
await asyncio.sleep(2 ** attempt)
```
With:
```python
_delay = (2 ** attempt) * (1.0 + random.uniform(-0.25, 0.25))
await asyncio.sleep(_delay)
```

**Step 3: Add `import random` to module imports if not already present**

Check if `import random` exists in the file. If not, add it to the import block at the top.

**Step 4: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('backend/core/gcp_vm_manager.py').read()); print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py
git commit -m "fix: add jitter to GCP retry backoff to prevent thundering herd"
```

---

### Task 10: Circuit Breaker Re-export for Consolidation

**Files:**
- Modify: `backend/core/resilience/circuit_breaker.py`

**Context:** The canonical circuit breaker is at `backend/kernel/circuit_breaker.py` (661 lines, enterprise-grade with Registry, RetryWithBackoff, jitter). `backend/core/resilience/circuit_breaker.py` (315 lines) is a simpler duplicate. As a first consolidation step, make the resilience module re-export from the kernel module while preserving backward compatibility.

**Step 1: Read both files to understand API differences**

Read `backend/kernel/circuit_breaker.py` (full file) and `backend/core/resilience/circuit_breaker.py` (full file). Compare their public APIs.

**Step 2: Replace resilience circuit_breaker.py with re-exports**

Replace the contents of `backend/core/resilience/circuit_breaker.py` with:

```python
"""
Circuit Breaker — Re-exports from canonical kernel implementation.

This module previously had its own implementation. It now re-exports
from backend.kernel.circuit_breaker for consolidation. The CircuitOpen
exception is preserved for backward compatibility.

Canonical implementation: backend/kernel/circuit_breaker.py
"""

# Re-export canonical implementation
from backend.kernel.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerState,
    CircuitBreakerRegistry,
    RetryConfig,
    RetryWithBackoff,
    get_circuit_breaker,
    get_registry,
)

# Backward-compatible exception (callers catch CircuitOpen)
class CircuitOpen(Exception):
    """Raised when a circuit breaker is in OPEN state."""
    pass


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerState",
    "CircuitBreakerRegistry",
    "CircuitOpen",
    "RetryConfig",
    "RetryWithBackoff",
    "get_circuit_breaker",
    "get_registry",
]
```

**Step 3: Verify all imports still work**

Run: `python3 -c "from backend.core.resilience.circuit_breaker import CircuitBreaker, CircuitOpen; print('OK')"`
Run: `python3 -c "from backend.kernel.circuit_breaker import CircuitBreaker, get_circuit_breaker; print('OK')"`
Expected: Both `OK`

**Step 4: Search for imports of the old module and verify compatibility**

Search for files importing from `backend.core.resilience.circuit_breaker` to ensure all symbols they use are now re-exported.

**Step 5: Commit**

```bash
git add backend/core/resilience/circuit_breaker.py
git commit -m "refactor: consolidate resilience circuit_breaker to re-export from kernel"
```

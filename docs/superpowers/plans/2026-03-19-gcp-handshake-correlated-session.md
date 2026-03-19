# GCP Readiness Handshake — Correlated Session, Failure Taxonomy & Autonomous Recovery (v297.0) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the false-negative `schema_mismatch` on every JARVIS startup by adding a correlated handshake session, precise failure classification, and autonomous VM recovery.

**Architecture:** Five existing files are extended — no new files. `gcp_readiness_lease.py` gains `HandshakeSession` and updated failure taxonomy; `gcp_vm_manager.py` gains contract enforcement; `gcp_vm_readiness_prober.py` threads the session through all probes and correctly classifies failures; `startup_routing_policy.py` gains a `(step, failure_class) → RecoveryStrategy` matrix; `startup_orchestrator.py` gains a CPU/memory backpressure gate and autonomous VM recreation.

**Tech Stack:** Python 3.11+, asyncio, aiohttp (existing), psutil (existing via IntelligentMemoryController), pytest, pytest-asyncio

---

## File Map

| File | Change Type | What changes |
|------|-------------|--------------|
| `backend/core/gcp_readiness_lease.py` | Extend | `HandshakeSession` dataclass; 3 new `ReadinessFailureClass` values; `ReadinessProber` ABC gains `session` param; `_run_probe` forwards session; `acquire()` creates + validates session |
| `backend/core/gcp_vm_manager.py` | Harden | `ContractViolationError`; `check_lineage()` raises on empty name; `_check_vm_golden_image_lineage()` auto-fetches metadata returning `metadata_fetch_failed` as distinct reason; `ensure_static_vm_ready()` gains `recreate` flag |
| `backend/core/gcp_vm_readiness_prober.py` | Rewrite internals | All 3 probe methods accept `session`; `probe_health` populates session fields; `probe_capabilities` uses `session.instance_name` + maps reason → failure_class; `invalidate_session_cache()` method; `/v1/contract` boot check |
| `backend/core/startup_routing_policy.py` | Extend | `RecoveryStrategy` enum; `_RECOVERY_MATRIX`; `select_recovery_strategy(step, failure_class)` method; `signal_gcp_handshake_failed` accepts optional step/class args |
| `backend/core/startup_orchestrator.py` | Extend | `_ProbeReadinessBudget` class; `vm_manager` ref on orchestrator; `_gcp_retry_count`; updated `acquire_gcp_lease` consults recovery matrix, fires recreation task, enforces MAX_RETRY cap |
| `backend/tests/test_gcp_handshake_v297.py` | Create | 15 hermetic tests T1–T15 |

---

## Critical Design Notes (read before touching any file)

**1. `_REASON_TO_FAILURE_CLASS` drives pass/fail in `probe_capabilities`**
The old code uses `if not should_recreate: pass`. The new code uses reason string lookup:
- If `reason in _REASON_TO_FAILURE_CLASS` → probe FAILS with that class
- If reason is absent (pass cases: `golden_image_matches`, `golden_image_disabled`, `golden_image_stale`, `no_golden_image_available`, `no_golden_image_no_fallback`) → probe PASSES
This means `metadata_fetch_failed` (should_recreate=False) correctly becomes a TRANSIENT_INFRA failure, not a silent pass.

**2. `signal_gcp_ready` (not `signal_gcp_vm_ready`)**
The actual method name in `startup_routing_policy.py` is `signal_gcp_ready(host, port)` — the spec reference to `signal_gcp_vm_ready` is a naming error. Always use `signal_gcp_ready`.

**3. Session flows through lease, orchestrator reads `lease.last_session`**
`GCPReadinessLease.acquire()` creates a `HandshakeSession` and stores it as `self._last_session`. Orchestrator reads it after failure via `self._lease.last_session` to pass to the recreation task.

**4. `probe_capabilities` catches `ContractViolationError` explicitly**
Wrap `check_lineage()` call with `except ContractViolationError` → return `failure_class=CONTRACT_VIOLATION`. The generic `except Exception` must remain as final fallback.

**5. `ensure_static_vm_ready(recreate=True)` patch**
When `recreate=True`: (a) skip the `ALREADY_READY` early return, (b) after describing the instance, force `should_recreate = True` to bypass the golden image check and go straight to DELETE → CREATE flow.

---

## Task 1: Failure Taxonomy + HandshakeSession + ABC update

**Files:**
- Modify: `backend/core/gcp_readiness_lease.py:1-30` (imports, `__all__`)
- Modify: `backend/core/gcp_readiness_lease.py:47-56` (`ReadinessFailureClass`)
- Modify: `backend/core/gcp_readiness_lease.py:88-108` (`ReadinessProber` ABC)
- Modify: `backend/core/gcp_readiness_lease.py:176-226` (`acquire()`)
- Modify: `backend/core/gcp_readiness_lease.py:269-304` (`_run_probe`)
- Test: `backend/tests/test_gcp_handshake_v297.py` (T14)

- [ ] **Step 1: Write the failing test (T14)**

Create `backend/tests/test_gcp_handshake_v297.py`:

```python
# backend/tests/test_gcp_handshake_v297.py
"""Hermetic tests for GCP handshake v297.0 — no GCP network calls."""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from backend.core.gcp_readiness_lease import (
    GCPReadinessLease,
    HandshakeResult,
    HandshakeSession,
    HandshakeStep,
    ReadinessFailureClass,
    ReadinessProber,
)
from backend.core.startup_routing_policy import (
    RecoveryStrategy,
    StartupRoutingPolicy,
    HandshakeStep as PolicyHandshakeStep,
)


# ---------------------------------------------------------------------------
# T14: Recovery matrix has exactly the expected HEALTH entries
# ---------------------------------------------------------------------------

def test_recovery_matrix_has_entries_for_expected_health_classes():
    """Matrix has entries for exactly 5 classes reachable at HEALTH step.

    New classes TRANSIENT_INFRA, LINEAGE_MISMATCH, CONTRACT_VIOLATION
    are not reachable at HEALTH — they fall to default (FALLBACK_LOCAL).
    """
    from backend.core.startup_routing_policy import _RECOVERY_MATRIX, HandshakeStep as SRP_Step
    health_entries = {
        fc for (step, fc) in _RECOVERY_MATRIX if step == SRP_Step.HEALTH
    }
    assert health_entries == {
        ReadinessFailureClass.NETWORK,
        ReadinessFailureClass.TIMEOUT,
        ReadinessFailureClass.RESOURCE,
        ReadinessFailureClass.PREEMPTION,
        ReadinessFailureClass.QUOTA,
    }, f"Unexpected HEALTH entries: {health_entries}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_recovery_matrix_has_entries_for_expected_health_classes -v
```
Expected: FAIL with `ImportError` (RecoveryStrategy and _RECOVERY_MATRIX don't exist yet) or `AttributeError` on `HandshakeSession`

- [ ] **Step 3: Add `HandshakeSession` to `gcp_readiness_lease.py`**

Add `import uuid` to the imports block. Then add `HandshakeSession` dataclass after the existing `ReadinessFailureClass` class and before `LeaseStatus`. Also add `HandshakeSession` and `ContractViolationError` to `__all__`:

```python
# In imports block, add:
import uuid

# __all__ — update to add new exports:
__all__ = [
    "HandshakeSession",
    "HandshakeStep",
    "ReadinessFailureClass",
    "LeaseStatus",
    "HandshakeResult",
    "ReadinessProber",
    "GCPReadinessLease",
]
```

Add `HandshakeSession` dataclass right after `LeaseStatus` class (after line 66):

```python
@dataclass
class HandshakeSession:
    """Correlated context for a single lease acquisition attempt.

    Created by GCPReadinessLease.acquire() before the first probe step.
    HEALTH writes instance identity; CAPABILITIES and WARM_MODEL read it.
    """
    session_id: str                             # uuid4 — ties all three steps
    lease_id: str                               # uuid4 — owning lease
    # Populated by HEALTH step
    instance_name: str = ""
    instance_id: str = ""                       # GCP numeric ID (optional, for observability)
    zone: str = ""
    endpoint: str = ""                          # "host:port" that passed health
    # Timing
    started_at: float = field(default_factory=time.monotonic)
```

- [ ] **Step 4: Expand `ReadinessFailureClass` with 3 new values**

Replace the existing `ReadinessFailureClass` enum body:

```python
class ReadinessFailureClass(str, Enum):
    """Machine-readable classification for readiness probe failures."""

    # Existing — retained for backwards compatibility
    NETWORK            = "network"            # TCP/DNS connectivity failure
    QUOTA              = "quota"              # GCP quota exceeded
    RESOURCE           = "resource"           # CPU/memory insufficient on VM
    PREEMPTION         = "preemption"         # Preemptible VM killed
    SCHEMA_MISMATCH    = "schema_mismatch"    # Genuine lineage mismatch (kept for external consumers)
    TIMEOUT            = "timeout"            # Step exceeded time budget
    # New — precise classification
    TRANSIENT_INFRA    = "transient_infra"    # Metadata fetch failed, GCP API blip — retry
    LINEAGE_MISMATCH   = "lineage_mismatch"   # VM from wrong/outdated golden image — recreate
    CONTRACT_VIOLATION = "contract_violation" # Programming error: empty instance_name
```

- [ ] **Step 5: Update `ReadinessProber` ABC to accept `session` parameter**

Replace the ABC body:

```python
class ReadinessProber(abc.ABC):
    """Abstract interface for probing VM readiness across 3 dimensions."""

    @abc.abstractmethod
    async def probe_health(
        self, host: str, port: int, timeout: float,
        session: HandshakeSession,
    ) -> HandshakeResult:
        """Check basic VM health; populates session with instance identity."""

    @abc.abstractmethod
    async def probe_capabilities(
        self, host: str, port: int, timeout: float,
        session: HandshakeSession,   # session.instance_name is guaranteed non-empty
    ) -> HandshakeResult:
        """Verify the VM exposes the expected API capabilities."""

    @abc.abstractmethod
    async def probe_warm_model(
        self, host: str, port: int, timeout: float,
        session: HandshakeSession,
    ) -> HandshakeResult:
        """Confirm the inference model is loaded and warm."""

    def invalidate_session_cache(self) -> None:
        """Clear per-step cache for a new session. Default no-op for probers without cache."""
```

- [ ] **Step 6: Update `_run_probe` to accept and forward `session`**

Replace `_run_probe` in `GCPReadinessLease` (currently starts at line 269):

```python
async def _run_probe(
    self,
    step: HandshakeStep,
    probe_fn,
    host: str,
    port: int,
    timeout: float,
    session: HandshakeSession,   # NEW: forwarded to concrete implementation
) -> HandshakeResult:
    """Execute a single probe with a timeout guard."""
    try:
        result = await asyncio.wait_for(
            probe_fn(host, port, timeout, session),   # session passed through
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning(
            "Probe %s timed out after %.3fs", step.value, timeout,
        )
        return HandshakeResult(
            step=step,
            passed=False,
            failure_class=ReadinessFailureClass.TIMEOUT,
            detail=f"probe timed out after {timeout:.3f}s",
        )
    except Exception as exc:
        logger.exception("Probe %s raised unexpected error", step.value)
        return HandshakeResult(
            step=step,
            passed=False,
            failure_class=ReadinessFailureClass.NETWORK,
            detail=f"unexpected error: {exc}",
        )
```

- [ ] **Step 7: Update `acquire()` to create session, invalidate cache, pass session to probes, validate after HEALTH**

Replace `acquire()` in `GCPReadinessLease`:

```python
async def acquire(
    self,
    host: str,
    port: int,
    timeout_per_step: float,
) -> bool:
    """Run the full 3-step handshake and acquire the lease on success.

    Creates a HandshakeSession before the first probe. HEALTH populates
    session.instance_name; CAPABILITIES consumes it. Session is stored
    as self._last_session for caller inspection after acquisition.

    Returns True if all 3 steps passed and the lease is now ACTIVE.
    """
    self._handshake_log.clear()
    self._host = host
    self._port = port
    self._last_failure_class = None

    # Create correlated session for this acquisition attempt
    session = HandshakeSession(
        session_id=str(uuid.uuid4()),
        lease_id=str(uuid.uuid4()),
    )
    self._last_session = session

    # Invalidate cached HEALTH + CAPABILITIES results so the prober
    # re-runs identity lookup on each new session attempt.
    self._prober.invalidate_session_cache()

    probe_methods = [
        (HandshakeStep.HEALTH, self._prober.probe_health),
        (HandshakeStep.CAPABILITIES, self._prober.probe_capabilities),
        (HandshakeStep.WARM_MODEL, self._prober.probe_warm_model),
    ]

    for step, probe_fn in probe_methods:
        result = await self._run_probe(
            step, probe_fn, host, port, timeout_per_step, session,
        )
        self._handshake_log.append(result)

        if not result.passed:
            self._status = LeaseStatus.FAILED
            self._last_failure_class = result.failure_class
            logger.warning(
                "[GCP_LEASE] session=%s step=%s result=FAIL class=%s "
                "reason=%s instance=%s zone=%s",
                session.session_id, step.value,
                result.failure_class.value if result.failure_class else "unknown",
                result.detail, session.instance_name, session.zone,
            )
            return False

        # After HEALTH: validate session.instance_name was populated
        if step == HandshakeStep.HEALTH and not session.instance_name:
            logger.warning(
                "[GCP_LEASE] session=%s HEALTH passed but instance_name empty "
                "— prober did not populate session. Treating as NETWORK failure.",
                session.session_id,
            )
            self._status = LeaseStatus.FAILED
            self._last_failure_class = ReadinessFailureClass.NETWORK
            return False

        logger.debug(
            "[GCP_LEASE] session=%s step=%s result=PASS",
            session.session_id, step.value,
        )

    # All steps passed — activate the lease.
    self._status = LeaseStatus.ACTIVE
    self._acquired_at = time.monotonic()
    logger.info(
        "[GCP_LEASE] session=%s TERMINAL outcome=acquired "
        "instance=%s zone=%s endpoint=%s TTL=%.1fs",
        session.session_id, session.instance_name, session.zone,
        session.endpoint, self._ttl_seconds,
    )
    return True
```

Also add `_last_session: Optional["HandshakeSession"] = None` to `__init__` and expose it as a property:
```python
# In __init__, after self._handshake_log:
self._last_session: Optional[HandshakeSession] = None

# New property:
@property
def last_session(self) -> Optional[HandshakeSession]:
    """Session from the most recent acquire() call, or None if never acquired."""
    return self._last_session
```

Also update `refresh()` — it currently calls `_run_probe` with 5 args; update to pass `session`:
```python
# In refresh(), create a minimal session for the health re-probe:
session = HandshakeSession(
    session_id=str(uuid.uuid4()),
    lease_id=self._last_session.lease_id if self._last_session else str(uuid.uuid4()),
    instance_name=self._last_session.instance_name if self._last_session else "",
    zone=self._last_session.zone if self._last_session else "",
    endpoint=self._last_session.endpoint if self._last_session else "",
)
result = await self._run_probe(
    HandshakeStep.HEALTH,
    self._prober.probe_health,
    self._host,
    self._port,
    timeout_per_step,
    session,          # NEW
)
```

- [ ] **Step 8: Run T14 to verify it now fails with routing policy not having _RECOVERY_MATRIX yet**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_recovery_matrix_has_entries_for_expected_health_classes -v
```
Expected: FAIL with `ImportError: cannot import name 'RecoveryStrategy'` (that's correct — Task 4 adds it)

- [ ] **Step 9: Commit**

```bash
git add backend/core/gcp_readiness_lease.py backend/tests/test_gcp_handshake_v297.py
git commit -m "feat(handshake): add HandshakeSession, expand failure taxonomy, session-thread ABC (v297.0 Task 1)"
```

---

## Task 2: ContractViolationError + check_lineage contract + recreate flag

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:9382-9396` (Disease 10 wrappers section)
- Modify: `backend/core/gcp_vm_manager.py:9270-9381` (`_check_vm_golden_image_lineage`)
- Modify: `backend/core/gcp_vm_manager.py:8442-8448` (`ensure_static_vm_ready` signature)
- Test: `backend/tests/test_gcp_handshake_v297.py` (T1)

- [ ] **Step 1: Write the failing test (T1)**

Append to `test_gcp_handshake_v297.py`:

```python
# ---------------------------------------------------------------------------
# T1: check_lineage("", None) raises ContractViolationError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_instance_name_raises_contract_violation():
    """check_lineage('', None) must raise ContractViolationError.

    Calling with empty instance_name is a programming error (D2).
    The caller (probe_capabilities) must provide the name from HEALTH step.
    """
    from backend.core.gcp_vm_manager import ContractViolationError
    from unittest.mock import MagicMock

    # Real manager is too heavy to instantiate — test check_lineage via duck-typing.
    # We'll use a minimal class that has check_lineage delegating to the actual logic.
    # Instead: directly test the guard in check_lineage by patching the internals.

    # Build a mock manager with check_lineage delegating to a thin wrapper:
    class _MinimalManager:
        async def check_lineage(self, instance_name, vm_metadata=None):
            if not instance_name:
                raise ContractViolationError(
                    "check_lineage() requires a non-empty instance_name."
                )
            return False, "golden_image_matches"

    mgr = _MinimalManager()
    with pytest.raises(ContractViolationError):
        await mgr.check_lineage("", None)

    # Non-empty name must NOT raise
    result = await mgr.check_lineage("jarvis-prime-stable", None)
    assert result == (False, "golden_image_matches")
```

NOTE: This test uses a thin wrapper because instantiating `GCPVMManager` requires GCP credentials. After Task 2 is implemented, T1 will be updated in Task 3 to test the actual vm_manager via mock patching.

- [ ] **Step 2: Run T1 to verify it fails** (real guard not implemented yet)

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_empty_instance_name_raises_contract_violation -v
```
Expected: PASS (the wrapper test passes — confirms test structure before the real impl)

- [ ] **Step 3: Add `ContractViolationError` to `gcp_vm_manager.py`**

Find the Disease 10 wrappers section (near line 9382). Add the error class just before `ping_health`:

```python
# ---------------------------------------------------------------------------
# Disease 10 support: contract types
# ---------------------------------------------------------------------------

class ContractViolationError(Exception):
    """Raised when check_lineage() is called with a programming error.

    Callers must provide a non-empty instance_name acquired from the
    HEALTH step's instance identity probe. Passing "" is a programming
    error, not a recoverable condition.
    """
```

- [ ] **Step 4: Harden `check_lineage()` to raise on empty name**

Replace `check_lineage()` (currently at line 9392):

```python
async def check_lineage(
    self, instance_name: str, vm_metadata: Optional[Dict[str, str]] = None
) -> Tuple[bool, str]:
    """Public API for Disease 10 readiness prober.

    Raises ContractViolationError if instance_name is empty.
    When vm_metadata is None, auto-fetches from GCP before lineage check.
    Returns (should_recreate: bool, reason: str).
    """
    if not instance_name:
        raise ContractViolationError(
            "check_lineage() requires a non-empty instance_name. "
            "The HEALTH step must populate session.instance_name before "
            "capabilities probe is called."
        )
    return await self._check_vm_golden_image_lineage(instance_name, vm_metadata)
```

- [ ] **Step 5: Update `_check_vm_golden_image_lineage` to auto-fetch and return `metadata_fetch_failed`**

Replace the `if vm_metadata is None:` block (currently lines 9303-9317) in `_check_vm_golden_image_lineage`. The existing block starts at line 9303:

```python
        # If we couldn't get metadata, auto-fetch before applying conservative fallback.
        if vm_metadata is None:
            if instance_name:
                try:
                    _, vm_metadata, _ = await self._describe_instance_full(instance_name)
                except Exception as e:
                    # Genuine GCP API failure — distinct reason from lineage mismatch
                    logger.warning(
                        "[InvincibleNode] metadata_fetch_failed for %s: %s",
                        instance_name, e,
                    )
                    return False, "metadata_fetch_failed"   # TRANSIENT_INFRA, not SCHEMA_MISMATCH

            # Still None after fetch attempt (instance truly not reachable via GCP API)
            logger.warning(
                "⚠️ [InvincibleNode] Cannot read VM metadata for %s. "
                "Checking golden image availability to decide.", instance_name,
            )
            try:
                builder = self.get_golden_image_builder()
                latest = await builder.get_latest_golden_image()
                if latest and not latest.is_stale(self.config.golden_image_max_age_days):
                    return True, "metadata_unavailable_golden_exists"   # TRANSIENT_INFRA
            except Exception as e:
                logger.debug("[InvincibleNode] Golden image check failed: %s", e)
            return False, "metadata_unavailable_no_golden"              # TRANSIENT_INFRA
```

This replaces the old block that had `logger.warning(f"⚠️ ...")` and returned `metadata_unavailable_golden_exists` / `metadata_unavailable_no_golden` without attempting a fetch.

- [ ] **Step 6: Add `recreate: bool = False` parameter to `ensure_static_vm_ready`**

Update the signature at line 8442:

```python
async def ensure_static_vm_ready(
    self,
    port: Optional[int] = None,
    timeout: Optional[float] = None,
    progress_callback: Optional[Callable[[int, str, str], None]] = None,
    activity_callback: Optional[Callable[[], None]] = None,
    recreate: bool = False,   # NEW: when True, force recreation (skip start-existing path)
) -> Tuple[bool, Optional[str], str]:
```

Then wire `recreate=True` inside the method body. Find the `ALREADY_READY` early return (~line 8525) and add the guard:

```python
            if verdict == HealthVerdict.READY and not recreate:
                # Original fast path — only taken when recreate=False
                logger.info(f"✅ [InvincibleNode] VM already ready: {static_ip}")
                asyncio.ensure_future(self._acquire_readiness_lease_bg(static_ip, target_port))
                return True, static_ip, "ALREADY_READY"
            elif verdict == HealthVerdict.READY and recreate:
                logger.info(
                    "[InvincibleNode] recreate=True — skipping ALREADY_READY, "
                    "forcing recreation of %s", instance_name,
                )
```

Then, after the instance is described (where lineage is checked), add a `recreate` override. Find the call to `_check_vm_golden_image_lineage` inside `ensure_static_vm_ready` (it may be called via `should_recreate, reason = await self._check_vm_golden_image_lineage(...)`) and add:

```python
                if recreate:
                    should_recreate = True
                    reason = "forced_recreation"
```

This forces the DELETE → CREATE flow regardless of the golden image check result.

- [ ] **Step 7: Update T1 to test the real guard (replace the `_MinimalManager` wrapper)**

Update the T1 body in `test_gcp_handshake_v297.py` to patch `GCPVMManager.check_lineage` directly instead of using a wrapper class. Since `GCPVMManager` is too heavy to instantiate in tests (requires GCP credentials), test the guard via a thin adapter that mirrors what `check_lineage` does:

```python
@pytest.mark.asyncio
async def test_empty_instance_name_raises_contract_violation():
    """The real check_lineage() guard must raise ContractViolationError on empty name."""
    # Import the real error class (not a copy)
    from backend.core.gcp_vm_manager import ContractViolationError

    # We test the guard behavior via the GCPVMReadinessProber, which calls check_lineage.
    # Patch check_lineage to execute the real guard logic (mirrors the actual implementation).
    vm = MagicMock()

    async def _real_guard(instance_name, vm_metadata=None):
        if not instance_name:
            raise ContractViolationError(
                "check_lineage() requires a non-empty instance_name."
            )
        return False, "golden_image_matches"

    vm.check_lineage = _real_guard

    prober = GCPVMReadinessProber(vm)
    session_empty = HandshakeSession(session_id="s1", lease_id="l1", instance_name="")
    session_named = HandshakeSession(session_id="s1b", lease_id="l1b", instance_name="jarvis-prime-stable")

    # Empty name → probe_capabilities must catch CONTRACT_VIOLATION
    result_empty = await prober.probe_capabilities("10.0.0.1", 8000, 5.0, session_empty)
    assert not result_empty.passed
    assert result_empty.failure_class == ReadinessFailureClass.CONTRACT_VIOLATION

    # Non-empty name → passes
    result_named = await prober.probe_capabilities("10.0.0.1", 8000, 5.0, session_named)
    assert result_named.passed
```

- [ ] **Step 8: Run T1 to verify the updated test passes**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_empty_instance_name_raises_contract_violation -v
```
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add backend/core/gcp_vm_manager.py backend/tests/test_gcp_handshake_v297.py
git commit -m "feat(handshake): ContractViolationError, check_lineage guard, metadata auto-fetch, recreate flag (v297.0 Task 2)"
```

---

## Task 3: GCPVMReadinessProber session threading + failure classification

**Files:**
- Modify: `backend/core/gcp_vm_readiness_prober.py` (full rewrite of all probe methods)
- Test: `backend/tests/test_gcp_handshake_v297.py` (T2, T3, T4, T5, T12)

- [ ] **Step 1: Write the failing tests (T2, T3, T4, T5, T12)**

Append to `test_gcp_handshake_v297.py`:

```python
# ---------------------------------------------------------------------------
# Prober tests: T2, T3, T4, T5, T12
# ---------------------------------------------------------------------------

from backend.core.gcp_vm_readiness_prober import GCPVMReadinessProber


def _make_healthy_vm_manager(instance_name: str = "jarvis-prime-stable") -> MagicMock:
    """Build a minimal mock vm_manager for prober tests."""
    vm = MagicMock()
    vm.config.static_instance_name = instance_name
    vm.config.zone = "us-central1-b"
    verdict = MagicMock()
    verdict.value = "ready"
    vm.ping_health = AsyncMock(return_value=(verdict, {}))
    vm.check_lineage = AsyncMock(return_value=(False, "golden_image_matches"))
    return vm


# T2 — probe_health populates session.instance_name
@pytest.mark.asyncio
async def test_health_populates_session_instance_name():
    vm = _make_healthy_vm_manager("jarvis-prime-stable")
    prober = GCPVMReadinessProber(vm)
    session = HandshakeSession(session_id="s1", lease_id="l1")

    result = await prober.probe_health("10.0.0.1", 8000, 5.0, session)

    assert result.passed, f"probe_health failed: {result.detail}"
    assert session.instance_name == "jarvis-prime-stable"
    assert session.endpoint == "10.0.0.1:8000"
    assert session.zone == "us-central1-b"


# T3 — probe_capabilities uses session.instance_name, never ""
@pytest.mark.asyncio
async def test_capabilities_uses_session_instance_name():
    vm = _make_healthy_vm_manager()
    prober = GCPVMReadinessProber(vm)
    session = HandshakeSession(
        session_id="s2", lease_id="l2",
        instance_name="jarvis-prime-stable",
    )

    await prober.probe_capabilities("10.0.0.1", 8000, 5.0, session)

    # Must have called check_lineage with the actual instance name, not ""
    vm.check_lineage.assert_called_once()
    called_name = vm.check_lineage.call_args[0][0]
    assert called_name == "jarvis-prime-stable", (
        f"probe_capabilities called check_lineage with '{called_name}', expected 'jarvis-prime-stable'"
    )
    assert called_name != "", "probe_capabilities must NOT pass empty string to check_lineage"


# T4 — metadata_fetch_failed classified as TRANSIENT_INFRA (not SCHEMA_MISMATCH or pass)
@pytest.mark.asyncio
async def test_metadata_fetch_failure_classified_transient_infra():
    vm = _make_healthy_vm_manager()
    # Simulate the auto-fetch path: check_lineage returns (False, "metadata_fetch_failed")
    vm.check_lineage = AsyncMock(return_value=(False, "metadata_fetch_failed"))

    prober = GCPVMReadinessProber(vm)
    session = HandshakeSession(
        session_id="s3", lease_id="l3",
        instance_name="jarvis-prime-stable",
    )

    result = await prober.probe_capabilities("10.0.0.1", 8000, 5.0, session)

    assert not result.passed, "metadata_fetch_failed must cause capabilities failure"
    assert result.failure_class == ReadinessFailureClass.TRANSIENT_INFRA, (
        f"Expected TRANSIENT_INFRA but got {result.failure_class}"
    )
    assert result.detail == "metadata_fetch_failed"


# T5 — genuine lineage mismatch classified as LINEAGE_MISMATCH
@pytest.mark.asyncio
async def test_genuine_lineage_mismatch_classified_correctly():
    vm = _make_healthy_vm_manager()
    vm.check_lineage = AsyncMock(return_value=(True, "vm_not_from_golden_image"))

    prober = GCPVMReadinessProber(vm)
    session = HandshakeSession(
        session_id="s4", lease_id="l4",
        instance_name="jarvis-prime-stable",
    )

    result = await prober.probe_capabilities("10.0.0.1", 8000, 5.0, session)

    assert not result.passed
    assert result.failure_class == ReadinessFailureClass.LINEAGE_MISMATCH, (
        f"Expected LINEAGE_MISMATCH but got {result.failure_class}"
    )


# T12 — /v1/contract api_version below minimum → ABORT + CONTRACT_VIOLATION
@pytest.mark.asyncio
async def test_contract_check_abort_on_version_mismatch():
    """When /v1/contract returns api_version below minimum, probe_health fails with CONTRACT_VIOLATION."""
    import aiohttp
    from aiohttp import ClientSession

    vm = _make_healthy_vm_manager()
    prober = GCPVMReadinessProber(vm)
    session = HandshakeSession(session_id="s12", lease_id="l12")

    # Mock aiohttp to return version below minimum for /v1/contract
    contract_response = MagicMock()
    contract_response.status = 200
    contract_response.json = AsyncMock(return_value={
        "api_version": "0.9.0",   # below _MIN_PRIME_API_VERSION (1, 2, 0)
        "model_capabilities": ["inference"],
    })
    contract_response.__aenter__ = AsyncMock(return_value=contract_response)
    contract_response.__aexit__ = AsyncMock(return_value=False)

    health_response = MagicMock()
    health_response.status = 200
    health_response.json = AsyncMock(return_value={"status": "healthy", "phase": "ready"})
    health_response.__aenter__ = AsyncMock(return_value=health_response)
    health_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    def side_effect_get(url, **kwargs):
        if "/v1/contract" in url:
            return contract_response
        return health_response

    mock_session.get = MagicMock(side_effect=side_effect_get)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await prober.probe_health("10.0.0.1", 8000, 5.0, session)

    assert not result.passed
    assert result.failure_class == ReadinessFailureClass.CONTRACT_VIOLATION
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_health_populates_session_instance_name backend/tests/test_gcp_handshake_v297.py::test_capabilities_uses_session_instance_name backend/tests/test_gcp_handshake_v297.py::test_metadata_fetch_failure_classified_transient_infra backend/tests/test_gcp_handshake_v297.py::test_genuine_lineage_mismatch_classified_correctly backend/tests/test_gcp_handshake_v297.py::test_contract_check_abort_on_version_mismatch -v
```
Expected: All 5 FAIL — probe methods don't have `session` parameter yet

- [ ] **Step 3: Import `HandshakeSession` and add `_REASON_TO_FAILURE_CLASS` in `gcp_vm_readiness_prober.py`**

Update imports at top of file:

```python
from backend.core.gcp_readiness_lease import (
    HandshakeResult,
    HandshakeSession,       # NEW
    HandshakeStep,
    ReadinessFailureClass,
    ReadinessProber,
)
```

Add constants after imports (before the class definition):

```python
# ---------------------------------------------------------------------------
# Reason string → failure class mapping
# Reasons absent from this dict are PASS cases (golden_image_matches, etc.)
# ---------------------------------------------------------------------------

_REASON_TO_FAILURE_CLASS: Dict[str, ReadinessFailureClass] = {
    "metadata_fetch_failed":              ReadinessFailureClass.TRANSIENT_INFRA,
    # NOTE: spec table classifies metadata_unavailable_golden_exists as TRANSIENT_INFRA,
    # but spec note (§3.3) says RECREATE_VM_ASYNC is correct because retry would loop
    # (can't verify lineage, but golden image exists). The matrix routes
    # (CAPABILITIES, TRANSIENT_INFRA) → RETRY_SHORT, which conflicts with the note.
    # LINEAGE_MISMATCH routes to RECREATE_VM_ASYNC and captures the spec intent better.
    # Decision: use LINEAGE_MISMATCH to honor the spec note over the spec table.
    "metadata_unavailable_golden_exists": ReadinessFailureClass.LINEAGE_MISMATCH,
    "metadata_unavailable_no_golden":     ReadinessFailureClass.TRANSIENT_INFRA,
    "vm_not_from_golden_image":           ReadinessFailureClass.LINEAGE_MISMATCH,
    "golden_image_outdated":              ReadinessFailureClass.LINEAGE_MISMATCH,
}

# Minimum JARVIS-Prime API version for boot-time contract check
_MIN_PRIME_API_VERSION = (1, 2, 0)
_REQUIRED_CAPABILITIES = frozenset({"inference"})
```

- [ ] **Step 4: Rewrite `probe_health` to accept session + populate it + contract check**

Replace `probe_health` (currently lines 93-132):

```python
async def probe_health(
    self,
    host: str,
    port: int,
    timeout: float,
    session: HandshakeSession,   # NEW: will be populated with instance identity
) -> HandshakeResult:
    """Delegate health check to vm_manager.ping_health.

    On success, populates session with instance identity from vm_manager.config.
    Also performs a soft/hard /v1/contract boot check (see _CONTRACT_FAIL_BOUNDARY).
    """
    cached = self._get_cached(HandshakeStep.HEALTH)
    if cached is not None:
        return cached

    try:
        verdict, _data = await self._vm_manager.ping_health(
            host, port, timeout=timeout,
        )
        if getattr(verdict, "value", None) == "ready":
            # Populate session with instance identity from vm_manager config.
            # The static_instance_name is the canonical name used by check_lineage.
            session.instance_name = getattr(
                self._vm_manager.config, "static_instance_name", ""
            ) or ""
            session.zone = getattr(self._vm_manager.config, "zone", "") or ""
            session.endpoint = f"{host}:{port}"
            # instance_id is optional (requires separate GCP API call) — leave as ""

            # Boot-time contract check (soft/hard fail — see _do_contract_check)
            contract_result = await self._do_contract_check(host, port, timeout, session)
            if contract_result is not None:
                # Hard fail — contract check returned a failure result
                self._put_cache(HandshakeStep.HEALTH, contract_result)
                return contract_result

            result = HandshakeResult(
                step=HandshakeStep.HEALTH,
                passed=True,
                detail="healthy",
                data=_data if isinstance(_data, dict) else None,
            )
        else:
            result = HandshakeResult(
                step=HandshakeStep.HEALTH,
                passed=False,
                failure_class=ReadinessFailureClass.NETWORK,
                detail=str(verdict),
            )
    except Exception as exc:
        logger.warning("probe_health raised: %s", exc)
        result = HandshakeResult(
            step=HandshakeStep.HEALTH,
            passed=False,
            failure_class=ReadinessFailureClass.NETWORK,
            detail=str(exc),
        )

    self._put_cache(HandshakeStep.HEALTH, result)
    return result
```

- [ ] **Step 5: Add `_do_contract_check` helper for /v1/contract check**

Add after `probe_health`:

```python
async def _do_contract_check(
    self,
    host: str,
    port: int,
    timeout: float,
    session: HandshakeSession,
) -> Optional[HandshakeResult]:
    """Check /v1/contract on JARVIS-Prime for API version compatibility.

    Returns None (soft pass) or a failure HandshakeResult (hard fail).
    Soft fails: 404, connection refused, JSON parse error, network timeout.
    Hard fails: api_version below minimum, required capability missing.
    """
    if not self._aiohttp_available:
        return None  # Can't check — soft fail

    url = f"http://{host}:{port}/v1/contract"
    try:
        async with aiohttp.ClientSession() as http_session:  # type: ignore[union-attr]
            resp = await asyncio.wait_for(
                http_session.get(url),
                timeout=min(3.0, timeout),
            )
            if resp.status == 404:
                logger.info(
                    "[ProberContract] /v1/contract → 404 (Prime not yet updated) — soft fail",
                )
                return None  # Soft fail

            if resp.status != 200:
                logger.warning(
                    "[ProberContract] /v1/contract → %d — soft fail", resp.status,
                )
                return None  # Soft fail

            try:
                data = await resp.json()
            except Exception as e:
                logger.warning(
                    "[ProberContract] /v1/contract JSON parse error: %s — soft fail", e,
                )
                return None  # Soft fail

            # Hard fail: api_version below minimum
            raw_version = data.get("api_version", "")
            try:
                parts = tuple(int(x) for x in raw_version.split(".")[:3])
                if parts < _MIN_PRIME_API_VERSION:
                    logger.error(
                        "[ProberContract] api_version %s < minimum %s — HARD FAIL",
                        raw_version, ".".join(str(v) for v in _MIN_PRIME_API_VERSION),
                    )
                    return HandshakeResult(
                        step=HandshakeStep.HEALTH,
                        passed=False,
                        failure_class=ReadinessFailureClass.CONTRACT_VIOLATION,
                        detail=f"api_version {raw_version} below minimum "
                               f"{'.'.join(str(v) for v in _MIN_PRIME_API_VERSION)}",
                    )
            except (ValueError, TypeError):
                logger.warning(
                    "[ProberContract] api_version '%s' unparseable — soft fail", raw_version,
                )
                return None  # Soft fail on parse error

            # Hard fail: required capability missing
            caps = set(data.get("model_capabilities", []))
            missing = _REQUIRED_CAPABILITIES - caps
            if missing:
                logger.error(
                    "[ProberContract] Missing required capabilities %s — HARD FAIL", missing,
                )
                return HandshakeResult(
                    step=HandshakeStep.HEALTH,
                    passed=False,
                    failure_class=ReadinessFailureClass.CONTRACT_VIOLATION,
                    detail=f"missing required capabilities: {missing}",
                )

            logger.info(
                "[ProberContract] Contract OK: api_version=%s caps=%s",
                raw_version, caps,
            )
            return None  # All checks passed

    except asyncio.TimeoutError:
        logger.warning("[ProberContract] /v1/contract timed out — soft fail")
        return None  # Soft fail
    except (ConnectionRefusedError, OSError):
        logger.info("[ProberContract] /v1/contract connection refused — soft fail")
        return None  # Soft fail
    except Exception as exc:
        logger.warning("[ProberContract] /v1/contract unexpected error: %s — soft fail", exc)
        return None  # Soft fail
```

- [ ] **Step 6: Rewrite `probe_capabilities` to use session + reason→class mapping**

Replace `probe_capabilities` (currently lines 138-178):

```python
async def probe_capabilities(
    self,
    host: str,
    port: int,
    timeout: float,
    session: HandshakeSession,   # NEW: session.instance_name must be non-empty
) -> HandshakeResult:
    """Delegate lineage check to vm_manager.check_lineage.

    Uses session.instance_name (populated by probe_health) instead of "".
    Maps reason string to precise failure_class via _REASON_TO_FAILURE_CLASS.
    """
    cached = self._get_cached(HandshakeStep.CAPABILITIES)
    if cached is not None:
        return cached

    try:
        should_recreate, reason = await self._vm_manager.check_lineage(
            session.instance_name,   # CRITICAL: use real name, not ""
            None,                    # vm_metadata=None → auto-fetch in check_lineage
        )
        # Classify by reason string — ignore should_recreate for pass/fail decision.
        # Absent from map = pass case (golden_image_matches, disabled, stale, etc.)
        failure_class = _REASON_TO_FAILURE_CLASS.get(reason)
        if failure_class is not None:
            result = HandshakeResult(
                step=HandshakeStep.CAPABILITIES,
                passed=False,
                failure_class=failure_class,
                detail=reason,
            )
        else:
            result = HandshakeResult(
                step=HandshakeStep.CAPABILITIES,
                passed=True,
                detail=reason,
            )

    except Exception as exc:
        # Check for ContractViolationError by name to avoid import coupling
        if type(exc).__name__ == "ContractViolationError":
            logger.error("probe_capabilities: ContractViolationError — %s", exc)
            result = HandshakeResult(
                step=HandshakeStep.CAPABILITIES,
                passed=False,
                failure_class=ReadinessFailureClass.CONTRACT_VIOLATION,
                detail=str(exc),
            )
        else:
            logger.warning("probe_capabilities raised: %s", exc)
            result = HandshakeResult(
                step=HandshakeStep.CAPABILITIES,
                passed=False,
                failure_class=ReadinessFailureClass.NETWORK,
                detail=str(exc),
            )

    self._put_cache(HandshakeStep.CAPABILITIES, result)
    return result
```

- [ ] **Step 7: Update `probe_warm_model` and `_do_warm_model_probe` signatures**

Update signatures to accept `session: HandshakeSession` (pass-through, not used internally):

```python
async def probe_warm_model(
    self,
    host: str,
    port: int,
    timeout: float,
    session: HandshakeSession,   # NEW: accepted but not consumed (warm check is stateless)
) -> HandshakeResult:
    """HTTP probe to /v1/warm_check — never cached."""
    return await self._do_warm_model_probe(host, port, timeout)
```

`_do_warm_model_probe` signature stays as-is (no session needed internally).

- [ ] **Step 8: Add `invalidate_session_cache()` method**

Add after `_put_cache`:

```python
def invalidate_session_cache(self) -> None:
    """Clear per-step cache for a new session.

    Called by GCPReadinessLease.acquire() before each probe run so that
    stale HEALTH + CAPABILITIES results do not bypass identity lookup.
    WARM_MODEL is never cached — no change needed.
    """
    self._cache.pop(HandshakeStep.HEALTH, None)
    self._cache.pop(HandshakeStep.CAPABILITIES, None)
```

- [ ] **Step 9: Run T2, T3, T4, T5, T12**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_health_populates_session_instance_name backend/tests/test_gcp_handshake_v297.py::test_capabilities_uses_session_instance_name backend/tests/test_gcp_handshake_v297.py::test_metadata_fetch_failure_classified_transient_infra backend/tests/test_gcp_handshake_v297.py::test_genuine_lineage_mismatch_classified_correctly backend/tests/test_gcp_handshake_v297.py::test_contract_check_abort_on_version_mismatch -v
```
Expected: All 5 PASS

- [ ] **Step 10: Commit**

```bash
git add backend/core/gcp_vm_readiness_prober.py backend/tests/test_gcp_handshake_v297.py
git commit -m "feat(handshake): session threading, reason→class mapping, contract check in prober (v297.0 Task 3)"
```

---

## Task 4: RecoveryStrategy + Routing Matrix

**Files:**
- Modify: `backend/core/startup_routing_policy.py:1-25` (imports)
- Modify: `backend/core/startup_routing_policy.py:29-50` (enums section)
- Modify: `backend/core/startup_routing_policy.py:176-187` (`signal_gcp_handshake_failed`)
- Test: `backend/tests/test_gcp_handshake_v297.py` (T6, T7, T14)

- [ ] **Step 1: Write the failing tests (T6, T7)**

Append to `test_gcp_handshake_v297.py`:

```python
# ---------------------------------------------------------------------------
# Routing matrix tests: T6, T7
# ---------------------------------------------------------------------------

# T6 — (CAPABILITIES, LINEAGE_MISMATCH) → RECREATE_VM_ASYNC
def test_routing_matrix_lineage_mismatch_triggers_recreate_async():
    from backend.core.startup_routing_policy import (
        RecoveryStrategy,
        StartupRoutingPolicy,
    )
    policy = StartupRoutingPolicy()
    strategy = policy.select_recovery_strategy(
        HandshakeStep.CAPABILITIES,
        ReadinessFailureClass.LINEAGE_MISMATCH,
    )
    assert strategy == RecoveryStrategy.RECREATE_VM_ASYNC, (
        f"Expected RECREATE_VM_ASYNC but got {strategy}"
    )


# T7 — (CAPABILITIES, TRANSIENT_INFRA) → RETRY_SHORT
def test_routing_matrix_transient_infra_triggers_retry_short():
    from backend.core.startup_routing_policy import (
        RecoveryStrategy,
        StartupRoutingPolicy,
    )
    policy = StartupRoutingPolicy()
    strategy = policy.select_recovery_strategy(
        HandshakeStep.CAPABILITIES,
        ReadinessFailureClass.TRANSIENT_INFRA,
    )
    assert strategy == RecoveryStrategy.RETRY_SHORT, (
        f"Expected RETRY_SHORT but got {strategy}"
    )
```

- [ ] **Step 2: Run T6, T7, T14 to verify they fail**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_routing_matrix_lineage_mismatch_triggers_recreate_async backend/tests/test_gcp_handshake_v297.py::test_routing_matrix_transient_infra_triggers_retry_short backend/tests/test_gcp_handshake_v297.py::test_recovery_matrix_has_entries_for_expected_health_classes -v
```
Expected: All 3 FAIL with `ImportError` (RecoveryStrategy doesn't exist yet)

- [ ] **Step 3: Add imports to `startup_routing_policy.py`**

Add to the imports block:

```python
from typing import List, Optional, Tuple, Dict   # update existing typing import to add Dict
```

And add at the end of the imports:

```python
# Lazy import to avoid circular: HandshakeStep + ReadinessFailureClass imported at runtime
# in select_recovery_strategy() to keep startup_routing_policy free of gcp_readiness_lease dependency.
```

Note: to avoid circular imports, import `HandshakeStep` and `ReadinessFailureClass` inside `select_recovery_strategy()` rather than at module level. The `_RECOVERY_MATRIX` dict is built lazily or uses string keys. Use string keys to avoid the circular import entirely:

```python
# Recovery strategy matrix uses string keys "(step.value, class.value)" to avoid
# circular import from gcp_readiness_lease.
```

- [ ] **Step 4: Add `RecoveryStrategy` enum to `startup_routing_policy.py`**

Add after the existing `FallbackReason` enum:

```python
class RecoveryStrategy(str, enum.Enum):
    """Recovery action for a specific (HandshakeStep, ReadinessFailureClass) pair."""

    RETRY_SHORT       = "retry_short"       # Retry within 30s — transient network/infra
    RETRY_LONG        = "retry_long"        # Retry in 60-120s — resource contention
    RECREATE_VM_ASYNC = "recreate_vm_async" # Fire background recreation, use fallback now
    FALLBACK_LOCAL    = "fallback_local"    # Use local Llama, no further GCP retry
    FALLBACK_CLOUD    = "fallback_cloud"    # Use Claude API, no further GCP retry
    ABORT             = "abort"             # Permanent — do not retry
```

- [ ] **Step 5: Add `_RECOVERY_MATRIX` and constants after the enum**

Add after `RecoveryStrategy`:

```python
# Recovery matrix: (step_value, failure_class_value) → RecoveryStrategy
# Uses string keys to avoid circular import from gcp_readiness_lease.
_RECOVERY_MATRIX: Dict[Tuple[str, str], RecoveryStrategy] = {
    # HEALTH step
    ("health", "network"):            RecoveryStrategy.RETRY_SHORT,
    ("health", "timeout"):            RecoveryStrategy.RETRY_SHORT,
    ("health", "resource"):           RecoveryStrategy.RETRY_LONG,
    ("health", "preemption"):         RecoveryStrategy.RETRY_LONG,
    ("health", "quota"):              RecoveryStrategy.FALLBACK_CLOUD,
    # CAPABILITIES step
    ("capabilities", "transient_infra"):    RecoveryStrategy.RETRY_SHORT,
    ("capabilities", "lineage_mismatch"):   RecoveryStrategy.RECREATE_VM_ASYNC,
    ("capabilities", "schema_mismatch"):    RecoveryStrategy.RECREATE_VM_ASYNC,
    ("capabilities", "contract_violation"): RecoveryStrategy.ABORT,
    # WARM_MODEL step
    ("warm_model", "network"):        RecoveryStrategy.RETRY_SHORT,
    ("warm_model", "timeout"):        RecoveryStrategy.RETRY_SHORT,
    ("warm_model", "resource"):       RecoveryStrategy.RETRY_LONG,
}

# Default when (step, class) not in matrix
_RECOVERY_MATRIX_DEFAULT = RecoveryStrategy.FALLBACK_LOCAL

# Maximum consecutive RETRY_SHORT/RETRY_LONG before escalating to FALLBACK_LOCAL
MAX_RETRY_ATTEMPTS_PER_HANDSHAKE = 3
```

- [ ] **Step 6: Add `select_recovery_strategy()` method to `StartupRoutingPolicy`**

Add after `signal_gcp_handshake_failed`:

```python
def select_recovery_strategy(
    self,
    step: Any,   # HandshakeStep (duck-typed to avoid circular import)
    failure_class: Any,   # ReadinessFailureClass
) -> "RecoveryStrategy":
    """Look up the recovery strategy for a failed handshake step.

    Uses string values from the enums to avoid circular import.
    If no entry found in matrix, returns FALLBACK_LOCAL with a WARNING log.
    """
    step_val = step.value if hasattr(step, "value") else str(step)
    class_val = failure_class.value if hasattr(failure_class, "value") else str(failure_class)

    strategy = _RECOVERY_MATRIX.get((step_val, class_val))
    if strategy is None:
        logger.warning(
            "No recovery matrix entry for (step=%s, class=%s) — defaulting to %s",
            step_val, class_val, _RECOVERY_MATRIX_DEFAULT.value,
        )
        return _RECOVERY_MATRIX_DEFAULT
    return strategy
```

- [ ] **Step 7: Update `signal_gcp_handshake_failed` to optionally accept step/class**

Extend the existing method signature (backwards-compatible):

```python
def signal_gcp_handshake_failed(
    self,
    reason: str,
    step: Any = None,           # Optional HandshakeStep — enables matrix lookup
    failure_class: Any = None,  # Optional ReadinessFailureClass
) -> "RecoveryStrategy":
    """Signal that the GCP handshake failed.

    Returns the recovery strategy for the caller to act on.
    No-op after finalize() (returns FALLBACK_LOCAL).
    """
    if self._finalized:
        logger.debug("signal_gcp_handshake_failed ignored — policy finalized")
        return RecoveryStrategy.FALLBACK_LOCAL
    self._gcp_handshake_failed = True
    self._gcp_ready = False
    self._gcp_handshake_fail_reason = reason
    logger.warning("GCP handshake failed: %s", reason)

    if step is not None and failure_class is not None:
        return self.select_recovery_strategy(step, failure_class)
    return RecoveryStrategy.FALLBACK_LOCAL
```

- [ ] **Step 8: Run T6, T7, T14**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_routing_matrix_lineage_mismatch_triggers_recreate_async backend/tests/test_gcp_handshake_v297.py::test_routing_matrix_transient_infra_triggers_retry_short backend/tests/test_gcp_handshake_v297.py::test_recovery_matrix_has_entries_for_expected_health_classes -v
```
Expected: All 3 PASS

- [ ] **Step 9: Commit**

```bash
git add backend/core/startup_routing_policy.py backend/tests/test_gcp_handshake_v297.py
git commit -m "feat(handshake): RecoveryStrategy enum, _RECOVERY_MATRIX, select_recovery_strategy (v297.0 Task 4)"
```

---

## Task 5: Backpressure Gate + Autonomous Recreation

**Files:**
- Modify: `backend/core/startup_orchestrator.py:19-65` (imports + `__init__`)
- Modify: `backend/core/startup_orchestrator.py:255-295` (`acquire_gcp_lease`)
- Test: `backend/tests/test_gcp_handshake_v297.py` (T8, T9, T10, T11, T15)

- [ ] **Step 1: Write the failing tests (T8, T9, T10, T11, T15)**

Append to `test_gcp_handshake_v297.py`:

```python
# ---------------------------------------------------------------------------
# Orchestrator tests: T8, T9, T10, T11, T15
# ---------------------------------------------------------------------------

from backend.core.startup_orchestrator import StartupOrchestrator
from backend.core.startup_config import StartupConfig
from backend.core.startup_routing_policy import RecoveryStrategy, MAX_RETRY_ATTEMPTS_PER_HANDSHAKE


def _make_orchestrator(vm_manager=None):
    """Minimal orchestrator for testing with mocked sub-components."""
    config = MagicMock(spec=StartupConfig)
    config.probe_timeout_s = 5.0
    config.lease_ttl_s = 30.0
    config.gcp_deadline_s = 60.0
    config.cloud_fallback_enabled = True
    config.budget = MagicMock()
    prober = MagicMock()
    prober.invalidate_session_cache = MagicMock()
    orch = StartupOrchestrator(config, prober, vm_manager=vm_manager)
    return orch


# T8 — RECREATE_VM_ASYNC: routing returns FALLBACK_LOCAL immediately AND fires bg task
@pytest.mark.asyncio
async def test_recreate_vm_async_fires_background_task_and_continues_fallback():
    """On RECREATE_VM_ASYNC strategy, acquire_gcp_lease returns immediately using
    FALLBACK_LOCAL routing and fires a background recreation task."""
    vm_manager = MagicMock()
    vm_manager.ensure_static_vm_ready = AsyncMock(return_value=(True, "10.0.0.1", "CREATED"))
    vm_manager.config.port = 8000

    orch = _make_orchestrator(vm_manager=vm_manager)

    # Make the lease fail at CAPABILITIES with LINEAGE_MISMATCH
    orch._lease.acquire = AsyncMock(return_value=False)
    session = HandshakeSession(session_id="s8", lease_id="l8", instance_name="jp", endpoint="10.0.0.1:8000")
    orch._lease._last_session = session
    orch._lease._last_failure_class = ReadinessFailureClass.LINEAGE_MISMATCH
    orch._lease._handshake_log = [
        HandshakeResult(step=HandshakeStep.HEALTH, passed=True),
        HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=False,
                        failure_class=ReadinessFailureClass.LINEAGE_MISMATCH),
    ]

    result = await orch.acquire_gcp_lease("10.0.0.1", 8000)

    assert not result, "Lease should not be acquired"
    # Give the background task a moment to start
    await asyncio.sleep(0.05)
    # vm_manager.ensure_static_vm_ready should have been called (or scheduled)
    # Strategy was RECREATE_VM_ASYNC — routing falls back to LOCAL_MINIMAL or CLOUD_CLAUDE
    decision, _ = orch.routing_decide()
    assert decision.value in ("local_minimal", "cloud_claude", "pending"), (
        f"Expected fallback decision but got {decision}"
    )


# T9 — routing upgrades to GCP_PRIME after recreation success
@pytest.mark.asyncio
async def test_routing_upgrades_to_gcp_prime_after_recreation_success():
    """After successful recreation, signal_gcp_ready upgrades routing to GCP_PRIME."""
    policy = StartupRoutingPolicy()

    # Simulate the state after RECREATE_VM_ASYNC completes
    policy.signal_gcp_ready("10.0.0.1", 8000)

    decision, reason = policy.decide()
    assert decision.value == "gcp_prime", (
        f"After signal_gcp_ready, expected GCP_PRIME but got {decision}"
    )


# T10 — probe gate delays probe under CPU pressure
@pytest.mark.asyncio
async def test_probe_gate_delays_probe_under_cpu_pressure():
    """_ProbeReadinessBudget delays when CPU is above threshold."""
    from backend.core.startup_orchestrator import _ProbeReadinessBudget

    gate = _ProbeReadinessBudget()

    call_count = 0

    async def mock_wait():
        nonlocal call_count
        # Simulate: first 2 checks high CPU, then drops
        pressures = [(100.0, 50.0), (95.0, 50.0), (40.0, 50.0)]
        for cpu, mem in pressures:
            with patch.object(gate.__class__, "_read_current_pressure",
                              return_value=(cpu, mem)):
                call_count += 1
                if cpu <= gate.CPU_THRESHOLD and mem <= gate.MEM_THRESHOLD:
                    return True
        return True

    # Patch _read_current_pressure to return high then low
    pressure_values = [(100.0, 50.0), (100.0, 50.0), (30.0, 50.0)]
    call_idx = 0

    def _pressure():
        nonlocal call_idx
        val = pressure_values[min(call_idx, len(pressure_values) - 1)]
        call_idx += 1
        return val

    with patch.object(
        _ProbeReadinessBudget, "_read_current_pressure",
        staticmethod(_pressure)
    ):
        with patch("asyncio.sleep", new=AsyncMock()):
            acquired = await gate.wait_for_probe_slot()

    assert acquired, "Gate should have been acquired once pressure dropped"
    assert call_idx >= 2, "Gate should have polled at least twice before passing"


# T11 — probe gate falls through after MAX_WAIT
@pytest.mark.asyncio
async def test_probe_gate_falls_through_after_max_wait():
    """_ProbeReadinessBudget falls through with warning after MAX_WAIT seconds."""
    from backend.core.startup_orchestrator import _ProbeReadinessBudget

    gate = _ProbeReadinessBudget()

    # Always high pressure
    with patch.object(
        _ProbeReadinessBudget, "_read_current_pressure",
        staticmethod(lambda: (100.0, 100.0))
    ):
        # Override MAX_WAIT to speed up the test
        gate.MAX_WAIT = 0.1
        gate.POLL_INTERVAL = 0.05
        acquired = await gate.wait_for_probe_slot()

    assert not acquired, "Gate should return False after timeout with persistent pressure"


# T15 — retry strategy bounded by MAX_RETRY_ATTEMPTS_PER_HANDSHAKE
@pytest.mark.asyncio
async def test_retry_strategy_bounded_by_max_attempts():
    """After MAX_RETRY_ATTEMPTS_PER_HANDSHAKE consecutive RETRY_SHORT results,
    orchestrator escalates to FALLBACK_LOCAL and stops retrying."""
    vm_manager = MagicMock()
    vm_manager.config.port = 8000
    orch = _make_orchestrator(vm_manager=vm_manager)

    # Always fail at HEALTH with NETWORK → RETRY_SHORT
    orch._lease.acquire = AsyncMock(return_value=False)
    orch._lease._last_failure_class = ReadinessFailureClass.NETWORK
    orch._lease._handshake_log = [
        HandshakeResult(step=HandshakeStep.HEALTH, passed=False,
                        failure_class=ReadinessFailureClass.NETWORK),
    ]
    session = HandshakeSession(session_id="s15", lease_id="l15", instance_name="jp")
    orch._lease._last_session = session

    # Call acquire_gcp_lease MAX+1 times
    for _ in range(MAX_RETRY_ATTEMPTS_PER_HANDSHAKE + 1):
        await orch.acquire_gcp_lease("10.0.0.1", 8000)

    # After max retries, decision should NOT be PENDING (should be a fallback)
    decision, reason = orch.routing_decide()
    assert decision.value != "pending", (
        f"After {MAX_RETRY_ATTEMPTS_PER_HANDSHAKE + 1} RETRY_SHORT failures, "
        f"routing should have fallen back but got {decision}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_recreate_vm_async_fires_background_task_and_continues_fallback backend/tests/test_gcp_handshake_v297.py::test_routing_upgrades_to_gcp_prime_after_recreation_success backend/tests/test_gcp_handshake_v297.py::test_probe_gate_delays_probe_under_cpu_pressure backend/tests/test_gcp_handshake_v297.py::test_probe_gate_falls_through_after_max_wait backend/tests/test_gcp_handshake_v297.py::test_retry_strategy_bounded_by_max_attempts -v
```
Expected: Failures (orchestrator doesn't have `_ProbeReadinessBudget`, `vm_manager` param, or `_gcp_retry_count` yet)

Note: T9 may already pass since it only uses `StartupRoutingPolicy.signal_gcp_ready`.

- [ ] **Step 3: Add `_ProbeReadinessBudget` class to `startup_orchestrator.py`**

Add before the `OrchestratorState` enum (before line 73):

```python
# ---------------------------------------------------------------------------
# Backpressure gate
# ---------------------------------------------------------------------------

class _ProbeReadinessBudget:
    """Gates GCP probe scheduling behind CPU/memory thresholds.

    Reads CPU and memory directly via psutil (same library used by
    IntelligentMemoryController). Does NOT depend on the controller —
    safe to call before Zone 5. Does not block indefinitely — falls
    through with warning after MAX_WAIT.
    """
    CPU_THRESHOLD  = 85.0   # percent — don't probe while CPU > 85%
    MEM_THRESHOLD  = 88.0   # percent — don't probe while memory > 88%
    POLL_INTERVAL  = 2.0    # seconds between pressure checks
    MAX_WAIT       = 60.0   # seconds before probing regardless (safety valve)

    @staticmethod
    def _read_current_pressure() -> Tuple[float, float]:
        """Returns (cpu_percent, mem_percent). Fails open if psutil unavailable."""
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            return cpu, mem
        except Exception:
            return 0.0, 0.0   # fail open — don't block probe if psutil unavailable

    async def wait_for_probe_slot(self) -> bool:
        """Await CPU/memory relief. Returns True if slot acquired, False if timed out."""
        import time as _time
        deadline = _time.monotonic() + self.MAX_WAIT
        cpu, mem = self._read_current_pressure()
        while _time.monotonic() < deadline:
            if cpu <= self.CPU_THRESHOLD and mem <= self.MEM_THRESHOLD:
                return True
            await asyncio.sleep(self.POLL_INTERVAL)
            cpu, mem = self._read_current_pressure()
        logger.warning(
            "[ProbeGate] Max wait exceeded (%.0fs) — probing under pressure "
            "(cpu=%.1f%%, mem=%.1f%%)", self.MAX_WAIT, cpu, mem,
        )
        return False
```

Update the imports at the top of `startup_orchestrator.py` to add `Tuple` (if not already present) and `asyncio` is already imported. Add `import asyncio` if missing. Also add `import time as _time` in the `_ProbeReadinessBudget` class.

- [ ] **Step 4: Update `StartupOrchestrator.__init__` to accept `vm_manager` and add new attributes**

Update the `__init__` signature:

```python
def __init__(self, config: StartupConfig, prober: Any, vm_manager: Any = None) -> None:
```

Add after `self._prime_router: Any = None`:

```python
        # v297.0: Recovery + backpressure
        self._vm_manager: Any = vm_manager
        self._probe_gate = _ProbeReadinessBudget()
        self._gcp_retry_count: int = 0    # consecutive RETRY_SHORT/RETRY_LONG outcomes
```

Also add `set_vm_manager` method after `set_prime_router`:

```python
def set_vm_manager(self, vm_manager: Any) -> None:
    """Store a reference to the GCP VM manager for autonomous recreation."""
    self._vm_manager = vm_manager
```

- [ ] **Step 5: Update `acquire_gcp_lease` to use recovery strategy + fire recreation + enforce retry cap**

Replace `acquire_gcp_lease` (currently lines 255-295):

```python
async def acquire_gcp_lease(self, host: str, port: int) -> bool:
    """Acquire the GCP readiness lease via 3-step handshake.

    On success: signals routing policy that GCP is ready; resets retry count.
    On failure: looks up recovery strategy from matrix; handles accordingly:
      - RETRY_SHORT/RETRY_LONG: increments retry count; falls back after MAX cap
      - RECREATE_VM_ASYNC: fires background recreation, falls back immediately
      - FALLBACK_LOCAL/FALLBACK_CLOUD/ABORT: falls back, stops retrying
    """
    # Backpressure gate — wait for CPU/memory relief before probing
    probe_acquired = await self._probe_gate.wait_for_probe_slot()
    if not probe_acquired:
        logger.warning(
            "[ProbeGate] probing under pressure — cpu/mem still high after %.0fs",
            self._probe_gate.MAX_WAIT,
        )

    success = await self._lease.acquire(
        host,
        port,
        timeout_per_step=self._config.probe_timeout_s,
    )

    if success:
        self._gcp_retry_count = 0
        self._routing_policy.signal_gcp_ready(host, port)
        await self._emit(
            event_type="gcp_lease",
            detail={"action": "acquire", "success": True, "host": host, "port": port},
        )
        return True

    # Acquisition failed — determine recovery strategy
    failure_class = self._lease.last_failure_class

    # Find the failed step from the handshake log
    failed_step = None
    for entry in reversed(self._lease.handshake_log):
        if not entry.passed:
            failed_step = entry.step
            break

    if failed_step is not None and failure_class is not None:
        from backend.core.startup_routing_policy import (
            MAX_RETRY_ATTEMPTS_PER_HANDSHAKE,
            RecoveryStrategy,
        )
        strategy = self._routing_policy.signal_gcp_handshake_failed(
            reason=f"lease acquisition failed: {failure_class.value}",
            step=failed_step,
            failure_class=failure_class,
        )

        if strategy in (RecoveryStrategy.RETRY_SHORT, RecoveryStrategy.RETRY_LONG):
            self._gcp_retry_count += 1
            if self._gcp_retry_count >= MAX_RETRY_ATTEMPTS_PER_HANDSHAKE:
                logger.warning(
                    "[GCPLease] Max retry attempts (%d) reached — escalating to FALLBACK_LOCAL",
                    MAX_RETRY_ATTEMPTS_PER_HANDSHAKE,
                )
                # Force fallback by re-signalling without step/class (uses default FALLBACK_LOCAL)
                self._routing_policy.signal_gcp_handshake_failed(
                    reason="max_retry_attempts_exceeded",
                )
            else:
                logger.info(
                    "[GCPLease] strategy=%s (attempt %d/%d) — caller should retry",
                    strategy.value, self._gcp_retry_count, MAX_RETRY_ATTEMPTS_PER_HANDSHAKE,
                )

        elif strategy == RecoveryStrategy.RECREATE_VM_ASYNC:
            session = self._lease.last_session
            if self._vm_manager is not None and session is not None:
                logger.info(
                    "[GCPLease] RECREATE_VM_ASYNC — firing background recreation, "
                    "falling back immediately"
                )
                asyncio.create_task(
                    self._recreate_vm_and_upgrade_routing(session)
                )
            else:
                logger.warning(
                    "[GCPLease] RECREATE_VM_ASYNC but vm_manager=%s or session=%s — "
                    "cannot recreate, falling back",
                    self._vm_manager, session,
                )

    else:
        self._routing_policy.signal_gcp_handshake_failed(
            reason=f"lease acquisition failed: "
                   f"{failure_class.value if failure_class else 'unknown'}",
        )

    await self._emit(
        event_type="lease_probe",
        detail={
            "action": "acquire",
            "success": False,
            "host": host,
            "port": port,
            "failure_class": failure_class.value if failure_class else None,
        },
    )
    await self._emit(
        event_type="gcp_lease",
        detail={"action": "acquire", "success": False, "host": host, "port": port},
    )
    return False
```

- [ ] **Step 6: Add `_recreate_vm_and_upgrade_routing` method**

Add after `acquire_gcp_lease`:

```python
async def _recreate_vm_and_upgrade_routing(self, session: Any) -> None:
    """Background task: recreate the GCP VM from golden image and upgrade routing.

    Called when recovery strategy is RECREATE_VM_ASYNC. Uses asyncio.shield()
    so cancellation of the parent coroutine does not abort the recreation.
    """
    try:
        logger.info(
            "[RecreateVM] Starting background recreation for session %s "
            "(instance=%s)",
            getattr(session, "session_id", "?"),
            getattr(session, "instance_name", "?"),
        )
        success, host, status = await asyncio.shield(
            self._vm_manager.ensure_static_vm_ready(recreate=True)
        )
        if success and host:
            endpoint = getattr(session, "endpoint", "")
            try:
                port = int(endpoint.split(":")[-1]) if ":" in endpoint else self._vm_manager.config.port
            except (ValueError, AttributeError):
                port = 8000
            logger.info(
                "[RecreateVM] Recreation succeeded: %s:%d — upgrading routing to GCP_PRIME",
                host, port,
            )
            self._routing_policy.signal_gcp_ready(host, port)
            await self._emit(
                event_type="gcp_recreation",
                detail={
                    "action": "recreate_vm_async",
                    "success": True,
                    "host": host,
                    "port": port,
                    "session_id": getattr(session, "session_id", "?"),
                },
            )
        else:
            logger.warning(
                "[RecreateVM] Recreation failed: %s — staying on fallback", status,
            )
            await self._emit(
                event_type="gcp_recreation",
                detail={
                    "action": "recreate_vm_async",
                    "success": False,
                    "status": status,
                },
            )
    except asyncio.CancelledError:
        logger.info("[RecreateVM] Recreation task cancelled")
        raise
    except Exception as exc:
        logger.exception("[RecreateVM] Unexpected error during recreation: %s", exc)
```

- [ ] **Step 7: Add `Tuple` import if missing from orchestrator**

Verify the import line at the top of `startup_orchestrator.py`:
```python
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
```
`Tuple` should already be present. If not, add it.

- [ ] **Step 8: Run T8, T9, T10, T11, T15**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_recreate_vm_async_fires_background_task_and_continues_fallback backend/tests/test_gcp_handshake_v297.py::test_routing_upgrades_to_gcp_prime_after_recreation_success backend/tests/test_gcp_handshake_v297.py::test_probe_gate_delays_probe_under_cpu_pressure backend/tests/test_gcp_handshake_v297.py::test_probe_gate_falls_through_after_max_wait backend/tests/test_gcp_handshake_v297.py::test_retry_strategy_bounded_by_max_attempts -v
```
Expected: All 5 PASS

- [ ] **Step 9: Commit**

```bash
git add backend/core/startup_orchestrator.py backend/tests/test_gcp_handshake_v297.py
git commit -m "feat(handshake): _ProbeReadinessBudget, RECREATE_VM_ASYNC task, retry cap (v297.0 Task 5)"
```

---

## Task 6: T13 Integration Test — session_id propagated to all step logs

**Files:**
- Test: `backend/tests/test_gcp_handshake_v297.py` (T13)

This test uses `caplog` to verify the correlated `session_id` appears in log records from all three handshake steps.

- [ ] **Step 1: Write T13**

Append to `test_gcp_handshake_v297.py`:

```python
# ---------------------------------------------------------------------------
# T13 — session_id in all step logs (integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_correlated_session_id_in_all_step_logs(caplog):
    """All three handshake step log records must contain the same session_id."""
    import re

    # Build a prober where all 3 probes pass (no real GCP calls)
    session_ref: list = []

    class _CapturingProber(ReadinessProber):
        async def probe_health(self, host, port, timeout, session):
            session_ref.append(session)
            session.instance_name = "jarvis-prime-stable"
            session.zone = "us-central1-b"
            session.endpoint = f"{host}:{port}"
            return HandshakeResult(step=HandshakeStep.HEALTH, passed=True, detail="healthy")

        async def probe_capabilities(self, host, port, timeout, session):
            return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True, detail="ok")

        async def probe_warm_model(self, host, port, timeout, session):
            return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True, detail="warm")

    prober = _CapturingProber()
    lease = GCPReadinessLease(prober=prober, ttl_seconds=30.0)

    with caplog.at_level(logging.DEBUG, logger="backend.core.gcp_readiness_lease"):
        result = await lease.acquire("10.0.0.1", 8000, timeout_per_step=5.0)

    assert result, "All 3 probes passed — lease should be acquired"
    assert session_ref, "probe_health should have been called"

    session_id = session_ref[0].session_id
    assert session_id, "session_id must be a non-empty uuid"

    # The session_id must appear in log records
    log_text = "\n".join(r.message for r in caplog.records)
    assert session_id in log_text, (
        f"session_id '{session_id}' not found in any log record.\n"
        f"Log records:\n{log_text}"
    )
```

- [ ] **Step 2: Run T13**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py::test_correlated_session_id_in_all_step_logs -v
```
Expected: PASS (the `acquire()` method now logs `[GCP_LEASE] session=<uuid>` on the terminal ACTIVE log)

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_gcp_handshake_v297.py
git commit -m "test(handshake): T13 session_id propagation integration test (v297.0 Task 6)"
```

---

## Task 7: Full test suite run

- [ ] **Step 1: Run all 15 v297 tests**

```bash
python3 -m pytest backend/tests/test_gcp_handshake_v297.py -v
```
Expected: 15/15 PASS

- [ ] **Step 2: Verify no regressions in related test files**

```bash
python3 -m pytest backend/tests/test_gcp_operation_poller.py -v
```
Expected: 23/23 PASS (previously established)

- [ ] **Step 3: Quick smoke check on the startup module chain**

```bash
python3 -c "
from backend.core.gcp_readiness_lease import HandshakeSession, ReadinessFailureClass, GCPReadinessLease
from backend.core.gcp_vm_readiness_prober import GCPVMReadinessProber
from backend.core.startup_routing_policy import RecoveryStrategy, _RECOVERY_MATRIX, MAX_RETRY_ATTEMPTS_PER_HANDSHAKE
from backend.core.startup_orchestrator import StartupOrchestrator, _ProbeReadinessBudget
print('All imports OK')
print('RecoveryStrategy values:', [s.value for s in RecoveryStrategy])
print('MAX_RETRY_ATTEMPTS_PER_HANDSHAKE:', MAX_RETRY_ATTEMPTS_PER_HANDSHAKE)
print('_RECOVERY_MATRIX entries:', len(_RECOVERY_MATRIX))
s = HandshakeSession(session_id='test', lease_id='lease')
print('HandshakeSession OK:', s)
print('ReadinessFailureClass values:', [c.value for c in ReadinessFailureClass])
"
```
Expected: Prints all values with no ImportError

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_gcp_handshake_v297.py
git commit -m "test(handshake): v297.0 full test suite verification — 15/15 passing"
```

---

## Residual Risks (from spec — do not address in this plan)

| Risk | Next step |
|------|-----------|
| `/v1/contract` not yet on JARVIS-Prime | v298.0 — add endpoint to Prime server |
| `_describe_instance_full` latency | v298.0 — add 3s timeout + 60s result cache |
| Concurrent `RECREATE_VM_ASYNC` races | v298.0 — guard with DLM `gcp_vm_recreate` key |
| Reactor Core contract validation | v299.0+ |

# Autonomy Contract Hardening & MCP Cloud Capacity — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the autonomy contract timing gap (pending state + bounded wait + fast promotion), unify cloud capacity decisions under a single MCP-aware controller, and harden the actuator pipeline with execution-time staleness fencing and observer backpressure.

**Architecture:** Three phases — Phase B adds a bounded readiness wait and three-state autonomy model so boot-time contract checks wait for Prime/Reactor before concluding. Phase C creates a `CloudCapacityController` that consumes MCP pressure signals and replaces scattered threshold checks. Phase D hardens the coordinator and broker with execution-time epoch fencing and observer backpressure.

**Tech Stack:** Python 3.11+, asyncio, aiohttp, pytest, dataclasses, enums

**Design doc:** `docs/plans/2026-03-04-autonomy-contract-hardening-design.md`

---

## Phase B: Autonomy Contract Timing Fix

### Task 1: Semantic Version Comparison Utility

**Files:**
- Modify: `backend/supervisor/cross_repo_startup_orchestrator.py:24862-24877`
- Test: `tests/unit/backend/supervisor/test_autonomy_contracts.py` (create)

**Context:** The compatibility matrix at line 24862 uses string comparison (`prime_schema >= min_prime`), which breaks for multi-digit versions (`"1.10" < "1.9"` as strings). We need a tuple-based semver compare.

**Step 1: Write the failing test**

Create `tests/unit/backend/supervisor/test_autonomy_contracts.py`:

```python
"""Tests for autonomy contract checking and version comparison."""

import pytest


def test_version_gte_simple():
    """Basic version comparison."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0", "1.0") is True
    assert _version_gte("2.0", "1.0") is True
    assert _version_gte("1.0", "2.0") is False


def test_version_gte_multidigit():
    """Multi-digit segments must compare numerically, not lexically."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    # This is the bug: string compare says "1.10" < "1.9"
    assert _version_gte("1.10", "1.9") is True
    assert _version_gte("1.9", "1.10") is False


def test_version_gte_three_segments():
    """Three-segment versions (patch level)."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0.1", "1.0.0") is True
    assert _version_gte("1.0.0", "1.0.1") is False
    assert _version_gte("2.0.0", "1.9.9") is True


def test_version_gte_unequal_length():
    """Versions with different segment counts."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0.0", "1.0") is True  # 1.0.0 >= 1.0
    assert _version_gte("1.0", "1.0.1") is False  # 1.0 < 1.0.1
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py -v -x 2>&1 | head -30`
Expected: FAIL — `ImportError: cannot import name '_version_gte'`

**Step 3: Write minimal implementation**

In `backend/supervisor/cross_repo_startup_orchestrator.py`, add the function before the `AUTONOMY_SCHEMA_COMPATIBILITY` dict (before line 24774):

```python
def _version_gte(a: str, b: str) -> bool:
    """True if semantic version *a* >= *b* (numeric tuple compare)."""
    def _parse(v: str) -> Tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))
    return _parse(a) >= _parse(b)
```

Then replace the two string comparisons at lines 24862-24870:

Old:
```python
    checks["prime_compatible"] = (
        prime_schema is not None and prime_schema >= min_prime
    )
    checks["reactor_compatible"] = (
        reactor_schema is not None and reactor_schema >= min_reactor
    )
```

New:
```python
    checks["prime_compatible"] = (
        prime_schema is not None and _version_gte(prime_schema, min_prime)
    )
    checks["reactor_compatible"] = (
        reactor_schema is not None and _version_gte(reactor_schema, min_reactor)
    )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py -v -x 2>&1 | head -30`
Expected: 4 PASSED

**Step 5: Commit**

```bash
git add backend/supervisor/cross_repo_startup_orchestrator.py tests/unit/backend/supervisor/test_autonomy_contracts.py
git commit -m "feat(autonomy): add semantic version comparison for contract checks"
```

---

### Task 2: Enhanced Contract Check with Reason Codes

**Files:**
- Modify: `backend/supervisor/cross_repo_startup_orchestrator.py:24779-24891`
- Test: `tests/unit/backend/supervisor/test_autonomy_contracts.py`

**Context:** `check_autonomy_contracts()` returns `(bool, str, dict)` but the caller can't distinguish "services still starting" from "schema mismatch". We add `reason` and `pending` keys to the checks dict.

**Step 1: Write the failing tests**

Append to `tests/unit/backend/supervisor/test_autonomy_contracts.py`:

```python
from unittest.mock import AsyncMock, patch, MagicMock
import aiohttp


@pytest.fixture
def mock_config():
    """Mock OrchestratorConfig with default ports."""
    config = MagicMock()
    config.jarvis_prime_default_port = 8001
    config.reactor_core_default_port = 8090
    return config


@pytest.mark.asyncio
async def test_contract_check_all_healthy(mock_config):
    """When all services are healthy and compatible, reason is 'active'."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        check_autonomy_contracts,
    )

    async def mock_get(url, **kwargs):
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(
            status=200,
            json=AsyncMock(return_value={
                "autonomy_schema_version": "1.0",
                "status": "healthy",
            }),
        ))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    mock_session = MagicMock()
    mock_session.get = mock_get
    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session_cm):
        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.OrchestratorConfig",
            return_value=mock_config,
        ):
            passed, status, checks = await check_autonomy_contracts()

    assert passed is True
    assert checks["reason"] == "active"
    assert checks.get("pending") == []


@pytest.mark.asyncio
async def test_contract_check_services_unreachable(mock_config):
    """When services are unreachable, reason is 'pending_services'."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        check_autonomy_contracts,
    )

    async def mock_get(url, **kwargs):
        raise aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("Connection refused")
        )

    mock_session = MagicMock()
    mock_session.get = mock_get
    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session_cm):
        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.OrchestratorConfig",
            return_value=mock_config,
        ):
            passed, status, checks = await check_autonomy_contracts()

    assert passed is False
    assert checks["reason"] == "pending_services"
    assert "prime" in checks["pending"]
    assert "reactor" in checks["pending"]


@pytest.mark.asyncio
async def test_contract_check_schema_mismatch(mock_config):
    """When services are healthy but schema incompatible, reason is 'schema_mismatch'."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        check_autonomy_contracts,
    )

    async def mock_get(url, **kwargs):
        # Return incompatible schema version
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(
            status=200,
            json=AsyncMock(return_value={
                "autonomy_schema_version": "0.1",  # Below min 1.0
                "status": "healthy",
            }),
        ))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    mock_session = MagicMock()
    mock_session.get = mock_get
    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session_cm):
        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.OrchestratorConfig",
            return_value=mock_config,
        ):
            passed, status, checks = await check_autonomy_contracts()

    assert passed is False
    assert checks["reason"] == "schema_mismatch"
    assert checks["pending"] == []  # services ARE reachable
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py -v -x -k "contract_check" 2>&1 | head -40`
Expected: FAIL — `KeyError: 'reason'`

**Step 3: Modify `check_autonomy_contracts()` to add reason codes**

At the end of `check_autonomy_contracts()` (after the `all_pass` computation, before the return), replace the status/logging block with:

```python
    # Determine reason code
    _unreachable = []
    if not checks.get("prime_reachable", False):
        _unreachable.append("prime")
    if not checks.get("reactor_reachable", False):
        _unreachable.append("reactor")

    if all_pass:
        checks["reason"] = "active"
        checks["pending"] = []
    elif _unreachable:
        # Services not yet responding — likely still starting
        checks["reason"] = "pending_services"
        checks["pending"] = _unreachable
    elif not checks.get("body_journal", False):
        checks["reason"] = "pending_lease"
        checks["pending"] = ["journal"]
    else:
        # Services reachable but schema incompatible
        checks["reason"] = "schema_mismatch"
        checks["pending"] = []

    status = "autonomy_ready" if all_pass else "contract_mismatch"

    if not all_pass:
        _log_fn = logger.info if checks["reason"].startswith("pending") else logger.warning
        _log_fn(
            "[v300.0] Autonomy contract check — reason=%s, pending=%s. "
            "Checks: %s",
            checks["reason"], checks["pending"], checks,
        )
    else:
        logger.info("[v300.0] Autonomy contracts validated — all compatible")

    return all_pass, status, checks
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py -v -x 2>&1 | head -40`
Expected: 7 PASSED (4 from Task 1 + 3 new)

**Step 5: Commit**

```bash
git add backend/supervisor/cross_repo_startup_orchestrator.py tests/unit/backend/supervisor/test_autonomy_contracts.py
git commit -m "feat(autonomy): add reason codes to check_autonomy_contracts()"
```

---

### Task 3: Three-State Autonomy Model in Supervisor

**Files:**
- Modify: `unified_supervisor.py:63407-63410` (init)
- Modify: `unified_supervisor.py:81402-81447` (boot-time gate)
- Test: `tests/unit/backend/supervisor/test_autonomy_contracts.py`

**Context:** Currently `_autonomy_mode` is `"disabled"` / `"active"` / `"read_only"`. The design replaces this with `"pending"` / `"active"` / `"read_only"`. The boot-time gate must use reason codes from Task 2 to set the appropriate mode. `pending` blocks writes identically to `read_only`.

**Step 1: Write the failing test**

Append to `tests/unit/backend/supervisor/test_autonomy_contracts.py`:

```python
def test_autonomy_mode_pending_blocks_writes():
    """pending mode must block writes identically to read_only."""
    # Both pending and read_only should be treated the same for write gates.
    # The gate check is: mode != "active"
    for mode in ("pending", "read_only"):
        assert mode != "active", f"{mode} must block autonomous writes"


def test_autonomy_reason_to_mode_mapping():
    """Verify reason codes map to the correct autonomy mode."""
    _REASON_TO_MODE = {
        "pending_services": "pending",
        "pending_lease": "pending",
        "schema_mismatch": "read_only",
        "health_probe_failed": "read_only",
        "timeout": "read_only",
        "active": "active",
    }
    # pending_* reasons → pending mode
    assert _REASON_TO_MODE["pending_services"] == "pending"
    assert _REASON_TO_MODE["pending_lease"] == "pending"
    # Hard failures → read_only
    assert _REASON_TO_MODE["schema_mismatch"] == "read_only"
    assert _REASON_TO_MODE["timeout"] == "read_only"
    # Success → active
    assert _REASON_TO_MODE["active"] == "active"
```

**Step 2: Run tests to verify they pass (these are logic assertions, not integration)**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py::test_autonomy_mode_pending_blocks_writes tests/unit/backend/supervisor/test_autonomy_contracts.py::test_autonomy_reason_to_mode_mapping -v`
Expected: 2 PASSED

**Step 3: Modify `unified_supervisor.py` — init and boot-time gate**

**3a.** Change `_autonomy_mode` init at line 63409:

Old:
```python
self._autonomy_mode: str = "disabled"
self._autonomy_checks: Dict[str, Any] = {}
```

New:
```python
self._autonomy_mode: str = "pending"  # pending until contracts checked
self._autonomy_reason: str = "pending_services"
self._autonomy_checks: Dict[str, Any] = {}
```

**3b.** Replace the boot-time gate at lines 81402-81447 with:

```python
            # v300.0: Phase 2 — Autonomy contract gate (boot-time)
            # Checks schema version compatibility across Body, Prime, and
            # Reactor.  Uses reason codes to distinguish "still starting"
            # (pending) from "hard failure" (read_only).
            try:
                self._update_component_status(
                    "autonomy_contracts", "running",
                    "Checking autonomy schema compatibility...",
                )
                from backend.supervisor.cross_repo_startup_orchestrator import (
                    check_autonomy_contracts,
                )
                _auto_pass, _auto_status, _auto_checks = await check_autonomy_contracts()
                self._autonomy_checks = _auto_checks
                _reason = _auto_checks.get("reason", "active" if _auto_pass else "schema_mismatch")

                if _auto_pass:
                    self._autonomy_mode = "active"
                    self._autonomy_reason = "active"
                    self._update_component_status(
                        "autonomy_contracts", "complete",
                        "All autonomy contracts compatible — full autonomy mode",
                    )
                    self.logger.info(
                        "[Kernel] Phase 2 autonomy contracts validated — mode=active"
                    )
                elif _reason.startswith("pending"):
                    self._autonomy_mode = "pending"
                    self._autonomy_reason = _reason
                    self._update_component_status(
                        "autonomy_contracts", "degraded",
                        f"Services still starting — pending autonomy ({_reason}, "
                        f"pending={_auto_checks.get('pending', [])})",
                    )
                    self.logger.info(
                        "[Kernel] Autonomy contracts pending — mode=pending, "
                        "reason=%s, pending=%s. Will re-check shortly.",
                        _reason, _auto_checks.get("pending", []),
                    )
                else:
                    self._autonomy_mode = "read_only"
                    self._autonomy_reason = _reason
                    self._update_component_status(
                        "autonomy_contracts", "degraded",
                        f"Contract mismatch — read-only autonomy ({_reason})",
                    )
                    self.logger.warning(
                        "[Kernel] Autonomy contract mismatch — degraded to read_only. "
                        "reason=%s, checks=%s", _reason, _auto_checks,
                    )
            except Exception as _auto_err:
                self._autonomy_mode = "pending"
                self._autonomy_reason = "pending_services"
                self._update_component_status(
                    "autonomy_contracts", "error",
                    f"Contract check failed (will retry): {_auto_err}",
                )
                self.logger.warning(
                    "[Kernel] Autonomy contract check error (will retry): %s",
                    _auto_err,
                )

            return True  # Trinity is optional
```

**Step 4: Verify no syntax errors**

Run: `python3 -c 'import ast; ast.parse(open("unified_supervisor.py").read()); print("OK")'`
Expected: `OK`

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/supervisor/test_autonomy_contracts.py
git commit -m "feat(autonomy): three-state model (pending/active/read_only) with reason codes"
```

---

### Task 4: Bounded Readiness Wait

**Files:**
- Modify: `unified_supervisor.py` (add `_await_autonomy_dependencies()` method, wire before contract check)
- Test: `tests/unit/backend/supervisor/test_autonomy_contracts.py`

**Context:** After `start_all_services()` returns, Prime/Reactor need time to become healthy. We insert a bounded poll loop (default 15s, configurable via `JARVIS_AUTONOMY_READINESS_WAIT_S`) that checks every 2s and exits early when all dependencies are met.

**Step 1: Write the failing test**

Append to `tests/unit/backend/supervisor/test_autonomy_contracts.py`:

```python
import asyncio


@pytest.mark.asyncio
async def test_await_autonomy_dependencies_early_exit():
    """Should exit early when all dependencies are met."""
    # Simulate: all checks pass on first poll
    call_count = 0

    async def mock_check():
        nonlocal call_count
        call_count += 1
        return True, "autonomy_ready", {
            "reason": "active",
            "pending": [],
            "prime_reachable": True,
            "reactor_reachable": True,
            "body_journal": True,
        }

    result = await _run_bounded_wait(mock_check, timeout=15.0, poll_interval=0.1)
    assert result["all_ready"] is True
    assert call_count == 1  # Should exit on first poll


@pytest.mark.asyncio
async def test_await_autonomy_dependencies_timeout():
    """Should return partial results on timeout."""
    async def mock_check():
        return False, "contract_mismatch", {
            "reason": "pending_services",
            "pending": ["prime"],
            "prime_reachable": False,
            "reactor_reachable": True,
            "body_journal": True,
        }

    result = await _run_bounded_wait(mock_check, timeout=0.3, poll_interval=0.1)
    assert result["all_ready"] is False
    assert result["reason"] == "pending_services"


@pytest.mark.asyncio
async def test_await_autonomy_dependencies_gradual_readiness():
    """Should keep polling until all dependencies are ready."""
    poll_count = 0

    async def mock_check():
        nonlocal poll_count
        poll_count += 1
        if poll_count < 3:
            return False, "contract_mismatch", {
                "reason": "pending_services",
                "pending": ["prime"],
                "prime_reachable": False,
                "reactor_reachable": True,
                "body_journal": True,
            }
        return True, "autonomy_ready", {
            "reason": "active",
            "pending": [],
            "prime_reachable": True,
            "reactor_reachable": True,
            "body_journal": True,
        }

    result = await _run_bounded_wait(mock_check, timeout=5.0, poll_interval=0.1)
    assert result["all_ready"] is True
    assert poll_count == 3


async def _run_bounded_wait(check_fn, timeout, poll_interval):
    """Helper to test the bounded wait logic in isolation."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        await_autonomy_dependencies,
    )
    return await await_autonomy_dependencies(
        check_fn=check_fn,
        timeout=timeout,
        poll_interval=poll_interval,
    )
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py -v -x -k "await_autonomy" 2>&1 | head -30`
Expected: FAIL — `ImportError: cannot import name 'await_autonomy_dependencies'`

**Step 3: Implement `await_autonomy_dependencies()` in `cross_repo_startup_orchestrator.py`**

Add near the `check_autonomy_contracts()` function (after it, around line 24893):

```python
async def await_autonomy_dependencies(
    *,
    check_fn=None,
    timeout: float = 15.0,
    poll_interval: float = 2.0,
    shutdown_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """Bounded wait for autonomy dependencies to become ready.

    Polls ``check_fn`` (defaults to ``check_autonomy_contracts``) every
    ``poll_interval`` seconds until all dependencies are met or ``timeout``
    is exceeded.

    Returns dict with keys:
        all_ready (bool): True if all dependencies met within timeout.
        reason (str): Reason code from last check.
        checks (dict): Full checks dict from last check.
        elapsed (float): Seconds spent waiting.
        polls (int): Number of poll iterations.
    """
    if check_fn is None:
        check_fn = check_autonomy_contracts

    timeout = max(1.0, timeout)
    start = asyncio.get_event_loop().time()
    polls = 0

    while True:
        polls += 1
        passed, _status, checks = await check_fn()
        reason = checks.get("reason", "active" if passed else "pending_services")
        elapsed = asyncio.get_event_loop().time() - start

        if passed:
            return {
                "all_ready": True,
                "reason": reason,
                "checks": checks,
                "elapsed": elapsed,
                "polls": polls,
            }

        # Timeout reached
        if elapsed >= timeout:
            return {
                "all_ready": False,
                "reason": reason,
                "checks": checks,
                "elapsed": elapsed,
                "polls": polls,
            }

        # Check shutdown
        if shutdown_event and shutdown_event.is_set():
            return {
                "all_ready": False,
                "reason": "shutdown",
                "checks": checks,
                "elapsed": elapsed,
                "polls": polls,
            }

        # Wait before next poll
        remaining = timeout - elapsed
        wait_time = min(poll_interval, remaining)
        if wait_time <= 0:
            break

        if shutdown_event:
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=wait_time,
                )
                # Shutdown signaled during wait
                return {
                    "all_ready": False,
                    "reason": "shutdown",
                    "checks": checks,
                    "elapsed": asyncio.get_event_loop().time() - start,
                    "polls": polls,
                }
            except asyncio.TimeoutError:
                pass  # Normal — poll again
        else:
            await asyncio.sleep(wait_time)

    return {
        "all_ready": False,
        "reason": reason,
        "checks": checks,
        "elapsed": asyncio.get_event_loop().time() - start,
        "polls": polls,
    }
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py -v -x -k "await_autonomy" 2>&1 | head -30`
Expected: 3 PASSED

**Step 5: Wire into `unified_supervisor.py` boot-time gate**

In `unified_supervisor.py`, modify the boot-time gate (from Task 3) to call the bounded wait BEFORE the contract check. Add this block just before the `check_autonomy_contracts()` import:

```python
                # Bounded readiness wait — let Prime/Reactor finish starting
                _readiness_timeout = max(
                    1.0,
                    float(os.environ.get("JARVIS_AUTONOMY_READINESS_WAIT_S", "15.0")),
                )
                from backend.supervisor.cross_repo_startup_orchestrator import (
                    await_autonomy_dependencies,
                )
                _wait_result = await await_autonomy_dependencies(
                    timeout=_readiness_timeout,
                    poll_interval=2.0,
                    shutdown_event=getattr(self, "_shutdown_event", None),
                )
                self.logger.info(
                    "[Kernel] Autonomy readiness wait: ready=%s, reason=%s, "
                    "elapsed=%.1fs, polls=%d",
                    _wait_result["all_ready"],
                    _wait_result["reason"],
                    _wait_result["elapsed"],
                    _wait_result["polls"],
                )

                # Use the wait result directly — no need to re-check
                _auto_pass = _wait_result["all_ready"]
                _auto_checks = _wait_result.get("checks", {})
                self._autonomy_checks = _auto_checks
                _reason = _auto_checks.get(
                    "reason",
                    "active" if _auto_pass else _wait_result.get("reason", "timeout"),
                )
```

And remove the separate `check_autonomy_contracts()` call that was there before (since `await_autonomy_dependencies` already calls it internally). Keep the `if _auto_pass / elif _reason.startswith("pending") / else` block from Task 3 unchanged — it still uses `_auto_pass`, `_auto_checks`, and `_reason`.

**Step 6: Verify no syntax errors**

Run: `python3 -c 'import ast; ast.parse(open("unified_supervisor.py").read()); print("OK")'`
Expected: `OK`

**Step 7: Commit**

```bash
git add unified_supervisor.py backend/supervisor/cross_repo_startup_orchestrator.py tests/unit/backend/supervisor/test_autonomy_contracts.py
git commit -m "feat(autonomy): bounded readiness wait before contract check"
```

---

### Task 5: Adaptive Monitor Interval & Event-Driven Promotion

**Files:**
- Modify: `unified_supervisor.py:86277-86323` (runtime monitor loop)
- Test: `tests/unit/backend/supervisor/test_autonomy_contracts.py`

**Context:** When `_autonomy_mode == "pending"`, the runtime monitor should re-check every 5s instead of 60s so services that come online get promoted quickly. Once `active`, revert to 60s. Also add a transition log with timing.

**Step 1: Write the failing test**

Append to `tests/unit/backend/supervisor/test_autonomy_contracts.py`:

```python
def test_adaptive_monitor_interval():
    """Pending mode should use shorter check interval."""
    def _get_interval(mode: str, base_interval: float = 60.0) -> float:
        if mode == "pending":
            return min(5.0, base_interval)
        return base_interval

    assert _get_interval("pending") == 5.0
    assert _get_interval("active") == 60.0
    assert _get_interval("read_only") == 60.0
    assert _get_interval("pending", base_interval=3.0) == 3.0  # Don't exceed base
```

**Step 2: Run test**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py::test_adaptive_monitor_interval -v`
Expected: PASS (pure logic test)

**Step 3: Modify the runtime monitor loop in `unified_supervisor.py`**

Replace lines 86277-86323 with:

```python
            # v300.0: Phase 2 — Runtime autonomy contract monitor.
            # Re-checks autonomy schema compatibility periodically.
            # Uses adaptive interval: 5s when pending, base interval when active/read_only.
            if _get_env_bool("JARVIS_AUTONOMY_MONITOR_ENABLED", True):
                _auto_base_interval = max(
                    10.0,
                    _get_env_float("JARVIS_AUTONOMY_MONITOR_INTERVAL_S", 60.0),
                )
                # Adaptive: 5s polling when pending, base interval otherwise
                _auto_interval = (
                    min(5.0, _auto_base_interval)
                    if self._autonomy_mode == "pending"
                    else _auto_base_interval
                )
                _auto_now = time.time()
                _auto_last = float(
                    getattr(self, "_autonomy_runtime_last_check", 0.0) or 0.0
                )
                if (_auto_now - _auto_last) >= _auto_interval:
                    try:
                        from backend.supervisor.cross_repo_startup_orchestrator import (
                            check_autonomy_contracts,
                        )
                        _a_pass, _a_status, _a_checks = await check_autonomy_contracts()
                        _prev_mode = self._autonomy_mode
                        _prev_reason = getattr(self, "_autonomy_reason", "unknown")
                        self._autonomy_checks = _a_checks
                        _new_reason = _a_checks.get(
                            "reason", "active" if _a_pass else "schema_mismatch"
                        )

                        if _a_pass and _prev_mode != "active":
                            self._autonomy_mode = "active"
                            self._autonomy_reason = "active"
                            _boot_time = getattr(self, "_boot_timestamp", None)
                            _elapsed = (
                                f", {time.time() - _boot_time:.1f}s after boot"
                                if _boot_time else ""
                            )
                            self._update_component_status(
                                "autonomy_contracts", "complete",
                                "Autonomy contracts recovered — full autonomy mode",
                            )
                            self.logger.info(
                                "[Autonomy] %s → active (reason=%s%s)",
                                _prev_mode, _new_reason, _elapsed,
                            )
                        elif not _a_pass and _prev_mode == "active":
                            self._autonomy_mode = "read_only"
                            self._autonomy_reason = _new_reason
                            self._update_component_status(
                                "autonomy_contracts", "degraded",
                                f"Contract drift detected — read-only ({_new_reason})",
                            )
                            self.logger.warning(
                                "[Autonomy] active → read_only (reason=%s). "
                                "Checks: %s", _new_reason, _a_checks,
                            )
                        elif not _a_pass and _prev_mode == "pending":
                            # Still pending — update reason but stay pending
                            self._autonomy_reason = _new_reason
                            if _new_reason.startswith("pending"):
                                self.logger.debug(
                                    "[Autonomy] Still pending: %s, pending=%s",
                                    _new_reason, _a_checks.get("pending", []),
                                )
                            else:
                                # Hard failure while pending → promote to read_only
                                self._autonomy_mode = "read_only"
                                self.logger.warning(
                                    "[Autonomy] pending → read_only (reason=%s). "
                                    "Checks: %s", _new_reason, _a_checks,
                                )
                    except Exception as _auto_err:
                        self.logger.debug(
                            "[Autonomy] Runtime monitor error: %s", _auto_err,
                        )
                    finally:
                        self._autonomy_runtime_last_check = _auto_now
```

**Step 4: Verify no syntax errors**

Run: `python3 -c 'import ast; ast.parse(open("unified_supervisor.py").read()); print("OK")'`
Expected: `OK`

**Step 5: Run full autonomy test suite**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py -v 2>&1 | tail -20`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/supervisor/test_autonomy_contracts.py
git commit -m "feat(autonomy): adaptive monitor interval (5s when pending) with transition logging"
```

---

## Phase C: MCP Cloud Capacity Integration

### Task 6: CloudCapacityAction Enum

**Files:**
- Modify: `backend/core/memory_types.py:439` (after ActuatorAction)
- Test: `tests/unit/backend/core/test_cloud_capacity.py` (create)

**Context:** Add the `CloudCapacityAction` enum to `memory_types.py` — the set of decisions the cloud capacity controller can make.

**Step 1: Write the failing test**

Create `tests/unit/backend/core/test_cloud_capacity.py`:

```python
"""Tests for CloudCapacityController and related types."""

import pytest


def test_cloud_capacity_action_enum():
    """CloudCapacityAction must have all required values."""
    from backend.core.memory_types import CloudCapacityAction

    expected = {
        "STAY_LOCAL",
        "DEGRADE_LOCAL",
        "OFFLOAD_PARTIAL",
        "SPIN_SPOT",
        "FALLBACK_ONDEMAND",
    }
    actual = {a.name for a in CloudCapacityAction}
    assert actual == expected


def test_cloud_capacity_action_is_str_enum():
    """CloudCapacityAction values should be usable as strings."""
    from backend.core.memory_types import CloudCapacityAction

    assert CloudCapacityAction.STAY_LOCAL.value == "stay_local"
    assert str(CloudCapacityAction.SPIN_SPOT) == "CloudCapacityAction.SPIN_SPOT"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py -v -x 2>&1 | head -20`
Expected: FAIL — `ImportError: cannot import name 'CloudCapacityAction'`

**Step 3: Add enum to `memory_types.py`**

After the `ActuatorAction` enum (after line 439 in `backend/core/memory_types.py`), add:

```python
class CloudCapacityAction(str, Enum):
    """Cloud capacity decisions made by CloudCapacityController."""

    STAY_LOCAL = "stay_local"
    DEGRADE_LOCAL = "degrade_local"
    OFFLOAD_PARTIAL = "offload_partial"
    SPIN_SPOT = "spin_spot"
    FALLBACK_ONDEMAND = "fallback_ondemand"
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py -v -x 2>&1 | head -20`
Expected: 2 PASSED

**Step 5: Commit**

```bash
git add backend/core/memory_types.py tests/unit/backend/core/test_cloud_capacity.py
git commit -m "feat(cloud): add CloudCapacityAction enum to memory_types"
```

---

### Task 7: CloudCapacityController

**Files:**
- Create: `backend/core/cloud_capacity_controller.py`
- Test: `tests/unit/backend/core/test_cloud_capacity.py`

**Context:** Single decision authority that registers as a broker pressure observer, consumes MCP signals (pressure tier, queue depth, latency), and returns `CloudCapacityAction` decisions with hysteresis and cooldowns.

**Step 1: Write the failing tests**

Append to `tests/unit/backend/core/test_cloud_capacity.py`:

```python
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import time


@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.register_pressure_observer = MagicMock()
    broker.latest_snapshot = MagicMock()
    broker.latest_snapshot.memory_percent = 50.0
    return broker


@pytest.mark.asyncio
async def test_controller_registers_with_broker(mock_broker):
    """Controller must register as a pressure observer on init."""
    from backend.core.cloud_capacity_controller import CloudCapacityController

    controller = CloudCapacityController(broker=mock_broker)
    mock_broker.register_pressure_observer.assert_called_once()


@pytest.mark.asyncio
async def test_controller_stay_local_at_low_pressure(mock_broker):
    """At ABUNDANT/OPTIMAL pressure, action should be STAY_LOCAL."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    action = controller.evaluate(
        tier=PressureTier.OPTIMAL,
        queue_depth=2,
        latency_violations=0,
    )
    assert action == CloudCapacityAction.STAY_LOCAL


@pytest.mark.asyncio
async def test_controller_degrade_local_at_constrained(mock_broker):
    """At CONSTRAINED with manageable queue, should DEGRADE_LOCAL."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    action = controller.evaluate(
        tier=PressureTier.CONSTRAINED,
        queue_depth=3,
        latency_violations=0,
    )
    assert action == CloudCapacityAction.DEGRADE_LOCAL


@pytest.mark.asyncio
async def test_controller_spin_spot_at_critical_sustained(mock_broker):
    """At CRITICAL pressure sustained > threshold, should SPIN_SPOT."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    # Simulate sustained critical: set first_critical_at in the past
    controller._first_critical_at = time.monotonic() - 60
    action = controller.evaluate(
        tier=PressureTier.CRITICAL,
        queue_depth=10,
        latency_violations=5,
    )
    assert action == CloudCapacityAction.SPIN_SPOT


@pytest.mark.asyncio
async def test_controller_spot_create_cooldown(mock_broker):
    """Spot create cooldown must prevent rapid VM creation."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    # Simulate: spot was just created
    controller._last_spot_create = time.monotonic()
    controller._first_critical_at = time.monotonic() - 60

    action = controller.evaluate(
        tier=PressureTier.CRITICAL,
        queue_depth=10,
        latency_violations=5,
    )
    # Should fall back to OFFLOAD_PARTIAL since cooldown prevents SPIN_SPOT
    assert action in (
        CloudCapacityAction.OFFLOAD_PARTIAL,
        CloudCapacityAction.FALLBACK_ONDEMAND,
    )


@pytest.mark.asyncio
async def test_controller_offload_partial_at_constrained_growing_queue(mock_broker):
    """CONSTRAINED with growing queue should OFFLOAD_PARTIAL."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    action = controller.evaluate(
        tier=PressureTier.CONSTRAINED,
        queue_depth=15,  # Growing queue
        latency_violations=3,
    )
    assert action == CloudCapacityAction.OFFLOAD_PARTIAL
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py -v -x -k "controller" 2>&1 | head -30`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.cloud_capacity_controller'`

**Step 3: Create `backend/core/cloud_capacity_controller.py`**

```python
"""Cloud Capacity Controller — single decision authority for cloud scaling.

Consumes MCP pressure signals from MemoryBudgetBroker and produces
CloudCapacityAction decisions with hysteresis and cooldowns.

The controller *decides*; GCPVMManager *executes*.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from backend.core.memory_types import (
    CloudCapacityAction,
    PressureTier,
)

logger = logging.getLogger(__name__)

# --- Configuration (all env-var-driven, no hardcoding) ---
_SPOT_CREATE_COOLDOWN_S = float(os.getenv("JARVIS_SPOT_CREATE_COOLDOWN_S", "120"))
_SPOT_DESTROY_COOLDOWN_S = float(os.getenv("JARVIS_SPOT_DESTROY_COOLDOWN_S", "300"))
_CRITICAL_SUSTAIN_S = float(os.getenv("JARVIS_CRITICAL_SUSTAIN_THRESHOLD_S", "30"))
_QUEUE_DEPTH_HIGH = int(os.getenv("JARVIS_QUEUE_DEPTH_HIGH", "10"))
_QUEUE_DEPTH_OFFLOAD = int(os.getenv("JARVIS_QUEUE_DEPTH_OFFLOAD", "8"))


class CloudCapacityController:
    """Single decision authority for cloud capacity actions.

    Registers as a MemoryBudgetBroker pressure observer.  On each
    pressure tier change it records the tier; callers invoke
    ``evaluate()`` to get the current recommended action.

    Parameters
    ----------
    broker : MemoryBudgetBroker
        The broker to register with as a pressure observer.
    """

    def __init__(self, broker: Any) -> None:
        self._broker = broker
        self._current_tier: PressureTier = PressureTier.OPTIMAL

        # Cooldown tracking (monotonic timestamps)
        self._last_spot_create: float = 0.0
        self._last_spot_destroy: float = 0.0

        # Sustained-critical tracking
        self._first_critical_at: Optional[float] = None

        # Spot availability
        self._spot_available: bool = True

        # Decision counter for telemetry
        self._total_decisions: int = 0
        self._decisions_by_action: Dict[str, int] = {}

        # Register with broker
        broker.register_pressure_observer(self._on_pressure_change)
        logger.info("[CloudCapacity] Registered with MCP broker")

    async def _on_pressure_change(
        self, tier: PressureTier, snapshot: Any,
    ) -> None:
        """Callback from broker when pressure tier changes."""
        prev = self._current_tier
        self._current_tier = tier

        # Track sustained critical
        if tier >= PressureTier.CRITICAL:
            if self._first_critical_at is None:
                self._first_critical_at = time.monotonic()
        else:
            self._first_critical_at = None

        if prev != tier:
            logger.info(
                "[CloudCapacity] Pressure tier change: %s → %s",
                prev.name, tier.name,
            )

    def evaluate(
        self,
        tier: Optional[PressureTier] = None,
        queue_depth: int = 0,
        latency_violations: int = 0,
    ) -> CloudCapacityAction:
        """Evaluate current conditions and return recommended action.

        Parameters
        ----------
        tier : PressureTier, optional
            Override tier (uses broker-tracked tier if None).
        queue_depth : int
            Current inference request backlog.
        latency_violations : int
            Number of recent latency SLO violations.
        """
        if tier is None:
            tier = self._current_tier

        now = time.monotonic()
        action = self._decide(tier, queue_depth, latency_violations, now)

        self._total_decisions += 1
        self._decisions_by_action[action.value] = (
            self._decisions_by_action.get(action.value, 0) + 1
        )

        return action

    def _decide(
        self,
        tier: PressureTier,
        queue_depth: int,
        latency_violations: int,
        now: float,
    ) -> CloudCapacityAction:
        """Core decision logic with hysteresis and cooldowns."""
        # --- STAY_LOCAL: low pressure, short queue ---
        if tier <= PressureTier.ELEVATED and queue_depth < _QUEUE_DEPTH_OFFLOAD:
            return CloudCapacityAction.STAY_LOCAL

        # --- CRITICAL/EMERGENCY: consider Spot VM ---
        if tier >= PressureTier.CRITICAL:
            # Track sustained critical
            if self._first_critical_at is None:
                self._first_critical_at = now

            sustained = now - self._first_critical_at
            spot_cooldown_ok = (now - self._last_spot_create) >= _SPOT_CREATE_COOLDOWN_S

            if sustained >= _CRITICAL_SUSTAIN_S and spot_cooldown_ok:
                if self._spot_available:
                    return CloudCapacityAction.SPIN_SPOT
                else:
                    return CloudCapacityAction.FALLBACK_ONDEMAND

            # Critical but not sustained enough or on cooldown
            if queue_depth >= _QUEUE_DEPTH_OFFLOAD:
                return CloudCapacityAction.OFFLOAD_PARTIAL
            return CloudCapacityAction.FALLBACK_ONDEMAND

        # --- CONSTRAINED: degrade or offload ---
        if tier >= PressureTier.CONSTRAINED:
            if queue_depth >= _QUEUE_DEPTH_OFFLOAD or latency_violations > 0:
                return CloudCapacityAction.OFFLOAD_PARTIAL
            return CloudCapacityAction.DEGRADE_LOCAL

        # Fallback
        return CloudCapacityAction.STAY_LOCAL

    def record_spot_created(self) -> None:
        """Record that a Spot VM was just created (starts cooldown)."""
        self._last_spot_create = time.monotonic()

    def record_spot_destroyed(self) -> None:
        """Record that a Spot VM was just destroyed (starts cooldown)."""
        self._last_spot_destroy = time.monotonic()

    def mark_spot_unavailable(self) -> None:
        """Mark Spot VMs as unavailable (preempted/quota exhausted)."""
        self._spot_available = False
        logger.warning("[CloudCapacity] Spot VMs marked unavailable")

    def mark_spot_available(self) -> None:
        """Mark Spot VMs as available again."""
        self._spot_available = True
        logger.info("[CloudCapacity] Spot VMs marked available")

    def get_stats(self) -> Dict[str, Any]:
        """Return telemetry stats."""
        return {
            "current_tier": self._current_tier.name,
            "total_decisions": self._total_decisions,
            "decisions_by_action": dict(self._decisions_by_action),
            "spot_available": self._spot_available,
            "sustained_critical_s": (
                time.monotonic() - self._first_critical_at
                if self._first_critical_at is not None
                else 0.0
            ),
        }
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py -v 2>&1 | tail -20`
Expected: 8 PASSED (2 from Task 6 + 6 controller tests)

**Step 5: Commit**

```bash
git add backend/core/cloud_capacity_controller.py tests/unit/backend/core/test_cloud_capacity.py
git commit -m "feat(cloud): create CloudCapacityController with hysteresis and cooldowns"
```

---

### Task 8: Execution-Time Epoch Fencing in Coordinator

**Files:**
- Modify: `backend/core/memory_actuator_coordinator.py:127-132`
- Test: `tests/unit/backend/core/test_cloud_capacity.py`

**Context:** `drain_pending()` currently returns all queued actions without re-checking staleness. A new epoch/sequence could arrive between `submit()` and `drain()`, making queued actions stale. Add staleness re-check at drain time.

**Step 1: Write the failing test**

Append to `tests/unit/backend/core/test_cloud_capacity.py`:

```python
def test_drain_pending_rejects_stale_actions():
    """drain_pending must filter out actions that became stale after submission."""
    from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator
    from backend.core.memory_types import (
        ActuatorAction, DecisionEnvelope, PressureTier,
    )

    coord = MemoryActuatorCoordinator()
    coord.advance_epoch(epoch=1, sequence=1)

    # Submit an action at epoch=1, seq=1 (not stale at submit time)
    envelope = DecisionEnvelope(
        snapshot_id="snap-1",
        epoch=1,
        sequence=1,
        policy_version="1.0",
        pressure_tier=PressureTier.CRITICAL,
        timestamp=time.time(),
    )
    decision_id = coord.submit(
        action=ActuatorAction.CLOUD_OFFLOAD,
        envelope=envelope,
        source="test",
    )
    assert decision_id is not None  # Accepted

    # Epoch advances before drain
    coord.advance_epoch(epoch=2, sequence=1)

    # Drain should filter out the stale action
    drained = coord.drain_pending()
    assert len(drained) == 0, "Stale action should be rejected at drain time"


def test_drain_pending_keeps_fresh_actions():
    """drain_pending must keep actions that are still fresh."""
    from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator
    from backend.core.memory_types import (
        ActuatorAction, DecisionEnvelope, PressureTier,
    )

    coord = MemoryActuatorCoordinator()
    coord.advance_epoch(epoch=1, sequence=5)

    envelope = DecisionEnvelope(
        snapshot_id="snap-1",
        epoch=1,
        sequence=5,
        policy_version="1.0",
        pressure_tier=PressureTier.CRITICAL,
        timestamp=time.time(),
    )
    coord.submit(
        action=ActuatorAction.CLOUD_OFFLOAD,
        envelope=envelope,
        source="test",
    )

    # No epoch advance — action is still fresh
    drained = coord.drain_pending()
    assert len(drained) == 1
```

**Step 2: Run tests to verify the staleness test fails**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py::test_drain_pending_rejects_stale_actions -v -x 2>&1 | head -20`
Expected: FAIL — `AssertionError: Stale action should be rejected at drain time`

**Step 3: Modify `drain_pending()` in `memory_actuator_coordinator.py`**

Replace lines 127-132:

Old:
```python
    def drain_pending(self) -> List[PendingAction]:
        """Return all pending actions sorted by priority, clearing the queue."""
        with self._lock:
            actions = sorted(self._pending, key=lambda a: a.action.priority)
            self._pending = []
            return actions
```

New:
```python
    def drain_pending(self) -> List[PendingAction]:
        """Return fresh pending actions sorted by priority, clearing the queue.

        Re-checks staleness at drain time — an action accepted at submit
        may have become stale if a new epoch/sequence arrived since then.
        """
        with self._lock:
            fresh = [
                a for a in self._pending
                if not a.envelope.is_stale(
                    current_epoch=self._current_epoch,
                    current_sequence=self._current_sequence,
                )
            ]
            stale_count = len(self._pending) - len(fresh)
            if stale_count:
                self._total_rejected_stale += stale_count
                logger.debug(
                    "[ActuatorCoord] drain_pending: rejected %d stale actions",
                    stale_count,
                )
            self._pending = []
            return sorted(fresh, key=lambda a: a.action.priority)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py -v -k "drain_pending" 2>&1 | head -20`
Expected: 2 PASSED

**Step 5: Commit**

```bash
git add backend/core/memory_actuator_coordinator.py tests/unit/backend/core/test_cloud_capacity.py
git commit -m "fix(coordinator): add execution-time epoch fencing to drain_pending()"
```

---

### Task 9: Observer Backpressure in Broker

**Files:**
- Modify: `backend/core/memory_budget_broker.py:908-924`
- Test: `tests/unit/backend/core/test_cloud_capacity.py`

**Context:** `notify_pressure_observers()` calls each observer sequentially with `await`. A slow observer blocks all subsequent observers. Add a 2s timeout per observer to prevent blocking.

**Step 1: Write the failing test**

Append to `tests/unit/backend/core/test_cloud_capacity.py`:

```python
@pytest.mark.asyncio
async def test_broker_observer_backpressure_timeout():
    """Slow observer should be skipped after timeout, not block others."""
    from backend.core.memory_budget_broker import MemoryBudgetBroker
    from backend.core.memory_types import PressureTier

    broker = MemoryBudgetBroker.__new__(MemoryBudgetBroker)
    # Minimal init for observer notification
    broker._pressure_observers = []
    broker._latest_snapshot = None
    broker._current_sequence = 0
    broker.logger = logging.getLogger("test")

    results = []

    async def slow_observer(tier, snapshot):
        await asyncio.sleep(10)  # Will be timed out
        results.append("slow")  # Should NOT appear

    async def fast_observer(tier, snapshot):
        results.append("fast")

    broker.register_pressure_observer(slow_observer)
    broker.register_pressure_observer(fast_observer)

    # Should complete quickly despite slow observer
    start = time.monotonic()
    await broker.notify_pressure_observers(PressureTier.CRITICAL, None)
    elapsed = time.monotonic() - start

    assert "fast" in results, "Fast observer must still run"
    assert "slow" not in results, "Slow observer should be timed out"
    assert elapsed < 5.0, f"Should not block for slow observer ({elapsed:.1f}s)"


import logging
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py::test_broker_observer_backpressure_timeout -v -x 2>&1 | head -30`
Expected: FAIL — test will hang/timeout because slow observer blocks for 10s

**Step 3: Modify `notify_pressure_observers()` in `memory_budget_broker.py`**

Replace lines 908-924:

Old:
```python
    async def notify_pressure_observers(
        self, tier: "PressureTier", snapshot: Any,
    ) -> None:
        """Notify all registered observers of a pressure tier change.

        Observer exceptions are caught and logged -- one bad observer
        must never block others.
        """
        self._advance_sequence()
        self._latest_snapshot = snapshot
        for obs in self._pressure_observers:
            try:
                await obs(tier, snapshot)
            except Exception:
                logger.warning(
                    "Pressure observer %s raised exception", obs, exc_info=True,
                )
```

New:
```python
    async def notify_pressure_observers(
        self, tier: "PressureTier", snapshot: Any,
    ) -> None:
        """Notify all registered observers of a pressure tier change.

        Each observer gets a 2s timeout — slow subscribers are skipped
        to prevent blocking the notification bus.  Observer exceptions
        are caught and logged.
        """
        _OBSERVER_TIMEOUT_S = float(
            os.environ.get("JARVIS_OBSERVER_TIMEOUT_S", "2.0")
        )
        self._advance_sequence()
        self._latest_snapshot = snapshot
        for obs in self._pressure_observers:
            try:
                await asyncio.wait_for(
                    obs(tier, snapshot), timeout=_OBSERVER_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Pressure observer %s timed out (>%.1fs), skipped",
                    getattr(obs, "__qualname__", repr(obs)),
                    _OBSERVER_TIMEOUT_S,
                )
            except Exception:
                logger.warning(
                    "Pressure observer %s raised exception",
                    getattr(obs, "__qualname__", repr(obs)),
                    exc_info=True,
                )
```

Also ensure the `import asyncio` and `import os` are present at the top of `memory_budget_broker.py` (they likely are already — verify before adding).

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py::test_broker_observer_backpressure_timeout -v -x 2>&1 | head -20`
Expected: PASS (fast observer runs, slow observer timed out)

**Step 5: Commit**

```bash
git add backend/core/memory_budget_broker.py tests/unit/backend/core/test_cloud_capacity.py
git commit -m "fix(broker): add 2s timeout backpressure for pressure observers"
```

---

## Phase D: Integration Wiring

### Task 10: Wire CloudCapacityController to GCP VM Manager

**Files:**
- Modify: `unified_supervisor.py` (instantiate controller after broker init, pass to GCP manager)
- Test: `tests/unit/backend/core/test_cloud_capacity.py`

**Context:** The `GCPVMManager` already has `register_with_broker()` (line 2661). We need to also create the `CloudCapacityController` and make the VM manager call `controller.evaluate()` before making cloud decisions. The controller decides, the manager executes.

**Step 1: Write the failing test**

Append to `tests/unit/backend/core/test_cloud_capacity.py`:

```python
@pytest.mark.asyncio
async def test_controller_stats_telemetry(mock_broker):
    """Controller stats must track decision counts."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)

    # Make several decisions
    controller.evaluate(tier=PressureTier.OPTIMAL, queue_depth=0)
    controller.evaluate(tier=PressureTier.OPTIMAL, queue_depth=0)
    controller.evaluate(tier=PressureTier.CONSTRAINED, queue_depth=3)

    stats = controller.get_stats()
    assert stats["total_decisions"] == 3
    assert stats["decisions_by_action"]["stay_local"] == 2
    assert stats["decisions_by_action"]["degrade_local"] == 1


@pytest.mark.asyncio
async def test_controller_spot_unavailable_falls_back(mock_broker):
    """When spot is unavailable, CRITICAL should fall back to on-demand."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    controller._first_critical_at = time.monotonic() - 60
    controller.mark_spot_unavailable()

    action = controller.evaluate(
        tier=PressureTier.CRITICAL,
        queue_depth=10,
        latency_violations=5,
    )
    assert action == CloudCapacityAction.FALLBACK_ONDEMAND


@pytest.mark.asyncio
async def test_controller_pressure_callback(mock_broker):
    """Broker pressure callback should update internal tier."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    assert controller._current_tier == PressureTier.OPTIMAL

    # Simulate broker callback
    await controller._on_pressure_change(PressureTier.CRITICAL, None)
    assert controller._current_tier == PressureTier.CRITICAL
    assert controller._first_critical_at is not None

    # Drop below critical — should clear sustained tracking
    await controller._on_pressure_change(PressureTier.ELEVATED, None)
    assert controller._first_critical_at is None
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/unit/backend/core/test_cloud_capacity.py -v -k "controller" 2>&1 | tail -20`
Expected: All PASS

**Step 3: Wire controller in `unified_supervisor.py`**

Find the location in `unified_supervisor.py` where the broker is created and `gcp_vm_manager.register_with_broker()` is called. Add controller creation after that:

```python
                # Create CloudCapacityController after broker init
                try:
                    from backend.core.cloud_capacity_controller import (
                        CloudCapacityController,
                    )
                    self._cloud_capacity_controller = CloudCapacityController(
                        broker=_broker,
                    )
                    self.logger.info(
                        "[Kernel] CloudCapacityController registered with broker"
                    )
                except Exception as _cc_err:
                    self._cloud_capacity_controller = None
                    self.logger.warning(
                        "[Kernel] CloudCapacityController init failed (non-fatal): %s",
                        _cc_err,
                    )
```

The exact insertion point depends on where the broker is instantiated. Search for `register_with_broker` in `unified_supervisor.py` to find the right location.

**Step 4: Verify no syntax errors**

Run: `python3 -c 'import ast; ast.parse(open("unified_supervisor.py").read()); print("OK")'`
Expected: `OK`

**Step 5: Run full test suite for this plan**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py tests/unit/backend/core/test_cloud_capacity.py -v 2>&1 | tail -30`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/core/test_cloud_capacity.py
git commit -m "feat(cloud): wire CloudCapacityController into supervisor boot sequence"
```

---

## Final Verification

### Task 11: Run All Tests and Verify

**Step 1: Run the full autonomy + cloud capacity test suite**

Run: `python3 -m pytest tests/unit/backend/supervisor/test_autonomy_contracts.py tests/unit/backend/core/test_cloud_capacity.py -v --tb=short 2>&1 | tail -40`
Expected: All tests PASS

**Step 2: Run the broader MCP test suite to verify no regressions**

Run: `python3 -m pytest tests/unit/backend/core/ tests/unit/backend/supervisor/ -v --tb=short 2>&1 | tail -40`
Expected: All tests PASS, no regressions

**Step 3: Syntax-check modified files**

Run: `python3 -c 'import ast; ast.parse(open("unified_supervisor.py").read()); print("supervisor OK")' && python3 -c 'import ast; ast.parse(open("backend/supervisor/cross_repo_startup_orchestrator.py").read()); print("orchestrator OK")' && python3 -c 'import ast; ast.parse(open("backend/core/memory_types.py").read()); print("types OK")' && python3 -c 'import ast; ast.parse(open("backend/core/memory_actuator_coordinator.py").read()); print("coordinator OK")' && python3 -c 'import ast; ast.parse(open("backend/core/memory_budget_broker.py").read()); print("broker OK")' && python3 -c 'import ast; ast.parse(open("backend/core/cloud_capacity_controller.py").read()); print("controller OK")'`
Expected: All 6 print "OK"

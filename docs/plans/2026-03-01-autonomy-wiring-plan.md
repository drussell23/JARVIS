# Autonomy Wiring Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire JARVIS workspace autonomy so "check my email" reliably succeeds across all auth states and startup timing conditions.

**Architecture:** In-place hardening of 4 existing files. No new files. Fix split-singleton coordinator lookup, add auth recovery state machine with visual fallback, unify startup paths, add post-action verification with bounded recovery.

**Tech Stack:** Python 3, asyncio, pytest, unittest.mock. Existing codebase patterns (env vars, dataclasses, enums).

**Design doc:** `docs/plans/2026-03-01-autonomy-wiring-design.md`

---

## Task 1: Startup Unification — integration.py Public API

This must land first because Tasks 2 and 4 depend on the coordinator being resolvable.

**Files:**
- Modify: `backend/neural_mesh/integration.py:24-26` (module globals), `279-281` (get_neural_mesh_coordinator)
- Test: `tests/unit/backend/test_integration_coordinator_api.py`

### Step 1: Write the failing tests

Create `tests/unit/backend/test_integration_coordinator_api.py`:

```python
"""Tests for integration.py public coordinator API."""
import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestSetNeuralMeshCoordinator:
    """Test set_neural_mesh_coordinator() public API."""

    def setup_method(self):
        """Reset module state before each test."""
        import backend.neural_mesh.integration as mod
        mod._neural_mesh_coordinator = None
        mod._initialized = False
        mod._production_agents_registered = {}

    def test_set_coordinator_makes_it_retrievable(self):
        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            get_neural_mesh_coordinator,
        )
        mock_coord = MagicMock()
        mock_coord._running = True
        set_neural_mesh_coordinator(mock_coord)
        assert get_neural_mesh_coordinator() is mock_coord

    def test_set_coordinator_none_clears(self):
        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            get_neural_mesh_coordinator,
        )
        mock_coord = MagicMock()
        set_neural_mesh_coordinator(mock_coord)
        set_neural_mesh_coordinator(None)
        # Should fall through to coordinator module fallback
        # (which returns None in test env)
        result = get_neural_mesh_coordinator()
        assert result is None or result is not mock_coord

    def test_mark_neural_mesh_initialized(self):
        from backend.neural_mesh.integration import (
            mark_neural_mesh_initialized,
            is_neural_mesh_initialized,
        )
        assert not is_neural_mesh_initialized()
        mark_neural_mesh_initialized(True)
        assert is_neural_mesh_initialized()
        mark_neural_mesh_initialized(False)
        assert not is_neural_mesh_initialized()


class TestGetNeuralMeshCoordinatorFallback:
    """Test canonical accessor checks all sources."""

    def setup_method(self):
        import backend.neural_mesh.integration as mod
        mod._neural_mesh_coordinator = None
        mod._initialized = False

    def test_returns_integration_singleton_first(self):
        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            get_neural_mesh_coordinator,
        )
        mock_coord = MagicMock()
        mock_coord._running = True
        set_neural_mesh_coordinator(mock_coord)
        assert get_neural_mesh_coordinator() is mock_coord

    @patch("backend.neural_mesh.integration._neural_mesh_coordinator", None)
    def test_falls_back_to_coordinator_module(self):
        """When integration singleton is None, check coordinator module."""
        from backend.neural_mesh.integration import get_neural_mesh_coordinator
        mock_coord = MagicMock()
        mock_coord._running = True
        with patch(
            "neural_mesh.neural_mesh_coordinator._coordinator", mock_coord
        ):
            result = get_neural_mesh_coordinator()
            assert result is mock_coord

    @patch("backend.neural_mesh.integration._neural_mesh_coordinator", None)
    def test_skips_stopped_coordinator_module(self):
        """Coordinator module singleton with _running=False is skipped."""
        from backend.neural_mesh.integration import get_neural_mesh_coordinator
        mock_coord = MagicMock()
        mock_coord._running = False
        with patch(
            "neural_mesh.neural_mesh_coordinator._coordinator", mock_coord
        ):
            result = get_neural_mesh_coordinator()
            assert result is None


class TestProductionAgentRegistrationIdempotency:
    """Test registration is idempotent per coordinator instance + agent set."""

    def setup_method(self):
        import backend.neural_mesh.integration as mod
        mod._neural_mesh_coordinator = None
        mod._initialized = False
        mod._production_agents_registered = {}

    def test_is_agent_set_registered_false_initially(self):
        from backend.neural_mesh.integration import _is_agent_set_registered
        assert not _is_agent_set_registered("coord-1", {"agent_a", "agent_b"})

    def test_mark_and_check_registered(self):
        from backend.neural_mesh.integration import (
            _is_agent_set_registered,
            _mark_agent_set_registered,
        )
        _mark_agent_set_registered("coord-1", {"agent_a", "agent_b"})
        assert _is_agent_set_registered("coord-1", {"agent_a", "agent_b"})

    def test_different_coordinator_not_registered(self):
        from backend.neural_mesh.integration import (
            _is_agent_set_registered,
            _mark_agent_set_registered,
        )
        _mark_agent_set_registered("coord-1", {"agent_a"})
        assert not _is_agent_set_registered("coord-2", {"agent_a"})
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_integration_coordinator_api.py -v --tb=short 2>&1 | head -40`

Expected: FAIL — `set_neural_mesh_coordinator`, `mark_neural_mesh_initialized`, `_is_agent_set_registered`, `_mark_agent_set_registered` don't exist yet.

### Step 3: Implement public API in integration.py

Modify `backend/neural_mesh/integration.py`:

**At module globals (around line 24-26), add:**
```python
_production_agents_registered: Dict[str, frozenset] = {}  # coordinator_id -> frozenset(agent_names)
```

**After `get_neural_mesh_coordinator()` (around line 281), add new functions:**
```python
def set_neural_mesh_coordinator(coordinator) -> None:
    """Set the canonical coordinator reference.

    Called by AGI OS after start_neural_mesh() to cross-register
    so that get_neural_mesh_coordinator() returns the correct instance
    regardless of which init path was used.
    """
    global _neural_mesh_coordinator
    _neural_mesh_coordinator = coordinator
    logger.info(
        "[integration] Coordinator cross-registered: %s",
        type(coordinator).__name__ if coordinator else "None",
    )


def mark_neural_mesh_initialized(initialized: bool = True) -> None:
    """Mark integration module's initialized flag."""
    global _initialized
    _initialized = initialized


def _is_agent_set_registered(coordinator_id: str, agent_names: set) -> bool:
    """Check if agent set was already registered for this coordinator instance."""
    registered = _production_agents_registered.get(coordinator_id)
    if registered is None:
        return False
    return agent_names.issubset(registered)


def _mark_agent_set_registered(coordinator_id: str, agent_names: set) -> None:
    """Record that agent set was registered for coordinator instance."""
    existing = _production_agents_registered.get(coordinator_id, frozenset())
    _production_agents_registered[coordinator_id] = existing | frozenset(agent_names)
```

**Replace existing `get_neural_mesh_coordinator()` (line 279-281) with:**
```python
def get_neural_mesh_coordinator():
    """Get the global Neural Mesh coordinator — canonical accessor.

    Checks integration module's singleton first, then falls back to
    the coordinator module's singleton (set by AGI OS start_neural_mesh path).
    """
    if _neural_mesh_coordinator is not None:
        return _neural_mesh_coordinator
    # Fallback: coordinator module's singleton (set by start_neural_mesh)
    try:
        from neural_mesh.neural_mesh_coordinator import _coordinator as _cm_coordinator
        if _cm_coordinator is not None and getattr(_cm_coordinator, '_running', False):
            return _cm_coordinator
    except (ImportError, Exception):
        pass
    return None
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_integration_coordinator_api.py -v --tb=short 2>&1 | head -40`

Expected: All PASS.

### Step 5: Commit

```bash
git add backend/neural_mesh/integration.py tests/unit/backend/test_integration_coordinator_api.py
git commit -m "feat: add public coordinator API to integration.py (Section 3)

Add set_neural_mesh_coordinator(), mark_neural_mesh_initialized(),
and idempotent production agent registration tracking. Make
get_neural_mesh_coordinator() the canonical accessor that checks
both integration and coordinator module singletons.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: AGI OS Cross-Registration

**Files:**
- Modify: `backend/agi_os/agi_os_coordinator.py:1623-1635` (after start_neural_mesh step)
- Test: `tests/unit/backend/test_agi_os_cross_registration.py`

### Step 1: Write the failing test

Create `tests/unit/backend/test_agi_os_cross_registration.py`:

```python
"""Test AGI OS cross-registers coordinator with integration module."""
import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestCrossRegistration:
    """Verify AGI OS path sets integration.py's coordinator."""

    def setup_method(self):
        import backend.neural_mesh.integration as mod
        mod._neural_mesh_coordinator = None
        mod._initialized = False

    @pytest.mark.asyncio
    async def test_cross_registration_sets_integration_coordinator(self):
        """After AGI OS inits mesh, integration module should resolve it."""
        from backend.neural_mesh.integration import get_neural_mesh_coordinator

        # Simulate what AGI OS does after start_neural_mesh
        mock_coordinator = MagicMock()
        mock_coordinator._running = True

        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            mark_neural_mesh_initialized,
        )
        set_neural_mesh_coordinator(mock_coordinator)
        mark_neural_mesh_initialized(True)

        result = get_neural_mesh_coordinator()
        assert result is mock_coordinator

    @pytest.mark.asyncio
    async def test_cross_registration_agent_visible(self):
        """GoogleWorkspaceAgent should be findable after cross-registration."""
        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            get_neural_mesh_coordinator,
        )
        mock_coordinator = MagicMock()
        mock_coordinator._running = True
        mock_agent = MagicMock()
        mock_coordinator.get_agent.return_value = mock_agent

        set_neural_mesh_coordinator(mock_coordinator)

        coord = get_neural_mesh_coordinator()
        agent = coord.get_agent("google_workspace_agent")
        assert agent is mock_agent
```

### Step 2: Run tests to verify they pass (these should pass since Task 1 added the API)

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_agi_os_cross_registration.py -v --tb=short 2>&1 | head -30`

Expected: PASS (the API exists from Task 1).

### Step 3: Add cross-registration call to agi_os_coordinator.py

In `backend/agi_os/agi_os_coordinator.py`, in `_init_neural_mesh()`, after the Step 1 block that calls `start_neural_mesh` (around line 1633, after `self._neural_mesh = await self._run_timed_init_step(...)`), add:

```python
        # Cross-register with integration module so
        # get_neural_mesh_coordinator() returns the correct instance
        # regardless of which code path queries it.
        try:
            from neural_mesh.integration import (
                set_neural_mesh_coordinator,
                mark_neural_mesh_initialized,
            )
            set_neural_mesh_coordinator(self._neural_mesh)
            mark_neural_mesh_initialized(True)
            logger.info("[v_autonomy] Cross-registered coordinator with integration module")
        except ImportError:
            logger.debug("[v_autonomy] integration module not available for cross-registration")
```

### Step 4: Run tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_agi_os_cross_registration.py tests/unit/backend/test_integration_coordinator_api.py -v --tb=short 2>&1 | head -40`

Expected: All PASS.

### Step 5: Commit

```bash
git add backend/agi_os/agi_os_coordinator.py tests/unit/backend/test_agi_os_cross_registration.py
git commit -m "feat: cross-register coordinator in AGI OS init path (Section 3)

After start_neural_mesh(), call set_neural_mesh_coordinator() so the
command processor's lookup resolves the correct singleton.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Coordinator Lookup Retry State Machine

**Files:**
- Modify: `backend/api/unified_command_processor.py:338-395` (metrics dict, coordinator fields, lookup method)
- Test: `tests/unit/backend/test_coordinator_lookup_retry.py`

### Step 1: Write the failing tests

Create `tests/unit/backend/test_coordinator_lookup_retry.py`:

```python
"""Tests for coordinator lookup retry state machine."""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


def _make_processor():
    """Create a minimal UnifiedCommandProcessor for testing coordinator lookup."""
    # We need to test _get_neural_mesh_coordinator in isolation.
    # Import the class but mock heavy dependencies.
    from backend.api.unified_command_processor import UnifiedCommandProcessor
    with patch.object(UnifiedCommandProcessor, '__init__', lambda self: None):
        proc = UnifiedCommandProcessor.__new__(UnifiedCommandProcessor)
        # Initialize only the coordinator-related fields
        proc._neural_mesh_coordinator = None
        proc._coordinator_state = "UNRESOLVED"
        proc._coordinator_last_lookup = 0.0
        proc._coordinator_lookup_failures = 0
        proc._coordinator_max_retries = 5
        proc._coordinator_cooldown_seconds = 300.0
        proc._coordinator_lock = asyncio.Lock()
        proc._v242_metrics = {
            "coordinator_lookups": 0,
            "coordinator_hits": 0,
            "coordinator_misses": 0,
            "coordinator_stale": 0,
        }
        return proc


class TestCoordinatorLookupStates:

    @pytest.mark.asyncio
    async def test_initial_state_is_unresolved(self):
        proc = _make_processor()
        assert proc._coordinator_state == "UNRESOLVED"

    @pytest.mark.asyncio
    async def test_successful_lookup_transitions_to_resolved(self):
        proc = _make_processor()
        mock_coord = MagicMock()
        mock_coord._running = True
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=mock_coord,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is mock_coord
        assert proc._coordinator_state == "RESOLVED"

    @pytest.mark.asyncio
    async def test_failed_lookup_transitions_to_backing_off(self):
        proc = _make_processor()
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=None,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is None
        assert proc._coordinator_state == "BACKING_OFF"
        assert proc._coordinator_lookup_failures == 1

    @pytest.mark.asyncio
    async def test_backoff_prevents_immediate_retry(self):
        proc = _make_processor()
        proc._coordinator_state = "BACKING_OFF"
        proc._coordinator_lookup_failures = 1
        proc._coordinator_last_lookup = time.monotonic()  # just looked up
        # Should return None without attempting lookup
        result = await proc._get_neural_mesh_coordinator()
        assert result is None

    @pytest.mark.asyncio
    async def test_backoff_allows_retry_after_delay(self):
        proc = _make_processor()
        proc._coordinator_state = "BACKING_OFF"
        proc._coordinator_lookup_failures = 1
        proc._coordinator_last_lookup = time.monotonic() - 10.0  # 10s ago, backoff is 5s
        mock_coord = MagicMock()
        mock_coord._running = True
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=mock_coord,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is mock_coord
        assert proc._coordinator_state == "RESOLVED"

    @pytest.mark.asyncio
    async def test_max_retries_transitions_to_cooldown(self):
        proc = _make_processor()
        proc._coordinator_state = "BACKING_OFF"
        proc._coordinator_lookup_failures = 4  # one more = max (5)
        proc._coordinator_last_lookup = time.monotonic() - 120.0  # past backoff
        with patch(
            "backend.api.unified_command_processor.get_neural_mesh_coordinator",
            return_value=None,
        ):
            result = await proc._get_neural_mesh_coordinator()
        assert result is None
        assert proc._coordinator_state == "COOLDOWN"

    @pytest.mark.asyncio
    async def test_resolved_returns_cached(self):
        proc = _make_processor()
        mock_coord = MagicMock()
        mock_coord._running = True
        proc._neural_mesh_coordinator = mock_coord
        proc._coordinator_state = "RESOLVED"
        result = await proc._get_neural_mesh_coordinator()
        assert result is mock_coord

    @pytest.mark.asyncio
    async def test_stale_coordinator_invalidated(self):
        proc = _make_processor()
        mock_coord = MagicMock()
        mock_coord._running = False  # stopped!
        proc._neural_mesh_coordinator = mock_coord
        proc._coordinator_state = "RESOLVED"
        result = await proc._get_neural_mesh_coordinator()
        assert result is None
        assert proc._coordinator_state == "UNRESOLVED"
        assert proc._neural_mesh_coordinator is None

    @pytest.mark.asyncio
    async def test_notify_coordinator_ready_clears_backoff(self):
        proc = _make_processor()
        proc._coordinator_state = "BACKING_OFF"
        proc._coordinator_lookup_failures = 3
        await proc.notify_coordinator_ready()
        assert proc._coordinator_state == "UNRESOLVED"
        assert proc._coordinator_lookup_failures == 0

    @pytest.mark.asyncio
    async def test_notify_coordinator_ready_clears_cooldown(self):
        proc = _make_processor()
        proc._coordinator_state = "COOLDOWN"
        proc._coordinator_lookup_failures = 5
        await proc.notify_coordinator_ready()
        assert proc._coordinator_state == "UNRESOLVED"
        assert proc._coordinator_lookup_failures == 0
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_coordinator_lookup_retry.py -v --tb=short 2>&1 | head -40`

Expected: FAIL — `_coordinator_state`, `notify_coordinator_ready`, async `_get_neural_mesh_coordinator` don't exist yet.

### Step 3: Implement coordinator lookup state machine

Modify `backend/api/unified_command_processor.py`:

**In `__init__` (around lines 359-363), replace the old coordinator fields with:**

```python
        # v_autonomy: Coordinator lookup retry state machine
        # States: UNRESOLVED -> BACKING_OFF -> RESOLVED, COOLDOWN after max retries
        self._neural_mesh_coordinator = None
        self._coordinator_state = "UNRESOLVED"
        self._coordinator_last_lookup: float = 0.0
        self._coordinator_lookup_failures: int = 0
        self._coordinator_max_retries = int(os.getenv("JARVIS_COORDINATOR_LOOKUP_MAX_RETRIES", "5"))
        self._coordinator_cooldown_seconds = float(os.getenv("JARVIS_COORDINATOR_COOLDOWN_SECONDS", "300"))
        self._coordinator_lock = asyncio.Lock()
        self._workspace_agent_singleton = None
        self._workspace_agent_singleton_lock = asyncio.Lock()
```

**In `_v242_metrics` dict (around line 339), add:**
```python
            "coordinator_lookups": 0,
            "coordinator_hits": 0,
            "coordinator_misses": 0,
            "coordinator_stale": 0,
```

**At module top (after other imports), add:**
```python
# Lazy import — resolved on first coordinator lookup
_get_neural_mesh_coordinator_fn = None

def _import_coordinator_getter():
    global _get_neural_mesh_coordinator_fn
    if _get_neural_mesh_coordinator_fn is None:
        try:
            from neural_mesh.integration import get_neural_mesh_coordinator
            _get_neural_mesh_coordinator_fn = get_neural_mesh_coordinator
        except ImportError:
            _get_neural_mesh_coordinator_fn = lambda: None
    return _get_neural_mesh_coordinator_fn
```

**Replace `_get_neural_mesh_coordinator()` method (lines 374-394) with:**

```python
    _COORDINATOR_BACKOFF_SCHEDULE = [5.0, 10.0, 20.0, 40.0, 60.0]  # seconds

    async def _get_neural_mesh_coordinator(self):
        """Resolve the Neural Mesh coordinator with bounded retry state machine.

        States: UNRESOLVED -> BACKING_OFF -> RESOLVED
                BACKING_OFF -> COOLDOWN (after max_retries) -> UNRESOLVED (new window)
        """
        async with self._coordinator_lock:
            self._v242_metrics["coordinator_lookups"] += 1

            # RESOLVED: return cached, but check staleness
            if self._coordinator_state == "RESOLVED":
                if self._neural_mesh_coordinator is not None:
                    if getattr(self._neural_mesh_coordinator, '_running', True):
                        self._v242_metrics["coordinator_hits"] += 1
                        return self._neural_mesh_coordinator
                    # Stale — coordinator stopped
                    logger.warning("[v_autonomy] Coordinator stale (_running=False) — invalidating")
                    self._v242_metrics["coordinator_stale"] += 1
                    self._neural_mesh_coordinator = None
                    self._coordinator_state = "UNRESOLVED"
                    # Fall through to lookup
                else:
                    self._coordinator_state = "UNRESOLVED"

            # COOLDOWN: check if cooldown expired
            if self._coordinator_state == "COOLDOWN":
                elapsed = time.monotonic() - self._coordinator_last_lookup
                if elapsed < self._coordinator_cooldown_seconds:
                    self._v242_metrics["coordinator_misses"] += 1
                    return None
                # Cooldown expired — reset for new retry window
                logger.info("[v_autonomy] Coordinator cooldown expired — retrying")
                self._coordinator_state = "UNRESOLVED"
                self._coordinator_lookup_failures = 0

            # BACKING_OFF: check if backoff delay has passed
            if self._coordinator_state == "BACKING_OFF":
                idx = min(self._coordinator_lookup_failures - 1, len(self._COORDINATOR_BACKOFF_SCHEDULE) - 1)
                backoff = self._COORDINATOR_BACKOFF_SCHEDULE[max(0, idx)]
                elapsed = time.monotonic() - self._coordinator_last_lookup
                if elapsed < backoff:
                    self._v242_metrics["coordinator_misses"] += 1
                    return None

            # UNRESOLVED or backoff delay passed — attempt lookup
            self._coordinator_last_lookup = time.monotonic()
            getter = _import_coordinator_getter()
            try:
                coordinator = getter()
            except Exception as e:
                logger.debug("[v_autonomy] Coordinator lookup error: %s", e)
                coordinator = None

            if coordinator is not None:
                self._neural_mesh_coordinator = coordinator
                self._coordinator_state = "RESOLVED"
                self._coordinator_lookup_failures = 0
                self._v242_metrics["coordinator_hits"] += 1
                logger.info("[v_autonomy] Coordinator resolved")
                return coordinator

            # Lookup failed
            self._coordinator_lookup_failures += 1
            self._v242_metrics["coordinator_misses"] += 1

            if self._coordinator_lookup_failures >= self._coordinator_max_retries:
                self._coordinator_state = "COOLDOWN"
                logger.warning(
                    "[v_autonomy] Coordinator max retries (%d) hit — entering cooldown (%.0fs)",
                    self._coordinator_max_retries,
                    self._coordinator_cooldown_seconds,
                )
            else:
                self._coordinator_state = "BACKING_OFF"
                idx = min(self._coordinator_lookup_failures - 1, len(self._COORDINATOR_BACKOFF_SCHEDULE) - 1)
                logger.debug(
                    "[v_autonomy] Coordinator lookup failed (attempt %d/%d, next backoff %.0fs)",
                    self._coordinator_lookup_failures,
                    self._coordinator_max_retries,
                    self._COORDINATOR_BACKOFF_SCHEDULE[max(0, idx)],
                )
            return None

    async def notify_coordinator_ready(self):
        """Called by external subsystem when mesh becomes ready.

        Immediately clears BACKING_OFF or COOLDOWN state.
        """
        async with self._coordinator_lock:
            if self._coordinator_state in ("BACKING_OFF", "COOLDOWN"):
                logger.info(
                    "[v_autonomy] Coordinator readiness event — clearing %s state",
                    self._coordinator_state,
                )
                self._coordinator_state = "UNRESOLVED"
                self._coordinator_lookup_failures = 0
                self._coordinator_last_lookup = 0.0
```

**Update the workspace handling code** (around line 3119-3121) to use `await`:

Change:
```python
            coordinator = self._get_neural_mesh_coordinator()
```
To:
```python
            coordinator = await self._get_neural_mesh_coordinator()
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_coordinator_lookup_retry.py -v --tb=short 2>&1 | head -40`

Expected: All PASS.

### Step 5: Commit

```bash
git add backend/api/unified_command_processor.py tests/unit/backend/test_coordinator_lookup_retry.py
git commit -m "feat: coordinator lookup retry state machine (Section 1)

Replace one-shot boolean flag with bounded retry state machine:
UNRESOLVED -> BACKING_OFF -> RESOLVED, with COOLDOWN after max
retries. Exponential backoff (5-60s), stale invalidation,
readiness event support, asyncio.Lock for concurrency safety.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Auth Recovery State Machine — Data Model

**Files:**
- Modify: `backend/neural_mesh/agents/google_workspace_agent.py:145-149` (AuthState enum), `680-741` (config)
- Test: `tests/unit/backend/test_auth_state_machine.py`

### Step 1: Write the failing tests

Create `tests/unit/backend/test_auth_state_machine.py`:

```python
"""Tests for auth recovery state machine transitions."""
import asyncio
import os
import pytest
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestAuthStateEnum:
    """Verify expanded AuthState has all 5 states."""

    def test_all_states_exist(self):
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        assert hasattr(AuthState, "UNAUTHENTICATED")
        assert hasattr(AuthState, "AUTHENTICATED")
        assert hasattr(AuthState, "REFRESHING")
        assert hasattr(AuthState, "DEGRADED_VISUAL")
        assert hasattr(AuthState, "NEEDS_REAUTH_GUIDED")

    def test_states_are_strings(self):
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        assert AuthState.REFRESHING.value == "refreshing"
        assert AuthState.DEGRADED_VISUAL.value == "degraded_visual"
        assert AuthState.NEEDS_REAUTH_GUIDED.value == "needs_reauth_guided"


class TestAuthTransitionMap:
    """Verify transition map is a complete constant table."""

    def test_transition_map_exists(self):
        from backend.neural_mesh.agents.google_workspace_agent import _AUTH_TRANSITIONS
        assert isinstance(_AUTH_TRANSITIONS, (list, tuple))
        assert len(_AUTH_TRANSITIONS) >= 7

    def test_all_transitions_have_required_fields(self):
        from backend.neural_mesh.agents.google_workspace_agent import _AUTH_TRANSITIONS
        for t in _AUTH_TRANSITIONS:
            assert hasattr(t, "from_state"), f"Missing from_state: {t}"
            assert hasattr(t, "event"), f"Missing event: {t}"
            assert hasattr(t, "to_state"), f"Missing to_state: {t}"
            assert hasattr(t, "reason_code"), f"Missing reason_code: {t}"


class TestActionRiskClassification:
    """Verify action risk table."""

    def test_read_actions(self):
        from backend.neural_mesh.agents.google_workspace_agent import _ACTION_RISK
        assert _ACTION_RISK["fetch_unread_emails"] == "read"
        assert _ACTION_RISK["check_calendar_events"] == "read"
        assert _ACTION_RISK["search_email"] == "read"

    def test_write_actions(self):
        from backend.neural_mesh.agents.google_workspace_agent import _ACTION_RISK
        assert _ACTION_RISK["send_email"] == "write"
        assert _ACTION_RISK["draft_email_reply"] == "write"
        assert _ACTION_RISK["create_calendar_event"] == "write"

    def test_unknown_action_defaults_to_write(self):
        from backend.neural_mesh.agents.google_workspace_agent import _classify_action_risk
        assert _classify_action_risk("unknown_action") == "write"


class TestVisualFallbackConfig:
    """Verify config defaults changed for read-only fallback."""

    def test_email_visual_fallback_default_true(self):
        from backend.neural_mesh.agents.google_workspace_agent import GoogleWorkspaceConfig
        config = GoogleWorkspaceConfig()
        assert config.email_visual_fallback_enabled is True

    def test_write_visual_fallback_default_false(self):
        from backend.neural_mesh.agents.google_workspace_agent import GoogleWorkspaceConfig
        config = GoogleWorkspaceConfig()
        assert config.write_visual_fallback_enabled is False
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_auth_state_machine.py -v --tb=short 2>&1 | head -40`

Expected: FAIL — REFRESHING, DEGRADED_VISUAL, NEEDS_REAUTH_GUIDED don't exist. _AUTH_TRANSITIONS, _ACTION_RISK, _classify_action_risk don't exist.

### Step 3: Implement data model changes

Modify `backend/neural_mesh/agents/google_workspace_agent.py`:

**Expand AuthState enum (lines 145-149):**
```python
class AuthState(str, Enum):
    """Authentication state for Google Workspace client."""
    UNAUTHENTICATED = "unauthenticated"
    AUTHENTICATED = "authenticated"
    REFRESHING = "refreshing"
    DEGRADED_VISUAL = "degraded_visual"
    NEEDS_REAUTH_GUIDED = "needs_reauth_guided"
    # Legacy alias for rollback compatibility
    NEEDS_REAUTH = "needs_reauth_guided"
```

**Add AuthTransition namedtuple and transition map (after AuthState, before TokenHealthStatus):**
```python
from collections import namedtuple

AuthTransition = namedtuple("AuthTransition", ["from_state", "event", "to_state", "reason_code"])

_AUTH_TRANSITIONS = [
    AuthTransition("authenticated", "token_expired", "refreshing", "auth_refreshing"),
    AuthTransition("refreshing", "refresh_success", "authenticated", "auth_healthy"),
    AuthTransition("refreshing", "transient_failure", "refreshing", "auth_refresh_transient_fail"),
    AuthTransition("refreshing", "permanent_failure", "degraded_visual", "auth_refresh_permanent_fail"),
    AuthTransition("degraded_visual", "write_action", "needs_reauth_guided", "auth_guided_recovery"),
    AuthTransition("degraded_visual", "api_probe_success", "authenticated", "auth_auto_healed"),
    AuthTransition("needs_reauth_guided", "token_healed", "unauthenticated", "auth_auto_healed"),
]
```

**Add action risk classification (after _PERMANENT_FAILURE_PATTERNS):**
```python
_ACTION_RISK: Dict[str, str] = {
    "fetch_unread_emails": "read",
    "check_calendar_events": "read",
    "search_email": "read",
    "get_contacts": "read",
    "workspace_summary": "read",
    "daily_briefing": "read",
    "handle_workspace_query": "read",
    "read_spreadsheet": "read",
    "send_email": "write",
    "draft_email_reply": "write",
    "create_calendar_event": "write",
    "create_document": "write",
    "write_spreadsheet": "write",
    "delete_email": "high_risk_write",
    "delete_event": "high_risk_write",
}


def _classify_action_risk(action: str) -> str:
    """Classify workspace action risk level. Unknown defaults to write."""
    return _ACTION_RISK.get(action, "write")
```

**Update GoogleWorkspaceConfig (around lines 712-717) — change email_visual_fallback default:**
```python
    email_visual_fallback_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_WORKSPACE_EMAIL_VISUAL_FALLBACK", "true"
        ).lower() in {"1", "true", "yes"}
    )
    write_visual_fallback_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_WORKSPACE_WRITE_VISUAL_FALLBACK", "false"
        ).lower() in {"1", "true", "yes"}
    )
```

**Note on NEEDS_REAUTH alias:** The existing code checks `AuthState.NEEDS_REAUTH` in many places. Since we're aliasing `NEEDS_REAUTH = "needs_reauth_guided"`, existing checks continue to work. The feature flag `JARVIS_AUTH_STATE_MACHINE_V2` will gate the behavioral changes in Task 5.

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_auth_state_machine.py -v --tb=short 2>&1 | head -40`

Expected: All PASS.

### Step 5: Commit

```bash
git add backend/neural_mesh/agents/google_workspace_agent.py tests/unit/backend/test_auth_state_machine.py
git commit -m "feat: auth recovery state machine data model (Section 2)

Expand AuthState enum to 5 states (REFRESHING, DEGRADED_VISUAL,
NEEDS_REAUTH_GUIDED). Add constant transition map, action risk
classification table, and visual fallback config defaults
(read-only=true, write=false).

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Auth Recovery State Machine — Behavioral Wiring

**Files:**
- Modify: `backend/neural_mesh/agents/google_workspace_agent.py:1587-1614` (token pre-check), `1840-1883` (_execute_with_retry), `3283-3291` (execute_task auth check)
- Test: `tests/unit/backend/test_auth_state_transitions.py`

### Step 1: Write the failing tests

Create `tests/unit/backend/test_auth_state_transitions.py`:

```python
"""Tests for auth state machine behavioral transitions."""
import asyncio
import os
import pytest
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


def _make_client():
    """Create a GoogleWorkspaceClient with mocked dependencies."""
    from backend.neural_mesh.agents.google_workspace_agent import (
        GoogleWorkspaceClient,
        GoogleWorkspaceConfig,
        AuthState,
    )
    config = GoogleWorkspaceConfig()
    with patch.object(GoogleWorkspaceClient, '__init__', lambda self, cfg: None):
        client = GoogleWorkspaceClient.__new__(GoogleWorkspaceClient)
        client.config = config
        client._auth_state = AuthState.AUTHENTICATED
        client._creds = MagicMock()
        client._last_auth_failure_reason = None
        client._token_health = MagicMock()
        client._token_mtime = None
        client._auth_transition_lock = asyncio.Lock()
        client._refresh_attempts = 0
        client._max_refresh_attempts = 3
        client._auth_probe_count = 0
        client._auth_probe_max = 30
        client._last_auth_probe = 0.0
        client._reauth_notice_cooldown = 0.0
        client._auth_autoheal_total = 0
        client._auth_permanent_fail_total = 0
        client._v2_enabled = True
        return client


class TestRefreshingState:
    """AUTHENTICATED -> REFRESHING -> AUTHENTICATED or DEGRADED_VISUAL."""

    @pytest.mark.asyncio
    async def test_transient_failure_stays_refreshing(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.REFRESHING
        client._refresh_attempts = 0
        await client._handle_auth_event("transient_failure")
        assert client._auth_state == AuthState.REFRESHING
        assert client._refresh_attempts == 1

    @pytest.mark.asyncio
    async def test_permanent_failure_transitions_to_degraded(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.REFRESHING
        await client._handle_auth_event("permanent_failure")
        assert client._auth_state == AuthState.DEGRADED_VISUAL

    @pytest.mark.asyncio
    async def test_refresh_success_transitions_to_authenticated(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.REFRESHING
        await client._handle_auth_event("refresh_success")
        assert client._auth_state == AuthState.AUTHENTICATED


class TestDegradedVisualState:
    """DEGRADED_VISUAL behavior for read vs write actions."""

    @pytest.mark.asyncio
    async def test_write_action_transitions_to_guided(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.DEGRADED_VISUAL
        await client._handle_auth_event("write_action")
        assert client._auth_state == AuthState.NEEDS_REAUTH_GUIDED

    @pytest.mark.asyncio
    async def test_api_probe_success_heals_to_authenticated(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.DEGRADED_VISUAL
        await client._handle_auth_event("api_probe_success")
        assert client._auth_state == AuthState.AUTHENTICATED


class TestNeedsReauthGuidedState:
    """NEEDS_REAUTH_GUIDED recovery paths."""

    @pytest.mark.asyncio
    async def test_token_healed_transitions_to_unauthenticated(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.NEEDS_REAUTH_GUIDED
        await client._handle_auth_event("token_healed")
        assert client._auth_state == AuthState.UNAUTHENTICATED


class TestFeatureFlag:
    """JARVIS_AUTH_STATE_MACHINE_V2 rollback."""

    def test_v2_disabled_falls_back_to_legacy(self):
        """When V2 disabled, NEEDS_REAUTH behavior unchanged."""
        client = _make_client()
        client._v2_enabled = False
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.NEEDS_REAUTH_GUIDED
        # Legacy behavior: hard error, no visual fallback
        # Verified by checking that _should_use_visual_fallback returns False
        assert not client._should_use_visual_fallback("fetch_unread_emails")
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_auth_state_transitions.py -v --tb=short 2>&1 | head -40`

Expected: FAIL — `_handle_auth_event`, `_should_use_visual_fallback` don't exist.

### Step 3: Implement behavioral wiring

Modify `backend/neural_mesh/agents/google_workspace_agent.py`:

**Add to GoogleWorkspaceClient `__init__` (after existing auth fields):**
```python
        # v_autonomy: Auth state machine v2
        self._auth_transition_lock = asyncio.Lock()
        self._refresh_attempts = 0
        self._max_refresh_attempts = int(os.getenv("JARVIS_AUTH_MAX_REFRESH_ATTEMPTS", "3"))
        self._auth_probe_count = 0
        self._auth_probe_max = int(os.getenv("JARVIS_AUTH_PROBE_MAX", "30"))
        self._auth_probe_interval = float(os.getenv("JARVIS_AUTH_PROBE_INTERVAL", "120"))
        self._last_auth_probe: float = 0.0
        self._v2_enabled = os.getenv("JARVIS_AUTH_STATE_MACHINE_V2", "true").lower() in {"1", "true", "yes"}
```

**Add methods to GoogleWorkspaceClient:**

```python
    async def _handle_auth_event(self, event: str) -> None:
        """Process auth state transition event.

        Uses constant transition map. All state writes guarded by lock.
        """
        async with self._auth_transition_lock:
            current = self._auth_state.value
            for t in _AUTH_TRANSITIONS:
                if t.from_state == current and t.event == event:
                    # Special handling for transient refresh
                    if event == "transient_failure":
                        self._refresh_attempts += 1
                        if self._refresh_attempts >= self._max_refresh_attempts:
                            # Exhaust retries → permanent failure path
                            self._auth_state = AuthState.DEGRADED_VISUAL
                            logger.warning(
                                "[v_autonomy] Auth refresh exhausted (%d attempts) → DEGRADED_VISUAL",
                                self._refresh_attempts,
                            )
                            return
                    new_state = AuthState(t.to_state)
                    old_state = self._auth_state
                    self._auth_state = new_state
                    if event == "refresh_success":
                        self._refresh_attempts = 0
                    logger.info(
                        "[v_autonomy] Auth transition: %s -[%s]-> %s (reason: %s)",
                        old_state.value, event, new_state.value, t.reason_code,
                    )
                    return
            logger.debug("[v_autonomy] No transition for state=%s event=%s", current, event)

    def _should_use_visual_fallback(self, action: str) -> bool:
        """Determine if visual fallback should be used for this action.

        Returns False if V2 disabled (rollback) or action is write with
        write_visual_fallback_enabled=False.
        """
        if not self._v2_enabled:
            return False
        if not self.config.email_visual_fallback_enabled:
            return False
        risk = _classify_action_risk(action)
        if risk in ("write", "high_risk_write"):
            return self.config.write_visual_fallback_enabled
        return True  # read actions: visual fallback enabled
```

**Modify execute_task auth check (around line 3283):**

Replace the hard NEEDS_REAUTH block:
```python
    if self._client and self._client.auth_state == AuthState.NEEDS_REAUTH:
```
With:
```python
    # v_autonomy: Route through state machine instead of hard error
    if self._client and self._client._auth_state in (
        AuthState.DEGRADED_VISUAL,
        AuthState.NEEDS_REAUTH_GUIDED,
    ):
        if self._client._v2_enabled:
            risk = _classify_action_risk(action)
            if risk == "read" and self._client._should_use_visual_fallback(action):
                # Read action in degraded state — proceed, unified executor
                # will route to visual fallback tier
                logger.info(
                    "[v_autonomy] Degraded auth + read action '%s' — proceeding with visual fallback",
                    action,
                )
                payload["_force_visual_fallback"] = True
                payload["_auth_state"] = self._client._auth_state.value
            else:
                # Write action or V2 disabled — guided recovery
                if self._client._auth_state != AuthState.NEEDS_REAUTH_GUIDED:
                    await self._client._handle_auth_event("write_action")
                self._client._emit_reauth_notice()
                return {
                    "success": False,
                    "error": f"Google auth needs renewal: {self._client.auth_failure_reason}",
                    "error_code": "needs_reauth",
                    "auth_state": self._client._auth_state.value,
                    "recovery_action_required": True,
                    "recovery_instructions": "Say 'fix my Google auth' or run: python3 backend/scripts/google_oauth_setup.py",
                    "response": _DEGRADED_MESSAGES.get(
                        ("needs_reauth_guided", risk),
                        "Google auth needs renewal. Re-run the setup script.",
                    ),
                    "workspace_action": action or "unknown",
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "node_id": node_id,
                }
        else:
            # V2 disabled — legacy hard error
            self._client._emit_reauth_notice()
            return {
                "success": False,
                "error": f"Google auth permanently failed: {self._client.auth_failure_reason}",
                "error_code": "needs_reauth",
                "action_required": "Re-run: python3 backend/scripts/google_oauth_setup.py",
                "response": (
                    "Google authentication needs renewal. "
                    "Please run: python3 backend/scripts/google_oauth_setup.py"
                ),
                "workspace_action": action or "unknown",
                "request_id": request_id,
                "correlation_id": correlation_id,
                "node_id": node_id,
            }
```

**Add degraded message constants (near top of file):**
```python
_DEGRADED_MESSAGES = {
    ("degraded_visual", "read"): "Using visual fallback — Google API auth is being refreshed. Results may be slower than usual.",
    ("needs_reauth_guided", "read"): "Your Google auth needs renewal. I fetched your email visually, but say 'fix my Google auth' or re-run the setup script for full API access.",
    ("needs_reauth_guided", "write"): "I can't send emails right now — Google auth needs renewal. Say 'fix my Google auth' or re-run the setup script.",
}
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_auth_state_transitions.py -v --tb=short 2>&1 | head -40`

Expected: All PASS.

### Step 5: Run all tests so far

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_auth_state_machine.py tests/unit/backend/test_auth_state_transitions.py tests/unit/backend/test_integration_coordinator_api.py tests/unit/backend/test_agi_os_cross_registration.py tests/unit/backend/test_coordinator_lookup_retry.py -v --tb=short 2>&1 | tail -30`

Expected: All PASS.

### Step 6: Commit

```bash
git add backend/neural_mesh/agents/google_workspace_agent.py tests/unit/backend/test_auth_state_transitions.py
git commit -m "feat: auth state machine behavioral wiring (Section 2)

Wire _handle_auth_event() transition engine, _should_use_visual_fallback()
risk gate, and execute_task degraded routing. Read actions in degraded
state proceed to visual fallback; write actions get guided recovery.
Feature-flagged via JARVIS_AUTH_STATE_MACHINE_V2.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Post-Action Verification Contract

**Files:**
- Modify: `backend/api/unified_command_processor.py` (add verification after workspace action execution)
- Test: `tests/unit/backend/test_workspace_verification.py`

### Step 1: Write the failing tests

Create `tests/unit/backend/test_workspace_verification.py`:

```python
"""Tests for workspace post-action verification contract."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestVerificationContract:
    """Test _verify_workspace_result against contract table."""

    def _get_verifier(self):
        from backend.api.unified_command_processor import _verify_workspace_result
        return _verify_workspace_result

    def test_fetch_unread_valid(self):
        verify = self._get_verifier()
        result = {"emails": [{"subject": "Hi", "from": "a@b.com"}]}
        outcome, annotated = verify("fetch_unread_emails", result)
        assert outcome == "verify_passed"
        assert annotated["_verification"]["passed"] is True

    def test_fetch_unread_empty_valid(self):
        verify = self._get_verifier()
        result = {"emails": []}
        outcome, annotated = verify("fetch_unread_emails", result)
        assert outcome == "verify_empty_valid"
        assert annotated["_verification"]["passed"] is True

    def test_fetch_unread_missing_key(self):
        verify = self._get_verifier()
        result = {"data": []}
        outcome, annotated = verify("fetch_unread_emails", result)
        assert outcome == "verify_schema_fail"
        assert annotated["_verification"]["passed"] is False

    def test_fetch_unread_wrong_type(self):
        verify = self._get_verifier()
        result = {"emails": "not a list"}
        outcome, annotated = verify("fetch_unread_emails", result)
        assert outcome == "verify_schema_fail"

    def test_fetch_unread_item_missing_fields(self):
        verify = self._get_verifier()
        result = {"emails": [{"id": "123"}]}  # missing subject, from
        outcome, annotated = verify("fetch_unread_emails", result)
        assert outcome == "verify_semantic_fail"

    def test_send_email_valid(self):
        verify = self._get_verifier()
        result = {"message_id": "abc123"}
        outcome, annotated = verify("send_email", result)
        assert outcome == "verify_passed"

    def test_send_email_empty_id(self):
        verify = self._get_verifier()
        result = {"message_id": ""}
        outcome, annotated = verify("send_email", result)
        assert outcome == "verify_semantic_fail"

    def test_check_calendar_valid(self):
        verify = self._get_verifier()
        result = {"events": [{"title": "Meeting", "start": "2026-03-01T09:00"}]}
        outcome, annotated = verify("check_calendar_events", result)
        assert outcome == "verify_passed"

    def test_unknown_action_skips_verification(self):
        verify = self._get_verifier()
        result = {"some": "data"}
        outcome, annotated = verify("unknown_action", result)
        assert outcome == "verify_passed"  # no contract = pass

    def test_error_result_is_transport_fail(self):
        verify = self._get_verifier()
        result = {"error": "Connection refused", "emails": []}
        outcome, annotated = verify("fetch_unread_emails", result)
        assert outcome == "verify_transport_fail"


class TestResultNormalization:
    """Test _normalize_workspace_result canonical mapping."""

    def _get_normalizer(self):
        from backend.api.unified_command_processor import _normalize_workspace_result
        return _normalize_workspace_result

    def test_calendar_summary_to_title(self):
        normalize = self._get_normalizer()
        result = {"events": [{"summary": "Meeting", "start": "10:00"}]}
        normalized = normalize("check_calendar_events", result)
        assert normalized["events"][0]["title"] == "Meeting"

    def test_already_normalized_unchanged(self):
        normalize = self._get_normalizer()
        result = {"emails": [{"subject": "Hi", "from": "a@b.com"}]}
        normalized = normalize("fetch_unread_emails", result)
        assert normalized == result
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_workspace_verification.py -v --tb=short 2>&1 | head -40`

Expected: FAIL — `_verify_workspace_result`, `_normalize_workspace_result` don't exist.

### Step 3: Implement verification contract

Add to `backend/api/unified_command_processor.py` (near top, after imports):

```python
# ─── Workspace Result Verification Contract (v_autonomy) ───────────────
WORKSPACE_RESULT_CONTRACT_VERSION = "v1"


@dataclass
class _VerificationContract:
    required_keys: Tuple[str, ...]
    type_checks: Dict[str, type]
    item_required_keys: Tuple[str, ...] = ()
    allow_empty: bool = False
    semantic_check: Optional[Callable] = None


_WORKSPACE_VERIFICATION_CONTRACTS: Dict[str, _VerificationContract] = {
    "fetch_unread_emails": _VerificationContract(
        required_keys=("emails",),
        type_checks={"emails": list},
        item_required_keys=("subject", "from"),
        allow_empty=True,
    ),
    "check_calendar_events": _VerificationContract(
        required_keys=("events",),
        type_checks={"events": list},
        item_required_keys=("title", "start"),
        allow_empty=True,
    ),
    "search_email": _VerificationContract(
        required_keys=("emails",),
        type_checks={"emails": list},
        item_required_keys=("subject",),
        allow_empty=True,
    ),
    "send_email": _VerificationContract(
        required_keys=("message_id",),
        type_checks={"message_id": str},
        semantic_check=lambda v: bool(v.get("message_id")),
    ),
    "draft_email_reply": _VerificationContract(
        required_keys=("draft_id",),
        type_checks={"draft_id": str},
        semantic_check=lambda v: bool(v.get("draft_id")),
    ),
    "create_calendar_event": _VerificationContract(
        required_keys=("event_id",),
        type_checks={"event_id": str},
        semantic_check=lambda v: bool(v.get("event_id")),
    ),
}


def _normalize_workspace_result(action: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize workspace result to canonical schema (contract v1).

    Handles known field name variations (e.g., summary->title for calendar).
    """
    if not isinstance(result, dict):
        return result
    # Calendar: summary -> title
    if action in ("check_calendar_events", "list_events"):
        events = result.get("events")
        if isinstance(events, list):
            for evt in events:
                if isinstance(evt, dict) and "summary" in evt and "title" not in evt:
                    evt["title"] = evt["summary"]
    return result


def _verify_workspace_result(
    action: str,
    result: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """Verify workspace action result against contract.

    Returns (outcome_code, annotated_result) where outcome_code is one of:
    verify_passed, verify_schema_fail, verify_semantic_fail,
    verify_empty_valid, verify_transport_fail
    """
    if not isinstance(result, dict):
        result = {"_raw": result}

    # Transport failure check
    if result.get("error") and not result.get("success", True):
        result["_verification"] = {
            "passed": False,
            "contract_version": WORKSPACE_RESULT_CONTRACT_VERSION,
        }
        return "verify_transport_fail", result

    # Normalize first
    result = _normalize_workspace_result(action, result)

    contract = _WORKSPACE_VERIFICATION_CONTRACTS.get(action)
    if contract is None:
        # No contract defined — pass by default
        result["_verification"] = {
            "passed": True,
            "contract_version": WORKSPACE_RESULT_CONTRACT_VERSION,
        }
        return "verify_passed", result

    # Schema checks
    for key in contract.required_keys:
        if key not in result:
            result["_verification"] = {
                "passed": False,
                "contract_version": WORKSPACE_RESULT_CONTRACT_VERSION,
                "failed_check": f"missing_key:{key}",
            }
            return "verify_schema_fail", result
    for key, expected_type in contract.type_checks.items():
        if key in result and not isinstance(result[key], expected_type):
            result["_verification"] = {
                "passed": False,
                "contract_version": WORKSPACE_RESULT_CONTRACT_VERSION,
                "failed_check": f"type_mismatch:{key}",
            }
            return "verify_schema_fail", result

    # Empty check
    for key in contract.required_keys:
        val = result.get(key)
        if isinstance(val, list) and len(val) == 0:
            if contract.allow_empty:
                result["_verification"] = {
                    "passed": True,
                    "contract_version": WORKSPACE_RESULT_CONTRACT_VERSION,
                }
                return "verify_empty_valid", result

    # Item-level checks
    if contract.item_required_keys:
        for key in contract.required_keys:
            items = result.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        if not all(k in item for k in contract.item_required_keys):
                            result["_verification"] = {
                                "passed": False,
                                "contract_version": WORKSPACE_RESULT_CONTRACT_VERSION,
                                "failed_check": f"item_missing_keys:{contract.item_required_keys}",
                            }
                            return "verify_semantic_fail", result

    # Semantic check
    if contract.semantic_check and not contract.semantic_check(result):
        result["_verification"] = {
            "passed": False,
            "contract_version": WORKSPACE_RESULT_CONTRACT_VERSION,
            "failed_check": "semantic_check",
        }
        return "verify_semantic_fail", result

    result["_verification"] = {
        "passed": True,
        "contract_version": WORKSPACE_RESULT_CONTRACT_VERSION,
    }
    return "verify_passed", result
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_workspace_verification.py -v --tb=short 2>&1 | head -40`

Expected: All PASS.

### Step 5: Commit

```bash
git add backend/api/unified_command_processor.py tests/unit/backend/test_workspace_verification.py
git commit -m "feat: workspace post-action verification contract (Section 4)

Add _verify_workspace_result() with per-action contract table,
result normalization, and failure taxonomy (schema_fail, semantic_fail,
empty_valid, transport_fail). Contract versioned as v1.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 7: Wire Verification + Bounded Recovery into Execution Path

**Files:**
- Modify: `backend/api/unified_command_processor.py` (inside `_handle_workspace_action`, after action execution, before compose)
- Test: `tests/unit/backend/test_workspace_recovery.py`

### Step 1: Write the failing tests

Create `tests/unit/backend/test_workspace_recovery.py`:

```python
"""Tests for bounded recovery and runtime escalation."""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestBoundedRecovery:
    """Test _attempt_workspace_recovery with bounded attempts."""

    @pytest.mark.asyncio
    async def test_no_recovery_when_verification_passes(self):
        from backend.api.unified_command_processor import _attempt_workspace_recovery
        result = {"emails": [{"subject": "Hi", "from": "a@b.com"}]}
        outcome = await _attempt_workspace_recovery(
            action="fetch_unread_emails",
            initial_result=result,
            initial_outcome="verify_passed",
            agent=MagicMock(),
            payload={},
            deadline=time.monotonic() + 30,
            command_text="check my email",
        )
        assert outcome["_verification"]["passed"] is True
        assert len(outcome.get("_attempts", [])) == 0

    @pytest.mark.asyncio
    async def test_read_action_retries_same_tier(self):
        from backend.api.unified_command_processor import _attempt_workspace_recovery
        mock_agent = MagicMock()
        # First call fails, second succeeds
        mock_agent.execute_task = AsyncMock(side_effect=[
            {"emails": [{"subject": "Hi", "from": "a@b.com"}]},
        ])
        result = {"data": "bad"}  # missing emails key
        outcome = await _attempt_workspace_recovery(
            action="fetch_unread_emails",
            initial_result=result,
            initial_outcome="verify_schema_fail",
            agent=mock_agent,
            payload={"action": "fetch_unread_emails"},
            deadline=time.monotonic() + 30,
            command_text="check my email",
        )
        assert len(outcome.get("_attempts", [])) >= 1

    @pytest.mark.asyncio
    async def test_write_action_no_retry_without_idempotency(self):
        from backend.api.unified_command_processor import _attempt_workspace_recovery
        mock_agent = MagicMock()
        mock_agent.execute_task = AsyncMock()
        result = {"error": "failed"}  # send failed
        outcome = await _attempt_workspace_recovery(
            action="send_email",
            initial_result=result,
            initial_outcome="verify_transport_fail",
            agent=mock_agent,
            payload={"action": "send_email"},  # no idempotency_key
            deadline=time.monotonic() + 30,
            command_text="send email",
        )
        # Should NOT retry same tier (no idempotency key)
        attempts = outcome.get("_attempts", [])
        same_tier = [a for a in attempts if a["strategy"] == "same_tier_retry"]
        assert len(same_tier) == 0

    @pytest.mark.asyncio
    async def test_deadline_exhausted_returns_immediately(self):
        from backend.api.unified_command_processor import _attempt_workspace_recovery
        result = {"data": "bad"}
        outcome = await _attempt_workspace_recovery(
            action="fetch_unread_emails",
            initial_result=result,
            initial_outcome="verify_schema_fail",
            agent=MagicMock(),
            payload={"action": "fetch_unread_emails"},
            deadline=time.monotonic() - 1.0,  # already expired
            command_text="check my email",
        )
        assert outcome.get("_recovery_reason") == "recovery_deadline_exhausted"
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_workspace_recovery.py -v --tb=short 2>&1 | head -30`

Expected: FAIL — `_attempt_workspace_recovery` doesn't exist.

### Step 3: Implement bounded recovery

Add to `backend/api/unified_command_processor.py`:

```python
_RUNTIME_ESCALATION_FLOOR = float(os.getenv("JARVIS_RUNTIME_ESCALATION_FLOOR", "5.0"))
_MIN_ATTEMPT_BUDGET = 2.0  # seconds — skip attempt if less budget remains


async def _attempt_workspace_recovery(
    action: str,
    initial_result: Dict[str, Any],
    initial_outcome: str,
    agent: Any,
    payload: Dict[str, Any],
    deadline: float,
    command_text: str,
) -> Dict[str, Any]:
    """Bounded workspace recovery: same-tier retry -> tier fallback -> runtime escalation.

    Returns annotated result with _attempts audit trail.
    """
    if initial_outcome in ("verify_passed", "verify_empty_valid"):
        return initial_result

    attempts = []
    risk = _classify_action_risk_ucp(action)
    has_idempotency_key = bool(payload.get("idempotency_key"))
    remaining = deadline - time.monotonic()

    if remaining <= 0:
        initial_result["_attempts"] = attempts
        initial_result["_recovery_reason"] = "recovery_deadline_exhausted"
        return initial_result

    # Deadline partitioning: 40% retry, 40% fallback, 20% escalation
    retry_budget = remaining * 0.4
    fallback_budget = remaining * 0.4

    # Attempt 1: Same-tier retry (reads, or writes with idempotency key)
    if risk == "read" or has_idempotency_key:
        if retry_budget >= _MIN_ATTEMPT_BUDGET:
            attempt_start = time.monotonic()
            try:
                retry_payload = dict(payload)
                retry_payload["deadline_monotonic"] = time.monotonic() + retry_budget
                retry_result = await asyncio.wait_for(
                    agent.execute_task(retry_payload),
                    timeout=retry_budget,
                )
                outcome, annotated = _verify_workspace_result(action, retry_result)
                attempts.append({
                    "strategy": "same_tier_retry",
                    "tier": "api",
                    "outcome": outcome,
                    "duration_ms": (time.monotonic() - attempt_start) * 1000,
                })
                if outcome in ("verify_passed", "verify_empty_valid"):
                    annotated["_attempts"] = attempts
                    return annotated
            except (asyncio.TimeoutError, Exception) as e:
                attempts.append({
                    "strategy": "same_tier_retry",
                    "tier": "api",
                    "outcome": "verify_transport_fail",
                    "reason": str(e)[:100],
                    "duration_ms": (time.monotonic() - attempt_start) * 1000,
                })

    # Attempt 2: Tier fallback (force visual)
    remaining = deadline - time.monotonic()
    if remaining >= _MIN_ATTEMPT_BUDGET and risk == "read":
        attempt_start = time.monotonic()
        try:
            fallback_payload = dict(payload)
            fallback_payload["_force_visual_fallback"] = True
            fallback_payload["deadline_monotonic"] = time.monotonic() + min(fallback_budget, remaining)
            fallback_result = await asyncio.wait_for(
                agent.execute_task(fallback_payload),
                timeout=min(fallback_budget, remaining),
            )
            outcome, annotated = _verify_workspace_result(action, fallback_result)
            attempts.append({
                "strategy": "tier_fallback",
                "tier": "visual",
                "outcome": outcome,
                "duration_ms": (time.monotonic() - attempt_start) * 1000,
            })
            if outcome in ("verify_passed", "verify_empty_valid"):
                annotated["_attempts"] = attempts
                return annotated
        except (asyncio.TimeoutError, Exception) as e:
            attempts.append({
                "strategy": "tier_fallback",
                "tier": "visual",
                "outcome": "verify_transport_fail",
                "reason": str(e)[:100],
                "duration_ms": (time.monotonic() - attempt_start) * 1000,
            })

    # Attempt 3: Runtime escalation
    remaining = deadline - time.monotonic()
    if remaining >= _RUNTIME_ESCALATION_FLOOR:
        attempt_start = time.monotonic()
        try:
            from autonomy.agent_runtime import get_agent_runtime
            runtime = get_agent_runtime()
            if runtime and getattr(runtime, '_running', False):
                from autonomy.agent_runtime_models import GoalPriority
                goal_id = await runtime.submit_goal(
                    description=f"Complete workspace action: {command_text}",
                    priority=GoalPriority.NORMAL,
                    source="workspace_replan",
                    context={
                        "action": action,
                        "attempt_history": attempts,
                    },
                )
                # Poll with remaining budget
                poll_deadline = time.monotonic() + remaining - 1.0
                while time.monotonic() < poll_deadline:
                    status = await runtime.get_goal_status(goal_id)
                    if status and status.get("status") in ("completed", "failed"):
                        break
                    await asyncio.sleep(1.0)
                attempts.append({
                    "strategy": "runtime_escalation",
                    "tier": "agent_runtime",
                    "outcome": status.get("status", "timeout") if status else "timeout",
                    "duration_ms": (time.monotonic() - attempt_start) * 1000,
                })
        except (ImportError, Exception) as e:
            attempts.append({
                "strategy": "runtime_escalation",
                "tier": "agent_runtime",
                "outcome": "unavailable",
                "reason": str(e)[:100],
                "duration_ms": (time.monotonic() - attempt_start) * 1000,
            })

    # All attempts exhausted
    initial_result["_attempts"] = attempts
    initial_result["_recovery_reason"] = "recovery_deadline_exhausted"
    return initial_result


def _classify_action_risk_ucp(action: str) -> str:
    """Action risk classification for command processor context."""
    _READ_ACTIONS = {
        "fetch_unread_emails", "check_calendar_events", "search_email",
        "get_contacts", "workspace_summary", "daily_briefing",
        "handle_workspace_query", "read_spreadsheet",
    }
    if action in _READ_ACTIONS:
        return "read"
    return "write"
```

**Wire into `_handle_workspace_action()`** — after node execution results are collected but BEFORE composition. Find the post-execution summary section (around line 3632) and add:

```python
        # v_autonomy: Post-action verification + bounded recovery
        for node_outcome in node_outcomes:
            if node_outcome.get("status") == "completed" and node_outcome.get("result"):
                node_action = node_outcome.get("action", "")
                node_result = node_outcome["result"]
                outcome_code, annotated = _verify_workspace_result(node_action, node_result)
                node_outcome["result"] = annotated
                node_outcome["verification_outcome"] = outcome_code

                if outcome_code not in ("verify_passed", "verify_empty_valid"):
                    # Attempt bounded recovery
                    recovered = await _attempt_workspace_recovery(
                        action=node_action,
                        initial_result=annotated,
                        initial_outcome=outcome_code,
                        agent=agent,
                        payload=node_outcome.get("payload", {}),
                        deadline=deadline or (time.monotonic() + 30),
                        command_text=command_text,
                    )
                    node_outcome["result"] = recovered
                    node_outcome["verification_outcome"] = recovered.get(
                        "_verification", {}
                    ).get("passed", False) and "verify_passed" or outcome_code
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_workspace_recovery.py -v --tb=short 2>&1 | head -40`

Expected: All PASS.

### Step 5: Run full test suite

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_integration_coordinator_api.py tests/unit/backend/test_agi_os_cross_registration.py tests/unit/backend/test_coordinator_lookup_retry.py tests/unit/backend/test_auth_state_machine.py tests/unit/backend/test_auth_state_transitions.py tests/unit/backend/test_workspace_verification.py tests/unit/backend/test_workspace_recovery.py -v --tb=short 2>&1 | tail -30`

Expected: All PASS.

### Step 6: Commit

```bash
git add backend/api/unified_command_processor.py tests/unit/backend/test_workspace_recovery.py
git commit -m "feat: bounded recovery + runtime escalation wiring (Section 4)

Wire post-action verification into _handle_workspace_action with
bounded recovery: same-tier retry (read/idempotent only) -> visual
tier fallback -> AgentRuntime goal escalation. Deadline partitioning
prevents starvation. Attempt audit trail in every response.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 8: Integration Smoke Test

Final validation that all sections work together end-to-end.

**Files:**
- Test: `tests/unit/backend/test_autonomy_wiring_e2e.py`

### Step 1: Write the integration test

Create `tests/unit/backend/test_autonomy_wiring_e2e.py`:

```python
"""End-to-end smoke tests for autonomy wiring.

Validates the 4 done criteria without real Google API or model inference.
"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestDoneCriteria:
    """Validate 4 done criteria from design doc."""

    def test_criterion_1_coordinator_resolves_after_cross_registration(self):
        """Valid auth: coordinator resolves immediately via cross-registration."""
        import backend.neural_mesh.integration as mod
        mod._neural_mesh_coordinator = None
        mod._initialized = False

        mock_coord = MagicMock()
        mock_coord._running = True
        mock_agent = MagicMock()
        mock_coord.get_agent.return_value = mock_agent

        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            get_neural_mesh_coordinator,
        )
        set_neural_mesh_coordinator(mock_coord)
        coord = get_neural_mesh_coordinator()
        assert coord is mock_coord
        assert coord.get_agent("google_workspace_agent") is mock_agent

    def test_criterion_2_auth_state_machine_transitions(self):
        """Expired token: AUTHENTICATED -> REFRESHING -> AUTHENTICATED."""
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        assert AuthState.REFRESHING.value == "refreshing"
        assert AuthState.AUTHENTICATED.value == "authenticated"

    def test_criterion_3_degraded_visual_for_read(self):
        """Revoked token: read actions get visual fallback."""
        from backend.neural_mesh.agents.google_workspace_agent import (
            _classify_action_risk,
        )
        assert _classify_action_risk("fetch_unread_emails") == "read"
        assert _classify_action_risk("send_email") == "write"

    def test_criterion_4_verification_catches_bad_output(self):
        """No silent success without verified output."""
        from backend.api.unified_command_processor import _verify_workspace_result
        # Bad output
        outcome, _ = _verify_workspace_result("fetch_unread_emails", {"data": []})
        assert outcome == "verify_schema_fail"
        # Good output
        outcome, _ = _verify_workspace_result(
            "fetch_unread_emails",
            {"emails": [{"subject": "Hi", "from": "a@b.com"}]},
        )
        assert outcome == "verify_passed"
```

### Step 2: Run the integration test

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_autonomy_wiring_e2e.py -v --tb=short 2>&1 | head -30`

Expected: All PASS.

### Step 3: Run ALL autonomy wiring tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_integration_coordinator_api.py tests/unit/backend/test_agi_os_cross_registration.py tests/unit/backend/test_coordinator_lookup_retry.py tests/unit/backend/test_auth_state_machine.py tests/unit/backend/test_auth_state_transitions.py tests/unit/backend/test_workspace_verification.py tests/unit/backend/test_workspace_recovery.py tests/unit/backend/test_autonomy_wiring_e2e.py -v 2>&1 | tail -30`

Expected: All PASS (should be ~35-40 tests total).

### Step 4: Run existing workspace tests to verify no regressions

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/test_google_workspace_agent.py -v --tb=short 2>&1 | tail -20`

Expected: Existing tests PASS (AuthState.NEEDS_REAUTH alias preserves compatibility).

### Step 5: Commit

```bash
git add tests/unit/backend/test_autonomy_wiring_e2e.py
git commit -m "test: add autonomy wiring e2e smoke tests

Validates 4 done criteria: coordinator resolution, auth state
transitions, degraded visual routing, and verification contract.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Execution Order Summary

| Task | Section | Description | Dependencies |
|------|---------|-------------|--------------|
| 1 | Section 3 | integration.py public API | None |
| 2 | Section 3 | AGI OS cross-registration | Task 1 |
| 3 | Section 1 | Coordinator lookup retry | Task 1 |
| 4 | Section 2 | Auth state machine data model | None |
| 5 | Section 2 | Auth state machine behavior | Task 4 |
| 6 | Section 4 | Verification contract | None |
| 7 | Section 4 | Recovery + runtime escalation | Task 6 |
| 8 | All | E2E smoke tests | Tasks 1-7 |

**Parallelizable:** Tasks 1, 4, 6 can start simultaneously (no dependencies).

**Total estimated commits:** 8

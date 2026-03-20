# ECAPA Lifecycle Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate 13 independent ECAPA-TDNN load paths into a single authoritative `EcapaFacade` with an explicit state machine, 3-tier degradation contract, and structured telemetry.

**Architecture:** `EcapaFacade` (`backend/core/ecapa_facade.py`) owns all ECAPA lifecycle state, backend selection, and capability policy. It delegates model loading to the existing `MLEngineRegistry` and cloud extraction to `CloudECAPAClient`. Consumers call `facade.extract_embedding()` / `facade.check_capability()` instead of loading ECAPA directly. Migration is phased with a feature flag `ECAPA_USE_FACADE` for rollback.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, existing `MLEngineRegistry` + `CloudECAPAClient`, pytest

**Spec:** `docs/superpowers/specs/2026-03-19-ecapa-lifecycle-facade-design.md`

---

## File Structure

### New Files
- `backend/core/ecapa_facade.py` — State machine, lifecycle, backend probing, extraction routing, capability checks, telemetry (~400 lines)
- `backend/core/ecapa_types.py` — All shared types: `EcapaState`, `EcapaTier`, `EcapaFacadeConfig`, `EmbeddingResult`, `CapabilityCheck`, `EcapaStateEvent`, exceptions, `VoiceCapability`, `STATE_TO_TIER` (~120 lines)
- `tests/unit/core/test_ecapa_facade.py` — Unit tests for state machine, concurrency, capability matrix (~500 lines)
- `tests/unit/core/test_ecapa_types.py` — Unit tests for types, config, state-to-tier mapping (~80 lines)

### Modified Files (Phase 1 only)
- `unified_supervisor.py` — Add facade creation + `start()` call in shadow mode alongside existing code

---

## Phase 1: Introduce Facade (No Consumer Changes)

### Task 1: Create ECAPA types module

**Files:**
- Create: `backend/core/ecapa_types.py`
- Test: `tests/unit/core/test_ecapa_types.py`

- [ ] **Step 1: Write failing tests for types**

```python
# tests/unit/core/test_ecapa_types.py
"""Tests for ECAPA facade types."""
import pytest
import numpy as np


def test_ecapa_state_values():
    from backend.core.ecapa_types import EcapaState
    assert EcapaState.UNINITIALIZED.value == "uninitialized"
    assert EcapaState.READY.value == "ready"
    assert len(EcapaState) == 7


def test_ecapa_tier_values():
    from backend.core.ecapa_types import EcapaTier
    assert len(EcapaTier) == 3


def test_state_to_tier_mapping():
    from backend.core.ecapa_types import EcapaState, EcapaTier, STATE_TO_TIER
    assert STATE_TO_TIER[EcapaState.UNINITIALIZED] == EcapaTier.UNAVAILABLE
    assert STATE_TO_TIER[EcapaState.PROBING] == EcapaTier.UNAVAILABLE
    assert STATE_TO_TIER[EcapaState.LOADING] == EcapaTier.UNAVAILABLE
    assert STATE_TO_TIER[EcapaState.READY] == EcapaTier.READY
    assert STATE_TO_TIER[EcapaState.DEGRADED] == EcapaTier.DEGRADED
    assert STATE_TO_TIER[EcapaState.UNAVAILABLE] == EcapaTier.UNAVAILABLE
    assert STATE_TO_TIER[EcapaState.RECOVERING] == EcapaTier.UNAVAILABLE
    # Every state must have a tier mapping
    for state in EcapaState:
        assert state in STATE_TO_TIER


def test_config_from_env_defaults(monkeypatch):
    from backend.core.ecapa_types import EcapaFacadeConfig
    # Clear any env overrides
    for key in ["ECAPA_FAILURE_THRESHOLD", "ECAPA_RECOVERY_THRESHOLD",
                "ECAPA_TRANSITION_COOLDOWN_S", "ECAPA_REPROBE_INTERVAL_S",
                "ECAPA_REPROBE_MAX_BACKOFF_S", "ECAPA_REPROBE_BUDGET",
                "ECAPA_PROBE_TIMEOUT_S", "ECAPA_LOCAL_LOAD_TIMEOUT_S",
                "ECAPA_MAX_CONCURRENT_EXTRACTIONS", "ECAPA_RECOVERING_FAIL_THRESHOLD"]:
        monkeypatch.delenv(key, raising=False)
    cfg = EcapaFacadeConfig.from_env()
    assert cfg.failure_threshold == 3
    assert cfg.recovery_threshold == 3
    assert cfg.probe_timeout_s == 8.0
    assert cfg.max_concurrent_extractions == 4
    assert cfg.recovering_fail_threshold == 2


def test_config_from_env_overrides(monkeypatch):
    from backend.core.ecapa_types import EcapaFacadeConfig
    monkeypatch.setenv("ECAPA_FAILURE_THRESHOLD", "5")
    monkeypatch.setenv("ECAPA_PROBE_TIMEOUT_S", "12.5")
    cfg = EcapaFacadeConfig.from_env()
    assert cfg.failure_threshold == 5
    assert cfg.probe_timeout_s == 12.5


def test_embedding_result_success():
    from backend.core.ecapa_types import EmbeddingResult
    r = EmbeddingResult(
        embedding=np.zeros(192), backend="local",
        latency_ms=50.0, from_cache=False, dimension=192, error=None,
    )
    assert r.success is True


def test_embedding_result_failure():
    from backend.core.ecapa_types import EmbeddingResult
    r = EmbeddingResult(
        embedding=None, backend="local",
        latency_ms=0.0, from_cache=False, dimension=192, error="timeout",
    )
    assert r.success is False


def test_voice_capability_enum():
    from backend.core.ecapa_types import VoiceCapability
    assert VoiceCapability.VOICE_UNLOCK.value == "CAP_VOICE_UNLOCK"
    assert len(VoiceCapability) == 8


def test_ecapa_errors():
    from backend.core.ecapa_types import (
        EcapaError, EcapaUnavailableError, EcapaOverloadError, EcapaTimeoutError,
    )
    assert issubclass(EcapaUnavailableError, EcapaError)
    assert issubclass(EcapaOverloadError, EcapaError)
    assert issubclass(EcapaTimeoutError, EcapaError)
    err = EcapaOverloadError(retry_after_s=2.5)
    assert err.retry_after_s == 2.5
    assert "2.5" in str(err)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_ecapa_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.ecapa_types'`

- [ ] **Step 3: Implement ecapa_types.py**

Create `backend/core/ecapa_types.py` containing all type definitions from the spec's "Core Type Definitions" and "Error Model" sections:
- `EcapaState` enum (7 states)
- `EcapaTier` enum (3 tiers)
- `STATE_TO_TIER` mapping dict
- `VoiceCapability` enum (8 capabilities)
- `EcapaFacadeConfig` dataclass with `from_env()` classmethod
- `EmbeddingResult` frozen dataclass with `success` property
- `CapabilityCheck` frozen dataclass
- `EcapaStateEvent` frozen dataclass
- `EcapaError`, `EcapaUnavailableError`, `EcapaOverloadError`, `EcapaTimeoutError`

Use the exact code from the spec. For `numpy` import, use a lazy/optional pattern:
```python
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING
if TYPE_CHECKING:
    import numpy as np
```
And at runtime in `EmbeddingResult`, store embedding as `Optional[Any]` to avoid hard numpy dependency in the types module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_ecapa_types.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ecapa_types.py tests/unit/core/test_ecapa_types.py
git commit -m "feat(ecapa): add ECAPA facade types, config, and error hierarchy"
```

---

### Task 2: Create EcapaFacade state machine core

**Files:**
- Create: `backend/core/ecapa_facade.py`
- Test: `tests/unit/core/test_ecapa_facade.py`

- [ ] **Step 1: Write failing tests for state machine transitions**

```python
# tests/unit/core/test_ecapa_facade.py
"""Tests for EcapaFacade state machine."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ecapa_types import (
    EcapaState, EcapaTier, EcapaFacadeConfig, EcapaStateEvent,
    EcapaUnavailableError, EcapaOverloadError,
)


def _make_config(**overrides):
    """Create a fast config for testing."""
    defaults = dict(
        failure_threshold=2, recovery_threshold=2,
        transition_cooldown_s=0.0, reprobe_interval_s=0.1,
        reprobe_max_backoff_s=0.5, reprobe_budget=3,
        probe_timeout_s=1.0, local_load_timeout_s=2.0,
        max_concurrent_extractions=4, recovering_fail_threshold=2,
    )
    defaults.update(overrides)
    return EcapaFacadeConfig(**defaults)


def _mock_registry():
    """Create a mock MLEngineRegistry."""
    registry = MagicMock()
    wrapper = MagicMock()
    wrapper.is_loaded = False
    wrapper.load = AsyncMock(return_value=True)
    # Simulate extract returning a 192-dim embedding
    import numpy as np
    wrapper.extract = AsyncMock(return_value=np.zeros(192))
    registry.get_wrapper.return_value = wrapper
    return registry, wrapper


def _mock_cloud_client(healthy=True):
    """Create a mock CloudECAPAClient."""
    client = MagicMock()
    import numpy as np
    if healthy:
        client.health_check = AsyncMock(return_value=True)
        client.extract_embedding = AsyncMock(return_value=np.zeros(192))
    else:
        client.health_check = AsyncMock(return_value=False)
        client.extract_embedding = AsyncMock(side_effect=ConnectionError("unreachable"))
    return client


@pytest.mark.asyncio
async def test_initial_state():
    from backend.core.ecapa_facade import EcapaFacade
    registry, _ = _mock_registry()
    facade = EcapaFacade(registry=registry, config=_make_config())
    assert facade.state == EcapaState.UNINITIALIZED
    assert facade.tier == EcapaTier.UNAVAILABLE
    assert facade.active_backend is None


@pytest.mark.asyncio
async def test_start_probes_and_reaches_ready_via_local():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True  # Local model available
    facade = EcapaFacade(registry=registry, config=_make_config())
    await facade.start()
    # Give background tasks time to probe
    ready = await facade.ensure_ready(timeout=5.0)
    assert ready is True
    assert facade.state == EcapaState.READY
    assert facade.tier == EcapaTier.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_start_reaches_ready_via_cloud():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = False
    wrapper.load = AsyncMock(side_effect=asyncio.sleep(10))  # Local slow
    cloud = _mock_cloud_client(healthy=True)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())
    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)
    assert ready is True
    assert facade.active_backend in ("cloud_run", "docker")
    await facade.stop()


@pytest.mark.asyncio
async def test_no_backends_reaches_unavailable():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = False
    wrapper.load = AsyncMock(side_effect=RuntimeError("no speechbrain"))
    cloud = _mock_cloud_client(healthy=False)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())
    await facade.start()
    ready = await facade.ensure_ready(timeout=2.0)
    assert ready is False
    assert facade.state == EcapaState.UNAVAILABLE
    await facade.stop()


@pytest.mark.asyncio
async def test_ready_to_degraded_after_M_failures():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config(failure_threshold=2))
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY
    # Now make extractions fail
    wrapper.extract = AsyncMock(side_effect=RuntimeError("backend died"))
    for _ in range(2):
        try:
            await facade.extract_embedding(b"audio")
        except Exception:
            pass
    assert facade.state == EcapaState.DEGRADED
    assert facade.tier == EcapaTier.DEGRADED
    await facade.stop()


@pytest.mark.asyncio
async def test_illegal_transition_ready_to_unavailable():
    """READY cannot jump directly to UNAVAILABLE."""
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    # Internal: verify the state machine rejects this
    transitioned = await facade._try_transition(EcapaState.UNAVAILABLE, reason="test")
    assert transitioned is False
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_stop_from_any_state_returns_to_uninitialized():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    await facade.stop()
    assert facade.state == EcapaState.UNINITIALIZED


@pytest.mark.asyncio
async def test_start_is_idempotent():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    await facade.start()
    await facade.start()  # Second call should be no-op
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_ensure_ready_returns_true_for_degraded():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config(failure_threshold=1))
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    wrapper.extract = AsyncMock(side_effect=RuntimeError("fail"))
    try:
        await facade.extract_embedding(b"audio")
    except Exception:
        pass
    assert facade.tier == EcapaTier.DEGRADED
    # ensure_ready returns True for DEGRADED
    assert await facade.ensure_ready(timeout=1.0) is True
    await facade.stop()


@pytest.mark.asyncio
async def test_extract_raises_unavailable_when_not_ready():
    from backend.core.ecapa_facade import EcapaFacade
    registry, _ = _mock_registry()
    facade = EcapaFacade(registry=registry, config=_make_config())
    # Not started — UNINITIALIZED
    with pytest.raises(EcapaUnavailableError):
        await facade.extract_embedding(b"audio")


@pytest.mark.asyncio
async def test_telemetry_events_emitted():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    events = []
    facade.subscribe(lambda e: events.append(e))
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    await facade.stop()
    # Should have at least UNINITIALIZED->PROBING and ->READY transitions
    assert len(events) >= 2
    assert all(isinstance(e, EcapaStateEvent) for e in events)
    codes = [e.warning_code for e in events]
    assert "ECAPA_W001" in codes  # Probing started


@pytest.mark.asyncio
async def test_concurrent_ensure_ready_dedupes():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    await facade.start()
    # Launch 10 concurrent ensure_ready calls
    results = await asyncio.gather(*[facade.ensure_ready(timeout=5.0) for _ in range(10)])
    assert all(r is True for r in results)
    await facade.stop()


@pytest.mark.asyncio
async def test_backpressure_semaphore():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    import numpy as np
    # Make extraction take 1 second
    async def slow_extract(*a, **kw):
        await asyncio.sleep(1.0)
        return np.zeros(192)
    wrapper.extract = AsyncMock(side_effect=slow_extract)
    facade = EcapaFacade(
        registry=registry,
        config=_make_config(max_concurrent_extractions=2),
    )
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    # Launch 2 extractions (fills semaphore), then try a 3rd
    tasks = [asyncio.create_task(facade.extract_embedding(b"a")) for _ in range(2)]
    await asyncio.sleep(0.05)  # Let them acquire semaphore
    with pytest.raises(EcapaOverloadError):
        await facade.extract_embedding(b"overflow")
    for t in tasks:
        t.cancel()
    await facade.stop()


@pytest.mark.asyncio
async def test_capability_check_per_tier():
    from backend.core.ecapa_facade import EcapaFacade
    from backend.core.ecapa_types import VoiceCapability
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())

    # UNAVAILABLE tier (not started)
    check = facade.check_capability(VoiceCapability.VOICE_UNLOCK)
    assert check.allowed is False
    assert check.tier == EcapaTier.UNAVAILABLE

    check = facade.check_capability(VoiceCapability.BASIC_COMMAND)
    assert check.allowed is True  # Always allowed

    await facade.start()
    await facade.ensure_ready(timeout=5.0)

    # READY tier
    check = facade.check_capability(VoiceCapability.VOICE_UNLOCK)
    assert check.allowed is True

    check = facade.check_capability(VoiceCapability.ENROLLMENT)
    assert check.allowed is True

    await facade.stop()


@pytest.mark.asyncio
async def test_startup_cancellation():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = False
    wrapper.load = AsyncMock(side_effect=asyncio.sleep(60))  # Very slow
    facade = EcapaFacade(registry=registry, config=_make_config())
    await facade.start()
    await asyncio.sleep(0.1)
    await facade.stop()  # Should cancel cleanly
    assert facade.state == EcapaState.UNINITIALIZED


@pytest.mark.asyncio
async def test_restart_consistency():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY
    await facade.stop()
    assert facade.state == EcapaState.UNINITIALIZED
    # Restart
    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)
    assert ready is True
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_get_status_returns_dict():
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    status = facade.get_status()
    assert isinstance(status, dict)
    assert "state" in status
    assert "tier" in status
    assert "active_backend" in status
    assert status["state"] == EcapaState.UNINITIALIZED.value
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    status = facade.get_status()
    assert status["state"] == EcapaState.READY.value
    assert status["tier"] == EcapaTier.READY.value
    await facade.stop()


@pytest.mark.asyncio
async def test_start_is_nonblocking():
    """SLO: start() must return immediately without blocking supervisor."""
    from backend.core.ecapa_facade import EcapaFacade
    import time
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = False
    wrapper.load = AsyncMock(side_effect=asyncio.sleep(60))
    facade = EcapaFacade(registry=registry, config=_make_config())
    t0 = time.monotonic()
    await facade.start()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"start() blocked for {elapsed:.1f}s (must be non-blocking)"
    await facade.stop()


@pytest.mark.asyncio
async def test_flapping_hysteresis():
    """Alternating success/fail must not cause rapid state flapping."""
    from backend.core.ecapa_facade import EcapaFacade
    import numpy as np
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config(failure_threshold=3))
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    # Alternate: 2 failures, 1 success, 2 failures, 1 success — never hits 3 consecutive
    for _ in range(3):
        wrapper.extract = AsyncMock(side_effect=RuntimeError("fail"))
        try:
            await facade.extract_embedding(b"a")
        except Exception:
            pass
        try:
            await facade.extract_embedding(b"a")
        except Exception:
            pass
        wrapper.extract = AsyncMock(return_value=np.zeros(192))
        await facade.extract_embedding(b"a")
    # Should still be READY (success resets counter before hitting 3)
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_cloud_unavailable_local_fallback():
    """Cloud probe fails -> local model loads -> READY via local."""
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    cloud = _mock_cloud_client(healthy=False)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())
    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)
    assert ready is True
    assert facade.active_backend == "local"
    await facade.stop()


@pytest.mark.asyncio
async def test_local_unavailable_cloud_fallback():
    """Local model import fails -> cloud serves -> READY via cloud."""
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = False
    wrapper.load = AsyncMock(side_effect=ImportError("no speechbrain"))
    cloud = _mock_cloud_client(healthy=True)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())
    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)
    assert ready is True
    assert facade.active_backend in ("cloud_run", "docker")
    await facade.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_ecapa_facade.py -v 2>&1 | head -30`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.ecapa_facade'`

- [ ] **Step 3: Implement EcapaFacade core**

Create `backend/core/ecapa_facade.py`. Key implementation details:

**State machine (`_try_transition`):** Build a `_LEGAL_TRANSITIONS` dict mapping `(from_state, to_state) -> bool`. Check against it before any transition. Hold `_state_lock` during transition. Emit `EcapaStateEvent` after releasing lock.

**`start()`:** Set state to PROBING. Launch `_probe_and_load()` as a background task. If already started (state != UNINITIALIZED), return immediately (idempotent).

**`_probe_and_load()`:** Probe all backends concurrently using `asyncio.gather()` with individual timeouts. Cloud probe: call `cloud_client.health_check()`. Local probe: check `registry.get_wrapper("ecapa_tdnn").is_loaded` or attempt `wrapper.load()` via `asyncio.to_thread()`. First healthy backend transitions to READY. If none found, transition to UNAVAILABLE and schedule `_background_reprobe()`.

**`stop()`:** Cancel all background tasks. Transition to UNINITIALIZED. Release model reference.

**`extract_embedding()`:** Check tier — raise `EcapaUnavailableError` if UNAVAILABLE. Acquire backpressure semaphore (non-blocking `try_acquire`, raise `EcapaOverloadError` if full). Route to active backend. On success, update success counters. On failure, update failure counters and check thresholds for state transition.

**`ensure_ready()`:** If tier is READY or DEGRADED, return True immediately. Otherwise, await `_ready_event` with timeout. Return True if event set within timeout, False otherwise.

**`check_capability()`:** Pure function — lookup tier, return `CapabilityCheck` from a static mapping table. No I/O.

**`get_status()`:** Return dict with `state`, `tier`, `active_backend`, `uptime_s`,
`consecutive_failures`, `consecutive_successes`, `reprobe_budget_remaining`, `metrics`.
Pure read of internal state — no I/O.

**`subscribe()`:** Append callback to `_subscribers` list. Return a lambda that removes it.

**Singleton factory `get_ecapa_facade()`:** Module-level `_facade_instance` + asyncio.Lock. PID file at `~/.jarvis/ecapa_facade.pid`.

**`_reset_facade()`:** Test-only function that sets `_facade_instance = None`. Used by test teardown to reset singleton state between tests.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_ecapa_facade.py -v`
Expected: All 22 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ecapa_facade.py tests/unit/core/test_ecapa_facade.py
git commit -m "feat(ecapa): implement EcapaFacade state machine with full test suite"
```

---

### Task 3: Add Cloud SQL decoupling and warning noise tests

**Files:**
- Modify: `tests/unit/core/test_ecapa_facade.py`

- [ ] **Step 1: Write the additional tests**

Append to `tests/unit/core/test_ecapa_facade.py`:

```python
@pytest.mark.asyncio
async def test_cloud_sql_down_ecapa_ready():
    """ECAPA readiness must NOT depend on Cloud SQL."""
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    # Cloud SQL is irrelevant — facade never checks it
    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)
    assert ready is True
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_warning_noise_bounded():
    """Each state transition should emit <= 3 warnings per root_cause_id."""
    from backend.core.ecapa_facade import EcapaFacade
    registry, wrapper = _mock_registry()
    wrapper.is_loaded = True
    facade = EcapaFacade(registry=registry, config=_make_config())
    events = []
    facade.subscribe(lambda e: events.append(e))
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    await facade.stop()
    # Group events by root_cause_id
    from collections import Counter
    root_counts = Counter(e.root_cause_id for e in events)
    for root_id, count in root_counts.items():
        assert count <= 3, f"root_cause_id {root_id} emitted {count} events (max 3)"


@pytest.mark.asyncio
async def test_singleton_fencing():
    """get_ecapa_facade() returns same instance."""
    from backend.core.ecapa_facade import get_ecapa_facade, _reset_facade
    _reset_facade()  # Clean state for test
    registry, _ = _mock_registry()
    f1 = await get_ecapa_facade(registry=registry, config=_make_config())
    f2 = await get_ecapa_facade()
    assert f1 is f2
    _reset_facade()
```

- [ ] **Step 2: Run all facade tests**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_ecapa_facade.py -v`
Expected: All 25 tests PASS (22 from Task 2 + 3 new)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_ecapa_facade.py
git commit -m "test(ecapa): add Cloud SQL decoupling, warning noise, and singleton tests"
```

---

### Task 4: Wire facade into supervisor in shadow mode

**Files:**
- Modify: `unified_supervisor.py` (add ~15 lines in init + startup)

- [ ] **Step 1: Read the supervisor init section**

Read `unified_supervisor.py` around lines 67144-67196 to find the existing ECAPA policy initialization block. The new facade creation goes AFTER the existing `_ecapa_policy` dict (which stays in place for shadow mode).

- [ ] **Step 2: Add facade creation in supervisor `__init__`**

After the existing `_ecapa_policy` block (~line 67196), add:

```python
        # v300.0: EcapaFacade (shadow mode alongside legacy ECAPA plumbing)
        self._ecapa_facade: Optional["EcapaFacade"] = None
        self._ecapa_facade_enabled = os.getenv(
            "ECAPA_USE_FACADE", "true"
        ).lower() in ("true", "1", "yes")
```

- [ ] **Step 3: Add facade start in the startup sequence**

Find the ECAPA background verification task launch (~line 74053-74058). BEFORE the existing `_run_ecapa_verification_bg` task creation, add:

```python
                # v300.0: Start EcapaFacade (non-blocking, shadow mode)
                if self._ecapa_facade_enabled and self._ecapa_facade is None:
                    try:
                        from backend.core.ecapa_facade import EcapaFacade
                        from backend.core.ecapa_types import EcapaFacadeConfig
                        _registry = None
                        try:
                            from backend.voice_unlock.ml_engine_registry import get_ml_registry_sync
                            _registry = get_ml_registry_sync(auto_create=True)
                        except Exception:
                            pass
                        if _registry is not None:
                            _cloud = None
                            try:
                                from backend.voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client
                                _cloud = await get_cloud_ecapa_client()
                            except Exception:
                                pass  # Cloud client optional — facade works without it
                            self._ecapa_facade = EcapaFacade(
                                registry=_registry,
                                cloud_client=_cloud,
                                config=EcapaFacadeConfig.from_env(),
                            )
                            await self._ecapa_facade.start()
                            self.logger.info("[Kernel] EcapaFacade started (shadow mode)")
                    except Exception as _facade_err:
                        self.logger.debug(f"[Kernel] EcapaFacade init skipped: {_facade_err}")
```

- [ ] **Step 4: Add facade stop in supervisor shutdown**

Search for `_ecapa_reprobe_task` cancellation in `unified_supervisor.py` (around line 94154-94166 inside `_apply_ecapa_policy`). Also search for any `async def _shutdown` or `async def _cleanup` or `async def _stop` method in the supervisor kernel class. Add the facade stop near where existing ECAPA background tasks are cancelled. If no centralized shutdown exists, add it at the location where `_ecapa_reprobe_task.cancel()` is called:

```python
        # v300.0: Stop EcapaFacade
        if self._ecapa_facade is not None:
            try:
                await self._ecapa_facade.stop()
            except Exception:
                pass
            self._ecapa_facade = None
```

- [ ] **Step 5: Verify rollback works (ECAPA_USE_FACADE=false)**

Set `ECAPA_USE_FACADE=false` in a test and verify the facade is not created. The supervisor should skip facade creation and use the old ECAPA plumbing.

- [ ] **Step 6: Verify the existing tests still pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_ecapa_facade.py tests/unit/core/test_ecapa_types.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(ecapa): wire EcapaFacade into supervisor in shadow mode (Phase 1)"
```

---

## Phase 2-6: Consumer Migration (Summary Tasks)

> Phases 2-6 are described at task level. Each phase should be implemented as a
> separate PR/commit series after Phase 1 is validated in production.

### Task 5: Phase 2 — Migrate primary consumers (3 files)

**Files:**
- Modify: `backend/voice/speaker_verification_service.py`
- Modify: `backend/voice_unlock/intelligent_voice_unlock_service.py`
- Modify: `backend/voice_unlock/voice_biometric_intelligence.py`

For each file:
- [ ] Add `ECAPA_USE_FACADE` env var check at the top
- [ ] Replace `extract_speaker_embedding` import with `get_ecapa_facade().extract_embedding()`
- [ ] Replace `ensure_ecapa_available` calls with `facade.ensure_ready()`
- [ ] Add `facade.check_capability()` gating before security-sensitive operations
- [ ] Keep old code path behind `if not ECAPA_USE_FACADE` for rollback
- [ ] Run existing voice unlock tests to verify no regression
- [ ] Commit: `feat(ecapa): migrate primary consumers to EcapaFacade (Phase 2)`

### Task 6: Phase 3 — Migrate secondary consumers (16 files)

**Files:** All 16 files listed in the spec's Phase 3 section.

For each file:
- [ ] Replace direct ECAPA imports with facade calls
- [ ] Add capability checks where appropriate
- [ ] Flip `ECAPA_USE_FACADE` default to `true`
- [ ] Run full test suite
- [ ] Commit: `feat(ecapa): migrate secondary consumers to EcapaFacade (Phase 3)`

### Task 7: Phase 4 — Delete old load paths (10 files)

**Files:** All files listed in the spec's Phase 4 section, plus `process_isolated_ml_loader.py`.

For each file:
- [ ] Delete direct `safe_from_hparams` ECAPA calls
- [ ] Verify only facade loads ECAPA models (grep check)
- [ ] Run: `grep -rn "safe_from_hparams.*ecapa" backend/ --include="*.py" | grep -v "prebake\|compile\|test"` — expect 0 results
- [ ] Run full test suite
- [ ] Commit: `refactor(ecapa): delete 9 old direct ECAPA load paths (Phase 4)`

### Task 8: Phase 5 — Gut supervisor ECAPA plumbing (~600 lines)

**Files:**
- Modify: `unified_supervisor.py`

- [ ] Remove `_ecapa_policy` dict (~lines 67154-67196)
- [ ] Remove `_apply_ecapa_policy` method
- [ ] Remove `_verify_ecapa_pipeline` method
- [ ] Remove `_classify_ecapa_failure` method
- [ ] Remove `_apply_ecapa_backend_environment` method
- [ ] Remove `_ecapa_reprobe_task` and `_ecapa_cloud_warmup_task` handling
- [ ] Remove Phase 2 ECAPA probe duplication (~lines 77813-77875)
- [ ] Remove Phase 4 ECAPA background verification (~lines 73890-74058)
- [ ] Replace with single facade `start()` call (already added in Task 4, remove shadow mode guard)
- [ ] Wire `vbi_health_monitor.py` to subscribe to facade events
- [ ] Run all tests
- [ ] Run: `grep -n "_ecapa_policy\|_apply_ecapa_policy\|_verify_ecapa_pipeline\|_ecapa_reprobe_task" unified_supervisor.py` — expect 0 results
- [ ] Commit: `refactor(ecapa): remove ~600 lines of ECAPA plumbing from supervisor (Phase 5)`

### Task 9: Phase 6 — Remove feature flag and dead code

**Files:**
- Modify: `backend/core/ecapa_facade.py` (remove shadow mode)
- Modify: All Phase 2-3 migrated files (remove `ECAPA_USE_FACADE` checks)
- Modify: `unified_supervisor.py` (remove `_ecapa_facade_enabled` flag)

- [ ] Remove all `ECAPA_USE_FACADE` env var checks
- [ ] Remove old code path fallbacks from Phase 2-3 files
- [ ] Remove shadow mode guard from supervisor
- [ ] Strip `MLEngineRegistry` of ECAPA-specific policy/routing logic (keep model loading)
- [ ] Run full test suite
- [ ] Commit: `refactor(ecapa): remove ECAPA_USE_FACADE flag and dead code (Phase 6)`

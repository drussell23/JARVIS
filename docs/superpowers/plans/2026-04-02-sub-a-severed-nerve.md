# Sub-project A: The Severed Nerve — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `CUExecutionSensor` to the `UnifiedIntakeRouter` so CU failure envelopes flow through the full Ouroboros governance pipeline instead of being dropped.

**Architecture:** Two surgical production changes (sensor wiring + priority map entry) plus two integration tests proving the spine works end-to-end. The singleton re-wiring pattern is already built into `CUExecutionSensor.__init__` — we just need to call it from `IntakeLayerService._build_components()`.

**Tech Stack:** Python 3.12, asyncio, pytest (async), unittest.mock

**Spec:** `docs/superpowers/specs/2026-04-02-sub-a-severed-nerve-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/core/ouroboros/governance/intake/intake_layer_service.py` | Modify (~line 613) | Wire CUExecutionSensor singleton to router |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py` | Modify (line 42) | Add `"cu_execution": 5` to `_PRIORITY_MAP` |
| `tests/governance/intake/test_cu_execution_spine.py` | Create | Spine test: sensor → router.ingest |
| `tests/governance/intake/test_intake_layer_cu_wiring.py` | Create | Verify IntakeLayerService wires CUExecutionSensor |

---

### Task 1: Add `cu_execution` to `_PRIORITY_MAP`

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/unified_intake_router.py:33-43`
- Test: `tests/governance/intake/test_cu_execution_spine.py`

- [ ] **Step 1: Write the failing test**

Create `tests/governance/intake/test_cu_execution_spine.py`:

```python
"""CUExecutionSensor spine tests — envelope flows sensor → router."""
import pytest

from backend.core.ouroboros.governance.intake.unified_intake_router import _PRIORITY_MAP


def test_cu_execution_has_explicit_priority():
    """cu_execution must have an explicit priority, not fallback 99."""
    assert "cu_execution" in _PRIORITY_MAP
    assert _PRIORITY_MAP["cu_execution"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/intake/test_cu_execution_spine.py::test_cu_execution_has_explicit_priority -v`
Expected: FAIL with `KeyError` or `AssertionError` (cu_execution not in map)

- [ ] **Step 3: Add `cu_execution` to the priority map**

In `backend/core/ouroboros/governance/intake/unified_intake_router.py`, change `_PRIORITY_MAP` (lines 33-43):

```python
_PRIORITY_MAP: Dict[str, int] = {
    "voice_human": 0,
    "test_failure": 1,
    "backlog": 2,
    "ai_miner": 3,
    "architecture": 3,
    "exploration": 4,
    "roadmap": 4,
    "capability_gap": 5,
    "cu_execution": 5,
    "runtime_health": 6,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/intake/test_cu_execution_spine.py::test_cu_execution_has_explicit_priority -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/unified_intake_router.py tests/governance/intake/test_cu_execution_spine.py
git commit -m "feat(intake): add cu_execution to priority map at level 5"
```

---

### Task 2: Spine test — sensor emits envelope to router on graduation

**Files:**
- Modify: `tests/governance/intake/test_cu_execution_spine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/governance/intake/test_cu_execution_spine.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
    CUExecutionRecord,
    CUExecutionSensor,
)


def _make_failure_record(goal: str = "send message to Alice", error: str = "target not found") -> CUExecutionRecord:
    """Build a CU failure record with a deterministic signature."""
    return CUExecutionRecord(
        goal=goal,
        success=False,
        steps_completed=2,
        steps_total=5,
        elapsed_s=3.0,
        error=error,
        is_messaging=True,
        contact="Alice",
        app="messages",
    )


@pytest.fixture()
def fresh_cu_sensor():
    """Yield a fresh CUExecutionSensor with cleared singleton state."""
    # Reset singleton so each test gets a clean sensor
    CUExecutionSensor._instance = None
    sensor = CUExecutionSensor.__new__(CUExecutionSensor)
    sensor._initialized = False
    yield sensor
    # Cleanup
    CUExecutionSensor._instance = None


@pytest.mark.asyncio
async def test_graduation_emits_envelope_to_router(fresh_cu_sensor):
    """After 3 failures with the same signature, sensor calls router.ingest()."""
    mock_router = MagicMock()
    mock_router.ingest = AsyncMock(return_value="enqueued")

    sensor = CUExecutionSensor(router=mock_router, repo="jarvis")

    # Feed 3 failures (graduation threshold is 3)
    for _ in range(3):
        await sensor.record(_make_failure_record())

    # Verify envelope was emitted
    assert sensor._total_envelopes_emitted >= 1
    mock_router.ingest.assert_called_once()

    # Verify envelope contents
    envelope = mock_router.ingest.call_args[0][0]
    assert envelope.source == "cu_execution"
    assert envelope.repo == "jarvis"
    assert "cu_task_planner.py" in envelope.target_files or len(envelope.target_files) > 0


@pytest.mark.asyncio
async def test_no_router_logs_warning_and_drops(fresh_cu_sensor, caplog):
    """Without a router, graduation logs a warning and does not raise."""
    sensor = CUExecutionSensor(router=None, repo="jarvis")

    for _ in range(3):
        await sensor.record(_make_failure_record())

    assert sensor._total_envelopes_emitted == 0
    assert "No router wired" in caplog.text


@pytest.mark.asyncio
async def test_success_records_do_not_trigger_graduation(fresh_cu_sensor):
    """Successful CU executions should not accumulate toward graduation."""
    mock_router = MagicMock()
    mock_router.ingest = AsyncMock(return_value="enqueued")

    sensor = CUExecutionSensor(router=mock_router, repo="jarvis")

    for _ in range(5):
        await sensor.record(CUExecutionRecord(
            goal="send message to Alice",
            success=True,
            steps_completed=5,
            steps_total=5,
            elapsed_s=2.0,
        ))

    assert sensor._total_envelopes_emitted == 0
    mock_router.ingest.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intake/test_cu_execution_spine.py -v`
Expected: All 4 tests PASS (the sensor itself works — it just needs a router)

Note: `test_no_router_logs_warning_and_drops` proves the current broken state. `test_graduation_emits_envelope_to_router` proves the sensor works when given a router. Both should pass already because the sensor code is correct — it's the wiring in IntakeLayerService that's missing.

- [ ] **Step 3: Commit**

```bash
git add tests/governance/intake/test_cu_execution_spine.py
git commit -m "test(intake): add CUExecutionSensor spine tests for graduation and routing"
```

---

### Task 3: Wire CUExecutionSensor in IntakeLayerService

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py` (~line 613)
- Test: `tests/governance/intake/test_intake_layer_cu_wiring.py`

- [ ] **Step 1: Write the failing test**

Create `tests/governance/intake/test_intake_layer_cu_wiring.py`:

```python
"""Verify IntakeLayerService wires CUExecutionSensor to the router."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
)
from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
    CUExecutionSensor,
)


@pytest.fixture()
def fresh_cu_singleton():
    """Reset CUExecutionSensor singleton before/after test."""
    CUExecutionSensor._instance = None
    yield
    CUExecutionSensor._instance = None


@pytest.mark.asyncio
async def test_intake_layer_wires_cu_sensor(tmp_path, fresh_cu_singleton):
    """After start(), the CUExecutionSensor singleton must have a router."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)

    await svc.start()

    try:
        # The singleton should now have a router wired
        sensor = CUExecutionSensor()
        assert sensor._router is not None, (
            "CUExecutionSensor._router is None after IntakeLayerService.start() — "
            "wiring is missing in _build_components()"
        )

        # Verify it's in the sensors list (has start/stop lifecycle)
        cu_sensors = [s for s in svc._sensors if isinstance(s, CUExecutionSensor)]
        assert len(cu_sensors) == 1, (
            f"Expected exactly 1 CUExecutionSensor in _sensors, found {len(cu_sensors)}"
        )
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_cu_sensor_router_matches_intake_router(tmp_path, fresh_cu_singleton):
    """CUExecutionSensor's router must be the same instance as the intake router."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)

    await svc.start()

    try:
        sensor = CUExecutionSensor()
        assert sensor._router is svc._router, (
            "CUExecutionSensor._router is not the same instance as "
            "IntakeLayerService._router — wiring uses wrong router"
        )
    finally:
        await svc.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intake/test_intake_layer_cu_wiring.py -v`
Expected: FAIL — `sensor._router is None` because `_build_components()` doesn't wire it yet.

- [ ] **Step 3: Wire CUExecutionSensor in `_build_components()`**

In `backend/core/ouroboros/governance/intake/intake_layer_service.py`, add the following block after the `TodoScannerSensor` block (around line 613, before the `ReactorEventConsumer` block at line 615):

```python
        # ---- CUExecutionSensor (Pillar 6: Vision Neuroplasticity) ----
        # Event-driven sensor — records fed by ActionDispatcher after CU execution.
        # Singleton re-wiring: CUExecutionSensor.__init__ accepts router= on
        # re-init (if already constructed by get_cu_execution_sensor() elsewhere).
        try:
            from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
                CUExecutionSensor,
            )
            _cu_sensor = CUExecutionSensor(router=self._router, repo="jarvis")
            self._sensors.append(_cu_sensor)
            logger.info("[IntakeLayer] CUExecutionSensor wired (vision neuroplasticity active)")
        except Exception as exc:
            logger.debug("[IntakeLayer] CUExecutionSensor skipped: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intake/test_intake_layer_cu_wiring.py -v`
Expected: PASS — both tests green

- [ ] **Step 5: Run existing intake tests to verify no regressions**

Run: `python3 -m pytest tests/governance/intake/ -v`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/intake/intake_layer_service.py tests/governance/intake/test_intake_layer_cu_wiring.py
git commit -m "feat(intake): wire CUExecutionSensor to router in IntakeLayerService

CUExecutionSensor singleton was never given the intake router, causing
all CU graduation envelopes to be dropped with 'No router wired' warning.
Now wired in _build_components() alongside other neuroplasticity sensors."
```

---

### Task 4: End-to-end integration test — graduation through ingest

**Files:**
- Modify: `tests/governance/intake/test_cu_execution_spine.py`

- [ ] **Step 1: Write the E2E integration test**

Append to `tests/governance/intake/test_cu_execution_spine.py`:

```python
from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
)


@pytest.mark.asyncio
async def test_e2e_cu_graduation_through_intake_layer(tmp_path, fresh_cu_sensor):
    """Full E2E: CU failures → sensor graduation → router.ingest().

    This proves the spinal cord is connected: ActionDispatcher feeds
    CUExecutionSensor, which emits to the router wired by IntakeLayerService.
    """
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)

    await svc.start()

    try:
        # Spy on the router's ingest method
        original_ingest = svc._router.ingest
        ingest_calls = []

        async def spy_ingest(envelope):
            ingest_calls.append(envelope)
            return await original_ingest(envelope)

        svc._router.ingest = spy_ingest

        # Get the singleton sensor (now wired by IntakeLayerService)
        sensor = CUExecutionSensor()
        assert sensor._router is not None, "Pre-condition: sensor must have router"

        # Feed 3 identical failures to cross graduation threshold
        for _ in range(3):
            await sensor.record(_make_failure_record())

        # Verify envelope was emitted and reached the router
        assert sensor._total_envelopes_emitted >= 1, (
            "Sensor did not emit any envelopes after 3 failures"
        )
        assert len(ingest_calls) >= 1, (
            "Router.ingest was never called — envelope dropped between sensor and router"
        )

        # Verify envelope metadata
        envelope = ingest_calls[0]
        assert envelope.source == "cu_execution"
        assert envelope.repo == "jarvis"
        assert envelope.urgency == "normal"
    finally:
        await svc.stop()
```

- [ ] **Step 2: Run the full test suite**

Run: `python3 -m pytest tests/governance/intake/test_cu_execution_spine.py tests/governance/intake/test_intake_layer_cu_wiring.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/governance/intake/test_cu_execution_spine.py
git commit -m "test(intake): add E2E integration test for CU graduation through intake layer"
```

---

### Task 5: Run full regression and verify

**Files:** None (verification only)

- [ ] **Step 1: Run all intake tests**

Run: `python3 -m pytest tests/governance/intake/ -v`
Expected: All tests PASS (existing + new)

- [ ] **Step 2: Run full governance test suite**

Run: `python3 -m pytest tests/governance/ -v --timeout=30`
Expected: No new failures introduced. Pre-existing failures (noted in memory: `test_preflight.py`, `test_e2e.py`, `test_pipeline_deadline.py`, `test_phase2c_acceptance.py`) are expected.

- [ ] **Step 3: Final commit with all changes**

If any fixups were needed during regression, commit them:

```bash
git add -u
git commit -m "fix(intake): address regression findings from CU sensor wiring"
```

If no fixups needed, this step is a no-op.

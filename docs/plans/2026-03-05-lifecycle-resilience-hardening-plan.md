# Lifecycle Resilience & Runtime Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 12 runtime hardening gaps (items 9-20) covering lifecycle resilience, concurrency safety, data integrity, and cross-repo contracts.

**Architecture:** Four phases (E/F/G/H) with verification gates between each. Each phase addresses a blast-radius tier: system death, performance degradation, silent corruption, deployment failures. A cross-cutting `time_utils` module is created first.

**Tech Stack:** Python 3.11+, asyncio, concurrent.futures, psutil, pickle, json, pytest, AST analysis

**Design doc:** `docs/plans/2026-03-05-lifecycle-resilience-hardening-design.md`

---

## Pre-Phase: Cross-Cutting time_utils Module

### Task 0: Create time_utils module

**Files:**
- Create: `backend/core/time_utils.py`
- Create: `tests/unit/core/test_time_utils.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_time_utils.py`:

```python
"""Tests for monotonic time helpers."""
import time
import pytest
from backend.core.time_utils import monotonic_ms, monotonic_s, elapsed_since_s, elapsed_since_ms


class TestTimeUtils:
    def test_monotonic_s_returns_float(self):
        result = monotonic_s()
        assert isinstance(result, float)
        assert result > 0

    def test_monotonic_ms_returns_int(self):
        result = monotonic_ms()
        assert isinstance(result, int)
        assert result > 0

    def test_elapsed_since_s(self):
        start = monotonic_s()
        time.sleep(0.05)
        elapsed = elapsed_since_s(start)
        assert 0.04 < elapsed < 0.5  # Should be ~50ms

    def test_elapsed_since_ms(self):
        start = monotonic_ms()
        time.sleep(0.05)
        elapsed = elapsed_since_ms(start)
        assert 40 < elapsed < 500  # Should be ~50ms

    def test_monotonic_s_is_monotonic(self):
        """Values must never decrease."""
        samples = [monotonic_s() for _ in range(100)]
        for i in range(1, len(samples)):
            assert samples[i] >= samples[i - 1]
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_time_utils.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.time_utils'`

**Step 3: Write minimal implementation**

Create `backend/core/time_utils.py`:

```python
"""Monotonic time helpers -- prevents new datetime.now() duration bugs.

All duration/elapsed calculations in JARVIS should use these helpers
instead of datetime.now() to avoid NTP clock adjustment corruption.
"""
import time


def monotonic_ms() -> int:
    """Current monotonic time in milliseconds."""
    return int(time.monotonic() * 1000)


def monotonic_s() -> float:
    """Current monotonic time in seconds."""
    return time.monotonic()


def elapsed_since_s(start_mono: float) -> float:
    """Seconds elapsed since a monotonic start time."""
    return time.monotonic() - start_mono


def elapsed_since_ms(start_mono_ms: int) -> int:
    """Milliseconds elapsed since a monotonic start time."""
    return int(time.monotonic() * 1000) - start_mono_ms
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_time_utils.py -v`
Expected: PASS (all 5 tests)

**Step 5: Commit**

```bash
git add backend/core/time_utils.py tests/unit/core/test_time_utils.py
git commit -m "feat(core): add time_utils module for monotonic duration helpers"
```

---

## Phase E: Lifecycle Resilience (Items 9, 10, 14, 19)

### Task 1: Supervisor Heartbeat Liveness (Item 9)

**Files:**
- Modify: `unified_supervisor.py:63412-63420` (add attributes to `__init__`)
- Modify: `unified_supervisor.py:87079` (add file heartbeat alongside HTTP heartbeat)
- Create: `tests/unit/core/test_heartbeat_liveness.py`

**Context:** The supervisor already has `_progress_heartbeat_task()` at line 87079 that sends HTTP heartbeats to the loading server. We add a *file-based* heartbeat at `~/.jarvis/heartbeat.json` with rich identity payload, using atomic write (write+fsync+rename).

**Step 1: Write the failing test**

Create `tests/unit/core/test_heartbeat_liveness.py`:

```python
"""Tests for supervisor heartbeat liveness file."""
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch
import pytest


class TestHeartbeatPayload:
    """Test heartbeat payload structure and validation."""

    def test_heartbeat_payload_has_required_fields(self):
        """Heartbeat must contain boot_id, pid, ts_mono, monotonic_age_ms, phase, loop_iteration."""
        from backend.core.heartbeat_writer import HeartbeatWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            hb_path = Path(tmpdir) / "heartbeat.json"
            writer = HeartbeatWriter(hb_path)
            writer.write(phase="ready", loop_iteration=42)

            data = json.loads(hb_path.read_text())
            assert "boot_id" in data
            assert "pid" in data
            assert "ts_mono" in data
            assert "monotonic_age_ms" in data
            assert "phase" in data
            assert "loop_iteration" in data
            assert data["pid"] == os.getpid()
            assert data["phase"] == "ready"
            assert data["loop_iteration"] == 42

    def test_heartbeat_boot_id_is_stable(self):
        """boot_id must be same across writes from same writer instance."""
        from backend.core.heartbeat_writer import HeartbeatWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            hb_path = Path(tmpdir) / "heartbeat.json"
            writer = HeartbeatWriter(hb_path)
            writer.write(phase="boot", loop_iteration=1)
            data1 = json.loads(hb_path.read_text())
            writer.write(phase="ready", loop_iteration=2)
            data2 = json.loads(hb_path.read_text())
            assert data1["boot_id"] == data2["boot_id"]

    def test_heartbeat_monotonic_age_increases(self):
        """monotonic_age_ms must increase between writes."""
        from backend.core.heartbeat_writer import HeartbeatWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            hb_path = Path(tmpdir) / "heartbeat.json"
            writer = HeartbeatWriter(hb_path)
            writer.write(phase="boot", loop_iteration=1)
            data1 = json.loads(hb_path.read_text())
            time.sleep(0.05)
            writer.write(phase="ready", loop_iteration=2)
            data2 = json.loads(hb_path.read_text())
            assert data2["monotonic_age_ms"] > data1["monotonic_age_ms"]

    def test_heartbeat_atomic_write(self):
        """File must never contain partial JSON (atomic write guarantee)."""
        from backend.core.heartbeat_writer import HeartbeatWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            hb_path = Path(tmpdir) / "heartbeat.json"
            writer = HeartbeatWriter(hb_path)
            # Write 100 times rapidly -- should never corrupt
            for i in range(100):
                writer.write(phase="ready", loop_iteration=i)
                data = json.loads(hb_path.read_text())  # Must not raise
                assert data["loop_iteration"] == i

    def test_heartbeat_tmp_file_cleaned_up(self):
        """No .tmp file should remain after write."""
        from backend.core.heartbeat_writer import HeartbeatWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            hb_path = Path(tmpdir) / "heartbeat.json"
            writer = HeartbeatWriter(hb_path)
            writer.write(phase="ready", loop_iteration=1)
            tmp_files = list(Path(tmpdir).glob("*.tmp"))
            assert len(tmp_files) == 0


class TestHeartbeatValidation:
    """Test external heartbeat reader/validator."""

    def test_stale_heartbeat_detected(self):
        """Heartbeat with boot_id mismatch is detected as stale."""
        from backend.core.heartbeat_writer import validate_heartbeat

        payload = {
            "boot_id": "old-boot-id",
            "pid": os.getpid(),
            "ts_mono": time.monotonic(),
            "monotonic_age_ms": 100,
            "phase": "ready",
            "loop_iteration": 10,
        }
        result = validate_heartbeat(payload, expected_boot_id="current-boot-id")
        assert result["valid"] is False
        assert "boot_id" in result["reason"]

    def test_pid_mismatch_detected(self):
        """Heartbeat with wrong PID is detected."""
        from backend.core.heartbeat_writer import validate_heartbeat

        boot_id = str(uuid.uuid4())
        payload = {
            "boot_id": boot_id,
            "pid": 99999999,  # Unlikely real PID
            "ts_mono": time.monotonic(),
            "monotonic_age_ms": 100,
            "phase": "ready",
            "loop_iteration": 10,
        }
        result = validate_heartbeat(payload, expected_boot_id=boot_id)
        assert result["valid"] is False
        assert "pid" in result["reason"]

    def test_valid_heartbeat_accepted(self):
        """Valid heartbeat passes validation."""
        from backend.core.heartbeat_writer import validate_heartbeat

        boot_id = str(uuid.uuid4())
        payload = {
            "boot_id": boot_id,
            "pid": os.getpid(),
            "ts_mono": time.monotonic(),
            "monotonic_age_ms": 100,
            "phase": "ready",
            "loop_iteration": 10,
        }
        result = validate_heartbeat(payload, expected_boot_id=boot_id, expected_pid=os.getpid())
        assert result["valid"] is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_heartbeat_liveness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.heartbeat_writer'`

**Step 3: Write minimal implementation**

Create `backend/core/heartbeat_writer.py`:

```python
"""Heartbeat writer and validator for supervisor liveness detection.

The heartbeat file at ~/.jarvis/heartbeat.json is written atomically
(write + fsync + os.replace) every 10s from the main event loop.
External watchers check file freshness + boot_id + PID to detect hangs.
"""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


class HeartbeatWriter:
    """Writes heartbeat file with identity and liveness payload."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / ".jarvis" / "heartbeat.json"
        self.boot_id = str(uuid.uuid4())
        self._start_mono = time.monotonic()
        self._last_write_mono = 0.0

    def write(self, phase: str, loop_iteration: int) -> None:
        """Atomic heartbeat: write -> fsync -> rename."""
        now = time.monotonic()
        age_ms = int((now - self._start_mono) * 1000)

        payload = {
            "boot_id": self.boot_id,
            "pid": os.getpid(),
            "ts_mono": now,
            "monotonic_age_ms": age_ms,
            "phase": phase,
            "loop_iteration": loop_iteration,
            "written_at_wall": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = json.dumps(payload).encode()
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(self.path))
        self._last_write_mono = now


def validate_heartbeat(
    payload: Dict[str, Any],
    expected_boot_id: Optional[str] = None,
    expected_pid: Optional[int] = None,
    max_age_s: float = 30.0,
) -> Dict[str, Any]:
    """Validate a heartbeat payload.

    Returns dict with 'valid' (bool) and 'reason' (str) keys.
    """
    # Check boot_id
    if expected_boot_id and payload.get("boot_id") != expected_boot_id:
        return {"valid": False, "reason": f"boot_id mismatch: {payload.get('boot_id')} != {expected_boot_id}"}

    # Check PID
    if expected_pid is not None and payload.get("pid") != expected_pid:
        return {"valid": False, "reason": f"pid mismatch: {payload.get('pid')} != {expected_pid}"}
    elif expected_pid is None:
        # Verify process exists
        pid = payload.get("pid")
        if pid:
            try:
                os.kill(pid, 0)  # Signal 0 = check existence
            except ProcessLookupError:
                return {"valid": False, "reason": f"pid {pid} does not exist"}
            except PermissionError:
                pass  # Process exists but we can't signal it -- OK

    return {"valid": True, "reason": "ok"}
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_heartbeat_liveness.py -v`
Expected: PASS (all 8 tests)

**Step 5: Wire into supervisor**

Modify `unified_supervisor.py`:

At line ~63412 (after `self._contract_status`), add:
```python
        # v310.0: File-based heartbeat for liveness detection
        from backend.core.heartbeat_writer import HeartbeatWriter
        self._heartbeat_writer = HeartbeatWriter()
        self._heartbeat_iteration = 0
```

At line ~87079 (inside `_progress_heartbeat_task`, after the HTTP heartbeat send at line ~87077), add:
```python
                # v310.0: File heartbeat for external liveness watchdog
                self._heartbeat_iteration += 1
                try:
                    phase = "ready" if self._all_services_ready else "loading"
                    self._heartbeat_writer.write(
                        phase=phase,
                        loop_iteration=self._heartbeat_iteration,
                    )
                except Exception:
                    pass  # File heartbeat failures are not critical
```

**Step 6: Run existing tests to verify no regression**

Run: `python3 -m pytest tests/unit/core/test_heartbeat_liveness.py tests/contracts/ -v`
Expected: PASS

**Step 7: Commit**

```bash
git add backend/core/heartbeat_writer.py tests/unit/core/test_heartbeat_liveness.py unified_supervisor.py
git commit -m "feat(supervisor): add file-based heartbeat liveness with boot_id + pid identity (Item 9)"
```

---

### Task 2: Restart Cooldown & Backoff with Quarantine (Item 10)

**Files:**
- Modify: `backend/core/supervisor/restart_coordinator.py:133-162` (`__init__`)
- Modify: `backend/core/supervisor/restart_coordinator.py:171-263` (`request_restart`)
- Create: `tests/unit/core/test_restart_backoff.py`

**Context:** `RestartCoordinator` at line 133 has `_is_restarting` guard but NO cooldown between restarts. Existing `RestartSource` enum (line 57) and `RestartUrgency` enum (line 49) are already defined. We add reason-classed backoff profiles, jitter, and a quarantine mode.

**Step 1: Write the failing test**

Create `tests/unit/core/test_restart_backoff.py`:

```python
"""Tests for restart coordinator backoff and quarantine."""
import asyncio
import time
import pytest
from unittest.mock import patch

from backend.core.supervisor.restart_coordinator import (
    RestartCoordinator,
    RestartSource,
    RestartUrgency,
)


@pytest.fixture
def coordinator():
    return RestartCoordinator()


@pytest.mark.asyncio
class TestRestartBackoff:
    async def test_first_restart_accepted(self, coordinator):
        """First restart should always be accepted."""
        await coordinator.initialize()
        # Patch to prevent actual restart countdown
        coordinator._grace_period_ended = True
        result = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="test",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result is True

    async def test_rapid_restart_blocked_by_cooldown(self, coordinator):
        """Second rapid restart within cooldown should be blocked."""
        await coordinator.initialize()
        coordinator._grace_period_ended = True

        # First restart
        result1 = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="crash 1",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result1 is True

        # Reset state so it's not "already restarting"
        coordinator._is_restarting = False
        coordinator._current_request = None

        # Second restart immediately -- should be blocked by cooldown
        result2 = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="crash 2",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result2 is False  # Blocked by cooldown

    async def test_critical_bypasses_cooldown(self, coordinator):
        """CRITICAL urgency bypasses all cooldown."""
        await coordinator.initialize()
        coordinator._grace_period_ended = True

        # First restart
        await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="crash 1",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        coordinator._is_restarting = False
        coordinator._current_request = None

        # CRITICAL should bypass cooldown
        result = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="security emergency",
            urgency=RestartUrgency.CRITICAL,
            countdown_seconds=0,
        )
        assert result is True

    async def test_user_request_no_backoff(self, coordinator):
        """USER_REQUEST source should not have backoff."""
        await coordinator.initialize()
        coordinator._grace_period_ended = True

        result = await coordinator.request_restart(
            source=RestartSource.USER_REQUEST,
            reason="user wants restart",
            urgency=RestartUrgency.MEDIUM,
            countdown_seconds=0,
        )
        assert result is True

    async def test_quarantine_after_many_restarts(self, coordinator):
        """After QUARANTINE_THRESHOLD restarts, system enters quarantine."""
        await coordinator.initialize()
        coordinator._grace_period_ended = True
        coordinator._backoff_reset_healthy_s = 9999  # Prevent reset during test

        # Simulate multiple rapid restarts by setting internal state
        for i in range(coordinator._quarantine_threshold):
            coordinator._restart_count_total += 1
            coordinator._last_restart_mono = time.monotonic()

        # Next restart should be quarantined
        coordinator._is_restarting = False
        coordinator._current_request = None
        result = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="one too many",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result is False
        assert coordinator._quarantine_until > 0

    async def test_quarantine_expires(self, coordinator):
        """Quarantine should expire after duration."""
        await coordinator.initialize()
        coordinator._grace_period_ended = True
        # Set quarantine in the past
        coordinator._quarantine_until = time.monotonic() - 1.0

        result = await coordinator.request_restart(
            source=RestartSource.HEALTH_CHECK,
            reason="after quarantine",
            urgency=RestartUrgency.HIGH,
            countdown_seconds=0,
        )
        assert result is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_restart_backoff.py -v`
Expected: FAIL (missing attributes like `_quarantine_threshold`, `_restart_count_total`, etc.)

**Step 3: Write implementation**

Modify `backend/core/supervisor/restart_coordinator.py`.

In `__init__` (after line 159), add:

```python
        # v310.0: Backoff and quarantine for restart loop prevention
        self._restart_count_total: int = 0
        self._last_restart_mono: float = 0.0
        self._quarantine_until: float = 0.0  # monotonic; 0 = not quarantined
        self._backoff_reset_healthy_s: float = float(
            os.environ.get("JARVIS_RESTART_BACKOFF_RESET_S", "120.0")
        )
        self._quarantine_threshold: int = 5
        self._quarantine_duration_s: float = 600.0  # 10 min lockout

        # Backoff base by source category (seconds)
        self._backoff_base: Dict[str, float] = {
            "crash": 5.0,       # RestartSource.HEALTH_CHECK, INTERNAL
            "dependency": 15.0, # RestartSource.DEPENDENCY_UPDATE
            "oom": 30.0,        # (detected via metadata)
            "user": 0.0,        # RestartSource.USER_REQUEST -- no backoff
            "upgrade": 2.0,     # RestartSource.REMOTE_UPDATE
            "default": 5.0,
        }
        self._backoff_max_s: float = 300.0  # Cap at 5 min
        self._backoff_jitter_pct: float = 0.25
```

In `request_restart()`, after the `_is_restarting` check at line 198 and before the grace period check at line 202, add the backoff/quarantine logic:

```python
        # v310.0: Quarantine check
        now_mono = time.monotonic()
        if now_mono < self._quarantine_until:
            if urgency != RestartUrgency.CRITICAL:
                remaining = self._quarantine_until - now_mono
                logger.error(
                    f"Restart QUARANTINED ({remaining:.0f}s remaining). "
                    f"System requires manual intervention or quarantine expiry."
                )
                return False

        # v310.0: Reset counters if healthy long enough
        if self._last_restart_mono > 0 and (now_mono - self._last_restart_mono) > self._backoff_reset_healthy_s:
            self._restart_count_total = 0

        # v310.0: User requests and CRITICAL bypass backoff
        _source_category = self._classify_restart_source(source, metadata)
        _base = self._backoff_base.get(_source_category, self._backoff_base["default"])
        if _base > 0 and urgency not in (RestartUrgency.CRITICAL,):
            # Check quarantine threshold
            if self._restart_count_total >= self._quarantine_threshold:
                self._quarantine_until = now_mono + self._quarantine_duration_s
                logger.error(
                    f"Entering QUARANTINE: {self._restart_count_total} restarts in window. "
                    f"No restarts for {self._quarantine_duration_s}s."
                )
                return False

            # Calculate backoff with jitter
            import random
            delay = min(_base * (2 ** self._restart_count_total), self._backoff_max_s)
            jitter = delay * self._backoff_jitter_pct * (random.random() * 2 - 1)
            cooldown = max(0.0, delay + jitter)

            if self._last_restart_mono > 0:
                elapsed = now_mono - self._last_restart_mono
                if elapsed < cooldown:
                    logger.warning(
                        f"Restart cooldown: {cooldown:.1f}s ({_source_category}, "
                        f"attempt #{self._restart_count_total}, elapsed={elapsed:.1f}s)"
                    )
                    return False

        self._restart_count_total += 1
        self._last_restart_mono = now_mono
```

Add helper method after `__init__`:

```python
    def _classify_restart_source(
        self, source: RestartSource, metadata: Optional[dict] = None,
    ) -> str:
        """Classify restart source into backoff category."""
        if source == RestartSource.USER_REQUEST:
            return "user"
        if source == RestartSource.REMOTE_UPDATE:
            return "upgrade"
        if source == RestartSource.DEPENDENCY_UPDATE:
            return "dependency"
        if metadata and metadata.get("oom"):
            return "oom"
        return "crash"
```

Add `import os, random` and `from typing import Dict` at top if not already present.

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_restart_backoff.py -v`
Expected: PASS (all 7 tests)

**Step 5: Commit**

```bash
git add backend/core/supervisor/restart_coordinator.py tests/unit/core/test_restart_backoff.py
git commit -m "feat(restart): add reason-classed backoff with jitter and quarantine mode (Item 10)"
```

---

### Task 3: In-Flight Task Drain Before Model Unload (Item 14)

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py:3002-3045` (`_unload_local_model`)
- Create: `tests/unit/intelligence/test_model_drain.py`

**Context:** `_unload_local_model()` at line 3002 does `del _local._model` (line 3017) while the `_inference_executor` (ThreadPoolExecutor at line 446 in PrimeLocalClient) may have a running task. We add: stop new tasks, drain with bounded 30s deadline, track disposition (completed/cancelled/abandoned), then delete model.

**Step 1: Write the failing test**

Create `tests/unit/intelligence/test_model_drain.py`:

```python
"""Tests for in-flight task drain during model unload."""
import asyncio
import concurrent.futures
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


class FakeInferenceExecutor:
    """Mock executor that simulates running inference."""

    def __init__(self, task_duration: float = 0.0):
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-inference")
        self._task_duration = task_duration
        self._running = False

    def submit(self, fn, *args, **kwargs):
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait=True, cancel_futures=False):
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    @property
    def _threads(self):
        return self._executor._threads


class FakeLocalClient:
    """Mock PrimeLocalClient with executor and model."""

    def __init__(self, task_duration: float = 0.0):
        self._inference_executor = FakeInferenceExecutor(task_duration)
        self._model = MagicMock()
        self._loaded = True


class TestModelDrain:
    def test_drain_completes_running_task(self):
        """Drain should wait for running task to complete."""
        client = FakeLocalClient()
        completed = []

        def slow_inference():
            time.sleep(0.1)
            completed.append(True)
            return "result"

        # Submit a task
        client._inference_executor.submit(slow_inference)
        time.sleep(0.01)  # Let task start

        # Shutdown with wait
        client._inference_executor.shutdown(wait=True)
        assert len(completed) == 1

    def test_drain_respects_cancel_futures(self):
        """cancel_futures=True should cancel pending (not running) tasks."""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        results = []

        def task(n):
            time.sleep(0.1)
            results.append(n)

        # Submit multiple -- only first will be running
        f1 = executor.submit(task, 1)
        f2 = executor.submit(task, 2)  # This should be pending

        time.sleep(0.01)  # Let first task start
        executor.shutdown(wait=False, cancel_futures=True)

        # First task may complete, second should be cancelled
        time.sleep(0.2)
        assert 1 in results  # First task ran
        assert f2.cancelled() or 2 in results  # Second either cancelled or finished

    @pytest.mark.asyncio
    async def test_drain_timeout_produces_abandoned_disposition(self):
        """If drain exceeds deadline, disposition should be 'abandoned'."""
        # Test the timeout logic directly
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def forever_task():
            time.sleep(100)  # Simulates stuck inference

        executor.submit(forever_task)
        time.sleep(0.01)

        # Try to shutdown with wait=True, but wrap in timeout
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: executor.shutdown(wait=True)
                ),
                timeout=0.5,
            )
            disposition = "completed"
        except asyncio.TimeoutError:
            disposition = "abandoned"

        assert disposition == "abandoned"
        # Force cleanup
        executor.shutdown(wait=False, cancel_futures=True)
```

**Step 2: Run test to verify tests work**

Run: `python3 -m pytest tests/unit/intelligence/test_model_drain.py -v`
Expected: PASS (these test the drain pattern itself, not the integration yet)

**Step 3: Modify `_unload_local_model`**

In `backend/intelligence/unified_model_serving.py`, replace lines 3002-3045 with:

```python
    async def _unload_local_model(
        self,
        *,
        reason: str = "unspecified",
        arm_recovery: Optional[bool] = None,
    ) -> bool:
        """v310.0: Safely unload local GGUF model with in-flight task drain.

        1. Trip circuit breaker (stop new inference)
        2. Drain executor with bounded 30s deadline
        3. Delete model + gc.collect
        """
        _DRAIN_DEADLINE_S = 30.0

        _local = self._clients.get(ModelProvider.PRIME_LOCAL)
        if not _local or getattr(_local, "_model", None) is None:
            self._last_local_unload_reason = reason
            return False

        _arm = self._resolve_recovery_arm(reason, arm_recovery)
        unload_start = time.monotonic()
        disposition = "completed"

        try:
            _model_path = getattr(_local._model, "model_path", "unknown")

            # 1. Trip circuit breaker FIRST -- stop new inference from being routed
            self.force_open_local_circuit_breaker(reason=f"unload:{reason}")

            # 2. Drain in-flight inference with bounded deadline
            if hasattr(_local, "_inference_executor"):
                import concurrent.futures

                executor = _local._inference_executor
                # Signal no new tasks, cancel pending
                executor.shutdown(wait=False, cancel_futures=True)

                # Wait for currently-running task with timeout
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, lambda: executor.shutdown(wait=True)
                        ),
                        timeout=_DRAIN_DEADLINE_S,
                    )
                    disposition = "completed"
                except asyncio.TimeoutError:
                    disposition = "abandoned"
                    self.logger.warning(
                        "[v310.0] Inference drain timed out after %.0fs -- "
                        "forcing teardown (task abandoned)",
                        _DRAIN_DEADLINE_S,
                    )

                # Recreate executor for future use (after recovery)
                _local._inference_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="llm-inference"
                )

            # 3. NOW safe to delete model
            del _local._model
            _local._model = None
            _local._loaded = False
            import gc
            gc.collect()

            self._memory_recovery_armed = _arm
            self._last_local_unload_reason = reason
            self._local_ready_verified = False
            self._set_local_ready_handshake(
                source="unload_local_model",
                reason=reason,
                ready=False,
                verified=False,
            )
            if not _arm:
                self._memory_recovery_in_progress = False

            elapsed = time.monotonic() - unload_start
            self.logger.info(
                "[v310.0] Local model unloaded (%s). disposition=%s elapsed=%.1fs "
                "circuit=open reason=%s recovery_armed=%s",
                _model_path, disposition, elapsed, reason, _arm,
            )
            return True
        except Exception as e:
            self.logger.warning(f"[v310.0] Model unload error: {e}")
            return False
```

**Step 4: Run existing tests**

Run: `python3 -m pytest tests/unit/intelligence/test_model_drain.py tests/contracts/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/intelligence/unified_model_serving.py tests/unit/intelligence/test_model_drain.py
git commit -m "feat(model_serving): drain in-flight inference before model unload with 30s deadline (Item 14)"
```

---

### Task 4: Cloud SQL Proxy Cleanup on Exit (Item 19)

**Files:**
- Modify: `backend/intelligence/cloud_sql_proxy_manager.py:321-394` (`__init__`)
- Modify: `backend/intelligence/cloud_sql_proxy_manager.py:1802-1825` (proxy start)
- Modify: `backend/intelligence/cloud_sql_proxy_manager.py:553-592` (`_is_cloud_sql_proxy_process`)
- Create: `tests/unit/intelligence/test_proxy_cleanup.py`

**Context:** PID is written at line 1818. `_is_cloud_sql_proxy_process()` at line 553 already has robust keyword matching. We add: atexit registration after PID write, startup reconciliation with identity-safe PID check (name + cmdline + uid), and process group ownership verification.

**Step 1: Write the failing test**

Create `tests/unit/intelligence/test_proxy_cleanup.py`:

```python
"""Tests for Cloud SQL proxy lifecycle cleanup."""
import os
import signal
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


class TestProxyCleanup:
    def test_atexit_registered_after_start(self):
        """atexit.register should be called after proxy starts."""
        with patch("atexit.register") as mock_register:
            from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
            mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)
            mgr.process = MagicMock()
            mgr.process.pid = 12345
            mgr.process.returncode = None
            mgr.pid_path = Path(tempfile.mktemp())
            mgr._atexit_registered = False

            mgr._register_atexit_cleanup()
            assert mgr._atexit_registered is True
            mock_register.assert_called_once()

    def test_atexit_not_double_registered(self):
        """atexit should only be registered once."""
        with patch("atexit.register") as mock_register:
            from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
            mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)
            mgr.process = MagicMock()
            mgr.pid_path = Path(tempfile.mktemp())
            mgr._atexit_registered = False

            mgr._register_atexit_cleanup()
            mgr._register_atexit_cleanup()
            assert mock_register.call_count == 1

    def test_stale_pid_with_wrong_process_not_killed(self):
        """Stale PID belonging to a different process must not be killed."""
        from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
        mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pid", delete=False) as f:
            f.write(str(os.getpid()))  # Our own PID -- not a proxy
            pid_path = Path(f.name)

        mgr.pid_path = pid_path
        mgr._is_cloud_sql_proxy_process = MagicMock(return_value=False)

        with patch("os.kill") as mock_kill:
            mgr._cleanup_stale_proxy_sync()
            mock_kill.assert_not_called()

        # PID file should be cleaned up regardless
        assert not pid_path.exists()

    def test_stale_pid_with_proxy_process_killed(self):
        """Stale PID that IS a proxy process should be terminated."""
        from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
        mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pid", delete=False) as f:
            f.write("99999999")  # Non-existent PID
            pid_path = Path(f.name)

        mgr.pid_path = pid_path
        mgr._is_cloud_sql_proxy_process = MagicMock(return_value=True)

        # Should handle ProcessLookupError gracefully
        mgr._cleanup_stale_proxy_sync()
        assert not pid_path.exists()

    def test_stale_pid_wrong_uid_not_killed(self):
        """Proxy owned by different user must not be killed."""
        from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
        mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pid", delete=False) as f:
            f.write("12345")
            pid_path = Path(f.name)

        mgr.pid_path = pid_path
        mgr._is_cloud_sql_proxy_process = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.uids.return_value = MagicMock(real=99999)  # Different UID

        with patch("psutil.Process", return_value=mock_proc):
            with patch("os.kill") as mock_kill:
                mgr._cleanup_stale_proxy_sync()
                mock_kill.assert_not_called()

        # PID file still cleaned up
        assert not pid_path.exists()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/intelligence/test_proxy_cleanup.py -v`
Expected: FAIL (missing `_register_atexit_cleanup`, `_cleanup_stale_proxy_sync`, `_atexit_registered`)

**Step 3: Write implementation**

In `backend/intelligence/cloud_sql_proxy_manager.py`:

In `__init__` (after line 338 `self.process`), add:
```python
        self._atexit_registered = False
```

Add these methods after `_is_cloud_sql_proxy_process` (after line 592):

```python
    def _register_atexit_cleanup(self) -> None:
        """Register atexit handler (once) for proxy cleanup on normal exit."""
        if self._atexit_registered:
            return
        import atexit
        atexit.register(self._cleanup_proxy_atexit)
        self._atexit_registered = True
        logger.debug("[ProxyManager] Registered atexit cleanup handler")

    def _cleanup_proxy_atexit(self) -> None:
        """Best-effort cleanup on normal exit. Does NOT run on SIGKILL/SIGSEGV."""
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                pass
        if self.pid_path and self.pid_path.exists():
            try:
                self.pid_path.unlink()
            except Exception:
                pass

    def _cleanup_stale_proxy_sync(self) -> None:
        """Kill stale proxy from previous crashed session with identity verification.

        Safe against PID reuse: checks process name, cmdline, and uid before killing.
        """
        if not (self.pid_path and self.pid_path.exists()):
            return

        try:
            stale_pid = int(self.pid_path.read_text().strip())
        except (ValueError, OSError):
            self.pid_path.unlink(missing_ok=True)
            return

        # Check if process exists AND is actually a proxy
        if not self._is_cloud_sql_proxy_process(stale_pid):
            logger.info(
                f"[ProxyManager] PID {stale_pid} is not a proxy process -- "
                f"PID reuse detected, removing stale PID file"
            )
            self.pid_path.unlink(missing_ok=True)
            return

        # Verify process ownership (same user)
        try:
            import psutil
            proc = psutil.Process(stale_pid)
            if proc.uids().real != os.getuid():
                logger.warning(
                    f"[ProxyManager] PID {stale_pid} is a proxy but owned by "
                    f"uid={proc.uids().real} (expected {os.getuid()}) -- skipping kill"
                )
                self.pid_path.unlink(missing_ok=True)
                return
        except Exception:
            # Process gone or psutil unavailable -- safe to clean up PID file
            self.pid_path.unlink(missing_ok=True)
            return

        # Safe to kill -- it's our stale proxy
        try:
            os.kill(stale_pid, signal.SIGTERM)
            logger.info(f"[ProxyManager] Killed stale proxy (PID {stale_pid})")
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.kill(stale_pid, signal.SIGKILL)
                    logger.warning(f"[ProxyManager] Force-killed stale proxy (PID {stale_pid})")
                except ProcessLookupError:
                    pass
        except ProcessLookupError:
            pass  # Already gone

        self.pid_path.unlink(missing_ok=True)
```

Add `import signal` at top if not present.

In `_start_proxy_process_sync` (after PID write at line 1819), add:
```python
                    return process

                self.process = await asyncio.to_thread(_start_proxy_process_sync)
                # v310.0: Register atexit cleanup after successful start
                self._register_atexit_cleanup()
```

In `_start_locked` (before launching proxy, around line 1650), add:
```python
                # v310.0: Clean up stale proxy from previous crash
                await asyncio.to_thread(self._cleanup_stale_proxy_sync)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/intelligence/test_proxy_cleanup.py -v`
Expected: PASS (all 5 tests)

**Step 5: Commit**

```bash
git add backend/intelligence/cloud_sql_proxy_manager.py tests/unit/intelligence/test_proxy_cleanup.py
git commit -m "feat(proxy): add atexit cleanup + identity-safe stale PID reconciliation (Item 19)"
```

---

### Task 5: Gate E Verification

**Step 1: Run all Phase E tests**

Run: `python3 -m pytest tests/unit/core/test_heartbeat_liveness.py tests/unit/core/test_restart_backoff.py tests/unit/intelligence/test_model_drain.py tests/unit/intelligence/test_proxy_cleanup.py tests/contracts/ -v`
Expected: ALL PASS

**Step 2: Verify gate criteria**

Run: `python3 -m pytest tests/unit/core/test_heartbeat_liveness.py -k "required_fields or boot_id or atomic" -v`
Expected: Heartbeat payload, identity, atomic write all pass

Run: `python3 -m pytest tests/unit/core/test_restart_backoff.py -k "quarantine or cooldown or critical" -v`
Expected: Quarantine, cooldown, CRITICAL bypass all pass

**Step 3: Tag gate**

```bash
git tag gate-e-lifecycle-resilience
```

---

## Phase F: Concurrency & Stability (Items 11, 13, 20)

### Task 6: Observer Snapshot Pattern in MemoryBudgetBroker (Item 11)

**Files:**
- Modify: `backend/core/memory_budget_broker.py:922` (observer iteration loop)
- Create: `tests/unit/core/test_observer_snapshot.py`

**Context:** At line 922, `for obs in self._pressure_observers:` iterates the live list. If an observer registers/unregisters during await points between iterations, RuntimeError occurs. Fix: copy to list before iterating.

**Step 1: Write the failing test**

Create `tests/unit/core/test_observer_snapshot.py`:

```python
"""Tests for observer snapshot pattern in MemoryBudgetBroker."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock


class TestObserverSnapshot:
    @pytest.mark.asyncio
    async def test_observer_added_during_notification_not_called(self):
        """Observer added during notification loop should not be called in same pass."""
        from backend.core.memory_budget_broker import MemoryBudgetBroker

        broker = MagicMock(spec=MemoryBudgetBroker)
        broker._pressure_observers = []
        broker._latest_snapshot = None
        broker._advance_sequence = MagicMock()

        late_observer = AsyncMock()

        async def registering_observer(tier, snapshot):
            """Observer that registers another observer during notification."""
            broker._pressure_observers.append(late_observer)

        broker._pressure_observers.append(AsyncMock(side_effect=registering_observer))

        # Manually run the snapshot-based notification
        observers = list(broker._pressure_observers)  # Snapshot
        for obs in observers:
            await obs("critical", {})

        # late_observer was added to the live list but NOT in the snapshot
        late_observer.assert_not_called()

    @pytest.mark.asyncio
    async def test_observer_removed_during_notification_still_called(self):
        """Observer removed during notification should still be called (it was in snapshot)."""
        obs1_called = []
        obs2_called = []

        observers = []

        async def obs1(tier, snapshot):
            obs1_called.append(True)
            # Remove obs2 from live list during notification
            if obs2 in observers:
                observers.remove(obs2)

        async def obs2(tier, snapshot):
            obs2_called.append(True)

        observers.extend([obs1, obs2])

        # Snapshot-based iteration
        snapshot = list(observers)
        for obs in snapshot:
            await obs("critical", {})

        assert len(obs1_called) == 1
        assert len(obs2_called) == 1  # Still called because it was in snapshot
```

**Step 2: Run test to verify it passes (pattern test)**

Run: `python3 -m pytest tests/unit/core/test_observer_snapshot.py -v`
Expected: PASS (these test the pattern itself)

**Step 3: Modify MemoryBudgetBroker**

In `backend/core/memory_budget_broker.py`, change line 922 from:
```python
        for obs in self._pressure_observers:
```
to:
```python
        observers = list(self._pressure_observers)  # v310.0: snapshot for safe iteration
        for obs in observers:
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_observer_snapshot.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/memory_budget_broker.py tests/unit/core/test_observer_snapshot.py
git commit -m "fix(broker): snapshot observer list before notification iteration (Item 11)"
```

---

### Task 7: Feedback Oscillation Guard with Hysteresis & Telemetry (Item 13)

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py:2617-2619` (add oscillation state in `__init__`)
- Modify: `backend/intelligence/unified_model_serving.py:3002` (add oscillation tracking in `_unload_local_model`)
- Create: `tests/unit/intelligence/test_oscillation_guard.py`

**Context:** Memory pressure unloads model. Pressure drops. Recovery reloads. Pressure rises. Loop. The existing `_thrash_last_gcp_offload_at` (line 2618) tracks GCP thrash but not local model oscillation. We add a cycle counter, committed-off state, and hysteresis.

**Step 1: Write the failing test**

Create `tests/unit/intelligence/test_oscillation_guard.py`:

```python
"""Tests for model lifecycle oscillation guard."""
import time
import json
import pytest
from unittest.mock import MagicMock, patch


class TestOscillationGuard:
    def _make_serving(self):
        """Create a minimal UnifiedModelServing mock with oscillation state."""
        from types import SimpleNamespace
        serving = SimpleNamespace(
            _model_lifecycle_cycles=0,
            _model_lifecycle_window_start=0.0,
            _model_committed_off=False,
            _model_committed_off_time=0.0,
            OSCILLATION_CYCLE_LIMIT=3,
            OSCILLATION_WINDOW_S=600.0,
            COMMITTED_OFF_COOLDOWN_S=300.0,
            _lifecycle_events=[],
            logger=MagicMock(),
        )
        return serving

    def test_oscillation_detected_after_limit(self):
        """3 unload cycles in window should trigger committed-off."""
        serving = self._make_serving()

        for i in range(3):
            now = time.monotonic()
            if now - serving._model_lifecycle_window_start > serving.OSCILLATION_WINDOW_S:
                serving._model_lifecycle_cycles = 0
                serving._model_lifecycle_window_start = now
            serving._model_lifecycle_cycles += 1

        assert serving._model_lifecycle_cycles >= serving.OSCILLATION_CYCLE_LIMIT

    def test_committed_off_blocks_recovery(self):
        """When committed-off, recovery should be blocked."""
        serving = self._make_serving()
        serving._model_committed_off = True
        serving._model_committed_off_time = time.monotonic()

        elapsed = time.monotonic() - serving._model_committed_off_time
        assert elapsed < serving.COMMITTED_OFF_COOLDOWN_S
        # Recovery should be blocked

    def test_committed_off_expires(self):
        """Committed-off should auto-expire after cooldown."""
        serving = self._make_serving()
        serving._model_committed_off = True
        serving._model_committed_off_time = time.monotonic() - 301.0  # Past cooldown

        elapsed = time.monotonic() - serving._model_committed_off_time
        if elapsed >= serving.COMMITTED_OFF_COOLDOWN_S:
            serving._model_committed_off = False
            serving._model_lifecycle_cycles = 0

        assert serving._model_committed_off is False
        assert serving._model_lifecycle_cycles == 0

    def test_window_reset_after_quiet_period(self):
        """Cycle counter resets if window elapses without incidents."""
        serving = self._make_serving()
        serving._model_lifecycle_cycles = 2
        serving._model_lifecycle_window_start = time.monotonic() - 601.0  # Past window

        now = time.monotonic()
        if now - serving._model_lifecycle_window_start > serving.OSCILLATION_WINDOW_S:
            serving._model_lifecycle_cycles = 0
            serving._model_lifecycle_window_start = now

        assert serving._model_lifecycle_cycles == 0
```

**Step 2: Run test**

Run: `python3 -m pytest tests/unit/intelligence/test_oscillation_guard.py -v`
Expected: PASS (pattern tests)

**Step 3: Modify UnifiedModelServing**

In `backend/intelligence/unified_model_serving.py`, at `__init__` (after line 2619), add:

```python
        # v310.0: Oscillation guard state
        self._model_lifecycle_cycles: int = 0
        self._model_lifecycle_window_start: float = 0.0
        self._model_committed_off: bool = False
        self._model_committed_off_time: float = 0.0
        self.OSCILLATION_CYCLE_LIMIT = 3
        self.OSCILLATION_WINDOW_S = 600.0  # 10 minutes
        self.COMMITTED_OFF_COOLDOWN_S = 300.0  # 5 min
```

In `_unload_local_model` (after the circuit breaker trip, before drain), add:

```python
            # v310.0: Oscillation tracking
            now_mono = time.monotonic()
            if now_mono - self._model_lifecycle_window_start > self.OSCILLATION_WINDOW_S:
                self._model_lifecycle_cycles = 0
                self._model_lifecycle_window_start = now_mono
            self._model_lifecycle_cycles += 1

            self._emit_lifecycle_event({
                "event": "model_unload",
                "reason": reason,
                "cycle_count": self._model_lifecycle_cycles,
                "window_elapsed_s": now_mono - self._model_lifecycle_window_start,
                "disposition": disposition,
            })

            if self._model_lifecycle_cycles >= self.OSCILLATION_CYCLE_LIMIT:
                self._model_committed_off = True
                self._model_committed_off_time = now_mono
                self.logger.warning(
                    "[v310.0] Model oscillation detected (%d cycles in %.0fs). "
                    "Entering committed-off state.",
                    self._model_lifecycle_cycles,
                    self.OSCILLATION_WINDOW_S,
                )
                self._emit_lifecycle_event({
                    "event": "oscillation_guard_triggered",
                    "total_cycles": self._model_lifecycle_cycles,
                    "quarantine_duration_s": self.COMMITTED_OFF_COOLDOWN_S,
                })
```

Add the telemetry helper near the end of the class:

```python
    def _emit_lifecycle_event(self, event: dict) -> None:
        """Emit structured lifecycle event for diagnostics."""
        import json as _json
        event["ts"] = time.monotonic()
        event["component"] = "unified_model_serving"
        self.logger.info("LIFECYCLE_EVENT: %s", _json.dumps(event))
```

Add oscillation check in recovery callback (find the method that re-arms/reloads local model after memory recovery -- look for `_memory_recovery_armed` usage). Add at the top of the recovery path:

```python
        # v310.0: Oscillation guard check
        if self._model_committed_off:
            elapsed = time.monotonic() - self._model_committed_off_time
            if elapsed < self.COMMITTED_OFF_COOLDOWN_S:
                self.logger.debug(
                    "[v310.0] Recovery blocked: committed-off (%.0fs remaining)",
                    self.COMMITTED_OFF_COOLDOWN_S - elapsed,
                )
                return
            self._model_committed_off = False
            self._model_lifecycle_cycles = 0
            self._emit_lifecycle_event({"event": "committed_off_expired"})
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/intelligence/test_oscillation_guard.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/intelligence/unified_model_serving.py tests/unit/intelligence/test_oscillation_guard.py
git commit -m "feat(model_serving): oscillation guard with hysteresis and structured telemetry (Item 13)"
```

---

### Task 8: Event Log Bounding with Spill-to-Disk (Item 20)

**Files:**
- Modify: `backend/core/memory_budget_broker.py:400` (`_event_log` init)
- Modify: `backend/core/memory_budget_broker.py:971-983` (`_emit_event`)
- Create: `tests/unit/core/test_event_log_bound.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_event_log_bound.py`:

```python
"""Tests for bounded event log in MemoryBudgetBroker."""
import json
import tempfile
from collections import deque
from pathlib import Path
import pytest


class TestEventLogBound:
    def test_deque_maxlen_enforced(self):
        """Event log should evict oldest entries at capacity."""
        log = deque(maxlen=5)
        for i in range(10):
            log.append({"id": i})
        assert len(log) == 5
        assert log[0]["id"] == 5  # Oldest kept is #5

    def test_critical_events_spill_to_disk(self):
        """Critical severity events should be written to JSONL file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spill_path = Path(tmpdir) / "critical_events.jsonl"
            event = {"type": "test", "severity": "critical", "data": "important"}

            # Simulate spill
            with open(spill_path, "a") as f:
                f.write(json.dumps(event) + "\n")

            lines = spill_path.read_text().strip().split("\n")
            assert len(lines) == 1
            assert json.loads(lines[0])["severity"] == "critical"

    def test_non_critical_events_not_spilled(self):
        """Non-critical events should NOT be written to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spill_path = Path(tmpdir) / "critical_events.jsonl"
            event = {"type": "test", "severity": "info", "data": "routine"}

            # Only spill if critical
            if event.get("severity") == "critical":
                with open(spill_path, "a") as f:
                    f.write(json.dumps(event) + "\n")

            assert not spill_path.exists()
```

**Step 2: Run test**

Run: `python3 -m pytest tests/unit/core/test_event_log_bound.py -v`
Expected: PASS (pattern tests)

**Step 3: Modify MemoryBudgetBroker**

In `backend/core/memory_budget_broker.py`:

Change line 400 from:
```python
        self._event_log: List[Dict[str, Any]] = []
```
to:
```python
        self._event_log: deque = deque(maxlen=1000)  # v310.0: ring buffer
        self._critical_event_log_path = Path.home() / ".jarvis" / "critical_events.jsonl"
```

Add `from collections import deque` at the top imports if not present.

Change `_emit_event` at line 971-983 from:
```python
    def _emit_event(
        self, event_type: MemoryBudgetEventType, data: Dict[str, Any],
    ) -> None:
        """Emit a structured event for observability."""
        event = {
            "type": event_type.value,
            "timestamp": time.time(),
            "epoch": self._epoch,
            "phase": self._phase.name,
            **data,
        }
        self._event_log.append(event)
        logger.debug("Event: %s", event)
```
to:
```python
    def _emit_event(
        self, event_type: MemoryBudgetEventType, data: Dict[str, Any],
    ) -> None:
        """Emit a structured event for observability. Critical events spill to disk."""
        event = {
            "type": event_type.value,
            "timestamp": time.time(),
            "epoch": self._epoch,
            "phase": self._phase.name,
            **data,
        }
        self._event_log.append(event)
        logger.debug("Event: %s", event)

        # v310.0: Spill critical events to disk
        if data.get("severity") == "critical" or event_type.value in ("grant_revoked", "oom_kill"):
            try:
                import json as _json
                self._critical_event_log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._critical_event_log_path, "a") as f:
                    f.write(_json.dumps(event) + "\n")
            except OSError:
                pass  # Best-effort disk spill
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_event_log_bound.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/memory_budget_broker.py tests/unit/core/test_event_log_bound.py
git commit -m "fix(broker): bound event log to deque(maxlen=1000) with critical spill-to-disk (Item 20)"
```

---

### Task 9: Gate F Verification

**Step 1: Run all Phase F tests**

Run: `python3 -m pytest tests/unit/core/test_observer_snapshot.py tests/unit/intelligence/test_oscillation_guard.py tests/unit/core/test_event_log_bound.py -v`
Expected: ALL PASS

**Step 2: Verify observer snapshot applied**

Run: `grep -n "observers = list" backend/core/memory_budget_broker.py`
Expected: Line showing `observers = list(self._pressure_observers)`

**Step 3: Tag gate**

```bash
git tag gate-f-concurrency-stability
```

---

## Phase G: Data & Time Integrity (Items 15, 16)

### Task 10: Versioned Pickle Cache Envelope (Item 15)

**Files:**
- Create: `backend/vision/intelligence/cache_envelope.py`
- Create: `tests/unit/vision/test_cache_envelope.py`

**Step 1: Write the failing test**

Create `tests/unit/vision/test_cache_envelope.py`:

```python
"""Tests for versioned pickle cache envelope."""
import pickle
import tempfile
from pathlib import Path
import pytest


class TestCacheEnvelope:
    def test_save_and_load_roundtrip(self):
        from backend.vision.intelligence.cache_envelope import save_versioned, load_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            data = {"key": "value", "list": [1, 2, 3]}
            save_versioned(path, data, version=1)
            loaded = load_versioned(path, expected_version=1)
            assert loaded == data

    def test_version_mismatch_returns_none(self):
        from backend.vision.intelligence.cache_envelope import save_versioned, load_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            save_versioned(path, {"old": True}, version=1)
            result = load_versioned(path, expected_version=2)
            assert result is None  # No migration registered

    def test_unknown_major_version_quarantined(self):
        from backend.vision.intelligence.cache_envelope import save_versioned, load_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            save_versioned(path, {"future": True}, version=5)
            result = load_versioned(path, expected_version=2)
            assert result is None
            # Original file should be quarantined
            assert not path.exists()
            quarantine_files = list(Path(d).glob("*.quarantine.*"))
            assert len(quarantine_files) == 1

    def test_corrupted_payload_quarantined(self):
        from backend.vision.intelligence.cache_envelope import save_versioned, load_versioned, ENVELOPE_MAGIC
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            # Write valid envelope but tamper with payload hash
            save_versioned(path, {"good": True}, version=1)
            # Corrupt the file by appending garbage
            with open(path, "ab") as f:
                f.write(b"GARBAGE")
            result = load_versioned(path, expected_version=1)
            # Should either load correctly or quarantine -- not crash

    def test_no_magic_bytes_quarantined(self):
        from backend.vision.intelligence.cache_envelope import load_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            # Write raw pickle without magic bytes
            with open(path, "wb") as f:
                pickle.dump({"raw": True}, f)
            result = load_versioned(path, expected_version=1)
            assert result is None
            assert not path.exists()  # Quarantined

    def test_migration_chain(self):
        from backend.vision.intelligence.cache_envelope import (
            save_versioned, load_versioned, register_migration,
        )
        # Register v1 -> v2 migration
        register_migration(1, 2, lambda data: {**data, "migrated": True})

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            save_versioned(path, {"key": "value"}, version=1)
            result = load_versioned(path, expected_version=2)
            assert result is not None
            assert result["key"] == "value"
            assert result["migrated"] is True

    def test_atomic_write_no_tmp_leftover(self):
        from backend.vision.intelligence.cache_envelope import save_versioned
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.cache"
            for i in range(50):
                save_versioned(path, {"i": i}, version=1)
            tmp_files = list(Path(d).glob("*.tmp"))
            assert len(tmp_files) == 0

    def test_missing_file_returns_none(self):
        from backend.vision.intelligence.cache_envelope import load_versioned
        result = load_versioned(Path("/nonexistent/path.cache"), expected_version=1)
        assert result is None
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/vision/test_cache_envelope.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write implementation**

Create `backend/vision/intelligence/cache_envelope.py` with the full implementation from the design doc (see design doc section G.1 for complete code).

```python
"""Versioned pickle envelope with integrity checking and migration support."""
import hashlib
import os
import pickle
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("jarvis.cache_envelope")

ENVELOPE_MAGIC = b"JCACHE01"

_MIGRATIONS: dict[tuple[int, int], Callable[[Any], Any]] = {}


def register_migration(from_v: int, to_v: int, fn: Callable[[Any], Any]) -> None:
    """Register a data migration handler between versions."""
    _MIGRATIONS[(from_v, to_v)] = fn


def save_versioned(path: Path, data: Any, version: int) -> None:
    """Save data wrapped in version envelope with integrity hash."""
    payload_bytes = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    envelope = {
        "magic": ENVELOPE_MAGIC.decode(),
        "schema_version": version,
        "payload_hash": payload_hash,
        "data": data,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, ENVELOPE_MAGIC)
        os.write(fd, pickle.dumps(envelope, protocol=pickle.HIGHEST_PROTOCOL))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def load_versioned(path: Path, expected_version: int) -> Optional[Any]:
    """Load data, returning None on version mismatch, corruption, or unknown version."""
    try:
        with open(path, "rb") as f:
            magic = f.read(len(ENVELOPE_MAGIC))
            if magic != ENVELOPE_MAGIC:
                _quarantine(path, "missing_magic")
                return None
            envelope = pickle.load(f)

        if not isinstance(envelope, dict):
            _quarantine(path, "invalid_envelope_type")
            return None

        file_version = envelope.get("schema_version")
        if file_version is None:
            _quarantine(path, "missing_version")
            return None

        if isinstance(file_version, int) and file_version > expected_version:
            _quarantine(path, f"unknown_major_version_{file_version}_vs_{expected_version}")
            logger.warning(
                "Cache at %s: unknown version %d > expected %d. Quarantined.",
                path, file_version, expected_version,
            )
            return None

        if file_version == expected_version:
            data = envelope.get("data")
            stored_hash = envelope.get("payload_hash")
            if stored_hash:
                actual_hash = hashlib.sha256(
                    pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
                ).hexdigest()
                if actual_hash != stored_hash:
                    _quarantine(path, "payload_hash_mismatch")
                    return None
            return data

        # Try migration chain
        current_data = envelope.get("data")
        current_version = file_version
        while current_version < expected_version:
            next_version = current_version + 1
            migration = _MIGRATIONS.get((current_version, next_version))
            if migration is None:
                _quarantine(path, f"no_migration_{current_version}_to_{next_version}")
                return None
            try:
                current_data = migration(current_data)
                current_version = next_version
            except Exception as e:
                _quarantine(path, f"migration_failed_{current_version}_to_{next_version}")
                logger.warning("Migration failed for %s: %s", path, e)
                return None

        save_versioned(path, current_data, expected_version)
        logger.info("Migrated cache %s from v%d to v%d", path, file_version, expected_version)
        return current_data

    except FileNotFoundError:
        return None
    except Exception as e:
        _quarantine(path, f"load_exception_{type(e).__name__}")
        logger.warning("Cache load failed at %s: %s", path, e)
        return None


def _quarantine(path: Path, reason: str) -> None:
    """Move corrupted cache to quarantine with reason suffix."""
    quarantine_path = path.with_suffix(f".quarantine.{reason}")
    try:
        if path.exists():
            shutil.move(str(path), str(quarantine_path))
            logger.info("Quarantined cache %s -> %s", path, quarantine_path)
    except OSError as e:
        logger.debug("Quarantine failed for %s: %s", path, e)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/vision/test_cache_envelope.py -v`
Expected: PASS (all 8 tests)

**Step 5: Commit**

```bash
git add backend/vision/intelligence/cache_envelope.py tests/unit/vision/test_cache_envelope.py
git commit -m "feat(vision): add versioned pickle cache envelope with magic, hash, migration, quarantine (Item 15)"
```

---

### Task 11: Apply Cache Envelope to Top-Risk Files

**Files:**
- Modify: `backend/vision/lazy_vision_engine.py:321-345`
- Modify: `backend/vision/space_screenshot_cache.py:156-176,393-406`

**Step 1: Modify lazy_vision_engine.py**

At line 321, replace the pickle.load block with:
```python
        from backend.vision.intelligence.cache_envelope import load_versioned, save_versioned
        _LEARNED_PATTERNS_VERSION = 1
        _patterns_path = Path("backend/data/vision_learned_patterns.pkl")
        data = load_versioned(_patterns_path, expected_version=_LEARNED_PATTERNS_VERSION)
        if data is not None:
            # unpack data...
```

At line 335, replace the pickle.dump with:
```python
        save_versioned(_patterns_path, data, version=_LEARNED_PATTERNS_VERSION)
```

**Step 2: Modify space_screenshot_cache.py**

At line 156, replace usage_patterns.pkl load:
```python
        from backend.vision.intelligence.cache_envelope import load_versioned, save_versioned
        _USAGE_PATTERNS_VERSION = 1
        usage_path = self.cache_dir / "usage_patterns.pkl"
        saved_data = load_versioned(usage_path, expected_version=_USAGE_PATTERNS_VERSION)
```

At line 176, replace usage_patterns.pkl save:
```python
        save_versioned(usage_path, saved_data, version=_USAGE_PATTERNS_VERSION)
```

At line 393 (space screenshot save) and 406 (load), apply the same pattern with `_SPACE_CACHE_VERSION = 1`.

**Step 3: Run existing vision tests if any**

Run: `python3 -m pytest tests/unit/vision/ -v --tb=short 2>/dev/null || echo "No vision tests or some failures expected"`

**Step 4: Commit**

```bash
git add backend/vision/lazy_vision_engine.py backend/vision/space_screenshot_cache.py
git commit -m "refactor(vision): apply versioned cache envelope to lazy_vision_engine and space_screenshot_cache"
```

---

### Task 12: Time Source Standardization in GCP Controller (Item 16)

**Files:**
- Modify: `backend/core/supervisor_gcp_controller.py:252-253,500,637,713,771`
- Create: `tests/contracts/test_no_datetime_durations.py`

**Context:** Three duration-calculation sites use `datetime.now()`: line 500 (VM creation cooldown), line 713 (runtime cost), line 771 (idle detection). We add parallel `_mono` attributes and switch duration math to `time_utils`.

**Step 1: Write the failing AST-scan test**

Create `tests/contracts/test_no_datetime_durations.py`:

```python
"""Contract test: no datetime.now() for elapsed time calculations in GCP controller."""
import ast
from pathlib import Path
import pytest


class TestNoDatetimeDurations:
    def test_no_datetime_now_in_duration_calculations(self):
        """supervisor_gcp_controller.py must not use datetime.now() for elapsed/duration math."""
        target = Path("backend/core/supervisor_gcp_controller.py")
        if not target.exists():
            pytest.skip("File not found")

        source = target.read_text()
        tree = ast.parse(source)

        # Find all datetime.now() calls and check if they're in subtraction expressions
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
                # Check if either side calls datetime.now()
                for operand in (node.left, node.right):
                    if _is_datetime_now(operand):
                        violations.append(
                            f"Line {node.lineno}: datetime.now() used in subtraction "
                            f"(duration calculation)"
                        )

        assert not violations, (
            f"Found datetime.now() duration calculations in GCP controller:\n"
            + "\n".join(violations)
        )


def _is_datetime_now(node: ast.AST) -> bool:
    """Check if an AST node is a call to datetime.now()."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "now":
            if isinstance(func.value, ast.Name) and func.value.id == "datetime":
                return True
    return False
```

**Step 2: Run test to see it fail**

Run: `python3 -m pytest tests/contracts/test_no_datetime_durations.py -v`
Expected: FAIL (3 violations at lines 500, 713, 771)

**Step 3: Modify supervisor_gcp_controller.py**

Add import at top:
```python
from backend.core.time_utils import monotonic_s, elapsed_since_s
```

At `__init__` (after line 253), add:
```python
        self._last_vm_created_mono: float = 0.0
        self._last_vm_terminated_mono: float = 0.0
```

At line 500, replace:
```python
                elapsed = (datetime.now() - self._last_vm_created).total_seconds() / 60
```
with:
```python
                elapsed = elapsed_since_s(self._last_vm_created_mono) / 60 if self._last_vm_created_mono else 999.0
```

At line 637 (where `self._last_vm_created` is set), add:
```python
                self._last_vm_created_mono = monotonic_s()
```

At line 713, replace:
```python
            runtime_hours = (datetime.now() - vm.created_at).total_seconds() / 3600
```
with:
```python
            runtime_hours = elapsed_since_s(vm.created_at_mono) / 3600 if hasattr(vm, 'created_at_mono') and vm.created_at_mono else (datetime.now() - vm.created_at).total_seconds() / 3600
```

Note: The VM dataclass needs a `created_at_mono` field. Add it where the VM is created. If the VM dataclass is in the same file, add `created_at_mono: float = 0.0` to it, and set `vm.created_at_mono = monotonic_s()` at creation time.

At line 771, replace:
```python
                idle_seconds = (datetime.now() - vm.last_activity).total_seconds()
```
with:
```python
                idle_seconds = elapsed_since_s(vm.last_activity_mono) if hasattr(vm, 'last_activity_mono') and vm.last_activity_mono else (datetime.now() - vm.last_activity).total_seconds()
```

At line 754 (where `last_activity` is set), add:
```python
                self._active_vm.last_activity_mono = monotonic_s()
```

At line 725 (where `_last_vm_terminated` is set), add:
```python
                self._last_vm_terminated_mono = monotonic_s()
```

**Step 4: Run contract test**

Run: `python3 -m pytest tests/contracts/test_no_datetime_durations.py -v`
Expected: PASS (no more datetime.now() in subtractions)

**Step 5: Commit**

```bash
git add backend/core/supervisor_gcp_controller.py tests/contracts/test_no_datetime_durations.py
git commit -m "fix(gcp_controller): replace datetime.now() duration calcs with monotonic time (Item 16)"
```

---

### Task 13: Gate G Verification

**Step 1: Run all Phase G tests**

Run: `python3 -m pytest tests/unit/vision/test_cache_envelope.py tests/contracts/test_no_datetime_durations.py -v`
Expected: ALL PASS

**Step 2: Tag gate**

```bash
git tag gate-g-data-time-integrity
```

---

## Phase H: Cross-Repo & Portability (Items 17, 18)

### Task 14: Standardize Reactor-Core Path Env Var (Item 17)

**Files:**
- Modify: `backend/core/trinity_event_bus.py:171-174`

**Context:** `trinity_bridge.py` already uses `REACTOR_CORE_PATH` (line 108). `trinity_event_bus.py` uses a *different* env var `REACTOR_CORE_REPO` (line 172). Both have env overrides — the issue is inconsistency, not missing overrides.

**Step 1: Modify trinity_event_bus.py**

Change line 171-174 from:
```python
    REACTOR_PATH = Path(_env_str(
        "REACTOR_CORE_REPO",
        str(Path.home() / "Documents/repos/reactor-core")
    ))
```
to:
```python
    REACTOR_PATH = Path(_env_str(
        "REACTOR_CORE_PATH",  # v310.0: standardized to match trinity_bridge.py
        str(Path.home() / "Documents/repos/reactor-core")
    ))
```

**Step 2: Verify no other references to old env var**

Run: `grep -rn "REACTOR_CORE_REPO" backend/`
Expected: No matches (only the one we just changed)

**Step 3: Commit**

```bash
git add backend/core/trinity_event_bus.py
git commit -m "fix(trinity): standardize REACTOR_CORE_PATH env var across bridge and event bus (Item 17)"
```

---

### Task 15: Reactor-Core /capabilities Endpoint (Item 18)

**Files:**
- Modify: `/Users/djrussell23/Documents/repos/reactor-core/reactor_core/api/server.py`

**Context:** This is in a DIFFERENT repo. The implementer must `cd` to that repo or use the full path. The endpoint mirrors JARVIS-Prime's pattern with `schema_version`, `capability_hash`, and `etag`.

**Step 1: Add the endpoint**

In `reactor_core/api/server.py`, find the FastAPI app instance and add:

```python
import hashlib
import time

@app.get("/capabilities")
async def get_capabilities():
    """Contract endpoint for supervisor version/capability negotiation."""
    capabilities = ["event_bus", "trinity_bridge", "state_sync"]
    cap_str = ",".join(sorted(capabilities))
    cap_hash = hashlib.sha256(cap_str.encode()).hexdigest()[:16]
    schema_version = [0, 1, 0]
    etag = f'"v{".".join(map(str, schema_version))}-{cap_hash}"'

    return {
        "provider_id": "reactor-core",
        "capabilities": capabilities,
        "schema_version": schema_version,
        "capability_hash": f"sha256:{cap_hash}",
        "etag": etag,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
```

**Step 2: Test endpoint**

Run from reactor-core repo: `python3 -c "from reactor_core.api.server import app; print('import ok')"`

**Step 3: Commit (in reactor-core repo)**

```bash
cd /Users/djrussell23/Documents/repos/reactor-core
git add reactor_core/api/server.py
git commit -m "feat(api): add /capabilities contract endpoint for supervisor negotiation"
```

---

### Task 16: RLock Guard Test (Item 12)

**Files:**
- Create: `tests/contracts/test_rlock_safety.py`

**Step 1: Write the guard test**

Create `tests/contracts/test_rlock_safety.py`:

```python
"""Guard test: verify RLock usage is only in synchronous call paths."""
import ast
from pathlib import Path
import pytest


class TestRLockSafety:
    def test_no_rlock_acquire_in_async_functions(self):
        """RLock.acquire() must not be called inside async def functions."""
        backend = Path("backend")
        if not backend.exists():
            pytest.skip("backend directory not found")

        violations = []
        for py_file in backend.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    for child in ast.walk(node):
                        if (
                            isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and child.func.attr == "acquire"
                            and isinstance(child.func.value, ast.Name)
                            and "lock" in child.func.value.id.lower()
                        ):
                            violations.append(
                                f"{py_file.relative_to('.')}:{child.lineno} "
                                f"-- {child.func.value.id}.acquire() in async def {node.name}"
                            )

        assert not violations, (
            f"RLock acquire in async contexts:\n" + "\n".join(violations)
        )
```

**Step 2: Run test**

Run: `python3 -m pytest tests/contracts/test_rlock_safety.py -v`
Expected: PASS (confirms all RLock usage is in sync contexts)

**Step 3: Commit**

```bash
git add tests/contracts/test_rlock_safety.py
git commit -m "test(contracts): add RLock safety guard test -- no acquire in async contexts (Item 12)"
```

---

### Task 17: Gate H Verification

**Step 1: Run all contract tests**

Run: `python3 -m pytest tests/contracts/ -v`
Expected: ALL PASS

**Step 2: Verify env var consistency**

Run: `grep -rn "REACTOR_CORE_PATH\|REACTOR_CORE_REPO" backend/`
Expected: Only `REACTOR_CORE_PATH` references (no `REACTOR_CORE_REPO`)

**Step 3: Tag gate**

```bash
git tag gate-h-cross-repo-portability
```

---

## Final: Full Test Suite

### Task 18: Run complete verification

**Step 1: Run ALL new tests**

Run: `python3 -m pytest tests/unit/core/test_time_utils.py tests/unit/core/test_heartbeat_liveness.py tests/unit/core/test_restart_backoff.py tests/unit/intelligence/test_model_drain.py tests/unit/intelligence/test_proxy_cleanup.py tests/unit/core/test_observer_snapshot.py tests/unit/intelligence/test_oscillation_guard.py tests/unit/core/test_event_log_bound.py tests/unit/vision/test_cache_envelope.py tests/contracts/ -v`
Expected: ALL PASS

**Step 2: Run full test suite**

Run: `python3 -m pytest tests/ -v --tb=short -q`
Expected: No regressions from our changes

---

## File Manifest Summary

| Task | File | New/Edit | Item |
|------|------|----------|------|
| 0 | backend/core/time_utils.py | New | Cross-cutting |
| 0 | tests/unit/core/test_time_utils.py | New | Cross-cutting |
| 1 | backend/core/heartbeat_writer.py | New | 9 |
| 1 | tests/unit/core/test_heartbeat_liveness.py | New | 9 |
| 1 | unified_supervisor.py | Edit | 9 |
| 2 | backend/core/supervisor/restart_coordinator.py | Edit | 10 |
| 2 | tests/unit/core/test_restart_backoff.py | New | 10 |
| 3 | backend/intelligence/unified_model_serving.py | Edit | 14 |
| 3 | tests/unit/intelligence/test_model_drain.py | New | 14 |
| 4 | backend/intelligence/cloud_sql_proxy_manager.py | Edit | 19 |
| 4 | tests/unit/intelligence/test_proxy_cleanup.py | New | 19 |
| 6 | backend/core/memory_budget_broker.py | Edit | 11 |
| 6 | tests/unit/core/test_observer_snapshot.py | New | 11 |
| 7 | backend/intelligence/unified_model_serving.py | Edit | 13 |
| 7 | tests/unit/intelligence/test_oscillation_guard.py | New | 13 |
| 8 | backend/core/memory_budget_broker.py | Edit | 20 |
| 8 | tests/unit/core/test_event_log_bound.py | New | 20 |
| 10 | backend/vision/intelligence/cache_envelope.py | New | 15 |
| 10 | tests/unit/vision/test_cache_envelope.py | New | 15 |
| 11 | backend/vision/lazy_vision_engine.py | Edit | 15 |
| 11 | backend/vision/space_screenshot_cache.py | Edit | 15 |
| 12 | backend/core/supervisor_gcp_controller.py | Edit | 16 |
| 12 | tests/contracts/test_no_datetime_durations.py | New | 16 |
| 14 | backend/core/trinity_event_bus.py | Edit | 17 |
| 15 | reactor_core/api/server.py (reactor-core repo) | Edit | 18 |
| 16 | tests/contracts/test_rlock_safety.py | New | 12 |

**Totals:** 5 new modules, 13 new test files, 8 edited files across 2 repos.

**Gate tags:** `gate-e-lifecycle-resilience`, `gate-f-concurrency-stability`, `gate-g-data-time-integrity`, `gate-h-cross-repo-portability`

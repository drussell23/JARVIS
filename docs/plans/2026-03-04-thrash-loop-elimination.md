# Thrash Loop Elimination Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Break the self-reinforcing memory thrashing feedback loop between email triage, model serving, and the memory quantizer.

**Architecture:** Four surgical changes to existing files: (1) memory pressure admission gate in agent_runtime, (2) model-swapping fast-fail in PrimeLocalClient, (3) deadline propagation from runner to extraction, (4) hysteresis exit threshold + public property in memory_quantizer. No new files.

**Tech Stack:** Python asyncio, existing MemoryQuantizer, PrimeLocalClient, EmailTriageRunner

---

### Task 1: Public `thrash_state` Property + Hysteresis Exit Threshold on MemoryQuantizer

**Files:**
- Modify: `backend/core/memory_quantizer.py:451` (add property near `_thrash_state` init)
- Modify: `backend/core/memory_quantizer.py:1370-1381` (hysteresis in `_check_thrash_state`)
- Test: `tests/unit/backend/core/test_memory_quantizer_thrash_hysteresis.py`

**Step 1: Write the failing tests**

Create `tests/unit/backend/core/test_memory_quantizer_thrash_hysteresis.py`:

```python
"""Tests for thrash_state property and hysteresis exit thresholds."""

import asyncio
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


@pytest.fixture
def quantizer():
    """Build a MemoryQuantizer with monitoring disabled."""
    with patch("core.memory_quantizer.psutil"):
        from core.memory_quantizer import MemoryQuantizer
        mq = MemoryQuantizer.__new__(MemoryQuantizer)
        # Minimal init for testing _check_thrash_state
        mq._thrash_state = "healthy"
        mq._thrash_callbacks = []
        mq._recovery_callbacks = []
        mq._thrash_warning_since = 0.0
        mq._thrash_emergency_since = 0.0
        mq._thrash_recovery_since = 0.0
        mq._pagein_rate = 0.0
        mq._pagein_rate_ema = 0.0
        mq.current_metrics = None
        return mq


def test_thrash_state_property_returns_current_state(quantizer):
    """thrash_state property exposes the internal _thrash_state."""
    assert quantizer.thrash_state == "healthy"
    quantizer._thrash_state = "emergency"
    assert quantizer.thrash_state == "emergency"


@pytest.mark.asyncio
async def test_emergency_holds_until_exit_threshold(quantizer):
    """Emergency state should NOT drop to thrashing when rate is above exit threshold."""
    import time
    quantizer._thrash_state = "emergency"
    # Rate below emergency entry (2000) but above exit (2000 * 0.7 = 1400)
    quantizer._pagein_rate = 1600.0
    quantizer._pagein_rate_ema = 1600.0
    await quantizer._check_thrash_state()
    # Should HOLD emergency, not drop to thrashing
    assert quantizer.thrash_state == "emergency"


@pytest.mark.asyncio
async def test_emergency_drops_to_thrashing_below_exit_threshold(quantizer):
    """Emergency state drops to thrashing when rate falls below exit threshold."""
    import time
    quantizer._thrash_state = "emergency"
    # Rate below exit threshold (1400) but above healthy (100)
    quantizer._pagein_rate = 300.0
    quantizer._pagein_rate_ema = 300.0
    await quantizer._check_thrash_state()
    assert quantizer.thrash_state == "thrashing"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/core/test_memory_quantizer_thrash_hysteresis.py -v`

Expected: FAIL — `thrash_state` property doesn't exist, hysteresis test fails

**Step 3: Add the `thrash_state` property**

In `backend/core/memory_quantizer.py`, after line 451 (`self._thrash_state: str = "healthy"`), add a property. Find a suitable location after the `__init__` method ends (around line 486 after "Memory Quantizer initialized"):

```python
@property
def thrash_state(self) -> str:
    """Current thrash state: 'healthy', 'thrashing', or 'emergency'."""
    return self._thrash_state
```

**Step 4: Add hysteresis exit threshold in `_check_thrash_state`**

In `backend/core/memory_quantizer.py`, in the deadband section of `_check_thrash_state()` (~line 1370), replace the `rate > THRASH_PAGEIN_HEALTHY` block:

Current code (lines 1370-1381):
```python
        elif rate > THRASH_PAGEIN_HEALTHY:
            # Deadband: below warning but still above healthy floor.
            # Do not claim full recovery while pageins remain elevated.
            self._thrash_warning_since = 0.0
            self._thrash_emergency_since = 0.0
            self._thrash_recovery_since = 0.0
            if old_state == "emergency":
                new_state = "thrashing"
            elif old_state == "thrashing":
                return
            else:
                new_state = "healthy"
```

Replace with:
```python
        elif rate > THRASH_PAGEIN_HEALTHY:
            # Deadband: below warning but still above healthy floor.
            # Do not claim full recovery while pageins remain elevated.
            self._thrash_warning_since = 0.0
            self._thrash_emergency_since = 0.0
            self._thrash_recovery_since = 0.0
            # Hysteresis: hold emergency until rate drops below exit threshold
            # (70% of entry) to prevent flapping between states.
            _exit_ratio = float(os.environ.get("THRASH_EXIT_RATIO", "0.7"))
            _emergency_exit = THRASH_PAGEIN_EMERGENCY * _exit_ratio
            if old_state == "emergency" and rate >= _emergency_exit:
                return  # Hold emergency state
            elif old_state == "emergency":
                new_state = "thrashing"
            elif old_state == "thrashing":
                return
            else:
                new_state = "healthy"
```

**Step 5: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/core/test_memory_quantizer_thrash_hysteresis.py -v`

Expected: PASS

**Step 6: Commit**

```bash
git add backend/core/memory_quantizer.py tests/unit/backend/core/test_memory_quantizer_thrash_hysteresis.py
git commit -m "feat(memory_quantizer): add thrash_state property and hysteresis exit threshold

Prevents state flapping between emergency/thrashing by holding emergency
until rate drops below 70% of entry threshold. Exposes thrash_state as
public property for admission control by other subsystems.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: `_model_swapping` Fast-Fail Guard in PrimeLocalClient.generate()

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py:1130-1142`
- Test: `tests/unit/backend/test_model_serving_swap_guard.py`

**Step 1: Write the failing test**

Create `tests/unit/backend/test_model_serving_swap_guard.py`:

```python
"""Tests for PrimeLocalClient.generate() model_swapping guard."""

import asyncio
import os
import sys
import time
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest


@pytest.fixture
def prime_local_client():
    """Build a minimal PrimeLocalClient for testing generate()."""
    from intelligence.unified_model_serving import PrimeLocalClient
    client = PrimeLocalClient.__new__(PrimeLocalClient)
    client.logger = MagicMock()
    client._loaded = True
    client._model = MagicMock()
    client._model_path = MagicMock()
    client._model_path.name = "test-model.gguf"
    client._model_swapping = False
    client._inference_executor = None
    client._current_model_entry = {"name": "test", "quality_rank": 1}
    return client


@pytest.mark.asyncio
async def test_generate_fast_fails_during_model_swap(prime_local_client):
    """generate() should return immediately with error when _model_swapping is True."""
    from intelligence.unified_model_serving import ModelRequest
    prime_local_client._model_swapping = True

    request = ModelRequest(
        messages=[{"role": "user", "content": "test"}],
        max_tokens=10,
    )
    response = await prime_local_client.generate(request)

    assert response.success is False
    assert "model_swap_in_progress" in response.error
    assert response.latency_ms < 100  # Must be fast, not blocked


@pytest.mark.asyncio
async def test_generate_proceeds_when_not_swapping(prime_local_client):
    """generate() should proceed normally when _model_swapping is False."""
    from intelligence.unified_model_serving import ModelRequest
    prime_local_client._model_swapping = False
    # Mock the inference path
    mock_result = {"choices": [{"text": "hello"}], "usage": {"total_tokens": 5}}
    prime_local_client._model.return_value = mock_result
    prime_local_client._inference_executor = None

    request = ModelRequest(
        messages=[{"role": "user", "content": "test"}],
        max_tokens=10,
    )

    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(return_value=mock_result)
        response = await prime_local_client.generate(request)

    assert response.success is True
    assert response.content == "hello"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_model_serving_swap_guard.py -v`

Expected: FAIL — no swap guard exists, generate() blocks during swap

**Step 3: Add the `_model_swapping` guard**

In `backend/intelligence/unified_model_serving.py`, in `PrimeLocalClient.generate()` at line 1130, insert the guard after `response` is created (after line 1136) and before the `not self._loaded` check (line 1138):

```python
        # Fast-fail during model swap — don't queue behind the swap operation.
        # Callers (e.g. email triage extraction) catch the failure and fall
        # through to heuristic-only processing.
        if getattr(self, '_model_swapping', False):
            response.success = False
            response.error = "model_swap_in_progress"
            response.latency_ms = (time.time() - start_time) * 1000
            return response
```

Insert between lines 1136 and 1138.

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_model_serving_swap_guard.py -v`

Expected: PASS

**Step 5: Commit**

```bash
git add backend/intelligence/unified_model_serving.py tests/unit/backend/test_model_serving_swap_guard.py
git commit -m "fix(model_serving): fast-fail generate() during model swap

PrimeLocalClient.generate() now returns immediately with
'model_swap_in_progress' error when _model_swapping is True, instead of
queuing on the single-worker executor and stalling behind the swap.
Prevents inference contention during thrash-triggered model downgrades.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Deadline Propagation from Agent Runtime → Runner → Extraction

**Files:**
- Modify: `backend/autonomy/email_triage/runner.py:215,339-341`
- Modify: `backend/autonomy/agent_runtime.py:2837-2839`
- Test: `tests/unit/backend/email_triage/test_deadline_propagation.py`

**Step 1: Write the failing test**

Create `tests/unit/backend/email_triage/test_deadline_propagation.py`:

```python
"""Tests for deadline propagation through triage pipeline."""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.schemas import TriageCycleReport


@pytest.mark.asyncio
async def test_run_cycle_propagates_deadline_to_extract_features():
    """run_cycle(deadline=X) must pass deadline to extract_features()."""
    from autonomy.email_triage.runner import EmailTriageRunner
    from autonomy.email_triage.config import get_triage_config

    config = get_triage_config()
    config.enabled = True
    config.max_emails_per_cycle = 1

    runner = EmailTriageRunner.__new__(EmailTriageRunner)
    runner._config = config
    runner._resolver = MagicMock()
    runner._state_store = None
    runner._label_map = {}
    runner._labels_initialized = True
    runner._fencing_token = 0
    runner._warmed_up = True
    runner._cold_start_done = True
    runner._outcome_collector = MagicMock()
    runner._outcome_collector.record = AsyncMock()
    runner._weight_adapter = None

    # Mock workspace agent to return one email
    mock_workspace = AsyncMock()
    mock_workspace.list_emails = AsyncMock(return_value=[
        {"id": "msg1", "from": "test@example.com", "subject": "Test", "snippet": "hi", "labelIds": []}
    ])
    runner._resolver.get = lambda name: {
        "workspace_agent": mock_workspace,
        "router": MagicMock(),
        "notifier": MagicMock(),
    }.get(name)

    deadline = time.monotonic() + 25.0
    captured_deadline = None

    original_extract = None
    async def mock_extract(email_dict, router, deadline=None, config=None):
        nonlocal captured_deadline
        captured_deadline = deadline
        from autonomy.email_triage.schemas import EmailFeatures
        return EmailFeatures(
            message_id="msg1", sender="test@example.com",
            sender_domain="example.com", subject="Test", snippet="hi",
            is_reply=False, has_attachment=False, label_ids=[],
            keywords=[], sender_frequency=0, urgency_signals=[],
            extraction_confidence=0.5, extraction_source="heuristic",
        )

    with patch("autonomy.email_triage.runner.extract_features", side_effect=mock_extract):
        with patch("autonomy.email_triage.runner.score_email", return_value=MagicMock(tier=3, score=0.5, signals=[])):
            with patch("autonomy.email_triage.runner.apply_label", new_callable=AsyncMock):
                try:
                    await asyncio.wait_for(runner.run_cycle(deadline=deadline), timeout=5.0)
                except Exception:
                    pass  # May fail on other dependencies, that's fine

    assert captured_deadline is not None, "deadline was not propagated to extract_features"
    assert captured_deadline == deadline, f"Expected {deadline}, got {captured_deadline}"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_deadline_propagation.py -v`

Expected: FAIL — `run_cycle()` doesn't accept `deadline` parameter

**Step 3: Add deadline parameter to `run_cycle()`**

In `backend/autonomy/email_triage/runner.py`, modify the `run_cycle` signature at line 215:

Change:
```python
    async def run_cycle(self) -> TriageCycleReport:
```
To:
```python
    async def run_cycle(self, *, deadline: Optional[float] = None) -> TriageCycleReport:
```

Then at line 339-341 where `extract_features` is called, change:

```python
                features = await extract_features(
                    email, self._resolver.get("router"), config=self._config,
                )
```
To:
```python
                features = await extract_features(
                    email, self._resolver.get("router"),
                    deadline=deadline, config=self._config,
                )
```

**Step 4: Pass deadline from agent_runtime**

In `backend/autonomy/agent_runtime.py`, at lines 2837/2839 where `runner.run_cycle()` is called, change both call sites:

Line ~2837:
```python
                        report = await asyncio.wait_for(runner.run_cycle(), timeout=timeout)
```
To:
```python
                        _deadline = time.monotonic() + timeout
                        report = await asyncio.wait_for(runner.run_cycle(deadline=_deadline), timeout=timeout)
```

Line ~2864 (the fail-open path):
```python
                    report = await asyncio.wait_for(runner.run_cycle(), timeout=timeout)
```
To:
```python
                    _deadline = time.monotonic() + timeout
                    report = await asyncio.wait_for(runner.run_cycle(deadline=_deadline), timeout=timeout)
```

**Step 5: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_deadline_propagation.py -v`

Expected: PASS

**Step 6: Commit**

```bash
git add backend/autonomy/email_triage/runner.py backend/autonomy/agent_runtime.py tests/unit/backend/email_triage/test_deadline_propagation.py
git commit -m "fix(email_triage): propagate deadline from agent_runtime through runner to extraction

run_cycle() now accepts a deadline parameter and passes it to
extract_features(), which forwards it to router.generate(). This
activates the existing v280.6 budget-aware timeout in PrimeLocalClient,
preventing unbounded inference from consuming the full 30s triage budget.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Memory Pressure Admission Gate in `_maybe_run_email_triage()`

**Files:**
- Modify: `backend/autonomy/agent_runtime.py:2780-2785`
- Test: `tests/unit/backend/email_triage/test_triage_pressure_gate.py`

**Step 1: Write the failing tests**

Create `tests/unit/backend/email_triage/test_triage_pressure_gate.py`:

```python
"""Tests for memory pressure admission gate in _maybe_run_email_triage."""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


def _build_runtime():
    """Build a minimal UnifiedAgentRuntime for testing _maybe_run_email_triage."""
    from autonomy.agent_runtime import UnifiedAgentRuntime
    rt = UnifiedAgentRuntime.__new__(UnifiedAgentRuntime)
    rt._last_email_triage_run = 0.0
    rt._triage_disabled_logged = False
    rt._triage_pressure_skip_count = 0
    return rt


@pytest.mark.asyncio
async def test_triage_skipped_when_thrashing():
    """Triage must not launch when memory quantizer reports thrashing."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "thrashing"

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            await rt._maybe_run_email_triage()

    # Should have skipped — runner never imported/instantiated
    assert rt._triage_pressure_skip_count == 1


@pytest.mark.asyncio
async def test_triage_skipped_when_emergency():
    """Triage must not launch when memory quantizer reports emergency."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "emergency"

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 1


@pytest.mark.asyncio
async def test_triage_proceeds_when_healthy():
    """Triage should proceed normally when memory is healthy."""
    rt = _build_runtime()

    mock_mq = MagicMock()
    mock_mq.thrash_state = "healthy"

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            # Will fail on runner import, but that proves it got past the gate
            with pytest.raises(Exception):
                await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 0


@pytest.mark.asyncio
async def test_consecutive_skips_increase_backoff():
    """Each consecutive skip should increase the backoff interval."""
    rt = _build_runtime()
    rt._triage_pressure_skip_count = 3  # Already skipped 3 times

    mock_mq = MagicMock()
    mock_mq.thrash_state = "emergency"

    before_run = rt._last_email_triage_run

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 4
    # Backoff should have pushed _last_email_triage_run forward
    assert rt._last_email_triage_run != before_run


@pytest.mark.asyncio
async def test_drift_guard_disables_extraction_after_5_skips():
    """After 5 consecutive pressure blocks, extraction should be auto-disabled."""
    rt = _build_runtime()
    rt._triage_pressure_skip_count = 4  # Next skip will be 5th

    mock_mq = MagicMock()
    mock_mq.thrash_state = "emergency"

    # Clear any existing env var
    os.environ.pop("EMAIL_TRIAGE_EXTRACTION_ENABLED", None)

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 5
    assert os.environ.get("EMAIL_TRIAGE_EXTRACTION_ENABLED") == "false"

    # Cleanup
    os.environ.pop("EMAIL_TRIAGE_EXTRACTION_ENABLED", None)


@pytest.mark.asyncio
async def test_skip_count_resets_on_healthy():
    """Skip count should reset to 0 when memory returns to healthy."""
    rt = _build_runtime()
    rt._triage_pressure_skip_count = 3

    mock_mq = MagicMock()
    mock_mq.thrash_state = "healthy"

    with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
        with patch(
            "core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            # Will fail on runner import, but proves it got past the gate
            with pytest.raises(Exception):
                await rt._maybe_run_email_triage()

    assert rt._triage_pressure_skip_count == 0
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_triage_pressure_gate.py -v`

Expected: FAIL — no pressure gate exists, `_triage_pressure_skip_count` not initialized

**Step 3: Add `_triage_pressure_skip_count` to `__init__`**

Find the `__init__` method of `UnifiedAgentRuntime` and add near the existing `_last_email_triage_run` initialization:

```python
        self._triage_pressure_skip_count: int = 0
```

**Step 4: Add the memory pressure gate**

In `backend/autonomy/agent_runtime.py`, in `_maybe_run_email_triage()`, insert after line 2785 (`self._last_email_triage_run = now`) and before line 2786 (`try:`):

```python
        # ── Memory pressure admission gate ──────────────────────────
        # Hard gate: refuse to launch triage when system is thrashing or
        # in emergency. Prevents the feedback loop where email triage
        # inference triggers mmap page faults → thrash detection → model
        # swap → inference stalls → triage timeout → retry → repeat.
        try:
            from core.memory_quantizer import get_memory_quantizer_instance
            _mq = get_memory_quantizer_instance()
            if _mq:
                _thrash = getattr(_mq, 'thrash_state', 'healthy')
                if _thrash in ('thrashing', 'emergency'):
                    self._triage_pressure_skip_count += 1
                    # Exponential backoff with jitter: 60s, 120s, 240s... cap 600s
                    import random
                    _base_interval = interval
                    _exp = min(self._triage_pressure_skip_count - 1, 4)
                    _backoff = min(600.0, _base_interval * (2 ** _exp))
                    _backoff *= (0.8 + 0.4 * random.random())  # ±20% jitter
                    self._last_email_triage_run = now - interval + _backoff
                    logger.info(
                        "[AgentRuntime] Email triage deferred: memory_state=%s, "
                        "consecutive_skips=%d, next_attempt_in=%.0fs",
                        _thrash, self._triage_pressure_skip_count, _backoff,
                    )
                    # Drift guard: auto-disable extraction after 5 consecutive blocks
                    if self._triage_pressure_skip_count >= 5:
                        os.environ.setdefault('EMAIL_TRIAGE_EXTRACTION_ENABLED', 'false')
                        logger.warning(
                            "[AgentRuntime] Drift guard: extraction auto-disabled after %d "
                            "consecutive memory pressure blocks",
                            self._triage_pressure_skip_count,
                        )
                    return
                else:
                    self._triage_pressure_skip_count = 0
        except Exception:
            pass  # Gate import failure = proceed (fail-open on gate itself)

```

**Step 5: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_triage_pressure_gate.py -v`

Expected: PASS

**Step 6: Run existing triage integration tests for regression**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_agent_runtime_integration.py -v`

Expected: PASS (existing tests unaffected — they don't mock memory quantizer, so gate falls through via import exception)

**Step 7: Commit**

```bash
git add backend/autonomy/agent_runtime.py tests/unit/backend/email_triage/test_triage_pressure_gate.py
git commit -m "fix(agent_runtime): add memory pressure admission gate to email triage

_maybe_run_email_triage() now checks MemoryQuantizer.thrash_state before
launching. Under thrashing/emergency: skips with exponential backoff +
jitter. After 5 consecutive blocks, auto-disables extraction (the
expensive model call) via drift guard. Breaks the self-reinforcing
thrash loop at its entry point.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Integration Verification

**Files:**
- No new files — verify the 4 changes work together

**Step 1: Run all new tests together**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/core/test_memory_quantizer_thrash_hysteresis.py tests/unit/backend/test_model_serving_swap_guard.py tests/unit/backend/email_triage/test_deadline_propagation.py tests/unit/backend/email_triage/test_triage_pressure_gate.py -v`

Expected: ALL PASS

**Step 2: Run existing related test suites for regression**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ tests/unit/backend/core/test_memory_quantizer_admission_snapshot.py tests/unit/backend/core/test_memory_quantizer_recovery_callbacks.py tests/unit/backend/test_unified_model_serving_memory_recovery.py -v`

Expected: ALL PASS

**Step 3: Verify the causal chain is broken**

Manual verification checklist:
- [ ] When `thrash_state == "emergency"`, `_maybe_run_email_triage` returns immediately (gate blocks)
- [ ] When `_model_swapping == True`, `PrimeLocalClient.generate()` returns `model_swap_in_progress` in <10ms
- [ ] When deadline is set, `PrimeLocalClient.generate()` uses `asyncio.wait_for` with remaining budget
- [ ] When rate drops from 2000 to 1500 (above 70% exit), emergency state holds (no flapping)
- [ ] After 5 consecutive pressure-blocked triage cycles, extraction is auto-disabled
- [ ] All gate decisions are logged with machine-parseable reason codes

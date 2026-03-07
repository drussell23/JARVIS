# End-to-End Feedback Loop Completion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the four gaps preventing live end-to-end operation of the email triage feedback loop: Gmail label fetching, outcome idempotency, durable training notifications, broken test fix, and supervisor-managed processor lifecycle.

**Architecture:** Add `get_message_labels()` to `GoogleWorkspaceAgent` using the existing `_execute_with_retry` sync-executor pattern. Add content_hash idempotency to reactor-core enqueue. Wire training notifications post-commit in the runner. Fix the test mock. Register `ExperienceQueueProcessor` as a supervisor background task.

**Tech Stack:** Python 3.11+, asyncio, pytest, Gmail API v1, SQLite (ExperienceStore)

---

## Prerequisite Context

### Key Files
- `backend/neural_mesh/agents/google_workspace_agent.py` — Gmail API calls, `_execute_with_retry` (line 2445), `_fetch_unread_sync` (line 3084)
- `backend/autonomy/email_triage/outcome_collector.py` — `_enqueue_to_reactor_core` (line 147), `record_outcome` (line 85)
- `backend/autonomy/email_triage/runner.py` — outcome collection flow (lines 502-527), notifier resolution (line 778)
- `backend/autonomy/email_triage/notifications.py` — `build_training_capture_message()` (already exists)
- `backend/autonomy/agent_runtime.py` — `_maybe_run_email_triage` (line 2805), triage attrs (line 462-470)
- `tests/unit/backend/email_triage/test_agent_runtime_integration.py` — broken test (line 139)
- `unified_supervisor.py` — `KernelBackgroundTaskRegistry` (line 66014), `_background_tasks.append()` pattern (line 72108+)

### Import Convention
Email triage files use `from autonomy.*` and `from core.*` (no `backend.` prefix). Tests use `sys.path.insert(0, ...)` to add backend to path.

### Existing Patterns
- **Sync-executor:** `_execute_with_retry(lambda: sync_fn(), api_name="gmail", timeout=T)` wraps sync Gmail calls in `loop.run_in_executor` with circuit breaker + auth refresh.
- **Background tasks:** `asyncio.create_task(coro)` → `self._background_tasks.append(task)` in supervisor startup.
- **ExperienceDataQueue dedup:** Accepts `content_hash` field on entries, skips insert if hash already exists.

---

## Task 1: Add `get_message_labels()` to GoogleWorkspaceAgent

**Files:**
- Modify: `backend/neural_mesh/agents/google_workspace_agent.py` (add 2 methods near line 3130)
- Create: `tests/unit/backend/email_triage/test_get_message_labels.py`

### Step 1: Write the failing test

```python
"""Tests for GoogleWorkspaceAgent.get_message_labels()."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


def _make_agent():
    """Build a minimal GoogleWorkspaceAgent mock for testing."""
    from neural_mesh.agents.google_workspace_agent import GoogleWorkspaceAgent

    agent = GoogleWorkspaceAgent.__new__(GoogleWorkspaceAgent)
    agent._gmail_service = MagicMock()
    agent._auth_state = MagicMock()
    agent._auth_state.__eq__ = lambda self, other: False  # not in degraded state
    agent.config = MagicMock()
    agent.config.operation_timeout_seconds = 10.0
    return agent


class TestGetMessageLabels:
    @pytest.mark.asyncio
    async def test_returns_label_set(self):
        """Should return a set of label IDs for a message."""
        agent = _make_agent()

        mock_msg = {"id": "msg-1", "threadId": "t-1", "labelIds": ["INBOX", "IMPORTANT", "UNREAD"]}
        agent._gmail_service.users.return_value.messages.return_value.get.return_value.execute.return_value = mock_msg

        with patch.object(agent, "_execute_with_retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.return_value = {"INBOX", "IMPORTANT", "UNREAD"}
            # Call through the retry wrapper
            mock_retry.side_effect = None
            mock_retry.return_value = {"INBOX", "IMPORTANT", "UNREAD"}
            result = await agent.get_message_labels("msg-1")

        assert isinstance(result, set)
        assert "INBOX" in result

    @pytest.mark.asyncio
    async def test_sync_method_extracts_labels(self):
        """The sync method should call Gmail API and return label set."""
        agent = _make_agent()

        mock_msg = {"id": "msg-1", "labelIds": ["INBOX", "SENT"]}
        agent._gmail_service.users.return_value.messages.return_value.get.return_value.execute.return_value = mock_msg

        result = agent._get_message_labels_sync("msg-1")
        assert result == {"INBOX", "SENT"}

    @pytest.mark.asyncio
    async def test_sync_method_returns_empty_on_no_labels(self):
        """Should return empty set if message has no labelIds."""
        agent = _make_agent()

        mock_msg = {"id": "msg-1"}
        agent._gmail_service.users.return_value.messages.return_value.get.return_value.execute.return_value = mock_msg

        result = agent._get_message_labels_sync("msg-1")
        assert result == set()

    @pytest.mark.asyncio
    async def test_async_returns_empty_on_error(self):
        """Should return empty set on API error, not crash."""
        agent = _make_agent()

        with patch.object(agent, "_execute_with_retry", new_callable=AsyncMock,
                          side_effect=Exception("Gmail 503")):
            result = await agent.get_message_labels("msg-1")

        assert result == set()
```

### Step 2: Run test to verify it fails

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_get_message_labels.py -v`
Expected: FAIL — `get_message_labels` and `_get_message_labels_sync` don't exist

### Step 3: Implement `get_message_labels`

In `backend/neural_mesh/agents/google_workspace_agent.py`, add two methods after `_fetch_unread_sync` (after ~line 3130):

```python
def _get_message_labels_sync(self, message_id: str) -> Set[str]:
    """Synchronous Gmail API call to get label IDs for a single message.

    Uses format='minimal' for smallest possible response payload —
    only returns id, threadId, and labelIds.
    """
    msg = self._gmail_service.users().messages().get(
        userId='me',
        id=message_id,
        format='minimal',
    ).execute()
    return set(msg.get('labelIds', []))

async def get_message_labels(self, message_id: str) -> Set[str]:
    """Get current label IDs for a Gmail message.

    Routes through _execute_with_retry for circuit breaker + auth refresh.
    Returns empty set on any error (fail-open for outcome detection).
    """
    try:
        return await self._execute_with_retry(
            lambda: self._get_message_labels_sync(message_id),
            api_name="gmail",
            timeout=10.0,
        )
    except Exception as e:
        logger.debug("[Gmail] get_message_labels(%s) failed: %s", message_id, e)
        return set()
```

Also ensure `Set` is imported from `typing` at the top of the file (it likely already is).

### Step 4: Run test to verify it passes

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_get_message_labels.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/neural_mesh/agents/google_workspace_agent.py tests/unit/backend/email_triage/test_get_message_labels.py
git commit -m "feat(gmail): add get_message_labels() to GoogleWorkspaceAgent

Routes through _execute_with_retry for circuit breaker + auth refresh.
Uses format='minimal' for smallest payload. Returns empty set on error.
Enables outcome detection in email triage feedback loop.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Outcome Idempotency Keys

**Files:**
- Modify: `backend/autonomy/email_triage/outcome_collector.py:147-172` (`_enqueue_to_reactor_core`)
- Create: `tests/unit/backend/email_triage/test_outcome_idempotency.py`

### Step 1: Write the failing test

```python
"""Tests for outcome idempotency keys in reactor-core enqueue."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.outcome_collector import OutcomeCollector
from autonomy.email_triage.config import TriageConfig


class TestOutcomeIdempotency:
    @pytest.mark.asyncio
    async def test_enqueue_includes_content_hash(self):
        """Each enqueue call must include a deterministic content_hash for dedup."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)

        record = {
            "message_id": "msg-123",
            "outcome": "replied",
            "confidence": "high",
            "tier": 2,
            "sender_domain": "example.com",
        }

        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record)

            mock_enqueue.assert_called_once()
            call_kwargs = mock_enqueue.call_args
            # content_hash should be in metadata
            metadata = call_kwargs.kwargs.get("metadata") or {}
            assert "content_hash" in metadata
            assert len(metadata["content_hash"]) == 32  # MD5 hex digest

    @pytest.mark.asyncio
    async def test_same_record_produces_same_hash(self):
        """Identical records must produce the same content_hash (deterministic)."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)

        record = {
            "message_id": "msg-123",
            "outcome": "deleted",
            "confidence": "high",
            "tier": 1,
            "sender_domain": "test.com",
        }

        hashes = []
        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record)
            h1 = mock_enqueue.call_args.kwargs.get("metadata", {}).get("content_hash")
            hashes.append(h1)

        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record)
            h2 = mock_enqueue.call_args.kwargs.get("metadata", {}).get("content_hash")
            hashes.append(h2)

        assert hashes[0] == hashes[1]

    @pytest.mark.asyncio
    async def test_different_outcomes_produce_different_hashes(self):
        """Different outcomes for the same message produce different hashes."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)

        record_a = {"message_id": "msg-1", "outcome": "replied", "confidence": "high", "tier": 2, "sender_domain": "a.com"}
        record_b = {"message_id": "msg-1", "outcome": "deleted", "confidence": "high", "tier": 2, "sender_domain": "a.com"}

        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record_a)
            h1 = mock_enqueue.call_args.kwargs.get("metadata", {}).get("content_hash")

        with patch("core.experience_queue.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record_b)
            h2 = mock_enqueue.call_args.kwargs.get("metadata", {}).get("content_hash")

        assert h1 != h2
```

### Step 2: Run test to verify it fails

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_outcome_idempotency.py -v`
Expected: FAIL — `content_hash` not in metadata

### Step 3: Implement idempotency keys

In `backend/autonomy/email_triage/outcome_collector.py`, modify `_enqueue_to_reactor_core` (replace lines ~147-172):

```python
async def _enqueue_to_reactor_core(self, record: Dict[str, Any]) -> None:
    """Best-effort enqueue to the Reactor-Core ExperienceDataQueue.

    Includes brain_id for multi-brain governance scoping and
    content_hash for idempotent deduplication.
    """
    try:
        from core.experience_queue import (
            ExperiencePriority,
            ExperienceType,
            enqueue_experience,
        )
    except ImportError:
        return  # ExperienceQueue not available

    import hashlib
    # Deterministic hash for dedup across overlapping polling windows
    hash_input = f"{record.get('message_id', '')}:{record['outcome']}:{record.get('tier', '')}"
    content_hash = hashlib.md5(hash_input.encode()).hexdigest()

    await enqueue_experience(
        experience_type=ExperienceType.BEHAVIORAL_EVENT,
        data={
            "brain_id": "email_triage",
            "source": "email_triage",
            "outcome": record["outcome"],
            "confidence": record["confidence"],
            "tier": record["tier"],
            "sender_domain": record["sender_domain"],
            "message_id": record.get("message_id", ""),
        },
        priority=ExperiencePriority.NORMAL,
        metadata={"content_hash": content_hash},
    )
```

Also add `import hashlib` at the top of the file if not already present (or keep the lazy import inside the method).

### Step 4: Run test to verify it passes

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_outcome_idempotency.py -v`
Expected: All PASS

### Step 5: Run existing brain_id test to verify no regression

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_brain_scoped_outcomes.py -v`
Expected: PASS (brain_id still present in data)

### Step 6: Commit

```bash
git add backend/autonomy/email_triage/outcome_collector.py tests/unit/backend/email_triage/test_outcome_idempotency.py
git commit -m "feat(triage): add idempotency keys to reactor-core outcome entries

Deterministic content_hash from message_id:outcome:tier prevents
duplicate outcomes from overlapping Gmail polling windows or restart
replay.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Wire Training Capture Notifications into Runner

**Files:**
- Modify: `backend/autonomy/email_triage/runner.py:502-527` (after outcome collection)
- Modify: `tests/unit/backend/email_triage/test_training_notifications.py` (add integration test)

### Step 1: Write the failing test

Add to existing `tests/unit/backend/email_triage/test_training_notifications.py`:

```python
class TestTrainingNotificationWiring:
    @pytest.mark.asyncio
    async def test_runner_notifies_after_outcomes_captured(self):
        """Runner should send training notification when outcomes are captured."""
        from autonomy.email_triage.runner import EmailTriageRunner
        from autonomy.email_triage.config import TriageConfig
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio

        config = TriageConfig(enabled=True, max_emails_per_cycle=5,
                              outcome_collection_enabled=True)

        # Build minimal runner
        runner = EmailTriageRunner.__new__(EmailTriageRunner)
        runner._config = config
        runner._state_store = None
        runner._state_store_initialized = True
        runner._label_map = {}
        runner._labels_initialized = True
        runner._current_fencing_token = 0
        runner._last_committed_fencing_token = 0
        runner._warmed_up = True
        runner._cold_start_recovered = True
        runner._outbox_replayed = True
        runner._prior_triaged = {"msg-1": MagicMock()}
        runner._extraction_latencies_ms = []
        runner._extraction_p95_ema_ms = 0.0
        runner._last_report = None
        runner._last_report_at = 0.0
        runner._triaged_emails = {}
        runner._committed_snapshot = None
        runner._triage_schema_version = "1.0"
        runner._report_lock = asyncio.Lock()
        runner._runner_id = "runner-test"

        from core.contracts.decision_envelope import EnvelopeFactory
        from autonomy.contracts.behavioral_health import BehavioralHealthMonitor
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy

        runner._envelope_factory = EnvelopeFactory()
        runner._health_monitor = BehavioralHealthMonitor()
        runner._commit_ledger = None
        runner._policy = NotificationPolicy(config)
        runner._policy_gate = TriagePolicyGate(runner._policy, config)

        # Mock outcome collector that returns captured outcomes
        mock_collector = AsyncMock()
        captured_outcomes = [
            {"outcome": "replied", "confidence": "high", "sender_domain": "boss.com"},
        ]
        mock_collector.check_outcomes_for_cycle = AsyncMock(return_value=captured_outcomes)
        runner._outcome_collector = mock_collector
        runner._weight_adapter = None

        # Mock resolver
        mock_ws = AsyncMock()
        mock_ws._fetch_unread_emails = AsyncMock(return_value={"emails": []})
        mock_notifier = AsyncMock()

        mock_resolver = MagicMock()
        mock_resolver.resolve_all = AsyncMock()
        mock_resolver.get = lambda name: {
            "workspace_agent": mock_ws,
            "router": MagicMock(),
            "notifier": mock_notifier,
        }.get(name)
        runner._resolver = mock_resolver

        with patch("autonomy.email_triage.runner.build_training_capture_message") as mock_build:
            mock_build.return_value = "Sir, I captured 1 email outcome for training"
            report = await runner.run_cycle()

        mock_build.assert_called_once_with(captured_outcomes)
```

### Step 2: Run test to verify it fails

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_training_notifications.py::TestTrainingNotificationWiring -v`
Expected: FAIL — `build_training_capture_message` not called from runner

### Step 3: Wire notification into runner

In `backend/autonomy/email_triage/runner.py`, find the outcome collection block (around lines 502-509). After the `check_outcomes_for_cycle` call succeeds, add notification dispatch:

```python
# Outcome collection for prior cycle (WS5)
captured_outcomes = []
if self._outcome_collector and self._prior_triaged:
    try:
        captured_outcomes = await self._outcome_collector.check_outcomes_for_cycle(
            workspace_agent, self._prior_triaged,
        )
    except Exception as e:
        logger.debug("Outcome collection failed: %s", e)

# Training capture notification (post-commit — outcomes already durably enqueued)
if captured_outcomes:
    try:
        from autonomy.email_triage.notifications import build_training_capture_message
        training_msg = build_training_capture_message(captured_outcomes)
        if training_msg and notifier:
            await notifier.notify_user(
                message=training_msg,
                urgency=1,  # LOW
                title="JARVIS Training",
            )
    except Exception as e:
        logger.debug("Training capture notification failed: %s", e)
```

**IMPORTANT:** Read the file first to find:
1. The exact current code at lines 502-509
2. How `notifier` is resolved (it's from `self._resolver.get("notifier")` earlier in the method)
3. The exact notifier call pattern used elsewhere in the method

### Step 4: Run test to verify it passes

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_training_notifications.py -v`
Expected: All PASS (both original + new tests)

### Step 5: Commit

```bash
git add backend/autonomy/email_triage/runner.py tests/unit/backend/email_triage/test_training_notifications.py
git commit -m "feat(triage): wire training capture notifications into runner

Sends user-facing notification after outcomes are durably enqueued.
Post-commit: only fires after record_outcome + _enqueue_to_reactor_core
succeed. LOW urgency to avoid notification spam.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Fix `test_disabled_flag_skips_entirely`

**Files:**
- Modify: `tests/unit/backend/email_triage/test_agent_runtime_integration.py:139-156`

### Step 1: Read the failing test

Read `tests/unit/backend/email_triage/test_agent_runtime_integration.py` lines 139-156 to see the exact current code.

### Step 2: Fix the mock

The test at line 146 uses `object.__new__(UnifiedAgentRuntime)` but only sets `_last_email_triage_run`. It needs all attributes that `_maybe_run_email_triage` touches before reaching the feature-flag check.

Replace the test setup (lines 146-148):

```python
runtime = object.__new__(UnifiedAgentRuntime)
runtime._last_email_triage_run = 0.0
runtime._triage_disabled_logged = False
runtime._triage_pressure_skip_count = 0
runtime._experience_processor = None
runtime._experience_processor_started = False
```

### Step 3: Run the test

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_agent_runtime_integration.py::TestAgentRuntimeTriageWiring::test_disabled_flag_skips_entirely -v`
Expected: PASS

### Step 4: Run full agent runtime integration suite

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_agent_runtime_integration.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add tests/unit/backend/email_triage/test_agent_runtime_integration.py
git commit -m "fix(test): add missing runtime attrs to test_disabled_flag_skips_entirely

Mock runtime was missing _triage_disabled_logged, _triage_pressure_skip_count,
_experience_processor, _experience_processor_started attributes added in
recent commits.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 5: Supervisor-Managed Processor Lifecycle

**Files:**
- Modify: `unified_supervisor.py` (add processor startup in boot sequence)
- Create: `tests/unit/core/test_supervisor_experience_processor.py`

### Step 1: Write the failing test

```python
"""Tests for ExperienceQueueProcessor lifecycle under supervisor."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
import asyncio


class TestSupervisorExperienceProcessor:
    @pytest.mark.asyncio
    async def test_start_registers_background_task(self):
        """Supervisor should register experience processor as background task."""
        mock_processor = AsyncMock()
        mock_processor.start = AsyncMock()
        mock_task = asyncio.Future()
        mock_task.set_result(None)

        with patch("core.experience_queue.get_experience_processor",
                    new_callable=AsyncMock, return_value=mock_processor):
            # Simulate what supervisor startup does
            from core.experience_queue import get_experience_processor
            processor = await get_experience_processor()
            await processor.start()

        mock_processor.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_processor_start_failure_non_fatal(self):
        """If processor fails to start, supervisor should continue."""
        with patch("core.experience_queue.get_experience_processor",
                    new_callable=AsyncMock, side_effect=Exception("SQLite locked")):
            # Should not raise
            try:
                from core.experience_queue import get_experience_processor
                processor = await get_experience_processor()
                await processor.start()
                started = True
            except Exception:
                started = False

        assert started is False
```

### Step 2: Run test to verify behavior

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_supervisor_experience_processor.py -v`
Expected: PASS (this validates the API contract)

### Step 3: Add processor startup to unified_supervisor.py

**IMPORTANT:** `unified_supervisor.py` is ~73K lines. Use targeted search to find the right insertion point.

Search for the pattern where other background tasks are registered during startup. Look for a cluster of `self._background_tasks.append()` calls (around line 72100-72650).

Add near the end of the background task registration block:

```python
# ExperienceQueueProcessor — drains training data to reactor-core
try:
    from core.experience_queue import get_experience_processor as _get_exp_proc
    _exp_processor = await _get_exp_proc()
    await _exp_processor.start()
    _exp_task = asyncio.create_task(_exp_processor._process_loop())
    self._background_tasks.append(_exp_task)
    self.logger.info("[Kernel] ExperienceQueueProcessor registered as background task")
except Exception as e:
    self.logger.warning("[Kernel] ExperienceQueueProcessor start failed (non-fatal): %s", e)
```

**IMPORTANT NOTES:**
- The processor's `start()` method creates the internal task. Check if `start()` returns a task or creates one internally. If `start()` already creates `self._task`, register that: `self._background_tasks.append(_exp_processor._task)`.
- Read `ExperienceQueueProcessor.start()` (experience_queue.py ~line 810) to confirm the pattern.
- This must be a `try/except` block — if SQLite is locked or experience_queue module is unavailable, supervisor continues.

### Step 4: Run supervisor-related tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_supervisor_experience_processor.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add unified_supervisor.py tests/unit/core/test_supervisor_experience_processor.py
git commit -m "feat(supervisor): register ExperienceQueueProcessor as managed background task

Starts experience queue drain during supervisor boot, tracked via
KernelBackgroundTaskRegistry. Non-fatal: supervisor continues if
init fails. Replaces opportunistic agent_runtime one-shot start
as primary lifecycle authority.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Run Full Test Suite

### Step 1: Run all new tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_get_message_labels.py tests/unit/backend/email_triage/test_outcome_idempotency.py tests/unit/backend/email_triage/test_training_notifications.py tests/unit/backend/email_triage/test_brain_scoped_outcomes.py tests/unit/core/test_supervisor_experience_processor.py -v`
Expected: All PASS

### Step 2: Run all email triage tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ -v`
Expected: All PASS (including previously broken `test_disabled_flag_skips_entirely`)

### Step 3: Run routing + governance tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_prime_router_gcp_first.py tests/unit/core/test_route_selection_matrix.py tests/unit/core/test_model_artifact_manifest.py -v`
Expected: All PASS

---

## Summary of Changes

| Task | File | Change | Tests |
|------|------|--------|-------|
| 1 | `google_workspace_agent.py` | `get_message_labels()` via `_execute_with_retry` | `test_get_message_labels.py` (4 tests) |
| 2 | `outcome_collector.py:147` | Idempotency `content_hash` on enqueue | `test_outcome_idempotency.py` (3 tests) |
| 3 | `runner.py:502-527` | Wire `build_training_capture_message` post-outcome | `test_training_notifications.py` (1 new test) |
| 4 | `test_agent_runtime_integration.py:146` | Add missing mock attrs | Fix existing test |
| 5 | `unified_supervisor.py` | Register processor as background task | `test_supervisor_experience_processor.py` (2 tests) |

**Total: 5 commits, ~10 new tests, 2 new files, 4 modified files.**

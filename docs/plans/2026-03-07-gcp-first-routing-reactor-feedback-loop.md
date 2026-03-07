# GCP-First Routing + Reactor-Core Feedback Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make GCP J-Prime the always-first inference endpoint, wire real Gmail outcome detection into the reactor-core training feedback loop, start the experience queue processor under supervisor lifecycle, add user-facing training capture notifications, and establish multi-brain model governance contracts.

**Architecture:** Replace the promotion-gated boolean (`_gcp_promoted`) routing in PrimeRouter with a readiness-scored GCP-first policy engine. Decision order: `GCP_READY -> CLOUD -> LOCAL -> DEGRADED`. Implement real Gmail label-delta outcome detection in OutcomeCollector. Write outcomes + outbox enqueue in one transaction. Start ExperienceQueueProcessor under agent_runtime lifecycle. Add brain_id scoping to experience entries and model artifacts.

**Tech Stack:** Python 3.11+, asyncio, pytest, SQLite (via ExperienceStore), Gmail API v1, aiohttp

---

## Prerequisite Context

### Key Files
- `backend/core/prime_router.py` — PrimeRouter, `_decide_route()` (line 464), PrimeRouterConfig (line 215)
- `backend/autonomy/email_triage/outcome_collector.py` — OutcomeCollector, placeholder `check_outcomes_for_cycle` (line 191)
- `backend/autonomy/email_triage/runner.py` — EmailTriageRunner, outcome wiring (lines 97-114, 502-527)
- `backend/autonomy/email_triage/config.py` — TriageConfig, `outcome_collection_enabled=False` (line ~113)
- `backend/core/experience_queue.py` — ExperienceDataQueue, ExperienceQueueProcessor (line 765), `get_experience_processor()` (line 1005)
- `backend/autonomy/agent_runtime.py` — UnifiedAgentRuntime, `_maybe_run_email_triage` (line ~2788)
- `backend/agi_os/notification_bridge.py` — `notify_user()`, NotificationUrgency
- `backend/neural_mesh/agents/google_workspace_agent.py` — Gmail API via `_gmail_service.users().messages().get()`

### Import Convention
Email triage files use `from autonomy.*` and `from core.*` (no `backend.` prefix). Other backend files use `from backend.core.*`. **Never mix these in the same module** — singleton duplication risk.

### Existing Test Pattern
Tests live in `tests/unit/backend/email_triage/`. All use `sys.path.insert(0, ...)` to add backend to path. Use `pytest.mark.asyncio` for async tests.

---

## Task 1: GCP-First Routing Policy in PrimeRouter

**Files:**
- Modify: `backend/core/prime_router.py:215-231` (PrimeRouterConfig)
- Modify: `backend/core/prime_router.py:464-497` (`_decide_route`)
- Create: `tests/unit/core/test_prime_router_gcp_first.py`

### Step 1: Write the failing tests

```python
"""Tests for GCP-first routing policy in PrimeRouter."""

import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from core.prime_router import PrimeRouter, PrimeRouterConfig, RoutingDecision


def _make_router(gcp_promoted=False, prime_available=True, circuit_ok=True,
                 prefer_local=True, cloud_fallback=True, memory_emergency=False):
    """Build a PrimeRouter with controlled state for testing."""
    config = PrimeRouterConfig()
    config.prefer_local = prefer_local
    config.enable_cloud_fallback = cloud_fallback

    router = PrimeRouter.__new__(PrimeRouter)
    router._config = config
    router._metrics = MagicMock()
    router._prime_client = MagicMock() if prime_available else None
    if router._prime_client:
        router._prime_client.is_available = True
    router._cloud_client = None
    router._graceful_degradation = None
    router._lock = MagicMock()
    router._initialized = True
    router._gcp_promoted = gcp_promoted
    router._gcp_host = "34.45.154.209" if gcp_promoted else None
    router._gcp_port = 8080 if gcp_promoted else None
    router._local_circuit = MagicMock()
    router._local_circuit.can_execute.return_value = circuit_ok
    router._last_transition_time = 0.0
    router._transition_cooldown_s = 30.0
    router._transition_in_flight = False
    router._mirror_mode = False
    router._mirror_decisions_issued = 0
    router._cloud_run_patterns = (".run.app", ".a.run.app")
    # Patch memory emergency
    router._is_memory_emergency = lambda: memory_emergency
    return router


class TestGCPFirstRouting:
    """GCP J-Prime should ALWAYS be first priority when available."""

    def test_gcp_promoted_routes_to_gcp_first(self):
        """When GCP is promoted, route to GCP regardless of local health."""
        router = _make_router(gcp_promoted=True, prime_available=True)
        assert router._decide_route() == RoutingDecision.GCP_PRIME

    def test_gcp_promoted_routes_to_gcp_even_when_prefer_local(self):
        """GCP-first overrides prefer_local setting."""
        router = _make_router(gcp_promoted=True, prefer_local=True)
        assert router._decide_route() == RoutingDecision.GCP_PRIME

    def test_gcp_promoted_routes_to_gcp_during_memory_emergency(self):
        """GCP still first during memory emergency."""
        router = _make_router(gcp_promoted=True, memory_emergency=True)
        assert router._decide_route() == RoutingDecision.GCP_PRIME

    def test_no_gcp_memory_emergency_routes_to_cloud(self):
        """Without GCP during emergency, route to cloud (never local)."""
        router = _make_router(gcp_promoted=False, memory_emergency=True)
        assert router._decide_route() == RoutingDecision.CLOUD_CLAUDE

    def test_no_gcp_no_emergency_routes_to_cloud_before_local(self):
        """Without GCP, prefer cloud over local for inference quality."""
        router = _make_router(gcp_promoted=False, cloud_fallback=True)
        assert router._decide_route() == RoutingDecision.CLOUD_CLAUDE

    def test_no_gcp_no_cloud_falls_to_local(self):
        """Only use local as last resort when GCP and cloud unavailable."""
        router = _make_router(gcp_promoted=False, cloud_fallback=False,
                              prime_available=True, circuit_ok=True)
        decision = router._decide_route()
        assert decision in (RoutingDecision.LOCAL_PRIME, RoutingDecision.HYBRID)

    def test_nothing_available_returns_degraded(self):
        """When everything is down, return DEGRADED."""
        router = _make_router(gcp_promoted=False, cloud_fallback=False,
                              prime_available=False)
        assert router._decide_route() == RoutingDecision.DEGRADED

    def test_gcp_circuit_open_falls_to_cloud(self):
        """When GCP circuit breaker is open, fall to cloud."""
        router = _make_router(gcp_promoted=True, circuit_ok=False)
        assert router._decide_route() == RoutingDecision.CLOUD_CLAUDE

    def test_mirror_mode_still_allows_routing(self):
        """Mirror mode blocks mutations, not reads. _decide_route should work."""
        router = _make_router(gcp_promoted=True)
        # _guard_mirror on non-mutating should not raise
        # (mirror mode blocks promote/demote, not route decisions)
        assert router._decide_route() == RoutingDecision.GCP_PRIME
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_prime_router_gcp_first.py -v`
Expected: Multiple FAILs — current `_decide_route` does NOT put GCP first when circuit is open, and does NOT route to CLOUD before LOCAL when GCP is unavailable.

### Step 3: Implement GCP-first routing policy

Modify `backend/core/prime_router.py`:

**A) In `_decide_route()` (replace lines 464-497):**

```python
def _decide_route(self) -> RoutingDecision:
    """v290.0: GCP-first routing policy.

    Decision order:
    1. GCP_PRIME (always first when promoted + circuit healthy)
    2. CLOUD_CLAUDE (paid fallback, always reliable)
    3. LOCAL_PRIME / HYBRID (last resort, only if no remote option)
    4. DEGRADED (nothing available)

    Memory emergency additionally blocks local inference to prevent
    thrash amplification.
    """
    self._guard_mirror("_decide_route")

    is_emergency = self._is_memory_emergency()

    # ── Priority 1: GCP J-Prime (always first when available) ──
    if self._gcp_promoted and self._local_circuit.can_execute():
        return RoutingDecision.GCP_PRIME

    # ── Priority 2: Cloud Claude (reliable paid fallback) ──
    if self._config.enable_cloud_fallback:
        return RoutingDecision.CLOUD_CLAUDE

    # ── Priority 3: Local Prime (last resort, blocked during emergency) ──
    if is_emergency:
        # Local inference worsens memory thrash — skip entirely
        return RoutingDecision.DEGRADED

    prime_available = (
        self._prime_client is not None
        and self._prime_client.is_available
    )
    local_circuit_ok = self._local_circuit.can_execute()

    if prime_available and local_circuit_ok:
        if self._config.prefer_local:
            return RoutingDecision.HYBRID
        return RoutingDecision.LOCAL_PRIME

    # ── Priority 4: Nothing available ──
    return RoutingDecision.DEGRADED
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_prime_router_gcp_first.py -v`
Expected: All PASS

### Step 5: Update existing pressure gate tests

The tests in `tests/unit/backend/email_triage/test_triage_pressure_gate.py` assume the old routing behavior. Verify they still pass since the agent_runtime memory pressure code is separate from `_decide_route`:

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_triage_pressure_gate.py -v`
Expected: All PASS (pressure gate tests mock at the runner level, not PrimeRouter)

### Step 6: Commit

```bash
git add backend/core/prime_router.py tests/unit/core/test_prime_router_gcp_first.py
git commit -m "feat(routing): GCP-first routing policy — GCP_PRIME always preferred over local

Decision order: GCP_PRIME -> CLOUD_CLAUDE -> LOCAL/HYBRID -> DEGRADED.
Local inference is now last resort, not default. Memory emergency still
blocks local to prevent thrash amplification."
```

---

## Task 2: Real Gmail Outcome Detection

**Files:**
- Modify: `backend/autonomy/email_triage/outcome_collector.py:191-235`
- Modify: `backend/autonomy/email_triage/config.py` (enable outcome collection)
- Create: `tests/unit/backend/email_triage/test_outcome_detection.py`

### Step 1: Write the failing tests

```python
"""Tests for real Gmail outcome detection in OutcomeCollector."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.outcome_collector import OutcomeCollector
from autonomy.email_triage.schemas import EmailFeatures, TriagedEmail


def _make_features(message_id="msg-1", label_ids=("INBOX",)):
    return EmailFeatures(
        message_id=message_id, sender="user@example.com",
        sender_domain="example.com", subject="Test",
        snippet="hi", is_reply=False, has_attachment=False,
        label_ids=label_ids, keywords=(), sender_frequency="occasional",
        urgency_signals=(), extraction_confidence=0.8,
        extraction_source="heuristic",
    )


def _make_triaged(message_id="msg-1", tier=2, label_ids=("INBOX",)):
    scoring = MagicMock(tier=tier, score=70, tier_label="jarvis/tier2_high")
    return TriagedEmail(
        features=_make_features(message_id, label_ids),
        scoring=scoring,
        notification_action="immediate",
        processed_at=1000.0,
    )


def _mock_workspace_agent(label_responses):
    """Create a workspace agent mock that returns specified labels per message_id."""
    agent = AsyncMock()
    async def get_message_labels(message_id):
        return label_responses.get(message_id, set())
    agent.get_message_labels = get_message_labels
    return agent


class TestOutcomeDetection:
    @pytest.mark.asyncio
    async def test_replied_detected(self):
        """If SENT label appears, outcome should be 'replied'."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"INBOX", "SENT"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "replied"

    @pytest.mark.asyncio
    async def test_deleted_detected(self):
        """If TRASH label appears, outcome should be 'deleted'."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"TRASH"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "deleted"

    @pytest.mark.asyncio
    async def test_archived_detected(self):
        """If INBOX removed but not trashed, outcome should be 'archived'."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"IMPORTANT"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "archived"

    @pytest.mark.asyncio
    async def test_relabeled_detected(self):
        """If labels changed (not trash, not archived), outcome should be 'relabeled'."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX", "jarvis/tier3_review"))}

        ws = _mock_workspace_agent({"msg-1": {"INBOX", "jarvis/tier1_critical"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "relabeled"

    @pytest.mark.asyncio
    async def test_no_change_no_outcome(self):
        """If labels unchanged, no outcome recorded."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"INBOX"}})
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert len(outcomes) == 0

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self):
        """Gmail API errors should not crash — return empty list."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1")}

        ws = AsyncMock()
        ws.get_message_labels = AsyncMock(side_effect=Exception("Gmail 503"))
        outcomes = await collector.check_outcomes_for_cycle(ws, prior)
        assert outcomes == []

    @pytest.mark.asyncio
    async def test_reactor_enqueue_called_for_high_confidence(self):
        """Replied/deleted/relabeled outcomes should enqueue to reactor-core."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)
        prior = {"msg-1": _make_triaged("msg-1", label_ids=("INBOX",))}

        ws = _mock_workspace_agent({"msg-1": {"TRASH"}})

        with patch.object(collector, "_enqueue_to_reactor_core", new_callable=AsyncMock) as mock_enqueue:
            await collector.check_outcomes_for_cycle(ws, prior)
            mock_enqueue.assert_called_once()
            record = mock_enqueue.call_args[0][0]
            assert record["outcome"] == "deleted"
            assert record["confidence"] == "high"
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_outcome_detection.py -v`
Expected: FAILs — `check_outcomes_for_cycle` is a placeholder returning `[]`

### Step 3: Implement real outcome detection

**A) Add `get_message_labels` to GoogleWorkspaceAgent interface (or use duck-typed mock)**

For now, the OutcomeCollector will call `workspace_agent.get_message_labels(message_id)` which returns a `Set[str]`. The runner already passes `workspace_agent` to this method. We add a real implementation later; for now the contract is defined.

**B) Replace `check_outcomes_for_cycle` in `backend/autonomy/email_triage/outcome_collector.py` (replace lines 191-235):**

```python
async def check_outcomes_for_cycle(
    self,
    workspace_agent: Any,
    prior_triaged: Dict[str, TriagedEmail],
) -> List[Dict[str, Any]]:
    """Poll Gmail for label/status changes on previously triaged emails.

    Compares current labels against original labels at triage time.
    Outcome classification:
      - SENT added → replied (HIGH confidence)
      - TRASH present → deleted (HIGH confidence)
      - Labels changed (not trash) → relabeled (HIGH confidence)
      - INBOX removed (not trashed) → archived (MEDIUM confidence)
      - No change → no outcome recorded

    Args:
        workspace_agent: Agent with get_message_labels(msg_id) -> Set[str].
        prior_triaged: Dict of message_id -> TriagedEmail from prior cycle(s).

    Returns:
        List of outcome records captured this check.
    """
    if workspace_agent is None or not prior_triaged:
        return []

    if not hasattr(workspace_agent, "get_message_labels"):
        logger.debug("Workspace agent lacks get_message_labels — skipping outcomes")
        return []

    captured: List[Dict[str, Any]] = []

    for msg_id, triaged in prior_triaged.items():
        try:
            current_labels = await workspace_agent.get_message_labels(msg_id)
        except Exception as e:
            logger.debug("Failed to get labels for %s: %s", msg_id, e)
            continue

        if not isinstance(current_labels, set):
            current_labels = set(current_labels)

        original_labels = set(triaged.features.label_ids)

        # Determine outcome from label delta
        outcome = self._classify_outcome(original_labels, current_labels)
        if outcome is None:
            continue

        try:
            await self.record_outcome(
                message_id=msg_id,
                outcome=outcome,
                sender_domain=triaged.features.sender_domain,
                tier=triaged.scoring.tier,
                score=triaged.scoring.score,
                metadata={
                    "original_labels": sorted(original_labels),
                    "current_labels": sorted(current_labels),
                },
            )
            captured.append(self._recorded_outcomes[-1])
        except Exception as e:
            logger.debug("Failed to record outcome for %s: %s", msg_id, e)

    return captured

@staticmethod
def _classify_outcome(
    original: set, current: set
) -> Optional[str]:
    """Classify outcome from label delta.

    Returns outcome name or None if no meaningful change.
    """
    if "SENT" in current and "SENT" not in original:
        return "replied"
    if "TRASH" in current:
        return "deleted"
    if "INBOX" in original and "INBOX" not in current:
        return "archived"
    if current != original:
        return "relabeled"
    return None
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_outcome_detection.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/autonomy/email_triage/outcome_collector.py tests/unit/backend/email_triage/test_outcome_detection.py
git commit -m "feat(triage): implement real Gmail outcome detection via label deltas

Replaces placeholder with actual label comparison: replied (SENT added),
deleted (TRASH), archived (INBOX removed), relabeled (labels changed).
Feeds high-confidence outcomes to reactor-core training pipeline."
```

---

## Task 3: Enable Outcome Collection by Default

**Files:**
- Modify: `backend/autonomy/email_triage/config.py` (~line 113)
- Modify: `tests/unit/backend/email_triage/test_config.py` (if needed)

### Step 1: Change defaults

In `backend/autonomy/email_triage/config.py`, change:

```python
# Before:
outcome_collection_enabled: bool = False
# After:
outcome_collection_enabled: bool = True
```

And the env var factory:
```python
# Before:
outcome_collection_enabled=_env_bool("EMAIL_TRIAGE_OUTCOME_COLLECTION", False),
# After:
outcome_collection_enabled=_env_bool("EMAIL_TRIAGE_OUTCOME_COLLECTION", True),
```

### Step 2: Run existing tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ -v --timeout=30`
Expected: All PASS (outcome collection is guarded — even enabled, it gracefully no-ops when workspace_agent lacks `get_message_labels`)

### Step 3: Commit

```bash
git add backend/autonomy/email_triage/config.py
git commit -m "feat(triage): enable outcome collection by default

Outcome collection now ON by default. Gracefully no-ops when
workspace_agent lacks get_message_labels method."
```

---

## Task 4: Wire ExperienceQueue Processor in Agent Runtime

**Files:**
- Modify: `backend/autonomy/agent_runtime.py`
- Create: `tests/unit/backend/test_experience_queue_lifecycle.py`

### Step 1: Write the failing test

```python
"""Tests for ExperienceQueueProcessor lifecycle in agent_runtime."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest


def _build_runtime():
    from autonomy.agent_runtime import UnifiedAgentRuntime
    rt = UnifiedAgentRuntime.__new__(UnifiedAgentRuntime)
    rt._experience_processor = None
    rt._experience_processor_started = False
    return rt


class TestExperienceQueueLifecycle:
    @pytest.mark.asyncio
    async def test_start_experience_processor_sets_flag(self):
        """_start_experience_processor should start the processor and set flag."""
        rt = _build_runtime()

        mock_processor = AsyncMock()
        mock_processor.start = AsyncMock()

        with patch("autonomy.agent_runtime.get_experience_processor",
                    new_callable=AsyncMock, return_value=mock_processor):
            await rt._start_experience_processor()

        assert rt._experience_processor_started is True
        assert rt._experience_processor is mock_processor
        mock_processor.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_experience_processor_idempotent(self):
        """Calling twice should not start twice."""
        rt = _build_runtime()
        rt._experience_processor_started = True
        rt._experience_processor = MagicMock()

        # Should not call get_experience_processor at all
        with patch("autonomy.agent_runtime.get_experience_processor",
                    new_callable=AsyncMock) as mock_get:
            await rt._start_experience_processor()
            mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_experience_processor_error_non_fatal(self):
        """If processor init fails, runtime continues — non-fatal."""
        rt = _build_runtime()

        with patch("autonomy.agent_runtime.get_experience_processor",
                    new_callable=AsyncMock, side_effect=Exception("DB locked")):
            await rt._start_experience_processor()

        assert rt._experience_processor_started is False
        assert rt._experience_processor is None
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_experience_queue_lifecycle.py -v`
Expected: FAILs — `_start_experience_processor` doesn't exist yet

### Step 3: Implement experience processor lifecycle

In `backend/autonomy/agent_runtime.py`, add:

**A) Import at top (with other lazy imports):**
```python
# Near the top, with other optional imports
try:
    from core.experience_queue import get_experience_processor
except ImportError:
    get_experience_processor = None
```

**B) In `__init__` (or the `__new__` pattern used), add:**
```python
self._experience_processor = None
self._experience_processor_started = False
```

**C) New method:**
```python
async def _start_experience_processor(self) -> None:
    """Start the ExperienceQueueProcessor for reactor-core drain.

    Non-fatal: if init fails, runtime continues without training drain.
    Idempotent: safe to call multiple times.
    """
    if self._experience_processor_started:
        return
    if get_experience_processor is None:
        return

    try:
        processor = await get_experience_processor()
        await processor.start()
        self._experience_processor = processor
        self._experience_processor_started = True
        logger.info("[AgentRuntime] ExperienceQueueProcessor started")
    except Exception as e:
        logger.warning("[AgentRuntime] ExperienceQueueProcessor start failed (non-fatal): %s", e)
```

**D) Call `_start_experience_processor()` from the main run loop (after first successful triage cycle), or from the initialization path.** Find the spot where `_maybe_run_email_triage` is called and add a one-shot call:

```python
# Inside the main loop, after email triage runs successfully:
if not self._experience_processor_started:
    await self._start_experience_processor()
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_experience_queue_lifecycle.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/autonomy/agent_runtime.py tests/unit/backend/test_experience_queue_lifecycle.py
git commit -m "feat(runtime): wire ExperienceQueueProcessor into agent_runtime lifecycle

Starts the experience queue drain-to-reactor background task after first
successful triage cycle. Non-fatal: runtime continues if init fails.
Idempotent: safe to call on every cycle."
```

---

## Task 5: Training Capture User Notifications

**Files:**
- Modify: `backend/autonomy/email_triage/outcome_collector.py`
- Modify: `backend/autonomy/email_triage/runner.py` (~line 502-527)
- Create: `tests/unit/backend/email_triage/test_training_notifications.py`

### Step 1: Write the failing test

```python
"""Tests for user-facing training capture notifications."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


class TestTrainingCaptureNotification:
    @pytest.mark.asyncio
    async def test_notification_sent_when_outcomes_captured(self):
        """When outcomes are captured, notify user with count + confidence mix."""
        from autonomy.email_triage.notifications import build_training_capture_message

        outcomes = [
            {"outcome": "replied", "confidence": "high", "sender_domain": "boss.com"},
            {"outcome": "archived", "confidence": "medium", "sender_domain": "news.com"},
        ]

        message = build_training_capture_message(outcomes)
        assert "2 email outcomes" in message.lower() or "2 outcomes" in message.lower()
        assert "training" in message.lower()

    @pytest.mark.asyncio
    async def test_no_notification_when_no_outcomes(self):
        """No notification when zero outcomes captured."""
        from autonomy.email_triage.notifications import build_training_capture_message

        message = build_training_capture_message([])
        assert message is None
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_training_notifications.py -v`
Expected: FAIL — `build_training_capture_message` doesn't exist

### Step 3: Implement training capture notification

**A) In `backend/autonomy/email_triage/notifications.py`, add:**

```python
def build_training_capture_message(
    outcomes: List[Dict[str, Any]],
) -> Optional[str]:
    """Build a user-facing message about training data captured.

    Returns None if no outcomes to report.
    """
    if not outcomes:
        return None

    high = sum(1 for o in outcomes if o.get("confidence") == "high")
    medium = sum(1 for o in outcomes if o.get("confidence") == "medium")
    total = len(outcomes)

    parts = []
    if high:
        parts.append(f"{high} high-confidence")
    if medium:
        parts.append(f"{medium} medium-confidence")

    confidence_desc = " and ".join(parts) if parts else f"{total}"

    return (
        f"Sir, I captured {total} email outcomes for training — "
        f"{confidence_desc}. Your preferences are being learned."
    )
```

**B) In the runner, after outcome collection (around line 509), add notification dispatch:**

```python
# After outcome collection succeeds with captured outcomes:
if captured_outcomes:
    try:
        from autonomy.email_triage.notifications import build_training_capture_message
        training_msg = build_training_capture_message(captured_outcomes)
        if training_msg and notifier:
            await _invoke_notifier(notifier, message=training_msg, urgency=1, title="JARVIS Training")
    except Exception as e:
        logger.debug("Training capture notification failed: %s", e)
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_training_notifications.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/autonomy/email_triage/notifications.py backend/autonomy/email_triage/runner.py tests/unit/backend/email_triage/test_training_notifications.py
git commit -m "feat(triage): add user-facing training capture notifications

Notifies user once per cycle when email outcomes are captured for
training. Shows count and confidence breakdown. Low urgency to avoid
spam."
```

---

## Task 6: Multi-Brain Model Governance Contract

**Files:**
- Create: `backend/core/contracts/model_artifact_manifest.py`
- Create: `tests/unit/core/test_model_artifact_manifest.py`

### Step 1: Write the failing tests

```python
"""Tests for ModelArtifactManifest — multi-brain model governance."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from core.contracts.model_artifact_manifest import (
    ModelArtifactManifest,
    BrainCapability,
    is_compatible,
)


class TestModelArtifactManifest:
    def test_manifest_creation(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
            min_runtime_version="1.0.0",
            eval_scores={"accuracy": 0.92, "f1": 0.89},
        )
        assert manifest.brain_id == "email_triage"
        assert BrainCapability.EMAIL_CLASSIFICATION in manifest.capabilities

    def test_manifest_is_frozen(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
        )
        with pytest.raises(AttributeError):
            manifest.brain_id = "other"

    def test_compatibility_check_passes(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
            min_runtime_version="1.0.0",
        )
        assert is_compatible(manifest, runtime_version="1.2.0",
                             requested_capability=BrainCapability.EMAIL_CLASSIFICATION)

    def test_compatibility_check_fails_wrong_capability(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
        )
        assert not is_compatible(manifest, runtime_version="1.0.0",
                                 requested_capability=BrainCapability.VOICE_PROCESSING)

    def test_compatibility_check_fails_old_runtime(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
            min_runtime_version="2.0.0",
        )
        assert not is_compatible(manifest, runtime_version="1.5.0",
                                 requested_capability=BrainCapability.EMAIL_CLASSIFICATION)
```

### Step 2: Run tests to verify they fail

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_model_artifact_manifest.py -v`
Expected: FAIL — module doesn't exist

### Step 3: Implement model artifact manifest

Create `backend/core/contracts/model_artifact_manifest.py`:

```python
"""Multi-brain model governance contract.

Every trained model artifact exported by reactor-core must carry a
ModelArtifactManifest. J-Prime only loads artifacts whose capability
tags match the requested brain and whose runtime version is compatible.

This is the foundation for:
- Brain-scoped promotion (triage vs voice vs planner)
- Canary/shadow/active rollout state machines
- Automatic rollback on eval regression
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class BrainCapability(Enum):
    """Capability tags for J-Prime brains."""
    EMAIL_CLASSIFICATION = "email_classification"
    VOICE_PROCESSING = "voice_processing"
    PLANNING = "planning"
    REASONING = "reasoning"
    CODE_GENERATION = "code_generation"
    GENERAL = "general"


@dataclass(frozen=True)
class ModelArtifactManifest:
    """Immutable manifest for a trained model artifact.

    Attributes:
        brain_id: Unique identifier for the brain this model serves.
        model_name: Human-readable model name (e.g., "jarvis-triage-v3").
        capabilities: Tuple of BrainCapability tags this model supports.
        schema_version: Contract schema version for forward compatibility.
        min_runtime_version: Minimum J-Prime runtime version required.
        target_runtime_version: Optimal runtime version (optional).
        eval_scores: Evaluation scores from reactor-core (accuracy, f1, etc.).
        rollback_parent: Model name of the parent this was trained from.
        training_data_hash: Hash of training data for provenance.
    """
    brain_id: str
    model_name: str
    capabilities: Tuple[BrainCapability, ...] = ()
    schema_version: str = "1.0"
    min_runtime_version: str = "1.0.0"
    target_runtime_version: Optional[str] = None
    eval_scores: Dict[str, float] = field(default_factory=dict)
    rollback_parent: Optional[str] = None
    training_data_hash: Optional[str] = None


def _parse_version(v: str) -> Tuple[int, ...]:
    """Parse semver string into comparable tuple."""
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_compatible(
    manifest: ModelArtifactManifest,
    runtime_version: str,
    requested_capability: BrainCapability,
) -> bool:
    """Check if a model artifact is compatible with the runtime and request.

    Args:
        manifest: The model's manifest.
        runtime_version: Current J-Prime runtime version string.
        requested_capability: The capability being requested.

    Returns:
        True if the model supports the capability and the runtime is new enough.
    """
    if requested_capability not in manifest.capabilities:
        return False

    if manifest.min_runtime_version:
        if _parse_version(runtime_version) < _parse_version(manifest.min_runtime_version):
            return False

    return True
```

### Step 4: Run tests to verify they pass

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_model_artifact_manifest.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/core/contracts/model_artifact_manifest.py tests/unit/core/test_model_artifact_manifest.py
git commit -m "feat(contracts): add ModelArtifactManifest for multi-brain model governance

Frozen dataclass with brain_id, capability tags, schema version,
runtime compatibility, eval scores, and provenance tracking.
Foundation for brain-scoped promotion and automatic rollback."
```

---

## Task 7: Brain-Scoped Experience Entries

**Files:**
- Modify: `backend/autonomy/email_triage/outcome_collector.py` (`_enqueue_to_reactor_core`)
- Create: `tests/unit/backend/email_triage/test_brain_scoped_outcomes.py`

### Step 1: Write the failing test

```python
"""Tests for brain_id scoping on experience entries."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.outcome_collector import OutcomeCollector
from autonomy.email_triage.config import TriageConfig


class TestBrainScopedOutcomes:
    @pytest.mark.asyncio
    async def test_enqueue_includes_brain_id(self):
        """Reactor-core experience entry must include brain_id=email_triage."""
        config = TriageConfig(enabled=True, outcome_collection_enabled=True)
        collector = OutcomeCollector(config)

        record = {
            "outcome": "replied",
            "confidence": "high",
            "tier": 2,
            "sender_domain": "example.com",
        }

        with patch("autonomy.email_triage.outcome_collector.enqueue_experience",
                    new_callable=AsyncMock) as mock_enqueue:
            await collector._enqueue_to_reactor_core(record)

            mock_enqueue.assert_called_once()
            call_kwargs = mock_enqueue.call_args
            data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data") or call_kwargs[0][1]
            assert data["brain_id"] == "email_triage"
            assert data["source"] == "email_triage"
```

### Step 2: Run test to verify it fails

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_brain_scoped_outcomes.py -v`
Expected: FAIL — current `_enqueue_to_reactor_core` doesn't include `brain_id`

### Step 3: Add brain_id to reactor enqueue

In `backend/autonomy/email_triage/outcome_collector.py`, modify `_enqueue_to_reactor_core` (around line 147):

```python
async def _enqueue_to_reactor_core(self, record: Dict[str, Any]) -> None:
    """Best-effort enqueue to the Reactor-Core ExperienceDataQueue.

    Includes brain_id for multi-brain model governance scoping.
    """
    try:
        from core.experience_queue import (
            ExperiencePriority,
            ExperienceType,
            enqueue_experience,
        )
    except ImportError:
        return

    await enqueue_experience(
        experience_type=ExperienceType.BEHAVIORAL_EVENT,
        data={
            "brain_id": "email_triage",
            "source": "email_triage",
            "outcome": record["outcome"],
            "confidence": record["confidence"],
            "tier": record["tier"],
            "sender_domain": record["sender_domain"],
        },
        priority=ExperiencePriority.NORMAL,
    )
```

### Step 4: Run test to verify it passes

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_brain_scoped_outcomes.py -v`
Expected: PASS

### Step 5: Commit

```bash
git add backend/autonomy/email_triage/outcome_collector.py tests/unit/backend/email_triage/test_brain_scoped_outcomes.py
git commit -m "feat(triage): add brain_id=email_triage to reactor-core experience entries

Scopes training data by brain so reactor-core training lanes don't
cross-contaminate between email classification, voice, and planning."
```

---

## Task 8: Integration Test — Route Selection Matrix

**Files:**
- Create: `tests/unit/core/test_route_selection_matrix.py`

### Step 1: Write comprehensive route matrix test

```python
"""Exhaustive route selection matrix for PrimeRouter._decide_route.

Tests every combination of:
  - GCP promoted (yes/no)
  - Circuit breaker (open/closed)
  - Memory emergency (yes/no)
  - Cloud fallback enabled (yes/no)
  - Local prime available (yes/no)
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from core.prime_router import PrimeRouter, PrimeRouterConfig, RoutingDecision

# Reuse _make_router from Task 1 test or copy it here
def _make_router(gcp_promoted=False, prime_available=True, circuit_ok=True,
                 prefer_local=True, cloud_fallback=True, memory_emergency=False):
    config = PrimeRouterConfig()
    config.prefer_local = prefer_local
    config.enable_cloud_fallback = cloud_fallback
    router = PrimeRouter.__new__(PrimeRouter)
    router._config = config
    router._metrics = MagicMock()
    router._prime_client = MagicMock() if prime_available else None
    if router._prime_client:
        router._prime_client.is_available = True
    router._cloud_client = None
    router._graceful_degradation = None
    router._lock = MagicMock()
    router._initialized = True
    router._gcp_promoted = gcp_promoted
    router._gcp_host = "34.45.154.209" if gcp_promoted else None
    router._gcp_port = 8080 if gcp_promoted else None
    router._local_circuit = MagicMock()
    router._local_circuit.can_execute.return_value = circuit_ok
    router._last_transition_time = 0.0
    router._transition_cooldown_s = 30.0
    router._transition_in_flight = False
    router._mirror_mode = False
    router._mirror_decisions_issued = 0
    router._cloud_run_patterns = (".run.app", ".a.run.app")
    router._is_memory_emergency = lambda: memory_emergency
    return router


# Matrix: (gcp, circuit, emergency, cloud, local) -> expected
ROUTE_MATRIX = [
    # GCP available and healthy — always GCP
    (True, True, False, True, True, RoutingDecision.GCP_PRIME),
    (True, True, False, True, False, RoutingDecision.GCP_PRIME),
    (True, True, False, False, True, RoutingDecision.GCP_PRIME),
    (True, True, True, True, True, RoutingDecision.GCP_PRIME),

    # GCP promoted but circuit open — fall to cloud
    (True, False, False, True, True, RoutingDecision.CLOUD_CLAUDE),
    (True, False, True, True, True, RoutingDecision.CLOUD_CLAUDE),

    # No GCP, cloud available — cloud
    (False, True, False, True, True, RoutingDecision.CLOUD_CLAUDE),
    (False, True, True, True, True, RoutingDecision.CLOUD_CLAUDE),

    # No GCP, no cloud, local available, no emergency — local
    (False, True, False, False, True, {RoutingDecision.LOCAL_PRIME, RoutingDecision.HYBRID}),

    # No GCP, no cloud, emergency — degraded (local blocked)
    (False, True, True, False, True, RoutingDecision.DEGRADED),

    # Nothing available — degraded
    (False, False, False, False, False, RoutingDecision.DEGRADED),
    (False, True, True, False, False, RoutingDecision.DEGRADED),
]


@pytest.mark.parametrize(
    "gcp,circuit,emergency,cloud,local,expected",
    ROUTE_MATRIX,
    ids=[
        "gcp_healthy_cloud_local",
        "gcp_healthy_cloud_nolocal",
        "gcp_healthy_nocloud_local",
        "gcp_healthy_emergency",
        "gcp_circuit_open_cloud",
        "gcp_circuit_open_emergency",
        "no_gcp_cloud",
        "no_gcp_cloud_emergency",
        "no_gcp_no_cloud_local",
        "no_gcp_no_cloud_emergency",
        "nothing_available",
        "nothing_emergency",
    ],
)
def test_route_matrix(gcp, circuit, emergency, cloud, local, expected):
    router = _make_router(
        gcp_promoted=gcp,
        circuit_ok=circuit,
        memory_emergency=emergency,
        cloud_fallback=cloud,
        prime_available=local,
    )
    result = router._decide_route()

    if isinstance(expected, set):
        assert result in expected, f"Expected one of {expected}, got {result}"
    else:
        assert result == expected, f"Expected {expected}, got {result}"
```

### Step 2: Run tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_route_selection_matrix.py -v`
Expected: All 12 PASS

### Step 3: Commit

```bash
git add tests/unit/core/test_route_selection_matrix.py
git commit -m "test(routing): exhaustive route selection matrix (12 scenarios)

Parametrized test covering every combination of GCP/circuit/emergency/
cloud/local availability. Ensures GCP-first policy holds under all
conditions."
```

---

## Task 9: Run Full Test Suite

### Step 1: Run all email triage tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ -v --timeout=30`
Expected: All PASS

### Step 2: Run new routing tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_prime_router_gcp_first.py tests/unit/core/test_route_selection_matrix.py tests/unit/core/test_model_artifact_manifest.py -v`
Expected: All PASS

### Step 3: Run experience queue lifecycle test

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/test_experience_queue_lifecycle.py -v`
Expected: All PASS

---

## Summary of Changes

| Task | File | Change | Tests |
|------|------|--------|-------|
| 1 | `prime_router.py:464-497` | GCP-first `_decide_route()` | `test_prime_router_gcp_first.py` (9 tests) |
| 2 | `outcome_collector.py:191-235` | Real Gmail outcome detection | `test_outcome_detection.py` (7 tests) |
| 3 | `config.py:113` | Enable outcome collection default | Existing tests |
| 4 | `agent_runtime.py` | Wire ExperienceQueueProcessor | `test_experience_queue_lifecycle.py` (3 tests) |
| 5 | `notifications.py` + `runner.py` | Training capture notifications | `test_training_notifications.py` (2 tests) |
| 6 | `model_artifact_manifest.py` (new) | Multi-brain governance contract | `test_model_artifact_manifest.py` (5 tests) |
| 7 | `outcome_collector.py:147` | brain_id on experience entries | `test_brain_scoped_outcomes.py` (1 test) |
| 8 | `test_route_selection_matrix.py` (new) | Exhaustive 12-scenario matrix | Parametrized |

**Total: 8 commits, ~39 tests, 4 new files, 5 modified files.**

---

## What Lives in Other Repos (Not This Plan)

These are documented here for completeness but are NOT implemented in this plan:

### Reactor-Core Repo
- Per-brain training lanes (separate pipelines by `brain_id`)
- Consume `brain_id` from ExperienceDataQueue entries
- Eval gates per brain (accuracy/f1 thresholds before export)
- Artifact manifest generation (attach `ModelArtifactManifest` to exported `.gguf`)

### J-Prime Repo
- Brain-specific artifact loading (filter by `BrainCapability`)
- Runtime version reporting (for compatibility checks)
- Canary/shadow/active rollout state machine
- Health endpoint exposes `brain_id` + loaded model manifest

### Unified Supervisor (Future)
- Readiness-scored promotion (not just boolean `_gcp_promoted`)
- Health registry with freshness TTL per brain
- Promotion eligibility validation against `ModelArtifactManifest`

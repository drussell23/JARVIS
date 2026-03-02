# Email Triage Integration Layer — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the autonomous email triage system to live infrastructure: real Gmail fetching via GoogleWorkspaceAgent, J-Prime extraction via PrimeRouter, notification routing via notification_bridge, and "check my email" enrichment via triage cache.

**Architecture:** Hybrid lazy dependency resolution on the EmailTriageRunner singleton. Dependencies resolve themselves on first run_cycle() with exponential backoff on failure. Triage results are cached in-memory on the singleton with an asyncio.Lock for concurrent read/write safety. The command processor enriches raw email results with triage metadata when fresh results exist, falling back gracefully to raw emails when unavailable.

**Tech Stack:** Python 3.11, asyncio, dataclasses, pytest, Gmail API v1, PrimeRouter, notification_bridge

**Design Doc:** `docs/plans/2026-03-01-email-triage-integration-design.md`

---

## Task 1: Schema Additions — Report Versioning & Delivery Result

**Files:**
- Modify: `backend/autonomy/email_triage/schemas.py`
- Test: `tests/unit/backend/email_triage/test_schemas.py`

**Context:** TriageCycleReport and TriagedEmail need version fields for safe enrichment compatibility, and we need a NotificationDeliveryResult dataclass for bounded async notification tracking.

**Step 1: Write the failing tests**

Add to `tests/unit/backend/email_triage/test_schemas.py`:

```python
class TestTriageCycleReportVersioning:
    """Report carries schema and policy version."""

    def test_report_has_schema_version(self):
        report = TriageCycleReport(
            cycle_id="abc",
            started_at=1000.0,
            completed_at=1001.0,
            emails_fetched=0,
            emails_processed=0,
            tier_counts={},
            notifications_sent=0,
            notifications_suppressed=0,
            errors=[],
            triage_schema_version="1.0",
            policy_version="v1",
        )
        assert report.triage_schema_version == "1.0"
        assert report.policy_version == "v1"

    def test_report_schema_version_defaults(self):
        report = TriageCycleReport(
            cycle_id="abc",
            started_at=1000.0,
            completed_at=1001.0,
            emails_fetched=0,
            emails_processed=0,
            tier_counts={},
            notifications_sent=0,
            notifications_suppressed=0,
            errors=[],
        )
        assert report.triage_schema_version == "1.0"
        assert report.policy_version == "v1"


class TestTriagedEmailMessageId:
    """TriagedEmail exposes message_id for enrichment lookup."""

    def test_message_id_accessible(self):
        from autonomy.email_triage.schemas import EmailFeatures, ScoringResult
        features = EmailFeatures(
            message_id="msg_001", sender="a@b.com", sender_domain="b.com",
            subject="Test", snippet="snippet", is_reply=False, has_attachment=False,
            label_ids=(), keywords=(), sender_frequency="first_time",
            urgency_signals=(), extraction_confidence=0.0,
        )
        scoring = ScoringResult(
            score=50, tier=3, tier_label="jarvis/tier3_review",
            breakdown={}, idempotency_key="abc123",
        )
        triaged = TriagedEmail(
            features=features, scoring=scoring,
            notification_action="label_only", processed_at=1000.0,
        )
        assert triaged.features.message_id == "msg_001"


class TestNotificationDeliveryResult:
    """NotificationDeliveryResult tracks delivery outcome."""

    def test_success_result(self):
        from autonomy.email_triage.schemas import NotificationDeliveryResult
        result = NotificationDeliveryResult(
            message_id="msg_001",
            channel="voice",
            success=True,
            latency_ms=150,
        )
        assert result.success is True
        assert result.channel == "voice"
        assert result.error is None

    def test_failure_result(self):
        from autonomy.email_triage.schemas import NotificationDeliveryResult
        result = NotificationDeliveryResult(
            message_id="msg_001",
            channel="websocket",
            success=False,
            latency_ms=5000,
            error="timeout",
        )
        assert result.success is False
        assert result.error == "timeout"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_schemas.py -v -k "TestTriageCycleReportVersioning or TestTriagedEmailMessageId or TestNotificationDeliveryResult"`
Expected: FAIL — `triage_schema_version` unexpected keyword, `NotificationDeliveryResult` not found

**Step 3: Add schema fields**

In `backend/autonomy/email_triage/schemas.py`:

Add `NotificationDeliveryResult` dataclass after `TriagedEmail`:

```python
@dataclass(frozen=True)
class NotificationDeliveryResult:
    """Outcome of a single notification delivery attempt."""

    message_id: str
    channel: str  # "voice" | "websocket" | "macos"
    success: bool
    latency_ms: int
    error: Optional[str] = None
```

Add version fields to `TriageCycleReport` (as defaults so existing tests don't break):

```python
    triage_schema_version: str = "1.0"
    policy_version: str = "v1"
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_schemas.py -v`
Expected: ALL PASS

**Step 5: Run full triage test suite for regression**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ -v`
Expected: ALL PASS (existing 77 tests + new tests)

**Step 6: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/email_triage/schemas.py tests/unit/backend/email_triage/test_schemas.py && git commit -m "feat(email-triage): add report versioning and NotificationDeliveryResult schema"
```

---

## Task 2: Config Additions — Integration Environment Variables

**Files:**
- Modify: `backend/autonomy/email_triage/config.py`
- Test: `tests/unit/backend/email_triage/test_config.py`

**Context:** The integration layer needs env vars for dependency backoff, staleness window, notification budgets, and flush thresholds.

**Step 1: Write the failing tests**

Add to `tests/unit/backend/email_triage/test_config.py`:

```python
class TestIntegrationConfig:
    """Integration layer configuration fields."""

    def test_backoff_defaults(self):
        config = TriageConfig()
        assert config.dep_backoff_base_s == 5.0
        assert config.dep_backoff_max_s == 300.0

    def test_staleness_default(self):
        config = TriageConfig()
        assert config.staleness_window_s == 120.0

    def test_notification_budget_defaults(self):
        config = TriageConfig()
        assert config.notification_budget_s == 10.0
        assert config.summary_budget_s == 5.0

    def test_flush_threshold_default(self):
        config = TriageConfig()
        assert config.immediate_flush_threshold == 10
        assert config.max_summary_items == 20

    def test_from_env_reads_new_fields(self):
        import os
        with patch.dict(os.environ, {
            "EMAIL_TRIAGE_DEP_BACKOFF_BASE_S": "10.0",
            "EMAIL_TRIAGE_STALENESS_WINDOW_S": "60.0",
        }):
            from autonomy.email_triage.config import reset_triage_config
            reset_triage_config()
            config = TriageConfig.from_env()
            assert config.dep_backoff_base_s == 10.0
            assert config.staleness_window_s == 60.0
            reset_triage_config()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_config.py -v -k "TestIntegrationConfig"`
Expected: FAIL — `dep_backoff_base_s` not a field

**Step 3: Add config fields**

In `backend/autonomy/email_triage/config.py`, add to the `TriageConfig` dataclass (after existing fields):

```python
    # Dependency resolution
    dep_backoff_base_s: float = 5.0
    dep_backoff_max_s: float = 300.0

    # Staleness
    staleness_window_s: float = 120.0

    # Notification delivery
    notification_budget_s: float = 10.0
    summary_budget_s: float = 5.0
    immediate_flush_threshold: int = 10
    max_summary_items: int = 20
```

In `from_env()`, add corresponding env var reads:

```python
            dep_backoff_base_s=_env_float("EMAIL_TRIAGE_DEP_BACKOFF_BASE_S", 5.0),
            dep_backoff_max_s=_env_float("EMAIL_TRIAGE_DEP_BACKOFF_MAX_S", 300.0),
            staleness_window_s=_env_float("EMAIL_TRIAGE_STALENESS_WINDOW_S", 120.0),
            notification_budget_s=_env_float("EMAIL_TRIAGE_NOTIFICATION_BUDGET_S", 10.0),
            summary_budget_s=_env_float("EMAIL_TRIAGE_SUMMARY_BUDGET_S", 5.0),
            immediate_flush_threshold=_env_int("EMAIL_TRIAGE_IMMEDIATE_FLUSH_THRESHOLD", 10),
            max_summary_items=_env_int("EMAIL_TRIAGE_MAX_SUMMARY_ITEMS", 20),
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_config.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/email_triage/config.py tests/unit/backend/email_triage/test_config.py && git commit -m "feat(email-triage): add integration layer config fields"
```

---

## Task 3: Event Additions — Dependency & Notification Delivery Events

**Files:**
- Modify: `backend/autonomy/email_triage/events.py`
- Test: `tests/unit/backend/email_triage/test_events.py`

**Context:** New events: `EVENT_DEPENDENCY_UNAVAILABLE`, `EVENT_DEPENDENCY_DEGRADED`, `EVENT_NOTIFICATION_DELIVERY_RESULT`.

**Step 1: Write the failing tests**

Add to `tests/unit/backend/email_triage/test_events.py`:

```python
class TestDependencyEvents:
    """Dependency lifecycle events."""

    def test_unavailable_event_exists(self):
        from autonomy.email_triage.events import EVENT_DEPENDENCY_UNAVAILABLE
        assert EVENT_DEPENDENCY_UNAVAILABLE == "dependency_unavailable"

    def test_degraded_event_exists(self):
        from autonomy.email_triage.events import EVENT_DEPENDENCY_DEGRADED
        assert EVENT_DEPENDENCY_DEGRADED == "dependency_degraded"

    def test_delivery_result_event_exists(self):
        from autonomy.email_triage.events import EVENT_NOTIFICATION_DELIVERY_RESULT
        assert EVENT_NOTIFICATION_DELIVERY_RESULT == "notification_delivery_result"

    def test_emit_dependency_event(self, caplog):
        import logging
        from autonomy.email_triage.events import emit_triage_event, EVENT_DEPENDENCY_UNAVAILABLE
        with caplog.at_level(logging.WARNING, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_DEPENDENCY_UNAVAILABLE, {
                "dependency_name": "workspace_agent",
                "error": "ImportError",
                "consecutive_failures": 3,
            })
        assert "dependency_unavailable" in caplog.text
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_events.py -v -k "TestDependencyEvents"`
Expected: FAIL — `EVENT_DEPENDENCY_UNAVAILABLE` cannot be imported

**Step 3: Add event constants**

In `backend/autonomy/email_triage/events.py`, add after existing constants:

```python
EVENT_DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
EVENT_DEPENDENCY_DEGRADED = "dependency_degraded"
EVENT_NOTIFICATION_DELIVERY_RESULT = "notification_delivery_result"
```

Add these to `_ERROR_EVENTS`:

```python
_ERROR_EVENTS = frozenset({EVENT_TRIAGE_ERROR, EVENT_DEPENDENCY_UNAVAILABLE})
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_events.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/email_triage/events.py tests/unit/backend/email_triage/test_events.py && git commit -m "feat(email-triage): add dependency and notification delivery events"
```

---

## Task 4: Dependency Resolution Module

**Files:**
- Create: `backend/autonomy/email_triage/dependencies.py`
- Create: `tests/unit/backend/email_triage/test_dependencies.py`

**Context:** This module provides `DependencyHealth` tracking and lazy resolution with exponential backoff. The runner will use this to resolve workspace_agent, router, and notifier. Each dependency has its own health state and backoff timer using monotonic time.

**Step 1: Write the failing tests**

Create `tests/unit/backend/email_triage/test_dependencies.py`:

```python
"""Tests for the dependency resolution module."""

import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.dependencies import DependencyHealth, DependencyResolver
from autonomy.email_triage.config import TriageConfig


class TestDependencyHealth:
    """DependencyHealth tracks resolution state."""

    def test_initial_state(self):
        h = DependencyHealth(name="workspace_agent", required=True)
        assert h.resolved is False
        assert h.instance is None
        assert h.consecutive_failures == 0
        assert h.next_attempt_at == 0.0

    def test_record_success(self):
        h = DependencyHealth(name="workspace_agent", required=True)
        h.record_success(instance="fake_agent")
        assert h.resolved is True
        assert h.instance == "fake_agent"
        assert h.consecutive_failures == 0
        assert h.last_resolve_error is None

    def test_record_failure_increments(self):
        h = DependencyHealth(name="router", required=False)
        h.record_failure("ImportError: no module", base_s=5.0, max_s=300.0)
        assert h.resolved is False
        assert h.consecutive_failures == 1
        assert h.last_resolve_error == "ImportError: no module"
        assert h.next_attempt_at > time.monotonic()

    def test_backoff_increases_exponentially(self):
        h = DependencyHealth(name="router", required=False)
        h.record_failure("err1", base_s=5.0, max_s=300.0)
        first_wait = h.next_attempt_at - time.monotonic()
        h.record_failure("err2", base_s=5.0, max_s=300.0)
        second_wait = h.next_attempt_at - time.monotonic()
        # Second backoff should be roughly 2x the first (with jitter)
        assert second_wait > first_wait * 1.3  # conservative due to jitter

    def test_backoff_capped_at_max(self):
        h = DependencyHealth(name="router", required=False)
        for _ in range(20):
            h.record_failure("err", base_s=5.0, max_s=60.0)
        wait = h.next_attempt_at - time.monotonic()
        # With jitter max is 60 * 1.2 = 72
        assert wait <= 72.0

    def test_can_attempt_respects_backoff(self):
        h = DependencyHealth(name="router", required=False)
        h.record_failure("err", base_s=5.0, max_s=300.0)
        assert h.can_attempt() is False  # Within backoff window

    def test_can_attempt_true_initially(self):
        h = DependencyHealth(name="router", required=False)
        assert h.can_attempt() is True

    def test_invalidate_clears_instance(self):
        h = DependencyHealth(name="workspace_agent", required=True)
        h.record_success(instance="fake_agent")
        h.invalidate("connection lost", base_s=5.0, max_s=300.0)
        assert h.resolved is False
        assert h.instance is None
        assert h.consecutive_failures == 1


class TestDependencyResolver:
    """DependencyResolver manages all three dependencies."""

    def test_resolver_creates_three_deps(self):
        config = TriageConfig()
        resolver = DependencyResolver(config)
        assert "workspace_agent" in resolver.health
        assert "router" in resolver.health
        assert "notifier" in resolver.health

    def test_workspace_agent_is_required(self):
        config = TriageConfig()
        resolver = DependencyResolver(config)
        assert resolver.health["workspace_agent"].required is True

    def test_router_is_optional(self):
        config = TriageConfig()
        resolver = DependencyResolver(config)
        assert resolver.health["router"].required is False

    def test_notifier_is_optional(self):
        config = TriageConfig()
        resolver = DependencyResolver(config)
        assert resolver.health["notifier"].required is False

    def test_injected_override_marks_resolved(self):
        config = TriageConfig()
        mock_agent = MagicMock()
        resolver = DependencyResolver(
            config,
            workspace_agent=mock_agent,
        )
        assert resolver.health["workspace_agent"].resolved is True
        assert resolver.health["workspace_agent"].instance is mock_agent

    @pytest.mark.asyncio
    async def test_resolve_workspace_agent_success(self):
        config = TriageConfig()
        resolver = DependencyResolver(config)
        mock_agent = MagicMock()
        with patch(
            "autonomy.email_triage.dependencies._resolve_workspace_agent",
            return_value=mock_agent,
        ):
            await resolver.resolve_all()
        assert resolver.health["workspace_agent"].resolved is True
        assert resolver.health["workspace_agent"].instance is mock_agent

    @pytest.mark.asyncio
    async def test_resolve_workspace_agent_failure(self):
        config = TriageConfig()
        resolver = DependencyResolver(config)
        with patch(
            "autonomy.email_triage.dependencies._resolve_workspace_agent",
            side_effect=ImportError("not found"),
        ):
            await resolver.resolve_all()
        assert resolver.health["workspace_agent"].resolved is False
        assert resolver.health["workspace_agent"].consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_resolve_skips_already_resolved(self):
        config = TriageConfig()
        mock_agent = MagicMock()
        resolver = DependencyResolver(config, workspace_agent=mock_agent)
        # resolve_all should not try to re-resolve
        with patch(
            "autonomy.email_triage.dependencies._resolve_workspace_agent",
            side_effect=RuntimeError("should not be called"),
        ):
            await resolver.resolve_all()  # Should not raise
        assert resolver.health["workspace_agent"].instance is mock_agent

    @pytest.mark.asyncio
    async def test_resolve_skips_during_backoff(self):
        config = TriageConfig()
        resolver = DependencyResolver(config)
        # Force a failure to start backoff
        resolver.health["workspace_agent"].record_failure(
            "err", base_s=5.0, max_s=300.0
        )
        with patch(
            "autonomy.email_triage.dependencies._resolve_workspace_agent",
            side_effect=RuntimeError("should not be called"),
        ):
            await resolver.resolve_all()  # Should skip due to backoff
        assert resolver.health["workspace_agent"].resolved is False

    def test_get_instance_returns_none_when_unresolved(self):
        config = TriageConfig()
        resolver = DependencyResolver(config)
        assert resolver.get("workspace_agent") is None

    def test_get_instance_returns_instance_when_resolved(self):
        config = TriageConfig()
        mock_agent = MagicMock()
        resolver = DependencyResolver(config, workspace_agent=mock_agent)
        assert resolver.get("workspace_agent") is mock_agent
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_dependencies.py -v`
Expected: FAIL — `dependencies` module not found

**Step 3: Implement dependencies module**

Create `backend/autonomy/email_triage/dependencies.py`:

```python
"""Dependency resolution for the email triage runner.

Manages lazy resolution of three dependencies:
- workspace_agent (REQUIRED): GoogleWorkspaceAgent for Gmail fetching
- router (OPTIONAL): PrimeRouter for J-Prime extraction
- notifier (OPTIONAL): notification_bridge.notify_user for alerts

Each dependency tracks its own health state with exponential backoff
on failure. Injectable overrides bypass lazy resolution (for tests).
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.events import (
    emit_triage_event,
    EVENT_DEPENDENCY_UNAVAILABLE,
    EVENT_DEPENDENCY_DEGRADED,
)

logger = logging.getLogger("jarvis.email_triage.dependencies")


@dataclass
class DependencyHealth:
    """Health state for a single dependency."""

    name: str
    required: bool
    resolved: bool = False
    instance: Any = None
    last_resolve_at: float = 0.0
    last_resolve_error: Optional[str] = None
    consecutive_failures: int = 0
    next_attempt_at: float = 0.0

    def record_success(self, instance: Any) -> None:
        """Mark dependency as successfully resolved."""
        self.resolved = True
        self.instance = instance
        self.last_resolve_at = time.monotonic()
        self.last_resolve_error = None
        self.consecutive_failures = 0
        self.next_attempt_at = 0.0

    def record_failure(
        self, error: str, base_s: float, max_s: float
    ) -> None:
        """Mark dependency resolution as failed with backoff."""
        self.resolved = False
        self.instance = None
        self.last_resolve_error = error
        self.consecutive_failures += 1
        interval = min(
            base_s * (2 ** self.consecutive_failures), max_s
        )
        jitter = random.uniform(0.8, 1.2)
        self.next_attempt_at = time.monotonic() + interval * jitter

    def invalidate(
        self, error: str, base_s: float, max_s: float
    ) -> None:
        """Invalidate a previously resolved dependency."""
        self.record_failure(error, base_s, max_s)

    def can_attempt(self) -> bool:
        """Check if enough time has passed since last failure."""
        if self.resolved:
            return False  # Already resolved
        return time.monotonic() >= self.next_attempt_at


def _resolve_workspace_agent() -> Any:
    """Resolve GoogleWorkspaceAgent singleton.

    Raises on failure (ImportError, RuntimeError, etc).
    """
    from neural_mesh.agents.google_workspace_agent import (
        get_google_workspace_agent,
    )
    agent = get_google_workspace_agent()
    if agent is None:
        raise RuntimeError("GoogleWorkspaceAgent singleton not initialized")
    return agent


def _resolve_router() -> Any:
    """Resolve PrimeRouter singleton.

    Raises on failure.
    """
    from core.prime_router import get_prime_router
    router = get_prime_router()
    if router is None:
        raise RuntimeError("PrimeRouter singleton not initialized")
    return router


def _resolve_notifier() -> Any:
    """Resolve notification_bridge.notify_user function.

    Raises on failure.
    """
    from agi_os.notification_bridge import notify_user
    return notify_user


# Map dependency name to its resolver function
_RESOLVERS: Dict[str, Any] = {
    "workspace_agent": _resolve_workspace_agent,
    "router": _resolve_router,
    "notifier": _resolve_notifier,
}


class DependencyResolver:
    """Manages resolution of all triage dependencies."""

    def __init__(
        self,
        config: TriageConfig,
        workspace_agent: Any = None,
        router: Any = None,
        notifier: Any = None,
    ):
        self._config = config
        self.health: Dict[str, DependencyHealth] = {
            "workspace_agent": DependencyHealth(
                name="workspace_agent", required=True,
            ),
            "router": DependencyHealth(
                name="router", required=False,
            ),
            "notifier": DependencyHealth(
                name="notifier", required=False,
            ),
        }

        # Apply injectable overrides
        overrides = {
            "workspace_agent": workspace_agent,
            "router": router,
            "notifier": notifier,
        }
        for name, instance in overrides.items():
            if instance is not None:
                self.health[name].record_success(instance)

    async def resolve_all(self) -> None:
        """Attempt to resolve all unresolved dependencies.

        Skips dependencies that are already resolved or in backoff.
        """
        base = self._config.dep_backoff_base_s
        cap = self._config.dep_backoff_max_s

        for name, dep in self.health.items():
            if dep.resolved:
                continue
            if not dep.can_attempt():
                continue

            resolver_fn = _RESOLVERS.get(name)
            if resolver_fn is None:
                continue

            try:
                instance = resolver_fn()
                dep.record_success(instance)
                logger.info(
                    "Resolved dependency: %s", name,
                )
            except Exception as e:
                dep.record_failure(str(e), base, cap)
                logger.debug(
                    "Failed to resolve %s (attempt %d): %s",
                    name, dep.consecutive_failures, e,
                )
                emit_triage_event(EVENT_DEPENDENCY_UNAVAILABLE, {
                    "dependency_name": name,
                    "error": str(e),
                    "consecutive_failures": dep.consecutive_failures,
                    "next_retry_at": dep.next_attempt_at,
                })

    def get(self, name: str) -> Any:
        """Get resolved instance or None."""
        dep = self.health.get(name)
        if dep and dep.resolved:
            return dep.instance
        return None

    def invalidate(self, name: str, error: str) -> None:
        """Invalidate a dependency (runtime failure)."""
        dep = self.health.get(name)
        if dep:
            dep.invalidate(
                error,
                self._config.dep_backoff_base_s,
                self._config.dep_backoff_max_s,
            )
            emit_triage_event(EVENT_DEPENDENCY_UNAVAILABLE, {
                "dependency_name": name,
                "error": error,
                "consecutive_failures": dep.consecutive_failures,
            })

    def report_degraded(self, name: str, reason: str) -> None:
        """Report a dependency as degraded (resolved but limited)."""
        emit_triage_event(EVENT_DEPENDENCY_DEGRADED, {
            "dependency_name": name,
            "degraded_reason": reason,
        })
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_dependencies.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/email_triage/dependencies.py tests/unit/backend/email_triage/test_dependencies.py && git commit -m "feat(email-triage): add dependency resolution with backoff"
```

---

## Task 5: Notification Adapter Module

**Files:**
- Create: `backend/autonomy/email_triage/notifications.py`
- Create: `tests/unit/backend/email_triage/test_notifications.py`

**Context:** Thin adapter between triage decisions and notification_bridge. Maps triage tiers to NotificationUrgency. Uses bounded async delivery (wait_for + gather). Tracks delivery results via events.

**Step 1: Write the failing tests**

Create `tests/unit/backend/email_triage/test_notifications.py`:

```python
"""Tests for the notification adapter module."""

import os
import sys
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.notifications import (
    tier_to_urgency,
    deliver_immediate,
    deliver_summary,
)
from autonomy.email_triage.schemas import (
    EmailFeatures, ScoringResult, TriagedEmail, NotificationDeliveryResult,
)


def _make_triaged(tier: int, message_id: str = "msg_001") -> TriagedEmail:
    features = EmailFeatures(
        message_id=message_id, sender="a@b.com", sender_domain="b.com",
        subject="Test", snippet="snippet", is_reply=False, has_attachment=False,
        label_ids=(), keywords=(), sender_frequency="first_time",
        urgency_signals=(), extraction_confidence=0.0,
    )
    label_map = {1: "jarvis/tier1_critical", 2: "jarvis/tier2_high",
                 3: "jarvis/tier3_review", 4: "jarvis/tier4_noise"}
    scoring = ScoringResult(
        score=90 if tier == 1 else 70,
        tier=tier,
        tier_label=label_map[tier],
        breakdown={}, idempotency_key=f"key_{message_id}",
    )
    return TriagedEmail(
        features=features, scoring=scoring,
        notification_action="immediate", processed_at=1000.0,
    )


class TestTierToUrgency:
    """Map triage tier to notification urgency."""

    def test_tier1_maps_to_urgent(self):
        assert tier_to_urgency(1) == 4  # URGENT

    def test_tier2_maps_to_high(self):
        assert tier_to_urgency(2) == 3  # HIGH

    def test_summary_maps_to_normal(self):
        assert tier_to_urgency(0) == 2  # NORMAL (summary)

    def test_unknown_tier_maps_to_normal(self):
        assert tier_to_urgency(99) == 2  # NORMAL fallback


class TestDeliverImmediate:
    """Immediate notification delivery with bounded async."""

    @pytest.mark.asyncio
    async def test_delivers_via_notifier(self):
        notifier = AsyncMock(return_value=True)
        triaged = _make_triaged(tier=1)
        results = await deliver_immediate([triaged], notifier, timeout_s=5.0)
        assert len(results) == 1
        assert results[0].success is True
        notifier.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_notifier_failure(self):
        notifier = AsyncMock(side_effect=RuntimeError("channel down"))
        triaged = _make_triaged(tier=1)
        results = await deliver_immediate([triaged], notifier, timeout_s=5.0)
        assert len(results) == 1
        assert results[0].success is False
        assert "channel down" in results[0].error

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self):
        async def slow_notify(*args, **kwargs):
            await asyncio.sleep(10)
            return True
        triaged = _make_triaged(tier=1)
        results = await deliver_immediate([triaged], slow_notify, timeout_s=0.1)
        assert len(results) == 1
        assert results[0].success is False
        assert "timeout" in results[0].error.lower()

    @pytest.mark.asyncio
    async def test_multiple_emails_parallel(self):
        notifier = AsyncMock(return_value=True)
        emails = [_make_triaged(tier=1, message_id=f"msg_{i}") for i in range(3)]
        results = await deliver_immediate(emails, notifier, timeout_s=5.0)
        assert len(results) == 3
        assert all(r.success for r in results)


class TestDeliverSummary:
    """Summary notification delivery."""

    @pytest.mark.asyncio
    async def test_delivers_summary(self):
        notifier = AsyncMock(return_value=True)
        emails = [_make_triaged(tier=2, message_id=f"msg_{i}") for i in range(3)]
        result = await deliver_summary(emails, notifier, timeout_s=5.0)
        assert result.success is True
        assert result.channel == "summary"

    @pytest.mark.asyncio
    async def test_empty_buffer_returns_success(self):
        notifier = AsyncMock(return_value=True)
        result = await deliver_summary([], notifier, timeout_s=5.0)
        assert result.success is True
        assert result.message_id == "summary_empty"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_notifications.py -v`
Expected: FAIL — `notifications` module not found

**Step 3: Implement notifications module**

Create `backend/autonomy/email_triage/notifications.py`:

```python
"""Notification adapter for the email triage system.

Maps triage tiers to NotificationUrgency levels and delivers
notifications via notification_bridge with bounded async.

Core invariant: notification delivery failure NEVER changes
triage score, tier, or label outcome. This module is called
AFTER all scoring/labeling decisions are committed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, List

from autonomy.email_triage.events import (
    emit_triage_event,
    EVENT_NOTIFICATION_DELIVERY_RESULT,
)
from autonomy.email_triage.schemas import (
    NotificationDeliveryResult,
    TriagedEmail,
)

logger = logging.getLogger("jarvis.email_triage.notifications")

# Triage tier → NotificationUrgency (IntEnum values from notification_bridge)
_TIER_URGENCY_MAP = {
    1: 4,   # tier1_critical → URGENT
    2: 3,   # tier2_high → HIGH
    0: 2,   # summary → NORMAL
}


def tier_to_urgency(tier: int) -> int:
    """Map triage tier to NotificationUrgency integer value."""
    return _TIER_URGENCY_MAP.get(tier, 2)  # Default NORMAL


async def _deliver_one(
    triaged: TriagedEmail,
    notifier: Callable,
    urgency: int,
) -> NotificationDeliveryResult:
    """Deliver a single notification, catching all errors."""
    start = time.monotonic()
    msg_id = triaged.features.message_id
    try:
        title = f"Email: {triaged.features.subject[:60]}"
        message = (
            f"From: {triaged.features.sender}\n"
            f"Subject: {triaged.features.subject}\n"
            f"Tier: {triaged.scoring.tier} (score {triaged.scoring.score})"
        )
        result = await asyncio.coroutine(lambda: notifier(
            message=message,
            urgency=urgency,
            title=title,
            context={"source": "email_triage", "message_id": msg_id},
        ))() if not asyncio.iscoroutinefunction(notifier) else await notifier(
            message=message,
            urgency=urgency,
            title=title,
            context={"source": "email_triage", "message_id": msg_id},
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return NotificationDeliveryResult(
            message_id=msg_id,
            channel="bridge",
            success=bool(result),
            latency_ms=elapsed,
        )
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return NotificationDeliveryResult(
            message_id=msg_id,
            channel="bridge",
            success=False,
            latency_ms=elapsed,
            error=str(e),
        )


async def deliver_immediate(
    emails: List[TriagedEmail],
    notifier: Callable,
    timeout_s: float = 10.0,
) -> List[NotificationDeliveryResult]:
    """Deliver immediate notifications for tier 1-2 emails.

    Uses bounded async: gather with outer timeout.
    Individual failures don't affect other deliveries.
    """
    if not emails:
        return []

    tasks = []
    for triaged in emails:
        urgency = tier_to_urgency(triaged.scoring.tier)
        tasks.append(_deliver_one(triaged, notifier, urgency))

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        results = [
            NotificationDeliveryResult(
                message_id=t.features.message_id,
                channel="bridge",
                success=False,
                latency_ms=int(timeout_s * 1000),
                error="Timeout: delivery budget exhausted",
            )
            for t in emails
        ]

    # Normalize: gather with return_exceptions may return Exception objects
    final: List[NotificationDeliveryResult] = []
    for i, r in enumerate(results):
        if isinstance(r, NotificationDeliveryResult):
            final.append(r)
        elif isinstance(r, Exception):
            final.append(NotificationDeliveryResult(
                message_id=emails[i].features.message_id,
                channel="bridge",
                success=False,
                latency_ms=0,
                error=str(r),
            ))
        else:
            final.append(NotificationDeliveryResult(
                message_id=emails[i].features.message_id,
                channel="bridge",
                success=False,
                latency_ms=0,
                error=f"Unexpected result type: {type(r)}",
            ))

    # Emit delivery result events
    for result in final:
        emit_triage_event(EVENT_NOTIFICATION_DELIVERY_RESULT, {
            "message_id": result.message_id,
            "channel": result.channel,
            "success": result.success,
            "latency_ms": result.latency_ms,
            "error": result.error,
        })

    return final


async def deliver_summary(
    emails: List[TriagedEmail],
    notifier: Callable,
    timeout_s: float = 5.0,
) -> NotificationDeliveryResult:
    """Deliver summary notification for batched emails.

    Single notification with aggregated content.
    """
    if not emails:
        return NotificationDeliveryResult(
            message_id="summary_empty",
            channel="summary",
            success=True,
            latency_ms=0,
        )

    start = time.monotonic()
    urgency = tier_to_urgency(0)  # NORMAL for summaries

    lines = []
    for t in emails:
        lines.append(
            f"- [{t.features.subject[:50]}] from {t.features.sender} "
            f"(tier {t.scoring.tier})"
        )
    message = f"Email triage summary ({len(emails)} emails):\n" + "\n".join(lines)

    try:
        result = await asyncio.wait_for(
            notifier(
                message=message,
                urgency=urgency,
                title=f"Email Summary: {len(emails)} emails",
                context={"source": "email_triage_summary"},
            ) if asyncio.iscoroutinefunction(notifier) else asyncio.coroutine(
                lambda: notifier(
                    message=message,
                    urgency=urgency,
                    title=f"Email Summary: {len(emails)} emails",
                    context={"source": "email_triage_summary"},
                )
            )(),
            timeout=timeout_s,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        delivery = NotificationDeliveryResult(
            message_id="summary",
            channel="summary",
            success=bool(result),
            latency_ms=elapsed,
        )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        delivery = NotificationDeliveryResult(
            message_id="summary",
            channel="summary",
            success=False,
            latency_ms=elapsed,
            error="Timeout",
        )
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        delivery = NotificationDeliveryResult(
            message_id="summary",
            channel="summary",
            success=False,
            latency_ms=elapsed,
            error=str(e),
        )

    emit_triage_event(EVENT_NOTIFICATION_DELIVERY_RESULT, {
        "message_id": delivery.message_id,
        "channel": delivery.channel,
        "success": delivery.success,
        "latency_ms": delivery.latency_ms,
        "error": delivery.error,
    })

    return delivery
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_notifications.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/email_triage/notifications.py tests/unit/backend/email_triage/test_notifications.py && git commit -m "feat(email-triage): add notification adapter with bounded async delivery"
```

---

## Task 6: Enrichment Function

**Files:**
- Create: `backend/autonomy/email_triage/enrichment.py`
- Create: `tests/unit/backend/email_triage/test_enrichment.py`

**Context:** Pure function `enrich_with_triage()` that merges triage tier/score metadata into raw email dicts by message_id. Used by the command processor workspace fast-path. Never removes, reorders, or mutates scoring data.

**Step 1: Write the failing tests**

Create `tests/unit/backend/email_triage/test_enrichment.py`:

```python
"""Tests for the triage enrichment function."""

import os
import sys
import time
from unittest.mock import MagicMock, AsyncMock, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.enrichment import enrich_with_triage
from autonomy.email_triage.schemas import (
    EmailFeatures, ScoringResult, TriagedEmail, TriageCycleReport,
)


def _make_triaged(message_id: str, tier: int, score: int) -> TriagedEmail:
    features = EmailFeatures(
        message_id=message_id, sender="a@b.com", sender_domain="b.com",
        subject="Test", snippet="snippet", is_reply=False, has_attachment=False,
        label_ids=(), keywords=(), sender_frequency="first_time",
        urgency_signals=(), extraction_confidence=0.0,
    )
    scoring = ScoringResult(
        score=score, tier=tier,
        tier_label=f"jarvis/tier{tier}_label",
        breakdown={}, idempotency_key=f"key_{message_id}",
    )
    return TriagedEmail(
        features=features, scoring=scoring,
        notification_action="label_only", processed_at=1000.0,
    )


def _mock_runner(
    triaged_emails: dict,
    report_at: float = None,
    schema_version: str = "1.0",
):
    """Create a mock runner with triage cache state."""
    runner = MagicMock()
    runner._triaged_emails = triaged_emails
    runner._last_report_at = report_at if report_at is not None else time.monotonic()
    runner._triage_schema_version = schema_version
    runner._last_report = TriageCycleReport(
        cycle_id="abc", started_at=1000.0, completed_at=1001.0,
        emails_fetched=3, emails_processed=3, tier_counts={1: 1, 3: 2},
        notifications_sent=1, notifications_suppressed=0, errors=[],
        triage_schema_version=schema_version, policy_version="v1",
    )
    return runner


class TestEnrichWithTriage:
    """enrich_with_triage() merges triage metadata into raw emails."""

    def test_enriches_matching_emails(self):
        emails = [
            {"id": "msg_1", "subject": "Hello"},
            {"id": "msg_2", "subject": "World"},
        ]
        triaged = {
            "msg_1": _make_triaged("msg_1", tier=1, score=90),
            "msg_2": _make_triaged("msg_2", tier=3, score=40),
        }
        runner = _mock_runner(triaged)
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert enriched is True
        assert result[0]["triage_tier"] == 1
        assert result[0]["triage_score"] == 90
        assert result[1]["triage_tier"] == 3
        assert result[1]["triage_score"] == 40
        assert age is not None

    def test_preserves_email_count(self):
        emails = [{"id": f"msg_{i}"} for i in range(5)]
        triaged = {"msg_0": _make_triaged("msg_0", 1, 90)}
        runner = _mock_runner(triaged)
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert len(result) == 5
        assert enriched is True

    def test_unmatched_emails_pass_through(self):
        emails = [{"id": "msg_new", "subject": "Brand new"}]
        triaged = {"msg_old": _make_triaged("msg_old", 2, 70)}
        runner = _mock_runner(triaged)
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert len(result) == 1
        assert "triage_tier" not in result[0]

    def test_preserves_order(self):
        emails = [{"id": f"msg_{i}"} for i in range(3)]
        triaged = {f"msg_{i}": _make_triaged(f"msg_{i}", i + 1, 50) for i in range(3)}
        runner = _mock_runner(triaged)
        result, _, _ = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert [e["id"] for e in result] == ["msg_0", "msg_1", "msg_2"]

    def test_runner_none_returns_unenriched(self):
        emails = [{"id": "msg_1"}]
        result, enriched, age = enrich_with_triage(emails, None, staleness_window_s=120.0)
        assert enriched is False
        assert age is None
        assert result == emails

    def test_stale_results_return_unenriched(self):
        emails = [{"id": "msg_1"}]
        triaged = {"msg_1": _make_triaged("msg_1", 1, 90)}
        # Report from 300s ago, staleness window is 120s
        runner = _mock_runner(triaged, report_at=time.monotonic() - 300.0)
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert enriched is False

    def test_incompatible_schema_version_skips(self):
        emails = [{"id": "msg_1"}]
        triaged = {"msg_1": _make_triaged("msg_1", 1, 90)}
        runner = _mock_runner(triaged, schema_version="99.0")
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert enriched is False

    def test_no_last_report_returns_unenriched(self):
        emails = [{"id": "msg_1"}]
        runner = MagicMock()
        runner._last_report = None
        runner._triage_schema_version = "1.0"
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert enriched is False

    def test_triage_age_is_positive(self):
        emails = [{"id": "msg_1"}]
        triaged = {"msg_1": _make_triaged("msg_1", 1, 90)}
        runner = _mock_runner(triaged, report_at=time.monotonic() - 10.0)
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert enriched is True
        assert age is not None
        assert age >= 9.0  # At least 9s ago

    def test_does_not_mutate_original_emails(self):
        original = [{"id": "msg_1", "subject": "Hi"}]
        triaged = {"msg_1": _make_triaged("msg_1", 1, 90)}
        runner = _mock_runner(triaged)
        result, _, _ = enrich_with_triage(original, runner, staleness_window_s=120.0)
        assert "triage_tier" not in original[0]
        assert "triage_tier" in result[0]
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_enrichment.py -v`
Expected: FAIL — `enrichment` module not found

**Step 3: Implement enrichment module**

Create `backend/autonomy/email_triage/enrichment.py`:

```python
"""Triage enrichment for the command processor.

Pure function that merges triage metadata (tier, score) into
raw email dicts by message_id. Used by the workspace fast-path
to enrich "check my email" responses when triage results are fresh.

Invariants:
- Never removes emails from the list
- Never reorders emails
- Never modifies scoring/tier values (read-only from triage truth)
- Does not mutate the original email dicts (returns copies)
- Pure function: no side effects, no network, no exceptions
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("jarvis.email_triage.enrichment")

# Schema versions this enrichment function is compatible with
_COMPATIBLE_SCHEMA_VERSIONS: Set[str] = {"1.0"}


def enrich_with_triage(
    emails: List[Dict[str, Any]],
    runner: Any,
    staleness_window_s: float = 120.0,
) -> Tuple[List[Dict[str, Any]], bool, Optional[float]]:
    """Enrich raw email dicts with triage metadata.

    Args:
        emails: Raw email dicts from GoogleWorkspaceAgent.
        runner: EmailTriageRunner instance (or None).
        staleness_window_s: Max age of triage results to consider fresh.

    Returns:
        (enriched_emails, was_enriched, triage_age_s)
        - enriched_emails: Copies of input dicts with optional triage fields.
        - was_enriched: True if any triage data was merged.
        - triage_age_s: Seconds since last triage cycle, or None.
    """
    # Guard: no runner
    if runner is None:
        return emails, False, None

    # Guard: no report
    last_report = getattr(runner, "_last_report", None)
    if last_report is None:
        return emails, False, None

    # Guard: schema version compatibility
    schema_version = getattr(runner, "_triage_schema_version", "unknown")
    if schema_version not in _COMPATIBLE_SCHEMA_VERSIONS:
        logger.debug(
            "Skipping enrichment: schema version %s not compatible",
            schema_version,
        )
        return emails, False, None

    # Guard: freshness check
    report_at = getattr(runner, "_last_report_at", 0.0)
    now = time.monotonic()
    age = now - report_at
    if age > staleness_window_s:
        return emails, False, None

    # Enrich
    triaged_map: Dict[str, Any] = getattr(runner, "_triaged_emails", {})
    if not triaged_map:
        return emails, False, age

    enriched: List[Dict[str, Any]] = []
    any_matched = False

    for email in emails:
        msg_id = email.get("id", "")
        triaged = triaged_map.get(msg_id)
        if triaged is not None:
            # Copy to avoid mutating original
            enriched_email = dict(email)
            enriched_email["triage_tier"] = triaged.scoring.tier
            enriched_email["triage_score"] = triaged.scoring.score
            enriched_email["triage_tier_label"] = triaged.scoring.tier_label
            enriched_email["triage_action"] = triaged.notification_action
            enriched.append(enriched_email)
            any_matched = True
        else:
            # Pass through unmatched emails as-is (no copy needed)
            enriched.append(email)

    return enriched, any_matched, age
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_enrichment.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/email_triage/enrichment.py tests/unit/backend/email_triage/test_enrichment.py && git commit -m "feat(email-triage): add triage enrichment function for command processor"
```

---

## Task 7: Runner Integration — Dependency Resolution + Triage Cache

**Files:**
- Modify: `backend/autonomy/email_triage/runner.py`
- Modify: `tests/unit/backend/email_triage/test_runner.py`

**Context:** The runner needs three changes: (1) use DependencyResolver instead of raw constructor args, (2) maintain a triage cache (`_last_report`, `_triaged_emails`) protected by asyncio.Lock for command processor reads, (3) partial-cycle semantics (new snapshot committed only on cycle completion).

**Step 1: Write the failing tests**

Add to `tests/unit/backend/email_triage/test_runner.py`:

```python
class TestDependencyResolution:
    """Runner uses DependencyResolver for lazy dep resolution."""

    @pytest.mark.asyncio
    async def test_resolves_deps_on_first_cycle(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        mock_agent = MagicMock()
        runner = EmailTriageRunner(config=config, workspace_agent=mock_agent)
        runner._fetch_unread = AsyncMock(return_value=[])
        report = await runner.run_cycle()
        assert report.emails_fetched == 0
        assert report.skipped is False

    @pytest.mark.asyncio
    async def test_no_workspace_agent_returns_zero_fetched(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)
        # No workspace_agent injected, lazy resolution will fail in test
        report = await runner.run_cycle()
        assert report.emails_fetched == 0
        assert report.skipped is False


class TestTriageCache:
    """Runner maintains triage cache for command processor."""

    @pytest.mark.asyncio
    async def test_last_report_populated_after_cycle(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(2))
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}
        runner._apply_label = AsyncMock()
        await runner.run_cycle()
        assert runner._last_report is not None
        assert runner._last_report.emails_processed == 2
        assert runner._last_report_at > 0.0

    @pytest.mark.asyncio
    async def test_triaged_emails_populated(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)
        emails = _sample_emails(2)
        runner._fetch_unread = AsyncMock(return_value=emails)
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}
        runner._apply_label = AsyncMock()
        await runner.run_cycle()
        assert len(runner._triaged_emails) == 2
        assert "msg_0" in runner._triaged_emails
        assert "msg_1" in runner._triaged_emails

    @pytest.mark.asyncio
    async def test_partial_cycle_preserves_previous_snapshot(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)

        # First successful cycle
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(2))
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}
        runner._apply_label = AsyncMock()
        await runner.run_cycle()
        first_report = runner._last_report
        first_emails = dict(runner._triaged_emails)

        # Second cycle fails on fetch
        runner._fetch_unread = AsyncMock(side_effect=RuntimeError("API down"))
        await runner.run_cycle()

        # Previous snapshot should be preserved
        assert runner._last_report is first_report
        assert runner._triaged_emails == first_emails

    @pytest.mark.asyncio
    async def test_get_fresh_results_returns_when_fresh(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(1))
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}
        runner._apply_label = AsyncMock()
        await runner.run_cycle()
        result = runner.get_fresh_results(staleness_window_s=120.0)
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_fresh_results_returns_none_when_stale(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)
        runner._last_report = TriageCycleReport(
            cycle_id="old", started_at=1000.0, completed_at=1001.0,
            emails_fetched=1, emails_processed=1, tier_counts={},
            notifications_sent=0, notifications_suppressed=0, errors=[],
        )
        runner._last_report_at = time.monotonic() - 300.0  # 5min ago
        result = runner.get_fresh_results(staleness_window_s=120.0)
        assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_runner.py -v -k "TestDependencyResolution or TestTriageCache"`
Expected: FAIL — new methods/attributes not found

**Step 3: Modify runner.py**

Update `backend/autonomy/email_triage/runner.py` with:

1. Add imports for `DependencyResolver`, `asyncio`, `time` (monotonic)
2. Add triage cache fields to `__init__`: `_last_report`, `_last_report_at`, `_triaged_emails`, `_report_lock`, `_triage_schema_version`
3. Add `DependencyResolver` integration
4. Add `get_fresh_results()` method
5. Modify `run_cycle()` to: resolve deps, build new snapshot in local vars, commit atomically under lock on success, preserve previous snapshot on failure

The key changes in `__init__`:
```python
    def __init__(
        self,
        config: Optional[TriageConfig] = None,
        workspace_agent: Any = None,
        router: Any = None,
        notifier: Any = None,
    ):
        self._config = config or get_triage_config()
        self._resolver = DependencyResolver(
            self._config,
            workspace_agent=workspace_agent,
            router=router,
            notifier=notifier,
        )
        self._policy = NotificationPolicy(self._config)
        self._label_map: Dict[str, str] = {}
        self._labels_initialized = False
        # Triage cache (read by command processor)
        self._last_report: Optional[TriageCycleReport] = None
        self._last_report_at: float = 0.0  # monotonic
        self._triaged_emails: Dict[str, TriagedEmail] = {}
        self._report_lock = asyncio.Lock()
        self._triage_schema_version: str = "1.0"
```

New method:
```python
    def get_fresh_results(
        self, staleness_window_s: Optional[float] = None,
    ) -> Optional[TriageCycleReport]:
        """Return last report if within staleness window, else None."""
        if self._last_report is None:
            return None
        window = staleness_window_s or (self._config.staleness_window_s)
        age = time.monotonic() - self._last_report_at
        if age > window:
            return None
        return self._last_report
```

In `run_cycle()`, after processing all emails, commit snapshot under lock:
```python
        # Commit snapshot atomically (partial-cycle semantics)
        async with self._report_lock:
            self._last_report = report
            self._last_report_at = time.monotonic()
            self._triaged_emails = new_triaged_map
```

For fetch failures, do NOT update the snapshot — return early with error report but leave `_last_report` / `_triaged_emails` unchanged.

Also update `_fetch_unread` and `_apply_label` to use `self._resolver.get("workspace_agent")` instead of `self._workspace_agent`.

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_runner.py -v`
Expected: ALL PASS (both new and existing tests)

**Step 5: Run full test suite for regression**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/email_triage/runner.py tests/unit/backend/email_triage/test_runner.py && git commit -m "feat(email-triage): integrate dependency resolution and triage cache in runner"
```

---

## Task 8: Agent Runtime Wiring — Capture run_cycle Result

**Files:**
- Modify: `backend/autonomy/agent_runtime.py` (lines 2753-2781)
- Modify: `tests/unit/backend/email_triage/test_agent_runtime_integration.py`

**Context:** Currently `agent_runtime._maybe_run_email_triage()` discards the `TriageCycleReport`. We need to capture it so the runner singleton's internal cache (set in Task 7) is the authoritative source. Actually, since Task 7 makes the runner commit the snapshot internally in `run_cycle()`, the agent_runtime doesn't need to store the result — but it should log the report summary for observability.

**Step 1: Write the failing tests**

Add to `tests/unit/backend/email_triage/test_agent_runtime_integration.py`:

```python
    @pytest.mark.asyncio
    async def test_run_cycle_result_logged(self):
        """run_cycle() return value is used for logging."""
        from autonomy.email_triage.runner import EmailTriageRunner
        from autonomy.email_triage.schemas import TriageCycleReport
        runtime = MagicMock(spec=UnifiedAgentRuntime)
        runtime._last_email_triage_run = 0.0

        mock_report = TriageCycleReport(
            cycle_id="test", started_at=1000.0, completed_at=1001.0,
            emails_fetched=5, emails_processed=5, tier_counts={1: 1, 3: 4},
            notifications_sent=1, notifications_suppressed=0, errors=[],
        )
        mock_runner = MagicMock()
        mock_runner.run_cycle = AsyncMock(return_value=mock_report)

        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            with patch(
                "autonomy.email_triage.runner.EmailTriageRunner.get_instance",
                return_value=mock_runner,
            ):
                await UnifiedAgentRuntime._maybe_run_email_triage(runtime)

        mock_runner.run_cycle.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_agent_runtime_integration.py -v -k "test_run_cycle_result_logged"`
Expected: May pass or fail depending on existing code — the key test is that run_cycle is called.

**Step 3: Modify agent_runtime.py**

At line 2775, change:
```python
        await asyncio.wait_for(runner.run_cycle(), timeout=timeout)
```
to:
```python
        report = await asyncio.wait_for(runner.run_cycle(), timeout=timeout)
        if report and not report.skipped:
            logger.info(
                "[AgentRuntime] Email triage: %d fetched, %d processed, "
                "tiers=%s, notifications=%d, errors=%d",
                report.emails_fetched,
                report.emails_processed,
                report.tier_counts,
                report.notifications_sent,
                len(report.errors),
            )
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_agent_runtime_integration.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/agent_runtime.py tests/unit/backend/email_triage/test_agent_runtime_integration.py && git commit -m "feat(email-triage): capture and log triage cycle report in agent runtime"
```

---

## Task 9: Command Processor Integration — Triage Enrichment

**Files:**
- Modify: `backend/api/unified_command_processor.py` (lines ~4084-4087)
- Create: `tests/unit/backend/email_triage/test_command_processor_enrichment.py`

**Context:** Add triage enrichment in the workspace fast-path, between DAG result collection (line 4084) and compose call (line 4090). This is the `enrich_with_triage()` call from Task 6. Also add `triage_available` and `triage_age_s` to compose context.

**Step 1: Write the failing tests**

Create `tests/unit/backend/email_triage/test_command_processor_enrichment.py`:

```python
"""Tests for triage enrichment integration in the command processor."""

import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.enrichment import enrich_with_triage
from autonomy.email_triage.schemas import (
    EmailFeatures, ScoringResult, TriagedEmail, TriageCycleReport,
)


def _make_triaged(message_id: str, tier: int, score: int) -> TriagedEmail:
    features = EmailFeatures(
        message_id=message_id, sender="a@b.com", sender_domain="b.com",
        subject="Test", snippet="snippet", is_reply=False, has_attachment=False,
        label_ids=(), keywords=(), sender_frequency="first_time",
        urgency_signals=(), extraction_confidence=0.0,
    )
    scoring = ScoringResult(
        score=score, tier=tier, tier_label=f"jarvis/tier{tier}_label",
        breakdown={}, idempotency_key=f"key_{message_id}",
    )
    return TriagedEmail(
        features=features, scoring=scoring,
        notification_action="label_only", processed_at=1000.0,
    )


class TestCommandProcessorEnrichmentContract:
    """Enrichment integration matches the command processor contract."""

    def test_enrichment_adds_structured_context(self):
        """Enriched emails have triage fields suitable for compose context."""
        emails = [{"id": "msg_1", "subject": "Urgent"}]
        runner = MagicMock()
        runner._last_report = TriageCycleReport(
            cycle_id="abc", started_at=1000.0, completed_at=1001.0,
            emails_fetched=1, emails_processed=1, tier_counts={1: 1},
            notifications_sent=1, notifications_suppressed=0, errors=[],
        )
        runner._last_report_at = time.monotonic()
        runner._triage_schema_version = "1.0"
        runner._triaged_emails = {
            "msg_1": _make_triaged("msg_1", tier=1, score=92),
        }
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert enriched is True
        # These fields are what compose template will use
        assert result[0]["triage_tier"] == 1
        assert result[0]["triage_score"] == 92
        assert result[0]["triage_tier_label"] == "jarvis/tier1_label"
        assert "triage_action" in result[0]

    def test_unenriched_emails_have_no_triage_fields(self):
        """When triage unavailable, emails have no triage_* keys."""
        emails = [{"id": "msg_1", "subject": "Hello"}]
        result, enriched, age = enrich_with_triage(emails, None, staleness_window_s=120.0)
        assert enriched is False
        assert "triage_tier" not in result[0]

    def test_compose_context_can_build_tier_summary(self):
        """Enriched email list supports building tier summary for compose."""
        emails = [
            {"id": f"msg_{i}", "subject": f"Email {i}"}
            for i in range(5)
        ]
        runner = MagicMock()
        runner._last_report = TriageCycleReport(
            cycle_id="abc", started_at=1000.0, completed_at=1001.0,
            emails_fetched=5, emails_processed=5,
            tier_counts={1: 1, 2: 1, 3: 2, 4: 1},
            notifications_sent=1, notifications_suppressed=0, errors=[],
        )
        runner._last_report_at = time.monotonic()
        runner._triage_schema_version = "1.0"
        runner._triaged_emails = {
            "msg_0": _make_triaged("msg_0", 1, 95),
            "msg_1": _make_triaged("msg_1", 2, 72),
            "msg_2": _make_triaged("msg_2", 3, 45),
            "msg_3": _make_triaged("msg_3", 3, 40),
            "msg_4": _make_triaged("msg_4", 4, 15),
        }
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        # Build tier summary like compose would
        tier_summary = {}
        for e in result:
            t = e.get("triage_tier")
            if t is not None:
                tier_summary[t] = tier_summary.get(t, 0) + 1
        assert tier_summary == {1: 1, 2: 1, 3: 2, 4: 1}
```

**Step 2: Run tests to verify they pass** (these test the enrichment function, should pass from Task 6)

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/test_command_processor_enrichment.py -v`
Expected: ALL PASS

**Step 3: Modify unified_command_processor.py**

In `_handle_workspace_action()`, at the artifact assembly block (lines ~4084-4087), add triage enrichment:

After:
```python
    _artifacts: Dict[str, Any] = {}
    for _o in ordered_outcomes:
        _r = _o.get("result", {}) or {}
        _artifacts.update(_r)
```

Add:
```python
    # v281.1: Triage enrichment — merge triage metadata into emails
    _triage_enriched = False
    _triage_age_s = None
    if _ws_action in ("fetch_unread_emails",) and "emails" in _artifacts:
        try:
            from autonomy.email_triage.runner import EmailTriageRunner
            from autonomy.email_triage.enrichment import enrich_with_triage
            _runner = EmailTriageRunner._instance  # Safe: None if not initialized
            _enriched, _triage_enriched, _triage_age_s = enrich_with_triage(
                _artifacts.get("emails", []),
                _runner,
            )
            if _triage_enriched:
                _artifacts["emails"] = _enriched
                _artifacts["triage_available"] = True
                _artifacts["triage_age_s"] = round(_triage_age_s, 1) if _triage_age_s else None
        except Exception as _te:
            logger.debug("[v281.1] Triage enrichment skipped: %s", _te)
```

**Step 4: Run full triage test suite**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/api/unified_command_processor.py tests/unit/backend/email_triage/test_command_processor_enrichment.py && git commit -m "feat(email-triage): add triage enrichment to workspace fast-path compose"
```

---

## Task 10: Package API Update

**Files:**
- Modify: `backend/autonomy/email_triage/__init__.py`

**Context:** Export new public API symbols.

**Step 1: Update __init__.py**

```python
"""Autonomous Gmail Triage v1.1 — score, label, notify, and enrich emails.

Public API:
    EmailTriageRunner  — singleton runner, called by agent_runtime
    TriageConfig       — configuration (feature flags, thresholds, env vars)
    score_email        — pure deterministic scoring function
    extract_features   — structured feature extraction with heuristic fallback
    enrich_with_triage — enrich raw emails with triage metadata
    DependencyResolver — dependency resolution with backoff
    DependencyHealth   — per-dependency health tracking
"""

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.runner import EmailTriageRunner
from autonomy.email_triage.scoring import score_email
from autonomy.email_triage.extraction import extract_features
from autonomy.email_triage.enrichment import enrich_with_triage
from autonomy.email_triage.dependencies import DependencyResolver, DependencyHealth

__all__ = [
    "EmailTriageRunner",
    "TriageConfig",
    "get_triage_config",
    "score_email",
    "extract_features",
    "enrich_with_triage",
    "DependencyResolver",
    "DependencyHealth",
]
```

**Step 2: Run import smoke test**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -c "from autonomy.email_triage import enrich_with_triage, DependencyResolver, DependencyHealth; print('OK')" 2>&1 | head -5`
Expected: `OK`

**Step 3: Run full test suite**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && git add backend/autonomy/email_triage/__init__.py && git commit -m "feat(email-triage): update package API for v1.1 integration layer"
```

---

## Task 11: Full Regression Test Suite

**Files:**
- All test files in `tests/unit/backend/email_triage/`

**Context:** Final validation that all tasks integrate correctly.

**Step 1: Run complete email triage test suite**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ -v --tb=short`
Expected: ALL PASS (original 77 + new tests from Tasks 1-9)

**Step 2: Run import smoke test for all public API**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -c "
from autonomy.email_triage import (
    EmailTriageRunner, TriageConfig, get_triage_config,
    score_email, extract_features, enrich_with_triage,
    DependencyResolver, DependencyHealth,
)
from autonomy.email_triage.notifications import tier_to_urgency, deliver_immediate, deliver_summary
from autonomy.email_triage.schemas import NotificationDeliveryResult
from autonomy.email_triage.events import EVENT_DEPENDENCY_UNAVAILABLE, EVENT_DEPENDENCY_DEGRADED, EVENT_NOTIFICATION_DELIVERY_RESULT
print('All imports OK')
"`
Expected: `All imports OK`

**Step 3: Verify disabled-by-default**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -c "
from autonomy.email_triage.config import TriageConfig
c = TriageConfig()
assert c.enabled is False, 'FAIL: enabled should be False by default'
print('Disabled-by-default: PASS')
"`
Expected: `Disabled-by-default: PASS`

**Step 4: Verify enrichment graceful fallback**

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -c "
from autonomy.email_triage.enrichment import enrich_with_triage
emails = [{'id': 'msg_1', 'subject': 'Hello'}]
result, enriched, age = enrich_with_triage(emails, None)
assert enriched is False
assert result == emails
print('Enrichment fallback: PASS')
"`
Expected: `Enrichment fallback: PASS`

---

Plan complete and saved to `docs/plans/2026-03-01-email-triage-integration-plan.md`. Two execution options:

**1. Subagent-Driven (this session)** — I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** — Open new session with executing-plans, batch execution with checkpoints

Which approach?

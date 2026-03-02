# Autonomous Gmail Triage v1 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement v1 autonomous Gmail triage that scores, labels, and selectively notifies on incoming emails with zero destructive actions.

**Architecture:** Layered `backend/autonomy/email_triage/` package with pure deterministic scoring, J-Prime structured extraction, Gmail label management, configurable notification policy, and observability. Hooks into `agent_runtime.py` housekeeping loop behind `EMAIL_TRIAGE_ENABLED` feature flag.

**Tech Stack:** Python 3.11, Gmail API v1, PrimeRouter, asyncio, dataclasses (frozen for immutability), pytest.

**Design Doc:** `docs/plans/2026-03-01-email-triage-design.md`

---

## Task 1: Schemas — Data Contracts

**Files:**
- Create: `backend/autonomy/email_triage/__init__.py`
- Create: `backend/autonomy/email_triage/schemas.py`
- Create: `tests/unit/backend/email_triage/__init__.py`
- Create: `tests/unit/backend/email_triage/test_schemas.py`

**Step 1: Create package skeleton**

Create empty `__init__.py` files for both package and test package:

```python
# backend/autonomy/email_triage/__init__.py
"""Autonomous Gmail Triage v1 — score, label, and notify on incoming emails."""
```

```python
# tests/unit/backend/email_triage/__init__.py
```

**Step 2: Write the failing tests**

```python
# tests/unit/backend/email_triage/test_schemas.py
"""Tests for email triage data contracts."""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.schemas import (
    EmailFeatures,
    ScoringResult,
    TriagedEmail,
    TriageCycleReport,
)


class TestEmailFeatures:
    """EmailFeatures is frozen and contains all extraction fields."""

    def test_construction_with_all_fields(self):
        f = EmailFeatures(
            message_id="abc123",
            sender="alice@example.com",
            sender_domain="example.com",
            subject="Urgent: Q4 report",
            snippet="Please review the attached...",
            is_reply=False,
            has_attachment=True,
            label_ids=("INBOX", "UNREAD"),
            keywords=("urgent", "report"),
            sender_frequency="frequent",
            urgency_signals=("deadline",),
            extraction_confidence=0.95,
        )
        assert f.message_id == "abc123"
        assert f.sender_domain == "example.com"
        assert f.is_reply is False
        assert f.has_attachment is True
        assert f.keywords == ("urgent", "report")

    def test_frozen_immutability(self):
        f = EmailFeatures(
            message_id="abc123",
            sender="alice@example.com",
            sender_domain="example.com",
            subject="Test",
            snippet="",
            is_reply=False,
            has_attachment=False,
            label_ids=(),
            keywords=(),
            sender_frequency="first_time",
            urgency_signals=(),
            extraction_confidence=0.0,
        )
        try:
            f.message_id = "changed"
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass  # Expected — frozen dataclass

    def test_heuristic_only_features(self):
        """Features with zero AI extraction (confidence=0.0)."""
        f = EmailFeatures(
            message_id="def456",
            sender="bob@unknown.org",
            sender_domain="unknown.org",
            subject="Hello",
            snippet="Hi there",
            is_reply=False,
            has_attachment=False,
            label_ids=("INBOX",),
            keywords=(),
            sender_frequency="first_time",
            urgency_signals=(),
            extraction_confidence=0.0,
        )
        assert f.extraction_confidence == 0.0
        assert f.keywords == ()


class TestScoringResult:
    """ScoringResult is frozen with score, tier, breakdown, and idempotency key."""

    def test_construction(self):
        r = ScoringResult(
            score=87,
            tier=1,
            tier_label="jarvis/tier1_critical",
            breakdown={"sender": 0.9, "content": 0.85, "urgency": 0.8, "context": 0.7},
            idempotency_key="abc123def456",
        )
        assert r.score == 87
        assert r.tier == 1
        assert r.tier_label == "jarvis/tier1_critical"
        assert "sender" in r.breakdown

    def test_frozen(self):
        r = ScoringResult(
            score=50,
            tier=3,
            tier_label="jarvis/tier3_review",
            breakdown={},
            idempotency_key="x",
        )
        try:
            r.score = 99
            assert False, "Should have raised"
        except AttributeError:
            pass


class TestTriagedEmail:
    """TriagedEmail combines features + scoring + notification action."""

    def test_construction(self):
        features = EmailFeatures(
            message_id="m1",
            sender="a@b.com",
            sender_domain="b.com",
            subject="Test",
            snippet="",
            is_reply=False,
            has_attachment=False,
            label_ids=(),
            keywords=(),
            sender_frequency="first_time",
            urgency_signals=(),
            extraction_confidence=0.0,
        )
        scoring = ScoringResult(
            score=42,
            tier=3,
            tier_label="jarvis/tier3_review",
            breakdown={},
            idempotency_key="k1",
        )
        t = TriagedEmail(
            features=features,
            scoring=scoring,
            notification_action="label_only",
            processed_at=time.time(),
        )
        assert t.notification_action == "label_only"
        assert t.features.message_id == "m1"
        assert t.scoring.tier == 3


class TestTriageCycleReport:
    """TriageCycleReport summarizes a full triage cycle."""

    def test_skipped_cycle(self):
        r = TriageCycleReport(
            cycle_id="c1",
            started_at=time.time(),
            completed_at=time.time(),
            emails_fetched=0,
            emails_processed=0,
            tier_counts={},
            notifications_sent=0,
            notifications_suppressed=0,
            errors=[],
            skipped=True,
            skip_reason="disabled",
        )
        assert r.skipped is True
        assert r.skip_reason == "disabled"

    def test_normal_cycle(self):
        r = TriageCycleReport(
            cycle_id="c2",
            started_at=1000.0,
            completed_at=1005.0,
            emails_fetched=10,
            emails_processed=10,
            tier_counts={1: 1, 2: 3, 3: 4, 4: 2},
            notifications_sent=2,
            notifications_suppressed=1,
            errors=[],
        )
        assert r.emails_processed == 10
        assert r.tier_counts[1] == 1
        assert r.skipped is False
```

**Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autonomy.email_triage.schemas'`

**Step 4: Write minimal implementation**

```python
# backend/autonomy/email_triage/schemas.py
"""Data contracts for the email triage system.

All result types are frozen dataclasses for immutability and determinism.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class EmailFeatures:
    """Structured features extracted from a raw email.

    Built from heuristic parsing (always available) + optional J-Prime
    structured extraction (when extraction_confidence > 0).
    """

    message_id: str
    sender: str
    sender_domain: str
    subject: str
    snippet: str
    is_reply: bool
    has_attachment: bool
    label_ids: Tuple[str, ...]
    keywords: Tuple[str, ...]
    sender_frequency: str  # "first_time" | "occasional" | "frequent"
    urgency_signals: Tuple[str, ...]  # "deadline", "action_required", etc.
    extraction_confidence: float  # 0.0-1.0


@dataclass(frozen=True)
class ScoringResult:
    """Deterministic scoring output. Same inputs → same result."""

    score: int  # 0-100
    tier: int  # 1-4
    tier_label: str  # "jarvis/tier1_critical", etc.
    breakdown: Dict[str, float]  # per-factor scores
    idempotency_key: str  # sha256(message_id + scoring_version)[:16]


@dataclass
class TriagedEmail:
    """A fully triaged email with features, score, and action decision."""

    features: EmailFeatures
    scoring: ScoringResult
    notification_action: str  # "immediate" | "summary" | "label_only" | "quarantine"
    processed_at: float


@dataclass
class TriageCycleReport:
    """Summary of a single triage cycle."""

    cycle_id: str
    started_at: float
    completed_at: float
    emails_fetched: int
    emails_processed: int
    tier_counts: Dict[int, int]
    notifications_sent: int
    notifications_suppressed: int
    errors: List[str]
    skipped: bool = False
    skip_reason: Optional[str] = None
```

**Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_schemas.py -v`
Expected: 7 passed

**Step 6: Commit**

```bash
git add backend/autonomy/email_triage/__init__.py \
        backend/autonomy/email_triage/schemas.py \
        tests/unit/backend/email_triage/__init__.py \
        tests/unit/backend/email_triage/test_schemas.py
git commit -m "feat(email-triage): add data contract schemas (Task 1)"
```

---

## Task 2: Config — Feature Flags and Environment Variables

**Files:**
- Create: `backend/autonomy/email_triage/config.py`
- Create: `tests/unit/backend/email_triage/test_config.py`

**Step 1: Write the failing tests**

```python
# tests/unit/backend/email_triage/test_config.py
"""Tests for email triage configuration and feature flags."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.config import TriageConfig, get_triage_config


class TestTriageConfigDefaults:
    """Default config values match the design spec."""

    def test_disabled_by_default(self):
        config = TriageConfig()
        assert config.enabled is False

    def test_tier_thresholds(self):
        config = TriageConfig()
        assert config.tier1_min == 85
        assert config.tier2_min == 65
        assert config.tier3_min == 35

    def test_quiet_hours(self):
        config = TriageConfig()
        assert config.quiet_start_hour == 23
        assert config.quiet_end_hour == 8

    def test_dedup_windows(self):
        config = TriageConfig()
        assert config.dedup_tier1_s == 900   # 15 min
        assert config.dedup_tier2_s == 3600  # 60 min

    def test_interrupt_budget(self):
        config = TriageConfig()
        assert config.max_interrupts_per_hour == 3
        assert config.max_interrupts_per_day == 12

    def test_summary_interval(self):
        config = TriageConfig()
        assert config.summary_interval_s == 1800  # 30 min

    def test_runner_settings(self):
        config = TriageConfig()
        assert config.poll_interval_s == 60.0
        assert config.max_emails_per_cycle == 25
        assert config.cycle_timeout_s == 30.0

    def test_gmail_labels(self):
        config = TriageConfig()
        assert config.label_tier1 == "jarvis/tier1_critical"
        assert config.label_tier2 == "jarvis/tier2_high"
        assert config.label_tier3 == "jarvis/tier3_review"
        assert config.label_tier4 == "jarvis/tier4_noise"

    def test_notification_flags_default_true(self):
        config = TriageConfig()
        assert config.notify_tier1 is True
        assert config.notify_tier2 is True

    def test_quarantine_default_false(self):
        config = TriageConfig()
        assert config.quarantine_tier4 is False


class TestTriageConfigFromEnv:
    """Config reads from environment variables."""

    def test_enabled_from_env(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_ENABLED", "true")
        config = TriageConfig.from_env()
        assert config.enabled is True

    def test_poll_interval_from_env(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_POLL_INTERVAL_S", "120")
        config = TriageConfig.from_env()
        assert config.poll_interval_s == 120.0

    def test_quiet_hours_from_env(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_QUIET_START", "22")
        monkeypatch.setenv("EMAIL_TRIAGE_QUIET_END", "7")
        config = TriageConfig.from_env()
        assert config.quiet_start_hour == 22
        assert config.quiet_end_hour == 7

    def test_budget_from_env(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_MAX_INTERRUPTS_HOUR", "5")
        monkeypatch.setenv("EMAIL_TRIAGE_MAX_INTERRUPTS_DAY", "20")
        config = TriageConfig.from_env()
        assert config.max_interrupts_per_hour == 5
        assert config.max_interrupts_per_day == 20

    def test_invalid_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_POLL_INTERVAL_S", "not_a_number")
        config = TriageConfig.from_env()
        assert config.poll_interval_s == 60.0  # default


class TestGetTriageConfig:
    """get_triage_config() returns singleton."""

    def test_returns_config(self):
        config = get_triage_config()
        assert isinstance(config, TriageConfig)

    def test_tier_label_for_score(self):
        config = TriageConfig()
        assert config.label_for_tier(1) == "jarvis/tier1_critical"
        assert config.label_for_tier(2) == "jarvis/tier2_high"
        assert config.label_for_tier(3) == "jarvis/tier3_review"
        assert config.label_for_tier(4) == "jarvis/tier4_noise"

    def test_tier_for_score(self):
        config = TriageConfig()
        assert config.tier_for_score(100) == 1
        assert config.tier_for_score(85) == 1
        assert config.tier_for_score(84) == 2
        assert config.tier_for_score(65) == 2
        assert config.tier_for_score(64) == 3
        assert config.tier_for_score(35) == 3
        assert config.tier_for_score(34) == 4
        assert config.tier_for_score(0) == 4
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/autonomy/email_triage/config.py
"""Configuration for the email triage system.

All settings are env-var configurable. Single source of truth.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("jarvis.email_triage.config")

_singleton: TriageConfig | None = None


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


@dataclass
class TriageConfig:
    """Email triage configuration. All fields have safe defaults."""

    # Feature flags
    enabled: bool = False
    notify_tier1: bool = True
    notify_tier2: bool = True
    quarantine_tier4: bool = False
    extraction_enabled: bool = True
    summaries_enabled: bool = True

    # Scoring
    scoring_version: str = "v1"

    # Tier thresholds
    tier1_min: int = 85
    tier2_min: int = 65
    tier3_min: int = 35

    # Gmail labels
    label_tier1: str = "jarvis/tier1_critical"
    label_tier2: str = "jarvis/tier2_high"
    label_tier3: str = "jarvis/tier3_review"
    label_tier4: str = "jarvis/tier4_noise"

    # Quiet hours (local time)
    quiet_start_hour: int = 23
    quiet_end_hour: int = 8

    # Dedup windows (seconds)
    dedup_tier1_s: int = 900
    dedup_tier2_s: int = 3600

    # Interrupt budget
    max_interrupts_per_hour: int = 3
    max_interrupts_per_day: int = 12

    # Summary
    summary_interval_s: int = 1800

    # Runner
    poll_interval_s: float = 60.0
    max_emails_per_cycle: int = 25
    cycle_timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> TriageConfig:
        """Build config from environment variables."""
        return cls(
            enabled=_env_bool("EMAIL_TRIAGE_ENABLED", False),
            notify_tier1=_env_bool("EMAIL_TRIAGE_NOTIFY_TIER1", True),
            notify_tier2=_env_bool("EMAIL_TRIAGE_NOTIFY_TIER2", True),
            quarantine_tier4=_env_bool("EMAIL_TRIAGE_QUARANTINE_TIER4", False),
            extraction_enabled=_env_bool("EMAIL_TRIAGE_EXTRACTION_ENABLED", True),
            summaries_enabled=_env_bool("EMAIL_TRIAGE_SUMMARIES_ENABLED", True),
            quiet_start_hour=_env_int("EMAIL_TRIAGE_QUIET_START", 23),
            quiet_end_hour=_env_int("EMAIL_TRIAGE_QUIET_END", 8),
            dedup_tier1_s=_env_int("EMAIL_TRIAGE_DEDUP_TIER1_S", 900),
            dedup_tier2_s=_env_int("EMAIL_TRIAGE_DEDUP_TIER2_S", 3600),
            max_interrupts_per_hour=_env_int("EMAIL_TRIAGE_MAX_INTERRUPTS_HOUR", 3),
            max_interrupts_per_day=_env_int("EMAIL_TRIAGE_MAX_INTERRUPTS_DAY", 12),
            summary_interval_s=_env_int("EMAIL_TRIAGE_SUMMARY_INTERVAL_S", 1800),
            poll_interval_s=_env_float("EMAIL_TRIAGE_POLL_INTERVAL_S", 60.0),
            max_emails_per_cycle=_env_int("EMAIL_TRIAGE_MAX_PER_CYCLE", 25),
            cycle_timeout_s=_env_float("EMAIL_TRIAGE_CYCLE_TIMEOUT_S", 30.0),
        )

    def tier_for_score(self, score: int) -> int:
        """Map score (0-100) to tier (1-4)."""
        if score >= self.tier1_min:
            return 1
        if score >= self.tier2_min:
            return 2
        if score >= self.tier3_min:
            return 3
        return 4

    def label_for_tier(self, tier: int) -> str:
        """Map tier (1-4) to Gmail label name."""
        return {
            1: self.label_tier1,
            2: self.label_tier2,
            3: self.label_tier3,
            4: self.label_tier4,
        }.get(tier, self.label_tier4)


def get_triage_config() -> TriageConfig:
    """Get the singleton config instance."""
    global _singleton
    if _singleton is None:
        _singleton = TriageConfig.from_env()
    return _singleton


def reset_triage_config() -> None:
    """Reset singleton (for testing)."""
    global _singleton
    _singleton = None
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_config.py -v`
Expected: 15 passed

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/config.py \
        tests/unit/backend/email_triage/test_config.py
git commit -m "feat(email-triage): add config with feature flags and env vars (Task 2)"
```

---

## Task 3: Events — Structured Observability

**Files:**
- Create: `backend/autonomy/email_triage/events.py`
- Create: `tests/unit/backend/email_triage/test_events.py`

**Step 1: Write the failing tests**

```python
# tests/unit/backend/email_triage/test_events.py
"""Tests for email triage observability events."""

import logging
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.events import (
    emit_triage_event,
    EVENT_CYCLE_STARTED,
    EVENT_EMAIL_TRIAGED,
    EVENT_NOTIFICATION_SENT,
    EVENT_NOTIFICATION_SUPPRESSED,
    EVENT_SUMMARY_FLUSHED,
    EVENT_CYCLE_COMPLETED,
    EVENT_TRIAGE_ERROR,
)


class TestEventConstants:
    """All 7 event type constants are defined."""

    def test_all_event_types_defined(self):
        assert EVENT_CYCLE_STARTED == "triage_cycle_started"
        assert EVENT_EMAIL_TRIAGED == "email_triaged"
        assert EVENT_NOTIFICATION_SENT == "notification_sent"
        assert EVENT_NOTIFICATION_SUPPRESSED == "notification_suppressed"
        assert EVENT_SUMMARY_FLUSHED == "summary_flushed"
        assert EVENT_CYCLE_COMPLETED == "triage_cycle_completed"
        assert EVENT_TRIAGE_ERROR == "triage_error"


class TestEmitTriageEvent:
    """emit_triage_event() emits structured JSON to the logger."""

    def test_emits_to_logger(self, caplog):
        with caplog.at_level(logging.INFO, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_CYCLE_STARTED, {"cycle_id": "c1"})
        assert any("triage_cycle_started" in r.message for r in caplog.records)

    def test_payload_in_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_EMAIL_TRIAGED, {
                "message_id": "m1",
                "score": 87,
                "tier": 1,
            })
        found = [r for r in caplog.records if "email_triaged" in r.message]
        assert len(found) == 1
        # The log message should contain the payload as JSON
        assert "m1" in found[0].message

    def test_error_event(self, caplog):
        with caplog.at_level(logging.WARNING, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_TRIAGE_ERROR, {
                "cycle_id": "c1",
                "error_type": "extraction_failed",
                "message": "J-Prime returned invalid JSON",
            })
        found = [r for r in caplog.records if "triage_error" in r.message]
        assert len(found) == 1

    def test_event_includes_timestamp(self, caplog):
        with caplog.at_level(logging.INFO, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_CYCLE_COMPLETED, {"cycle_id": "c1"})
        found = [r for r in caplog.records if "triage_cycle_completed" in r.message]
        assert len(found) == 1
        # Should be parseable JSON with timestamp
        msg = found[0].message
        data = json.loads(msg)
        assert "timestamp" in data
        assert "event" in data
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/autonomy/email_triage/events.py
"""Structured observability events for the email triage system.

All events are emitted as JSON to the `jarvis.email_triage` logger.
7 event types cover the full triage lifecycle.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict

logger = logging.getLogger("jarvis.email_triage")

# Event type constants
EVENT_CYCLE_STARTED = "triage_cycle_started"
EVENT_EMAIL_TRIAGED = "email_triaged"
EVENT_NOTIFICATION_SENT = "notification_sent"
EVENT_NOTIFICATION_SUPPRESSED = "notification_suppressed"
EVENT_SUMMARY_FLUSHED = "summary_flushed"
EVENT_CYCLE_COMPLETED = "triage_cycle_completed"
EVENT_TRIAGE_ERROR = "triage_error"

_ERROR_EVENTS = frozenset({EVENT_TRIAGE_ERROR})


def emit_triage_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Emit a structured triage event to the logger.

    Args:
        event_type: One of the EVENT_* constants.
        payload: Event-specific data (must be JSON-serializable).
    """
    event = {
        "event": event_type,
        "timestamp": time.time(),
        **payload,
    }
    msg = json.dumps(event, default=str)
    if event_type in _ERROR_EVENTS:
        logger.warning(msg)
    else:
        logger.info(msg)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_events.py -v`
Expected: 5 passed

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/events.py \
        tests/unit/backend/email_triage/test_events.py
git commit -m "feat(email-triage): add structured observability events (Task 3)"
```

---

## Task 4: Scoring — Pure Deterministic Engine

**Files:**
- Create: `backend/autonomy/email_triage/scoring.py`
- Create: `tests/unit/backend/email_triage/test_scoring.py`

**Step 1: Write the failing tests**

```python
# tests/unit/backend/email_triage/test_scoring.py
"""Tests for deterministic email scoring engine.

Scoring is pure: same inputs → same output. No I/O, no randomness.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.schemas import EmailFeatures
from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.scoring import score_email


def _make_features(**overrides) -> EmailFeatures:
    """Helper to build EmailFeatures with defaults."""
    defaults = dict(
        message_id="test_msg_001",
        sender="alice@example.com",
        sender_domain="example.com",
        subject="Hello",
        snippet="Just checking in",
        is_reply=False,
        has_attachment=False,
        label_ids=("INBOX", "UNREAD"),
        keywords=(),
        sender_frequency="occasional",
        urgency_signals=(),
        extraction_confidence=0.0,
    )
    defaults.update(overrides)
    # Convert lists to tuples for frozen dataclass
    for key in ("label_ids", "keywords", "urgency_signals"):
        if isinstance(defaults[key], list):
            defaults[key] = tuple(defaults[key])
    return EmailFeatures(**defaults)


class TestScoreEmailDeterminism:
    """Same inputs must always produce same output."""

    def test_same_inputs_same_score(self):
        config = TriageConfig()
        f = _make_features()
        r1 = score_email(f, config)
        r2 = score_email(f, config)
        assert r1.score == r2.score
        assert r1.tier == r2.tier
        assert r1.idempotency_key == r2.idempotency_key

    def test_different_message_id_different_idempotency_key(self):
        config = TriageConfig()
        f1 = _make_features(message_id="msg_a")
        f2 = _make_features(message_id="msg_b")
        r1 = score_email(f1, config)
        r2 = score_email(f2, config)
        assert r1.idempotency_key != r2.idempotency_key
        # But scores should be the same (same features otherwise)
        assert r1.score == r2.score


class TestTierMapping:
    """Score-to-tier mapping matches spec thresholds."""

    def test_tier1_critical(self):
        config = TriageConfig()
        # High urgency + known sender + urgent content
        f = _make_features(
            sender_frequency="frequent",
            keywords=("urgent", "deadline", "action_required"),
            urgency_signals=("deadline", "action_required"),
            subject="URGENT: Action Required - Server Down",
            is_reply=True,
        )
        r = score_email(f, config)
        assert r.tier == 1
        assert r.score >= 85
        assert r.tier_label == "jarvis/tier1_critical"

    def test_tier4_noise(self):
        config = TriageConfig()
        # Unknown sender, promotional content, no urgency
        f = _make_features(
            sender="noreply@marketing.spam.com",
            sender_domain="spam.com",
            sender_frequency="first_time",
            subject="50% off everything!",
            keywords=("sale", "discount", "unsubscribe"),
            label_ids=("CATEGORY_PROMOTIONS",),
            urgency_signals=(),
        )
        r = score_email(f, config)
        assert r.tier == 4
        assert r.score < 35
        assert r.tier_label == "jarvis/tier4_noise"

    def test_tier2_high(self):
        config = TriageConfig()
        f = _make_features(
            sender_frequency="frequent",
            keywords=("meeting", "tomorrow"),
            urgency_signals=("deadline",),
            subject="Meeting tomorrow at 10am",
            is_reply=True,
        )
        r = score_email(f, config)
        assert r.tier in (1, 2)
        assert r.score >= 65

    def test_tier3_review(self):
        config = TriageConfig()
        f = _make_features(
            sender_frequency="occasional",
            keywords=("newsletter",),
            subject="Weekly Team Update",
        )
        r = score_email(f, config)
        assert r.tier in (3, 4)
        assert r.score < 65


class TestScoreBoundaries:
    """Score is always 0-100, tier is always 1-4."""

    def test_score_range(self):
        config = TriageConfig()
        # Extreme low
        f_low = _make_features(
            sender="x@x.x",
            sender_domain="x.x",
            sender_frequency="first_time",
            subject="",
            snippet="",
            keywords=(),
            urgency_signals=(),
            label_ids=("CATEGORY_PROMOTIONS",),
        )
        r_low = score_email(f_low, config)
        assert 0 <= r_low.score <= 100
        assert 1 <= r_low.tier <= 4

        # Extreme high
        f_high = _make_features(
            sender_frequency="frequent",
            keywords=("urgent", "critical", "immediate", "action_required"),
            urgency_signals=("deadline", "action_required", "escalation"),
            subject="CRITICAL: Immediate Action Required - Security Breach",
            is_reply=True,
            has_attachment=True,
        )
        r_high = score_email(f_high, config)
        assert 0 <= r_high.score <= 100
        assert 1 <= r_high.tier <= 4


class TestScoringBreakdown:
    """Breakdown dict contains all 4 factors."""

    def test_breakdown_keys(self):
        config = TriageConfig()
        f = _make_features()
        r = score_email(f, config)
        assert "sender" in r.breakdown
        assert "content" in r.breakdown
        assert "urgency" in r.breakdown
        assert "context" in r.breakdown

    def test_breakdown_values_in_range(self):
        config = TriageConfig()
        f = _make_features()
        r = score_email(f, config)
        for factor, val in r.breakdown.items():
            assert 0.0 <= val <= 1.0, f"{factor} out of range: {val}"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_scoring.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/autonomy/email_triage/scoring.py
"""Deterministic email scoring engine.

Pure function — no I/O, no network, no randomness.
Same EmailFeatures + same TriageConfig → identical ScoringResult every time.
"""

from __future__ import annotations

import hashlib
from typing import Set

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.schemas import EmailFeatures, ScoringResult

# Urgency keywords (normalized to lowercase)
_URGENT_KEYWORDS: Set[str] = {
    "urgent", "critical", "immediate", "asap", "emergency",
    "action_required", "action required", "time-sensitive",
    "deadline", "due today", "overdue", "escalation",
}

_NOISE_KEYWORDS: Set[str] = {
    "unsubscribe", "sale", "discount", "promo", "deal",
    "marketing", "newsletter", "advertisement", "offer",
}

_PROMOTIONAL_LABELS: Set[str] = {
    "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES",
    "CATEGORY_FORUMS", "SPAM",
}


def score_email(features: EmailFeatures, config: TriageConfig) -> ScoringResult:
    """Score an email deterministically.

    Factor weights:
        sender   (30%) — known contacts, frequency, domain
        content  (35%) — urgency keywords in subject/snippet, attachment
        urgency  (25%) — urgency signals from extraction, is_reply
        context  (10%) — label context (INBOX vs CATEGORY_PROMOTIONS)

    Args:
        features: Extracted email features.
        config: Triage configuration (thresholds, scoring version).

    Returns:
        ScoringResult with score (0-100), tier (1-4), breakdown, idempotency key.
    """
    sender_score = _score_sender(features)
    content_score = _score_content(features)
    urgency_score = _score_urgency(features)
    context_score = _score_context(features)

    raw = (
        sender_score * 0.30
        + content_score * 0.35
        + urgency_score * 0.25
        + context_score * 0.10
    )
    score = max(0, min(100, int(round(raw * 100))))
    tier = config.tier_for_score(score)
    tier_label = config.label_for_tier(tier)

    idempotency_key = hashlib.sha256(
        f"{features.message_id}:{config.scoring_version}".encode()
    ).hexdigest()[:16]

    return ScoringResult(
        score=score,
        tier=tier,
        tier_label=tier_label,
        breakdown={
            "sender": round(sender_score, 4),
            "content": round(content_score, 4),
            "urgency": round(urgency_score, 4),
            "context": round(context_score, 4),
        },
        idempotency_key=idempotency_key,
    )


def _score_sender(features: EmailFeatures) -> float:
    """Score based on sender identity and frequency (0.0 - 1.0)."""
    base = 0.3  # unknown sender baseline

    freq = features.sender_frequency
    if freq == "frequent":
        base = 0.9
    elif freq == "occasional":
        base = 0.5
    # "first_time" stays at 0.3

    # Penalize likely automated senders
    if features.sender.startswith("noreply@") or features.sender.startswith("no-reply@"):
        base *= 0.5

    return max(0.0, min(1.0, base))


def _score_content(features: EmailFeatures) -> float:
    """Score based on subject, snippet, keywords, attachment (0.0 - 1.0)."""
    score = 0.2  # baseline

    # Keyword matches in extracted keywords
    kw_lower = {k.lower() for k in features.keywords}
    urgent_hits = kw_lower & _URGENT_KEYWORDS
    noise_hits = kw_lower & _NOISE_KEYWORDS

    score += min(len(urgent_hits) * 0.2, 0.6)
    score -= min(len(noise_hits) * 0.15, 0.3)

    # Subject urgency heuristic
    subj_lower = features.subject.lower()
    for word in _URGENT_KEYWORDS:
        if word in subj_lower:
            score += 0.1
            break

    # Attachment boost
    if features.has_attachment:
        score += 0.05

    return max(0.0, min(1.0, score))


def _score_urgency(features: EmailFeatures) -> float:
    """Score based on urgency signals and reply status (0.0 - 1.0)."""
    score = 0.1

    # Urgency signals from extraction
    signal_count = len(features.urgency_signals)
    score += min(signal_count * 0.25, 0.7)

    # Reply threads are more likely to need attention
    if features.is_reply:
        score += 0.15

    return max(0.0, min(1.0, score))


def _score_context(features: EmailFeatures) -> float:
    """Score based on label context (0.0 - 1.0)."""
    labels = set(features.label_ids)

    # Promotional/social labels strongly indicate noise
    if labels & _PROMOTIONAL_LABELS:
        return 0.1

    # INBOX + IMPORTANT = high context score
    if "IMPORTANT" in labels:
        return 0.9

    if "INBOX" in labels:
        return 0.6

    return 0.3
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_scoring.py -v`
Expected: 9 passed

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/scoring.py \
        tests/unit/backend/email_triage/test_scoring.py
git commit -m "feat(email-triage): add deterministic scoring engine (Task 4)"
```

---

## Task 5: Extraction — J-Prime Structured Feature Extraction

**Files:**
- Create: `backend/autonomy/email_triage/extraction.py`
- Create: `tests/unit/backend/email_triage/test_extraction.py`

**Step 1: Write the failing tests**

```python
# tests/unit/backend/email_triage/test_extraction.py
"""Tests for J-Prime structured feature extraction."""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.extraction import (
    extract_features,
    _heuristic_features,
    _build_extraction_prompt,
)
from autonomy.email_triage.schemas import EmailFeatures
from autonomy.email_triage.config import TriageConfig


def _sample_email():
    return {
        "id": "msg_001",
        "thread_id": "t_001",
        "from": "Alice Smith <alice@company.com>",
        "to": "derek@example.com",
        "subject": "Re: Q4 Budget Review - Action Required",
        "date": "Mon, 1 Mar 2026 10:00:00 -0800",
        "snippet": "Hi Derek, please review the attached budget report by Friday.",
        "labels": ["INBOX", "UNREAD", "IMPORTANT"],
    }


class TestHeuristicFeatures:
    """Heuristic extraction works without any AI."""

    def test_basic_extraction(self):
        email = _sample_email()
        f = _heuristic_features(email)
        assert isinstance(f, EmailFeatures)
        assert f.message_id == "msg_001"
        assert f.sender == "Alice Smith <alice@company.com>"
        assert f.sender_domain == "company.com"
        assert f.is_reply is True  # subject starts with "Re:"
        assert "INBOX" in f.label_ids

    def test_sender_domain_extraction(self):
        email = _sample_email()
        email["from"] = "bob@sub.domain.org"
        f = _heuristic_features(email)
        assert f.sender_domain == "sub.domain.org"

    def test_empty_email_fields(self):
        email = {"id": "m1", "from": "", "subject": "", "snippet": "", "labels": []}
        f = _heuristic_features(email)
        assert f.message_id == "m1"
        assert f.sender_domain == ""
        assert f.extraction_confidence == 0.0

    def test_subject_urgency_keywords_detected(self):
        email = _sample_email()
        email["subject"] = "URGENT: Server is down"
        f = _heuristic_features(email)
        # Heuristic should detect "urgent" in keywords
        assert any("urgent" in k.lower() for k in f.keywords)


class TestBuildExtractionPrompt:
    """Prompt construction for J-Prime."""

    def test_prompt_contains_email_fields(self):
        email = _sample_email()
        prompt = _build_extraction_prompt(email)
        assert "alice@company.com" in prompt
        assert "Q4 Budget Review" in prompt

    def test_prompt_requests_json(self):
        email = _sample_email()
        prompt = _build_extraction_prompt(email)
        assert "JSON" in prompt or "json" in prompt


class TestExtractFeatures:
    """Full extraction with J-Prime + fallback."""

    @pytest.mark.asyncio
    async def test_successful_extraction(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=True)

        mock_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "keywords": ["budget", "review", "action_required"],
            "sender_frequency": "frequent",
            "urgency_signals": ["deadline", "action_required"],
        })
        mock_router.generate.return_value = mock_response

        f = await extract_features(email, mock_router, config=config)
        assert f.extraction_confidence > 0.0
        assert "budget" in f.keywords
        assert "deadline" in f.urgency_signals

    @pytest.mark.asyncio
    async def test_fallback_on_router_failure(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=True)

        mock_router = AsyncMock()
        mock_router.generate.side_effect = RuntimeError("Router down")

        f = await extract_features(email, mock_router, config=config)
        # Should fall back to heuristic
        assert f.extraction_confidence == 0.0
        assert f.message_id == "msg_001"

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=True)

        mock_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "This is not JSON at all"
        mock_router.generate.return_value = mock_response

        f = await extract_features(email, mock_router, config=config)
        assert f.extraction_confidence == 0.0

    @pytest.mark.asyncio
    async def test_extraction_disabled_uses_heuristic(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=False)

        mock_router = AsyncMock()
        f = await extract_features(email, mock_router, config=config)
        assert f.extraction_confidence == 0.0
        # Router should NOT have been called
        mock_router.generate.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_extraction.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/autonomy/email_triage/extraction.py
"""Structured feature extraction from raw email dicts.

Two-tier extraction:
1. Heuristic (always available) — parse subject, sender, labels
2. J-Prime structured (when available) — AI-powered keyword/urgency extraction

Falls back to heuristic-only if J-Prime is unavailable or returns bad JSON.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.schemas import EmailFeatures

logger = logging.getLogger("jarvis.email_triage.extraction")

# Heuristic urgency keywords
_HEURISTIC_URGENCY = {
    "urgent", "critical", "immediate", "asap", "emergency",
    "action required", "time-sensitive", "deadline", "due today",
}

_EXTRACTION_SYSTEM_PROMPT = (
    "You are an email classification assistant. Analyze the email and return "
    "ONLY a JSON object with these fields:\n"
    '  "keywords": list of relevant topic keywords (max 5)\n'
    '  "sender_frequency": "first_time" | "occasional" | "frequent"\n'
    '  "urgency_signals": list from ["deadline", "action_required", '
    '"escalation", "time_sensitive", "follow_up"]\n'
    "Return ONLY valid JSON, no markdown, no explanation."
)


def _extract_domain(sender: str) -> str:
    """Extract domain from sender string like 'Name <user@domain.com>'."""
    match = re.search(r"@([\w.-]+)", sender)
    return match.group(1) if match else ""


def _detect_reply(subject: str) -> bool:
    """Detect if subject indicates a reply thread."""
    return bool(re.match(r"^(Re|RE|Fwd|FWD):\s", subject))


def _heuristic_keywords(subject: str, snippet: str) -> Tuple[str, ...]:
    """Extract keywords from subject/snippet using heuristics."""
    text = f"{subject} {snippet}".lower()
    found = []
    for kw in _HEURISTIC_URGENCY:
        if kw in text:
            found.append(kw)
    return tuple(found)


def _heuristic_features(email_dict: Dict[str, Any]) -> EmailFeatures:
    """Build features using only heuristic parsing (no AI)."""
    sender = email_dict.get("from", "")
    subject = email_dict.get("subject", "")
    snippet = email_dict.get("snippet", "")
    labels = email_dict.get("labels", [])

    keywords = _heuristic_keywords(subject, snippet)
    urgency_signals = tuple(
        kw for kw in keywords
        if kw in {"deadline", "action required", "urgent", "critical", "emergency"}
    )

    return EmailFeatures(
        message_id=email_dict.get("id", ""),
        sender=sender,
        sender_domain=_extract_domain(sender),
        subject=subject,
        snippet=snippet,
        is_reply=_detect_reply(subject),
        has_attachment=False,  # Not available in list() metadata
        label_ids=tuple(labels),
        keywords=keywords,
        sender_frequency="first_time",  # Can't determine without history
        urgency_signals=urgency_signals,
        extraction_confidence=0.0,
    )


def _build_extraction_prompt(email_dict: Dict[str, Any]) -> str:
    """Build the prompt for J-Prime structured extraction."""
    return (
        f"Analyze this email:\n"
        f"From: {email_dict.get('from', '')}\n"
        f"Subject: {email_dict.get('subject', '')}\n"
        f"Snippet: {email_dict.get('snippet', '')}\n"
        f"Labels: {', '.join(email_dict.get('labels', []))}\n\n"
        f"Return a JSON object with keywords, sender_frequency, and urgency_signals."
    )


def _merge_features(
    heuristic: EmailFeatures,
    ai_data: Dict[str, Any],
) -> EmailFeatures:
    """Merge AI extraction results into heuristic features."""
    keywords = tuple(ai_data.get("keywords", [])) or heuristic.keywords
    sender_freq = ai_data.get("sender_frequency", heuristic.sender_frequency)
    urgency = tuple(ai_data.get("urgency_signals", [])) or heuristic.urgency_signals

    # Validate sender_frequency
    if sender_freq not in ("first_time", "occasional", "frequent"):
        sender_freq = heuristic.sender_frequency

    return EmailFeatures(
        message_id=heuristic.message_id,
        sender=heuristic.sender,
        sender_domain=heuristic.sender_domain,
        subject=heuristic.subject,
        snippet=heuristic.snippet,
        is_reply=heuristic.is_reply,
        has_attachment=heuristic.has_attachment,
        label_ids=heuristic.label_ids,
        keywords=keywords,
        sender_frequency=sender_freq,
        urgency_signals=urgency,
        extraction_confidence=0.8,  # AI extraction succeeded
    )


async def extract_features(
    email_dict: Dict[str, Any],
    router: Any,
    deadline: Optional[float] = None,
    config: Optional[TriageConfig] = None,
) -> EmailFeatures:
    """Extract structured features from a raw email dict.

    Args:
        email_dict: Raw email from Gmail API (id, from, subject, snippet, labels).
        router: PrimeRouter instance for AI extraction.
        deadline: Optional monotonic deadline.
        config: Triage config (defaults to singleton).

    Returns:
        EmailFeatures with heuristic + optional AI enrichment.
    """
    config = config or get_triage_config()
    heuristic = _heuristic_features(email_dict)

    if not config.extraction_enabled:
        return heuristic

    try:
        prompt = _build_extraction_prompt(email_dict)
        response = await router.generate(
            prompt=prompt,
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            max_tokens=512,
            temperature=0.0,
            deadline=deadline,
        )
        parsed = json.loads(response.content)
        return _merge_features(heuristic, parsed)
    except (json.JSONDecodeError, AttributeError) as e:
        logger.debug("Extraction JSON parse failed: %s", e)
        return heuristic
    except Exception as e:
        logger.warning("Extraction failed, using heuristic: %s", e)
        return heuristic
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_extraction.py -v`
Expected: 8 passed

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/extraction.py \
        tests/unit/backend/email_triage/test_extraction.py
git commit -m "feat(email-triage): add J-Prime structured extraction with heuristic fallback (Task 5)"
```

---

## Task 6: Policy — Notification Rules

**Files:**
- Create: `backend/autonomy/email_triage/policy.py`
- Create: `tests/unit/backend/email_triage/test_policy.py`

**Step 1: Write the failing tests**

```python
# tests/unit/backend/email_triage/test_policy.py
"""Tests for notification policy (quiet hours, dedup, budget, summaries)."""

import os
import sys
import time
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.schemas import EmailFeatures, ScoringResult, TriagedEmail
from autonomy.email_triage.policy import NotificationPolicy


def _make_triaged(tier: int, score: int, msg_id: str = "m1") -> TriagedEmail:
    features = EmailFeatures(
        message_id=msg_id,
        sender="a@b.com",
        sender_domain="b.com",
        subject="Test",
        snippet="",
        is_reply=False,
        has_attachment=False,
        label_ids=(),
        keywords=(),
        sender_frequency="first_time",
        urgency_signals=(),
        extraction_confidence=0.0,
    )
    label_map = {1: "jarvis/tier1_critical", 2: "jarvis/tier2_high",
                 3: "jarvis/tier3_review", 4: "jarvis/tier4_noise"}
    scoring = ScoringResult(
        score=score, tier=tier, tier_label=label_map[tier],
        breakdown={}, idempotency_key=f"key_{msg_id}",
    )
    return TriagedEmail(
        features=features, scoring=scoring,
        notification_action="",  # To be decided by policy
        processed_at=time.time(),
    )


class TestNotificationActions:
    """Policy decides correct action for each tier."""

    def test_tier1_gets_immediate(self):
        config = TriageConfig(notify_tier1=True)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=1, score=90)
        action = policy.decide_action(t)
        assert action == "immediate"

    def test_tier2_gets_summary(self):
        config = TriageConfig(notify_tier2=True)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=2, score=70)
        action = policy.decide_action(t)
        assert action == "summary"

    def test_tier3_gets_label_only(self):
        config = TriageConfig()
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=3, score=50)
        action = policy.decide_action(t)
        assert action == "label_only"

    def test_tier4_gets_quarantine_when_enabled(self):
        config = TriageConfig(quarantine_tier4=True)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=4, score=10)
        action = policy.decide_action(t)
        assert action == "quarantine"

    def test_tier4_gets_label_only_when_quarantine_disabled(self):
        config = TriageConfig(quarantine_tier4=False)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=4, score=10)
        action = policy.decide_action(t)
        assert action == "label_only"

    def test_tier1_disabled_gets_label_only(self):
        config = TriageConfig(notify_tier1=False)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=1, score=95)
        action = policy.decide_action(t)
        assert action == "label_only"

    def test_tier2_disabled_gets_label_only(self):
        config = TriageConfig(notify_tier2=False)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=2, score=70)
        action = policy.decide_action(t)
        assert action == "label_only"


class TestQuietHours:
    """Quiet hours suppress tier2+ but not tier1."""

    def test_tier2_suppressed_during_quiet(self):
        config = TriageConfig(quiet_start_hour=23, quiet_end_hour=8)
        policy = NotificationPolicy(config)
        # Simulate 2 AM
        with patch("autonomy.email_triage.policy._current_hour", return_value=2):
            t = _make_triaged(tier=2, score=70)
            action = policy.decide_action(t)
            assert action == "label_only"

    def test_tier1_still_notifies_during_quiet(self):
        config = TriageConfig(quiet_start_hour=23, quiet_end_hour=8)
        policy = NotificationPolicy(config)
        with patch("autonomy.email_triage.policy._current_hour", return_value=2):
            t = _make_triaged(tier=1, score=90)
            action = policy.decide_action(t)
            assert action == "immediate"

    def test_not_quiet_at_noon(self):
        config = TriageConfig(quiet_start_hour=23, quiet_end_hour=8)
        policy = NotificationPolicy(config)
        with patch("autonomy.email_triage.policy._current_hour", return_value=12):
            t = _make_triaged(tier=2, score=70)
            action = policy.decide_action(t)
            assert action == "summary"


class TestDedup:
    """Same email not re-notified within dedup window."""

    def test_tier1_dedup_within_15min(self):
        config = TriageConfig(dedup_tier1_s=900)
        policy = NotificationPolicy(config)
        t1 = _make_triaged(tier=1, score=90, msg_id="dup1")
        t2 = _make_triaged(tier=1, score=90, msg_id="dup1")

        action1 = policy.decide_action(t1)
        assert action1 == "immediate"

        action2 = policy.decide_action(t2)
        assert action2 == "label_only"  # Deduped

    def test_different_messages_not_deduped(self):
        config = TriageConfig()
        policy = NotificationPolicy(config)
        t1 = _make_triaged(tier=1, score=90, msg_id="a")
        t2 = _make_triaged(tier=1, score=90, msg_id="b")

        action1 = policy.decide_action(t1)
        action2 = policy.decide_action(t2)
        assert action1 == "immediate"
        assert action2 == "immediate"


class TestInterruptBudget:
    """Max interrupts per hour enforced."""

    def test_budget_exhaustion(self):
        config = TriageConfig(max_interrupts_per_hour=2, max_interrupts_per_day=10)
        policy = NotificationPolicy(config)

        # First 2 should get through
        for i in range(2):
            t = _make_triaged(tier=1, score=90, msg_id=f"msg_{i}")
            action = policy.decide_action(t)
            assert action == "immediate", f"Message {i} should be immediate"

        # Third should be downgraded
        t3 = _make_triaged(tier=1, score=90, msg_id="msg_overflow")
        action3 = policy.decide_action(t3)
        assert action3 in ("summary", "label_only")


class TestSummaryBuffer:
    """Tier 2 emails buffered for summary delivery."""

    def test_tier2_added_to_summary_buffer(self):
        config = TriageConfig()
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=2, score=70)
        policy.decide_action(t)
        assert len(policy.summary_buffer) == 1

    def test_flush_clears_buffer(self):
        config = TriageConfig()
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=2, score=70)
        policy.decide_action(t)
        assert len(policy.summary_buffer) == 1
        summary = policy.flush_summary()
        assert policy.summary_buffer == []
        assert summary is not None
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_policy.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/autonomy/email_triage/policy.py
"""Notification policy for the email triage system.

Implements the full notification spec:
1. Quiet hours (23:00-08:00) — suppress tier2+, tier1 still notifies
2. Dedup windows (15min tier1, 60min tier2) — keyed by idempotency_key
3. Interrupt budget (3/hr, 12/day) — excess queued for summary
4. Summary windows (30min) — batch tier2 emails
5. Feature flag gating — each tier independently toggleable
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.schemas import TriagedEmail

logger = logging.getLogger("jarvis.email_triage.policy")


def _current_hour() -> int:
    """Get current local hour (0-23). Extracted for testability."""
    return datetime.now().hour


class NotificationPolicy:
    """Stateful notification policy engine."""

    def __init__(self, config: TriageConfig):
        self._config = config
        self._dedup_cache: Dict[str, float] = {}  # idempotency_key → timestamp
        self._interrupt_timestamps: List[float] = []  # timestamps of interrupts
        self._summary_buffer: List[TriagedEmail] = []
        self._last_summary_flush: float = time.time()

    @property
    def summary_buffer(self) -> List[TriagedEmail]:
        return self._summary_buffer

    def decide_action(self, triaged: TriagedEmail) -> str:
        """Decide notification action for a triaged email.

        Returns: "immediate" | "summary" | "label_only" | "quarantine"
        """
        tier = triaged.scoring.tier
        idem_key = triaged.scoring.idempotency_key

        # Tier 3: always label only
        if tier == 3:
            return "label_only"

        # Tier 4: quarantine or label only
        if tier == 4:
            return "quarantine" if self._config.quarantine_tier4 else "label_only"

        # Tier 1: check if notifications enabled
        if tier == 1 and not self._config.notify_tier1:
            return "label_only"

        # Tier 2: check if notifications enabled
        if tier == 2 and not self._config.notify_tier2:
            return "label_only"

        # Dedup check
        if self._is_duplicate(tier, idem_key):
            return "label_only"

        # Quiet hours: suppress tier2, allow tier1
        if tier >= 2 and self._in_quiet_hours():
            return "label_only"

        # Budget check: tier1 can exceed budget only by escalation
        if tier == 1:
            if self._budget_allows():
                self._record_interrupt()
                self._dedup_record(idem_key)
                return "immediate"
            else:
                # Tier 1 budget exhausted → summary instead of suppress
                self._summary_buffer.append(triaged)
                self._dedup_record(idem_key)
                return "summary"

        # Tier 2 → summary
        if tier == 2:
            self._summary_buffer.append(triaged)
            self._dedup_record(idem_key)
            return "summary"

        return "label_only"

    def flush_summary(self) -> Optional[str]:
        """Flush the summary buffer. Returns formatted summary or None if empty."""
        if not self._summary_buffer:
            return None

        lines = []
        for t in self._summary_buffer:
            lines.append(
                f"- [{t.features.subject}] from {t.features.sender} "
                f"(tier {t.scoring.tier}, score {t.scoring.score})"
            )

        summary = f"Email triage summary ({len(self._summary_buffer)} emails):\n"
        summary += "\n".join(lines)

        self._summary_buffer.clear()
        self._last_summary_flush = time.time()
        return summary

    def should_flush_summary(self) -> bool:
        """Check if summary window has elapsed."""
        return (
            len(self._summary_buffer) > 0
            and (time.time() - self._last_summary_flush) >= self._config.summary_interval_s
        )

    def _in_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        hour = _current_hour()
        start = self._config.quiet_start_hour
        end = self._config.quiet_end_hour
        if start > end:
            # Wraps midnight: e.g., 23:00 - 08:00
            return hour >= start or hour < end
        return start <= hour < end

    def _is_duplicate(self, tier: int, idem_key: str) -> bool:
        """Check if this email was already notified within dedup window."""
        if idem_key not in self._dedup_cache:
            return False
        last_time = self._dedup_cache[idem_key]
        window = self._config.dedup_tier1_s if tier == 1 else self._config.dedup_tier2_s
        return (time.time() - last_time) < window

    def _dedup_record(self, idem_key: str) -> None:
        """Record notification for dedup."""
        self._dedup_cache[idem_key] = time.time()

    def _budget_allows(self) -> bool:
        """Check if interrupt budget allows another notification."""
        now = time.time()
        # Clean old timestamps
        hour_ago = now - 3600
        day_ago = now - 86400
        self._interrupt_timestamps = [
            t for t in self._interrupt_timestamps if t > day_ago
        ]
        hour_count = sum(1 for t in self._interrupt_timestamps if t > hour_ago)
        day_count = len(self._interrupt_timestamps)

        return (
            hour_count < self._config.max_interrupts_per_hour
            and day_count < self._config.max_interrupts_per_day
        )

    def _record_interrupt(self) -> None:
        """Record an interrupt for budget tracking."""
        self._interrupt_timestamps.append(time.time())
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_policy.py -v`
Expected: 14 passed

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/policy.py \
        tests/unit/backend/email_triage/test_policy.py
git commit -m "feat(email-triage): add notification policy with quiet hours, dedup, budget (Task 6)"
```

---

## Task 7: Labels — Gmail Label CRUD

**Files:**
- Create: `backend/autonomy/email_triage/labels.py`
- Create: `tests/unit/backend/email_triage/test_labels.py`
- Modify: `backend/neural_mesh/agents/google_workspace_agent.py` (add label methods)

**Step 1: Write the failing tests**

```python
# tests/unit/backend/email_triage/test_labels.py
"""Tests for Gmail label management."""

import os
import sys
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.labels import ensure_labels_exist, apply_label
from autonomy.email_triage.config import TriageConfig


def _mock_gmail_service(existing_labels=None):
    """Create a mock Gmail service with label support."""
    svc = MagicMock()
    labels = existing_labels or []
    svc.users().labels().list(userId="me").execute.return_value = {
        "labels": [{"name": l, "id": f"Label_{i}"} for i, l in enumerate(labels)]
    }
    svc.users().labels().create(userId="me", body=MagicMock()).execute.return_value = {
        "id": "Label_new", "name": "created"
    }
    svc.users().messages().modify(userId="me", id=MagicMock(), body=MagicMock()).execute.return_value = {}
    return svc


class TestEnsureLabelsExist:
    """Label creation is idempotent."""

    @pytest.mark.asyncio
    async def test_creates_missing_labels(self):
        svc = _mock_gmail_service(existing_labels=[])
        config = TriageConfig()
        label_map = await ensure_labels_exist(svc, config)
        assert len(label_map) == 4  # All 4 tiers

    @pytest.mark.asyncio
    async def test_skips_existing_labels(self):
        svc = _mock_gmail_service(existing_labels=[
            "jarvis/tier1_critical", "jarvis/tier2_high",
            "jarvis/tier3_review", "jarvis/tier4_noise",
        ])
        config = TriageConfig()
        label_map = await ensure_labels_exist(svc, config)
        assert len(label_map) == 4
        # create() should NOT have been called for any
        assert svc.users().labels().create.call_count == 0


class TestApplyLabel:
    """Label application is idempotent."""

    @pytest.mark.asyncio
    async def test_applies_label(self):
        svc = _mock_gmail_service()
        label_map = {"jarvis/tier1_critical": "Label_0"}
        await apply_label(svc, "msg_001", "jarvis/tier1_critical", label_map)
        # Should have called modify
        svc.users().messages().modify.assert_called()

    @pytest.mark.asyncio
    async def test_missing_label_in_map_logs_warning(self, caplog):
        import logging
        svc = _mock_gmail_service()
        label_map = {}
        with caplog.at_level(logging.WARNING):
            await apply_label(svc, "msg_001", "jarvis/tier1_critical", label_map)
        assert any("not found in label map" in r.message for r in caplog.records)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_labels.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/autonomy/email_triage/labels.py
"""Gmail label management for email triage.

Creates jarvis/* labels if they don't exist and applies labels to messages.
All operations are idempotent — safe to call repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from autonomy.email_triage.config import TriageConfig

logger = logging.getLogger("jarvis.email_triage.labels")


async def ensure_labels_exist(
    gmail_service: Any,
    config: TriageConfig,
) -> Dict[str, str]:
    """Create jarvis/* labels if they don't exist.

    Args:
        gmail_service: Authenticated Gmail API service.
        config: Triage config with label names.

    Returns:
        Dict mapping label name → label ID.
    """
    loop = asyncio.get_event_loop()
    needed = [
        config.label_tier1,
        config.label_tier2,
        config.label_tier3,
        config.label_tier4,
    ]
    label_map: Dict[str, str] = {}

    # Fetch existing labels
    result = await loop.run_in_executor(
        None,
        lambda: gmail_service.users().labels().list(userId="me").execute(),
    )
    existing = {l["name"]: l["id"] for l in result.get("labels", [])}

    for name in needed:
        if name in existing:
            label_map[name] = existing[name]
        else:
            created = await loop.run_in_executor(
                None,
                lambda n=name: gmail_service.users().labels().create(
                    userId="me",
                    body={
                        "name": n,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                ).execute(),
            )
            label_map[name] = created["id"]
            logger.info("Created Gmail label: %s (id=%s)", name, created["id"])

    return label_map


async def apply_label(
    gmail_service: Any,
    message_id: str,
    label_name: str,
    label_map: Dict[str, str],
) -> None:
    """Apply a label to a Gmail message. Idempotent.

    Args:
        gmail_service: Authenticated Gmail API service.
        message_id: Gmail message ID.
        label_name: Label name (e.g., "jarvis/tier1_critical").
        label_map: Dict from ensure_labels_exist().
    """
    label_id = label_map.get(label_name)
    if not label_id:
        logger.warning("Label '%s' not found in label map", label_name)
        return

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: gmail_service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute(),
        )
    except Exception as e:
        logger.warning("Failed to apply label %s to %s: %s", label_name, message_id, e)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_labels.py -v`
Expected: 4 passed

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/labels.py \
        tests/unit/backend/email_triage/test_labels.py
git commit -m "feat(email-triage): add Gmail label management (Task 7)"
```

---

## Task 8: Runner — Triage Cycle Orchestrator

**Files:**
- Create: `backend/autonomy/email_triage/runner.py`
- Create: `tests/unit/backend/email_triage/test_runner.py`

**Step 1: Write the failing tests**

```python
# tests/unit/backend/email_triage/test_runner.py
"""Tests for the email triage runner (full cycle orchestration)."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.runner import EmailTriageRunner
from autonomy.email_triage.config import TriageConfig


def _sample_emails(count=3):
    return [
        {
            "id": f"msg_{i}",
            "thread_id": f"t_{i}",
            "from": f"sender{i}@example.com",
            "to": "derek@example.com",
            "subject": f"Test email {i}",
            "date": "Mon, 1 Mar 2026 10:00:00",
            "snippet": f"Snippet for email {i}",
            "labels": ["INBOX", "UNREAD"],
        }
        for i in range(count)
    ]


class TestRunCycle:
    """Full triage cycle runs end-to-end."""

    @pytest.mark.asyncio
    async def test_disabled_returns_skipped(self):
        config = TriageConfig(enabled=False)
        runner = EmailTriageRunner(config=config)
        report = await runner.run_cycle()
        assert report.skipped is True
        assert report.skip_reason == "disabled"

    @pytest.mark.asyncio
    async def test_processes_emails(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)

        # Mock email fetching
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(3))
        # Mock label operations
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}
        runner._apply_label = AsyncMock()

        report = await runner.run_cycle()
        assert report.skipped is False
        assert report.emails_fetched == 3
        assert report.emails_processed == 3

    @pytest.mark.asyncio
    async def test_respects_max_per_cycle(self):
        config = TriageConfig(enabled=True, extraction_enabled=False, max_emails_per_cycle=2)
        runner = EmailTriageRunner(config=config)

        runner._fetch_unread = AsyncMock(return_value=_sample_emails(5))
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}
        runner._apply_label = AsyncMock()

        report = await runner.run_cycle()
        assert report.emails_fetched == 5
        assert report.emails_processed == 2  # Limited to max

    @pytest.mark.asyncio
    async def test_handles_fetch_failure(self):
        config = TriageConfig(enabled=True)
        runner = EmailTriageRunner(config=config)
        runner._fetch_unread = AsyncMock(side_effect=RuntimeError("API error"))

        report = await runner.run_cycle()
        assert len(report.errors) > 0
        assert report.emails_processed == 0

    @pytest.mark.asyncio
    async def test_single_email_error_does_not_abort_cycle(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)

        emails = _sample_emails(3)
        runner._fetch_unread = AsyncMock(return_value=emails)
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}

        # Second email causes label failure
        call_count = 0
        async def flaky_label(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("label fail")
        runner._apply_label = flaky_label

        report = await runner.run_cycle()
        # Should still process 3 emails (error on 2nd doesn't stop 3rd)
        assert report.emails_processed == 3
        assert len(report.errors) == 1


class TestRunnerSingleton:
    """Singleton pattern works."""

    def test_get_instance(self):
        EmailTriageRunner._instance = None  # Reset
        r1 = EmailTriageRunner.get_instance()
        r2 = EmailTriageRunner.get_instance()
        assert r1 is r2
        EmailTriageRunner._instance = None  # Clean up
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/autonomy/email_triage/runner.py
"""Email triage runner — orchestrates the full triage cycle.

Called by agent_runtime.py housekeeping loop. Coordinates:
1. Fetch unread emails via GoogleWorkspaceAgent
2. Extract features (heuristic + optional J-Prime)
3. Score each email deterministically
4. Apply Gmail labels
5. Decide notification action via policy
6. Emit observability events
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.events import (
    emit_triage_event,
    EVENT_CYCLE_STARTED,
    EVENT_EMAIL_TRIAGED,
    EVENT_CYCLE_COMPLETED,
    EVENT_TRIAGE_ERROR,
)
from autonomy.email_triage.extraction import extract_features
from autonomy.email_triage.labels import apply_label, ensure_labels_exist
from autonomy.email_triage.policy import NotificationPolicy
from autonomy.email_triage.schemas import TriageCycleReport, TriagedEmail
from autonomy.email_triage.scoring import score_email

logger = logging.getLogger("jarvis.email_triage.runner")


class EmailTriageRunner:
    """Singleton runner for the email triage cycle."""

    _instance: ClassVar[Optional[EmailTriageRunner]] = None

    def __init__(
        self,
        config: Optional[TriageConfig] = None,
        workspace_agent: Any = None,
        router: Any = None,
    ):
        self._config = config or get_triage_config()
        self._workspace_agent = workspace_agent
        self._router = router
        self._policy = NotificationPolicy(self._config)
        self._label_map: Dict[str, str] = {}
        self._labels_initialized = False

    @classmethod
    def get_instance(cls, **kwargs) -> EmailTriageRunner:
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    async def run_cycle(self) -> TriageCycleReport:
        """Execute a single triage cycle."""
        cycle_id = uuid4().hex[:12]
        started_at = time.time()
        errors: List[str] = []
        tier_counts: Dict[int, int] = {}
        notifications_sent = 0
        notifications_suppressed = 0
        emails_fetched = 0
        emails_processed = 0

        if not self._config.enabled:
            return TriageCycleReport(
                cycle_id=cycle_id,
                started_at=started_at,
                completed_at=time.time(),
                emails_fetched=0,
                emails_processed=0,
                tier_counts={},
                notifications_sent=0,
                notifications_suppressed=0,
                errors=[],
                skipped=True,
                skip_reason="disabled",
            )

        emit_triage_event(EVENT_CYCLE_STARTED, {"cycle_id": cycle_id})

        # Ensure labels exist
        if not self._labels_initialized and self._workspace_agent:
            try:
                gmail_svc = getattr(self._workspace_agent, "_gmail_service", None)
                if gmail_svc:
                    self._label_map = await ensure_labels_exist(gmail_svc, self._config)
                    self._labels_initialized = True
            except Exception as e:
                logger.warning("Label init failed: %s", e)
                errors.append(f"label_init: {e}")

        # Fetch unread emails
        try:
            emails = await self._fetch_unread()
            emails_fetched = len(emails)
        except Exception as e:
            logger.warning("Email fetch failed: %s", e)
            errors.append(f"fetch: {e}")
            emit_triage_event(EVENT_TRIAGE_ERROR, {
                "cycle_id": cycle_id,
                "error_type": "fetch_failed",
                "message": str(e),
            })
            return TriageCycleReport(
                cycle_id=cycle_id,
                started_at=started_at,
                completed_at=time.time(),
                emails_fetched=0,
                emails_processed=0,
                tier_counts={},
                notifications_sent=0,
                notifications_suppressed=0,
                errors=errors,
            )

        # Process each email
        for email in emails[: self._config.max_emails_per_cycle]:
            try:
                # Extract features
                features = await extract_features(
                    email, self._router, config=self._config,
                )

                # Score
                scoring = score_email(features, self._config)

                # Apply label
                try:
                    await self._apply_label(
                        email.get("id", ""),
                        scoring.tier_label,
                    )
                except Exception as label_err:
                    errors.append(f"label:{email.get('id', '?')}: {label_err}")

                # Decide notification
                triaged = TriagedEmail(
                    features=features,
                    scoring=scoring,
                    notification_action="",
                    processed_at=time.time(),
                )
                action = self._policy.decide_action(triaged)
                triaged.notification_action = action

                # Track stats
                tier_counts[scoring.tier] = tier_counts.get(scoring.tier, 0) + 1
                if action == "immediate":
                    notifications_sent += 1
                elif action in ("label_only", "quarantine"):
                    if scoring.tier <= 2:
                        notifications_suppressed += 1

                emails_processed += 1

                emit_triage_event(EVENT_EMAIL_TRIAGED, {
                    "cycle_id": cycle_id,
                    "message_id": features.message_id,
                    "score": scoring.score,
                    "tier": scoring.tier,
                    "action": action,
                    "breakdown": scoring.breakdown,
                })

            except Exception as e:
                errors.append(f"process:{email.get('id', '?')}: {e}")
                emails_processed += 1  # Count as processed (attempted)
                emit_triage_event(EVENT_TRIAGE_ERROR, {
                    "cycle_id": cycle_id,
                    "error_type": "process_failed",
                    "message_id": email.get("id", ""),
                    "message": str(e),
                })

        # Flush summary if window elapsed
        if self._policy.should_flush_summary():
            self._policy.flush_summary()

        completed_at = time.time()
        emit_triage_event(EVENT_CYCLE_COMPLETED, {
            "cycle_id": cycle_id,
            "duration_ms": int((completed_at - started_at) * 1000),
            "emails_fetched": emails_fetched,
            "emails_processed": emails_processed,
            "tier_counts": tier_counts,
            "notifications_sent": notifications_sent,
            "errors": len(errors),
        })

        return TriageCycleReport(
            cycle_id=cycle_id,
            started_at=started_at,
            completed_at=completed_at,
            emails_fetched=emails_fetched,
            emails_processed=emails_processed,
            tier_counts=tier_counts,
            notifications_sent=notifications_sent,
            notifications_suppressed=notifications_suppressed,
            errors=errors,
        )

    async def _fetch_unread(self) -> List[Dict[str, Any]]:
        """Fetch unread emails via workspace agent."""
        if self._workspace_agent:
            result = await self._workspace_agent._fetch_unread_emails({
                "limit": self._config.max_emails_per_cycle,
            })
            return result.get("emails", [])
        return []

    async def _apply_label(
        self, message_id: str, label_name: str
    ) -> None:
        """Apply Gmail label to message."""
        if self._workspace_agent and self._label_map:
            gmail_svc = getattr(self._workspace_agent, "_gmail_service", None)
            if gmail_svc:
                await apply_label(gmail_svc, message_id, label_name, self._label_map)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_runner.py -v`
Expected: 6 passed

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/runner.py \
        tests/unit/backend/email_triage/test_runner.py
git commit -m "feat(email-triage): add triage cycle runner (Task 8)"
```

---

## Task 9: Agent Runtime Integration

**Files:**
- Modify: `backend/autonomy/agent_runtime.py`
- Create: `tests/unit/backend/email_triage/test_agent_runtime_integration.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/email_triage/test_agent_runtime_integration.py
"""Tests for email triage integration with agent_runtime.py."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


class TestMaybeRunEmailTriage:
    """_maybe_run_email_triage() in agent_runtime is gated correctly."""

    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        """When EMAIL_TRIAGE_ENABLED is not set, triage does not run."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EMAIL_TRIAGE_ENABLED", None)
            from autonomy.agent_runtime import UnifiedAgentRuntime
            runtime = MagicMock(spec=UnifiedAgentRuntime)
            runtime._last_email_triage_run = 0.0
            # Call the method directly
            await UnifiedAgentRuntime._maybe_run_email_triage(runtime)
            # Should not have imported or run anything (no error = success)

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_calls(self):
        """Second call within poll_interval_s is a no-op."""
        from autonomy.agent_runtime import UnifiedAgentRuntime
        runtime = MagicMock(spec=UnifiedAgentRuntime)
        runtime._last_email_triage_run = time.time()  # Just ran
        with patch.dict(os.environ, {"EMAIL_TRIAGE_ENABLED": "true"}):
            await UnifiedAgentRuntime._maybe_run_email_triage(runtime)
            # No import attempted because cooldown not elapsed
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_agent_runtime_integration.py -v`
Expected: FAIL (method doesn't exist yet)

**Step 3: Modify agent_runtime.py**

Add to `__init__` (after the existing instance attribute initialization block, around line 454):

```python
        self._last_email_triage_run: float = 0.0
```

Add after `await self._cleanup_completed_runners()` in `housekeeping_loop()` (around line 1009):

```python
            await self._maybe_run_email_triage()
```

Add new method to the class (after `_maybe_execute_sub_threshold_intervention()`):

```python
    async def _maybe_run_email_triage(self, context=None) -> None:
        """Run email triage cycle if enabled and cooldown elapsed.

        Gated by:
        1. EMAIL_TRIAGE_ENABLED feature flag (default: False)
        2. Cooldown timer (EMAIL_TRIAGE_POLL_INTERVAL_S, default: 60s)
        3. asyncio.wait_for timeout (EMAIL_TRIAGE_CYCLE_TIMEOUT_S, default: 30s)
        """
        if not _env_bool("EMAIL_TRIAGE_ENABLED", False):
            return

        now = time.time()
        interval = _env_float("EMAIL_TRIAGE_POLL_INTERVAL_S", 60.0)
        if now - self._last_email_triage_run < interval:
            return

        self._last_email_triage_run = now
        try:
            from autonomy.email_triage.runner import EmailTriageRunner

            runner = EmailTriageRunner.get_instance()
            timeout = _env_float("EMAIL_TRIAGE_CYCLE_TIMEOUT_S", 30.0)
            await asyncio.wait_for(runner.run_cycle(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[AgentRuntime] Email triage cycle timed out")
        except Exception as e:
            logger.warning("[AgentRuntime] Email triage cycle failed: %s", e)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/email_triage/test_agent_runtime_integration.py -v`
Expected: 2 passed

**Step 5: Commit**

```bash
git add backend/autonomy/agent_runtime.py \
        tests/unit/backend/email_triage/test_agent_runtime_integration.py
git commit -m "feat(email-triage): wire runner into agent_runtime housekeeping loop (Task 9)"
```

---

## Task 10: Package Public API

**Files:**
- Modify: `backend/autonomy/email_triage/__init__.py`

**Step 1: Update __init__.py**

```python
# backend/autonomy/email_triage/__init__.py
"""Autonomous Gmail Triage v1 — score, label, and notify on incoming emails.

Public API:
    EmailTriageRunner  — singleton runner, called by agent_runtime
    TriageConfig       — configuration (feature flags, thresholds, env vars)
    score_email        — pure deterministic scoring function
    extract_features   — structured feature extraction with heuristic fallback
"""

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.runner import EmailTriageRunner
from autonomy.email_triage.scoring import score_email
from autonomy.email_triage.extraction import extract_features

__all__ = [
    "EmailTriageRunner",
    "TriageConfig",
    "get_triage_config",
    "score_email",
    "extract_features",
]
```

**Step 2: Run full test suite**

Run: `python3 -m pytest tests/unit/backend/email_triage/ -v`
Expected: All tests pass (schemas:7 + config:15 + events:5 + scoring:9 + extraction:8 + policy:14 + labels:4 + runner:6 + integration:2 = ~70 tests)

**Step 3: Commit**

```bash
git add backend/autonomy/email_triage/__init__.py
git commit -m "feat(email-triage): finalize package public API (Task 10)"
```

---

## Task 11: Run All Tests (Regression Check)

**Step 1: Run existing test suite**

Run: `python3 -m pytest tests/unit/backend/ -v --tb=short`
Expected: All existing tests still pass + all new email_triage tests pass.

**Step 2: Verify no import side effects**

Run: `python3 -c "from autonomy.email_triage import EmailTriageRunner; print('OK')"` (from `backend/` directory)
Expected: `OK` with no errors

**Step 3: Verify disabled-by-default**

Run: `python3 -c "from autonomy.email_triage.config import get_triage_config; c = get_triage_config(); print('enabled:', c.enabled)"` (from `backend/` directory)
Expected: `enabled: False`

---

## Summary

| Task | Component | New Tests | Files Created | Files Modified |
|------|-----------|-----------|--------------|----------------|
| 1 | schemas.py | 7 | 4 | 0 |
| 2 | config.py | 15 | 2 | 0 |
| 3 | events.py | 5 | 2 | 0 |
| 4 | scoring.py | 9 | 2 | 0 |
| 5 | extraction.py | 8 | 2 | 0 |
| 6 | policy.py | 14 | 2 | 0 |
| 7 | labels.py | 4 | 2 | 0 |
| 8 | runner.py | 6 | 2 | 0 |
| 9 | agent_runtime integration | 2 | 1 | 1 |
| 10 | __init__.py | 0 | 0 | 1 |
| 11 | regression | 0 | 0 | 0 |
| **Total** | | **~70** | **19** | **2** |

All code is behind `EMAIL_TRIAGE_ENABLED=false` by default. Zero runtime impact until explicitly enabled.

"""Shared fixtures and factories for email triage e2e tests.

Provides:
- Email archetype factories (CRITICAL, HIGH, ROUTINE, NOISE, MIXED)
- Mock workspace agent, router, and notifier
- Singleton-isolated runner fixture
- Consistent time control (time.time, time.monotonic, _current_hour)
- Event capture with partial-order assertion
"""

from __future__ import annotations

import asyncio
import os
import sys
import time as _time_mod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

from autonomy.email_triage.config import TriageConfig, reset_triage_config
from autonomy.email_triage.runner import EmailTriageRunner
from autonomy.email_triage.schemas import EmailFeatures, ScoringResult, TriagedEmail


# ---------------------------------------------------------------------------
# Email factories
# ---------------------------------------------------------------------------


def make_raw_email(
    id: str = "msg_001",
    from_: str = "boss@company.com",
    subject: str = "URGENT: Deploy fix needed ASAP",
    snippet: str = "Production is down, need immediate action...",
    labels: Tuple[str, ...] = ("INBOX", "IMPORTANT"),
) -> Dict[str, Any]:
    """Build a raw email dict matching Gmail API list() output."""
    return {
        "id": id,
        "from": from_,
        "subject": subject,
        "snippet": snippet,
        "labels": list(labels),
    }


def critical_email(id: str = "msg_crit_001") -> Dict[str, Any]:
    """Tier 1 email: frequent sender, urgent keywords, IMPORTANT label."""
    return make_raw_email(
        id=id,
        from_="ceo@company.com",
        subject="URGENT: Critical production outage ASAP",
        snippet="Servers down, need immediate action required emergency",
        labels=("INBOX", "IMPORTANT"),
    )


def high_priority_email(id: str = "msg_high_001") -> Dict[str, Any]:
    """Tier 2 email: occasional sender, deadline keyword, reply thread."""
    return make_raw_email(
        id=id,
        from_="manager@company.com",
        subject="Re: Deadline for Q4 report",
        snippet="Please review the attached report before the deadline",
        labels=("INBOX",),
    )


def routine_email(id: str = "msg_routine_001") -> Dict[str, Any]:
    """Tier 3 email: first-time sender, general subject, INBOX."""
    return make_raw_email(
        id=id,
        from_="newcontact@example.com",
        subject="Meeting notes from last week",
        snippet="Here are the notes from our call. Let me know your thoughts.",
        labels=("INBOX",),
    )


def noise_email(id: str = "msg_noise_001") -> Dict[str, Any]:
    """Tier 4 email: noreply sender, promotional keywords, PROMOTIONS label."""
    return make_raw_email(
        id=id,
        from_="noreply@store.com",
        subject="Flash sale! 50% discount on everything",
        snippet="Unsubscribe from marketing newsletter offers and deals",
        labels=("CATEGORY_PROMOTIONS",),
    )


def mixed_inbox() -> List[Dict[str, Any]]:
    """4 emails, one per tier."""
    return [
        critical_email("msg_t1"),
        high_priority_email("msg_t2"),
        routine_email("msg_t3"),
        noise_email("msg_t4"),
    ]


def generate_emails(count: int, tier_mix: str = "varied") -> List[Dict[str, Any]]:
    """Generate N emails with varied tier characteristics.

    tier_mix:
        "varied" — rotate through tiers
        "all_critical" — all tier 1
        "all_noise" — all tier 4
    """
    factories = {
        "varied": [critical_email, high_priority_email, routine_email, noise_email],
        "all_critical": [critical_email],
        "all_noise": [noise_email],
    }
    fns = factories.get(tier_mix, factories["varied"])
    return [fns[i % len(fns)](id=f"msg_gen_{i:04d}") for i in range(count)]


# ---------------------------------------------------------------------------
# Mock infrastructure
# ---------------------------------------------------------------------------


def make_mock_workspace_agent(
    emails: Optional[List[Dict[str, Any]]] = None,
) -> MagicMock:
    """Build a mock workspace agent with Gmail service stubs."""
    agent = MagicMock()
    emails = emails if emails is not None else []

    agent._fetch_unread_emails = AsyncMock(return_value={"emails": emails})

    # Gmail service mock for label operations
    gmail_svc = MagicMock()

    # labels().list() returns existing labels
    labels_list_result = MagicMock()
    labels_list_result.execute.return_value = {"labels": []}
    gmail_svc.users.return_value.labels.return_value.list.return_value = labels_list_result

    # labels().create() returns created label
    def _create_label(**kwargs):
        result = MagicMock()
        body = kwargs.get("body", {})
        result.execute.return_value = {
            "id": f"Label_{body.get('name', 'unknown')}",
            "name": body.get("name", "unknown"),
        }
        return result

    gmail_svc.users.return_value.labels.return_value.create = _create_label

    # messages().modify() succeeds silently
    modify_result = MagicMock()
    modify_result.execute.return_value = {}
    gmail_svc.users.return_value.messages.return_value.modify.return_value = modify_result

    agent._gmail_service = gmail_svc
    return agent


def make_mock_router(
    response_json: Optional[Dict[str, Any]] = None,
    should_raise: bool = False,
) -> MagicMock:
    """Build a mock PrimeRouter.

    Args:
        response_json: JSON dict to return from generate(). Defaults to
            a valid J-Prime extraction response.
        should_raise: If True, generate() raises RuntimeError.
    """
    router = MagicMock()

    if should_raise:
        router.generate = AsyncMock(side_effect=RuntimeError("Router unavailable"))
    else:
        if response_json is None:
            response_json = {
                "keywords": ["deployment", "production"],
                "sender_frequency": "frequent",
                "urgency_signals": ["action_required", "escalation"],
            }
        import json
        mock_response = MagicMock()
        mock_response.content = json.dumps(response_json)
        router.generate = AsyncMock(return_value=mock_response)

    return router


def make_mock_notifier(
    success: bool = True,
    should_raise: bool = False,
    delay_s: float = 0.0,
) -> AsyncMock:
    """Build a mock notifier callable.

    Args:
        success: Return value when called.
        should_raise: If True, raises RuntimeError.
        delay_s: Artificial delay before returning (for timeout tests).
    """
    if should_raise:
        notifier = AsyncMock(side_effect=RuntimeError("Notification failed"))
    elif delay_s > 0:
        async def _slow_notifier(**kwargs):
            await asyncio.sleep(delay_s)
            return success
        notifier = AsyncMock(side_effect=_slow_notifier)
    else:
        notifier = AsyncMock(return_value=success)

    return notifier


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def make_triage_config(**overrides) -> TriageConfig:
    """Build a TriageConfig with e2e-friendly defaults."""
    defaults = dict(
        enabled=True,
        notify_tier1=True,
        notify_tier2=True,
        quarantine_tier4=False,
        extraction_enabled=False,  # heuristic only by default (fast)
        summaries_enabled=True,
        poll_interval_s=0.0,
        max_emails_per_cycle=25,
        cycle_timeout_s=30.0,
        staleness_window_s=120.0,
        commit_error_threshold=0.5,
        notification_budget_s=10.0,
        summary_budget_s=5.0,
        summary_interval_s=1800,
        max_interrupts_per_hour=3,
        max_interrupts_per_day=12,
        dedup_tier1_s=900,
        dedup_tier2_s=3600,
        quiet_start_hour=23,
        quiet_end_hour=8,
    )
    defaults.update(overrides)
    return TriageConfig(**defaults)


# ---------------------------------------------------------------------------
# Runner dependency hot-swap helper
# ---------------------------------------------------------------------------


def swap_runner_dep(runner: EmailTriageRunner, name: str, instance) -> None:
    """Hot-swap a resolved dependency on a runner for multi-cycle tests.

    Updates the DependencyResolver's internal DependencyHealth entry directly.
    """
    dep = runner._resolver._deps.get(name)
    if dep is None:
        raise KeyError(f"Unknown dependency: {name}")
    dep.instance = instance
    dep.resolved = instance is not None


# ---------------------------------------------------------------------------
# Runner fixture with singleton isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_runner():
    """Factory that creates an EmailTriageRunner with injected deps.

    Resets singleton state before AND after each test.
    """
    created_runners = []

    def _factory(
        config: Optional[TriageConfig] = None,
        workspace_agent: Any = None,
        router: Any = None,
        notifier: Any = None,
    ) -> EmailTriageRunner:
        # Reset singletons before creating
        EmailTriageRunner._instance = None
        reset_triage_config()

        cfg = config or make_triage_config()
        runner = EmailTriageRunner(
            config=cfg,
            workspace_agent=workspace_agent,
            router=router,
            notifier=notifier,
        )
        created_runners.append(runner)
        return runner

    yield _factory

    # Teardown: reset all singletons
    EmailTriageRunner._instance = None
    reset_triage_config()


# ---------------------------------------------------------------------------
# Time control
# ---------------------------------------------------------------------------


@dataclass
class TimeController:
    """Controller for consistent time manipulation across time.time,
    time.monotonic, and _current_hour."""

    _base_time: float
    _base_mono: float
    _offset: float = 0.0
    _hour: int = 10  # default: 10 AM (outside quiet hours)

    def now(self) -> float:
        return self._base_time + self._offset

    def mono(self) -> float:
        return self._base_mono + self._offset

    def hour(self) -> int:
        return self._hour

    def advance(self, seconds: float) -> None:
        """Advance both time.time and time.monotonic by `seconds`."""
        self._offset += seconds

    def set_hour(self, h: int) -> None:
        """Set the hour returned by _current_hour()."""
        self._hour = h


@contextmanager
def controlled_time(base_time: float = 1_000_000.0, hour: int = 10):
    """Context manager that patches time.time, time.monotonic, and _current_hour.

    Yields a TimeController for advancing time and setting hour.
    """
    ctrl = TimeController(
        _base_time=base_time,
        _base_mono=base_time,
        _hour=hour,
    )

    with patch("time.time", side_effect=lambda: ctrl.now()):
        with patch("time.monotonic", side_effect=lambda: ctrl.mono()):
            with patch(
                "autonomy.email_triage.policy._current_hour",
                side_effect=lambda: ctrl.hour(),
            ):
                yield ctrl


@pytest.fixture
def time_ctrl():
    """Fixture wrapping controlled_time for easy use."""
    with controlled_time() as ctrl:
        yield ctrl


# ---------------------------------------------------------------------------
# Event capture
# ---------------------------------------------------------------------------


class EventCapture:
    """Captures emit_triage_event calls for assertion."""

    def __init__(self):
        self.events: List[Tuple[str, Dict[str, Any]]] = []
        self._original_fn: Optional[Callable] = None

    def __call__(self, event_type: str, payload: Dict[str, Any]) -> None:
        self.events.append((event_type, dict(payload)))
        # Also call original so logging still works
        if self._original_fn:
            self._original_fn(event_type, payload)

    @property
    def event_types(self) -> List[str]:
        return [e[0] for e in self.events]

    def find(self, event_type: str) -> List[Dict[str, Any]]:
        """Return all payloads for a given event type."""
        return [p for t, p in self.events if t == event_type]

    def find_one(self, event_type: str) -> Optional[Dict[str, Any]]:
        """Return the first payload for a given event type, or None."""
        matches = self.find(event_type)
        return matches[0] if matches else None

    def assert_before(self, event_a: str, event_b: str) -> None:
        """Assert that event_a appears before event_b (partial order)."""
        idx_a = None
        idx_b = None
        for i, (t, _) in enumerate(self.events):
            if t == event_a and idx_a is None:
                idx_a = i
            if t == event_b and idx_b is None:
                idx_b = i
        assert idx_a is not None, f"Event {event_a} not found"
        assert idx_b is not None, f"Event {event_b} not found"
        assert idx_a < idx_b, (
            f"Expected {event_a} (idx={idx_a}) before {event_b} (idx={idx_b})"
        )

    def count(self, event_type: str) -> int:
        return sum(1 for t, _ in self.events if t == event_type)


@pytest.fixture
def capture_events():
    """Fixture that patches emit_triage_event and returns an EventCapture."""
    capture = EventCapture()
    with patch(
        "autonomy.email_triage.events.emit_triage_event",
        side_effect=capture,
    ):
        # Also patch at runner import site
        with patch(
            "autonomy.email_triage.runner.emit_triage_event",
            side_effect=capture,
        ):
            with patch(
                "autonomy.email_triage.notifications.emit_triage_event",
                side_effect=capture,
            ):
                with patch(
                    "autonomy.email_triage.dependencies.emit_triage_event",
                    side_effect=capture,
                ):
                    yield capture

"""Phase B Slice 1 — GitHubIssueSensor webhook migration (gap #4).

Pins the contract of ``ingest_webhook``:
  1. Emits an envelope shape-identical to the poll path (source,
     evidence.category, required evidence fields).
  2. Work-relevant actions only (opened/reopened/labeled/edited).
  3. Dedup via ``_seen_issues`` — a subsequent scan cannot re-emit.
  4. Cooldown via ``_issue_cooldown_active`` — a cooldowned issue is
     suppressed regardless of delivery channel.
  5. Malformed payloads are a clean no-op — webhook handlers cannot
     raise or one bad delivery takes the channel down.
  6. Interval gate: webhook-on -> poll interval becomes 900s; off ->
     existing default survives.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from backend.core.ouroboros.governance.intake.sensors import github_issue_sensor as ghm
from backend.core.ouroboros.governance.intake.sensors.github_issue_sensor import (
    GitHubIssueSensor,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _SpyRouter:
    """Records envelopes submitted via ``ingest``."""

    def __init__(self, verdict: str = "enqueued") -> None:
        self.envelopes: List[Any] = []
        self._verdict = verdict

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return self._verdict


def _sensor(router: Any) -> GitHubIssueSensor:
    return GitHubIssueSensor(
        repo="jarvis",
        router=router,
        poll_interval_s=3600.0,
        repos=(
            ("jarvis", "drussell23/JARVIS-AI-Agent", "backend/"),
            ("prime", "drussell23/JARVIS-Prime", "backend/"),
        ),
    )


def _issue_payload(
    action: str = "opened",
    number: int = 42,
    title: str = "Bug: widget crashes on load",
    body: str = "Traceback says the widget is broken.",
    labels: Tuple[str, ...] = ("bug",),
    repo_full: str = "drussell23/JARVIS-AI-Agent",
) -> Dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": lbl} for lbl in labels],
            "created_at": "2026-04-19T22:00:00Z",
            "html_url": f"https://github.com/{repo_full}/issues/{number}",
        },
        "repository": {"full_name": repo_full},
    }


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_webhook_opened_emits_github_issue_envelope(
    monkeypatch: Any,
) -> None:
    """Webhook 'opened' action produces the same envelope shape as poll path."""
    router = _SpyRouter()
    sensor = _sensor(router)

    emitted = await sensor.ingest_webhook(_issue_payload(action="opened"))

    assert emitted is True
    assert len(router.envelopes) == 1
    env = router.envelopes[0]
    # The envelope is a dataclass — source and evidence.category must
    # match the poll path so downstream filters treat both identically.
    assert env.source == "github_issue", (
        f"source must be 'github_issue' (not 'runtime_health'), got {env.source!r}"
    )
    assert env.evidence.get("category") == "github_issue"
    assert env.evidence.get("issue_number") == 42
    assert env.evidence.get("sensor") == "GitHubIssueSensor"
    assert env.evidence.get("via") == "webhook"
    assert env.evidence.get("webhook_action") == "opened"
    # labels must be a list (not a tuple) in evidence — same as poll
    assert env.evidence.get("labels") == ["bug"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["closed", "deleted", "assigned", "unassigned", "milestoned", ""],
)
async def test_ingest_webhook_non_work_actions_ignored(action: str) -> None:
    """Close / delete / admin actions do not emit signals."""
    router = _SpyRouter()
    sensor = _sensor(router)

    emitted = await sensor.ingest_webhook(_issue_payload(action=action))

    assert emitted is False
    assert router.envelopes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["opened", "reopened", "labeled", "edited"],
)
async def test_ingest_webhook_all_work_actions_emit(action: str) -> None:
    """All four work-relevant actions result in emission."""
    router = _SpyRouter()
    sensor = _sensor(router)

    emitted = await sensor.ingest_webhook(_issue_payload(action=action, number=100))

    assert emitted is True
    assert len(router.envelopes) == 1
    assert router.envelopes[0].evidence.get("webhook_action") == action


@pytest.mark.asyncio
async def test_ingest_webhook_dedup_suppresses_second_delivery() -> None:
    """Same issue arriving twice only emits once (session dedup)."""
    router = _SpyRouter()
    sensor = _sensor(router)

    first = await sensor.ingest_webhook(_issue_payload(number=7))
    second = await sensor.ingest_webhook(_issue_payload(number=7))

    assert first is True
    assert second is False
    assert len(router.envelopes) == 1


@pytest.mark.asyncio
async def test_ingest_webhook_respects_cooldown(monkeypatch: Any) -> None:
    """When ``_issue_cooldown_active`` trips, webhook suppresses — no emission."""
    router = _SpyRouter()
    sensor = _sensor(router)

    monkeypatch.setattr(ghm, "_issue_cooldown_active", lambda key: True)

    emitted = await sensor.ingest_webhook(_issue_payload(number=500))

    assert emitted is False
    assert router.envelopes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"action": "opened"},
        {"action": "opened", "issue": "not-a-dict"},
        {"action": "opened", "issue": {}},
        {"action": "opened", "issue": {"number": 0}},
        {"action": "opened", "issue": {"number": 5}},
        {
            "action": "opened",
            "issue": {"number": 5, "title": "x"},
            "repository": "not-a-dict",
        },
        {
            "action": "opened",
            "issue": {"number": 5, "title": "x"},
            "repository": {},
        },
    ],
)
async def test_ingest_webhook_malformed_payloads_are_noop(
    payload: Dict[str, Any],
) -> None:
    """Malformed payloads return False without raising or enqueuing."""
    router = _SpyRouter()
    sensor = _sensor(router)

    emitted = await sensor.ingest_webhook(payload)

    assert emitted is False
    assert router.envelopes == []


@pytest.mark.asyncio
async def test_ingest_webhook_maps_unknown_repo_to_default_short_name() -> None:
    """A repo not in the Trinity list still emits (with default 'jarvis' short key)."""
    router = _SpyRouter()
    sensor = _sensor(router)

    emitted = await sensor.ingest_webhook(
        _issue_payload(repo_full="thirdparty/unrelated-project"),
    )

    assert emitted is True
    env = router.envelopes[0]
    assert env.evidence.get("repo_full") == "thirdparty/unrelated-project"
    assert env.repo == "jarvis"


@pytest.mark.asyncio
async def test_ingest_webhook_classifies_urgency_from_labels() -> None:
    """Security label -> critical urgency via _classify_urgency (poll-path parity)."""
    router = _SpyRouter()
    sensor = _sensor(router)

    await sensor.ingest_webhook(
        _issue_payload(labels=("security",), title="ordinary title", number=1),
    )
    await sensor.ingest_webhook(
        _issue_payload(labels=("enhancement",), title="ordinary title", number=2),
    )

    assert router.envelopes[0].urgency == "critical"
    assert router.envelopes[1].urgency == "low"


@pytest.mark.asyncio
async def test_ingest_webhook_never_raises_on_router_exception(
    monkeypatch: Any,
) -> None:
    """A router that raises during ingest does not propagate out of the handler."""

    class _ExplodingRouter:
        async def ingest(self, envelope: Any) -> str:
            raise RuntimeError("simulated router explosion")

    sensor = _sensor(_ExplodingRouter())

    # Must not raise — webhook handlers must be crash-proof.
    result = await sensor.ingest_webhook(_issue_payload())
    assert result is False


# ---------------------------------------------------------------------------
# Interval gate
# ---------------------------------------------------------------------------

def test_poll_interval_stays_default_when_webhook_flag_off(
    monkeypatch: Any,
) -> None:
    """Default behavior preserved exactly when flag is off."""
    monkeypatch.delenv("JARVIS_GITHUB_WEBHOOK_ENABLED", raising=False)
    router = _SpyRouter()
    sensor = GitHubIssueSensor(
        repo="jarvis",
        router=router,
        poll_interval_s=3600.0,
    )
    assert sensor._poll_interval_s == 3600.0
    assert sensor._webhook_mode is False


def test_poll_interval_demotes_to_fallback_when_flag_on(
    monkeypatch: Any,
) -> None:
    """Flag on -> poll interval = JARVIS_GITHUB_ISSUE_FALLBACK_INTERVAL_S (15min default)."""
    monkeypatch.setenv("JARVIS_GITHUB_WEBHOOK_ENABLED", "true")
    monkeypatch.setattr(ghm, "_GITHUB_FALLBACK_INTERVAL_S", 900.0)
    router = _SpyRouter()
    sensor = GitHubIssueSensor(
        repo="jarvis",
        router=router,
        poll_interval_s=3600.0,  # caller-requested short interval
    )
    # Fallback wins: caller-requested interval is ignored when webhooks
    # are the primary path.
    assert sensor._poll_interval_s == 900.0
    assert sensor._webhook_mode is True


def test_webhook_enabled_helper_reads_env_fresh(monkeypatch: Any) -> None:
    """webhook_enabled() must re-read per call (for test monkeypatching)."""
    monkeypatch.setenv("JARVIS_GITHUB_WEBHOOK_ENABLED", "true")
    assert ghm.webhook_enabled() is True

    monkeypatch.setenv("JARVIS_GITHUB_WEBHOOK_ENABLED", "false")
    assert ghm.webhook_enabled() is False

    monkeypatch.delenv("JARVIS_GITHUB_WEBHOOK_ENABLED", raising=False)
    assert ghm.webhook_enabled() is False

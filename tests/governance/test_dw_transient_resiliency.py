"""Dynamic 5xx Resiliency Matrix — DW transient-network absorb loop.

Mandated bulletproof #2: a transient ``upstream_error`` from the DoubleWord
API must be absorbed by the backoff-retry loop so the generation RECOVERS on a
subsequent attempt, WITHOUT the failure being mis-labeled ``terminal_quota``
and terminally tripping the session circuit breaker. This is the exact foil
from soak bt-2026-07-22-082657, where one transient upstream blip killed the
whole session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
)
from backend.core.ouroboros.governance.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    VerdictAction,
)
from backend.core.ouroboros.governance.provider_retry_classifier import (
    RetryDecision,
    classify,
)


class _DWUpstreamError(Exception):
    """Duck-typed DoubleWord upstream_error (transient) — mirrors the real
    ``DoublewordInfraError`` surface the retry loop reads."""

    def __init__(self) -> None:
        super().__init__(
            'Chat completions (stream) failed: 400 '
            '{"error":{"message":"The upstream provider rejected the request.",'
            '"code":"upstream_error"}}'
        )
        self.status_code = 400
        self.response_body = '{"code":"upstream_error"}'
        self.ratelimit_reset_ts = None


def _success_result():
    return SimpleNamespace(
        candidates=[object()], generation_duration_s=1.1, cost_usd=0.0021
    )


async def test_transient_upstream_error_is_absorbed_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tiny backoff so the test doesn't actually wait.
    monkeypatch.setenv("JARVIS_DW_TRANSIENT_BACKOFF_BASE_S", "0.01")
    monkeypatch.setenv("JARVIS_DW_TRANSIENT_BACKOFF_CAP_S", "0.02")
    monkeypatch.setenv("JARVIS_DW_TRANSIENT_MAX_RETRIES", "2")
    monkeypatch.delenv("JARVIS_JPRIME_PRIMACY", raising=False)
    monkeypatch.delenv("JARVIS_BACKGROUND_ALLOW_FALLBACK", raising=False)
    monkeypatch.delenv("FORCE_CLAUDE_BACKGROUND", raising=False)

    # tier0.generate: transient upstream_error ONCE, then success.
    tier0 = Mock()
    tier0._realtime_enabled = True
    tier0.generate = AsyncMock(
        side_effect=[_DWUpstreamError(), _success_result()]
    )

    # fallback is None → if the loop wrongly cascaded, recovery would be
    # impossible; a clean return proves the blip was absorbed on the DW lane.
    gen = CandidateGenerator(primary=Mock(), tier0=tier0, fallback=None)
    # Isolate the DW seam: skip J-Prime primacy + hosted resilience lane.
    gen._try_jprime_primacy = AsyncMock(return_value=None)
    gen._try_hosted_resilience_lane = AsyncMock(return_value=None)

    context = SimpleNamespace(
        signal_urgency="low",
        signal_source="test",
        is_read_only=False,
        op_id="op-transient-test-0001",
    )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=120)

    result = await gen._generate_background(context, deadline)

    # Recovered: the second (successful) attempt's result is returned.
    assert result is not None
    assert len(result.candidates) == 1
    # The loop RETRIED — exactly two provider calls (blip, then success).
    assert tier0.generate.await_count == 2


async def test_transient_network_never_trips_terminal_breaker() -> None:
    """A stream of TRANSIENT_NETWORK decisions (the upstream_error class) must
    NEVER drive the breaker to OPEN_TERMINAL — the containment guarantee that
    keeps one transient blip from killing the session."""
    # The upstream_error classifies TRANSIENT_NETWORK, not terminal_quota.
    decision = classify(
        failure_class="DoublewordInfraError",
        http_status=400,
        failure_message="400 upstream_error: upstream provider rejected",
    )
    assert decision is RetryDecision.TRANSIENT_NETWORK

    breaker = CircuitBreaker(op_id="op-containment-0001")
    terminal_seen = False
    for _ in range(25):
        verdict = breaker.evaluate(decision)
        if verdict.action == VerdictAction.TERMINATE_UNRESOLVED:
            terminal_seen = True
            break
        if breaker.state == CircuitState.OPEN_TERMINAL:
            terminal_seen = True
            break

    assert not terminal_seen, (
        "TRANSIENT_NETWORK must never terminally trip the session breaker"
    )
    assert breaker.state != CircuitState.OPEN_TERMINAL


async def test_429_without_retry_after_still_terminal_quota() -> None:
    """Guard the other side: a genuine quota wall (429, no Retry-After) is
    still TERMINAL_QUOTA — the resiliency matrix only reclassifies the
    transient network class, not real hard-stops."""
    assert (
        classify(None, http_status=429)
        is RetryDecision.TERMINAL_QUOTA
    )
    assert (
        classify(None, http_status=429, retry_after_present=True)
        is RetryDecision.TRANSIENT_NETWORK
    )

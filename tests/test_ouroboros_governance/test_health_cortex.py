"""Tests for HealthCortex — Trinity Consciousness real-time health aggregation.

TDD coverage for TC01, TC02, TC03, TC04, TC31 plus supporting cases.

Test index
----------
TC01  test_snapshot_aggregates_all_subsystems
TC02  test_snapshot_degrades_on_three_unknowns
TC03  test_state_transition_emits_event
TC04  test_trend_stores_rolling_window
TC31  test_health_cortex_handles_exception

Supporting:
    test_all_healthy_score_is_1
    test_one_degraded_lowers_score
    test_get_snapshot_returns_cached
    test_stop_flushes_trend
    test_start_loads_trend
    test_no_event_on_steady_state
    test_psutil_failure_resources_unknown
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.consciousness.health_cortex import (
    HealthCortex,
    _snapshot_from_json,
    _snapshot_to_json,
)
from backend.core.ouroboros.consciousness.types import (
    HealthTrend,
    TrinityHealthSnapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _healthy_subsystem_mock(state: str = "active") -> MagicMock:
    """Return a MagicMock whose .health() returns a healthy dict."""
    mock = MagicMock()
    mock.health.return_value = {"state": state, "uptime_s": 100}
    return mock


def _running_subsystem_mock(running: bool = True) -> MagicMock:
    mock = MagicMock()
    mock.health.return_value = {"running": running, "queue_depth": 0}
    return mock


def _make_cortex(
    subsystems: Dict[str, Any],
    comm: Any = None,
    poll_interval_s: float = 0.01,
    trend_path: Path = None,
    tmp_path: Path = None,
) -> HealthCortex:
    """Factory that wires tmp_path for persistence by default."""
    if trend_path is None and tmp_path is not None:
        trend_path = tmp_path / "health_trend.jsonl"
    elif trend_path is None:
        # Use a throw-away path so tests never touch the real home dir
        import tempfile, os
        trend_path = Path(tempfile.mkdtemp()) / "health_trend.jsonl"
    if comm is None:
        comm = MagicMock()
        comm.emit_heartbeat = AsyncMock()
    return HealthCortex(
        subsystems=subsystems,
        comm=comm,
        poll_interval_s=poll_interval_s,
        trend_path=trend_path,
    )


async def _run_one_poll(cortex: HealthCortex) -> None:
    """Run a single poll cycle without starting the background task."""
    await cortex._poll_once()


# ---------------------------------------------------------------------------
# TC01 — snapshot aggregates all subsystems
# ---------------------------------------------------------------------------

class TestTC01SnapshotAggregatesAllSubsystems:
    """TC01: pass 3 mock subsystems, all healthy -> snapshot populated + HEALTHY."""

    @pytest.mark.asyncio
    async def test_snapshot_aggregates_all_subsystems(self, tmp_path):
        subsystems = {
            "jarvis": _healthy_subsystem_mock("active"),
            "prime": _running_subsystem_mock(True),
            "reactor": _running_subsystem_mock(True),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)
        await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap is not None, "Snapshot must be populated after poll"

        # All three trinity slots present
        assert snap.jarvis.name == "jarvis"
        assert snap.prime.name == "prime"
        assert snap.reactor.name == "reactor"

        # All healthy
        assert snap.jarvis.status == "healthy"
        assert snap.prime.status == "healthy"
        assert snap.reactor.status == "healthy"

        assert snap.overall_verdict == "HEALTHY"
        assert snap.overall_score == pytest.approx(1.0, abs=0.15)


# ---------------------------------------------------------------------------
# TC02 — three consecutive unknowns -> DEGRADED
# ---------------------------------------------------------------------------

class TestTC02DegradedOnThreeUnknowns:
    """TC02: subsystem raises 3 times -> DEGRADED verdict."""

    @pytest.mark.asyncio
    async def test_snapshot_degrades_on_three_unknowns(self, tmp_path):
        failing = MagicMock()
        failing.health.side_effect = RuntimeError("service crashed")

        subsystems = {
            "jarvis": failing,
            "prime": _running_subsystem_mock(True),
            "reactor": _running_subsystem_mock(True),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)

        # Three consecutive failing polls
        for _ in range(3):
            await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap is not None
        assert snap.jarvis.status == "unknown"
        assert snap.overall_verdict == "DEGRADED"

    @pytest.mark.asyncio
    async def test_streak_resets_on_recovery(self, tmp_path):
        """Streak resets when subsystem recovers."""
        failing = MagicMock()
        failing.health.side_effect = RuntimeError("crash")

        subsystems = {
            "jarvis": failing,
            "prime": _running_subsystem_mock(True),
            "reactor": _running_subsystem_mock(True),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)

        for _ in range(3):
            await _run_one_poll(cortex)

        # Recover
        failing.health.side_effect = None
        failing.health.return_value = {"state": "active"}
        await _run_one_poll(cortex)

        assert cortex._unknown_streak["jarvis"] == 0


# ---------------------------------------------------------------------------
# TC03 — state transition emits CommMessage
# ---------------------------------------------------------------------------

class TestTC03StateTransitionEmitsEvent:
    """TC03: HEALTHY -> DEGRADED transition emits a HEARTBEAT message."""

    @pytest.mark.asyncio
    async def test_state_transition_emits_event(self, tmp_path):
        healthy_mock = MagicMock()
        healthy_mock.health.return_value = {"state": "active"}

        comm = MagicMock()
        comm.emit_heartbeat = AsyncMock()

        subsystems = {
            "jarvis": healthy_mock,
            "prime": _running_subsystem_mock(True),
            "reactor": _running_subsystem_mock(True),
        }
        cortex = _make_cortex(subsystems, comm=comm, tmp_path=tmp_path)

        # First poll — establishes HEALTHY as baseline (no emission on first poll)
        await _run_one_poll(cortex)
        comm.emit_heartbeat.assert_not_called()

        # Force jarvis to fail to flip to DEGRADED.
        # Reset debounce using the same far-past sentinel the cortex uses
        # internally so the next transition always passes the debounce check.
        from backend.core.ouroboros.consciousness.health_cortex import _TRANSITION_DEBOUNCE_S
        healthy_mock.health.side_effect = RuntimeError("down")
        cortex._last_heartbeat_s = -_TRANSITION_DEBOUNCE_S - 1.0

        for _ in range(3):
            await _run_one_poll(cortex)

        comm.emit_heartbeat.assert_called_once()
        call_kwargs = comm.emit_heartbeat.call_args
        assert "verdict_transition" in call_kwargs.kwargs.get("phase", "") or \
               "verdict_transition" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_debounce_suppresses_repeated_transitions(self, tmp_path):
        """Rapid oscillation does not spam heartbeats."""
        failing_mock = MagicMock()
        failing_mock.health.side_effect = RuntimeError("down")

        comm = MagicMock()
        comm.emit_heartbeat = AsyncMock()

        subsystems = {
            "jarvis": failing_mock,
            "prime": _running_subsystem_mock(True),
            "reactor": _running_subsystem_mock(True),
        }
        cortex = _make_cortex(subsystems, comm=comm, tmp_path=tmp_path)

        # Establish healthy baseline
        failing_mock.health.side_effect = None
        failing_mock.health.return_value = {"state": "active"}
        await _run_one_poll(cortex)

        # Flip to failing, allow emit by resetting debounce to far past
        from backend.core.ouroboros.consciousness.health_cortex import _TRANSITION_DEBOUNCE_S
        failing_mock.health.side_effect = RuntimeError("down")
        cortex._last_heartbeat_s = -_TRANSITION_DEBOUNCE_S - 1.0
        for _ in range(3):
            await _run_one_poll(cortex)

        first_call_count = comm.emit_heartbeat.call_count
        assert first_call_count == 1

        # Recover
        failing_mock.health.side_effect = None
        failing_mock.health.return_value = {"state": "active"}
        cortex._unknown_streak["jarvis"] = 0

        # Do NOT reset debounce — second transition should be suppressed
        await _run_one_poll(cortex)
        # Count must NOT increase (still within debounce window)
        assert comm.emit_heartbeat.call_count == first_call_count


# ---------------------------------------------------------------------------
# TC04 — trend ring-buffer respects 720-entry cap
# ---------------------------------------------------------------------------

class TestTC04TrendRollingWindow:
    """TC04: adding 720+ snapshots keeps len at 720."""

    def test_trend_stores_rolling_window(self):
        trend = HealthTrend(max_entries=720)
        for _ in range(800):
            snap = _make_minimal_snapshot()
            trend.add(snap)
        assert len(trend) == 720

    def test_trend_integration_via_cortex(self, tmp_path):
        """HealthCortex's trend is capped at 720 after many synthetic adds."""
        cortex = _make_cortex({}, tmp_path=tmp_path)
        for _ in range(750):
            cortex._trend.add(_make_minimal_snapshot())
        assert len(cortex._trend) == 720


# ---------------------------------------------------------------------------
# TC31 — exception in .health() does not crash cortex
# ---------------------------------------------------------------------------

class TestTC31HandlesException:
    """TC31: subsystem .health() raises -> status UNKNOWN, no crash."""

    @pytest.mark.asyncio
    async def test_health_cortex_handles_exception(self, tmp_path):
        exploding = MagicMock()
        exploding.health.side_effect = ValueError("boom")

        subsystems = {
            "jarvis": exploding,
            "prime": _running_subsystem_mock(True),
            "reactor": _running_subsystem_mock(True),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)

        # Must not raise
        await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap is not None
        assert snap.jarvis.status == "unknown"
        assert snap.jarvis.score == pytest.approx(0.0)
        assert "boom" in snap.jarvis.details.get("error", "")

    @pytest.mark.asyncio
    async def test_async_health_exception_handled(self, tmp_path):
        """Async .health() coroutine that raises is also handled gracefully."""
        async_mock = AsyncMock(side_effect=RuntimeError("async boom"))
        # Wrap in object with .health()
        obj = MagicMock()
        obj.health = async_mock

        cortex = _make_cortex({"jarvis": obj, "prime": _running_subsystem_mock(), "reactor": _running_subsystem_mock()}, tmp_path=tmp_path)
        await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap.jarvis.status == "unknown"


# ---------------------------------------------------------------------------
# Supporting test: all healthy score is 1.0
# ---------------------------------------------------------------------------

class TestAllHealthyScoreIsOne:
    @pytest.mark.asyncio
    async def test_all_healthy_score_is_1(self, tmp_path):
        subsystems = {
            "jarvis": _healthy_subsystem_mock("active"),
            "prime": _healthy_subsystem_mock("active"),
            "reactor": _healthy_subsystem_mock("active"),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)
        await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap is not None
        assert snap.overall_score == pytest.approx(1.0, abs=0.05)
        assert snap.overall_verdict == "HEALTHY"


# ---------------------------------------------------------------------------
# Supporting test: one degraded lowers score
# ---------------------------------------------------------------------------

class TestOneDegradedLowersScore:
    @pytest.mark.asyncio
    async def test_one_degraded_lowers_score(self, tmp_path):
        degraded_mock = MagicMock()
        degraded_mock.health.return_value = {"state": "degraded"}

        subsystems = {
            "jarvis": degraded_mock,
            "prime": _healthy_subsystem_mock("active"),
            "reactor": _healthy_subsystem_mock("active"),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)
        await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap is not None
        assert snap.overall_score < 1.0
        assert snap.overall_verdict == "DEGRADED"
        assert snap.jarvis.status == "degraded"
        assert snap.jarvis.score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Supporting test: get_snapshot returns cached without blocking
# ---------------------------------------------------------------------------

class TestGetSnapshotReturnsCached:
    @pytest.mark.asyncio
    async def test_get_snapshot_returns_cached(self, tmp_path):
        subsystems = {"jarvis": _healthy_subsystem_mock(), "prime": _running_subsystem_mock(), "reactor": _running_subsystem_mock()}
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)

        # Before any poll
        assert cortex.get_snapshot() is None

        await _run_one_poll(cortex)
        first = cortex.get_snapshot()
        assert first is not None

        # Second call returns same object without additional polling
        second = cortex.get_snapshot()
        assert second is first  # exact same object


# ---------------------------------------------------------------------------
# Supporting test: stop flushes trend to disk
# ---------------------------------------------------------------------------

class TestStopFlushesTrend:
    @pytest.mark.asyncio
    async def test_stop_flushes_trend(self, tmp_path):
        subsystems = {"jarvis": _healthy_subsystem_mock(), "prime": _running_subsystem_mock(), "reactor": _running_subsystem_mock()}
        trend_path = tmp_path / "trend.jsonl"
        cortex = _make_cortex(subsystems, trend_path=trend_path)

        await _run_one_poll(cortex)
        assert not trend_path.exists(), "File should not exist before stop"

        await cortex.stop()
        assert trend_path.exists(), "Trend file must be written on stop"

        content = trend_path.read_text()
        assert len(content.strip()) > 0

        # Verify at least one valid JSON line
        lines = [l for l in content.splitlines() if l.strip()]
        assert len(lines) >= 1
        data = json.loads(lines[0])
        assert "overall_verdict" in data


# ---------------------------------------------------------------------------
# Supporting test: start loads trend from disk
# ---------------------------------------------------------------------------

class TestStartLoadsTrend:
    @pytest.mark.asyncio
    async def test_start_loads_trend(self, tmp_path):
        trend_path = tmp_path / "trend.jsonl"

        # Pre-populate the file with 5 snapshots
        snaps = [_make_minimal_snapshot() for _ in range(5)]
        lines = [_snapshot_to_json(s) for s in snaps]
        trend_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cortex = _make_cortex({}, trend_path=trend_path)
        assert len(cortex._trend) == 0  # Not loaded yet

        await cortex.start()
        # Give the poll task a tiny moment to not interfere; stop immediately
        await cortex.stop()

        assert len(cortex._trend) >= 5, "Trend should have loaded 5 pre-populated entries"


# ---------------------------------------------------------------------------
# Supporting test: no event on steady state
# ---------------------------------------------------------------------------

class TestNoEventOnSteadyState:
    @pytest.mark.asyncio
    async def test_no_event_on_steady_state(self, tmp_path):
        """Same verdict twice in a row -> no CommMessage emitted."""
        comm = MagicMock()
        comm.emit_heartbeat = AsyncMock()

        subsystems = {
            "jarvis": _healthy_subsystem_mock("active"),
            "prime": _running_subsystem_mock(True),
            "reactor": _running_subsystem_mock(True),
        }
        cortex = _make_cortex(subsystems, comm=comm, tmp_path=tmp_path)

        # Two polls, same HEALTHY verdict
        await _run_one_poll(cortex)
        await _run_one_poll(cortex)

        comm.emit_heartbeat.assert_not_called()


# ---------------------------------------------------------------------------
# Supporting test: psutil failure -> resources marked gracefully
# ---------------------------------------------------------------------------

class TestPsutilFailureResourcesUnknown:
    @pytest.mark.asyncio
    async def test_psutil_failure_resources_unknown(self, tmp_path):
        """When psutil raises, resources are returned with zero values, no crash."""
        subsystems = {
            "jarvis": _healthy_subsystem_mock(),
            "prime": _running_subsystem_mock(),
            "reactor": _running_subsystem_mock(),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)

        with patch(
            "backend.core.ouroboros.consciousness.health_cortex._PSUTIL_AVAILABLE",
            False,
        ):
            await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap is not None
        assert snap.resources.cpu_percent == pytest.approx(0.0)
        assert snap.resources.pressure == "NORMAL"


# ---------------------------------------------------------------------------
# Additional: async subsystem callable is supported
# ---------------------------------------------------------------------------

class TestAsyncSubsystemCallable:
    @pytest.mark.asyncio
    async def test_async_callable_subsystem(self, tmp_path):
        """A subsystem registered as an async callable (not object) works."""
        async def async_health():
            return {"state": "active", "mode": "async"}

        subsystems = {
            "jarvis": async_health,
            "prime": _running_subsystem_mock(True),
            "reactor": _running_subsystem_mock(True),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)
        await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap is not None
        assert snap.jarvis.status == "healthy"


# ---------------------------------------------------------------------------
# Additional: CRITICAL verdict when 2+ subsystems at zero score
# ---------------------------------------------------------------------------

class TestCriticalVerdictTwoZeroScores:
    @pytest.mark.asyncio
    async def test_two_failed_subsystems_critical(self, tmp_path):
        failing = MagicMock()
        failing.health.side_effect = RuntimeError("down")

        subsystems = {
            "jarvis": failing,
            "prime": failing,
            "reactor": _healthy_subsystem_mock("active"),
        }
        cortex = _make_cortex(subsystems, tmp_path=tmp_path)
        await _run_one_poll(cortex)

        snap = cortex.get_snapshot()
        assert snap is not None
        assert snap.overall_verdict == "CRITICAL"


# ---------------------------------------------------------------------------
# JSONL round-trip
# ---------------------------------------------------------------------------

class TestJsonlRoundTrip:
    def test_snapshot_roundtrip(self):
        snap = _make_minimal_snapshot()
        line = _snapshot_to_json(snap)
        restored = _snapshot_from_json(line)
        assert restored is not None
        assert restored.overall_verdict == snap.overall_verdict
        assert restored.overall_score == pytest.approx(snap.overall_score)
        assert restored.jarvis.name == snap.jarvis.name

    def test_corrupt_line_returns_none(self):
        assert _snapshot_from_json("{not valid json}") is None
        assert _snapshot_from_json('{"missing_key": true}') is None


# ---------------------------------------------------------------------------
# Shared fixture helper
# ---------------------------------------------------------------------------

def _make_minimal_snapshot(verdict: str = "HEALTHY") -> TrinityHealthSnapshot:
    from backend.core.ouroboros.consciousness.types import (
        BudgetHealth, ResourceHealth, SubsystemHealth, TrustHealth,
    )
    now = _utcnow()

    def _sh(name: str) -> SubsystemHealth:
        return SubsystemHealth(name=name, status="healthy", score=1.0, details={}, polled_at_utc=now)

    return TrinityHealthSnapshot(
        timestamp_utc=now,
        overall_verdict=verdict,
        overall_score=1.0,
        jarvis=_sh("jarvis"),
        prime=_sh("prime"),
        reactor=_sh("reactor"),
        resources=ResourceHealth(cpu_percent=10.0, ram_percent=40.0, disk_percent=20.0, pressure="NORMAL"),
        budget=BudgetHealth(daily_spend_usd=0.1, iteration_spend_usd=0.01, remaining_usd=9.9),
        trust=TrustHealth(current_tier="governed", graduation_progress=0.2),
    )

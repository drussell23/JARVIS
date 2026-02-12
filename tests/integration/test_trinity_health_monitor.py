"""
Integration tests for TrinityHealthMonitor.

Tests verify:
1. HTTP health check handling (200, 500, connection refused, timeouts)
2. Heartbeat file freshness detection (fresh, stale, missing)
3. Startup grace period suppression of unhealthy status
4. Health snapshot aggregation and serialization
5. Callback dispatch on status transitions
6. Optional component graceful degradation
7. Weighted health score calculation
8. Concurrent health check parallelism
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.trinity_health_monitor import (
    ComponentHealthStatus,
    ComponentStatus,
    HealthCheckResult,
    TrinityComponent,
    TrinityHealthConfig,
    TrinityHealthMonitor,
    TrinityHealthSnapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    http_timeout: float = 2.0,
    max_heartbeat_age: float = 30.0,
    consecutive_failures: int = 3,
    jarvis_port: int = 0,
    prime_port: int = 0,
    reactor_port: int = 0,
    weights: Optional[Dict[TrinityComponent, float]] = None,
) -> TrinityHealthConfig:
    """Build a TrinityHealthConfig pointed at *tmp_path* with fast timeouts."""
    trinity_dir = tmp_path / "trinity"
    (trinity_dir / "components").mkdir(parents=True, exist_ok=True)
    (trinity_dir / "state").mkdir(parents=True, exist_ok=True)

    config = TrinityHealthConfig(
        trinity_dir=trinity_dir,
        max_heartbeat_age_seconds=max_heartbeat_age,
        heartbeat_warning_age_seconds=max_heartbeat_age * 0.75,
        http_timeout_seconds=http_timeout,
        http_retry_attempts=1,
        http_retry_delay_seconds=0.1,
        check_interval_seconds=60.0,  # long — we call check_health() manually
        broadcast_interval_seconds=60.0,
        consecutive_failures_for_unhealthy=consecutive_failures,
        jarvis_backend_port=jarvis_port,
        jarvis_prime_port=prime_port,
        reactor_core_port=reactor_port,
    )
    if weights:
        config.component_weights = weights

    return config


def _write_heartbeat(
    trinity_dir: Path,
    component: str,
    *,
    age_seconds: float = 0.0,
    status: str = "healthy",
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a heartbeat JSON file for *component* with a given age."""
    hb_dir = trinity_dir / "components"
    hb_dir.mkdir(parents=True, exist_ok=True)
    hb_file = hb_dir / f"{component}.json"
    payload = {
        "timestamp": time.time() - age_seconds,
        "status": status,
        "uptime_seconds": 1234.0,
        **(extra or {}),
    }
    hb_file.write_text(json.dumps(payload))
    return hb_file


# ---------------------------------------------------------------------------
# TestTrinityHealthMonitorHTTP
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTrinityHealthMonitorHTTP:
    """HTTP-based health checks via the mock_health_server fixture."""

    async def test_healthy_component_http_200(self, mock_health_server, tmp_path):
        """HTTP 200 from the mock server -> JARVIS Body status healthy."""
        srv = mock_health_server
        srv["set_response"](
            "/health/ready",
            status=200,
            body={"phase": "healthy", "ready": True, "uptime_seconds": 42},
        )

        config = _make_config(tmp_path, jarvis_port=srv["port"])
        monitor = TrinityHealthMonitor(config=config)

        # Patch J-Prime / Reactor / CodingCouncil so they don't interfere
        with patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mock_prime, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mock_reactor, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mock_council:
            mock_prime.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_PRIME,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )
            mock_reactor.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )
            mock_council.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )

            snapshot = await monitor.check_health()

        body = snapshot.components[TrinityComponent.JARVIS_BODY]
        assert body.status == ComponentStatus.HEALTHY
        assert body.http_response_time_ms is not None
        assert body.http_response_time_ms > 0
        assert body.consecutive_failures == 0

        await monitor.stop()

    async def test_unhealthy_component_http_500(self, mock_health_server, tmp_path):
        """HTTP 500 -> JARVIS Body treated as degraded / unhealthy."""
        srv = mock_health_server
        srv["set_response"](
            "/health/ready",
            status=500,
            body={"error": "internal failure"},
        )

        config = _make_config(tmp_path, jarvis_port=srv["port"], consecutive_failures=1)
        monitor = TrinityHealthMonitor(config=config)

        with patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mp.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_PRIME,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )

            snapshot = await monitor.check_health()

        body = snapshot.components[TrinityComponent.JARVIS_BODY]
        # With consecutive_failures=1 and the first real failure, status should
        # be UNHEALTHY (>= threshold) or at least non-healthy.
        assert body.status in (ComponentStatus.UNHEALTHY, ComponentStatus.DEGRADED)
        assert body.last_error is not None

        await monitor.stop()

    async def test_unavailable_component_connection_refused(self, tmp_path):
        """Point at a dead port -> connection error, non-healthy status."""
        # Use a port that is almost certainly not listening
        config = _make_config(tmp_path, jarvis_port=19999, http_timeout=1.0)
        monitor = TrinityHealthMonitor(config=config)

        with patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mp.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_PRIME,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )

            snapshot = await monitor.check_health()

        body = snapshot.components[TrinityComponent.JARVIS_BODY]
        assert body.status in (ComponentStatus.UNHEALTHY, ComponentStatus.DEGRADED)
        assert body.last_error is not None
        # Error should mention connection issue
        assert any(
            kw in (body.last_error or "").lower()
            for kw in ("connect", "refused", "connection", "timeout")
        )

        await monitor.stop()

    async def test_slow_response_timeout(self, mock_health_server, tmp_path):
        """Very slow response should be handled via timeout."""
        srv = mock_health_server
        srv["set_response"]("/health/ready", status=200, body={"phase": "healthy"})
        # Set latency much higher than the configured HTTP timeout
        srv["set_latency"]("/health/ready", 5.0)

        config = _make_config(tmp_path, jarvis_port=srv["port"], http_timeout=0.5)
        monitor = TrinityHealthMonitor(config=config)

        with patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mp.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_PRIME,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL,
                status=ComponentStatus.OPTIONAL_OFFLINE,
            )

            start = time.monotonic()
            snapshot = await monitor.check_health()
            elapsed = time.monotonic() - start

        body = snapshot.components[TrinityComponent.JARVIS_BODY]
        # Should have timed out rather than waiting the full 5s
        assert elapsed < 4.0, f"Should have timed out quickly, took {elapsed:.1f}s"
        # Status should not be HEALTHY since we timed out
        assert body.status != ComponentStatus.HEALTHY
        assert body.last_error is not None

        await monitor.stop()

    async def test_recovery_detection(self, mock_health_server, tmp_path):
        """Start unhealthy (500), switch to healthy (200), verify transition."""
        srv = mock_health_server
        config = _make_config(tmp_path, jarvis_port=srv["port"], consecutive_failures=1)
        monitor = TrinityHealthMonitor(config=config)

        async def _stub_offline(component, status=ComponentStatus.OPTIONAL_OFFLINE):
            return ComponentHealthStatus(component=component, status=status)

        with patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mp.return_value = await _stub_offline(TrinityComponent.JARVIS_PRIME)
            mr.return_value = await _stub_offline(TrinityComponent.REACTOR_CORE)
            mc.return_value = await _stub_offline(TrinityComponent.CODING_COUNCIL)

            # Phase 1: unhealthy
            srv["set_response"]("/health/ready", status=500, body={"error": "down"})
            snap1 = await monitor.check_health()
            status1 = snap1.components[TrinityComponent.JARVIS_BODY].status
            assert status1 != ComponentStatus.HEALTHY

            # Phase 2: recover
            srv["set_response"](
                "/health/ready",
                status=200,
                body={"phase": "healthy", "ready": True, "uptime_seconds": 10},
            )
            snap2 = await monitor.check_health()
            status2 = snap2.components[TrinityComponent.JARVIS_BODY].status
            assert status2 == ComponentStatus.HEALTHY

        await monitor.stop()

    async def test_concurrent_health_checks(self, mock_health_server, tmp_path):
        """Multiple components checked in parallel should overlap."""
        srv = mock_health_server
        srv["set_response"]("/health/ready", status=200, body={"phase": "healthy", "ready": True})
        # Add a 0.3s latency to simulate slow responses
        srv["set_latency"]("/health/ready", 0.3)

        config = _make_config(tmp_path, jarvis_port=srv["port"], http_timeout=2.0)
        monitor = TrinityHealthMonitor(config=config)

        # Write heartbeat files for the heartbeat-checked components
        _write_heartbeat(config.trinity_dir, "jarvis_prime", age_seconds=1.0)
        _write_heartbeat(config.trinity_dir, "reactor_core", age_seconds=1.0)
        _write_heartbeat(config.trinity_dir, "coding_council", age_seconds=1.0)

        start = time.monotonic()
        snapshot = await monitor.check_health()
        elapsed = time.monotonic() - start

        # All 4 components are checked in parallel via asyncio.gather.
        # If they ran serially, the HTTP check alone would be >= 0.3s,
        # plus heartbeat checks. Parallel should keep total under 1.5s.
        assert elapsed < 2.0, f"Checks took {elapsed:.1f}s, expected parallel execution"
        # We should have results for at least JARVIS_BODY
        assert TrinityComponent.JARVIS_BODY in snapshot.components

        await monitor.stop()


# ---------------------------------------------------------------------------
# TestTrinityHealthMonitorHeartbeat
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTrinityHealthMonitorHeartbeat:
    """Heartbeat-file based health checks."""

    async def test_fresh_heartbeat_healthy(self, tmp_path):
        """Heartbeat file with recent timestamp -> healthy."""
        config = _make_config(tmp_path, max_heartbeat_age=30.0, jarvis_port=19999)
        _write_heartbeat(config.trinity_dir, "jarvis_prime", age_seconds=2.0)

        monitor = TrinityHealthMonitor(config=config)

        # Only test J-Prime heartbeat; patch the others
        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_BODY, status=ComponentStatus.HEALTHY
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE, status=ComponentStatus.OPTIONAL_OFFLINE
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL, status=ComponentStatus.OPTIONAL_OFFLINE
            )

            snapshot = await monitor.check_health()

        prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
        assert prime.status == ComponentStatus.HEALTHY
        assert prime.heartbeat_age_seconds is not None
        assert prime.heartbeat_age_seconds < 30.0
        assert prime.consecutive_failures == 0

        await monitor.stop()

    async def test_stale_heartbeat_detection(self, tmp_path):
        """Heartbeat file with old timestamp (>threshold) -> unhealthy."""
        stale_threshold = 10.0
        config = _make_config(tmp_path, max_heartbeat_age=stale_threshold, jarvis_port=19999)
        # Write a heartbeat that is 200 seconds old — well past any threshold
        _write_heartbeat(config.trinity_dir, "jarvis_prime", age_seconds=200.0)

        monitor = TrinityHealthMonitor(config=config)

        # Patch get_heartbeat_threshold so orchestration config doesn't override
        with patch.object(config, "get_heartbeat_threshold", return_value=stale_threshold), \
             patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_BODY, status=ComponentStatus.HEALTHY
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE, status=ComponentStatus.OPTIONAL_OFFLINE
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL, status=ComponentStatus.OPTIONAL_OFFLINE
            )

            snapshot = await monitor.check_health()

        prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
        assert prime.status == ComponentStatus.UNHEALTHY
        assert prime.last_error is not None
        assert "stale" in prime.last_error.lower() or ">" in prime.last_error

        await monitor.stop()

    async def test_missing_heartbeat_unknown(self, tmp_path):
        """No heartbeat file at all -> OPTIONAL_OFFLINE (never seen)."""
        config = _make_config(tmp_path, max_heartbeat_age=30.0, jarvis_port=19999)
        # Do NOT write any heartbeat file for J-Prime

        monitor = TrinityHealthMonitor(config=config)

        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_BODY, status=ComponentStatus.HEALTHY
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE, status=ComponentStatus.OPTIONAL_OFFLINE
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL, status=ComponentStatus.OPTIONAL_OFFLINE
            )

            snapshot = await monitor.check_health()

        prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
        # v125.0: never-seen heartbeat -> OPTIONAL_OFFLINE
        assert prime.status == ComponentStatus.OPTIONAL_OFFLINE

        await monitor.stop()

    async def test_grace_period_suppresses_unhealthy(self, tmp_path):
        """During startup grace period, stale heartbeat -> STARTING, not UNHEALTHY."""
        stale_threshold = 10.0
        config = _make_config(tmp_path, max_heartbeat_age=stale_threshold, jarvis_port=19999)
        # Write a stale heartbeat so it would normally be UNHEALTHY
        _write_heartbeat(config.trinity_dir, "jarvis_prime", age_seconds=200.0)

        # Record startup so grace period is active
        config.record_component_startup(TrinityComponent.JARVIS_PRIME)

        monitor = TrinityHealthMonitor(config=config)

        # Patch get_heartbeat_threshold + grace period check
        with patch.object(config, "get_heartbeat_threshold", return_value=stale_threshold), \
             patch.object(config, "is_in_startup_grace_period", return_value=True), \
             patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_BODY, status=ComponentStatus.HEALTHY
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE, status=ComponentStatus.OPTIONAL_OFFLINE
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL, status=ComponentStatus.OPTIONAL_OFFLINE
            )

            snapshot = await monitor.check_health()

        prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
        # During grace period the monitor must NOT mark as UNHEALTHY
        assert prime.status == ComponentStatus.STARTING
        assert "grace period" in (prime.last_error or "").lower()

        await monitor.stop()


# ---------------------------------------------------------------------------
# TestTrinityHealthSnapshot
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTrinityHealthSnapshot:
    """Health snapshot aggregation and serialization."""

    async def test_aggregate_health_all_healthy(self, tmp_path):
        """All components healthy -> overall healthy."""
        config = _make_config(tmp_path, jarvis_port=19999)
        monitor = TrinityHealthMonitor(config=config)

        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_BODY, status=ComponentStatus.HEALTHY
            )
            mp.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_PRIME, status=ComponentStatus.HEALTHY
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE, status=ComponentStatus.HEALTHY
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL, status=ComponentStatus.HEALTHY
            )

            snapshot = await monitor.check_health()

        assert snapshot.overall_status == ComponentStatus.HEALTHY
        assert snapshot.health_score >= 0.7  # should be high

        await monitor.stop()

    async def test_aggregate_health_one_down(self, tmp_path):
        """One required component unhealthy -> system degraded or unhealthy."""
        config = _make_config(tmp_path, jarvis_port=19999)
        monitor = TrinityHealthMonitor(config=config)

        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            # JARVIS Body is unhealthy -- the critical component
            mb.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_BODY, status=ComponentStatus.UNHEALTHY
            )
            mp.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_PRIME, status=ComponentStatus.HEALTHY
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE, status=ComponentStatus.HEALTHY
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL, status=ComponentStatus.HEALTHY
            )

            snapshot = await monitor.check_health()

        # JARVIS Body unhealthy -> overall UNHEALTHY (critical component)
        assert snapshot.overall_status == ComponentStatus.UNHEALTHY

        await monitor.stop()

    async def test_snapshot_serializable(self, tmp_path):
        """Snapshot can be converted to dict and JSON-serialized."""
        config = _make_config(tmp_path, jarvis_port=19999)
        monitor = TrinityHealthMonitor(config=config)

        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_BODY, status=ComponentStatus.HEALTHY
            )
            mp.return_value = ComponentHealthStatus(
                component=TrinityComponent.JARVIS_PRIME, status=ComponentStatus.DEGRADED,
                last_error="voice model loading",
            )
            mr.return_value = ComponentHealthStatus(
                component=TrinityComponent.REACTOR_CORE, status=ComponentStatus.OPTIONAL_OFFLINE
            )
            mc.return_value = ComponentHealthStatus(
                component=TrinityComponent.CODING_COUNCIL, status=ComponentStatus.OPTIONAL_OFFLINE
            )

            snapshot = await monitor.check_health()

        d = snapshot.to_dict()
        assert isinstance(d, dict)
        assert "overall_status" in d
        assert "health_score" in d
        assert "components" in d

        # Must be JSON-serializable without error
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        roundtrip = json.loads(serialized)
        assert roundtrip["overall_status"] == d["overall_status"]

        await monitor.stop()


# ---------------------------------------------------------------------------
# TestHealthMonitorCallbacks
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestHealthMonitorCallbacks:
    """Callback dispatch on health and component status transitions."""

    async def test_status_transition_callback(self, tmp_path):
        """Register a component callback, trigger state change, verify it fires."""
        config = _make_config(tmp_path, jarvis_port=19999)
        monitor = TrinityHealthMonitor(config=config)

        transitions: List[Tuple[TrinityComponent, ComponentStatus, ComponentStatus]] = []

        def _on_change(component, old_status, new_status):
            transitions.append((component, old_status, new_status))

        monitor.register_component_callback(_on_change)

        async def _stub(component, status):
            return ComponentHealthStatus(component=component, status=status)

        # First check: all UNKNOWN -> HEALTHY triggers transitions
        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = await _stub(TrinityComponent.JARVIS_BODY, ComponentStatus.HEALTHY)
            mp.return_value = await _stub(TrinityComponent.JARVIS_PRIME, ComponentStatus.HEALTHY)
            mr.return_value = await _stub(TrinityComponent.REACTOR_CORE, ComponentStatus.HEALTHY)
            mc.return_value = await _stub(TrinityComponent.CODING_COUNCIL, ComponentStatus.HEALTHY)

            await monitor.check_health()

        # All should have transitioned from UNKNOWN -> HEALTHY
        assert len(transitions) >= 1  # at least one transition fired
        # Verify at least JARVIS_BODY transitioned
        body_transitions = [
            (old, new) for comp, old, new in transitions
            if comp == TrinityComponent.JARVIS_BODY
        ]
        assert len(body_transitions) == 1
        assert body_transitions[0] == (ComponentStatus.UNKNOWN, ComponentStatus.HEALTHY)

        # Second check: change JARVIS_BODY to UNHEALTHY
        transitions.clear()

        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = await _stub(TrinityComponent.JARVIS_BODY, ComponentStatus.UNHEALTHY)
            mp.return_value = await _stub(TrinityComponent.JARVIS_PRIME, ComponentStatus.HEALTHY)
            mr.return_value = await _stub(TrinityComponent.REACTOR_CORE, ComponentStatus.HEALTHY)
            mc.return_value = await _stub(TrinityComponent.CODING_COUNCIL, ComponentStatus.HEALTHY)

            await monitor.check_health()

        body_transitions = [
            (old, new) for comp, old, new in transitions
            if comp == TrinityComponent.JARVIS_BODY
        ]
        assert len(body_transitions) == 1
        assert body_transitions[0] == (ComponentStatus.HEALTHY, ComponentStatus.UNHEALTHY)

        await monitor.stop()

    async def test_optional_component_not_blocking(self, tmp_path):
        """Optional component offline should not drag overall system status down."""
        config = _make_config(tmp_path, jarvis_port=19999)
        monitor = TrinityHealthMonitor(config=config)

        async def _stub(component, status):
            return ComponentHealthStatus(component=component, status=status)

        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = await _stub(TrinityComponent.JARVIS_BODY, ComponentStatus.HEALTHY)
            # J-Prime and Reactor-Core are optional and offline
            mp.return_value = await _stub(TrinityComponent.JARVIS_PRIME, ComponentStatus.OPTIONAL_OFFLINE)
            mr.return_value = await _stub(TrinityComponent.REACTOR_CORE, ComponentStatus.OPTIONAL_OFFLINE)
            mc.return_value = await _stub(TrinityComponent.CODING_COUNCIL, ComponentStatus.OPTIONAL_OFFLINE)

            snapshot = await monitor.check_health()

        # Optional offline components should NOT make overall system UNHEALTHY
        # Body is healthy and optional components are excluded from scoring
        assert snapshot.overall_status != ComponentStatus.UNHEALTHY
        # Health score should still be reasonable since optional components are excluded
        assert snapshot.health_score >= 0.5

        await monitor.stop()

    async def test_health_score_calculation(self, tmp_path):
        """Weighted health score correctly computed across components."""
        weights = {
            TrinityComponent.JARVIS_BODY: 1.0,
            TrinityComponent.JARVIS_PRIME: 0.7,
            TrinityComponent.REACTOR_CORE: 0.7,
            TrinityComponent.CODING_COUNCIL: 0.5,
            TrinityComponent.TRINITY_SYNC: 0.8,
        }
        config = _make_config(tmp_path, jarvis_port=19999, weights=weights)
        monitor = TrinityHealthMonitor(config=config)

        async def _stub(component, status):
            return ComponentHealthStatus(component=component, status=status)

        with patch.object(monitor, "_check_jarvis_body", new_callable=AsyncMock) as mb, \
             patch.object(monitor, "_check_jarvis_prime", new_callable=AsyncMock) as mp, \
             patch.object(monitor, "_check_reactor_core", new_callable=AsyncMock) as mr, \
             patch.object(monitor, "_check_coding_council", new_callable=AsyncMock) as mc:
            mb.return_value = await _stub(TrinityComponent.JARVIS_BODY, ComponentStatus.HEALTHY)
            mp.return_value = await _stub(TrinityComponent.JARVIS_PRIME, ComponentStatus.HEALTHY)
            mr.return_value = await _stub(TrinityComponent.REACTOR_CORE, ComponentStatus.DEGRADED)
            mc.return_value = await _stub(TrinityComponent.CODING_COUNCIL, ComponentStatus.UNHEALTHY)

            snapshot = await monitor.check_health()

        # Manually verify:
        # BODY: 1.0 * 1.0 = 1.0
        # PRIME: 0.7 * 1.0 = 0.7
        # REACTOR: 0.7 * 0.5 = 0.35  (DEGRADED = 0.5)
        # COUNCIL: 0.5 * 0.0 = 0.0  (UNHEALTHY = 0.0)
        # TRINITY_SYNC gets auto-calculated: body healthy + prime healthy -> HEALTHY
        #   so TRINITY_SYNC: 0.8 * 1.0 = 0.8
        # total_weight = 1.0 + 0.7 + 0.7 + 0.5 + 0.8 = 3.7
        # weighted_score = 1.0 + 0.7 + 0.35 + 0.0 + 0.8 = 2.85
        # score = 2.85 / 3.7 ≈ 0.770
        expected_score = 2.85 / 3.7

        assert abs(snapshot.health_score - expected_score) < 0.05, (
            f"Expected score ~{expected_score:.3f}, got {snapshot.health_score:.3f}"
        )

        await monitor.stop()

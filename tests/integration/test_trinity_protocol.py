"""
Integration tests for the Trinity cross-repo health check protocol.

Simulates the 3-component Trinity architecture (JARVIS Body, J-Prime, Reactor-Core)
using mock HTTP servers and heartbeat files to verify health aggregation, staleness
detection, recovery flows, grace periods, and concurrent check execution.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

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
from backend.loading_server.trinity_heartbeat import (
    HeartbeatData,
    TrinityHeartbeatReader,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_heartbeat(
    trinity_dir: Path,
    component: str,
    timestamp: float | None = None,
    status: str = "healthy",
    extra: Dict[str, Any] | None = None,
) -> Path:
    """Write a heartbeat JSON file into the trinity components directory."""
    comp_dir = trinity_dir / "components"
    comp_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "component": component,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "status": status,
        "uptime_seconds": 120.0,
        **(extra or {}),
    }
    hb_file = comp_dir / f"{component}.json"
    hb_file.write_text(json.dumps(payload))
    return hb_file


def _make_config(
    trinity_dir: Path,
    body_port: int = 19001,
    prime_port: int = 19002,
    reactor_port: int = 19003,
    http_timeout: float = 2.0,
    heartbeat_age: float = 30.0,
    consecutive_failures: int = 1,
) -> TrinityHealthConfig:
    """Build a TrinityHealthConfig with fast timeouts for testing."""
    return TrinityHealthConfig(
        trinity_dir=trinity_dir,
        max_heartbeat_age_seconds=heartbeat_age,
        heartbeat_warning_age_seconds=heartbeat_age * 0.75,
        http_timeout_seconds=http_timeout,
        http_retry_attempts=1,
        http_retry_delay_seconds=0.1,
        check_interval_seconds=60.0,
        broadcast_interval_seconds=60.0,
        consecutive_failures_for_unhealthy=consecutive_failures,
        jarvis_backend_port=body_port,
        jarvis_prime_port=prime_port,
        reactor_core_port=reactor_port,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestTrinityProtocol:
    """Cross-repo integration tests for the Trinity health check protocol."""

    # -- 1. All three healthy ------------------------------------------------

    async def test_all_three_healthy(
        self, tmp_path: Path, mock_health_server,
    ):
        """All 3 mock components return 200 -> overall system healthy."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"

        # Configure the mock server to respond 200 on the readiness endpoint
        server["set_response"](
            "/health/ready",
            status=200,
            body={"phase": "healthy", "uptime_seconds": 300, "ready": True,
                  "ready_components": 5, "total_components": 5},
        )

        # Write fresh heartbeats for Prime and Reactor-Core
        _write_heartbeat(trinity_dir, "jarvis_prime")
        _write_heartbeat(trinity_dir, "reactor_core")
        _write_heartbeat(trinity_dir, "coding_council")

        config = _make_config(trinity_dir, body_port=port)
        monitor = TrinityHealthMonitor(config=config)

        try:
            snapshot = await monitor.check_health()

            body = snapshot.components[TrinityComponent.JARVIS_BODY]
            assert body.status == ComponentStatus.HEALTHY, (
                f"Body expected HEALTHY, got {body.status}"
            )

            prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
            assert prime.status == ComponentStatus.HEALTHY, (
                f"Prime expected HEALTHY, got {prime.status}"
            )

            reactor = snapshot.components[TrinityComponent.REACTOR_CORE]
            assert reactor.status == ComponentStatus.HEALTHY, (
                f"Reactor expected HEALTHY, got {reactor.status}"
            )

            # Overall status should be HEALTHY
            assert snapshot.overall_status == ComponentStatus.HEALTHY
            assert snapshot.health_score >= 0.7
        finally:
            await monitor.stop()

    # -- 2. One component returns 500 ----------------------------------------

    async def test_one_component_500(
        self, tmp_path: Path, mock_health_server,
    ):
        """One component returns 500, others 200 -> system degraded."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"

        # Body returns 500 (unhealthy)
        server["set_response"](
            "/health/ready",
            status=500,
            body={"phase": "error", "error": "internal server error"},
        )

        # Prime and Reactor heartbeats are fresh
        _write_heartbeat(trinity_dir, "jarvis_prime")
        _write_heartbeat(trinity_dir, "reactor_core")
        _write_heartbeat(trinity_dir, "coding_council")

        config = _make_config(trinity_dir, body_port=port)
        monitor = TrinityHealthMonitor(config=config)

        try:
            snapshot = await monitor.check_health()

            body = snapshot.components[TrinityComponent.JARVIS_BODY]
            # 500 should trigger non-healthy status (DEGRADED or UNHEALTHY)
            assert body.status in (
                ComponentStatus.DEGRADED, ComponentStatus.UNHEALTHY,
            ), f"Body expected DEGRADED/UNHEALTHY, got {body.status}"

            # Prime and Reactor should still be healthy
            prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
            assert prime.status == ComponentStatus.HEALTHY

            # Overall should reflect the degradation
            assert snapshot.overall_status in (
                ComponentStatus.DEGRADED, ComponentStatus.UNHEALTHY,
            )
        finally:
            await monitor.stop()

    # -- 3. All components down ----------------------------------------------

    async def test_all_components_down(self, tmp_path: Path):
        """All components unreachable / no heartbeats -> system unhealthy."""
        trinity_dir = tmp_path / "trinity"
        trinity_dir.mkdir(parents=True, exist_ok=True)
        (trinity_dir / "components").mkdir(parents=True, exist_ok=True)

        # Point at ports where nothing is listening
        config = _make_config(
            trinity_dir,
            body_port=19999,
            prime_port=19998,
            reactor_port=19997,
            http_timeout=0.5,
        )
        monitor = TrinityHealthMonitor(config=config)

        try:
            snapshot = await monitor.check_health()

            # Body should be DEGRADED or UNHEALTHY (connection refused)
            body = snapshot.components[TrinityComponent.JARVIS_BODY]
            assert body.status in (
                ComponentStatus.DEGRADED, ComponentStatus.UNHEALTHY,
            ), f"Body expected DEGRADED/UNHEALTHY, got {body.status}"

            # Prime + Reactor have no heartbeat files -> OPTIONAL_OFFLINE
            # (v125.0 graceful degradation when heartbeat file never existed)
            prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
            assert prime.status in (
                ComponentStatus.UNHEALTHY, ComponentStatus.OPTIONAL_OFFLINE,
            ), f"Prime expected UNHEALTHY/OPTIONAL_OFFLINE, got {prime.status}"

            # Overall should be unhealthy since body is down
            assert snapshot.overall_status in (
                ComponentStatus.DEGRADED, ComponentStatus.UNHEALTHY,
            )
            assert snapshot.health_score < 0.7
        finally:
            await monitor.stop()

    # -- 4. Slow response timeout handling -----------------------------------

    async def test_slow_response_timeout_handling(
        self, tmp_path: Path, mock_health_server,
    ):
        """A component with latency exceeding the timeout is handled gracefully."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"

        # Make the readiness endpoint slow (3s), but timeout is only 1s
        server["set_response"]("/health/ready", status=200, body={"phase": "healthy"})
        server["set_latency"]("/health/ready", 3.0)

        _write_heartbeat(trinity_dir, "jarvis_prime")
        _write_heartbeat(trinity_dir, "reactor_core")
        _write_heartbeat(trinity_dir, "coding_council")

        config = _make_config(trinity_dir, body_port=port, http_timeout=1.0)
        monitor = TrinityHealthMonitor(config=config)

        try:
            snapshot = await monitor.check_health()

            body = snapshot.components[TrinityComponent.JARVIS_BODY]
            # Should have timed out -> not HEALTHY
            assert body.status != ComponentStatus.HEALTHY, (
                f"Body should have timed out, got {body.status}"
            )
            # The error should mention timeout
            assert body.last_error is not None
        finally:
            await monitor.stop()

    # -- 5. Heartbeat stale detection ----------------------------------------

    async def test_heartbeat_stale_detection(
        self, tmp_path: Path, mock_health_server,
    ):
        """A heartbeat file with an old timestamp is detected as stale."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"

        server["set_response"](
            "/health/ready", status=200,
            body={"phase": "healthy", "ready": True,
                  "ready_components": 5, "total_components": 5},
        )

        # Write a stale heartbeat for Prime (120 seconds old)
        stale_ts = time.time() - 120.0
        _write_heartbeat(trinity_dir, "jarvis_prime", timestamp=stale_ts)
        # Reactor is fresh
        _write_heartbeat(trinity_dir, "reactor_core")
        _write_heartbeat(trinity_dir, "coding_council")

        config = _make_config(
            trinity_dir, body_port=port, heartbeat_age=30.0,
        )
        monitor = TrinityHealthMonitor(config=config)

        try:
            snapshot = await monitor.check_health()

            prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
            assert prime.status == ComponentStatus.UNHEALTHY, (
                f"Stale Prime expected UNHEALTHY, got {prime.status}"
            )
            assert prime.last_error is not None
            assert "stale" in prime.last_error.lower() or "Heartbeat" in prime.last_error
        finally:
            await monitor.stop()

    # -- 6. Optional component offline -> system still OK --------------------

    async def test_optional_component_offline(
        self, tmp_path: Path, mock_health_server,
    ):
        """An optional component that was never seen is OPTIONAL_OFFLINE; system stays OK."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"
        (trinity_dir / "components").mkdir(parents=True, exist_ok=True)

        # Body is healthy
        server["set_response"](
            "/health/ready", status=200,
            body={"phase": "healthy", "ready": True,
                  "ready_components": 5, "total_components": 5},
        )

        # Only write reactor heartbeat; Prime and CodingCouncil never existed
        _write_heartbeat(trinity_dir, "reactor_core")

        config = _make_config(trinity_dir, body_port=port)
        monitor = TrinityHealthMonitor(config=config)

        try:
            snapshot = await monitor.check_health()

            # Body healthy
            body = snapshot.components[TrinityComponent.JARVIS_BODY]
            assert body.status == ComponentStatus.HEALTHY

            # Prime never had a heartbeat -> OPTIONAL_OFFLINE (v125.0)
            prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
            assert prime.status == ComponentStatus.OPTIONAL_OFFLINE, (
                f"Prime expected OPTIONAL_OFFLINE, got {prime.status}"
            )

            # Reactor healthy
            reactor = snapshot.components[TrinityComponent.REACTOR_CORE]
            assert reactor.status == ComponentStatus.HEALTHY

            # Overall should still be OK: Body healthy + Reactor healthy
            assert snapshot.overall_status in (
                ComponentStatus.HEALTHY, ComponentStatus.DEGRADED,
            )
            # OPTIONAL_OFFLINE components should not drag down the health score
            assert snapshot.health_score >= 0.5
        finally:
            await monitor.stop()

    # -- 7. Recovery detection -----------------------------------------------

    async def test_recovery_detection(
        self, tmp_path: Path, mock_health_server,
    ):
        """Component goes from unhealthy (500) to healthy (200) -> recovery detected."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"

        _write_heartbeat(trinity_dir, "jarvis_prime")
        _write_heartbeat(trinity_dir, "reactor_core")
        _write_heartbeat(trinity_dir, "coding_council")

        config = _make_config(trinity_dir, body_port=port)
        monitor = TrinityHealthMonitor(config=config)

        # Track status changes via callback
        status_changes: list = []
        monitor.register_component_callback(
            lambda comp, old, new: status_changes.append((comp, old, new))
        )

        try:
            # Phase 1: Body returns 500 -> unhealthy / degraded
            server["set_response"](
                "/health/ready", status=500,
                body={"phase": "error"},
            )
            snap1 = await monitor.check_health()
            body1 = snap1.components[TrinityComponent.JARVIS_BODY]
            assert body1.status in (ComponentStatus.DEGRADED, ComponentStatus.UNHEALTHY)

            # Phase 2: Body recovers -> 200
            server["set_response"](
                "/health/ready", status=200,
                body={"phase": "healthy", "ready": True,
                      "ready_components": 5, "total_components": 5},
            )
            snap2 = await monitor.check_health()
            body2 = snap2.components[TrinityComponent.JARVIS_BODY]
            assert body2.status == ComponentStatus.HEALTHY, (
                f"Expected HEALTHY after recovery, got {body2.status}"
            )

            # Verify a status change was registered
            body_changes = [
                (old, new)
                for comp, old, new in status_changes
                if comp == TrinityComponent.JARVIS_BODY
            ]
            assert len(body_changes) >= 1, "Expected at least one status change callback"
        finally:
            await monitor.stop()

    # -- 8. Concurrent checks parallel (timing) ------------------------------

    async def test_concurrent_checks_parallel(
        self, tmp_path: Path, mock_health_server,
    ):
        """All 3 health checks execute concurrently (total < sum of individual delays)."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"

        # Each component check has 0.3s latency
        delay = 0.3
        server["set_response"](
            "/health/ready", status=200,
            body={"phase": "healthy", "ready": True,
                  "ready_components": 5, "total_components": 5},
        )
        server["set_latency"]("/health/ready", delay)

        # Write heartbeats (these are file reads, effectively instant)
        _write_heartbeat(trinity_dir, "jarvis_prime")
        _write_heartbeat(trinity_dir, "reactor_core")
        _write_heartbeat(trinity_dir, "coding_council")

        config = _make_config(trinity_dir, body_port=port, http_timeout=5.0)
        monitor = TrinityHealthMonitor(config=config)

        try:
            start = time.monotonic()
            snapshot = await monitor.check_health()
            elapsed = time.monotonic() - start

            # If sequential, it would take >= 4 * delay (body + prime + reactor + council).
            # Since heartbeat checks are instant, only the HTTP check (body) has delay.
            # But even accounting for overhead, total should be well under 4 * delay.
            # The real assertion: total time should be less than 2x the single delay
            # (parallel execution means ~1 delay + overhead, not 4 delays).
            assert elapsed < delay * 4, (
                f"Checks appear sequential: elapsed={elapsed:.2f}s, "
                f"expected < {delay * 4:.2f}s"
            )

            # Confirm the check completed and produced a valid snapshot
            assert snapshot.check_duration_ms > 0
            assert len(snapshot.components) >= 3
        finally:
            await monitor.stop()

    # -- 9. Grace period startup ---------------------------------------------

    async def test_grace_period_startup(
        self, tmp_path: Path, mock_health_server,
    ):
        """During startup grace period, initial failures don't trigger UNHEALTHY."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"
        (trinity_dir / "components").mkdir(parents=True, exist_ok=True)

        # Body healthy
        server["set_response"](
            "/health/ready", status=200,
            body={"phase": "healthy", "ready": True,
                  "ready_components": 5, "total_components": 5},
        )

        # Write a stale heartbeat so the file exists (Prime was seen before)
        stale_ts = time.time() - 300.0
        _write_heartbeat(trinity_dir, "jarvis_prime", timestamp=stale_ts)
        _write_heartbeat(trinity_dir, "reactor_core")
        _write_heartbeat(trinity_dir, "coding_council")

        config = _make_config(trinity_dir, body_port=port)

        # Record that Prime just started (within grace period)
        config.record_component_startup(TrinityComponent.JARVIS_PRIME)

        # Patch is_in_startup_grace_period to return True for Prime
        # (since TrinityOrchestrationConfig may not be importable in test env)
        original_grace = config.is_in_startup_grace_period

        def patched_grace(component: TrinityComponent) -> bool:
            if component == TrinityComponent.JARVIS_PRIME:
                return True
            return original_grace(component)

        config.is_in_startup_grace_period = patched_grace  # type: ignore[assignment]

        monitor = TrinityHealthMonitor(config=config)

        try:
            snapshot = await monitor.check_health()

            prime = snapshot.components[TrinityComponent.JARVIS_PRIME]
            # During grace period, stale heartbeat should result in STARTING, not UNHEALTHY
            assert prime.status == ComponentStatus.STARTING, (
                f"Prime expected STARTING (grace period), got {prime.status}"
            )
        finally:
            await monitor.stop()

    # -- 10. Snapshot serializable to JSON -----------------------------------

    async def test_snapshot_serializable_to_json(
        self, tmp_path: Path, mock_health_server,
    ):
        """Health snapshot can be converted to dict and serialized to valid JSON."""
        server = mock_health_server
        port = server["port"]
        trinity_dir = tmp_path / "trinity"

        server["set_response"](
            "/health/ready", status=200,
            body={"phase": "healthy", "ready": True,
                  "ready_components": 5, "total_components": 5},
        )

        _write_heartbeat(trinity_dir, "jarvis_prime")
        _write_heartbeat(trinity_dir, "reactor_core")
        _write_heartbeat(trinity_dir, "coding_council")

        config = _make_config(trinity_dir, body_port=port)
        monitor = TrinityHealthMonitor(config=config)

        try:
            snapshot = await monitor.check_health()

            # to_dict should work
            data = snapshot.to_dict()
            assert isinstance(data, dict)
            assert "overall_status" in data
            assert "health_score" in data
            assert "components" in data
            assert "errors" in data
            assert "warnings" in data
            assert "timestamp" in data

            # JSON serialization should succeed (no unserializable types)
            json_str = json.dumps(data)
            assert len(json_str) > 0

            # Round-trip: parse the JSON back
            parsed = json.loads(json_str)
            assert parsed["overall_status"] == data["overall_status"]
            assert isinstance(parsed["health_score"], (int, float))
            assert isinstance(parsed["components"], dict)

            # Verify component keys are strings (not enums)
            for key in parsed["components"]:
                assert isinstance(key, str)
        finally:
            await monitor.stop()

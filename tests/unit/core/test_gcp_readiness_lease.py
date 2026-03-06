"""Tests for GCPReadinessLease — lease-based VM readiness with 3-part handshake.

Disease 10 — Startup Sequencing, Task 2.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import pytest

from backend.core.gcp_readiness_lease import (
    GCPReadinessLease,
    HandshakeResult,
    HandshakeStep,
    LeaseStatus,
    ReadinessFailureClass,
    ReadinessProber,
)


# ---------------------------------------------------------------------------
# Fake / Slow probers for testing
# ---------------------------------------------------------------------------

class FakeProber(ReadinessProber):
    """Controllable prober for testing with toggleable step outcomes."""

    def __init__(
        self,
        health_ok: bool = True,
        capabilities_ok: bool = True,
        warm_model_ok: bool = True,
    ) -> None:
        self.health_ok = health_ok
        self.capabilities_ok = capabilities_ok
        self.warm_model_ok = warm_model_ok
        self.probe_count: int = 0

    async def probe_health(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        self.probe_count += 1
        if self.health_ok:
            return HandshakeResult(step=HandshakeStep.HEALTH, passed=True)
        return HandshakeResult(
            step=HandshakeStep.HEALTH,
            passed=False,
            failure_class=ReadinessFailureClass.NETWORK,
            detail="health check failed",
        )

    async def probe_capabilities(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        self.probe_count += 1
        if self.capabilities_ok:
            return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)
        return HandshakeResult(
            step=HandshakeStep.CAPABILITIES,
            passed=False,
            failure_class=ReadinessFailureClass.SCHEMA_MISMATCH,
            detail="capabilities mismatch",
        )

    async def probe_warm_model(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        self.probe_count += 1
        if self.warm_model_ok:
            return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)
        return HandshakeResult(
            step=HandshakeStep.WARM_MODEL,
            passed=False,
            failure_class=ReadinessFailureClass.RESOURCE,
            detail="model not loaded",
        )


class SlowProber(ReadinessProber):
    """Prober that sleeps longer than the timeout on health probe."""

    def __init__(self, delay: float = 1.0) -> None:
        self.delay = delay

    async def probe_health(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        await asyncio.sleep(self.delay)
        return HandshakeResult(step=HandshakeStep.HEALTH, passed=True)

    async def probe_capabilities(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        return HandshakeResult(step=HandshakeStep.CAPABILITIES, passed=True)

    async def probe_warm_model(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        return HandshakeResult(step=HandshakeStep.WARM_MODEL, passed=True)


# ---------------------------------------------------------------------------
# TestHandshakeResult
# ---------------------------------------------------------------------------

class TestHandshakeResult:
    """Verify HandshakeResult creation and field defaults."""

    def test_passed_result(self):
        r = HandshakeResult(step=HandshakeStep.HEALTH, passed=True)
        assert r.passed is True
        assert r.step == HandshakeStep.HEALTH
        assert r.failure_class is None
        assert r.detail == ""
        assert r.data is None
        assert isinstance(r.timestamp, float)

    def test_failed_result_has_class(self):
        r = HandshakeResult(
            step=HandshakeStep.CAPABILITIES,
            passed=False,
            failure_class=ReadinessFailureClass.QUOTA,
            detail="quota exceeded",
            data={"limit": 100},
        )
        assert r.passed is False
        assert r.failure_class == ReadinessFailureClass.QUOTA
        assert r.detail == "quota exceeded"
        assert r.data == {"limit": 100}


# ---------------------------------------------------------------------------
# TestLeaseAcquisition
# ---------------------------------------------------------------------------

class TestLeaseAcquisition:
    """Verify the 3-step handshake acquisition logic."""

    async def test_full_handshake_succeeds(self):
        prober = FakeProber(health_ok=True, capabilities_ok=True, warm_model_ok=True)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        assert lease.status == LeaseStatus.INACTIVE

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)

        assert ok is True
        assert lease.status == LeaseStatus.ACTIVE
        assert lease.is_valid is True
        assert lease.host == "10.0.0.1"
        assert lease.port == 8080
        assert prober.probe_count == 3

    async def test_health_failure_blocks_lease(self):
        prober = FakeProber(health_ok=False, capabilities_ok=True, warm_model_ok=True)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)

        assert ok is False
        assert lease.status == LeaseStatus.FAILED
        assert lease.is_valid is False
        assert lease.last_failure_class == ReadinessFailureClass.NETWORK
        # Should stop after first failure — only 1 probe called
        assert prober.probe_count == 1

    async def test_capabilities_failure_blocks_lease(self):
        prober = FakeProber(health_ok=True, capabilities_ok=False, warm_model_ok=True)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)

        assert ok is False
        assert lease.status == LeaseStatus.FAILED
        assert lease.last_failure_class == ReadinessFailureClass.SCHEMA_MISMATCH
        # Health passed (1 probe), capabilities failed (1 probe) = 2 total
        assert prober.probe_count == 2

    async def test_warm_model_failure_blocks_lease(self):
        prober = FakeProber(health_ok=True, capabilities_ok=True, warm_model_ok=False)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)

        assert ok is False
        assert lease.status == LeaseStatus.FAILED
        assert lease.last_failure_class == ReadinessFailureClass.RESOURCE
        # Health (1) + capabilities (1) + warm_model (1) = 3
        assert prober.probe_count == 3


# ---------------------------------------------------------------------------
# TestLeaseLifecycle
# ---------------------------------------------------------------------------

class TestLeaseLifecycle:
    """Verify TTL expiry, refresh, revocation, and logging."""

    async def test_lease_expires_after_ttl(self):
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=0.05)  # 50ms TTL

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)
        assert ok is True
        assert lease.status == LeaseStatus.ACTIVE

        await asyncio.sleep(0.07)  # 70ms — past TTL

        assert lease.status == LeaseStatus.EXPIRED
        assert lease.is_valid is False

    async def test_refresh_extends_lease(self):
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=0.10)  # 100ms TTL

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)
        assert ok is True

        # Wait 60ms (inside TTL), then refresh
        await asyncio.sleep(0.06)
        assert lease.status == LeaseStatus.ACTIVE

        refresh_ok = await lease.refresh(timeout_per_step=5.0)
        assert refresh_ok is True

        # Wait another 60ms — should still be active because TTL was reset
        await asyncio.sleep(0.06)
        assert lease.status == LeaseStatus.ACTIVE

    async def test_refresh_fails_on_unhealthy_vm(self):
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)
        assert ok is True

        # Break health probing
        prober.health_ok = False
        refresh_ok = await lease.refresh(timeout_per_step=5.0)

        assert refresh_ok is False
        assert lease.status == LeaseStatus.FAILED

    async def test_revoke_immediately_invalidates(self):
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)
        assert ok is True
        assert lease.is_valid is True

        lease.revoke(reason="manual shutdown")

        assert lease.status == LeaseStatus.REVOKED
        assert lease.is_valid is False

    async def test_handshake_log_records_all_steps(self):
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        ok = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)
        assert ok is True

        log = lease.handshake_log
        assert len(log) == 3

        assert log[0].step == HandshakeStep.HEALTH
        assert log[0].passed is True
        assert log[1].step == HandshakeStep.CAPABILITIES
        assert log[1].passed is True
        assert log[2].step == HandshakeStep.WARM_MODEL
        assert log[2].passed is True

        # Returned list must be a copy — mutations should not affect internal state.
        log.clear()
        assert len(lease.handshake_log) == 3

    async def test_acquire_clears_previous_handshake_log(self):
        """A second acquire call must reset the log from the first attempt."""
        prober = FakeProber()
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        # First acquisition — succeeds, log has 3 entries.
        ok1 = await lease.acquire(host="10.0.0.1", port=8080, timeout_per_step=5.0)
        assert ok1 is True
        assert len(lease.handshake_log) == 3

        # Break warm_model so second acquire fails after 3 probes.
        prober.warm_model_ok = False
        ok2 = await lease.acquire(host="10.0.0.2", port=9090, timeout_per_step=5.0)
        assert ok2 is False

        # Log must reflect only the second attempt (3 entries, last one failed).
        log = lease.handshake_log
        assert len(log) == 3
        assert log[2].step == HandshakeStep.WARM_MODEL
        assert log[2].passed is False


# ---------------------------------------------------------------------------
# TestFailureClassification
# ---------------------------------------------------------------------------

class TestFailureClassification:
    """Verify failure class enum completeness and timeout classification."""

    def test_all_failure_classes_exist(self):
        expected = {
            "NETWORK", "QUOTA", "RESOURCE", "PREEMPTION",
            "SCHEMA_MISMATCH", "TIMEOUT",
        }
        actual = {member.name for member in ReadinessFailureClass}
        assert expected == actual

    async def test_timeout_failure_class(self):
        """A probe that exceeds timeout_per_step should classify as TIMEOUT."""
        prober = SlowProber(delay=1.0)
        lease = GCPReadinessLease(prober=prober, ttl_seconds=10.0)

        ok = await lease.acquire(
            host="10.0.0.1", port=8080, timeout_per_step=0.05,  # 50ms timeout
        )

        assert ok is False
        assert lease.status == LeaseStatus.FAILED
        assert lease.last_failure_class == ReadinessFailureClass.TIMEOUT

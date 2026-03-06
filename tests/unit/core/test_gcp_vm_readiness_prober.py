"""Tests for GCPVMReadinessProber — hybrid adapter delegating to GCPVMManager.

Disease 10 — Startup Sequencing, Task 5.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.gcp_readiness_lease import (
    HandshakeResult,
    HandshakeStep,
    ReadinessFailureClass,
)
from backend.core.gcp_vm_readiness_prober import GCPVMReadinessProber


# ---------------------------------------------------------------------------
# Fake VM manager & helpers
# ---------------------------------------------------------------------------

class _FakeVerdict:
    """Mimics HealthVerdict enum values for duck-type comparisons."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        if hasattr(other, "value"):
            return self.value == other.value
        return NotImplemented

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"_FakeVerdict({self.value!r})"


class FakeVMManager:
    """Controllable stand-in for GCPVMManager.

    Exposes the duck-typed interface that GCPVMReadinessProber expects:
    ``ping_health(host, port, timeout)`` and
    ``check_lineage(instance_name, vm_metadata)``.
    """

    def __init__(
        self,
        health_ready: bool = True,
        lineage_ok: bool = True,
    ) -> None:
        self._health_ready = health_ready
        self._lineage_ok = lineage_ok
        self.ping_health_calls: int = 0
        self.check_lineage_calls: int = 0

    async def ping_health(
        self, host: str, port: int, timeout: float = 10.0,
    ) -> Tuple["_FakeVerdict", Dict[str, Any]]:
        self.ping_health_calls += 1
        if self._health_ready:
            return _FakeVerdict("ready"), {"status": "ok"}
        return _FakeVerdict("unreachable"), {"status": "down"}

    async def check_lineage(
        self,
        instance_name: str,
        vm_metadata: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, str]:
        self.check_lineage_calls += 1
        if self._lineage_ok:
            return False, "lineage matches"  # should_recreate=False
        return True, "golden image mismatch"  # should_recreate=True


# ---------------------------------------------------------------------------
# TestProbeHealth
# ---------------------------------------------------------------------------

class TestProbeHealth:
    """Health probe delegation and caching."""

    @pytest.mark.asyncio
    async def test_healthy_vm_returns_passed(self) -> None:
        vm = FakeVMManager(health_ready=True)
        prober = GCPVMReadinessProber(vm)

        result = await prober.probe_health("10.0.0.1", 8080, timeout=5.0)

        assert result.passed is True
        assert result.step == HandshakeStep.HEALTH
        assert result.failure_class is None

    @pytest.mark.asyncio
    async def test_unreachable_vm_returns_failed(self) -> None:
        vm = FakeVMManager(health_ready=False)
        prober = GCPVMReadinessProber(vm)

        result = await prober.probe_health("10.0.0.1", 8080, timeout=5.0)

        assert result.passed is False
        assert result.step == HandshakeStep.HEALTH
        assert result.failure_class == ReadinessFailureClass.NETWORK

    @pytest.mark.asyncio
    async def test_health_result_is_cached(self) -> None:
        vm = FakeVMManager(health_ready=True)
        prober = GCPVMReadinessProber(vm, probe_cache_ttl=10.0)

        result1 = await prober.probe_health("10.0.0.1", 8080, timeout=5.0)
        assert result1.passed is True
        assert vm.ping_health_calls == 1

        # Flip underlying state — cached result should still be returned.
        vm._health_ready = False

        result2 = await prober.probe_health("10.0.0.1", 8080, timeout=5.0)
        assert result2.passed is True  # still cached
        assert vm.ping_health_calls == 1  # no second call

    @pytest.mark.asyncio
    async def test_health_exception_returns_network_failure(self) -> None:
        vm = FakeVMManager()
        prober = GCPVMReadinessProber(vm)

        # Make ping_health raise
        async def _explode(*a, **kw):
            raise ConnectionError("boom")

        vm.ping_health = _explode  # type: ignore[assignment]

        result = await prober.probe_health("10.0.0.1", 8080, timeout=5.0)
        assert result.passed is False
        assert result.failure_class == ReadinessFailureClass.NETWORK
        assert "boom" in result.detail


# ---------------------------------------------------------------------------
# TestProbeCapabilities
# ---------------------------------------------------------------------------

class TestProbeCapabilities:
    """Capabilities (lineage) probe delegation and caching."""

    @pytest.mark.asyncio
    async def test_matching_lineage_passes(self) -> None:
        vm = FakeVMManager(lineage_ok=True)
        prober = GCPVMReadinessProber(vm)

        result = await prober.probe_capabilities("10.0.0.1", 8080, timeout=5.0)

        assert result.passed is True
        assert result.step == HandshakeStep.CAPABILITIES
        assert result.failure_class is None

    @pytest.mark.asyncio
    async def test_mismatched_lineage_fails(self) -> None:
        vm = FakeVMManager(lineage_ok=False)
        prober = GCPVMReadinessProber(vm)

        result = await prober.probe_capabilities("10.0.0.1", 8080, timeout=5.0)

        assert result.passed is False
        assert result.step == HandshakeStep.CAPABILITIES
        assert result.failure_class == ReadinessFailureClass.SCHEMA_MISMATCH

    @pytest.mark.asyncio
    async def test_capabilities_exception_returns_network_failure(self) -> None:
        vm = FakeVMManager()
        prober = GCPVMReadinessProber(vm)

        async def _explode(*a, **kw):
            raise RuntimeError("lineage check error")

        vm.check_lineage = _explode  # type: ignore[assignment]

        result = await prober.probe_capabilities("10.0.0.1", 8080, timeout=5.0)
        assert result.passed is False
        assert result.failure_class == ReadinessFailureClass.NETWORK
        assert "lineage check error" in result.detail


# ---------------------------------------------------------------------------
# TestProbeWarmModel
# ---------------------------------------------------------------------------

class TestProbeWarmModel:
    """Warm-model probe: HTTP-based, never cached."""

    @pytest.mark.asyncio
    async def test_warm_probe_success(self) -> None:
        vm = FakeVMManager()
        prober = GCPVMReadinessProber(vm)

        # Mock aiohttp to return a 200 response
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"model": "loaded"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.core.gcp_vm_readiness_prober.aiohttp",
            create=True,
        ) as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            # Ensure the module thinks aiohttp is available
            with patch.object(prober, "_aiohttp_available", True):
                result = await prober.probe_warm_model(
                    "10.0.0.1", 8080, timeout=5.0,
                )

        assert result.passed is True
        assert result.step == HandshakeStep.WARM_MODEL
        assert result.data == {"model": "loaded"}

    @pytest.mark.asyncio
    async def test_warm_probe_timeout(self) -> None:
        vm = FakeVMManager()
        prober = GCPVMReadinessProber(vm)

        async def _slow_post(*a, **kw):
            await asyncio.sleep(100)

        mock_session = AsyncMock()
        mock_session.post = _slow_post
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.core.gcp_vm_readiness_prober.aiohttp",
            create=True,
        ) as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            with patch.object(prober, "_aiohttp_available", True):
                result = await prober.probe_warm_model(
                    "10.0.0.1", 8080, timeout=0.05,
                )

        assert result.passed is False
        assert result.step == HandshakeStep.WARM_MODEL
        assert result.failure_class == ReadinessFailureClass.TIMEOUT

    @pytest.mark.asyncio
    async def test_warm_probe_not_cached(self) -> None:
        """Warm model probe must hit the real endpoint every time (no caching)."""
        vm = FakeVMManager()
        prober = GCPVMReadinessProber(vm, probe_cache_ttl=60.0)

        call_count = 0

        async def _counting_warm_probe(host, port, timeout):
            nonlocal call_count
            call_count += 1
            return HandshakeResult(
                step=HandshakeStep.WARM_MODEL,
                passed=True,
                detail="warm",
            )

        # Monkey-patch the internal method to count calls
        prober._do_warm_model_probe = _counting_warm_probe  # type: ignore[attr-defined]

        await prober.probe_warm_model("10.0.0.1", 8080, timeout=5.0)
        await prober.probe_warm_model("10.0.0.1", 8080, timeout=5.0)

        assert call_count == 2, "warm model probe must not be cached"

    @pytest.mark.asyncio
    async def test_warm_probe_aiohttp_unavailable(self) -> None:
        """When aiohttp is not installed, probe returns NETWORK failure."""
        vm = FakeVMManager()
        prober = GCPVMReadinessProber(vm)
        prober._aiohttp_available = False

        result = await prober.probe_warm_model("10.0.0.1", 8080, timeout=5.0)

        assert result.passed is False
        assert result.failure_class == ReadinessFailureClass.NETWORK
        assert "aiohttp" in result.detail.lower()

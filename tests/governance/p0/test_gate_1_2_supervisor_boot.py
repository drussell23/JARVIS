"""Gate 1.2 — Supervisor boot contract assertion.

Rubric requirements:
- test_supervisor_boot_succeeds_on_contract_match
- test_supervisor_boot_fails_on_contract_version_mismatch
- test_supervisor_boot_fails_when_capability_endpoint_unreachable
- test_supervisor_emits_structured_reason_on_contract_failure
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, Optional


from backend.core.capability_contract import (
    CapabilityPayload,
    ModelCapability,
    EXPECTED_CONTRACT_VERSION,
)
from backend.core.supervisor_capability_checker import (
    SupervisorCapabilityChecker,
    CapabilityCheckResult,
    CapabilityCheckStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payload(contract_version: str = "1.0") -> CapabilityPayload:
    return CapabilityPayload(
        contract_version=contract_version,
        capability_schema_version="1.0",
        generated_at_utc="2026-03-10T22:00:00Z",
        models={
            "qwen-2.5-coder-7b": ModelCapability(
                loaded=True,
                context_window_size=8192,
                supports_intents=["code_generation"],
            )
        },
    )


def _make_checker(
    payload: Optional[CapabilityPayload] = None,
    fetch_raises: Optional[Exception] = None,
    endpoint_url: str = "http://fake-jprime:8000",
) -> SupervisorCapabilityChecker:
    checker = SupervisorCapabilityChecker(prime_endpoint=endpoint_url)
    if fetch_raises is not None:
        checker._fetch_capability = AsyncMock(side_effect=fetch_raises)
    else:
        checker._fetch_capability = AsyncMock(return_value=payload)
    return checker


# ---------------------------------------------------------------------------
# Gate 1.2 tests
# ---------------------------------------------------------------------------


class TestSupervisorBootSucceedsOnContractMatch:
    """test_supervisor_boot_succeeds_on_contract_match"""

    @pytest.mark.asyncio
    async def test_matching_contract_version_returns_ok(self):
        checker = _make_checker(payload=_make_payload(EXPECTED_CONTRACT_VERSION))
        result = await checker.check()
        assert result.status == CapabilityCheckStatus.OK

    @pytest.mark.asyncio
    async def test_ok_result_contains_payload(self):
        payload = _make_payload(EXPECTED_CONTRACT_VERSION)
        checker = _make_checker(payload=payload)
        result = await checker.check()
        assert result.payload is not None
        assert result.payload.contract_version == EXPECTED_CONTRACT_VERSION

    @pytest.mark.asyncio
    async def test_ok_result_has_no_reason_code(self):
        checker = _make_checker(payload=_make_payload(EXPECTED_CONTRACT_VERSION))
        result = await checker.check()
        assert result.reason_code is None


class TestSupervisorBootFailsOnContractVersionMismatch:
    """test_supervisor_boot_fails_on_contract_version_mismatch"""

    @pytest.mark.asyncio
    async def test_version_mismatch_returns_mismatch_status(self):
        checker = _make_checker(payload=_make_payload("99.0"))
        result = await checker.check()
        assert result.status == CapabilityCheckStatus.CONTRACT_VERSION_MISMATCH

    @pytest.mark.asyncio
    async def test_mismatch_is_not_ok(self):
        checker = _make_checker(payload=_make_payload("0.9"))
        result = await checker.check()
        assert result.status != CapabilityCheckStatus.OK

    @pytest.mark.asyncio
    async def test_mismatch_does_not_silently_proceed(self):
        checker = _make_checker(payload=_make_payload("2.0"))
        result = await checker.check()
        # Must not be OK — any other status is acceptable
        assert result.status != CapabilityCheckStatus.OK
        assert result.reason_code is not None

    @pytest.mark.asyncio
    async def test_empty_contract_version_fails(self):
        payload = _make_payload("")
        checker = _make_checker(payload=payload)
        result = await checker.check()
        assert result.status != CapabilityCheckStatus.OK


class TestSupervisorBootFailsWhenCapabilityEndpointUnreachable:
    """test_supervisor_boot_fails_when_capability_endpoint_unreachable"""

    @pytest.mark.asyncio
    async def test_connection_error_returns_telemetry_disconnect(self):
        import aiohttp
        checker = _make_checker(fetch_raises=aiohttp.ClientConnectorError(
            MagicMock(), OSError("Connection refused")
        ))
        result = await checker.check()
        assert result.status == CapabilityCheckStatus.TELEMETRY_DISCONNECT

    @pytest.mark.asyncio
    async def test_timeout_returns_telemetry_disconnect(self):
        import asyncio
        checker = _make_checker(fetch_raises=asyncio.TimeoutError())
        result = await checker.check()
        assert result.status == CapabilityCheckStatus.TELEMETRY_DISCONNECT

    @pytest.mark.asyncio
    async def test_unreachable_does_not_fall_back_to_local(self):
        """Hard fail — must NOT silently use local telemetry as fallback."""
        import asyncio
        checker = _make_checker(fetch_raises=asyncio.TimeoutError())
        result = await checker.check()
        # Status must be TELEMETRY_DISCONNECT, not OK with local data
        assert result.status == CapabilityCheckStatus.TELEMETRY_DISCONNECT
        assert result.payload is None  # No payload when disconnected

    @pytest.mark.asyncio
    async def test_http_500_returns_telemetry_disconnect(self):
        checker = _make_checker(fetch_raises=RuntimeError("HTTP 500 from capability endpoint"))
        result = await checker.check()
        assert result.status == CapabilityCheckStatus.TELEMETRY_DISCONNECT


class TestSupervisorEmitsStructuredReasonOnContractFailure:
    """test_supervisor_emits_structured_reason_on_contract_failure"""

    @pytest.mark.asyncio
    async def test_mismatch_emits_reason_code(self):
        checker = _make_checker(payload=_make_payload("99.0"))
        result = await checker.check()
        assert result.reason_code is not None
        assert isinstance(result.reason_code, str)
        assert len(result.reason_code) > 0

    @pytest.mark.asyncio
    async def test_disconnect_emits_reason_code(self):
        import asyncio
        checker = _make_checker(fetch_raises=asyncio.TimeoutError())
        result = await checker.check()
        assert result.reason_code is not None

    @pytest.mark.asyncio
    async def test_mismatch_reason_code_contains_version_info(self):
        checker = _make_checker(payload=_make_payload("99.0"))
        result = await checker.check()
        # Reason code must reference the version mismatch explicitly
        reason = result.reason_code or ""
        assert "CONTRACT_VERSION_MISMATCH" in reason or "99.0" in reason or "mismatch" in reason.lower()

    @pytest.mark.asyncio
    async def test_disconnect_reason_code_is_telemetry_disconnect(self):
        import asyncio
        checker = _make_checker(fetch_raises=asyncio.TimeoutError())
        result = await checker.check()
        assert "TELEMETRY_DISCONNECT" in (result.reason_code or "")

    @pytest.mark.asyncio
    async def test_result_is_structured_dataclass_not_exception(self):
        """Failures must return structured CapabilityCheckResult, never raise."""
        import asyncio
        checker = _make_checker(fetch_raises=asyncio.TimeoutError())
        # Must NOT raise — must return a structured result
        result = await checker.check()
        assert isinstance(result, CapabilityCheckResult)

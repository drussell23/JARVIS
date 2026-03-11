"""SupervisorCapabilityChecker — Boot-time J-Prime contract assertion.

Checks that J-Prime /capability endpoint is reachable and that its
contract_version matches EXPECTED_CONTRACT_VERSION.

Hard fail semantics:
  - Endpoint unreachable   → TELEMETRY_DISCONNECT
  - Version mismatch       → CONTRACT_VERSION_MISMATCH
  - Never falls back to local telemetry

Returns a structured CapabilityCheckResult — never raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from backend.core.capability_contract import (
    CapabilityPayload,
    EXPECTED_CONTRACT_VERSION,
    validate_capability_payload,
)

logger = logging.getLogger("JARVIS.SupervisorCapabilityChecker")


class CapabilityCheckStatus(Enum):
    OK = "OK"
    CONTRACT_VERSION_MISMATCH = "CONTRACT_VERSION_MISMATCH"
    TELEMETRY_DISCONNECT = "TELEMETRY_DISCONNECT"


@dataclass
class CapabilityCheckResult:
    status: CapabilityCheckStatus
    payload: Optional[CapabilityPayload] = None
    reason_code: Optional[str] = None


class SupervisorCapabilityChecker:
    """
    Checks J-Prime capability contract at supervisor boot.

    Hard fails (TELEMETRY_DISCONNECT) when endpoint unreachable.
    Hard fails (CONTRACT_VERSION_MISMATCH) when version doesn't match.
    Never falls back to local telemetry.
    """

    def __init__(self, prime_endpoint: str) -> None:
        self._prime_endpoint = prime_endpoint.rstrip("/")

    async def check(self) -> CapabilityCheckResult:
        """Fetch and validate capability payload.

        Returns a structured result — never raises.
        """
        try:
            payload = await self._fetch_capability()
        except Exception as exc:
            logger.error("[CapabilityChecker] Endpoint unreachable: %s", exc)
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.TELEMETRY_DISCONNECT,
                payload=None,
                reason_code="TELEMETRY_DISCONNECT",
            )

        if not payload or not payload.contract_version:
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.CONTRACT_VERSION_MISMATCH,
                payload=payload,
                reason_code="CONTRACT_VERSION_MISMATCH: empty version",
            )

        if payload.contract_version != EXPECTED_CONTRACT_VERSION:
            reason = (
                f"CONTRACT_VERSION_MISMATCH: "
                f"expected={EXPECTED_CONTRACT_VERSION!r} "
                f"got={payload.contract_version!r}"
            )
            logger.error("[CapabilityChecker] %s", reason)
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.CONTRACT_VERSION_MISMATCH,
                payload=payload,
                reason_code=reason,
            )

        logger.info(
            "[CapabilityChecker] Contract OK (version=%s)", payload.contract_version
        )
        return CapabilityCheckResult(
            status=CapabilityCheckStatus.OK,
            payload=payload,
            reason_code=None,
        )

    async def _fetch_capability(self) -> CapabilityPayload:
        """Fetch and parse the capability payload from J-Prime.

        Subclasses or tests may override this method for isolation.
        Raises on any network or HTTP error.
        """
        import aiohttp
        from backend.core.capability_contract import CAPABILITY_ENDPOINT_PATH

        url = f"{self._prime_endpoint}{CAPABILITY_ENDPOINT_PATH}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                raw = await resp.json()
                return validate_capability_payload(raw)

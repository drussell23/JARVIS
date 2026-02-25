# backend/core/handshake_protocol.py
"""
Handshake protocol for cross-repo component integration.

Defines versioned handshake messages, compatibility evaluation with
semver version windows, and capability checking. Components negotiate
protocol parameters (heartbeat intervals, API versions, capabilities)
via structured proposals and responses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "HandshakeProposal",
    "HandshakeResponse",
    "HandshakeManager",
    "evaluate_handshake",
    "parse_semver",
]


# ---------------------------------------------------------------------------
# Semver parsing
# ---------------------------------------------------------------------------

def parse_semver(version_str: str) -> Tuple[int, int, int]:
    """Parse a version string into (major, minor, patch).

    Accepts "1.2.3", "1.2", or "3". Missing components default to 0.
    """
    parts = str(version_str).strip().split(".")
    major = int(parts[0]) if len(parts) >= 1 and parts[0] else 0
    minor = int(parts[1]) if len(parts) >= 2 and parts[1] else 0
    patch = int(parts[2]) if len(parts) >= 3 and parts[2] else 0
    return (major, minor, patch)


def _version_tuple_le(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> bool:
    """Return True if version tuple *a* <= *b*."""
    return a <= b


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HandshakeProposal:
    """Immutable proposal sent by the supervisor to a component."""

    supervisor_epoch: int
    supervisor_instance_id: str
    expected_api_version_min: str
    expected_api_version_max: str
    required_capabilities: Tuple[str, ...]
    health_schema_hash: str
    heartbeat_interval_s: float
    heartbeat_ttl_s: float
    protocol_version: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict suitable for JSON transport."""
        return {
            "supervisor_epoch": self.supervisor_epoch,
            "supervisor_instance_id": self.supervisor_instance_id,
            "expected_api_version_min": self.expected_api_version_min,
            "expected_api_version_max": self.expected_api_version_max,
            "required_capabilities": list(self.required_capabilities),
            "health_schema_hash": self.health_schema_hash,
            "heartbeat_interval_s": self.heartbeat_interval_s,
            "heartbeat_ttl_s": self.heartbeat_ttl_s,
            "protocol_version": self.protocol_version,
        }


@dataclass(frozen=True)
class HandshakeResponse:
    """Immutable response returned by a component to the supervisor."""

    accepted: bool
    component_instance_id: str
    api_version: str
    capabilities: Tuple[str, ...]
    health_schema_hash: str
    rejection_reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HandshakeResponse":
        """Deserialize from a dict (e.g. parsed JSON)."""
        return cls(
            accepted=bool(data.get("accepted", False)),
            component_instance_id=str(data.get("component_instance_id", "")),
            api_version=str(data.get("api_version", "0.0.0")),
            capabilities=tuple(data.get("capabilities", ())),
            health_schema_hash=str(data.get("health_schema_hash", "")),
            rejection_reason=data.get("rejection_reason"),
            metadata=data.get("metadata"),
        )


# ---------------------------------------------------------------------------
# Compatibility evaluation
# ---------------------------------------------------------------------------

def evaluate_handshake(
    proposal: HandshakeProposal,
    response: HandshakeResponse,
) -> Tuple[bool, Optional[str]]:
    """Evaluate whether a handshake response is compatible with the proposal.

    Returns:
        (True, None) on success.
        (False, reason_string) on failure.
    """
    # 1. Component-side rejection
    if not response.accepted:
        reason = response.rejection_reason or "unknown"
        return False, f"component_rejected: {reason}"

    # 2. Legacy version "0.0.0" is always compatible (pre-handshake components)
    if response.api_version == "0.0.0" or parse_semver(response.api_version) == (0, 0, 0):
        logger.info(
            "Legacy component %s reported api_version 0.0.0 — accepting as compatible",
            response.component_instance_id,
        )
        return True, None

    # 3. Semver window check
    v_component = parse_semver(response.api_version)
    v_min = parse_semver(proposal.expected_api_version_min)
    v_max = parse_semver(proposal.expected_api_version_max)

    if not (_version_tuple_le(v_min, v_component) and _version_tuple_le(v_component, v_max)):
        return (
            False,
            f"version {response.api_version} outside [{proposal.expected_api_version_min}, {proposal.expected_api_version_max}]",
        )

    # 4. Required capabilities
    provided = set(response.capabilities)
    required = set(proposal.required_capabilities)
    missing = required - provided
    if missing:
        return False, f"missing_capabilities: {sorted(missing)}"

    # 5. Schema hash mismatch — warning only, not a rejection
    if proposal.health_schema_hash and response.health_schema_hash:
        if proposal.health_schema_hash != response.health_schema_hash:
            logger.warning(
                "Health schema hash mismatch for %s: proposal=%s response=%s (non-fatal)",
                response.component_instance_id,
                proposal.health_schema_hash,
                response.health_schema_hash,
            )

    return True, None


# ---------------------------------------------------------------------------
# HandshakeManager
# ---------------------------------------------------------------------------

class HandshakeManager:
    """Orchestrates handshake exchanges with remote components.

    Parameters:
        journal: An optional OrchestrationJournal instance for audit logging.
                 If unavailable (import failure, None), handshake still works
                 but journal entries are skipped.
    """

    def __init__(self, journal: Any = None) -> None:
        self._journal = journal

    # -- public API ---------------------------------------------------------

    async def perform_handshake(
        self,
        component: str,
        endpoint: str,
        proposal: HandshakeProposal,
    ) -> Tuple[bool, Optional[str]]:
        """Execute a handshake with a remote component.

        Sends an HTTP POST to ``{endpoint}/lifecycle/handshake`` carrying the
        proposal as JSON. Parses the response, evaluates compatibility, and
        returns the result.

        On HTTP 404 the manager falls back to :meth:`synthesize_legacy_response`.

        Returns:
            (ok, reason) — same semantics as :func:`evaluate_handshake`.
        """
        import aiohttp  # late import to avoid hard dependency at module level

        url = f"{endpoint.rstrip('/')}/lifecycle/handshake"
        payload = proposal.to_dict()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 404:
                        logger.info(
                            "Component %s at %s returned 404 — synthesizing legacy response",
                            component,
                            endpoint,
                        )
                        # Attempt to get health data for legacy synthesis
                        health_data = await self._fetch_health(session, endpoint)
                        legacy = await self.synthesize_legacy_response(component, health_data)
                        result = evaluate_handshake(proposal, legacy)
                        self._record_journal(component, proposal, legacy, result)
                        return result

                    if resp.status >= 400:
                        reason = f"http_error: {resp.status}"
                        self._record_journal(component, proposal, None, (False, reason))
                        return False, reason

                    data = await resp.json()
                    response = HandshakeResponse.from_dict(data)

        except Exception as exc:
            reason = f"connection_error: {type(exc).__name__}: {exc}"
            logger.error("Handshake with %s failed: %s", component, reason)
            self._record_journal(component, proposal, None, (False, reason))
            return False, reason

        result = evaluate_handshake(proposal, response)
        self._record_journal(component, proposal, response, result)
        return result

    async def synthesize_legacy_response(
        self,
        component: str,
        health_data: Dict[str, Any],
    ) -> HandshakeResponse:
        """Build a HandshakeResponse for services that lack ``/lifecycle/handshake``.

        Uses health endpoint data to infer capabilities. The synthesized
        response always reports api_version ``"0.0.0"`` (legacy fallback),
        which ``evaluate_handshake`` treats as universally compatible.
        """
        # Infer capabilities from health data keys
        capabilities: List[str] = list(health_data.get("capabilities", []))

        # Common capability inference from health keys
        if not capabilities:
            if health_data.get("model_loaded") or health_data.get("ready_for_inference"):
                capabilities.append("inference")
            if health_data.get("embedding_ready"):
                capabilities.append("embedding")
            if health_data.get("tts_ready"):
                capabilities.append("tts")
            if health_data.get("stt_ready"):
                capabilities.append("stt")

        return HandshakeResponse(
            accepted=True,
            component_instance_id=f"{component}:legacy",
            api_version="0.0.0",
            capabilities=tuple(capabilities),
            health_schema_hash="",
            rejection_reason=None,
            metadata={"legacy": True, "source": "health_endpoint"},
        )

    # -- internal helpers ---------------------------------------------------

    async def _fetch_health(self, session: Any, endpoint: str) -> Dict[str, Any]:
        """Best-effort fetch of ``/health`` from the component."""
        try:
            url = f"{endpoint.rstrip('/')}/health"
            import aiohttp
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return {}

    def _record_journal(
        self,
        component: str,
        proposal: HandshakeProposal,
        response: Optional[HandshakeResponse],
        result: Tuple[bool, Optional[str]],
    ) -> None:
        """Write an audit entry to the orchestration journal if available."""
        if self._journal is None:
            return
        try:
            ok, reason = result
            self._journal.fenced_write(
                action="handshake",
                target=component,
                payload={
                    "success": ok,
                    "reason": reason,
                    "proposal_epoch": proposal.supervisor_epoch,
                    "response_id": response.component_instance_id if response else None,
                },
            )
        except Exception:
            logger.debug("Failed to record handshake journal entry", exc_info=True)
